"""Local board dispatch is model-free, bounded, observed and interruptible."""
import copy
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import controller
import jev_agent
import timing_log as timing
from board_executor import LocalBoardExecutor, task_progressed
from task_board import validate_board
from test_regressions import state, grass, hostile, lit_campfire


def task(name, prefab=None, priority=1, depends=(), target=None, count=2):
    return {"id": name, "title": name, "priority": priority,
            "target": target or {"type": "inventory", "prefab": prefab, "count": count},
            "depends_on": list(depends)}


def board(*tasks):
    result = validate_board({"goal": "Local stock", "tasks": list(tasks)}, 1)
    return {**result, "board_id": "test-board", "quarter": 1}


def args(execute=True):
    return SimpleNamespace(log=Path("unused.log"), execute=execute,
                           move_seconds=1, min_confidence=None, show_probabilities=False)


class LocalBoardTests(unittest.TestCase):
    def setUp(self):
        self.executor = LocalBoardExecutor()
        self.addCleanup(controller.COLLECTION_TARGET_PAUSES.clear)
        controller.COLLECTION_TARGET_PAUSES.clear()

    def choose(self, plan, actual):
        return self.executor.choose(plan, actual, jev_agent.build_candidates(actual)[1])

    def test_priority_dependency_and_unavailable_task(self):
        actual = state([grass(9001, 2, True)])
        twigs = {**grass(9002, 1, True), "prefab": "sapling"}
        actual["nearby"].append(twigs)
        plan = board(task("grass", "cutgrass"), task("twigs", "twigs", 2))
        self.assertEqual(self.choose(plan, actual)[0]["id"], "grass")
        actual["nearby"][0]["pickable"] = False
        self.assertEqual(self.choose(plan, actual)[0]["id"], "twigs")
        plan["tasks"][1]["depends_on"] = ["grass"]
        self.assertIsNone(self.choose(plan, actual))

    def test_only_board_resources_no_exploration_or_off_board_opportunities(self):
        actual = state([grass(9001, 1, True)])
        for target in ({"type": "inventory", "prefab": "twigs", "count": 2},
                       {"type": "known_regions", "count": 3},
                       {"type": "visit_region", "node_id": "A"}):
            with self.subTest(target=target):
                self.assertIsNone(self.choose(board(task("goal", target=target)), actual))

    def test_done_expired_predicted_retired_and_unreachable_are_not_executed(self):
        plan = board(task("grass", "cutgrass"))
        for case in ("done", "expired", "predicted", "retired", "blocked"):
            actual, candidate = state([grass(9001, 2, True)]), copy.deepcopy(plan)
            if case == "done": actual = state(actual["nearby"], cutgrass=2)
            elif case == "expired": actual["world"]["time"] = 0.3
            elif case == "predicted": actual["_jev_execution_context"] = {"predicted_success": True}
            elif case == "retired": candidate["status"] = "completed"
            else: actual["nearby"][0]["collect_path"]["status"] = "blocked"
            with self.subTest(case=case):
                self.assertIsNone(self.choose(candidate, actual))

    def test_failures_pause_exact_target_not_another_task_or_board(self):
        actual = state([grass(9001, 1, True), {**grass(9002, 2, True), "prefab": "sapling"}])
        plan = board(task("grass", "cutgrass"), task("twigs", "twigs", 2))
        chosen = self.choose(plan, actual)
        with patch("board_executor.time.monotonic", return_value=100):
            self.executor.pause(plan, chosen[0], chosen[2])
            self.assertEqual(self.choose(plan, actual)[0]["id"], "twigs")
            self.assertEqual(self.choose({**plan, "board_id": "new"}, actual)[0]["id"], "grass")
        with patch("board_executor.time.monotonic", return_value=116):
            self.assertEqual(self.choose(plan, actual)[0]["id"], "grass")

    def test_work_requires_tool_safety_and_new_mod_and_matches_native_nearest(self):
        rock = {**grass(9003, 3, True), "prefab": "rock2", "tags": ["MINE_workable"],
                "work_required": 6}
        actual = state([rock])
        actual["telemetry"] = {"cancel_work": True}
        actual["player"]["inventory"]["items"] = [{"prefab": "pickaxe", "durability_percent": 1}]
        plan = board(task("gold", "goldnugget"))
        self.assertEqual(self.choose(plan, actual)[2]["_board_target_guid"], 9003)
        for case in ("old_mod", "tool", "path", "nearer_wrong_product"):
            changed = copy.deepcopy(actual)
            if case == "old_mod": changed["telemetry"] = {}
            elif case == "tool": changed["player"]["inventory"]["items"][0]["durability_percent"] = 0.01
            elif case == "path": changed["nearby"][0]["collect_path"]["status"] = "blocked"
            else: changed["nearby"].append({**rock, "prefab": "rock1", "guid": 9004, "distance": 1})
            with self.subTest(case=case): self.assertIsNone(self.choose(plan, changed))

    def test_tool_task_crafts_but_log_goal_does_not_invent_tool_subtask(self):
        actual = state([])
        actual["crafting"]["axe"] = True
        self.assertEqual(self.choose(board(task("axe", "axe", count=1)), actual)[1], "craft_axe")
        self.assertIsNone(self.choose(board(task("logs", "log")), actual))

    def test_normalized_log_goal_matches_canonical_loose_resource(self):
        resource = {**grass(9005, 2, True), "prefab": "log", "tags": ["_inventoryitem"]}
        plan = board(task("logs", "logs"))
        self.assertEqual(self.choose(plan, state([resource]))[1], "collect_log")

    def test_existing_hunger_and_night_rules_still_block_optional_collection(self):
        plan = board(task("grass", "cutgrass"))
        actual = state([grass(9001, 2, True)])
        actual["player"]["vitals"]["hunger"] = 10
        actual["player"]["inventory"]["items"].append({"prefab": "berries", "count": 1})
        self.assertIsNone(self.choose(plan, actual))
        actual = state([grass(9001, 2, True), lit_campfire()])
        actual["world"]["phase"] = "night"
        self.assertIsNone(self.choose(plan, actual))

    def test_structure_goal_uses_only_an_existing_legal_build_action(self):
        actual = state([])
        actual["crafting"]["sciencemachine"] = True
        plan = board(task("science", target={"type": "structure", "prefab": "sciencemachine"}))
        self.assertEqual(self.choose(plan, actual)[1], "build_sciencemachine")
        done = state([{"prefab": "sciencemachine", "distance": 3}])
        self.assertTrue(task_progressed(plan, plan["tasks"][0], actual, done))
        self.assertIsNone(self.choose(plan, done))

    def test_guard_ignores_new_resources_but_interrupts_threat_damage_and_light(self):
        original = state([])
        check = self.executor.guard(original)
        check(state([grass(9001, 2, True)]))
        for case in ("threat", "damage", "phase", "player"):
            changed = copy.deepcopy(original)
            if case == "threat": changed["nearby"] = [hostile(9009, 10)]
            elif case == "damage": changed["player"]["vitals"]["health"] -= 1
            elif case == "phase": changed["world"]["phase"] = "dusk"
            else: changed["player"]["guid"] = 2
            with self.subTest(case=case), self.assertRaises(controller.AutoExecutionInterrupted): check(changed)
        night = state([lit_campfire()])
        night["world"]["phase"] = "night"
        self.assertIsNone(self.executor.initial_handoff(night))
        with self.assertRaises(controller.AutoExecutionInterrupted):
            self.executor.guard(night)({**night, "nearby": []})

    def test_guard_checks_actual_work_ack_target(self):
        actual = state([])
        changed = {**actual, "work": {"sequence": 1, "target_guid": 999}}
        with self.assertRaisesRegex(controller.AutoExecutionInterrupted, "work_target_changed"):
            self.executor.guard(actual, {"_board_target_guid": 9003})(changed)

    def test_task_progress_not_passage_of_time_or_unrelated_loot(self):
        plan = board(task("grass", "cutgrass"))
        before, after = state([]), state([])
        after["player"]["vitals"]["hunger"] -= 1
        after["player"]["inventory"]["items"].append({"prefab": "flint", "count": 1})
        self.assertFalse(task_progressed(plan, plan["tasks"][0], before, after))
        self.assertTrue(task_progressed(plan, plan["tasks"][0], before, state([], cutgrass=1)))

    def test_held_movement_released_when_live_guard_interrupts(self):
        held = set()
        def key(vk, down): held.add(vk) if down else held.discard(vk)
        current = state([hostile(9009, 2)])
        with patch.object(controller, "send_key", side_effect=key), \
                patch.object(controller.telemetry, "latest_state", return_value=(current, 1)):
            with controller.guard_states(Path("unused"), self.executor.guard(state([]))):
                with self.assertRaises(controller.AutoExecutionInterrupted):
                    controller.tap([controller.VK["up"]], 0.5)
        self.assertFalse(held)
        self.assertIsNone(controller._state_guard.get())


class BoardLoopTests(unittest.TestCase):
    def setUp(self):
        self.current = state([grass(9101, 1, True), {**grass(9102, 2, True), "prefab": "sapling"}])
        self.plan = board(task("grass", "cutgrass", count=2), task("twigs", "twigs", 2, count=1))
        self.planner = Mock(update=Mock(side_effect=lambda *_: self.plan))
        self.prefetch = Mock(take=Mock(return_value=None))
        self.executor = LocalBoardExecutor()
        self.addCleanup(controller.COLLECTION_TARGET_PAUSES.clear)
        controller.COLLECTION_TARGET_PAUSES.clear()
        # Use real latest_state/guard logic, only replace the telemetry source.
        self.read = self.enterContext(patch.object(controller.telemetry, "latest_state",
            side_effect=lambda *_: (self.current, 1)))
        self.release = self.enterContext(patch.object(controller, "release_movement_keys"))
        self.key = self.enterContext(patch.object(controller, "tap"))
        self.request = self.enterContext(patch.object(jev_agent, "call_jev", return_value={
            "response": {"model": "test"}, "answer": {"choice": "wait", "confidence": 1}}))
        self.execute = self.enterContext(patch.object(jev_agent, "execute_action", side_effect=self.collect))

    def collect(self, action, *_):
        self.assertIsNone(controller._action_observer.get())
        product = controller.COLLECT_PRODUCTS.get(action.get("prefab"), action.get("prefab"))
        updated = copy.deepcopy(self.current)
        updated["player"]["inventory"]["items"].append({"prefab": product, "count": 1})
        updated["nearby"] = [e for e in updated["nearby"] if e["guid"] != action.get("guid")]
        self.current = updated
        return True

    def cycle(self, execute=True):
        return jev_agent.run_decision_cycle(args(execute), "unused", "unused", "test",
            self.planner, self.prefetch, self.executor)

    def test_consecutive_actions_and_next_task_without_jev_or_prefetch(self):
        self.current["nearby"].append(grass(9104, 3, True))
        for _ in range(3): self.assertFalse(self.cycle())
        self.assertEqual([c.args[0]["prefab"] for c in self.execute.call_args_list], ["grass", "grass", "sapling"])
        self.assertTrue(all(c.args[0]["_strict_preferred"] for c in self.execute.call_args_list))
        self.request.assert_not_called()
        self.prefetch.take.assert_not_called()
        self.prefetch.submit.assert_not_called()
        self.assertEqual(self.planner.record_action.call_count, 3)
        self.assertEqual(self.prefetch.discard.call_count, 3)

    def test_auto_timing_records_source_and_zero_model_requests(self):
        with timing.CycleTrace(1, emit_console=False) as trace:
            self.cycle()
        self.assertEqual(trace.record["decision_source"], "board_auto")
        self.assertEqual(trace.record["board_task_id"], "grass")
        self.assertEqual(trace.record["board_auto_outcome"], "executed")
        self.assertIn("board.auto_execute", trace.record["totals"])
        self.assertNotIn("decision.request", trace.record["totals"])

    def test_second_torch_is_automatically_crafted_then_goal_stops_execution(self):
        self.plan = board(task("reserve", "torch", count=2))
        self.current = state([])
        self.current["crafting"]["torch"] = True
        self.current["player"]["inventory"]["items"] = [{"prefab": "torch", "count": 1}]
        def craft(action, *_):
            self.assertEqual(action["kind"], "craft_torch")
            updated = copy.deepcopy(self.current)
            updated["player"]["inventory"]["items"][0]["count"] = 2
            self.current = updated
            return True
        self.execute.side_effect = craft
        self.cycle()
        self.request.assert_not_called()
        self.assertEqual(self.execute.call_count, 1)
        self.assertIsNone(self.executor.choose(self.plan, self.current,
            jev_agent.build_candidates(self.current)[1]))

    def test_preflight_rejects_changed_target_before_sending_input(self):
        original = self.current
        self.current = state([grass(9101, 1, False)])
        self.assertFalse(jev_agent.run_local_board_step(args(), original, self.plan,
            jev_agent.build_candidates(original)[1], self.planner, self.prefetch, self.executor))
        self.execute.assert_not_called()
        self.key.assert_not_called()

    def test_missing_local_target_hands_back_to_jev(self):
        self.current = state([])
        self.execute.side_effect = None
        self.execute.return_value = True
        self.cycle()
        self.request.assert_called_once()
        self.assertEqual(self.execute.call_args.args[0]["kind"], "wait")

    def test_failed_or_no_progress_action_is_paused_and_hands_back(self):
        for failure in (False, RuntimeError("timeout"), True):
            with self.subTest(failure=failure):
                self.executor = LocalBoardExecutor()
                self.execute.side_effect = [failure, True]
                self.cycle()
                self.assertEqual(len(self.executor.paused), 1)
        self.assertEqual(self.request.call_count, 3)

    def test_attack_or_light_event_uses_live_state_for_jev(self):
        for change in ("attack", "phase"):
            def interrupt(*_):
                updated = copy.deepcopy(self.current)
                if change == "attack": updated["nearby"].append(hostile(9199, 9))
                else: updated["world"]["phase"] = "dusk"
                self.current = updated
                controller.latest_state(Path("unused"))
                self.fail("guard must interrupt before the next operation")
            self.current = state([grass(9101, 1, True)])
            self.execute.side_effect = lambda action, *_: interrupt() if action["kind"] == "collect" else True
            self.cycle()
            self.assertIs(self.request.call_args.args[3], self.current)
        self.assertEqual(self.request.call_count, 2)

    def test_eat_goal_can_exceed_regular_jev_eating_heuristic(self):
        self.plan = board(task("fed", target={"type": "hunger_ratio", "at_least": 0.98}))
        self.current["player"]["inventory"]["items"].append({"prefab": "berries", "count": 2})
        self.assertNotIn("eat_safe_food", jev_agent.build_candidates(self.current)[1])
        def eat(action, *_):
            self.assertEqual(action["kind"], "eat_safe_food")
            updated = copy.deepcopy(self.current)
            updated["player"]["vitals"]["hunger"] = 150
            self.current = updated
            return True
        self.execute.side_effect = eat
        self.cycle()
        self.request.assert_not_called()

    def test_dry_run_never_automatically_executes_board(self):
        self.cycle(execute=False)
        self.execute.assert_not_called()
        self.key.assert_not_called()
        self.request.assert_called_once()

    def test_interrupted_work_including_ctrl_c_cancels_mod_dispatch(self):
        tree = {**grass(9103, 2, True), "prefab": "evergreen", "tags": ["CHOP_workable"],
                "work_required": 5}
        self.current = state([tree])
        self.current["telemetry"] = {"cancel_work": True}
        self.current["player"]["inventory"]["items"] = [{"prefab": "axe", "durability_percent": 1}]
        self.plan = board(task("logs", "log"))
        with patch.object(controller, "find_game_window", return_value=123), \
                patch.object(controller, "focus_game") as focus:
            for failure in (controller.AutoExecutionInterrupted("targeted_by_hostile"), KeyboardInterrupt()):
                self.execute.side_effect = failure
                with self.subTest(failure=type(failure).__name__):
                    if isinstance(failure, KeyboardInterrupt):
                        with self.assertRaises(KeyboardInterrupt): self.cycle()
                    else:
                        # Test only the auto step: normal JEV execution is a separate path.
                        self.assertFalse(jev_agent.run_local_board_step(args(), self.current, self.plan,
                            jev_agent.build_candidates(self.current)[1], self.planner, self.prefetch, self.executor))
                    self.key.assert_called_with([controller.VK["cancel_work"]], 0.04)
            self.assertEqual(focus.call_count, 2)


if __name__ == "__main__":
    unittest.main()
