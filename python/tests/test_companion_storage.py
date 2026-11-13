import copy
import json
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from test_companion import state, TOKEN, FakePeer, ImmediateWorker
from mcp_agent.companion_tasks import CompanionTasks, validate_board
from mcp_agent.companion_planner import request_plan, validate_plan
from mcp_agent.companion_agent import CompanionAgent
from mcp_server.companion_bridge import CompanionBridge
from mcp_server.game_bridge import BridgeError


def observation():
    observed = state()["companion"]
    observed["capabilities"] += ["storage", "give_owner", "drop_item", "receive_item"]
    observed.update(transfer_receipts=[], containers=[{"guid": 300001, "prefab": "treasurechest", "x": 6, "z": 0,
        "contents_known": True, "slot_count": 9, "free_slots": 8, "items": [{"guid": 400001, "prefab": "cutgrass", "count": 7, "slot": 1}]}])
    observed["buddy"]["inventory"] = {"cutgrass": 3}
    return observed


def board(kind="give_owner", count=3, **fields):
    target = {"type": kind}
    if kind != "inspect_storage": target.update(prefab="cutgrass", count=count)
    if kind in {"deposit", "withdraw", "inspect_storage"}: target["container_guid"] = 300001
    target.update(fields)
    return validate_board({"goal": "Exchange materials", "reply": "开始交接。", "tasks": [{"id": "exchange", "title": "Exchange", "target": target}]}, 1)


class StorageTasksTests(unittest.TestCase):
    def test_total_transfer_progress_comes_from_receipts_not_current_destination_stock(self):
        observed = observation();runtime = CompanionTasks();runtime.adopt(board("withdraw"), observed)
        task, op, args = runtime.next_action(observed)
        self.assertEqual(op, "withdraw")  # Already carrying three does not satisfy withdrawing three.
        self.assertEqual(args["total_count"], 3)
        observed["buddy"]["inventory"]["cutgrass"] = 80
        self.assertEqual(runtime.view(observed)["tasks"][0]["progress"]["current"], 0)
        observed["transfer_receipts"] = [{"task_key": args["task_key"], "count": 3}]
        self.assertEqual(runtime.next_action(observed)[1], "completed")

    def test_result_and_snapshot_are_idempotent_and_quantity_revision_keeps_receipt_key(self):
        observed = observation();runtime = CompanionTasks();runtime.adopt(board("deposit"), observed)
        _, _, args = runtime.next_action(observed)
        action = {"status": "completed", "data": {"task_key": args["task_key"], "count": 1}}
        runtime.on_result("exchange", action, observed);runtime.on_result("exchange", action, observed)
        observed["transfer_receipts"] = [action["data"]]
        self.assertEqual(runtime.view(observed)["tasks"][0]["progress"]["current"], 1)
        runtime.adopt(board("deposit", count=5), observed)
        _, op, changed = runtime.next_action(observed)
        self.assertEqual((op, changed["total_count"], changed["task_key"]), ("deposit", 5, args["task_key"]))
        # Another chest is a different operation, so it must not inherit this receipt.
        observed["containers"].append({**observed["containers"][0], "guid": 300002})
        runtime.adopt(board("deposit", container_guid=300002), observed)
        self.assertNotEqual(runtime.next_action(observed)[2]["task_key"], args["task_key"])

    def test_a_finished_batch_retains_counted_progress_and_new_requests_use_new_ids(self):
        observed = observation();runtime = CompanionTasks();runtime.adopt(board(), observed)
        _, _, args = runtime.next_action(observed)
        observed["transfer_receipts"] = [{"task_key": args["task_key"], "count": 3}]
        self.assertEqual(runtime.next_action(observed)[1], "completed")
        runtime.adopt(board(count=4), observed)
        self.assertEqual(runtime.next_action(observed)[2]["task_key"], args["task_key"])
        observed["transfer_receipts"][0]["count"] = 4
        self.assertEqual(runtime.next_action(observed)[1], "completed")
        fresh = board();fresh["tasks"][0]["id"] = "new_gift"
        runtime.adopt(fresh, observed)
        self.assertNotEqual(runtime.next_action(observed)[2]["task_key"], args["task_key"])

    def test_far_owner_is_followed_before_giving_and_missing_materials_request_planning(self):
        observed = observation();observed["buddy"]["owner_distance"] = 100
        runtime = CompanionTasks();runtime.adopt(board(), observed)
        self.assertEqual(runtime.next_action(observed)[1], "follow")
        observed["mode"] = "follow"
        self.assertEqual(runtime.next_action(observed)[1], "waiting")
        observed["buddy"]["owner_distance"] = 10
        self.assertEqual(runtime.next_action(observed)[1], "give_owner")
        observed["buddy"]["inventory"] = {}
        self.assertEqual(runtime.next_action(observed)[1], "needs_replan")

    def test_receiving_waits_for_voluntary_stock_without_gathering_and_has_a_timeout(self):
        clock = [0.];observed = observation();observed["buddy"].update(inventory={}, owner_distance=2)
        runtime = CompanionTasks(clock=lambda: clock[0]);runtime.adopt(board("receive_item", timeout_seconds=10), observed)
        self.assertEqual(runtime.next_action(observed)[1], "awaiting_items")
        clock[0] = 11
        self.assertIn("timed_out", runtime.next_action(observed)[2]["reason"])
        observed["buddy"]["inventory"] = {"cutgrass": 3}
        self.assertEqual(runtime.next_action(observed)[1], "completed")

    def test_inspection_requires_current_known_contents_and_busy_is_not_empty(self):
        observed = observation();runtime = CompanionTasks();runtime.adopt(board("inspect_storage"), observed)
        self.assertEqual(runtime.next_action(observed)[1], "completed")
        observed["containers"][0].update(contents_known=False, items=[])
        runtime = CompanionTasks();runtime.adopt(board("withdraw"), observed)
        self.assertEqual(runtime.next_action(observed)[1], "needs_replan")

    def test_busy_chest_waits_locally_and_closing_resumes_same_receipt_and_progress(self):
        clock = [0.];observed = observation();runtime = CompanionTasks(clock=lambda: clock[0])
        runtime.adopt(board("deposit"), observed);_, _, args = runtime.next_action(observed)
        observed["transfer_receipts"] = [{"task_key": args["task_key"], "count": 1}]
        chest = observed["containers"][0];chest.update(contents_known=False, reason="chest_busy", items=[])
        self.assertEqual(runtime.next_action(observed)[1], "waiting_storage")
        clock[0] = 59
        self.assertEqual(runtime.next_action(observed)[1], "waiting_storage")
        clock[0] = 61
        self.assertEqual(runtime.next_action(observed)[2]["reason"], "chest_busy_timeout")
        chest.update(contents_known=True, reason=None);observed["version"] += 1
        _, op, resumed = runtime.next_action(observed)
        self.assertEqual((op, resumed["task_key"]), ("deposit", args["task_key"]))
        self.assertEqual(runtime.view(observed)["tasks"][0]["progress"]["current"], 1)

    def test_busy_result_race_does_not_retry_the_same_stale_snapshot_immediately(self):
        clock = [0.];observed = observation();runtime = CompanionTasks(clock=lambda: clock[0]);runtime.adopt(board("withdraw"), observed)
        runtime.next_action(observed)
        self.assertTrue(runtime.on_result("exchange", {"status": "failed", "reason": "chest_busy"}, observed))
        self.assertEqual(runtime.next_action(observed)[1], "waiting_storage")
        clock[0] = .6
        self.assertEqual(runtime.next_action(observed)[1], "withdraw")
        # A new player instruction starts a fresh wait, preserving the transfer count.
        runtime.pause_search()
        self.assertNotIn("storage_wait_started", runtime.goals.records["exchange"])

    def test_busy_inspection_waits_but_a_removed_chest_requires_replanning(self):
        observed = observation();chest = observed["containers"][0]
        chest.update(contents_known=False, reason="chest_busy", items=[])
        runtime = CompanionTasks();runtime.adopt(board("inspect_storage"), observed)
        self.assertEqual(runtime.next_action(observed)[1], "waiting_storage")
        chest.update(contents_known=True, reason=None)
        self.assertEqual(runtime.next_action(observed)[1], "completed")
        runtime = CompanionTasks();runtime.adopt(board("deposit"), observed)
        observed["containers"] = []
        self.assertEqual(runtime.next_action(observed)[2]["reason"], "chest_unavailable")

    def test_no_take_owner_target_and_exact_storage_guid_is_required_by_the_planner(self):
        observed = observation()
        for target in ({"type": "take_owner", "prefab": "cutgrass", "count": 1},
                       {"type": "deposit", "prefab": "cutgrass", "count": 1, "container_guid": True}):
            with self.assertRaises(ValueError):
                validate_board({"goal": "Invalid", "tasks": [{"id": "x", "title": "x", "target": target}]}, 1)
        with self.assertRaisesRegex(ValueError, "observed chest"):
            validate_plan(board("deposit", container_guid=999), {"observation": observed})

    def test_transfer_respects_shared_dependencies_focus_and_scheduled_start(self):
        observed = observation();plan = board("deposit")
        plan["tasks"][0]["depends_on"] = ["supply"]
        plan["tasks"].append({"id": "supply", "title": "Wait", "focus": True, "target": {"type": "receive_item", "prefab": "twigs", "count": 1}})
        runtime = CompanionTasks();runtime.adopt(plan, observed)
        self.assertEqual(runtime.next_action(observed)[0]["id"], "supply")
        self.assertNotIn("transfer_key", runtime.goals.records["exchange"])
        observed["buddy"]["inventory"]["twigs"] = 1
        self.assertEqual(runtime.next_action(observed)[1], "deposit")
        timed = board("drop_item");timed["tasks"][0]["start_at"] = {"day": 2, "phase": "day"}
        runtime = CompanionTasks();runtime.adopt(timed, observed)
        self.assertEqual(runtime.next_action(observed)[1], "idle")


class StorageBridgeTests(unittest.TestCase):
    def setUp(self):
        self.bridge = CompanionBridge(TOKEN, clock=lambda: 0);self.data = state();self.data["companion"] = observation();self.seq = 0;self.exchange()
    def exchange(self, results=None):
        self.seq += 1
        return self.bridge.exchange({"token": TOKEN, "session_id": "local", "sequence": self.seq, "state": self.data, "results": results or []})
    def action(self, op, args): return self.bridge.action(op, self.bridge.observe()["observation_id"], args)
    def test_mutating_actions_require_current_capabilities_guid_and_valid_quantity(self):
        args = {"task_key": "a", "prefab": "cutgrass", "total_count": 3, "container_guid": 300001}
        for bad in ({**args, "total_count": True}, {**args, "total_count": 101}, {**args, "container_guid": 999}, {**args, "owner_guid": 8}):
            with self.assertRaises(ValueError): self.action("deposit", bad)
        with self.assertRaises(ValueError): self.action("take_owner", {})
        self.assertEqual(self.action("deposit", args)["op"], "deposit")
    def test_readonly_query_does_not_consume_the_native_action_queue_or_expose_owner_inventory(self):
        result = self.bridge.storage(300001)
        self.assertEqual(result["containers"][0]["items"][0]["count"], 7)
        self.assertIsNone(self.bridge.active)
        result["containers"][0]["items"].clear()
        self.assertTrue(self.bridge.storage()["containers"][0]["items"])
        self.assertNotIn("inventory", self.bridge.observe()["state"]["companion"]["owner"])
        self.data["snapshot_age_ms"] = 2500;self.exchange()
        with self.assertRaises(ValueError): self.bridge.storage()
    def test_receipt_survives_result_transport_and_new_intent_rejects_old_commands(self):
        result = self.action("give_owner", {"task_key": "a", "prefab": "cutgrass", "total_count": 1})
        self.exchange([{"id": result["id"], "revision": 1, "status": "completed", "data": {"task_key": "a", "count": 1}}])
        self.assertEqual(self.bridge.result(result["id"])["data"]["count"], 1)
        old = self.bridge.observe()["observation_id"]
        self.data["control"]["revision"] = self.data["companion"]["revision"] = 2;self.exchange()
        with self.assertRaises(ValueError): self.bridge.action("drop_item", old, {"task_key": "b", "prefab": "cutgrass", "total_count": 1})


class TransferPeer(FakePeer):
    def __init__(self):
        super().__init__();self.state["companion"] = observation();self.exchange()
    async def call_tool(self, name, args):
        if name != "buddy_action" or args["op"] not in {"deposit", "withdraw", "give_owner", "drop_item"}:
            return await super().call_tool(name, args)
        self.calls.append((name, args));record = self.bridge.action(**args)
        observed = self.state["companion"];details = args["args"]
        receipts = observed["transfer_receipts"]
        row = next((r for r in receipts if r["task_key"] == details["task_key"]), None)
        if row is None: row = {"task_key": details["task_key"], "count": 0};receipts.append(row)
        row["count"] += 1
        observed["buddy"]["inventory"][details["prefab"]] += 1 if args["op"] == "withdraw" else -1
        observed["version"] += 1
        self.exchange([{"id": record["id"], "status": "completed", "revision": 1, "data": dict(row), "reason": "one_item_transferred"}])
        return SimpleNamespace(is_error=False, structured_content=record)


class BusyTransferPeer(TransferPeer):
    def __init__(self):
        super().__init__();self.raced_counts = set()
    async def call_tool(self, name, args):
        observed = self.state["companion"]
        if name == "buddy_action" and args["op"] == "deposit":
            count = sum(row["count"] for row in observed["transfer_receipts"])
            if count not in self.raced_counts:
                self.raced_counts.add(count);self.calls.append((name, args));record = self.bridge.action(**args)
                observed["containers"][0].update(contents_known=False, reason="chest_busy", busy_openers=[{"guid": 10, "is_owner": True}])
                observed["version"] += 1
                self.exchange([{"id": record["id"], "status": "failed", "revision": 1, "reason": "chest_busy"}])
                return SimpleNamespace(is_error=False, structured_content=record)
        return await super().call_tool(name, args)


class StorageAgentTests(unittest.IsolatedAsyncioTestCase):
    async def test_three_separate_openings_do_not_accumulate_failures_or_replan_and_close_resumes(self):
        peer = BusyTransferPeer();contexts = []
        def planner(context, *_):
            contexts.append(context)
            return board("deposit") if len(contexts) == 1 else {"kind": "agent_reply", "outcome": "completed", "reply": "已存好三根草。", "evidence": "累计回执三根。"}
        with patch("mcp_agent.companion_agent.ThreadPoolExecutor", ImmediateWorker):
            agent = CompanionAgent(peer, planner, execute=True)
        try:
            for _ in range(3):
                for _ in range(8): await agent.cycle()
                self.assertEqual(len(contexts), 1)
                self.assertEqual(agent.failures, 0)
                peer.state["companion"]["containers"][0].update(contents_known=True, reason=None)
                peer.state["companion"]["version"] += 1;peer.exchange()
            for _ in range(8): await agent.cycle()
            self.assertEqual(len(contexts), 2)
            self.assertEqual(peer.state["companion"]["transfer_receipts"][0]["count"], 3)
            self.assertEqual(len(peer.raced_counts), 3)
            self.assertEqual(agent.failures, 0)
        finally: await agent.close()

    async def test_actual_transfer_progress_clears_old_action_failure_streak(self):
        peer = TransferPeer()
        with patch("mcp_agent.companion_agent.ThreadPoolExecutor", ImmediateWorker):
            agent = CompanionAgent(peer, lambda *_: board(), execute=True)
        try:
            await agent.cycle();await agent.cycle()
            self.assertIsNotNone(agent.active)
            agent.failures = 2;agent.last_failure = ("action", "native_action_failed")
            await agent.cycle()
            self.assertEqual(agent.failures, 0)
            self.assertEqual(peer.state["companion"]["transfer_receipts"][0]["count"], 1)
            # A fresh observation after processing the receipt precedes the next transfer.
            await agent.cycle()
            self.assertEqual(agent.failures, 0)
            self.assertEqual(peer.state["companion"]["transfer_receipts"][0]["count"], 2)
        finally: await agent.close()

    async def test_three_units_delivered_once_and_only_initial_and_completion_planning(self):
        peer = TransferPeer();contexts = []
        def planner(context, *_):
            contexts.append(context)
            return board() if len(contexts) == 1 else {"kind": "agent_reply", "outcome": "completed", "reply": "三根草已交给你。", "evidence": "交付回执累计三根草。"}
        with patch("mcp_agent.companion_agent.ThreadPoolExecutor", ImmediateWorker):
            agent = CompanionAgent(peer, planner, execute=True)
        try:
            for _ in range(15): await agent.cycle()
            ops = [a["op"] for name, a in peer.calls if name == "buddy_action"]
            self.assertEqual(ops, ["give_owner"]*3)
            self.assertEqual(len(contexts), 2)
            self.assertEqual(contexts[-1]["current_task_board"]["tasks"][0]["progress"]["current"], 3)
        finally: await agent.close()

    async def test_waiting_for_player_items_does_not_poll_the_model_or_issue_take(self):
        peer = TransferPeer();observed = peer.state["companion"];observed["buddy"].update(inventory={}, owner_distance=2);peer.exchange()
        calls = []
        def planner(context, *_):
            calls.append(context)
            return board("receive_item") if len(calls) == 1 else {"kind": "agent_reply", "outcome": "completed", "reply": "收到草了。", "evidence": "背包三根草。"}
        with patch("mcp_agent.companion_agent.ThreadPoolExecutor", ImmediateWorker):
            agent = CompanionAgent(peer, planner, execute=True)
        try:
            for _ in range(8): await agent.cycle()
            self.assertEqual(len(calls), 1)
            self.assertFalse(any(name == "buddy_action" for name, _ in peer.calls))
            observed["buddy"]["inventory"] = {"cutgrass": 3};observed["version"] += 1;peer.exchange()
            for _ in range(5): await agent.cycle()
            self.assertEqual(len(calls), 2)
        finally: await agent.close()


class StoragePlannerTests(unittest.TestCase):
    def test_storage_tool_is_offered_and_uses_actual_snapshot_slots(self):
        captured = []
        def request(url, key, body, validator, *, functions, **kwargs):
            captured.append(body)
            self.assertEqual(functions["query_storage"](container_guid=300001)["containers"][0]["items"][0]["count"], 7)
            self.assertNotIn("take_owner", body["messages"][0]["content"])
            return validator(board("withdraw"), None)
        with patch("mcp_agent.companion_planner.request_json_plan", request):
            request_plan("https://offline.invalid", "fake", "fake", {"observation": observation()}, [], threading.Event())
        self.assertIn("query_storage", {t["function"]["name"] for t in captured[0]["tools"]})


if __name__ == "__main__": unittest.main()
