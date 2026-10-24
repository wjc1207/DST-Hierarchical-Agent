"""Ordered planning regressions: no live game, model key or network."""
import copy
import json
import tempfile
import unittest
from concurrent.futures import Future
from pathlib import Path
from unittest.mock import patch

from mcp_agent.action_sequence import SequenceExecutor, validate_sequence, format_sequence
from mcp_agent.daily_planner import DailyPlanner, request_plan
from mcp_agent.navigation_intents import ExplorationExecutor
from mcp_agent.jev_client import compact_game_state


def state(**counts):
    return {"world": {"day": 1, "phase": "day", "time": 0.1},
            "player": {"guid": 1, "position": {"x": 0, "z": 0},
                       "vitals": {"health": 150, "hunger": 100, "hunger_max": 150},
                       "inventory": {"items": [{"prefab": k, "count": v} for k, v in counts.items()], "equipped": []}},
            "navigation": {"graph": {"current_node": "A", "known_nodes": 2,
                                     "current": {"explored_area": 100, "boundary_complete": False}, "routes": []}},
            "nearby": [], "crafting": {}}


def plan(*steps):
    return {**validate_sequence({"goal": "Offline sequence", "steps": list(steps)}, 1), "sequence_id": "run:1"}


class SequenceTests(unittest.TestCase):
    def test_validation_rejects_invalid_unbounded_or_unknown_operations(self):
        cases = [[], ["collect grass"], [{"action": "execute_lua"}],
                 [{"action": "craft", "prefab": "alchemyengine"}],
                 [{"action": "build", "prefab": "firepit"}],
                 [{"action": "collect", "prefab": "cutgrass", "count": True}],
                 [{"action": "travel_region"}], [{"action": "explore_current_region", "area": float("nan")}],
                 [{"action": "eat_safe_food", "hunger_ratio": 2}],
                 [{"action": "equip_torch", "timeout_seconds": 10000}],
                 [{"action": "wait", "seconds": 10, "timeout_seconds": 5}],
                 [{"id": "same", "action": "equip_torch"}] * 2]
        for steps in cases:
            with self.subTest(steps=steps), self.assertRaises(ValueError):
                validate_sequence({"goal": "Bad", "steps": steps}, 1)
        self.assertEqual(plan({"action": "collect", "prefab": "logs", "count": 2})["steps"][0]["prefab"], "log")

    def test_order_latches_consumed_ingredients_and_does_not_skip_to_visible_later_goal(self):
        sequence = plan({"id": "grass", "action": "collect", "prefab": "cutgrass", "count": 2},
                        {"id": "torch", "action": "craft", "prefab": "torch"})
        executor = SequenceExecutor()
        actions = {"collect_grass": {"kind": "collect", "product": "cutgrass", "guid": 1},
                   "craft_torch": {"kind": "craft_torch"}}
        nav = ExplorationExecutor()
        self.assertEqual(executor.choose(sequence, state(), actions, nav), "collect_grass")
        self.assertEqual(executor.choose(sequence, state(cutgrass=2), actions, nav), "craft_torch")
        self.assertEqual(executor.choose(sequence, state(), actions, nav), "craft_torch")
        view = executor.view(sequence, state(torch=1))
        self.assertEqual(view["status"], "completed")
        self.assertEqual(view["completed_step_ids"], ["grass", "torch"])
        self.assertNotIn("status", sequence["steps"][0])
        self.assertIn("ACTION SEQUENCE", format_sequence(view))

    def test_travel_requires_current_node_not_historical_entered_flag(self):
        sequence = plan({"action": "travel_region", "node_id": "B"})
        executor = SequenceExecutor()
        s = state()
        s["navigation"]["graph"]["routes"] = [{"node_id": "B", "entered": True}]
        actions = {"travel_B": {"kind": "explore", "mode": "graph", "node_id": "B"},
                   "travel_C": {"kind": "explore", "mode": "graph", "node_id": "C"}}
        self.assertEqual(executor.choose(sequence, s, actions, None), "travel_B")
        self.assertEqual(executor.view(sequence, s)["status"], "active")
        s["navigation"]["graph"]["current_node"] = "B"
        self.assertEqual(executor.view(sequence, s)["status"], "completed")

    def test_local_survey_and_new_region_search_have_bounded_distinct_completion(self):
        for action, field, increment in (("explore_current_region", "explored_area", 256),
                                          ("explore_other_region", "known_nodes", 1)):
            with self.subTest(action=action):
                executor = SequenceExecutor()
                sequence = plan({"action": action})
                s = state(); executor.view(sequence, s)
                graph = s["navigation"]["graph"]
                subject = graph["current"] if field == "explored_area" else graph
                subject[field] += increment - 1
                self.assertEqual(executor.view(sequence, s)["status"], "active")
                subject[field] += 1
                self.assertEqual(executor.view(sequence, s)["status"], "completed")
        executor = SequenceExecutor(); sequence = plan({"action": "explore_current_region"})
        s = state(); s["navigation"]["graph"]["current"]["boundary_complete"] = True
        self.assertEqual(executor.view(sequence, s)["status"], "completed")

    def test_predicted_facts_never_advance_and_timeouts_do_not_reset_on_poll(self):
        now = [0]
        executor = SequenceExecutor(clock=lambda: now[0])
        sequence = plan({"action": "collect", "prefab": "cutgrass", "count": 2, "timeout_seconds": 5})
        s = state(cutgrass=2); s["_jev_execution_context"] = {"predicted_success": True}
        self.assertEqual(executor.view(sequence, s)["completed_step_ids"], [])
        executor.view(sequence, state()); now[0] = 4; executor.view(sequence, state())
        now[0] = 5
        self.assertEqual(executor.view(sequence, state())["reason"], "step_timeout")

    def test_only_failures_end_sequence_resource_yields_continue_and_safety_hands_off(self):
        executor = SequenceExecutor(); sequence = plan({"action": "explore_other_region"})
        executor.view(sequence, state())
        executor.on_result("interrupted", "resource_types_changed")
        self.assertEqual(executor.status, "active")
        executor.on_result("interrupted", "health_lost")
        self.assertTrue(executor.needs_safety_decision)
        executor.on_result("failed", "verification_timeout")
        self.assertEqual(executor.view(sequence, state())["status"], "failed")

    def test_missing_recipe_does_not_execute_later_step_and_jev_sees_safety_role(self):
        sequence = plan({"action": "craft", "prefab": "axe"}, {"action": "collect", "prefab": "twigs", "count": 2})
        executor = SequenceExecutor()
        self.assertIsNone(executor.choose(sequence, state(), {"collect_sapling": {"kind": "collect", "product": "twigs"}}, None))
        self.assertEqual(executor.status, "failed")
        compact = compact_game_state(state(), sequence)
        self.assertIn("action_sequence", compact)
        self.assertIn("safety", " ".join(compact["decision_guidance"]))

    def test_collection_reuses_known_source_bfs_and_unknown_branch_search_with_tool_checks(self):
        sequence = plan({"action": "collect", "prefab": "goldnugget", "count": 1})
        s = state()
        s["player"]["inventory"]["items"] = [{"prefab": "pickaxe", "count": 1, "durability_percent": 1}]
        s["navigation"]["graph"]["routes"] = [
            {"node_id": "B", "hops": 1, "distance": 10, "entered": True, "resources_seen": ["rock2"]}]
        actions = {"travel_B": {"kind": "explore", "mode": "graph", "node_id": "B", "x": 4, "z": 0},
                   "explore_other_region": {"kind": "explore", "mode": "search", "x": 0, "z": 4}}
        executor, nav = SequenceExecutor(), ExplorationExecutor()
        self.assertEqual(executor.choose(sequence, s, actions, nav), "travel_B")
        self.assertEqual(nav.strategy, "known_source_bfs")
        s["player"]["inventory"]["items"] = []
        self.assertEqual(executor.choose(sequence, s, actions, nav), "explore_other_region")
        self.assertEqual(nav.strategy, "dfs_unknown_exit")

    def test_food_light_construction_and_wait_complete_from_their_own_effects(self):
        cases = [({"action": "eat_safe_food", "hunger_ratio": 0.8},
                  lambda s: s["player"]["vitals"].update(hunger=130)),
                 ({"action": "cook_food", "count": 2},
                  lambda s: s["player"]["inventory"]["items"].append({"prefab": "berries_cooked", "count": 2})),
                 ({"action": "equip_torch"},
                  lambda s: s["player"]["inventory"]["equipped"].append({"prefab": "torch"})),
                 ({"action": "build", "prefab": "sciencemachine"},
                  lambda s: s["nearby"].append({"prefab": "sciencemachine"}))]
        for operation, effect in cases:
            with self.subTest(action=operation):
                executor, s = SequenceExecutor(), state()
                sequence = plan(operation)
                self.assertEqual(executor.view(sequence, s)["status"], "active")
                effect(s)
                self.assertEqual(executor.view(sequence, s)["status"], "completed")
        now = [0]
        executor = SequenceExecutor(clock=lambda: now[0])
        sequence = plan({"action": "wait", "seconds": 2})
        executor.view(sequence, state()); now[0] = 1
        self.assertEqual(executor.view(sequence, state())["status"], "active")
        now[0] = 2
        self.assertEqual(executor.view(sequence, state())["status"], "completed")


class PlannerSequenceTests(unittest.TestCase):
    def test_new_requests_output_priority_goals_while_legacy_sequences_remain_readable(self):
        class Response:
            def __enter__(self): return self
            def __exit__(self, *_): pass
            def read(self):
                return json.dumps({"choices": [{"finish_reason": "stop", "message": {
                    "content": json.dumps({"goal":"Survey","tasks":[{"id":"regions","title":"Discover regions","target":{"type":"known_regions","count":3}}]})}}]}).encode()
        with patch("mcp_agent.daily_planner.urllib.request.urlopen", return_value=Response()) as send:
            result = request_plan("https://example.invalid", "offline", "fake", 1, {})
        self.assertEqual(result["execution_policy"], "priority_goals")
        prompt = json.loads(send.call_args.args[0].data)["messages"][0]["content"]
        self.assertIn("not an action sequence", prompt)

    def test_completed_or_failed_sequence_replans_with_progress_and_survives_quarter_change(self):
        for fail in (False, True):
            with self.subTest(fail=fail), tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as folder:
                planner = DailyPlanner("offline", board_log_dir=Path(folder))
                planner.executor.shutdown(wait=False)
                class Worker:
                    def __init__(self): self.calls = []
                    def submit(self, *args): self.calls.append(args); return Future()
                    def shutdown(self, **_): pass
                planner.executor = Worker()
                planner.plan = plan({"action": "collect", "prefab": "cutgrass", "count": 2})
                self.addCleanup(planner.close)
                s = state(); planner.update(s)
                s["world"]["time"] = 0.3
                planner.update(s)
                self.assertEqual(len(planner.executor.calls), 0, "quarter changes must not replace active steps")
                if fail: planner.sequence.fail("test_failure")
                else: s["player"]["inventory"]["items"] = [{"prefab": "cutgrass", "count": 2}]
                self.assertIsNone(planner.update(s))
                context = planner.executor.calls[0][5]
                self.assertEqual(context["replan_reason"], "sequence_failed" if fail else "sequence_completed")
                self.assertEqual(context["previous_action_sequence"]["status"], "failed" if fail else "completed")
                self.assertTrue(Path(folder, "action_sequence.json").exists())
