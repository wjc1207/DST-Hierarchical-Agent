"""Package layout regression: no model API calls, config writes or game inputs."""
import asyncio
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from common.paths import PROJECT_ROOT, PYTHON_ROOT
from mcp_agent import companion_agent, mcp_agent, mcp_probe
from mcp_server import companion_server, mcp_server


class LayoutTests(unittest.TestCase):
    def test_entry_points_share_project_root_and_keep_existing_config_location(self):
        for entry in (mcp_agent, mcp_probe, mcp_server, companion_agent, companion_server):
            with self.subTest(entry=entry.__name__):
                self.assertEqual(entry.PROJECT_ROOT, PROJECT_ROOT)
                self.assertEqual(entry.PROJECT_ROOT / "logs" / "mcp_bridge.json", PROJECT_ROOT / "logs" / "mcp_bridge.json")
        self.assertFalse((PROJECT_ROOT / "mcp_server.py").exists())
        self.assertFalse((PROJECT_ROOT / "mcp_agent.py").exists())

    def test_direct_entry_points_accept_help_from_an_unrelated_directory(self):
        with tempfile.TemporaryDirectory(dir=PYTHON_ROOT / "tests") as folder:
            for script in ("mcp_server/mcp_server.py", "mcp_agent/mcp_agent.py", "mcp_agent/mcp_probe.py",
                           "mcp_server/companion_server.py", "mcp_agent/companion_agent.py"):
                with self.subTest(script=script):
                    result = subprocess.run([sys.executable, str(PYTHON_ROOT / script), "--help"],
                                            cwd=folder, capture_output=True, text=True, timeout=10)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn("--config", result.stdout)
                    self.assertNotIn("Traceback", result.stderr)

    def test_package_entry_points_accept_help_without_starting_a_service(self):
        for package in ("mcp_server", "mcp_agent"):
            with self.subTest(package=package):
                result = subprocess.run([sys.executable, "-m", package, "--help"], cwd=PYTHON_ROOT,
                                        capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("--config", result.stdout)

    def test_host_and_probe_launch_the_relocated_server_using_project_cwd(self):
        try:
            import mcp
        except ImportError:
            self.skipTest("Optional MCP SDK not installed")
        class InspectedLaunch(Exception): pass
        seen = []
        def capture(parameters, **_):
            seen.append(parameters)
            raise InspectedLaunch
        args = SimpleNamespace(port=8765, config=Path("unused.json"))
        with patch.object(mcp, "Client", side_effect=capture), patch.object(mcp_agent, "check_bridge_port"):
            with self.assertRaises(InspectedLaunch):
                asyncio.run(mcp_agent.run(args, None, None))
            with self.assertRaises(InspectedLaunch):
                asyncio.run(mcp_probe.probe(args))
        for parameters in seen:
            self.assertEqual(Path(parameters.args[0]), PYTHON_ROOT / "mcp_server" / "mcp_server.py")
            self.assertEqual(Path(parameters.cwd), PROJECT_ROOT)


if __name__ == "__main__":
    unittest.main()
