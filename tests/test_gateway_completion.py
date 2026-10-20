"""Regression replay for gateway oscillation and transient torch replication."""
import copy
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import controller
import jev_agent
import timing_log as timing
from task_board import validate_board
from test_exploration import observed, route
from test_regressions import state


class GatewayTests(unittest.TestCase):
    def setUp(self):
        for memory in (controller.GRAPH_ROUTE_BLACKLIST, controller.GRAPH_GATEWAY_BLACKLIST,
                       controller.GRAPH_ROUTE_HISTORY):
            self.enterContext(patch.dict(memory, clear=True))

    def graph(self):
        actual = observed()
        actual["player"]["guid"] = 7001
        b = {**route("B"), "gateway_id": "A>B:0:0:4:0"}
        c = {**route("C"), "gateway_id": b["gateway_id"], "next_node_id": "B", "hops": 2}
        d = {**route("D"), "gateway_id": "A>D:0:0:0:4"}
        actual["navigation"]["graph"]["routes"] = [b, c, d]
        return actual, b, c, d

    def test_pause_excludes_every_destination_sharing_the_gateway(self):
        actual, b, c, _ = self.graph()
        actions = jev_agent.build_candidates(actual)[1]
        selected = next(a for a in actions.values() if a.get("target_node_id") == "C")
        with patch.object(controller.time, "monotonic", return_value=100) as clock:
            controller.pause_graph_route(actual, "B", b, "backtracking")
            targets = {a.get("target_node_id") for a in jev_agent.build_candidates(actual)[1].values()}
            self.assertNotIn("B", targets)
            self.assertNotIn("C", targets)
            self.assertIn("D", targets)
            with patch.object(controller, "latest_state", return_value=(actual, 1)), \
                    patch.object(controller, "explore_leg") as move:
                self.assertFalse(controller.follow_exploration(Path("unused"), selected))
            move.assert_not_called()
            self.assertFalse(controller.graph_route_available("C", c["gateway_id"], 7001))
            self.assertTrue(controller.graph_route_available("C", c["gateway_id"], 7002))
            clock.return_value = 131
            self.assertTrue(controller.graph_route_available("B", b["gateway_id"], 7001))
            self.assertTrue(controller.graph_route_available("C", c["gateway_id"], 7001))

    def test_history_is_shared_by_gateway_not_reset_by_changing_final_goal(self):
        actual, b, c, d = self.graph()
        self.assertEqual(controller.graph_history_key(actual, "B", b),
                         controller.graph_history_key(actual, "C", c))
        self.assertNotEqual(controller.graph_history_key(actual, "B", b),
                            controller.graph_history_key(actual, "D", d))

    def test_direct_leg_rechecks_paused_gateway_before_any_input(self):
        actual, b, c, _ = self.graph()
        controller.pause_graph_route(actual, "B", b, "stalled")
        with patch.object(controller, "latest_state", return_value=(actual, 1)), \
                patch.object(controller, "find_game_window") as window, \
                patch.object(controller, "tap") as tap:
            self.assertFalse(controller.explore_leg(Path("unused"), 4, 0, 4,
                navigation_mode="graph", target_node_id="C"))
        window.assert_not_called()
        tap.assert_not_called()

    def test_crossing_uses_its_own_tolerance_not_local_frontier_tolerance(self):
        actual, b, _, _ = self.graph()
        b["leg"] = {"x": 1.2, "z": 0, "distance": 1.2, "arrival_distance": 0.2}
        after = copy.deepcopy(actual)
        after["player"]["position"]["x"] = 1
        with patch.object(controller, "latest_state", return_value=(actual, 1)), \
                patch.object(controller, "wait_for_fresh_state", return_value=(after, 2)), \
                patch.object(controller, "find_game_window", return_value=123), \
                patch.object(controller, "focus_game"), patch.object(controller, "tap") as tap, \
                patch.object(controller, "release_movement_keys"):
            self.assertTrue(controller.explore_leg(Path("unused"), 1.2, 0, 1.2,
                navigation_mode="graph", target_node_id="B"))
        tap.assert_called_once()

    def waiting(self):
        actual, b, _, _ = self.graph()
        b["leg"] = {"x": 0, "z": 0, "distance": 0, "arrival_distance": 0.2, "awaiting_region": True}
        action = next(a for a in jev_agent.build_candidates(actual)[1].values()
                      if a.get("target_node_id") == "B")
        return actual, b, action

    def test_topology_confirmation_waits_without_moving_and_stops_on_arrival_or_threat(self):
        for cause in ("arrival", "threat"):
            actual, _, action = self.waiting()
            changed = copy.deepcopy(actual)
            if cause == "arrival": changed["navigation"]["graph"]["current_node"] = "B"
            else: changed["nearby"] = [{"prefab": "hound", "guid": 8, "activeThreat": True}]
            def wait(_path, _offset, predicate, timeout):
                self.assertEqual(timeout, 2)
                self.assertFalse(predicate(actual))
                self.assertTrue(predicate(changed))
                return changed, 2
            with patch.object(controller, "latest_state", return_value=(actual, 1)), \
                    patch.object(controller, "wait_for_state_condition", side_effect=wait), \
                    patch.object(controller, "explore_leg") as move, \
                    patch.object(controller, "tap") as tap, timing.CycleTrace(1, emit_console=False) as trace:
                controller.follow_exploration(Path("unused"), action)
            move.assert_not_called()
            tap.assert_not_called()
            self.assertEqual(trace.record["exploration_event"]["reason"],
                             "destination_reached" if cause == "arrival" else "targeted_by_hostile")

    def test_confirmation_timeout_pauses_gateway_and_does_not_walk_blindly(self):
        actual, b, action = self.waiting()
        with patch.object(controller, "latest_state", return_value=(actual, 1)), \
                patch.object(controller, "wait_for_state_condition", side_effect=RuntimeError("timeout")), \
                patch.object(controller, "explore_leg") as move, timing.CycleTrace(1, emit_console=False) as trace:
            self.assertFalse(controller.follow_exploration(Path("unused"), action))
        move.assert_not_called()
        self.assertEqual(trace.record["exploration_event"]["reason"], "region_confirmation_timeout")
        self.assertFalse(controller.graph_route_available("C", b["gateway_id"], 7001))


def torches(items, hands=0):
    actual = state([])
    actual["world"]["phase"] = "dusk"
    actual["crafting"]["torch"] = True
    actual["player"]["inventory"]["items"] = [{"prefab": "torch", "count": items}] if items else []
    actual["player"]["inventory"]["equipped"] = [{"prefab": "torch", "count": hands}] if hands else []
    return actual


class TorchCompletionTests(unittest.TestCase):
    def test_unequip_requires_conservation_even_with_another_torch_in_backpack(self):
        before, transient, restored = torches(1, 1), torches(1), torches(2)
        with patch.object(controller, "latest_state", return_value=(before, 1)), \
                patch.object(controller, "find_game_window", return_value=123), \
                patch.object(controller, "focus_game"), patch.object(controller, "tap"), \
                patch.object(controller, "wait_for_state_condition", return_value=(restored, 3)) as wait:
            controller.unequip_torch(Path("unused"))
        self.assertFalse(wait.call_args.args[2](transient))
        self.assertTrue(wait.call_args.args[2](restored))

    def test_cleanup_already_in_transit_waits_for_expected_total_without_second_keypress(self):
        transient, restored = torches(1), torches(2)
        with patch.object(controller, "latest_state", return_value=(transient, 1)), \
                patch.object(controller, "tap") as tap, \
                patch.object(controller, "wait_for_state_condition", return_value=(restored, 2)) as wait:
            controller.unequip_torch(Path("unused"), daylight_only=True, minimum_total=2)
        tap.assert_not_called()
        self.assertFalse(wait.call_args.args[2](transient))
        self.assertTrue(wait.call_args.args[2](restored))

    def test_real_auto_craft_chain_does_not_fall_back_to_jev_on_two_one_two_transition(self):
        before, crafted, transient, restored = torches(1), torches(1, 1), torches(1), torches(2)
        current, queued = [before], []
        def tap(keys, _duration):
            if keys == [controller.VK["craft_torch"]]: queued.append([crafted])
            elif keys == [controller.VK["unequip_torch"]]: queued.append([transient, restored])
            else: self.fail(f"Unexpected game input {keys}")
        checked = []
        def wait(_path, _offset, predicate, **_kwargs):
            for frame in queued.pop(0):
                current[0] = frame
                accepted = predicate(frame)
                checked.append((controller.item_count(frame, "torch"), accepted))
                if accepted: return frame, len(checked) + 1
            self.fail("verification did not wait for settled inventory")
        board = validate_board({"goal": "Reserve light", "tasks": [{"id": "reserve", "title": "Two torches",
            "target": {"type": "inventory", "prefab": "torch", "count": 2}}]}, 1)
        board.update(board_id="replay:1", quarter=1)
        planner = Mock(update=Mock(return_value=board))
        args = SimpleNamespace(log=Path("unused"), execute=True, show_probabilities=False,
                               min_confidence=None, move_seconds=1)
        with patch.object(controller.telemetry, "latest_state", side_effect=lambda *_: (current[0], 1)), \
                patch.object(controller, "find_game_window", return_value=123), \
                patch.object(controller, "focus_game"), patch.object(controller, "tap", side_effect=tap) as keys, \
                patch.object(controller, "release_movement_keys"), \
                patch.object(controller, "wait_for_state_condition", side_effect=wait), \
                patch.object(jev_agent, "call_jev") as request, timing.CycleTrace(1, emit_console=False) as trace:
            self.assertFalse(jev_agent.run_decision_cycle(args, "unused", "unused", "test", planner, Mock()))
        self.assertEqual(checked, [(2, True), (1, False), (2, True)])
        self.assertEqual(keys.call_count, 2)  # one craft, one unequip, never a second craft
        self.assertEqual(trace.record["board_auto_outcome"], "executed")
        self.assertEqual(trace.record["decision_source"], "board_auto")
        request.assert_not_called()
        self.assertEqual(controller.item_count(current[0], "torch"), 2)


if __name__ == "__main__":
    unittest.main()
