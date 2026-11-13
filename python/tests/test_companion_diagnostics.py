import json
import threading
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from test_companion import FakePeer, ImmediateWorker, state
from test_shared_planning import Response
from mcp_agent.companion_agent import CompanionAgent
from mcp_agent.companion_planner import CompanionPlanner, CompanionPlanningError, request_plan

SECRET="sk-secret-must-never-enter-diagnostics"


def invalid_response():
    return {"goal":"Invalid response", "tasks":[{"id":"bad","title":"Invalid", "target":{"type":SECRET}}]}


class PlanningDiagnosticTests(unittest.TestCase):
    def test_http_errors_preserve_the_status_in_a_safe_planning_exception(self):
        error=urllib.error.HTTPError("https://offline.invalid",401,"Unauthorized",{},None)
        with patch("mcp_agent.planning_core.urllib.request.urlopen",side_effect=error):
            with self.assertRaises(CompanionPlanningError) as caught:
                request_plan("https://offline.invalid",SECRET,"fake",{"observation":state()["companion"]},[],threading.Event())
        self.assertEqual(caught.exception.http_status, 401)
        self.assertIn("HTTP 401",caught.exception.safe_detail)
        self.assertNotIn(SECRET,caught.exception.safe_detail)

    def test_invalid_response_reports_the_actual_validation_stage_with_keys_redacted(self):
        diagnostics=[]
        with patch("mcp_agent.planning_core.urllib.request.urlopen",side_effect=[Response(invalid_response()),Response(invalid_response())]):
            with self.assertRaises(CompanionPlanningError) as caught:
                request_plan("https://offline.invalid",SECRET,"fake",{"observation":state()["companion"]},[],threading.Event(),
                             on_error=lambda stage,detail:diagnostics.append((stage,detail)))
        self.assertEqual([s for s,_ in diagnostics],["response_validation","response_validation"])
        self.assertIn("Unsupported companion",diagnostics[0][1])
        self.assertNotIn(SECRET,json.dumps(diagnostics)+caught.exception.safe_detail)
        self.assertIn("[REDACTED]",caught.exception.safe_detail)


class AgentDiagnosticTests(unittest.IsolatedAsyncioTestCase):
    async def test_balance_failure_preserves_a_scheduled_return_without_polling_deepseek(self):
        peer = FakePeer()
        observed = peer.state["companion"]
        observed["world"] = {"day": 1, "phase": "dusk", "seconds_until_next_phase": 30,
                             "phase_duration_seconds": {"day": 240, "dusk": 150, "night": 90}}
        observed["buddy"]["run_speed"] = 6
        observed["capabilities"].append("timed_return")
        planner = CompanionPlanner("https://offline.invalid", SECRET, "fake")
        raw = {"goal": "Return at night", "reply": "我入夜再回来。", "tasks": [
            {"id": "back", "title": "Return", "target": {"type": "return_owner"},
             "at": {"day": 1, "phase": "night"}}]}
        error = urllib.error.HTTPError("https://offline.invalid", 402, "Payment Required", {}, None)
        with patch("mcp_agent.companion_agent.ThreadPoolExecutor", ImmediateWorker), \
             patch("mcp_agent.planning_core.urllib.request.urlopen", side_effect=[Response(raw), error]) as request:
            agent = CompanionAgent(peer, planner, execute=True)
            try:
                for _ in range(10):
                    await agent.cycle()
                self.assertEqual(request.call_count, 2)
                self.assertTrue(any("余额不足" in item["text"] and not item["finish"]
                                    for item in peer.bridge.feedback.values()))
                self.assertFalse(any(name == "buddy_action" for name, _ in peer.calls))
                observed["world"].update(phase="night", seconds_until_next_phase=90)
                for _ in range(5):
                    await agent.cycle()
                self.assertEqual([args["op"] for name, args in peer.calls if name == "buddy_action"], ["follow"])
                self.assertEqual(request.call_count, 2)
            finally:
                await agent.close()

    async def test_failure_log_keeps_stage_and_safe_cause_instead_of_only_runtimeerror(self):
        peer=FakePeer();planner=CompanionPlanner("https://offline.invalid",SECRET,"fake")
        with tempfile.TemporaryDirectory() as folder:
            logfile=Path(folder)/"companion.jsonl"
            with patch("mcp_agent.companion_agent.ThreadPoolExecutor",ImmediateWorker), \
                 patch("mcp_agent.planning_core.urllib.request.urlopen",side_effect=[Response(invalid_response()),Response(invalid_response())]):
                agent=CompanionAgent(peer,planner,execute=True,log_path=logfile)
                try:
                    await agent.cycle();await agent.cycle()
                    raw=logfile.read_text(encoding="utf8");records=[json.loads(line) for line in raw.splitlines()]
                    details=[row for row in records if row["event"]=="planning_detail"]
                    self.assertEqual(len(details),2)
                    failure=next(row for row in records if row["event"]=="failure")
                    self.assertIn("invalid_json_or_plan",failure["reason"])
                    self.assertIn("Unsupported companion",failure["reason"])
                    self.assertNotIn(SECRET,raw)
                finally:await agent.close()


if __name__=="__main__":unittest.main()
