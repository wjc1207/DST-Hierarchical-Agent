"""Probe reports isolate client measurements and never treat server logs as RTT."""
import json
import unittest
from unittest.mock import patch
from tools.summarize_buddy_probe import summaries, MARKER, main


class ProbeReportTests(unittest.TestCase):
    def test_only_client_summaries_are_admitted(self):
        client = {"side": "client", "event": "summary", "run_id": "client:1", "metrics": {}}
        server = {"side": "server", "event": "summary", "metrics": {"fake": 1000}}
        lines = ["[00:01:01]: " + MARKER + json.dumps(server), MARKER + json.dumps(client)]
        self.assertEqual(summaries(lines), [client])

    def test_invalid_and_unrelated_lines_do_not_replace_a_report(self):
        self.assertEqual(summaries(["unrelated", MARKER + "broken", MARKER + "[]",
                                   MARKER + json.dumps({"side": "client", "event": "sent"})]), [])

    def test_repeated_reports_keep_their_order_and_units(self):
        rows = [{"side": "client", "event": "summary", "run_id": "A",
                 "metrics": {"ping_effect": {"count": 30, "p95_ms": value}}} for value in (75, 80)]
        self.assertEqual(summaries([MARKER + json.dumps(row) for row in rows]), rows)

    def test_cli_rejects_the_old_zero_clock_report(self):
        row = {"side": "client", "event": "summary", "clock": "GetTimeRealSeconds", "metrics": {"bind_effect": {"p50_ms": 0}}}
        with patch("sys.argv", ["probe", "unused.log"]), patch("pathlib.Path.read_text", return_value=MARKER + json.dumps(row)), \
                patch("sys.stderr"), self.assertRaises(SystemExit) as stopped:
            main()
        self.assertEqual(stopped.exception.code, 2)

    def test_cli_accepts_validated_clock_and_rejects_invalid_samples(self):
        for invalid in (0, 1):
            row = {"side": "client", "event": "summary", "clock": "walltime", "clock_validated": True,
                   "metrics": {}, "counts": {"clock_invalid": invalid}}
            with self.subTest(invalid=invalid), patch("sys.argv", ["probe", "unused.log"]), \
                    patch("pathlib.Path.read_text", return_value=MARKER + json.dumps(row)), patch("builtins.print"), patch("sys.stderr"):
                if invalid:
                    with self.assertRaises(SystemExit) as stopped:
                        main()
                    self.assertEqual(stopped.exception.code, 2)
                else:
                    self.assertEqual(main(), 0)
