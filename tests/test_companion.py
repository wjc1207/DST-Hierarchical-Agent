import asyncio
import copy
import json
import threading
import unittest
import urllib.request
from concurrent.futures import Future
from types import SimpleNamespace
from unittest.mock import patch

from mcp_server.companion_bridge import CompanionBridge
from mcp_server.companion_server import create_server
from mcp_server.game_bridge import BridgeError, serve_bridge
from mcp_agent.companion_planner import validate_plan, next_action, request_plan, collection_prefab
from mcp_agent.companion_agent import CompanionAgent

TOKEN = "companion-offline-test-" * 3


def state(enabled=True):
    return {"ready": True, "snapshot_age_ms": 0,
            "control": {"enabled": enabled, "revision": 1, "mode": "instruction", "instruction": "收集三根草然后回来",
                        "messages": [{"id": 1, "text": "收集三根草然后回来"}]},
            "companion": {"bound": True, "token": "buddy:1", "revision": 1, "mode": "stay", "version": 1,
                "capabilities": ["collect", "move", "follow", "stay"],
                "owner": {"guid": 10, "position": {"x": 0, "z": 0}},
                "buddy": {"guid": 20, "prefab": "wilson", "dead": False, "inventory": {}, "position": {"x": 5, "z": 0}, "owner_distance": 5},
                "nearby": [{"guid": 99, "prefab": "grass", "product": "cutgrass", "kind": "pick", "distance": 2, "x": 6, "z": 0}]}}


def plan():
    return validate_plan({"done": False, "reply": "我去收集三根草，再回到你身边。", "tasks": [
        {"id": "grass", "type": "collect", "prefab": "cutgrass", "count": 3}, {"id": "back", "type": "return_owner"}]})


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.time = 0
        self.bridge = CompanionBridge(TOKEN, clock=lambda: self.time)
        self.state = state()
        self.seq = 0
        self.exchange()

    def exchange(self, results=None, snapshot=None):
        self.seq += 1
        return self.bridge.exchange({"token": TOKEN, "session_id": "local", "sequence": self.seq,
                                     "state": snapshot or self.state, "results": results or []})

    def test_queue_repeated_delivery_and_server_confirmed_completion(self):
        obs = self.bridge.observe()
        action = self.bridge.action("collect", obs["observation_id"], {"target_guid": 99})
        self.assertEqual(action["status"], "queued")
        self.assertEqual(self.exchange()["command"]["id"], action["id"])
        self.assertEqual(self.exchange()["command"]["id"], action["id"])
        result = {"id": action["id"], "revision": 1, "status": "running"}
        self.assertIsNone(self.exchange([result])["command"])
        self.assertEqual(self.bridge.result(action["id"])["status"], "running")
        result["status"] = "completed"
        self.exchange([result])
        self.assertEqual(self.bridge.result(action["id"])["status"], "completed")

    def test_intent_change_rejects_old_observation_and_cancels_old_work(self):
        obs = self.bridge.observe()["observation_id"]
        action = self.bridge.action("move", obs, {"x": 5, "z": 0})
        self.state["control"].update(revision=2)
        self.state["control"]["messages"].append({"id": 2, "text": "先回来"})
        self.state["companion"]["revision"] = 2
        self.exchange()
        self.assertEqual(self.bridge.result(action["id"])["status"], "interrupted")
        with self.assertRaises(BridgeError):
            self.bridge.action("collect", obs, {"target_guid": 99})

    def test_bind_completion_survives_expected_token_change(self):
        for op in ("bind", "summon"):
            with self.subTest(op=op):
                self.state.update(ready=False, companion=None)
                self.exchange()
                action = self.bridge.action(op, self.bridge.observe()["observation_id"])
                self.assertEqual(self.exchange()["command"]["op"], op)
                self.state = state()
                self.exchange([{"id": action["id"], "status": "completed", "revision": 1}])
                self.assertEqual(self.bridge.result(action["id"])["status"], "completed")

    def test_stop_preempts_busy_executor_and_unoffered_targets_are_rejected(self):
        obs = self.bridge.observe()["observation_id"]
        with self.assertRaises(BridgeError):
            self.bridge.action("collect", obs, {"target_guid": 1000})
        first = self.bridge.action("collect", obs, {"target_guid": 99})
        stop = self.bridge.action("stop", obs)
        self.assertEqual(self.bridge.result(first["id"])["status"], "interrupted")
        self.assertEqual(self.exchange()["command"]["id"], stop["id"])

    def test_stale_snapshots_and_local_client_collisions_cannot_act(self):
        self.state["snapshot_age_ms"] = 2100
        self.exchange()
        with self.assertRaises(BridgeError):
            self.bridge.action("follow", self.bridge.observe()["observation_id"])
        with self.assertRaises(BridgeError):
            self.bridge.exchange({"token": TOKEN, "session_id": "other", "sequence": 1, "state": state()})
        self.time = 3.1
        with self.assertRaises(BridgeError):
            self.bridge.observe()

    def test_reply_is_idempotent_and_finish_cancels_pending_commands(self):
        action = self.bridge.action("collect", self.bridge.observe()["observation_id"], {"target_guid": 99})
        self.bridge.reply("完成了 🌲", 1, "same", True)
        self.bridge.reply("完成了 🌲", 1, "same", True)
        result = self.exchange()
        self.assertEqual(len(result["feedback"]), 1)
        self.assertIsNone(result["command"])
        self.assertEqual(self.bridge.result(action["id"])["status"], "interrupted")

    def test_invalid_envelope_does_not_change_active_intent(self):
        seq, saved = self.bridge.sequence, copy.deepcopy(self.bridge.state)
        with self.assertRaises(BridgeError):
            self.bridge.exchange({"token": TOKEN, "session_id": "local", "sequence": seq+1,
                                  "state": state(), "results": {"invalid": True}})
        self.assertEqual(self.bridge.state, saved)
        self.assertEqual(self.bridge.sequence, seq)

    def test_real_http_round_trip_preserves_utf8_feedback_and_authentication(self):
        server = serve_bridge(self.bridge, 0)
        try:
            self.bridge.reply("I'm collecting 草 🌲", 1, "unicode")
            data = {"token": TOKEN, "session_id": "local", "sequence": self.seq+1, "state": self.state}
            request = urllib.request.Request(f"http://127.0.0.1:{server.server_port}/v1/exchange",
                                             data=json.dumps(data, ensure_ascii=False).encode("utf8"))
            with urllib.request.urlopen(request, timeout=2) as response:
                raw = response.read()
            self.assertIn("草 🌲".encode("utf8"), raw)
            self.assertEqual(json.loads(raw)["feedback"][0]["text"], "I'm collecting 草 🌲")
        finally:
            server.shutdown();server.server_close()


class PlannerTests(unittest.TestCase):
    def test_native_collection_uses_latest_inventory_and_only_offered_sources(self):
        board = plan();snapshot = state()["companion"];done = set()
        task, op, args = next_action(board, snapshot, done)
        self.assertEqual((task["id"], op, args), ("grass", "collect", {"target_guid": 99}))
        snapshot["buddy"]["inventory"]["cutgrass"] = 3
        task, op, _ = next_action(board, snapshot, done)
        self.assertEqual((task["id"], op), ("back", "follow"))
        snapshot["mode"] = "follow"
        self.assertEqual(next_action(board, snapshot, done)[1], "waiting")
        snapshot["buddy"]["owner_distance"] = 3
        self.assertEqual(next_action(board, snapshot, done)[1], "completed")

    def test_unsupported_plans_and_unobserved_completion_are_not_fabricated(self):
        with self.assertRaises(ValueError):
            validate_plan({"done": False, "reply": "造箱子", "tasks": [{"id": "x", "type": "craft"}]})
        with self.assertRaises(ValueError):
            validate_plan({"done": True, "reply": "完成了", "evidence": ""})
        self.assertEqual(validate_plan({"done": False, "blocked": True, "reply": "暂不支持建造。"})["outcome"], "blocked")
        snapshot = state()["companion"];snapshot["nearby"] = []
        self.assertEqual(next_action(plan(), snapshot, set())[1], "blocked")

    def test_cancelled_plans_never_call_model(self):
        cancel = threading.Event();cancel.set()
        with patch("urllib.request.urlopen") as call:
            with self.assertRaisesRegex(RuntimeError, "superseded"):
                request_plan("https://offline.invalid", "fake", "fake", {}, [], cancel)
        call.assert_not_called()

    def test_entity_names_resolve_to_inventory_products_from_live_observations(self):
        snapshot = state()["companion"]
        self.assertEqual(collection_prefab("grass", snapshot), "cutgrass")
        snapshot["nearby"] = [{"guid": 100, "prefab": "mod_plant", "product": "mod_item", "distance": 2}]
        self.assertEqual(collection_prefab("mod_plant", snapshot), "mod_item")
        snapshot["nearby"] = [{"guid": 101, "prefab": "grass", "product": "grass", "distance": 2}]
        self.assertEqual(collection_prefab("grass", snapshot), "grass")


class ImmediateWorker:
    def __init__(self, **_): pass
    def submit(self, function, *args):
        future = Future()
        try: future.set_result(function(*args))
        except Exception as exc: future.set_exception(exc)
        return future
    def shutdown(self, **_): pass


class FakePeer:
    def __init__(self):
        self.state = state();self.seq = 0;self.bridge = CompanionBridge(TOKEN);self.calls = []
        self.exchange()
    def exchange(self, results=None):
        self.seq += 1
        return self.bridge.exchange({"token": TOKEN, "session_id": "local", "sequence": self.seq,
                                     "state": self.state, "results": results or []})
    async def call_tool(self, name, args):
        self.calls.append((name, args))
        try:
            if name == "buddy_observe":
                self.exchange();value = self.bridge.observe()
            elif name == "buddy_action":
                value = self.bridge.action(**args)
                if args["op"] == "collect":
                    inv = self.state["companion"]["buddy"]["inventory"]
                    inv["cutgrass"] = inv.get("cutgrass", 0)+1
                if args["op"] == "follow":
                    self.state["companion"].update(mode="follow")
                    self.state["companion"]["buddy"]["owner_distance"] = 2
                self.exchange([{"id": value["id"], "revision": value["revision"], "status": "completed"}])
            elif name == "buddy_reply":
                value = self.bridge.reply(**args)
            else:
                value = self.bridge.result(**args)
            return SimpleNamespace(is_error=False, structured_content=value)
        except BridgeError as exc:
            return SimpleNamespace(is_error=True, content=[SimpleNamespace(text=str(exc))])


class AgentTests(unittest.IsolatedAsyncioTestCase):
    async def test_result_arriving_after_observe_does_not_reuse_a_consumed_collection_target(self):
        peer=FakePeer();original=peer.call_tool;target=[None]
        async def call(name,args):
            if name=="buddy_result" and target[0] is not None:
                peer.state["companion"]["buddy"]["inventory"]["cutgrass"]=1
                peer.state["companion"]["nearby"]=[];peer.exchange();target[0]=None
            result=await original(name,args)
            if name=="buddy_action" and args["op"]=="collect":
                peer.state["companion"]["buddy"]["inventory"]={};peer.exchange();target[0]=args["args"]["target_guid"]
            return result
        peer.call_tool=call;contexts=[]
        def planner(context,*_):
            contexts.append(context)
            if len(contexts)==1:
                return validate_plan({"goal":"Collect one grass","tasks":[{"id":"grass","title":"Grass",
                    "target":{"type":"inventory","prefab":"cutgrass","count":1}}]})
            return validate_plan({"kind":"agent_reply","outcome":"completed","reply":"完成。","evidence":"背包已有一根草。"})
        with patch("mcp_agent.companion_agent.ThreadPoolExecutor",ImmediateWorker):
            agent=CompanionAgent(peer,planner,execute=True)
        try:
            for _ in range(9):await agent.cycle()
            self.assertEqual(len([args for name,args in peer.calls if name=="buddy_action" and args["op"]=="collect"]),1)
            self.assertEqual(contexts[-1]["observation"]["buddy"]["inventory"],{"cutgrass":1})
            self.assertEqual(agent.failures,0)
        finally:await agent.close()

    async def test_long_return_keeps_following_while_moving_and_only_replans_when_stalled(self):
        import time
        peer = FakePeer();observed = peer.state["companion"]
        observed["mode"] = "follow"
        observed["buddy"].update(owner_distance=1000, position={"x": 1000, "z": 0})
        contexts = []
        def planner(context, *_):
            contexts.append(context)
            return validate_plan({"done": False, "reply": "我正在回来。", "tasks": [{"id": "back", "type": "return_owner"}]})
        with patch("mcp_agent.companion_agent.ThreadPoolExecutor", ImmediateWorker):
            agent = CompanionAgent(peer, planner, execute=True)
        now = time.monotonic()
        try:
            with patch("time.monotonic", return_value=now):
                await agent.cycle();await agent.cycle()
            observed["buddy"].update(owner_distance=875, position={"x": 875, "z": 0})
            with patch("time.monotonic", return_value=now+25):
                await agent.cycle()
            self.assertEqual(agent.failures, 0)
            self.assertEqual(len(contexts), 1)
            with patch("time.monotonic", return_value=now+46):
                await agent.cycle()
            self.assertEqual(agent.failures, 1)
            self.assertEqual(contexts[-1]["replan_reason"], "return_owner_stalled")
        finally: await agent.close()

    async def test_pure_conversation_without_work_returns_to_follow_and_does_not_replan(self):
        peer = FakePeer();calls = []
        def planner(*_):
            calls.append(1);return validate_plan({"kind": "agent_reply", "outcome": "reply", "reply": "你好。"})
        with patch("mcp_agent.companion_agent.ThreadPoolExecutor", ImmediateWorker):
            agent = CompanionAgent(peer, planner, execute=True)
        try:
            for _ in range(8): await agent.cycle()
            self.assertEqual(len(calls), 1)
            self.assertTrue(any(row["finish"] for row in peer.bridge.feedback.values()))
        finally: await agent.close()

    async def test_pure_conversation_resumes_existing_tasks_after_a_message(self):
        peer = FakePeer();calls = []
        def planner(*_):
            calls.append(1)
            return plan() if len(calls) == 1 else validate_plan({"kind": "agent_reply", "outcome": "reply", "reply": "好呀。"})
        with patch("mcp_agent.companion_agent.ThreadPoolExecutor", ImmediateWorker):
            agent = CompanionAgent(peer, planner, execute=True)
        try:
            await agent.cycle();await agent.cycle()
            peer.state["control"]["revision"] = 2;peer.state["companion"]["revision"] = 2
            peer.state["control"]["messages"].append({"id": 2, "text": "加油"})
            await agent.cycle();await agent.cycle();await agent.cycle()
            self.assertEqual(peer.state["companion"]["buddy"]["inventory"]["cutgrass"], 2)
            self.assertFalse(any(row["finish"] for row in peer.bridge.feedback.values()))
        finally: await agent.close()

    async def test_common_scheduled_board_is_reviewed_once_then_wakes_without_polling_the_model(self):
        peer = FakePeer();peer.state["companion"]["world"] = {"day": 1, "phase": "day", "time": .2}
        contexts = []
        raw = {"goal": "夜间采草", "reply": "等到夜间采草。", "tasks": [
            {"id": "grass", "title": "Gather grass", "target": {"type": "inventory", "prefab": "cutgrass", "count": 3},
             "start_at": {"day": 1, "phase": "night"}}]}
        def planner(context, *_):
            contexts.append(context)
            return validate_plan(raw)
        with patch("mcp_agent.companion_agent.ThreadPoolExecutor", ImmediateWorker):
            agent = CompanionAgent(peer, planner, execute=True)
        try:
            for _ in range(12): await agent.cycle()
            self.assertEqual(len(contexts), 2)
            self.assertEqual(contexts[-1]["replan_reason"], "only_scheduled_tasks")
            self.assertFalse(any(name == "buddy_action" for name, _ in peer.calls))
            peer.state["companion"]["world"]["phase"] = "night"
            await agent.cycle()
            self.assertEqual(peer.state["companion"]["buddy"]["inventory"]["cutgrass"], 1)
            self.assertEqual(len(contexts), 2)
        finally: await agent.close()

    async def test_shared_board_progress_survives_a_message_and_obsolete_tasks_can_be_cancelled(self):
        peer = FakePeer();contexts = []
        def planner(context, *_):
            contexts.append(context)
            if len(contexts) == 1: return plan()
            return validate_plan({"goal": "只要两根草", "reply": "改为两根。", "cancel_task_ids": ["back"], "tasks": [
                {"id": "grass", "title": "Gather two", "target": {"type": "inventory", "prefab": "cutgrass", "count": 2}}]})
        with patch("mcp_agent.companion_agent.ThreadPoolExecutor", ImmediateWorker):
            agent = CompanionAgent(peer, planner, execute=True)
        try:
            await agent.cycle();await agent.cycle()
            self.assertEqual(peer.state["companion"]["buddy"]["inventory"]["cutgrass"], 1)
            peer.state["control"]["revision"] = 2;peer.state["companion"]["revision"] = 2
            peer.state["control"]["messages"].append({"id": 2, "text": "改为两根，不用回来"})
            await agent.cycle();await agent.cycle()
            self.assertEqual(peer.state["companion"]["buddy"]["inventory"]["cutgrass"], 2)
            self.assertEqual([t["id"] for t in agent.plan["tasks"]], ["grass"])
            self.assertEqual(contexts[1]["current_task_board"]["tasks"][1]["progress"]["current"], 1)
        finally: await agent.close()

    async def test_actor_change_clears_old_board_history_and_pending_completion(self):
        peer = FakePeer();contexts = []
        def planner(context, history, *_):
            contexts.append((context, history));return plan()
        with patch("mcp_agent.companion_agent.ThreadPoolExecutor", ImmediateWorker):
            agent = CompanionAgent(peer, planner, execute=True)
        try:
            await agent.cycle();await agent.cycle()
            self.assertTrue(agent.history)
            peer.state["companion"]["token"] = "replacement:1"
            peer.state["companion"]["buddy"]["guid"] = 21
            await agent.cycle()
            self.assertIsNone(contexts[-1][0]["current_task_board"])
            self.assertEqual(contexts[-1][1], [])
        finally: await agent.close()

    async def test_missing_resources_are_reported_without_three_replans_or_fake_action_failures(self):
        peer = FakePeer();peer.state["companion"]["nearby"] = []
        calls = []
        def planner(*_): calls.append(1);return plan()
        with patch("mcp_agent.companion_agent.ThreadPoolExecutor", ImmediateWorker):
            agent = CompanionAgent(peer, planner, execute=True)
        try:
            for _ in range(8): await agent.cycle()
            self.assertEqual(len(calls), 1)
            self.assertEqual(agent.failures, 0)
            self.assertFalse(any(name == "buddy_action" for name, _ in peer.calls))
            messages = [message["text"] for message in peer.bridge.feedback.values() if message["finish"]]
            self.assertEqual(len(messages), 1)
            self.assertIn("cutgrass", messages[0]);self.assertIn("15", messages[0])
        finally: await agent.close()

    async def test_ds_entity_name_grass_still_collects_actual_cutgrass(self):
        peer = FakePeer();contexts = []
        def planner(context, *_):
            contexts.append(context)
            if len(contexts) == 1:
                board = plan();board["tasks"][0]["target"]["prefab"] = "grass";return board
            return validate_plan({"done": True, "reply": "完成。", "evidence": "背包三根草，已到主人旁。"})
        with patch("mcp_agent.companion_agent.ThreadPoolExecutor", ImmediateWorker):
            agent = CompanionAgent(peer, planner, execute=True)
        records = []
        agent.events = SimpleNamespace(write=records.append)
        try:
            for _ in range(15): await agent.cycle()
            self.assertEqual(peer.state["companion"]["buddy"]["inventory"]["cutgrass"], 3)
            self.assertEqual(len(contexts), 2)
            self.assertTrue(any(row["event"] == "prefab_normalized" for row in records))
            self.assertEqual(sum(row["event"] == "action_result" for row in records), 4)
            self.assertFalse(any("token" in row or "api_key" in row for row in records))
        finally: await agent.close()

    async def test_collect_return_verify_and_finish_without_idle_model_calls(self):
        peer = FakePeer();contexts = []
        def planner(context, history, cancel):
            contexts.append(context)
            if len(contexts) == 1: return plan()
            self.assertEqual(context["observation"]["buddy"]["inventory"]["cutgrass"], 3)
            return validate_plan({"done": True, "reply": "三根草已收集，我回来了。", "evidence": "背包有三根草，离主人两单位。"})
        with patch("mcp_agent.companion_agent.ThreadPoolExecutor", ImmediateWorker):
            agent = CompanionAgent(peer, planner, execute=True)
        try:
            for _ in range(15): await agent.cycle()
            self.assertEqual(len(contexts), 2)
            actions = [args["op"] for name, args in peer.calls if name == "buddy_action"]
            self.assertEqual(actions, ["collect"]*3 + ["follow"])
            self.assertTrue(any(row["finish"] for row in peer.bridge.feedback.values()))
        finally:
            await agent.close()

    async def test_idle_clients_and_dry_run_do_not_call_models(self):
        peer = FakePeer();peer.state["control"]["enabled"] = False
        def unexpected(*_): raise AssertionError("Model called while idle")
        agent = CompanionAgent(peer, unexpected, execute=True)
        try:
            for _ in range(3): await agent.cycle()
            self.assertIsNone(agent.future)
        finally: await agent.close()

    async def test_new_messages_discard_running_old_plans(self):
        class PendingWorker(ImmediateWorker):
            def submit(self, *_): return Future()
        peer = FakePeer()
        with patch("mcp_agent.companion_agent.ThreadPoolExecutor", PendingWorker):
            agent = CompanionAgent(peer, None, execute=True)
        try:
            await agent.cycle();old, cancel = agent.future, agent.cancel
            old.set_running_or_notify_cancel()
            peer.state["control"]["revision"] = 2
            peer.state["control"]["messages"].append({"id": 2, "text": "先回来"})
            peer.state["companion"]["revision"] = 2
            await agent.cycle()
            self.assertTrue(cancel.is_set());self.assertIsNot(agent.future, old)
            old.set_result(plan())
            self.assertFalse(any(name == "buddy_action" for name, _ in peer.calls))
        finally: await agent.close()

    async def test_restarted_host_waits_for_existing_native_work(self):
        peer = FakePeer()
        peer.state["companion"]["action"] = {"id": "prior-process", "status": "running", "op": "collect"}
        def unexpected(*_): raise AssertionError("Planned over prior native work")
        agent = CompanionAgent(peer, unexpected, execute=True)
        try:
            await agent.cycle()
            self.assertIsNone(agent.future)
            self.assertFalse(any(name == "buddy_action" for name, _ in peer.calls))
        finally: await agent.close()

    async def test_real_mcp_tool_discovery_and_state(self):
        from mcp import Client
        bridge = CompanionBridge(TOKEN, clock=lambda: 0)
        bridge.exchange({"token": TOKEN, "session_id": "sdk", "sequence": 1, "state": state()})
        async with Client(create_server(bridge), read_timeout_seconds=3) as client:
            tools = await client.list_tools()
            self.assertEqual({t.name for t in tools.tools}, {"buddy_observe", "buddy_action", "buddy_result", "buddy_reply", "buddy_recipes", "buddy_storage", "buddy_places"})
            observed = await client.call_tool("buddy_observe", {})
            self.assertEqual(observed.structured_content["state"]["companion"]["buddy"]["guid"], 20)
