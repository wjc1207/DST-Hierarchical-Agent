"""Two-stage pipeline: predict after native start, confirm before any next input."""
import copy
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import controller
import jev_agent
import decision_prefetch as prefetch
from action_buffer import predict_success, started, counts
from test_prefetch import ControlledExecutor
from test_regressions import state, grass


class BufferTests(unittest.TestCase):
    def setUp(self):
        quiet = patch("builtins.print")
        quiet.start()
        self.addCleanup(quiet.stop)

    def worker(self):
        with patch.object(prefetch, "ThreadPoolExecutor", ControlledExecutor):
            worker = prefetch.DecisionPrefetch(Mock())
        self.addCleanup(worker.close)
        return worker

    def berry(self):
        return {**grass(22, 0.8, True), "prefab": "berrybush"}

    def test_pick_prediction_is_immutable_and_juicy_yield_is_three(self):
        before = state([{**self.berry(), "prefab": "berrybush_juicy"}])
        original = copy.deepcopy(before)
        predicted = predict_success(before, {"kind": "collect", "guid": 22, "prefab": "berrybush_juicy"})
        self.assertEqual(before, original)
        self.assertEqual(counts(predicted)["berries_juicy"], 3)
        self.assertFalse(predicted["nearby"][0]["pickable"])
        self.assertTrue(predicted["_jev_execution_context"]["predicted_success"])
        self.assertNotIn("collect_berrybush_juicy", jev_agent.build_candidates(predicted)[0])

    def test_tool_predictions_spend_materials_and_expose_the_new_tool(self):
        for tool, quantity in (("axe", 1), ("pickaxe", 2)):
            before = state([])
            before["crafting"][tool] = True
            before["player"]["inventory"]["items"] = [
                {"prefab": "twigs", "count": quantity}, {"prefab": "flint", "count": quantity}]
            predicted = predict_success(before, {"kind": "craft_" + tool})
            self.assertEqual(counts(predicted), {tool: 1})
            self.assertFalse(predicted["crafting"][tool])
            self.assertNotIn("craft_" + tool, jev_agent.build_candidates(predicted)[0])
            self.assertEqual(predicted["player"]["inventory"]["equipped"][0]["prefab"], tool)

    def test_tool_prediction_preserves_an_existing_hand_item(self):
        before = state([])
        before["crafting"]["axe"] = True
        before["player"]["inventory"] = {
            "items": [{"prefab": "twigs", "count": 1}, {"prefab": "flint", "count": 1}],
            "equipped": [{"prefab": "spear", "slot": "hands", "count": 1}]}
        predicted = predict_success(before, {"kind": "craft_axe"})
        self.assertEqual(predicted["player"]["inventory"]["equipped"],
                         before["player"]["inventory"]["equipped"])
        self.assertEqual(predicted["player"]["inventory"]["items"][0]["prefab"], "axe")

    def test_known_resource_reentry_does_not_invalidate_but_regrowth_does(self):
        before = state([])
        before["player"]["guid"] = 999
        actual = copy.deepcopy(before)
        actual["nearby"] = [self.berry()]
        with patch.dict(controller.EXPLORATION_SEEN_RESOURCES, {999: {"seen": {(22, "berrybush")}}}):
            self.assertTrue(prefetch.compatible(before, actual))
            before["nearby"] = [{**self.berry(), "pickable": False}]
            self.assertFalse(prefetch.compatible(before, actual))

    def test_unknown_yield_and_unsupported_actions_do_not_predict(self):
        before = state([{"guid": 1, "prefab": "flint", "tags": ["_inventoryitem"]}])
        self.assertIsNone(predict_success(before, {"kind": "collect", "guid": 1, "prefab": "flint"}))
        before["nearby"][0]["stack_count"] = 3
        predicted = predict_success(before, {"kind": "collect", "guid": 1, "prefab": "flint"})
        self.assertEqual(counts(predicted), {"flint": 3})
        self.assertEqual(predicted["nearby"], [])
        for kind in ("explore", "chop_nearest_tree", "cook_food", "craft_torch"):
            self.assertIsNone(predict_success(before, {"kind": kind}))

    def test_start_requires_native_action_and_exact_target_or_recipe(self):
        observed = state([])
        action = {"kind": "collect", "guid": 22}
        self.assertFalse(started(observed, action))
        observed["execution"] = {"doing": True, "action": "PICK", "target_guid": 23}
        self.assertFalse(started(observed, action))
        observed["execution"]["target_guid"] = 22
        self.assertTrue(started(observed, action))
        observed["execution"] = {"doing": True, "action": "BUILD", "recipe": "pickaxe"}
        self.assertFalse(started(observed, {"kind": "craft_axe"}))
        self.assertTrue(started(observed, {"kind": "craft_pickaxe"}))
        observed["execution"]["doing"] = False
        self.assertFalse(started(observed, {"kind": "craft_pickaxe"}))

    def test_predicted_response_cannot_be_consumed_without_completion_confirmation(self):
        worker = self.worker()
        observed = state([])
        worker.submit(observed, {"wait": "Wait"}, {"wait": {"kind": "wait"}}, None, predicted=True)
        worker.future.set_result(({"answer": {"choice": "wait"}}, 0.5))
        self.assertIsNone(worker.take(observed, {"wait": {"kind": "wait"}}))

    def test_latched_remote_start_requires_a_new_request_and_exact_target(self):
        observed = state([])
        action = {"kind": "collect", "guid": 22}
        observed["execution"] = {"doing": False, "action": "PICK", "target_guid": 22,
                                 "issued_at": 10, "confirmed_start": True}
        self.assertTrue(started(observed, action, since=9))
        self.assertFalse(started(observed, action, since=11))
        self.assertFalse(started(observed, {**action, "guid": 23}, since=9))
        observed["execution"]["confirmed_start"] = False
        self.assertFalse(started(observed, action, since=9))

    def test_confirmation_aggregates_stacks_but_rejects_wrong_outcomes_and_new_events(self):
        predicted = state([])
        predicted["player"]["inventory"]["items"] = [{"prefab": "berries", "count": 2}]
        actual = copy.deepcopy(predicted)
        actual["player"]["inventory"]["items"] = [{"prefab": "berries", "count": 1}]*2
        self.assertTrue(prefetch.compatible(predicted, actual))
        for change in ("inventory", "resource", "phase", "damage"):
            with self.subTest(change=change):
                worker = self.worker()
                worker.submit(predicted, {}, {}, None, predicted=True)
                altered = copy.deepcopy(actual)
                if change == "inventory": altered["player"]["inventory"]["items"] = []
                elif change == "resource": altered["nearby"] = [self.berry()]
                elif change == "phase": altered["world"]["phase"] = "dusk"
                else: altered["player"]["vitals"]["health"] -= 1
                self.assertFalse(worker.confirm(altered))
                self.assertTrue(worker.abandoned)

    def test_pipeline_submits_during_first_action_and_consumes_after_confirmation(self):
        worker = self.worker()
        before = state([self.berry()])
        before["player"]["vitals"]["hunger"] = 100
        action = {"kind": "collect", "guid": 22, "prefab": "berrybush"}
        completed = predict_success(before, action)
        completed.pop("_jev_execution_context")
        current = [before]
        first = {"response": {"model": "test"}, "answer": {"choice": "collect_berrybush", "confidence": 1}}
        second = {"response": {"model": "test"}, "answer": {"choice": "eat_safe_food", "confidence": 1}}
        args = SimpleNamespace(log=Path("unused.log"), execute=True, show_probabilities=False,
                               min_confidence=None, move_seconds=1)

        def execute(selected, *_args):
            if selected["kind"] != "collect": return True
            callback = controller._action_observer.get()
            callback({**before, "_pending_action": action}, "action_pending")
            self.assertEqual(len(worker.executor.calls), 0)
            progress = {**before, "execution": {"doing": True, "action": "PICK", "target_guid": 22}}
            callback(progress, "action_progress")
            self.assertEqual(len(worker.executor.calls), 1)
            self.assertFalse(worker.confirmed)
            self.assertEqual(counts(worker.source)["berries"], 1)
            self.assertNotIn("collect_berrybush", worker.dispatch)
            worker.future.set_result((second, 0.5))
            callback(progress, "action_progress")
            current[0] = completed
            callback(completed, "verified")
            self.assertEqual(len(worker.executor.calls), 1)
            return True

        with (
            patch.object(controller, "latest_state", side_effect=lambda *_: (current[0], 100)),
            patch.object(jev_agent, "call_jev", return_value=first) as request,
            patch.object(jev_agent, "execute_action", side_effect=execute) as execute_mock,
        ):
            jev_agent.run_decision_cycle(args, "url", "key", "test", prefetch=worker)
            self.assertTrue(worker.confirmed)
            jev_agent.run_decision_cycle(args, "url", "key", "test", prefetch=worker)
        request.assert_called_once()
        self.assertEqual(execute_mock.call_args.args[0]["kind"], "eat_safe_food")

    def test_progress_observer_is_notified_before_verification(self):
        before, after = state([]), state([])
        before["observed_at"], after["observed_at"] = 1, 2
        observer = Mock()
        with (patch.object(controller, "latest_state", side_effect=[(before, 101), (after, 102)]),
              patch.object(controller, "request_telemetry"), patch.object(controller.time, "sleep")):
            with controller.observe_action(observer):
                controller.wait_for_state_condition(Path("unused"), 100, lambda s: s["observed_at"] == 2)
        self.assertEqual([call.args[1] for call in observer.call_args_list], ["action_progress", "verified"])

    def test_failed_action_or_transient_new_resource_discards_the_buffer(self):
        for failure in ("action_failed", "new_resource"):
            with self.subTest(failure=failure):
                worker = self.worker()
                before = state([self.berry()])
                action = {"kind": "collect", "guid": 22, "prefab": "berrybush"}
                current = [before]
                result = {"response": {"model": "test"},
                          "answer": {"choice": "collect_berrybush", "confidence": 1}}
                args = SimpleNamespace(log=Path("unused.log"), execute=True, show_probabilities=False,
                                       min_confidence=None, move_seconds=1)

                def execute(*_args):
                    callback = controller._action_observer.get()
                    callback({**before, "_pending_action": action}, "action_pending")
                    progress = {**before, "execution": {"doing": True, "action": "PICK", "target_guid": 22}}
                    callback(progress, "action_progress")
                    self.assertEqual(len(worker.executor.calls), 1)
                    if failure == "action_failed":
                        return False
                    changed = copy.deepcopy(progress)
                    changed["nearby"].append(grass(23, 2, True))
                    callback(changed, "action_progress")
                    self.assertTrue(worker.abandoned)
                    # Event disappears before completion: it must not revive
                    # a decision made before that meaningful observation.
                    current[0] = predict_success(before, action)
                    callback(current[0], "verified")
                    return True

                with (patch.object(controller, "latest_state", side_effect=lambda *_: (current[0], 100)),
                      patch.object(jev_agent, "call_jev", return_value=result),
                      patch.object(jev_agent, "execute_action", side_effect=execute)):
                    jev_agent.run_decision_cycle(args, "url", "key", "test", prefetch=worker)
                self.assertTrue(worker.abandoned)
                self.assertFalse(worker.confirmed)
