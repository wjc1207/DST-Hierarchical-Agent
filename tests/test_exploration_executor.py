"""Navigation authority, persistence and model boundaries without game/API input."""
import copy
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import controller
import jev_agent
import timing_log as timing
from exploration_executor import ExplorationExecutor, sources_for
from task_board import validate_board
from test_exploration import observed, route
from test_regressions import grass


def board(target):
    return validate_board({"goal": "Get materials", "tasks": [
        {"id": "goal", "title": "Current goal", "priority": 1, "target": target},
    ]}, 1)


class ExplorationExecutorTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch("builtins.print"))
        self.enterContext(patch.object(controller, "release_movement_keys"))
        for memory in (controller.GRAPH_ROUTE_BLACKLIST, controller.GRAPH_GATEWAY_BLACKLIST):
            self.enterContext(patch.dict(memory, clear=True))
        self.navigator = ExplorationExecutor()

    def choose(self, actual, plan=None):
        return self.navigator.choose(actual, jev_agent.build_candidates(actual)[1], plan)

    def test_known_gold_sources_use_bfs_hops_not_ordinary_rocks(self):
        actual = observed()
        actual["navigation"]["graph"]["routes"] = [
            {**route("B"), "resources_seen": ["rock1"], "hops": 1, "distance": 1},
            {**route("C"), "resources_seen": ["rock2"], "hops": 2, "distance": 2},
            {**route("D"), "resources_seen": ["goldnugget"], "hops": 1, "distance": 10},
        ]
        _, action = self.choose(actual, board({"type": "inventory", "prefab": "goldnugget", "count": 1}))
        self.assertEqual(action["target_node_id"], "D")
        self.assertEqual(self.navigator.strategy, "known_source_bfs")
        self.assertNotIn("rock1", sources_for("goldnugget"))
        self.assertIn("rock2", sources_for("goldnugget"))

    def test_known_source_room_is_searched_locally_not_abandoned_at_entrance(self):
        actual = observed()
        actual["player"]["inventory"]["items"].append({"prefab": "pickaxe", "durability_percent": 1})
        actual["navigation"]["graph"]["current"]["resources_seen"] = ["rock2"]
        self.choose(actual, board({"type": "inventory", "prefab": "goldnugget", "count": 1}))
        self.assertEqual(self.navigator.strategy, "source_room")

    def test_explicit_visit_is_not_completed_by_an_old_entered_flag(self):
        actual = observed()  # B.entered is already true, but currently in A.
        plan = board({"type": "visit_region", "node_id": "B"})
        _, action = self.choose(actual, plan)
        self.assertEqual(action["target_node_id"], "B")
        self.assertEqual(self.navigator.strategy, "board_visit")
        arrived = copy.deepcopy(actual)
        arrived["navigation"]["graph"].update(current_node="B", routes=[])
        arrived["navigation"]["graph"]["current"]["id"] = "B"
        self.navigator.on_event({"reason": "destination_reached"}, arrived)
        self.choose(arrived, plan)
        self.assertNotEqual(self.navigator.strategy, "board_visit")

    def test_dfs_enters_unvisited_neighbor_and_backtracks_without_bouncing(self):
        actual = observed()
        actual["navigation"]["graph"]["routes"] = [{**route("B"), "hops": 1, "entered": False}]
        self.assertEqual(self.choose(actual)[1]["target_node_id"], "B")
        self.assertEqual(self.navigator.strategy, "dfs_neighbor")
        end = copy.deepcopy(actual)
        end["navigation"].update(status="no_frontier", search={"status": "boundary_complete"})
        end["navigation"]["graph"].update(current_node="B", routes=[{**route("A"), "hops": 1}])
        end["navigation"]["graph"]["current"].update(id="B", boundary_complete=True)
        self.navigator.on_event({"reason": "destination_reached"}, end)
        self.assertEqual(self.choose(end)[1]["target_node_id"], "A")
        self.assertEqual(self.navigator.strategy, "dfs_backtrack")
        root = copy.deepcopy(end)
        root["navigation"]["graph"].update(current_node="A", routes=[{**route("B"), "hops": 1}])
        root["navigation"]["graph"]["current"]["id"] = "A"
        self.navigator.on_event({"reason": "destination_reached"}, root)
        self.assertIsNone(self.choose(root))

    def test_navigation_intent_survives_local_collection_and_reordered_routes(self):
        actual = observed()
        plan = board({"type": "visit_region", "node_id": "B"})
        self.choose(actual, plan)
        self.navigator.on_event({"reason": "resource_types_changed"}, actual)
        self.navigator.after_local_action("collect")
        changed = copy.deepcopy(actual)
        changed["navigation"]["graph"]["routes"].insert(0, route("C"))
        key, action = self.choose(changed, plan)
        self.assertEqual(key, "travel_region_2")
        self.assertEqual(action["target_node_id"], "B")

    def test_paused_route_is_not_retried_and_not_marked_exhausted(self):
        actual = observed()
        plan = board({"type": "visit_region", "node_id": "B"})
        self.choose(actual, plan)
        self.navigator.on_event({"reason": "movement_stalled"}, actual)
        selected = self.choose(actual, plan)
        self.assertNotEqual(selected[1].get("target_node_id"), "B")
        self.assertFalse(actual["navigation"]["graph"]["current"]["boundary_complete"])

    def test_entered_but_unfinished_room_remains_a_pending_branch(self):
        actual = observed()
        actual["navigation"].update(status="no_frontier", search={"status": "boundary_complete"})
        actual["navigation"]["graph"]["current"]["boundary_complete"] = True
        self.assertEqual(self.choose(actual)[1]["target_node_id"], "B")
        self.assertEqual(self.navigator.strategy, "dfs_unfinished_room")

    def test_no_movement_when_night_has_no_torch_or_threat_is_active(self):
        for kind in ("night", "threat"):
            actual = observed()
            if kind == "night":
                actual["world"]["phase"] = "night"
            else:
                actual["nearby"] = [{"prefab": "hound", "guid": 99, "distance": 2,
                                      "activeThreat": True, "tags": ["hostile"]}]
            self.assertIsNone(self.choose(actual))

    def test_only_exploration_options_are_removed_from_jev_candidates(self):
        actual = observed()
        actual["nearby"] = [grass(1, 1, True)]
        criteria, actions = jev_agent.build_jev_candidates(actual)
        self.assertIn("collect_grass", criteria)
        self.assertIn("wait", criteria)
        self.assertTrue(all(a["kind"] != "explore" for a in actions.values()))

    def args(self, execute=True):
        return SimpleNamespace(log=Path("unused"), execute=execute, move_seconds=1,
                               show_probabilities=False, min_confidence=None)

    def test_automatic_search_continues_without_model_or_prefetch(self):
        actual = observed()
        prefetch = Mock()
        with (patch.object(controller, "latest_state", return_value=(actual, 1)),
              patch.object(jev_agent, "execute_action", return_value=True) as execute,
              patch.object(jev_agent, "call_jev") as api,
              timing.CycleTrace(1, emit_console=False) as trace):
            for _ in range(2):
                jev_agent.run_decision_cycle(self.args(), "unused", "unused", "test",
                                            prefetch=prefetch, exploration_executor=self.navigator)
        self.assertEqual(execute.call_count, 2)
        api.assert_not_called()
        prefetch.take.assert_not_called()
        self.assertEqual(trace.record["decision_source"], "exploration_auto")
        self.assertNotIn("decision.request", trace.record["totals"])

    def test_wait_releases_navigation_then_repeated_models_are_not_required(self):
        actual = observed()
        actual["nearby"] = [grass(1, 1, True)]
        response = {"response": {"model": "test"}, "answer": {"choice": "wait", "confidence": 0.1}}
        with (patch.object(controller, "latest_state", return_value=(actual, 1)),
              patch.object(jev_agent, "execute_action", return_value=True) as execute,
              patch.object(jev_agent, "call_jev", return_value=response) as api):
            for _ in range(2):
                jev_agent.run_decision_cycle(self.args(), "unused", "unused", "test",
                                            exploration_executor=self.navigator)
        api.assert_called_once()
        self.assertTrue(all(c.args[0]["kind"] == "explore" for c in execute.call_args_list))
        self.assertTrue(all(not k.startswith(("explore_", "travel_region_")) for k in api.call_args.args[4]))

    def test_new_resource_before_movement_overrides_automatic_continuation(self):
        actual = observed()
        changed = copy.deepcopy(actual)
        changed["nearby"] = [grass(1, 1, True)]
        response = {"response": {"model": "test"},
                    "answer": {"choice": "collect_grass", "confidence": 1}}
        with (patch.object(controller, "latest_state", side_effect=[(actual, 1), (changed, 2), (changed, 2), (changed, 2)]),
              patch.object(jev_agent, "execute_action", return_value=True) as execute,
              patch.object(jev_agent, "call_jev", return_value=response) as api):
            jev_agent.run_decision_cycle(self.args(), "unused", "unused", "test",
                                        exploration_executor=self.navigator)
        self.assertEqual(execute.call_args.args[0]["kind"], "collect")
        self.assertIs(api.call_args.args[3], changed)

    def test_real_event_loop_hands_new_resources_to_jev_without_exploration_prefetch(self):
        current = [observed()]
        legs = []

        def move(*_args, **_kwargs):
            fresh = copy.deepcopy(current[0])
            fresh["player"]["position"]["z"] += 3
            fresh["navigation"]["search"]["leg"]["z"] += 3
            legs.append(fresh)
            if len(legs) == 2:
                fresh["nearby"] = [grass(1, 1, True)]
            current[0] = fresh
            return True

        response = {"response": {"model": "test"},
                    "answer": {"choice": "collect_grass", "confidence": 1}}
        with (patch.object(controller, "latest_state", side_effect=lambda *_: (current[0], 1)),
              patch.object(controller, "explore_leg", side_effect=move),
              patch.object(controller, "collect", return_value=True) as collect,
              patch.object(jev_agent, "call_jev", return_value=response) as api):
            jev_agent.run_decision_cycle(self.args(), "unused", "unused", "test",
                                        exploration_executor=self.navigator)
            api.assert_not_called()
            self.assertTrue(self.navigator.handoff)
            self.assertEqual(self.navigator.last_event["reason"], "resource_types_changed")
            jev_agent.run_decision_cycle(self.args(), "unused", "unused", "test",
                                        exploration_executor=self.navigator)
        self.assertEqual(len(legs), 2)
        api.assert_called_once()
        collect.assert_called_once()

    def test_fresh_threat_before_start_is_handed_to_jev_not_movement(self):
        actual = observed()
        changed = copy.deepcopy(actual)
        changed["nearby"] = [{"prefab": "hound", "guid": 99, "distance": 2,
                               "dx": 2, "dz": 0, "activeThreat": True, "tags": ["hostile"]}]
        response = {"response": {"model": "test"},
                    "answer": {"choice": "flee_from_nearest_hostile", "confidence": 1}}
        with (patch.object(controller, "latest_state", side_effect=[(actual, 1), (changed, 2), (changed, 2), (changed, 2)]),
              patch.object(jev_agent, "execute_action", return_value=True) as execute,
              patch.object(jev_agent, "call_jev", return_value=response) as api):
            jev_agent.run_decision_cycle(self.args(), "unused", "unused", "test",
                                        exploration_executor=self.navigator)
        self.assertEqual(execute.call_args.args[0]["kind"], "flee_from_nearest_hostile")
        self.assertIs(api.call_args.args[3], changed)

    def test_only_idle_at_night_does_not_request_a_trivial_model_decision(self):
        actual = observed()
        actual["world"]["phase"] = "night"
        with (patch.object(controller, "latest_state", return_value=(actual, 1)),
              patch.object(jev_agent, "execute_action", return_value=True) as execute,
              patch.object(jev_agent, "call_jev") as api):
            jev_agent.run_decision_cycle(self.args(), "unused", "unused", "test",
                                        exploration_executor=self.navigator)
        self.assertEqual(execute.call_args.args[0]["kind"], "wait")
        api.assert_not_called()

    def test_dry_run_never_automatically_moves(self):
        actual = observed()
        with (patch.object(jev_agent, "execute_action") as execute,
              patch.object(controller, "latest_state") as read):
            self.assertFalse(jev_agent.run_executor_exploration(self.args(False), actual, None,
                jev_agent.build_candidates(actual)[1], None, None, self.navigator))
        execute.assert_not_called()
        read.assert_not_called()


if __name__ == "__main__":
    unittest.main()
