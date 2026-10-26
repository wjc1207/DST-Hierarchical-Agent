"""Structured planning is validated; only observed facts advance the board."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from concurrent.futures import Future
from unittest.mock import Mock, patch

from mcp_agent import daily_planner
from mcp_agent import jev_client as jev_agent
from common import game_rules as rules
from common.task_board import (validate_board, board_view, format_board,
                        INVENTORY_TARGET_PREFABS, INVENTORY_PREFAB_ALIASES)
from fixtures import state, grass


def raw_board():
    return {"goal": "Prepare light and materials", "tasks": [
        {"id": "grass", "title": "Reserve four grass", "priority": 1,
         "target": {"type": "inventory", "prefab": "cutgrass", "count": 4},
         "depends_on": [], "action_hints": ["collect_grass"]},
        {"id": "torch", "title": "Carry a torch", "priority": 2,
         "target": {"type": "inventory", "prefab": "torch", "count": 1},
         "depends_on": ["grass"], "action_hints": ["craft_torch"]}]}


class BoardExecutor:
    def __init__(self, **_kwargs): self.calls = []
    def submit(self, function, *args):
        self.calls.append((function, args))
        result = Future()
        result.set_result((validate_board(raw_board(), args[3]), 0.75, None))
        return result
    def shutdown(self, **_kwargs): pass


class PendingBoardExecutor:
    def __init__(self, **_kwargs): self.calls, self.futures = [], []
    def submit(self, function, *args):
        self.calls.append((function, args))
        future = Future()
        self.futures.append(future)
        return future
    def shutdown(self, **_kwargs): pass


class BoardTests(unittest.TestCase):
    def completed_state(self):
        actual = state([], cutgrass=4)
        actual["player"]["inventory"]["items"].append({"prefab": "torch", "count": 1})
        return actual

    def pending_planner(self, *, seeded=True):
        with patch.object(daily_planner, "ThreadPoolExecutor", PendingBoardExecutor):
            planner = daily_planner.DailyPlanner("secret", board_log_dir=None)
        self.addCleanup(planner.close)
        if seeded:
            planner.plan = validate_board(raw_board(), 1)
            planner.plan.update(quarter=1, board_id="original")
            planner.requested_slot = (1, 0)
        return planner

    def test_all_tasks_done_replans_once_immediately_without_waiting_for_quarter(self):
        planner = self.pending_planner()
        actual = self.completed_state()
        self.assertIsNone(planner.update(actual))
        self.assertEqual(len(planner.executor.calls), 1)
        context = planner.executor.calls[0][1][4]
        self.assertEqual(context["replan_reason"], "all_tasks_completed")
        self.assertEqual(context["previous_task_board"]["status"], "completed")
        self.assertTrue(all(t["status"] == "done" for t in context["previous_task_board"]["tasks"]))
        self.assertEqual(context["inventory"]["cutgrass"], 4)
        for _ in range(10):
            self.assertIsNone(planner.update(actual))
        self.assertEqual(len(planner.executor.calls), 1)
        self.assertIsNone(planner.plan)

    def test_partial_blocked_predicted_empty_or_dead_board_does_not_trigger_completion(self):
        for scenario in ("partial", "predicted", "dead", "empty"):
            with self.subTest(scenario=scenario):
                planner = self.pending_planner()
                actual = self.completed_state()
                if scenario == "partial": actual["player"]["inventory"]["items"][0]["count"] = 1
                elif scenario == "predicted": actual["_jev_execution_context"] = {"predicted_success": True}
                elif scenario == "dead": actual["player"]["vitals"]["dead"] = True
                else: planner.plan["tasks"] = []
                planner.update(actual)
                self.assertEqual(len(planner.executor.calls), 0)

    def test_already_satisfied_replies_are_rate_limited_not_resubmitted_each_cycle(self):
        planner = self.pending_planner(seeded=False)
        actual = self.completed_state()
        with patch.object(daily_planner.time, "monotonic", return_value=100) as clock:
            planner.update(actual)
            planner.executor.futures[0].set_result((validate_board(raw_board(), 1), 1, None))
            self.assertIsNone(planner.update(actual))
            self.assertEqual(len(planner.executor.calls), 2)
            clock.return_value = 101
            planner.executor.futures[1].set_result((validate_board(raw_board(), 1), 1, None))
            self.assertIsNone(planner.update(actual))
            for _ in range(5): planner.update(actual)
            self.assertEqual(len(planner.executor.calls), 2)
            clock.return_value = 100 + daily_planner.COMPLETION_REPLAN_INTERVAL
            planner.update(actual)
            self.assertEqual(len(planner.executor.calls), 3)

    def test_completion_request_failure_does_not_spin_and_new_quarter_can_request_again(self):
        planner = self.pending_planner()
        actual = self.completed_state()
        planner.update(actual)
        planner.executor.futures[0].set_result((None, 1, RuntimeError("offline")))
        for _ in range(5): self.assertIsNone(planner.update(actual))
        self.assertEqual(len(planner.executor.calls), 1)
        actual["world"]["time"] = 0.26
        planner.update(actual)
        self.assertEqual(len(planner.executor.calls), 2)

    def test_cli_table_shows_tasks_progress_and_blockers_without_control_sequences(self):
        board = validate_board(raw_board(), 1)
        board["tasks"][0]["title"] = "收集草以备夜晚使用" * 8 + "\x1b[2J"
        rendered = format_board(board_view(board, state([], cutgrass=2)), width=80)
        self.assertIn("TASK BOARD", rendered)
        self.assertIn("in_progress", rendered)
        self.assertIn("2/4", rendered)
        self.assertIn("blocked", rendered)
        self.assertIn("dependencies: grass", rendered)
        self.assertNotIn("\x1b", rendered)
        self.assertIn("...", rendered)
        row_widths = [len(line) for line in rendered.splitlines() if line.startswith("+")]
        self.assertEqual(len(set(row_widths)), 1)

    def test_cli_progress_is_throttled_but_status_changes_are_immediate(self):
        with patch.object(daily_planner, "ThreadPoolExecutor", BoardExecutor):
            planner = daily_planner.DailyPlanner("secret", board_log_dir=None)
        self.addCleanup(planner.close)
        board = validate_board(raw_board(), 1)
        board["board_id"] = "test:1"
        with patch.object(daily_planner.time, "monotonic", return_value=10) as clock, patch("builtins.print") as output:
            planner._publish_board(board_view(board, state([], cutgrass=1)))
            self.assertEqual(output.call_count, 1)
            clock.return_value = 11
            planner._publish_board(board_view(board, state([], cutgrass=2)))
            self.assertEqual(output.call_count, 1)
            clock.return_value = 13
            planner._publish_board(board_view(board, state([], cutgrass=2)))
            self.assertEqual(output.call_count, 2)
            clock.return_value = 13.1
            planner._publish_board(board_view(board, state([], cutgrass=4)))
            self.assertEqual(output.call_count, 3)

    def test_status_is_runtime_owned_and_dependencies_are_validated(self):
        raw = raw_board()
        raw["tasks"][0].update(status="done", progress={"current": 999})
        board = validate_board(raw, 1)
        self.assertNotIn("status", board["tasks"][0])
        for variant in ("duplicate", "cycle", "unknown", "bad_target", "too_many"):
            changed = raw_board()
            if variant == "duplicate": changed["tasks"][1]["id"] = "grass"
            elif variant == "cycle": changed["tasks"][0]["depends_on"] = ["torch"]
            elif variant == "unknown": changed["tasks"][0]["depends_on"] = ["missing"]
            elif variant == "bad_target": changed["tasks"][0]["target"]["type"] = "eval"
            else: changed["tasks"] *= 4
            with self.subTest(variant=variant), self.assertRaises(ValueError):
                validate_board(changed, 1)

    def test_inventory_alias_is_normalized_before_progress_and_dependencies(self):
        raw = raw_board()
        raw["tasks"][0].update(id="logs", title="Gather logs")
        raw["tasks"][0]["target"] = {"type": "inventory", "prefab": " LOGS ", "count": 2}
        raw["tasks"][1]["depends_on"] = ["logs"]
        board = validate_board(raw, 1)
        self.assertEqual(board["tasks"][0]["target"]["prefab"], "log")
        self.assertEqual(board["tasks"][0]["id"], "logs")
        self.assertEqual(raw["tasks"][0]["target"]["prefab"], " LOGS ")
        actual = state([])
        actual["player"]["inventory"]["items"] = [{"prefab": "log", "count": 2}]
        view = board_view(board, actual)
        self.assertEqual(view["tasks"][0]["status"], "done")
        self.assertEqual(view["tasks"][1]["status"], "ready")

    def test_inventory_names_are_explicit_not_fuzzy_or_singularized(self):
        for prefab in INVENTORY_TARGET_PREFABS:
            raw = raw_board()
            raw["tasks"][0]["target"]["prefab"] = prefab
            self.assertEqual(validate_board(raw, 1)["tasks"][0]["target"]["prefab"], prefab)
        for invalid in ("loggs", "rock", "twig", "grass", "sapling", "rock1", "unknown_item", "log;wait", None):
            raw = raw_board()
            raw["tasks"][0]["target"]["prefab"] = invalid
            with self.subTest(invalid=invalid), self.assertRaises(ValueError): validate_board(raw, 1)

    def test_inventory_catalog_covers_current_collection_work_and_food_products(self):
        products = set(rules.COLLECT_PRODUCTS.values()) | {"carrot"}
        products |= set().union(*rules.MINE_PRODUCTS.values())
        products |= {"log", "pinecone", "acorn", "charcoal", "livinglog", "marble", "moonrocknugget",
                     "ice", "redgem", "bluegem", "purplegem", "thulecite", "thulecite_pieces"}
        products |= set(rules.SAFE_FOOD_PREFABS)
        products |= set(rules.RAW_COOKABLE_FOOD_PREFABS)
        products |= {"torch", "axe", "pickaxe"}
        self.assertTrue(products <= INVENTORY_TARGET_PREFABS, products - INVENTORY_TARGET_PREFABS)

    def test_observed_inventory_updates_progress_dependencies_and_current_stock(self):
        board = validate_board(raw_board(), 1)
        board["quarter"] = 1
        view = board_view(board, state([], cutgrass=2))
        self.assertEqual([t["status"] for t in view["tasks"]], ["in_progress", "blocked"])
        self.assertEqual(view["ready_task_ids"], ["grass"])
        ready = board_view(board, state([], cutgrass=4))
        self.assertEqual([t["status"] for t in ready["tasks"]], ["done", "ready"])
        self.assertEqual(ready["ready_task_ids"], ["torch"])
        self.assertNotIn("status", board["tasks"][0])
        spent = board_view(board, state([], cutgrass=1))
        self.assertEqual(spent["tasks"][0]["progress"]["current"], 1)
        self.assertEqual(spent["tasks"][1]["status"], "blocked")

    def test_missing_hunger_or_region_telemetry_never_completes_a_task(self):
        raw = {"goal": "Recover and explore", "tasks": [
            {"id": "food", "title": "Eat", "target": {"type": "hunger_ratio", "at_least": 0.8}},
            {"id": "region", "title": "Visit forest", "target": {"type": "visit_region", "node_id": "forest"}}]}
        board = validate_board(raw, 1)
        self.assertTrue(all(t["status"] == "blocked" for t in board_view(board, {"world": {"day": 1}})["tasks"]))
        actual = state([])
        actual["navigation"]["graph"] = {"current_node": "forest"}
        self.assertTrue(all(t["status"] == "done" for t in board_view(board, actual)["tasks"]))


    def test_expired_board_has_no_ready_tasks(self):
        board = validate_board(raw_board(), 1)
        board["quarter"] = 1
        actual = state([])
        actual["world"]["time"] = 0.26
        view = board_view(board, actual)
        self.assertEqual(view["status"], "expired")
        self.assertEqual(view["ready_task_ids"], [])
        self.assertTrue(all(t["status"] == "expired" for t in view["tasks"]))

    def test_planner_logs_board_and_sends_previous_board_to_next_quarter(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(daily_planner, "ThreadPoolExecutor", BoardExecutor):
            planner = daily_planner.DailyPlanner("secret", board_log_dir=Path(directory))
            self.addCleanup(planner.close)
            current = state([], cutgrass=2)
            self.assertIsNone(planner.update(current))
            board = planner.update(current)
            self.assertEqual(board["kind"], "task_board")
            self.assertEqual(board["tasks"][0]["progress"]["current"], 2)
            snapshot = Path(directory, "task_board.json").read_text(encoding="utf8")
            self.assertNotIn("secret", snapshot)
            self.assertEqual(json.loads(snapshot)["task_board"]["board_id"], board["board_id"])
            planner.update(current)
            self.assertEqual(len(Path(directory, "task_board.jsonl").read_text(encoding="utf8").splitlines()), 1)
            current["player"]["inventory"]["items"][0]["count"] = 4
            planner.update(current)
            current["world"]["time"] = 0.26
            self.assertIsNone(planner.update(current))
            context = planner.executor.calls[-1][1][4]
            self.assertEqual(context["previous_task_board"]["status"], "expired")
            self.assertEqual(context["previous_task_board"]["tasks"][0]["progress"]["current"], 4)
            next_board = planner.update(current)
            self.assertNotEqual(next_board["board_id"], board["board_id"])

    def test_jev_receives_board_but_exploration_stays_with_executor(self):
        class Response:
            def __enter__(self): return self
            def __exit__(self, *_args): return False
            def read(self): return b'{"answers":{"next_action":{"choice":"wait","confidence":1}}}'
        board, actual = validate_board(raw_board(), 1), state([])
        criteria = {"wait": "Wait", "explore_current_region": "Explore"}
        with patch.object(jev_agent.urllib.request, "urlopen", return_value=Response()) as send:
            jev_agent.call_jev("https://example.invalid", "secret", "jev", actual, criteria, board)
        body = json.loads(send.call_args.args[0].data)
        supplied = json.loads(body["state"])
        self.assertIn("task_board", supplied)
        self.assertNotIn("daily_plan", supplied)
        self.assertEqual(body["questions"]["next_action"]["criteria"], {"wait": "Wait"})

    def test_jev_priority_payload_limits_role_to_safety_and_planning_gaps(self):
        class Response:
            def __enter__(self): return self
            def __exit__(self, *_args): return False
            def read(self): return b'{"answers":{"next_action":{"choice":"collect_flower"}}}'
        board, actual = validate_board(raw_board(), 1), state([])
        board.update(execution_policy="priority_goals", handoff_reason="safety")
        criteria = {"wait": "Wait", "collect_flower": "Recover sanity", "explore_current_region": "Explore"}
        with patch.object(jev_agent.urllib.request, "urlopen", return_value=Response()) as send:
            result = jev_agent.call_jev("https://example.invalid", "secret", "jev", actual, criteria, board)
        body = json.loads(send.call_args.args[0].data)
        question, supplied = body["questions"]["next_action"], json.loads(body["state"])
        self.assertEqual(result["answer"]["choice"], "collect_flower")
        self.assertEqual(set(question["criteria"]), {"wait", "collect_flower"})
        guidance = " ".join(supplied["decision_guidance"])
        self.assertIn("off-board opportunity arbitration is disabled", guidance)
        self.assertIn("health/hunger/sanity below 30%", guidance)
        self.assertEqual(supplied["task_board"]["handoff_reason"], "safety")

        board["handoff_reason"] = "planning_gap"
        guidance = " ".join(jev_agent.compact_game_state(actual, board)["decision_guidance"])
        self.assertIn("No task on this board can currently be advanced", guidance)
        self.assertIn("temporarily choose useful safe local work", guidance)
        self.assertNotIn("continue_main_task", guidance)

    def test_deepseek_is_prompted_for_interleaved_priority_goals(self):
        class Response:
            def __enter__(self): return self
            def __exit__(self, *_args): return False
            def read(self):
                return json.dumps({"choices": [{"finish_reason": "stop", "message": {
                    "content": json.dumps({"goal":"Gather","tasks":[
                        {"id":"grass","title":"Grass","target":{"type":"inventory","prefab":"cutgrass","count":4}}]})}}]}).encode()
        with patch.object(daily_planner.urllib.request, "urlopen", return_value=Response()) as send:
            board = daily_planner.request_plan("https://example.invalid", "secret", "test", 1, {})
        self.assertEqual(board["execution_policy"], "priority_goals")
        prompt = json.loads(send.call_args.args[0].data)["messages"][0]["content"]
        self.assertIn('"tasks"', prompt)
        self.assertIn("interleaved order", prompt)
        self.assertIn("off-board opportunities do not trigger it", prompt)
        self.assertIn("sanity recovery", prompt)
        self.assertIn("log not logs", prompt)

    def test_unknown_inventory_target_retries_then_normalizes_known_alias(self):
        class Response:
            def __init__(self, prefab): self.prefab = prefab
            def __enter__(self): return self
            def __exit__(self, *_args): return False
            def read(self):
                raw = {"goal":"Get logs","tasks":[{"id":"logs","title":"Logs","target":{"type":"inventory","prefab":self.prefab,"count":4}}]}
                return json.dumps({"choices": [{"finish_reason": "stop", "message": {
                    "content": json.dumps(raw)}}]}).encode()
        with patch.object(daily_planner.urllib.request, "urlopen",
                          side_effect=[Response("loggs"), Response("logs")]) as send:
            board = daily_planner.request_plan("https://example.invalid", "secret", "test", 1, {})
        self.assertEqual(send.call_count, 2)
        self.assertEqual(board["tasks"][0]["target"]["prefab"], "log")

    def test_planner_receives_canonical_inventory_names_and_aliases(self):
        planner = self.pending_planner(seeded=False)
        planner.update(state([]))
        capabilities = planner.executor.calls[0][1][6]["supported_capabilities"]
        self.assertEqual(capabilities["inventory_target_prefabs"], sorted(INVENTORY_TARGET_PREFABS))
        self.assertEqual(capabilities["inventory_prefab_aliases"], INVENTORY_PREFAB_ALIASES)
        self.assertIn("log", capabilities["inventory_target_prefabs"])
        self.assertNotIn("logs", capabilities["inventory_target_prefabs"])


    def test_verified_science_machine_fact_completes_task_even_after_leaving_it(self):
        raw = {"goal": "Research", "tasks": [{"id": "science", "title": "Build science machine",
               "target": {"type": "structure", "prefab": "sciencemachine"}}]}
        board = validate_board(raw, 1)
        self.assertEqual(board_view(board, state([]))["tasks"][0]["status"], "ready")
        self.assertEqual(board_view(board, state([]), verified_structures=["sciencemachine"])["tasks"][0]["status"], "done")
