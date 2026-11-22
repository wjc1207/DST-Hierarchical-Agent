"""Steam Workshop packaging, offline self-contained installer, and in-game pairing/setup tests."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from zipfile import ZipFile

from common.pairing import ensure_bridge_token, format_pair_command
from common.paths import PROJECT_ROOT, PYTHON_ROOT

sys.path.insert(0, str(PYTHON_ROOT / "tools"))
import package_mods  # noqa: E402


class WorkshopPairingAndPackagingTests(unittest.TestCase):
    def test_pairing_helper_creates_reuses_and_formats_commands(self):
        with tempfile.TemporaryDirectory(dir=PYTHON_ROOT / "tests") as folder:
            cfg = Path(folder) / "bridge.json"
            token1, created1 = ensure_bridge_token(cfg)
            self.assertTrue(created1)
            self.assertEqual(len(token1), 64)
            token2, created2 = ensure_bridge_token(cfg)
            self.assertFalse(created2)
            self.assertEqual(token1, token2)
            self.assertEqual(json.loads(cfg.read_text(encoding="utf-8"))["token"], token1)

            self.assertEqual(
                format_pair_command(token1, 8765, prefix="/agent", default_port=8765),
                f"/agent pair {token1}",
            )
            self.assertEqual(
                format_pair_command(token1, 9001, prefix="/agent", default_port=8765),
                f"/agent pair {token1} 9001",
            )
            self.assertEqual(
                format_pair_command(token1, 8766, prefix="/buddy", default_port=8766),
                f"/buddy pair {token1}",
            )
            self.assertEqual(
                format_pair_command(token1, 9002, prefix="/buddy", default_port=8766),
                f"/buddy pair {token1} 9002",
            )

    def test_cli_print_pair_command_for_all_four_entrypoints(self):
        cases = [
            ("mcp_server/mcp_server.py", "/agent", 8765, 9101),
            ("mcp_server/companion_server.py", "/buddy", 8766, 9102),
            ("mcp_agent/mcp_agent.py", "/agent", 8765, 9103),
            ("mcp_agent/companion_agent.py", "/buddy", 8766, 9104),
        ]
        with tempfile.TemporaryDirectory(dir=PYTHON_ROOT / "tests") as folder:
            for idx, (script, prefix, default_port, custom_port) in enumerate(cases):
                cfg = Path(folder) / f"cfg_{idx}.json"
                with self.subTest(script=script, mode="default_port"):
                    res = subprocess.run(
                        [sys.executable, str(PYTHON_ROOT / script),
                         "--config", str(cfg), "--port", str(default_port), "--print-pair-command"],
                        cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=10,
                    )
                    self.assertEqual(res.returncode, 0, res.stderr)
                    line = res.stdout.strip()
                    token = json.loads(cfg.read_text(encoding="utf-8"))["token"]
                    self.assertEqual(line, f"{prefix} pair {token}")
                    self.assertNotIn("Traceback", res.stderr)
                with self.subTest(script=script, mode="custom_port"):
                    res2 = subprocess.run(
                        [sys.executable, str(PYTHON_ROOT / script),
                         "--config", str(cfg), "--port", str(custom_port), "--print-pair-command"],
                        cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=10,
                    )
                    self.assertEqual(res2.returncode, 0, res2.stderr)
                    self.assertEqual(res2.stdout.strip(), f"{prefix} pair {token} {custom_port}")

    def test_lua_pairing_and_setup_modules_match_and_intercept_locally(self):
        player_lua = (PROJECT_ROOT / "mods/dst_player_agent/scripts/jev_bridge_pairing.lua").read_text(encoding="utf-8")
        buddy_lua = (PROJECT_ROOT / "mods/dst_buddy_agent/scripts/jev_bridge_pairing.lua").read_text(encoding="utf-8")
        self.assertEqual(player_lua, buddy_lua)
        self.assertIn("SetPersistentString", player_lua)
        self.assertIn("GetPersistentString", player_lua)
        self.assertIn('if cmd == "setup" then', player_lua)

        player_ui = (PROJECT_ROOT / "mods/dst_player_agent/scripts/jev_autonomy_ui.lua").read_text(encoding="utf-8")
        self.assertIn('Pairing.parse(text,"/agent",8765,true)', player_ui)
        self.assertIn('elseif p_action=="setup" then', player_ui)
        self.assertIn("1_install_agent.bat", player_ui)
        self.assertIn("2_run_player.bat", player_ui)
        self.assertLess(player_ui.index("Pairing.parse"), player_ui.index("return run(self,...)"))

        buddy_main = (PROJECT_ROOT / "mods/dst_buddy_agent/modmain.lua").read_text(encoding="utf-8")
        self.assertIn('Pairing.parse(text,"/buddy",8766,false)', buddy_main)
        self.assertIn('elseif p_action=="setup" then', buddy_main)
        self.assertIn("MODROOT", buddy_main)
        self.assertIn("1_install_agent.bat", buddy_main)
        self.assertIn("2_run_buddy.bat", buddy_main)
        self.assertIn('"jev_buddy_bridge_v1"', buddy_main)
        self.assertIn('version="0.8.1"', buddy_main)

        player_main = (PROJECT_ROOT / "mods/dst_player_agent/modmain.lua").read_text(encoding="utf-8")
        self.assertIn('"jev_player_bridge_v1"', player_main)
        self.assertIn("MODROOT", player_main)

    def test_package_mods_produces_self_contained_workshop_dirs_and_clean_zips(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
            out_dir = Path(folder)
            res = subprocess.run(
                [sys.executable, str(PYTHON_ROOT / "tools/package_mods.py"), "--output-dir", str(out_dir)],
                cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=20,
            )
            self.assertEqual(res.returncode, 0, res.stderr)

            expected_versions = {
                "dst_player_agent": "1.9.1",
                "dst_buddy_agent": "0.8.1",
            }
            for mod_id, meta in package_mods.MODS.items():
                config_name = meta["config_name"]
                port = meta["port"]
                run_stem = meta["run_stem"]
                expected_cfg = package_mods.blank_config_lua(port)

                # Verify self-contained Workshop upload folder
                stage = out_dir / "workshop" / mod_id
                self.assertTrue(stage.is_dir(), f"Missing workshop stage: {stage}")
                package_mods.verify_workshop_dir(stage, mod_id)
                modinfo = (stage / "modinfo.lua").read_text(encoding="utf-8")
                self.assertIn(f'version = "{expected_versions[mod_id]}"', modinfo)
                self.assertIn('forumthread = ""', modinfo)
                self.assertEqual((stage / "scripts" / config_name).read_text(encoding="utf-8"), expected_cfg)
                self.assertFalse((stage / "README.md").exists())
                self.assertTrue((stage / "preview.jpg").is_file())
                self.assertLessEqual((stage / "preview.jpg").stat().st_size, 1_000_000)

                # Embedded offline scripts + runner
                self.assertTrue((stage / "1_install_agent.bat").is_file())
                self.assertTrue((stage / f"{run_stem}.bat").is_file())
                self.assertTrue((stage / "1_install_agent.sh").is_file())
                self.assertTrue((stage / f"{run_stem}.sh").is_file())
                self.assertEqual(
                    (stage / "runner/version.txt").read_text(encoding="utf-8").strip(),
                    f"{mod_id}:{expected_versions[mod_id]}",
                )

                # Verify ZIP archive
                archive = out_dir / f"{mod_id}.zip"
                self.assertTrue(archive.is_file())
                with ZipFile(archive, "r") as zf:
                    names = set(zf.namelist())
                    self.assertIn(f"{mod_id}/modinfo.lua", names)
                    self.assertIn(f"{mod_id}/preview.jpg", names)
                    self.assertIn(f"{mod_id}/scripts/jev_bridge_pairing.lua", names)
                    self.assertIn(f"{mod_id}/scripts/{config_name}", names)
                    self.assertNotIn(f"{mod_id}/README.md", names)
                    self.assertEqual(zf.read(f"{mod_id}/scripts/{config_name}").decode("utf-8"), expected_cfg)

            # Verify GitHub one-click Python runner bundle
            runner_zip = out_dir / "dst_agent_runner.zip"
            self.assertTrue(runner_zip.is_file())
            with ZipFile(runner_zip, "r") as zf:
                names = set(zf.namelist())
                for required in (
                    "dst_agent_runner/setup_and_run.bat",
                    "dst_agent_runner/setup_and_run.sh",
                    "dst_agent_runner/scripts/launcher.ps1",
                    "dst_agent_runner/scripts/workshop_install.ps1",
                    "dst_agent_runner/.env.example",
                    "dst_agent_runner/data/DSTKnowledge.txt",
                    "dst_agent_runner/data/worldgen_resources.json",
                    "dst_agent_runner/python/requirements-mcp.txt",
                    "dst_agent_runner/python/common/pairing.py",
                    "dst_agent_runner/python/mcp_server/mcp_server.py",
                    "dst_agent_runner/python/mcp_server/companion_server.py",
                    "dst_agent_runner/python/mcp_agent/mcp_agent.py",
                    "dst_agent_runner/python/mcp_agent/companion_agent.py",
                ):
                    self.assertIn(required, names)
                for name in names:
                    self.assertFalse(name.endswith("/.env"), f"Leaked .env: {name}")
                    self.assertNotIn("/logs/", name)
                    self.assertNotIn("/agent_memory/", name)
                    self.assertNotIn("/tests/", name)
                    self.assertNotIn("__pycache__", name)

            # On Windows, smoke-test offline sync via workshop_install.ps1 -CheckOnly
            # and verify user .env, logs, and agent_memory are strictly preserved.
            if os.name == "nt":
                install_dir = out_dir / "user_install"
                (install_dir / "logs").mkdir(parents=True)
                (install_dir / "data" / "agent_memory").mkdir(parents=True)
                (install_dir / ".env").write_text("DEEPSEEK_API_KEY=secret-keep\n", encoding="utf-8")
                (install_dir / "logs" / "companion_bridge.json").write_text('{"token":"keep"}', encoding="utf-8")
                (install_dir / "data" / "agent_memory" / "global.md").write_text("keep-memory", encoding="utf-8")

                buddy_runner = out_dir / "workshop" / "dst_buddy_agent" / "runner"
                ps_res = subprocess.run(
                    [
                        "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                        "-File", str(buddy_runner / "scripts" / "workshop_install.ps1"),
                        "-SourceDir", str(buddy_runner),
                        "-InstallDir", str(install_dir),
                        "-DefaultMode", "buddy",
                        "-SkipShortcut", "-CheckOnly",
                    ],
                    cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=20,
                )
                self.assertEqual(ps_res.returncode, 0, ps_res.stderr + ps_res.stdout)
                self.assertEqual((install_dir / ".env").read_text(encoding="utf-8"), "DEEPSEEK_API_KEY=secret-keep\n")
                self.assertEqual((install_dir / "logs" / "companion_bridge.json").read_text(encoding="utf-8"), '{"token":"keep"}')
                self.assertEqual((install_dir / "data" / "agent_memory" / "global.md").read_text(encoding="utf-8"), "keep-memory")
                self.assertTrue((install_dir / "run_buddy.bat").is_file())
                self.assertTrue((install_dir / "run_player.bat").is_file())
                self.assertTrue((install_dir / "data" / "DSTKnowledge.txt").is_file())
                self.assertTrue((install_dir / "python" / "mcp_agent" / "companion_agent.py").is_file())
                self.assertEqual(
                    (install_dir / ".workshop_runner_version").read_text(encoding="utf-8").strip(),
                    "dst_buddy_agent:0.8.1",
                )

    def test_one_click_installer_scripts_encoding_and_syntax(self):
        bat = (PROJECT_ROOT / "setup_and_run.bat").read_bytes()
        self.assertFalse(bat.startswith(b"\xef\xbb\xbf"), "BAT file must not have UTF-8 BOM")
        sh = (PROJECT_ROOT / "setup_and_run.sh").read_bytes()
        self.assertNotIn(b"\r\n", sh, "Shell script must use LF line endings")
        for ps1 in ("scripts/launcher.ps1", "scripts/workshop_install.ps1", "install.ps1"):
            raw = (PROJECT_ROOT / ps1).read_bytes()
            self.assertTrue(raw.startswith(b"\xef\xbb\xbf"), f"{ps1} must have UTF-8 BOM for PowerShell 5.1")


if __name__ == "__main__":
    unittest.main()
