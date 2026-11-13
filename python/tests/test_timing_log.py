"""Timing records preserve outcomes and distinguish nested elapsed times."""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

from mcp_agent import jev_client as jev_agent
from common import timing_log as timing


class TimingTests(unittest.TestCase):
    def test_real_jev_client_records_packaging_headers_read_and_parse_without_request_content(self):
        from fixtures import state
        response = MagicMock()
        response.read.return_value = json.dumps({"answers": {"next_action": {
            "choice": "wait", "confidence": 1}}}).encode()
        with patch.object(jev_agent.urllib.request, "urlopen", return_value=response):
            with timing.CycleTrace(1, emit_console=False) as trace:
                result = jev_agent.call_jev("https://offline.invalid", "secret-key-not-in-timing",
                                           "fake", state([]), {"wait": "Wait"})
        self.assertEqual(result["answer"]["choice"], "wait")
        for name in ("api.encode_state", "api.encode_request", "api.response_headers",
                     "api.read_body", "api.parse_response"):
            self.assertIn(name, trace.record["totals"])
        self.assertNotIn("secret-key-not-in-timing", json.dumps(trace.record))


    def test_nested_times_separate_parent_total_from_self_time(self):
        with patch.object(timing.time, "perf_counter", side_effect=[0, 1, 2, 5, 7, 8]), patch("builtins.print"):
            with timing.CycleTrace(4) as trace:
                with timing.span("action.execute"):
                    with timing.span("mcp.observe"):
                        pass
        self.assertEqual(trace.record["total_ms"], 8000)
        self.assertEqual(trace.record["totals"]["action.execute"]["total_ms"], 6000)
        self.assertEqual(trace.record["totals"]["action.execute"]["self_ms"], 3000)
        parent, child = trace.record["spans"]
        self.assertEqual(child["parent_id"], parent["id"])
        self.assertEqual(child["duration_ms"], 3000)

    def test_repeated_measurements_are_aggregated_and_detail_is_bounded(self):
        with patch.object(timing, "MAX_SPANS", 2), patch("builtins.print"):
            with timing.CycleTrace(1) as trace:
                for _ in range(5):
                    timing.event("action.marker", action_id="fake")
                    with timing.span("mcp.observe", record=False):
                        pass
                    with timing.span("action.native_wait"):
                        pass
        self.assertEqual(trace.record["totals"]["mcp.observe"]["calls"], 5)
        self.assertEqual(trace.record["totals"]["action.native_wait"]["calls"], 5)
        self.assertEqual(len(trace.record["spans"]), 2)
        self.assertEqual(trace.record["dropped_spans"], 3)
        self.assertEqual(len(trace.record["events"]), 2)
        self.assertEqual(trace.record["dropped_events"], 3)
        self.assertEqual(trace.record["events"][0]["stage"], "action.marker")
        self.assertLessEqual(trace.record["events"][0]["at_ms"], trace.record["events"][1]["at_ms"])

    def test_timeout_is_logged_and_still_raised_then_context_is_reset(self):
        with tempfile.TemporaryDirectory() as directory, patch("builtins.print"):
            log = Path(directory) / "timing.jsonl"
            with self.assertRaisesRegex(RuntimeError, "fresh telemetry"):
                with timing.CycleTrace(2, log):
                    with timing.span("mcp.observe"):
                        raise RuntimeError("fresh telemetry unavailable")
            with timing.span("outside_cycle"):
                pass
            with timing.CycleTrace(3, log):
                timing.annotate(outcome="executed", action="wait")
            records = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
        failed, succeeded = records
        self.assertEqual(failed["outcome"], "error")
        self.assertEqual(failed["error_type"], "RuntimeError")
        self.assertEqual(failed["spans"][0]["stage"], "mcp.observe")
        self.assertEqual(failed["spans"][0]["outcome"], "error")
        self.assertEqual(succeeded["cycle"], 3)
        self.assertEqual(succeeded["spans"], [])
        self.assertEqual(failed["run_id"], succeeded["run_id"])

    def test_unwritable_timing_log_does_not_replace_action_error(self):
        with tempfile.TemporaryDirectory() as directory, patch("builtins.print") as printed:
            blocked = Path(directory) / "not_a_directory"
            blocked.write_text("blocked", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "action failed"):
                with timing.CycleTrace(1, blocked / "timing.jsonl"):
                    raise ValueError("action failed")
            self.assertTrue(any("timing_log_unavailable" in str(call) for call in printed.call_args_list))



if __name__ == "__main__":
    unittest.main()
