"""Timing records preserve outcomes and distinguish nested elapsed times."""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import controller
import jev_agent
import timing_log as timing


class TimingTests(unittest.TestCase):
    def test_wait_only_context_is_logged_without_replacing_the_model_decision(self):
        observed = {"world": {"day": 1, "phase": "day"}, "nearby": [],
                    "player": {"vitals": {"hunger": 100, "hunger_max": 150}},
                    "navigation": {"status": "no_frontier", "frontier_count": 3,
                                   "search": {"status": "temporarily_blocked"},
                                   "graph": {"known_nodes": 10, "routes": []},
                                   "diagnostics": {"graph_paths": {"no_observed_start": 1}}}}
        args = SimpleNamespace(log=Path("unused.log"), show_probabilities=False,
                               execute=False, min_confidence=None, move_seconds=1)
        result = {"response": {"model": "test"}, "answer": {"choice": "wait", "confidence": 1}}
        with (patch.object(controller, "latest_state", return_value=(observed, 1)),
              patch.object(jev_agent, "call_jev", return_value=result) as request,
              patch("builtins.print") as printed):
            with timing.CycleTrace(1) as trace:
                jev_agent.run_decision_cycle(args, "url", "test-key", "test")
        request.assert_called_once()
        context = trace.record["decision_candidates"]
        self.assertTrue(context["only_wait"])
        self.assertEqual(context["keys"], ["wait"])
        self.assertEqual(context["known_regions"], 10)
        self.assertIn("no_observed_start", context["path_diagnostics"]["graph_paths"])
        self.assertTrue(any("only_wait_available" in str(c) for c in printed.call_args_list))
        self.assertNotIn("test-key", json.dumps(trace.record))

    def test_nested_times_separate_parent_total_from_self_time(self):
        with patch.object(timing.time, "perf_counter", side_effect=[0, 1, 2, 5, 7, 8]), patch("builtins.print"):
            with timing.CycleTrace(4) as trace:
                with timing.span("action.execute"):
                    with timing.span("state.wait_fresh"):
                        pass
        self.assertEqual(trace.record["total_ms"], 8000)
        self.assertEqual(trace.record["totals"]["action.execute"]["total_ms"], 6000)
        self.assertEqual(trace.record["totals"]["action.execute"]["self_ms"], 3000)
        parent, child = trace.record["spans"]
        self.assertEqual(child["parent_id"], parent["id"])
        self.assertEqual(child["duration_ms"], 3000)

    def test_repeated_log_polls_are_aggregated_and_detail_is_bounded(self):
        with patch.object(timing, "MAX_SPANS", 2), patch("builtins.print"):
            with timing.CycleTrace(1) as trace:
                for _ in range(5):
                    with timing.span("state.read", record=False):
                        pass
                    with timing.span("input.hold"):
                        pass
        self.assertEqual(trace.record["totals"]["state.read"]["calls"], 5)
        self.assertEqual(trace.record["totals"]["input.hold"]["calls"], 5)
        self.assertEqual(len(trace.record["spans"]), 2)
        self.assertEqual(trace.record["dropped_spans"], 3)

    def test_timeout_is_logged_and_still_raised_then_context_is_reset(self):
        with tempfile.TemporaryDirectory() as directory, patch("builtins.print"):
            log = Path(directory) / "timing.jsonl"
            with self.assertRaisesRegex(RuntimeError, "fresh telemetry"):
                with timing.CycleTrace(2, log):
                    controller.wait_for_fresh_state(Path("unused.log"), 0, timeout=0)
            with timing.span("outside_cycle"):
                pass
            with timing.CycleTrace(3, log):
                timing.annotate(outcome="executed", action="wait")
            records = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
        failed, succeeded = records
        self.assertEqual(failed["outcome"], "error")
        self.assertEqual(failed["error_type"], "RuntimeError")
        self.assertEqual(failed["spans"][0]["stage"], "state.wait_fresh")
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

    def test_dry_run_and_abstention_are_recorded_without_game_input(self):
        from test_regressions import state, hostile
        observed = state([hostile(22, 3)])
        observed["player"]["inventory"]["equipped"] = [{"prefab": "axe", "weapon": True, "count": 1}]
        args = SimpleNamespace(log=Path("unused.log"), show_probabilities=False,
                               execute=False, min_confidence=0.5, move_seconds=1.0)
        result = {"response": {"model": "test"}, "answer": {"choice": "attack_nearest_hostile", "confidence": 0.1}}
        with (
            patch.object(controller, "latest_state", return_value=(observed, 1)),
            patch.object(jev_agent, "call_jev", return_value=result),
            patch.object(jev_agent, "execute_action") as execute,
            patch("builtins.print"),
        ):
            for execute_enabled, outcome in ((False, "dry_run"), (True, "abstained")):
                args.execute = execute_enabled
                with timing.CycleTrace(1) as trace:
                    self.assertFalse(jev_agent.run_decision_cycle(args, "url", "secret", "test"))
                self.assertEqual(trace.record["outcome"], outcome)
                self.assertEqual(trace.record["action"], "attack_nearest_hostile")
                self.assertNotIn("secret", json.dumps(trace.record))
                self.assertIn("decision.candidates", trace.record["totals"])
            execute.assert_not_called()


if __name__ == "__main__":
    unittest.main()
