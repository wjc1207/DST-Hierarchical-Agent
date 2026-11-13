"""Player interruptions, bounded planning and instruction completion, without APIs."""
import copy
import json
import threading
import unittest
from unittest.mock import patch

from common import autonomy
from mcp_agent.daily_planner import DailyPlanner, request_plan, validate_agent_response
from mcp_agent.mcp_agent import MCPAgent
from mcp_server.game_bridge import GameBridge, BridgeError
from test_autonomy import controlled
from test_mcp_agent import FakeClient, answer
from test_priority_board import PendingWorker, board, task


def dialogue(mode="instruction", text="收集五个木头"):
    snapshot = controlled(goal=None)
    snapshot["autonomy"].update(mode=mode, instruction=text if mode == "instruction" else None,
                               messages=[{"id": 1, "text": text}])
    return snapshot


def reply(outcome="completed"):
    return {"kind": "agent_reply", "outcome": outcome, "reply": "已按要求完成。",
            "evidence": "最新背包里有五个木头。"}


class DialogueTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch("builtins.print"))
        self.enterContext(patch("mcp_agent.daily_planner.ThreadPoolExecutor", PendingWorker))
        self.planner = DailyPlanner("offline", board_log_dir=None)
        self.addCleanup(self.planner.close)

    def adopt(self, snapshot):
        self.planner.update(snapshot)
        self.planner.future.set_result((board(task("logs", "log", count=5)), 1, None))
        return self.planner.update(snapshot)

    def test_interrupt_holds_old_board_and_preserves_records_and_conversation(self):
        snapshot = dialogue()
        self.adopt(snapshot)
        records = self.planner.goals
        conversation = list(self.planner.conversation)
        snapshot = copy.deepcopy(snapshot)
        snapshot["autonomy"].update(revision=2)
        snapshot["autonomy"]["messages"].append({"id": 2, "text": "先回家"})
        self.assertIsNone(self.planner.update(snapshot))
        self.assertTrue(self.planner.hold_execution)
        self.assertIs(self.planner.goals, records)
        self.assertEqual(list(self.planner.conversation), conversation)
        context = self.planner.executor.calls[-1][4]
        self.assertEqual(context["replan_reason"], "player_message")
        self.assertEqual(context["current_task_board"]["tasks"][0]["id"], "logs")
        self.assertEqual(context["interaction"]["instruction"], "收集五个木头")
        self.assertEqual(context["interaction"]["messages"][-1]["text"], "先回家")

    def test_new_message_cancels_old_request_token_and_discards_its_reply(self):
        snapshot = dialogue()
        self.planner.update(snapshot)
        old, token = self.planner.future, self.planner.cancel_event
        old.set_running_or_notify_cancel()
        snapshot["autonomy"].update(revision=2)
        snapshot["autonomy"]["messages"].append({"id": 2, "text": "改成收集草"})
        self.planner.update(snapshot)
        self.assertTrue(token.is_set())
        old.set_result((reply(), 1, None))
        self.assertIsNone(self.planner.update(snapshot))
        self.assertFalse(self.planner.instruction_finished)
        self.assertFalse(self.planner.feedback)

    def test_instruction_completion_is_reviewed_then_stays_idle_across_quarters(self):
        snapshot = dialogue()
        self.adopt(snapshot)
        snapshot["player"]["inventory"]["items"] = [{"prefab": "log", "count": 5}]
        self.assertIsNone(self.planner.update(snapshot))
        self.assertTrue(self.planner.hold_execution)
        self.assertEqual(self.planner.executor.calls[-1][4]["replan_reason"], "all_tasks_completed")
        self.planner.future.set_result((reply(), 1, None))
        self.assertIsNone(self.planner.update(snapshot))
        self.assertTrue(self.planner.feedback[-1]["finish"])
        calls = len(self.planner.executor.calls)
        snapshot["world"]["time"] = .8
        self.planner.update(snapshot)
        self.assertEqual(len(self.planner.executor.calls), calls)

    def test_autonomous_milestone_keeps_planning(self):
        snapshot = dialogue("autonomous")
        self.adopt(snapshot)
        snapshot["player"]["inventory"]["items"] = [{"prefab": "log", "count": 5}]
        self.planner.update(snapshot)
        self.assertIsNotNone(self.planner.future)
        self.assertFalse(self.planner.hold_execution)
        self.planner.future.set_result((board(task("grass", "cutgrass")), 1, None))
        self.assertIsNotNone(self.planner.update(snapshot))

    def test_already_completed_milestone_response_gets_bounded_final_review(self):
        snapshot = dialogue()
        self.adopt(snapshot)
        snapshot["player"]["inventory"]["items"] = [{"prefab": "log", "count": 5}]
        self.planner.update(snapshot)
        self.planner.future.set_result((board(task("logs", "log", count=5)), 1, None))
        self.planner.update(snapshot)
        calls = len(self.planner.executor.calls)
        with patch("mcp_agent.daily_planner.time.monotonic", return_value=self.planner.goal_retry_at + 1):
            self.assertIsNone(self.planner.update(snapshot))
        self.assertEqual(len(self.planner.executor.calls), calls + 1)
        self.assertEqual(self.planner.executor.calls[-1][4]["replan_reason"], "instruction_completion_check")

    def test_pure_chat_resumes_the_same_board(self):
        snapshot = dialogue()
        self.adopt(snapshot)
        snapshot["autonomy"].update(revision=2)
        snapshot["autonomy"]["messages"].append({"id": 2, "text": "你在干什么"})
        self.planner.update(snapshot)
        self.planner.future.set_result((reply("reply"), 1, None))
        result = self.planner.update(snapshot)
        self.assertEqual(result["tasks"][0]["id"], "logs")
        self.assertFalse(self.planner.hold_execution)
        self.assertFalse(self.planner.feedback[-1]["finish"])

    def test_first_reply_without_a_board_enters_instruction_waiting(self):
        for outcome in ("reply", "blocked", "completed"):
            with self.subTest(outcome=outcome):
                snapshot = dialogue()
                # Separate player turns invalidate the previous finished reply.
                snapshot["autonomy"]["revision"] += len(self.planner.executor.calls)
                self.planner.update(snapshot)
                self.planner.future.set_result((reply(outcome), 1, None))
                self.assertIsNone(self.planner.update(snapshot))
                self.assertFalse(self.planner.legacy_mode)
                self.assertTrue(self.planner.instruction_finished)
                self.assertTrue(self.planner.feedback[-1]["finish"])

    def test_model_reply_json_is_accepted_without_fabricating_a_board(self):
        class Response:
            def __enter__(self): return self
            def __exit__(self, *_): return False
            def read(self):
                return json.dumps({"choices": [{"finish_reason": "stop", "message": {
                    "content": json.dumps(reply("reply"))}}]}).encode()
        with patch("mcp_agent.daily_planner.urllib.request.urlopen", return_value=Response()):
            result = request_plan("https://example.invalid", "offline", "model", 1,
                                  {"interaction": dialogue()["autonomy"]})
        self.assertEqual(result["kind"], "agent_reply")
        self.assertNotIn("tasks", result)

    def test_planning_failure_after_interrupt_does_not_resume_old_work(self):
        snapshot = dialogue()
        self.adopt(snapshot)
        snapshot["autonomy"].update(revision=2)
        snapshot["autonomy"]["messages"].append({"id": 2, "text": "先回来"})
        self.planner.update(snapshot)
        self.planner.future.set_result((None, 1, RuntimeError("offline")))
        self.assertIsNone(self.planner.update(snapshot))
        self.assertTrue(self.planner.hold_execution)

    def test_completion_rechecks_current_progress_and_requires_evidence(self):
        snapshot = dialogue()
        self.adopt(snapshot)
        self.planner.request_review(snapshot, "test_review", immediate=True)
        self.planner.future.set_result((reply(), 1, None))
        self.planner.update(snapshot)
        self.assertFalse(self.planner.instruction_finished)
        context = {"interaction": snapshot["autonomy"]}
        with self.assertRaises(ValueError):
            validate_agent_response({**reply(), "evidence": ""}, 1, context)
        with self.assertRaises(ValueError):
            validate_agent_response(reply(), 1, {"interaction": {"mode": "autonomous"}})

    def test_schema_rejects_unordered_messages_and_invalid_modes(self):
        snapshot = dialogue()
        for change in ({"mode": []}, {"messages": [{"id": 2, "text": "late"}]},
                       {"messages": [{"id": 1, "text": "bad\ntext"}]},
                       {"messages": [{"id": 1, "text": "one"}] * 2}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                autonomy.settings({"autonomy": {**snapshot["autonomy"], **change}})

    def test_cancelled_request_never_starts_another_http_call(self):
        token = threading.Event()
        token.set()
        with patch("mcp_agent.daily_planner.urllib.request.urlopen") as send:
            with self.assertRaisesRegex(RuntimeError, "superseded"):
                request_plan("https://example.invalid", "offline", "model", 1, {}, cancel_event=token)
        send.assert_not_called()

    def test_feedback_is_idempotent_and_scoped_to_current_intent(self):
        bridge = GameBridge("test-token-" * 4)
        snapshot = dialogue()
        def exchange(seq):
            return bridge.exchange({"token": bridge.token, "session_id": "world", "sequence": seq, "state": snapshot})
        exchange(1)
        for _ in range(2):
            bridge.send_agent_feedback("做好了", 1, "reply-1", True)
        self.assertEqual(len(exchange(2)["agent_feedback"]), 1)
        with self.assertRaises(BridgeError):
            bridge.send_agent_feedback("different", 1, "reply-1", True)
        snapshot["autonomy"].update(revision=2)
        self.assertEqual(exchange(3)["agent_feedback"], [])
        with self.assertRaises(BridgeError):
            bridge.send_agent_feedback("旧回复", 1, "reply-2", True)


class HostDialogueTests(unittest.IsolatedAsyncioTestCase):
    async def test_first_completed_reply_reaches_lua_bridge_and_never_starts_work(self):
        with patch("mcp_agent.daily_planner.ThreadPoolExecutor", PendingWorker):
            planner = DailyPlanner("offline", board_log_dir=None)
        client = FakeClient()
        client.state["autonomy"] = dialogue()["autonomy"]
        agent = MCPAgent(client, answer, planner=planner, execute=True, poll=.001)
        try:
            await agent.cycle()
            planner.future.set_result((reply(), 1, None))
            await agent.cycle()
            messages = list(client.bridge.agent_feedback.values())
            self.assertEqual(len(messages), 1)
            self.assertTrue(messages[0]["finish"])
            await agent.cycle()
            self.assertFalse(any(name == "start_action" for name, _ in client.calls))
            self.assertEqual(sum(name == "send_agent_feedback" for name, _ in client.calls), 1)
        finally:
            await agent.shutdown()
            planner.close()

    async def test_planning_wait_does_not_call_jev_or_start_optional_work(self):
        with patch("mcp_agent.daily_planner.ThreadPoolExecutor", PendingWorker):
            planner = DailyPlanner("offline", board_log_dir=None)
        client = FakeClient()
        client.state["autonomy"] = dialogue()["autonomy"]
        def unexpected(*_):
            raise AssertionError("JEV optional work while dialogue replans")
        agent = MCPAgent(client, unexpected, planner=planner, execute=True, poll=.001)
        try:
            await agent.cycle()
            self.assertFalse(any(name == "start_action" for name, _ in client.calls))
        finally:
            await agent.shutdown()
            planner.close()

    async def test_player_message_cancels_an_inflight_action(self):
        client = FakeClient()
        client.auto_complete = False
        client.state["autonomy"] = dialogue("autonomous")["autonomy"]
        def interrupt(peer):
            if peer.bridge.active and peer.state["autonomy"]["revision"] == 1:
                peer.state["autonomy"]["revision"] = 2
                peer.state["autonomy"]["messages"].append({"id": 2, "text": "先回来"})
        client.hook = interrupt
        agent = MCPAgent(client, answer, execute=True, poll=.001)
        try:
            await agent.cycle()
            self.assertIsNone(agent.active_id)
            self.assertTrue(any(name == "cancel_action" for name, _ in client.calls))
        finally:
            await agent.shutdown()
