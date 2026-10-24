"""Exclusive loopback startup; fake listeners only, never the actual game port."""
import asyncio
import contextlib
import io
import subprocess
import sys
import tempfile
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from mcp_server.game_bridge import (BridgePortUnavailable, ExclusiveBridgeHTTPServer,
                         check_bridge_port)
from mcp_server import mcp_server
from mcp_agent.mcp_agent import run


ROOT = Path(__file__).resolve().parent.parent


class StartupTests(unittest.TestCase):
    def test_legacy_reusable_listener_is_rejected_without_disturbing_its_owner(self):
        # Reproduce the pre-fix server: Windows used to accept a second bind.
        listener = ThreadingHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
        try:
            port = listener.server_port
            with self.assertRaises(BridgePortUnavailable) as error:
                check_bridge_port(port)
            self.assertIn(f"127.0.0.1:{port}", str(error.exception))
            self.assertIn("Stop the other DST MCP", str(error.exception))
            self.assertGreaterEqual(listener.socket.fileno(), 0)
            self.assertEqual(listener.socket.getsockname()[1], port)
        finally:
            listener.server_close()
        check_bridge_port(port)  # Released ports can be used again.

    def test_new_listener_is_exclusive_against_new_and_legacy_servers(self):
        listener = ExclusiveBridgeHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
        try:
            self.assertFalse(listener.allow_reuse_port)
            with self.assertRaises(BridgePortUnavailable):
                check_bridge_port(listener.server_port)
            with self.assertRaises(OSError):
                ThreadingHTTPServer(listener.server_address, BaseHTTPRequestHandler)
        finally:
            listener.server_close()

    def test_host_fails_before_child_launch_models_or_observation_loop(self):
        listener = ThreadingHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
        decide, planner = Mock(), Mock()
        try:
            with self.assertRaises(BridgePortUnavailable):
                asyncio.run(run(SimpleNamespace(port=listener.server_port), decide, planner))
            decide.assert_not_called()
            planner.update.assert_not_called()
        finally:
            listener.server_close()

    def test_server_cli_fails_cleanly_before_creating_config_or_protocol_output(self):
        listener = ThreadingHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
        try:
            with tempfile.TemporaryDirectory(dir=ROOT / "tests") as folder:
                config = Path(folder) / "new.json"
                result = subprocess.run([sys.executable, str(ROOT / "mcp_server" / "mcp_server.py"),
                    "--bridge-only", "--config", str(config), "--port", str(listener.server_port)],
                    cwd=ROOT, capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("mcp_startup_error bridge_port_unavailable", result.stderr)
                self.assertIn("No existing process was stopped", result.stderr)
                self.assertNotIn("Traceback", result.stderr)
                self.assertEqual(result.stdout, "")
                self.assertFalse(config.exists())
        finally:
            listener.server_close()

    def test_final_server_bind_rechecks_after_preflight_race(self):
        listener = ThreadingHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
        try:
            with tempfile.TemporaryDirectory(dir=ROOT / "tests") as folder:
                args = ["mcp_server.py", "--bridge-only", "--config", str(Path(folder) / "new.json"),
                        "--port", str(listener.server_port)]
                with (patch.object(sys, "argv", args), patch.object(mcp_server, "check_bridge_port"),
                      contextlib.redirect_stderr(io.StringIO()) as output):
                    self.assertEqual(mcp_server.main(), 2)
                self.assertIn("bridge_port_unavailable", output.getvalue())
                self.assertNotIn("listening on", output.getvalue())
                self.assertGreaterEqual(listener.socket.fileno(), 0)
        finally:
            listener.server_close()


if __name__ == "__main__":
    unittest.main()
