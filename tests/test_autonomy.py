"""Local intent must survive replans and invalidate stale model/action work."""
import copy
import unittest
from unittest.mock import patch

from common import autonomy
from mcp_agent.daily_planner import DailyPlanner
from mcp_agent.mcp_agent import MCPAgent
from mcp_server.game_bridge import GameBridge, BridgeError
from test_mcp_agent import FakeClient, FakePlanner, answer
from test_priority_board import PendingWorker, board, task, state


def controlled(enabled=True, revision=1, goal="Build a Crock Pot"):
    result = state()
    result["player"]["guid"] = 1
    result["bridge"] = {"session_id": "world", "available_actions": {"wait": {"kind": "wait"}}}
    result["autonomy"] = {"enabled": enabled, "revision": revision, "goal": goal}
    return result


class AutonomyTests(unittest.TestCase):
    def test_schema_rejects_malformed_settings_and_keeps_old_clients_compatible(self):
        self.assertTrue(autonomy.enabled({}))
        for value in ([], {"enabled": 1, "revision": 0}, {"enabled": True, "revision": True},
                      {"enabled": True, "revision": 1, "goal": " "},
                      {"enabled": True, "revision": 1, "goal": "bad\ntext"},
                      {"enabled": True, "revision": 1, "goal": "中" * 257}):
            with self.assertRaises(ValueError):
                autonomy.settings({"autonomy": value})
        self.assertEqual(autonomy.settings(controlled(goal="中" * 256))["goal"], "中" * 256)

    def test_bridge_rejects_disabled_and_obsolete_observations(self):
        bridge = GameBridge("local-autonomy-test-token" * 2)
        def exchange(snapshot, seq):
            return bridge.exchange({"token": bridge.token, "session_id": "world", "sequence": seq, "state": snapshot})
        snapshot = controlled(False)
        exchange(snapshot, 1)
        observation = bridge.get_available_actions()["observation_id"]
        with self.assertRaisesRegex(BridgeError, "disabled"):
            bridge.start_action("wait", observation)
        snapshot = controlled(True, 2)
        exchange(snapshot, 2)
        observation = bridge.get_available_actions()["observation_id"]
        snapshot["autonomy"].update(revision=3, goal="Store logs")
        exchange(snapshot, 3)
        with self.assertRaisesRegex(BridgeError, "changed"):
            bridge.start_action("wait", observation)
        observation = bridge.get_available_actions()["observation_id"]
        action = bridge.start_action("wait", observation)
        response = exchange(snapshot, 4)
        self.assertEqual(response["command"]["autonomy_revision"], 3)
        self.assertEqual(action["autonomy_revision"], 3)

    @patch("mcp_agent.daily_planner.ThreadPoolExecutor", PendingWorker)
    def test_goal_changes_drop_pending_old_replies_and_reach_every_new_request(self):
        planner = DailyPlanner(api_key="offline", board_log_dir=None)
        first = controlled()
        planner.update(first)
        old = planner.future
        old.set_running_or_notify_cancel()
        self.assertEqual(planner.executor.calls[0][4]["long_term_goal"], "Build a Crock Pot")
        second = controlled(True, 2, "Store five logs")
        planner.update(second)
        self.assertIsNot(planner.future, old)
        self.assertIsNone(planner.plan)
        old.set_result((board(task("obsolete", "cutgrass")), 1, None))
        planner.future.set_result((board(task("logs", "log", count=5)), 1, None))
        plan = planner.update(second)
        self.assertEqual(plan["tasks"][0]["id"], "logs")
        next_window = copy.deepcopy(second)
        next_window["world"]["time"] = .4
        planner.update(next_window)
        self.assertEqual(planner.executor.calls[-1][4]["long_term_goal"], "Store five logs")
        self.assertNotIn("obsolete", str(planner.plan))

    @patch("mcp_agent.daily_planner.ThreadPoolExecutor", PendingWorker)
    def test_pause_calls_no_model_and_resume_preserves_the_existing_task_records(self):
        planner = DailyPlanner(api_key="offline", board_log_dir=None)
        snapshot = controlled()
        planner.update(snapshot)
        planner.future.set_result((board(task("logs", "log", count=5)), 1, None))
        planner.update(snapshot)
        records = planner.goals
        calls = len(planner.executor.calls)
        snapshot["autonomy"].update(enabled=False, revision=2)
        self.assertIsNone(planner.update(snapshot))
        self.assertEqual(len(planner.executor.calls), calls)
        self.assertIs(planner.goals, records)
        snapshot["autonomy"].update(enabled=True, revision=3)
        planner.update(snapshot)
        self.assertIs(planner.goals, records)
        self.assertEqual(planner.executor.calls[-1][4]["replan_reason"], "autonomy_resumed")


class HostAutonomyTests(unittest.IsolatedAsyncioTestCase):
    async def test_turning_mode_off_cancels_a_running_action(self):
        client = FakeClient()
        client.auto_complete = False
        client.state["autonomy"] = {"enabled": True, "revision": 1}
        def disable_when_running(peer):
            if peer.bridge.active:
                peer.state["autonomy"] = {"enabled": False, "revision": 2}
        client.hook = disable_when_running
        agent = MCPAgent(client, answer, execute=True, poll=.001)
        try:
            await agent.cycle()
            self.assertIsNone(agent.active_id)
            self.assertTrue(any(name == "cancel_action" for name, _ in client.calls))
            starts = sum(name == "start_action" for name, _ in client.calls)
            await agent.cycle()
            self.assertEqual(sum(name == "start_action" for name, _ in client.calls), starts)
        finally:
            await agent.shutdown()

    async def test_disabled_mode_neither_calls_models_nor_sends_actions(self):
        client, planner = FakeClient(), FakePlanner()
        client.state["autonomy"] = {"enabled": False, "revision": 0}
        def unexpected(*_):
            raise AssertionError("Disabled mode called a model")
        agent = MCPAgent(client, unexpected, planner=planner, execute=True, poll=.001)
        try:
            await agent.cycle()
            self.assertFalse(any(name == "start_action" for name, _ in client.calls))
        finally:
            await agent.shutdown()

    async def test_goal_edit_during_jev_call_discards_its_old_answer(self):
        client = FakeClient()
        client.state["autonomy"] = {"enabled": True, "revision": 1, "goal": "first"}
        def decide(*args):
            client.state["autonomy"] = {"enabled": True, "revision": 2, "goal": "second"}
            return answer(*args)
        agent = MCPAgent(client, decide, execute=True, poll=.001)
        try:
            await agent.cycle()
            self.assertFalse(any(name == "start_action" for name, _ in client.calls))
        finally:
            await agent.shutdown()
