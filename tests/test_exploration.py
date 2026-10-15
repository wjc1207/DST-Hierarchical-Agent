"""Topology travel and unknown-exit search remain independent JEV decisions."""
import copy
import unittest
from pathlib import Path
from unittest.mock import patch

import controller
import jev_agent
from decision_prefetch import current_action
from test_regressions import state


def route(node="B", name="grassland 2"):
    return {"node_id": node, "name": name, "biome": "grassland", "status": "ready",
            "explored_area": 240, "boundary_complete": False, "entered": True,
            "hops": 2, "distance": 8,
            "target": {"x": 8, "z": 0}, "leg": {"x": 4, "z": 0, "distance": 4}}


def observed():
    value = state([])
    value["navigation"].update({
        "current_region": "A", "current_biome": "forest",
        "graph": {"current_node": "A", "current": {
            "id": "A", "name": "mixed 1", "explored_area": 640,
            "boundary_complete": False}, "routes": [route()]},
        "search": {"status": "ready", "strategy": "directional", "boundary_complete": False,
                   "target": {"x": 0, "z": 20, "distance": 20},
                   "leg": {"x": 0, "z": 4, "distance": 4}},
    })
    return value


class ExplorationTests(unittest.TestCase):
    def test_legacy_road_telemetry_never_creates_an_exploration_choice(self):
        value = observed()
        value["navigation"].pop("search")
        value["navigation"]["road"] = route()
        self.assertNotIn("explore_other_region", jev_agent.build_candidates(value)[0])
        value["navigation"]["search"] = {**route(), "strategy": "road"}
        self.assertNotIn("explore_other_region", jev_agent.build_candidates(value)[0])

    def test_legacy_road_action_is_rejected_before_any_input(self):
        with (patch.object(controller, "follow_exploration") as follow,
              patch.object(controller, "send_key") as keys):
            with self.assertRaisesRegex(ValueError, "Road-following"):
                jev_agent.execute_action({"kind": "explore", "navigation_mode": "road"}, Path("unused"))
        follow.assert_not_called()
        keys.assert_not_called()

    def test_search_without_road_and_known_travel_coexist(self):
        criteria, dispatch = jev_agent.build_candidates(observed())
        self.assertIn("explore_current_region", criteria)
        self.assertIn("explore_other_region", criteria)
        self.assertIn("travel_region_1", criteria)
        self.assertNotIn("return_previous_region", criteria)
        self.assertEqual(dispatch["explore_other_region"]["navigation_mode"], "search")
        self.assertIn("mixed 1", criteria["explore_current_region"])
        self.assertIn("grassland 2", criteria["travel_region_1"])
        self.assertIn("240 square world units", criteria["travel_region_1"])
        self.assertIn("2 region connection(s)", criteria["travel_region_1"])

    def test_complete_boundary_hides_only_unknown_search(self):
        value = observed()
        value["navigation"]["graph"]["current"]["boundary_complete"] = True
        criteria, _ = jev_agent.build_candidates(value)
        self.assertNotIn("explore_other_region", criteria)
        self.assertIn("travel_region_1", criteria)

    def test_temporarily_blocked_is_not_complete_and_never_uses_road_fallback(self):
        value = observed()
        value["navigation"]["search"] = {"status": "temporarily_blocked", "boundary_complete": False}
        value["navigation"]["road"] = route()
        criteria, _ = jev_agent.build_candidates(value)
        self.assertNotIn("explore_other_region", criteria)
        self.assertFalse(value["navigation"]["graph"]["current"]["boundary_complete"])
        value["navigation"]["search"] = observed()["navigation"]["search"]
        self.assertIn("explore_other_region", jev_agent.build_candidates(value)[0])

    def test_destination_identity_survives_reordering_and_return_is_deduplicated(self):
        before = observed()
        after = copy.deepcopy(before)
        after["navigation"]["graph"]["routes"].insert(0, route("C", "grassland 3"))
        old = jev_agent.build_candidates(before)[1]["travel_region_1"]
        fresh = jev_agent.build_candidates(after)[1]
        self.assertEqual(current_action(old, fresh)[0], "travel_region_2")
        after["navigation"]["graph"]["routes"].append(route())
        choices = jev_agent.build_candidates(after)[1]
        self.assertEqual(sum(a.get("target_node_id") == "B" for a in choices.values()), 1)

    def test_search_dispatches_event_driven_intent(self):
        _, dispatch = jev_agent.build_candidates(observed())
        with patch.object(controller, "follow_exploration", return_value=True) as follow:
            self.assertTrue(jev_agent.execute_action(dispatch["explore_other_region"], Path("unused")))
        self.assertEqual(follow.call_args.args[1]["navigation_mode"], "search")

    def test_search_identity_is_region_intent_not_temporary_waypoint(self):
        old = jev_agent.build_candidates(observed())[1]["explore_other_region"]
        changed = observed()
        changed["navigation"]["search"]["target"]["z"] = 24
        fresh = jev_agent.build_candidates(changed)[1]
        self.assertEqual(current_action(old, fresh)[0], "explore_other_region")
        changed["navigation"]["graph"]["current"]["id"] = "C"
        self.assertIsNone(current_action(old, jev_agent.build_candidates(changed)[1]))

    def test_search_rechecks_danger_and_night_light(self):
        value = observed()
        value["navigation"]["hazards"] = [{"guid": 8, "prefab": "knight", "x": 0, "z": 10}]
        self.assertNotIn("explore_other_region", jev_agent.build_candidates(value)[0])
        value = observed()
        value["world"]["phase"] = "night"
        self.assertNotIn("explore_other_region", jev_agent.build_candidates(value)[0])

    def test_controller_reads_new_graph_route_list_and_search_section(self):
        for mode, node, x, z in (("graph", "B", 4, 0), ("search", None, 0, 4)):
            with self.subTest(mode=mode):
                before = observed()
                after = copy.deepcopy(before)
                after["player"]["position"].update(x=x, z=z)
                with (patch.object(controller, "latest_state", side_effect=[(before, 1), (before, 2)]),
                      patch.object(controller, "wait_for_fresh_state", return_value=(after, 3)),
                      patch.object(controller, "find_game_window", return_value=1),
                      patch.object(controller, "focus_game"), patch.object(controller, "tap") as tap,
                      patch.object(controller, "release_movement_keys")):
                    controller.EXPLORATION_STALL.update(target=None, count=0, last_position=None)
                    self.assertTrue(controller.explore_leg(Path("unused"), x, z, 4,
                                                          navigation_mode=mode, target_node_id=node))
                tap.assert_called_once()

    def test_only_wait_diagnostics_do_not_claim_the_region_is_exhausted(self):
        value = observed()
        nav = value["navigation"]
        nav.update(status="no_frontier", frontier_count=3,
                   search={"status": "temporarily_blocked"},
                   diagnostics={"graph_paths": {"no_safe_observed_gateway_path": 2}})
        nav["graph"].update(known_nodes=10, routes=[])
        context = jev_agent.candidate_context(value, ["wait"])
        self.assertTrue(context["only_wait"])
        self.assertEqual(context["known_regions"], 10)
        self.assertEqual(context["frontier_count"], 3)
        self.assertFalse(context["boundary_complete"])
        self.assertEqual(context["reported_routes"], 0)
        self.assertIn("no_safe_observed_gateway_path", context["path_diagnostics"]["graph_paths"])

    def test_confident_wait_is_not_misreported_as_only_wait_when_alternatives_exist(self):
        context = jev_agent.candidate_context(observed(), ["wait", "explore_other_region"])
        self.assertFalse(context["only_wait"])
        self.assertEqual(context["count"], 2)


if __name__ == "__main__":
    unittest.main()
