"""Both API boundaries receive the same facts; model-specific roles stay separate."""

import copy
import json
import unittest
from unittest.mock import patch

from mcp_agent import daily_planner
from mcp_agent import jev_client as jev_agent
from mcp_agent.model_state import model_game_state
from fixtures import grass, lit_campfire, state


class Response:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


class SharedStateTests(unittest.TestCase):
    def observed(self):
        actual = state([lit_campfire(), grass(11, 2, True)], cutgrass=4)
        actual["observed_at"] = 123.5
        actual["world"].update(phase="night", time_in_phase=0.4)
        actual["player"].update(guid=100, prefab="wilson")
        actual["player"]["vitals"].update(health_max=150, sanity_max=200)
        actual["player"]["inventory"]["items"] += [
            {"prefab": "torch", "guid": 51, "slot": 2, "count": 1, "durability_percent": 1},
            {"prefab": "pickaxe", "guid": 52, "slot": 3, "count": 1,
             "durability_percent": 0.82, "max_uses": 33, "weapon": True, "weapon_damage": 27},
            {"prefab": "carrot", "count": 2},
            {"prefab": "meat", "count": 1},
        ]
        actual["player"]["inventory"]["equipped"] = [
            {"prefab": "torch", "guid": 53, "slot": "hands", "count": 1,
             "durability_percent": 0.04},
        ]
        actual["work"] = {"action": "chop", "status": "completed", "sequence": 9,
                          "target_guid": 33}
        actual["execution"] = {"doing": False, "sequence": 10}
        actual["construction"] = {"campfire": {
            "status": "blocked", "reason": "no_safe_campfire_position",
            "combustible_clearance": 5, "combustibles_observed": 8,
        }}
        return actual

    def assert_same_facts(self, actual):
        expected = model_game_state(actual)
        planner = daily_planner.planning_context(actual, [])
        decider = jev_agent.compact_game_state(actual)
        self.assertEqual({k: planner[k] for k in expected}, expected)
        self.assertEqual({k: decider[k] for k in expected}, expected)
        return expected

    def test_fields_and_derived_signals_match_for_both_models(self):
        facts = self.assert_same_facts(self.observed())
        self.assertEqual(facts["inventory"]["torch"], 2)
        self.assertEqual(facts["inventory_details"]["equipped"][0]["durability_percent"], 0.04)
        self.assertEqual(facts["inventory_details"]["items"][2]["durability_percent"], 0.82)
        self.assertEqual(facts["state_basis"], "observed")
        self.assertEqual(facts["observed_at"], 123.5)
        self.assertEqual(facts["safe_food_count"], 2)
        self.assertEqual(facts["cookable_raw_count"], 3)
        self.assertTrue(facts["nearby_cooker"])
        self.assertTrue(facts["nearby_lit_fire"])
        self.assertTrue(facts["lighting"]["torch_equipped"])
        self.assertFalse(facts["weapon_equipped"])
        self.assertTrue(facts["weapon_carried"])
        self.assertEqual(facts["best_weapon"], "pickaxe")
        self.assertEqual(facts["work"]["target_guid"], 33)
        self.assertEqual(facts["vitals"]["sanity_max"], 200)
        self.assertEqual(facts["construction"]["campfire"], self.observed()["construction"]["campfire"])

    def test_farther_entities_are_not_filtered_for_only_one_model(self):
        actual = self.observed()
        actual["nearby"] = [grass(i, 0.1 * i, True) for i in range(25)] + [
            {"prefab": "knight", "guid": 80, "distance": 12, "tags": ["hostile"],
             "activeThreat": False, "potentialThreat": True, "attackable": True},
            {"prefab": "rock2", "guid": 81, "distance": 13, "tags": ["MINE_workable"],
             "work_required": 6},
            lit_campfire(distance=14),
        ]
        facts = self.assert_same_facts(actual)
        self.assertEqual(len(facts["nearby"]), 28)
        self.assertTrue(facts["nearby"][25]["potentialThreat"])
        self.assertEqual(facts["nearby"][26]["work_required"], 6)
        self.assertEqual(facts["nearby"][0]["collect_path"], actual["nearby"][0]["collect_path"])
        self.assertFalse(facts["nearby_lit_fire"])
        self.assertTrue(facts["lighting"]["observed_fires"][0]["lit"])

    def test_science_machine_keeps_both_semantic_and_real_prefab_ids_for_both_models(self):
        actual=self.observed()
        actual["nearby"].append({"prefab":"sciencemachine","game_prefab":"researchlab","guid":42,"distance":2})
        actual["crafting"]={"sciencemachine":True}
        facts=self.assert_same_facts(actual)
        science=next(e for e in facts["nearby"] if e["guid"]==42)
        self.assertEqual(science["prefab"],"sciencemachine")
        self.assertEqual(science["game_prefab"],"researchlab")
        self.assertTrue(facts["crafting"]["sciencemachine"])

    def test_missing_fuel_and_fire_tags_remain_unknown_for_both(self):
        actual = self.observed()
        del actual["player"]["inventory"]["equipped"][0]["durability_percent"]
        actual["nearby"] = [{"prefab": "campfire", "distance": 2}]
        facts = self.assert_same_facts(actual)
        self.assertNotIn("durability_percent", facts["inventory_details"]["equipped"][0])
        self.assertIsNone(facts["lighting"]["observed_fires"][0]["lit"])
        self.assertFalse(facts["nearby_lit_fire"])
        self.assertFalse(facts["nearby_cooker"])

    def test_model_snapshots_do_not_mutate_or_alias_telemetry(self):
        actual = self.observed()
        before = copy.deepcopy(actual)
        planner = daily_planner.planning_context(actual, [])
        decider = jev_agent.compact_game_state(actual)
        self.assertEqual(actual, before)
        planner["inventory_details"]["equipped"][0]["durability_percent"] = 0
        planner["world"]["phase"] = "day"
        planner["nearby"][0]["collect_path"]["waypoint"]["x"] = 999
        planner["construction"]["campfire"]["status"] = "ready"
        self.assertEqual(actual, before)
        self.assertEqual(decider["inventory_details"]["equipped"][0]["durability_percent"], 0.04)
        self.assertEqual(decider["world"]["phase"], "night")
        self.assertEqual(decider["construction"]["campfire"]["status"], "blocked")


    def test_actual_api_payloads_share_facts_but_not_model_specific_roles(self):
        actual = self.observed()
        actual["navigation"] = {"graph": {"current": {"id": "A:BGGrass"}}}
        actual["bridge"] = {"enabled": True, "protocol": 4, "board_revision": 2,
                            "available_actions": {"wait": {"kind": "wait"}}}
        planning = daily_planner.planning_context(actual, [{"action": "collect_grass"}])
        planning["dst_knowledge"] = "strategic reference"
        response = Response({"choices": [{"finish_reason": "stop", "message": {
            "content": json.dumps({"goal":"Find gold","tasks":[{"id":"gold","title":"Gold","target":{"type":"inventory","prefab":"goldnugget","count":1}}]}),
        }}]})
        with patch.object(daily_planner.urllib.request, "urlopen", return_value=response) as send:
            daily_planner.request_plan("https://example.invalid/plan", "secret", "planner", 1, planning)
        messages = json.loads(send.call_args.args[0].data)["messages"]
        deepseek_state = json.loads(messages[-1]["content"])
        reference = json.loads(messages[0]["content"].split(daily_planner.STATIC_REFERENCE_MARKER)[1])
        response = Response({"answers": {"next_action": {"choice": "wait", "confidence": 1}}})
        with patch.object(jev_agent.urllib.request, "urlopen", return_value=response) as send:
            jev_agent.call_jev("https://example.invalid/decide", "secret", "decider", actual,
                               {"wait": "Wait briefly"})
        jev_payload = json.loads(send.call_args.args[0].data)
        jev_state = json.loads(jev_payload["state"])
        for key in model_game_state(actual):
            if key == "region_resource_priors":
                for room, prefabs in jev_state[key].items():
                    self.assertEqual(reference[key].get(room, []), prefabs)
            elif key == "control":
                self.assertEqual({**reference["control_contract"], **deepseek_state[key]}, jev_state[key])
            else:
                self.assertEqual(deepseek_state[key], jev_state[key], key)
        self.assertNotIn("region_resource_priors", deepseek_state)
        self.assertNotIn("dst_knowledge", deepseek_state)
        self.assertEqual(reference["dst_knowledge"], "strategic reference")
        self.assertIn("recent_actions", deepseek_state)
        self.assertNotIn("recent_actions", jev_state)
        self.assertNotIn("dst_knowledge", jev_state)
        self.assertIn("decision_guidance", jev_state)
        self.assertNotIn("decision_guidance", deepseek_state)
        self.assertEqual(jev_payload["questions"]["next_action"]["criteria"], {"wait": "Wait briefly"})


if __name__ == "__main__":
    unittest.main()
