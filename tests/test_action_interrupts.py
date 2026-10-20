"""Live safety handoff and native-work ownership, without game input or APIs."""
import copy
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import controller
import jev_agent
import timing_log as timing
from board_executor import LocalBoardExecutor
from test_regressions import state, hostile


class ActionInterruptTests(unittest.TestCase):
    def setUp(self):
        self.current = state([{"guid": 20, "prefab": "evergreen", "distance": 2,
                               "tags": ["CHOP_workable"], "work_required": 15}])
        self.current["world"]["phase"] = "dusk"
        self.current["player"]["inventory"] = {
            "items": [{"prefab": "torch", "count": 1, "durability_percent": 1}],
            "equipped": [{"prefab": "axe", "count": 1, "durability_percent": 1}]}
        self.args = SimpleNamespace(log=Path("unused"), execute=True,
            show_probabilities=False, min_confidence=None, move_seconds=1)
        self.enterContext(patch.object(controller.telemetry, "latest_state",
            side_effect=lambda *_: (self.current, 2)))
        self.enterContext(patch.object(controller, "find_game_window", return_value=123))
        self.enterContext(patch.object(controller, "focus_game"))
        self.keys = self.enterContext(patch.object(controller, "tap"))
        self.gather = self.enterContext(patch.object(controller, "collect_nearby_drops"))
        self.api = self.enterContext(patch.object(jev_agent, "call_jev",
            return_value=self.answer("chop_nearest_tree")))

    @staticmethod
    def answer(choice):
        return {"response": {"model": "test"}, "answer": {"choice": choice, "confidence": 1}}

    def acknowledge(self):
        self.current = copy.deepcopy(self.current)
        self.current["work"] = {"sequence": 1, "action": "chop", "target_guid": 20,
            "x": 2, "z": 0, "status": "started"}
        return self.current, 3

    def test_model_work_hands_off_on_night_damage_or_hostile_without_backoff(self):
        original = copy.deepcopy(self.current)
        for event, reason in (("night", "night_light_requires_decision"),
                              ("damage", "health_lost"), ("hostile", "targeted_by_hostile")):
            self.current = copy.deepcopy(original)
            self.keys.reset_mock()
            worker = Mock(take=Mock(return_value=None))
            def wait(*_args, **_kwargs):
                if not self.current.get("work"):
                    return self.acknowledge()
                self.current = copy.deepcopy(self.current)
                if event == "night": self.current["world"]["phase"] = "night"
                elif event == "damage": self.current["player"]["vitals"]["health"] -= 100
                else: self.current["nearby"].append(hostile(99, 2))
                controller.latest_state(self.args.log)
                self.fail("live guard must interrupt work before its timeout")
            with self.subTest(event=event), patch.object(controller, "wait_for_state_condition", side_effect=wait):
                with timing.CycleTrace(1, emit_console=False) as trace:
                    self.assertFalse(jev_agent.run_decision_cycle(self.args, "unused", "unused", "test",
                                                                  prefetch=worker))
                self.assertEqual(trace.record["outcome"], "interrupted")
                self.assertEqual(trace.record["action_handoff_reason"], reason)
                self.assertNotIn("loop.retry_backoff", trace.record["totals"])
                self.keys.assert_called_with([controller.VK["cancel_work"]], 0.04)
                self.gather.assert_not_called()
                worker.discard.assert_called_with("action_interrupted")
                self.assertIsNone(controller._state_guard.get())

        # The following cycle can execute the model's light recovery normally.
        self.current["nearby"] = []
        self.current["world"]["phase"] = "night"
        self.api.return_value = self.answer("equip_torch")
        def equip_wait(_log, _offset, predicate, **_kwargs):
            inv = self.current["player"]["inventory"]
            inv["equipped"] = [{"prefab": "torch", "count": 1}]
            inv["items"] = []
            fresh, pos = controller.latest_state(self.args.log)
            self.assertTrue(predicate(fresh))
            return fresh, pos
        with patch.object(controller, "wait_for_state_condition", side_effect=equip_wait):
            self.assertFalse(jev_agent.run_decision_cycle(self.args, "unused", "unused", "test"))
        self.keys.assert_called_with([controller.VK["equip_torch"]], 0.08)

    def test_work_timeout_and_keyboard_interrupt_cancel_native_task(self):
        original = copy.deepcopy(self.current)
        for failure in (RuntimeError("timeout"), KeyboardInterrupt()):
            self.current = copy.deepcopy(original)
            self.keys.reset_mock()
            ack = self.acknowledge()
            self.current = copy.deepcopy(original)
            with self.subTest(failure=type(failure).__name__), patch.object(
                    controller, "wait_for_state_condition", side_effect=[ack, failure]):
                with self.assertRaises(type(failure)):
                    controller.complete_work_action(self.args.log, "chop_nearest_tree", "CHOP_workable")
            self.keys.assert_called_with([controller.VK["cancel_work"]], 0.04)
            self.gather.assert_not_called()

    def test_no_work_command_means_no_cancel(self):
        self.current["nearby"] = []
        with self.assertRaisesRegex(RuntimeError, "No nearby"):
            controller.complete_work_action(self.args.log, "chop_nearest_tree", "CHOP_workable")
        self.keys.assert_not_called()

    def test_success_keeps_drop_collection_and_does_not_send_cancel(self):
        original = copy.deepcopy(self.current)
        ack = self.acknowledge()
        completed = copy.deepcopy(ack[0])
        completed["work"]["status"] = "completed"
        self.current = original
        with patch.object(controller, "wait_for_state_condition", side_effect=[ack, (completed, 4)]):
            controller.complete_work_action(self.args.log, "chop_nearest_tree", "CHOP_workable")
        self.keys.assert_called_once_with([controller.VK["chop_nearest_tree"]], 0.08)
        self.gather.assert_called_once()

    def test_recovery_guards_do_not_cancel_their_own_light_or_flee_response(self):
        baseline = copy.deepcopy(self.current)
        baseline["world"]["phase"] = "night"
        for kind in ("craft_torch", "equip_torch", "build_campfire"):
            check = LocalBoardExecutor.guard(baseline, {"kind": kind}, allow_recovery=True)
            check(baseline)
            completed = copy.deepcopy(baseline)
            completed["player"]["inventory"]["equipped"] = [{"prefab": "torch"}]
            check(completed)
        check = LocalBoardExecutor.guard(baseline, {"kind": "flee_from_nearest_hostile"}, allow_recovery=True)
        changed = copy.deepcopy(baseline)
        changed["nearby"] = [hostile(99, 2)]
        changed["player"]["vitals"]["health"] -= 50
        check(changed)
        changed["world"]["phase"] = "day"
        check(changed)
        changed["player"]["vitals"]["dead"] = True
        with self.assertRaises(controller.AutoExecutionInterrupted): check(changed)


if __name__ == "__main__":
    unittest.main()
