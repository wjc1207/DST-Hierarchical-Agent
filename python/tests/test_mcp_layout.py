"""Package layout, 3-layer unidirectional DAG, and bidirectional archive isolation checks."""
import ast
import asyncio
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from common.paths import PROJECT_ROOT, PYTHON_ROOT
from mcp_agent import companion_agent
from mcp_server import companion_server


class LayoutTests(unittest.TestCase):
    def test_entry_points_share_project_root_and_legacy_files_stay_out_of_active_dirs(self):
        for entry in (companion_agent, companion_server):
            with self.subTest(entry=entry.__name__):
                self.assertEqual(entry.PROJECT_ROOT, PROJECT_ROOT)
                self.assertEqual(
                    entry.PROJECT_ROOT / "logs" / "companion_bridge.json",
                    PROJECT_ROOT / "logs" / "companion_bridge.json",
                )
        for obsolete in (
            PROJECT_ROOT / "mcp_server.py",
            PROJECT_ROOT / "mcp_agent.py",
            PROJECT_ROOT / "mods" / "dst_player_agent",
            PYTHON_ROOT / "mcp_server" / "mcp_server.py",
            PYTHON_ROOT / "mcp_server" / "game_bridge.py",
            PYTHON_ROOT / "mcp_server" / "transport_metrics.py",
            PYTHON_ROOT / "mcp_agent" / "mcp_agent.py",
            PYTHON_ROOT / "mcp_agent" / "mcp_probe.py",
            PYTHON_ROOT / "mcp_agent" / "daily_planner.py",
            PYTHON_ROOT / "mcp_agent" / "map_view.py",
            PYTHON_ROOT / "mcp_agent" / "map_icons.py",
            PYTHON_ROOT / "mcp_agent" / "model_state.py",
            PYTHON_ROOT / "mcp_agent" / "camp_execution.py",
            PYTHON_ROOT / "mcp_agent" / "compact_tasks.py",
            PYTHON_ROOT / "mcp_agent" / "companion_search.py",
            PYTHON_ROOT / "mcp_agent" / "companion_tasks.py",
            PYTHON_ROOT / "mcp_agent" / "maintenance.py",
            PYTHON_ROOT / "mcp_agent" / "navigation_intents.py",
            PYTHON_ROOT / "mcp_agent" / "priority_board.py",
            PYTHON_ROOT / "mcp_agent" / "region_resources.py",
            PYTHON_ROOT / "mcp_agent" / "residence.py",
        ):
            self.assertFalse(obsolete.exists(), f"Obsolete path should not exist in active tree: {obsolete}")

        for archived in (
            PROJECT_ROOT / "archive" / "mods" / "dst_player_agent",
            PROJECT_ROOT / "archive" / "python" / "mcp_server" / "player_server.py",
            PROJECT_ROOT / "archive" / "python" / "mcp_server" / "player_bridge.py",
            PROJECT_ROOT / "archive" / "python" / "mcp_server" / "player_task_service.py",
            PROJECT_ROOT / "archive" / "python" / "mcp_agent" / "player_agent.py",
            PROJECT_ROOT / "archive" / "python" / "mcp_agent" / "player_probe.py",
            PROJECT_ROOT / "archive" / "python" / "mcp_agent" / "player_planner.py",
            PROJECT_ROOT / "archive" / "python" / "tools" / "client_e2e_sandbox.py",
            PROJECT_ROOT / "archive" / "python" / "run_tests.py",
        ):
            self.assertTrue(archived.exists(), f"Missing archived client artifact: {archived}")

    def test_unidirectional_three_layer_dependency_dag(self):
        runtime_layers = {"common", "transport", "executor", "mcp_server", "mcp_agent"}
        allowed_internal = {
            "common": {"common"},
            "transport": {"transport"},
            "executor": {"common", "transport", "executor"},
            "mcp_server": {"common", "transport", "executor", "mcp_server"},
            "mcp_agent": {"common", "transport", "executor", "mcp_agent"},
        }
        for pkg in sorted(runtime_layers):
            for path in sorted((PYTHON_ROOT / pkg).rglob("*.py")):
                if "__pycache__" in path.parts:
                    continue
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        targets = [alias.name for alias in node.names]
                    elif isinstance(node, ast.ImportFrom):
                        targets = [node.module or ""]
                    else:
                        continue
                    for target in targets:
                        top = target.split(".")[0]
                        if top == "mcp" and pkg in {"common", "transport", "executor"}:
                            self.fail(f"Layer {pkg} ({path}) must not depend on MCP SDK ({target!r})")
                        if top in runtime_layers:
                            self.assertIn(
                                top, allowed_internal[pkg],
                                f"Forbidden cross-layer import in {path}: {pkg} -> {target}",
                            )

    def test_active_and_archive_python_trees_have_zero_cross_imports(self):
        forbidden_from_active = {
            "archive", "player_bridge", "player_planner", "player_server", "player_agent", "player_probe",
            "game_bridge", "daily_planner", "map_view", "map_icons", "model_state",
            "mcp_agent.mcp_agent", "mcp_server.mcp_server",
        }
        for path in sorted(PYTHON_ROOT.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    targets = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    targets = [node.module or ""]
                else:
                    continue
                for target in targets:
                    parts = set(target.split("."))
                    self.assertFalse(
                        target in forbidden_from_active or bool(parts & forbidden_from_active),
                        f"Active file {path} imports archived/legacy symbol {target!r}",
                    )

        archive_py = PROJECT_ROOT / "archive" / "python"
        forbidden_from_archive = {
            "tools.headless_sandbox", "transport", "executor",
            "mcp_agent.companion_agent", "mcp_agent.companion_planner",
            "mcp_server.companion_server", "mcp_server.companion_bridge",
        }
        for path in sorted(archive_py.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            text = path.read_text(encoding="utf-8")
            self.assertNotIn('_root / "python"', text, f"Archive file {path} references active python dir")
            self.assertNotIn('PROJECT_ROOT / "python"', text, f"Archive file {path} references active python dir")
            tree = ast.parse(text, filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    targets = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    targets = [node.module or ""]
                else:
                    continue
                for target in targets:
                    top = target.split(".")[0]
                    self.assertFalse(
                        target in forbidden_from_archive or top in {"transport", "executor"},
                        f"Archive file {path} imports active module {target!r}",
                    )

    def test_direct_entry_points_accept_help_from_an_unrelated_directory(self):
        with tempfile.TemporaryDirectory(dir=PYTHON_ROOT / "tests") as folder:
            for script in (
                PYTHON_ROOT / "mcp_server" / "companion_server.py",
                PYTHON_ROOT / "mcp_agent" / "companion_agent.py",
            ):
                with self.subTest(script=str(script)):
                    result = subprocess.run(
                        [sys.executable, str(script), "--help"],
                        cwd=folder, capture_output=True, text=True, timeout=10,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn("--config", result.stdout)
                    self.assertNotIn("Traceback", result.stderr)

    def test_package_entry_points_accept_help_without_starting_a_service(self):
        for package in ("mcp_server", "mcp_agent"):
            with self.subTest(package=package):
                result = subprocess.run(
                    [sys.executable, "-m", package, "--help"],
                    cwd=PYTHON_ROOT, capture_output=True, text=True, timeout=10,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("--config", result.stdout)

    def test_companion_host_launches_companion_server_using_project_cwd(self):
        try:
            import mcp
        except ImportError:
            self.skipTest("Optional MCP SDK not installed")

        class InspectedLaunch(Exception):
            pass

        seen = []

        def capture(parameters, **_):
            seen.append(parameters)
            raise InspectedLaunch

        args = SimpleNamespace(port=8766, config=Path("unused.json"), poll=0.2)
        with patch.object(mcp, "Client", side_effect=capture), patch.object(companion_agent, "check_bridge_port"):
            with self.assertRaises(InspectedLaunch):
                asyncio.run(companion_agent.run(args, None))
        self.assertEqual(len(seen), 1)
        self.assertEqual(Path(seen[0].args[0]), PYTHON_ROOT / "mcp_server" / "companion_server.py")
        self.assertEqual(Path(seen[0].cwd), PROJECT_ROOT)


if __name__ == "__main__":
    unittest.main()
