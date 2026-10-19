"""Prefetch remains advisory, bounded and matched to live semantic targets."""

import copy
import threading
import time
import unittest
from concurrent.futures import Future
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import controller
import decision_prefetch as module
import jev_agent
import timing_log as timing
from test_regressions import state, grass


class ControlledExecutor:
    def __init__(self, **_kwargs):
        self.calls = []

    def submit(self, *args):
        future = Future()
        future.set_running_or_notify_cancel()
        self.calls.append((args, future))
        return future

    def shutdown(self, **_kwargs):
        pass


class PrefetchTests(unittest.TestCase):
    def setUp(self):
        self.print_patch = patch("builtins.print")
        self.print_patch.start()
        self.addCleanup(self.print_patch.stop)

    def make_prefetch(self):
        with patch.object(module, "ThreadPoolExecutor", ControlledExecutor):
            worker = module.DecisionPrefetch(Mock())
        self.addCleanup(worker.close)
        return worker

    def test_background_request_can_finish_without_any_game_input(self):
        started, release = threading.Event(), threading.Event()
        def request(*_args):
            with timing.span("api.test_request"):
                started.set()
                release.wait(1)
            return {"answer": {"choice": "wait"}}
        worker = module.DecisionPrefetch(request)
        try:
            with patch.object(controller, "send_key") as keys:
                self.assertTrue(worker.submit(state([]), {"wait": "Wait"}, {"wait": {"kind": "wait"}}, None))
                self.assertTrue(started.wait(1))
                self.assertFalse(worker.future.done())
                release.set()
                packet = worker.future.result(timeout=1)
                self.assertIn("api.test_request", packet[2]["totals"])
                keys.assert_not_called()
        finally:
            release.set()
            worker.close()

    def test_only_one_request_is_kept_even_when_stale_request_is_running(self):
        worker = self.make_prefetch()
        before = state([])
        self.assertTrue(worker.submit(before, {"wait": "Wait"}, {"wait": {"kind": "wait"}}, None))
        self.assertFalse(worker.submit(before, {}, {}, None))
        changed = copy.deepcopy(before)
        changed["world"]["phase"] = "night"
        self.assertIsNone(worker.take(changed, {"wait": {"kind": "wait"}}))
        self.assertFalse(worker.submit(before, {}, {}, None))
        self.assertEqual(len(worker.executor.calls), 1)
        worker.future.set_result(({"answer": {"choice": "wait"}}, 0.5))
        self.assertTrue(worker.submit(before, {}, {}, None))

    def test_reordered_neighbor_indices_preserve_the_chosen_node(self):
        worker = self.make_prefetch()
        observed = state([])
        action = {"kind": "explore", "navigation_mode": "graph", "target_node_id": "forest"}
        worker.submit(observed, {"explore_adjacent_1": "Forest"}, {"explore_adjacent_1": action}, None)
        worker.future.set_result(({"answer": {"choice": "explore_adjacent_1"}}, 0.5))
        fresh = {"explore_adjacent_1": {**action, "target_node_id": "swamp"}, "explore_adjacent_2": action}
        result = worker.take(observed, fresh)
        self.assertIsNotNone(result)
        self.assertEqual(module.current_action(result[1], fresh)[0], "explore_adjacent_2")

    def test_completed_collection_target_is_not_replaced_by_another_guid(self):
        worker = self.make_prefetch()
        action = {"kind": "collect", "prefab": "grass", "guid": 10}
        worker.submit(state([]), {"collect_grass": "Grass"}, {"collect_grass": action}, None)
        worker.future.set_result(({"answer": {"choice": "collect_grass"}}, 0.5))
        self.assertIsNone(worker.take(state([]), {"collect_grass": {**action, "guid": 11}}))

    def test_expired_response_is_discarded_without_waiting(self):
        worker = self.make_prefetch()
        worker.submit(state([]), {"wait": "Wait"}, {"wait": {"kind": "wait"}}, None)
        worker.submitted -= 10
        self.assertIsNone(worker.take(state([]), {"wait": {"kind": "wait"}}))

    def test_crossing_regions_or_taking_damage_invalidates_the_observation(self):
        before = state([])
        for change in ("region", "inventory", "damage"):
            with self.subTest(change=change):
                after = copy.deepcopy(before)
                if change == "region":
                    after["navigation"]["graph"] = {"current_node": "new-region"}
                elif change == "inventory":
                    after["player"]["inventory"]["items"] = [{"prefab": "cutgrass", "count": 1}]
                else:
                    after["player"]["vitals"]["health"] -= 1
                self.assertFalse(module.compatible(before, after))

    def test_second_cycle_consumes_prefetch_instead_of_sending_another_request(self):
        worker = self.make_prefetch()
        observed = state([grass(10, 0.5, True)])
        result = {"response": {"model": "test"},
                  "answer": {"choice": "collect_grass", "confidence": 1}}
        args = SimpleNamespace(log=Path("unused.log"), execute=True, show_probabilities=False,
                               min_confidence=None, move_seconds=1.0)
        def execute(*_args):
            callback = controller._action_observer.get()
            submitted = len(worker.executor.calls)
            callback(observed, "movement")
            self.assertEqual(len(worker.executor.calls), submitted)
            callback(observed, "verified")
            return True
        with (
            patch.object(controller, "latest_state", return_value=(observed, 100)),
            patch.object(jev_agent, "call_jev", return_value=result) as request,
            patch.object(jev_agent, "execute_action", side_effect=execute),
        ):
            self.assertFalse(jev_agent.run_decision_cycle(args, "url", "key", "test", prefetch=worker))
            self.assertEqual(len(worker.executor.calls), 1)
            worker.future.set_result((result, 0.5))
            self.assertFalse(jev_agent.run_decision_cycle(args, "url", "key", "test", prefetch=worker))
        request.assert_called_once()

    def test_exploration_yield_requests_from_fresh_next_cycle_instead_of_prefetch(self):
        worker = self.make_prefetch()
        before = state([])
        berry = {**grass(11, 0.5, True), "prefab": "berrybush"}
        after = state([berry])
        explore = {"response": {"model": "test"},
                   "answer": {"choice": "explore_current_region", "confidence": 1}}
        collect = {"response": {"model": "test"},
                   "answer": {"choice": "collect_berries", "confidence": 1}}
        # Use live candidate naming rather than a fixture-specific alias.
        _, actions = jev_agent.build_candidates(after)
        collect["answer"]["choice"] = next(key for key, action in actions.items()
            if action["kind"] == "collect" and action.get("guid") == 11)
        args = SimpleNamespace(log=Path("unused.log"), execute=True, show_probabilities=False,
                               min_confidence=None, move_seconds=1.0)
        current = [before]

        def execute(action, *_args):
            if action["kind"] == "explore":
                callback = controller._action_observer.get()
                callback(before, "movement")
                callback(before, "verified")
                current[0] = after
                callback(after, "exploration_event")
            return True

        with (
            patch.object(controller, "latest_state", side_effect=lambda *_: (current[0], 100)),
            patch.object(jev_agent, "call_jev", side_effect=[explore, collect]) as request,
            patch.object(jev_agent, "execute_action", side_effect=execute) as execute_mock,
        ):
            self.assertFalse(jev_agent.run_decision_cycle(args, "url", "key", "test", prefetch=worker))
            self.assertEqual(len(worker.executor.calls), 0)
            self.assertFalse(jev_agent.run_decision_cycle(args, "url", "key", "test", prefetch=worker))
        self.assertEqual(request.call_count, 2)
        self.assertIs(request.call_args.args[3], after)
        self.assertEqual(execute_mock.call_args.args[0]["kind"], "collect")
        self.assertEqual(execute_mock.call_args.args[0]["guid"], 11)

    def test_basic_tool_crafting_prefetches_only_after_verified(self):
        for kind, recipe in (("craft_axe", "axe"), ("craft_pickaxe", "pickaxe")):
            with self.subTest(kind=kind):
                worker = self.make_prefetch()
                observed = state([])
                observed["crafting"][recipe] = True
                result = {"response": {"model": "test"},
                          "answer": {"choice": kind, "confidence": 1}}
                args = SimpleNamespace(log=Path("unused.log"), execute=True, show_probabilities=False,
                                       min_confidence=None, move_seconds=1.0)

                def execute(*_args):
                    callback = controller._action_observer.get()
                    callback(observed, "movement")
                    self.assertEqual(len(worker.executor.calls), 0)
                    callback(observed, "verified")
                    return True

                with (
                    patch.object(controller, "latest_state", return_value=(observed, 100)),
                    patch.object(jev_agent, "call_jev", return_value=result),
                    patch.object(jev_agent, "execute_action", side_effect=execute),
                ):
                    self.assertFalse(jev_agent.run_decision_cycle(args, "url", "key", "test", prefetch=worker))
                self.assertEqual(len(worker.executor.calls), 1)

    def test_observer_runs_while_keys_are_held_and_input_is_always_released(self):
        held = set()
        observed = []
        def key(vk, down):
            held.add(vk) if down else held.discard(vk)
        def checkpoint(*_args):
            observed.append(set(held))
            return False
        with patch.object(controller, "send_key", side_effect=key), patch.object(controller.time, "sleep"):
            with controller.observe_action(checkpoint):
                controller.tap([controller.VK["up"]], 0.5)
        self.assertEqual(observed, [{controller.VK["up"]}])
        self.assertEqual(held, set())

    def test_phase_change_after_prefetch_reply_prevents_execution(self):
        worker = self.make_prefetch()
        before = state([])
        after = copy.deepcopy(before)
        after["world"]["phase"] = "dusk"
        criteria, dispatch = jev_agent.build_candidates(before)
        worker.submit(before, criteria, dispatch, None)
        result = {"response": {"model": "test"},
                  "answer": {"choice": "explore_current_region", "confidence": 1}}
        worker.future.set_result((result, 0.5))
        args = SimpleNamespace(log=Path("unused.log"), execute=True, show_probabilities=False,
                               min_confidence=None, move_seconds=1.0)
        with (
            patch.object(controller, "latest_state", side_effect=[(before, 100), (after, 101)]),
            patch.object(jev_agent, "execute_action") as execute,
            patch.object(jev_agent, "call_jev") as request,
        ):
            self.assertFalse(jev_agent.run_decision_cycle(args, "url", "key", "test", prefetch=worker))
        execute.assert_not_called()
        request.assert_not_called()

    def test_synchronous_response_is_also_revalidated_before_input(self):
        before = state([grass(10, 0.5, True)])
        after = state([grass(10, 0.5, False)])
        result = {"response": {"model": "test"},
                  "answer": {"choice": "collect_grass", "confidence": 1}}
        args = SimpleNamespace(log=Path("unused.log"), execute=True, show_probabilities=False,
                               min_confidence=None, move_seconds=1.0)
        with (
            patch.object(controller, "latest_state", side_effect=[(before, 100), (after, 101)]),
            patch.object(jev_agent, "call_jev", return_value=result),
            patch.object(jev_agent, "execute_action") as execute,
        ):
            self.assertFalse(jev_agent.run_decision_cycle(args, "url", "key", "test"))
        execute.assert_not_called()

    def test_only_attack_is_confidence_gated_even_with_cli_override(self):
        cases = (("craft_axe", 0.0, 0.9, True),
                 ("explore_current_region", 0.01, 0.9, True),
                 ("attack_nearest_hostile", 0.44, None, False),
                 ("attack_nearest_hostile", 0.45, None, True),
                 ("attack_nearest_hostile", 0.7, 0.8, False))
        for choice, confidence, override, should_execute in cases:
            with self.subTest(choice=choice, confidence=confidence, override=override):
                kind = "explore" if choice.startswith("explore") else choice
                action = {"kind": kind}
                observed = state([])
                args = SimpleNamespace(log=Path("unused.log"), execute=True, show_probabilities=False,
                                       min_confidence=override, move_seconds=1)
                result = {"response": {"model": "test"},
                          "answer": {"choice": choice, "confidence": confidence}}
                with (
                    patch.object(controller, "latest_state", return_value=(observed, 100)),
                    patch.object(jev_agent, "build_candidates", return_value=({choice: "Action"}, {choice: action})),
                    patch.object(jev_agent, "call_jev", return_value=result),
                    patch.object(jev_agent, "execute_action", return_value=True) as execute,
                ):
                    jev_agent.run_decision_cycle(args, "url", "key", "test")
                self.assertEqual(execute.called, should_execute)

    def test_failed_optional_request_falls_back_without_raising(self):
        worker = self.make_prefetch()
        worker.submit(state([]), {"wait": "Wait"}, {"wait": {"kind": "wait"}}, None)
        worker.future.set_exception(RuntimeError("test request failed"))
        self.assertIsNone(worker.take(state([]), {"wait": {"kind": "wait"}}))

    def test_collection_requires_post_release_state_before_correcting_direction(self):
        before = state([grass(10, 4, True)])
        near = state([grass(10, 0.8, True)])
        done = state([grass(10, 0.8, False)], cutgrass=1)
        with (
            patch.object(controller, "latest_state", side_effect=[(before, 100), (before, 150)]),
            patch.object(controller, "wait_for_fresh_state", side_effect=[(before, 101), (near, 151)]) as fresh,
            patch.object(controller, "wait_for_state_condition", return_value=(done, 152)),
            patch.object(controller, "find_game_window", return_value=123),
            patch.object(controller, "focus_game"), patch.object(controller, "tap") as tap,
            patch.object(controller, "release_movement_keys"),
        ):
            self.assertTrue(controller.collect(Path("unused.log"), "grass"))
        self.assertEqual(fresh.call_args_list[1].args[1], 150)
        self.assertEqual(tap.call_count, 2)
        self.assertLess(tap.call_args_list[0].args[1] * 6, 4)

    def test_backtracking_pauses_route_even_when_each_local_leg_reports_progress(self):
        before = state([])
        before["player"]["position"]["x"] = 5
        route = {"status": "ready", "node_id": "test-destination",
                 "target": {"x": 0, "z": 0}, "leg": {"x": 0, "z": 0, "distance": 4}}
        before["navigation"]["graph"] = {"current_node": "test-start", "adjacent": [route]}
        after = copy.deepcopy(before)
        after["player"]["position"]["x"] = 0
        key = ("test-start", "test-destination")
        controller.GRAPH_ROUTE_HISTORY[key] = [(time.monotonic(), 0, 0), (time.monotonic(), 5, 0)]
        try:
            with (
                patch.object(controller, "latest_state", return_value=(before, 100)),
                patch.object(controller, "wait_for_fresh_state", return_value=(after, 101)),
                patch.object(controller, "find_game_window", return_value=123),
                patch.object(controller, "focus_game"), patch.object(controller, "tap"),
                patch.object(controller, "release_movement_keys"),
            ):
                self.assertTrue(controller.explore_leg(Path("unused.log"), 0, 0, 4,
                    navigation_mode="graph", target_node_id="test-destination"))
            self.assertFalse(controller.graph_route_available("test-destination"))
        finally:
            controller.GRAPH_ROUTE_HISTORY.pop(key, None)
            controller.GRAPH_ROUTE_BLACKLIST.pop("test-destination", None)


if __name__ == "__main__":
    unittest.main()
