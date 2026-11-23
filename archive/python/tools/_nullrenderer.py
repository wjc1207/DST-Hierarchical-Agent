"""Minimal standalone Klei nullrenderer harness helper for archive/python/tools/client_e2e_sandbox.py."""
from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

ARCHIVE_PYTHON_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = ARCHIVE_PYTHON_ROOT.parents[1]

DEFAULT_GAME_DIRS = (
    Path(r"C:\Program Files (x86)\Steam\steamapps\common\Don't Starve Together"),
    Path(r"D:\SteamLibrary\steamapps\common\Don't Starve Together"),
    Path(r"E:\SteamLibrary\steamapps\common\Don't Starve Together"),
)


def find_nullrenderer(game_dir: Path | None = None) -> tuple[Path, Path]:
    candidates: list[Path] = []
    if game_dir:
        candidates.append(game_dir)
    env_dir = os.environ.get("DST_GAME_DIR")
    if env_dir:
        candidates.append(Path(env_dir))
    candidates.extend(DEFAULT_GAME_DIRS)
    for root in candidates:
        for rel in (
            Path("bin64/dontstarve_dedicated_server_nullrenderer_x64.exe"),
            Path("bin/dontstarve_dedicated_server_nullrenderer.exe"),
            Path("bin64/dontstarve_dedicated_server_nullrenderer_x64"),
            Path("bin/dontstarve_dedicated_server_nullrenderer"),
        ):
            binary = root / rel
            if binary.is_file():
                return root, binary
    raise FileNotFoundError(
        "Cannot locate dontstarve_dedicated_server_nullrenderer binary. "
        "Pass --game-dir or set DST_GAME_DIR."
    )


class HeadlessDSTSandbox:
    """Minimal isolated offline DST nullrenderer server runner for archived client E2E tests."""

    def __init__(
        self,
        *,
        game_dir: Path | None = None,
        port: int = 18766,
        time_scale: float = 1.0,
        storage_root: Path | None = None,
        reset_world: bool = False,
        server_port: int = 11009,
        auth_port: int = 8776,
        master_port: int = 27026,
        day_mode: str = "default",
        transport: str = "console",
        verbose: bool = False,
    ):
        self.game_dir, self.binary = find_nullrenderer(game_dir)
        self.port = port
        self.time_scale = time_scale
        self.storage_root = (storage_root or (PROJECT_ROOT / "logs" / "client_sandbox" / "server")).resolve()
        self.cluster_dir = self.storage_root / "DoNotStarveTogether" / "TestSandbox"
        self.master_dir = self.cluster_dir / "Master"
        self.log_file = self.master_dir / "server_log.txt"
        self.reset_world = reset_world
        self.server_port = server_port
        self.auth_port = auth_port
        self.master_port = master_port
        self.day_mode = day_mode
        self.transport = transport
        self.verbose = verbose
        self.proc: subprocess.Popen[str] | None = None
        self._stdin_lock = threading.Lock()
        self._stop_tail = threading.Event()
        self._tail_thread: threading.Thread | None = None
        self._events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self._session_ready = threading.Event()

    def _prepare_cluster(self) -> None:
        if self.reset_world and self.master_dir.exists():
            save_dir = self.master_dir / "save"
            if save_dir.exists():
                shutil.rmtree(save_dir)
        self.master_dir.mkdir(parents=True, exist_ok=True)
        (self.cluster_dir / "cluster.ini").write_text(
            "[GAMEPLAY]\ngame_mode = survival\nmax_players = 2\npvp = false\npause_when_empty = false\n\n"
            "[NETWORK]\ncluster_name = DST_Agent_Archived_Sandbox\ncluster_description = Archived E2E\n"
            "cluster_intention = cooperative\noffline_cluster = true\nlan_only_cluster = true\ntick_rate = 30\n\n"
            "[MISC]\nconsole_enabled = true\n\n[SHARD]\nshard_enabled = false\n",
            encoding="utf-8",
        )
        (self.cluster_dir / "cluster_token.txt").write_text("pds-g^KU_HEADLESS^OFFLINE_TEST_TOKEN\n", encoding="utf-8")
        (self.master_dir / "server.ini").write_text(
            f"[NETWORK]\nserver_port = {self.server_port}\n\n[SHARD]\nis_master = true\n\n"
            f"[STEAM]\nauthentication_port = {self.auth_port}\nmaster_server_port = {self.master_port}\n\n"
            "[ACCOUNT]\nencode_user_path = true\n",
            encoding="utf-8",
        )
        (self.master_dir / "modoverrides.lua").write_text("return {}\n", encoding="utf-8")
        (self.master_dir / "worldgenoverride.lua").write_text(
            'return { override_enabled = true, worldgen_preset = "SURVIVAL_TOGETHER", '
            'settings_preset = "SURVIVAL_TOGETHER", '
            f'overrides = {{ day = {json.dumps(self.day_mode)}, weather = "never", wildfires = "never", hounds = "never" }} }}\n',
            encoding="utf-8",
        )
        if self.log_file.exists():
            self.log_file.unlink()

    def _tail_log(self) -> None:
        seen = 0
        while not self._stop_tail.is_set():
            if self.log_file.exists():
                try:
                    text = self.log_file.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    time.sleep(0.05)
                    continue
                if len(text) > seen:
                    chunk = text[seen:]
                    seen = len(text)
                    for line in chunk.splitlines():
                        if "Telling Client our new session identifier:" in line:
                            self._session_ready.set()
                        idx = line.find("[HEADLESS]")
                        if idx >= 0:
                            rest = line[idx + len("[HEADLESS]"):].rstrip("\r\n\t")
                            parts = rest.split(" ", 1)
                            tag = parts[0]
                            raw_payload = parts[1] if len(parts) > 1 else "{}"
                            try:
                                payload = json.loads(raw_payload)
                            except ValueError:
                                payload = raw_payload
                            if self.verbose or tag in {"NOTICE", "ERROR"}:
                                print(f"[HEADLESS:{tag}] {payload}", flush=True)
                            self._events.put((tag, payload))
            time.sleep(0.05)

    def send_lua(self, code: str) -> None:
        if not self.proc or not self.proc.stdin:
            raise RuntimeError("Headless DST process is not running")
        line = code.replace("\r", " ").replace("\n", " ").strip()
        with self._stdin_lock:
            self.proc.stdin.write(line + "\n")
            self.proc.stdin.flush()

    def wait_event(self, wanted_tag: str, *, predicate=None, timeout: float = 30.0) -> Any:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            remaining = max(0.05, deadline - time.monotonic())
            try:
                tag, payload = self._events.get(timeout=min(0.25, remaining))
            except queue.Empty:
                if self.proc and self.proc.poll() is not None:
                    raise RuntimeError(f"Headless DST exited prematurely with code {self.proc.returncode}")
                continue
            if tag == wanted_tag and (predicate is None or predicate(payload)):
                return payload
        raise TimeoutError(f"Timed out after {timeout:.1f}s waiting for [HEADLESS]{wanted_tag}")

    def start(self, *, boot_timeout: float = 120.0) -> dict[str, Any]:
        self._prepare_cluster()
        cmd = [
            str(self.binary),
            "-persistent_storage_root", str(self.storage_root),
            "-conf_dir", "DoNotStarveTogether",
            "-cluster", "TestSandbox",
            "-shard", "Master",
            "-offline",
            "-console",
        ]
        self.proc = subprocess.Popen(
            cmd, cwd=str(self.binary.parent),
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8",
        )
        self._tail_thread = threading.Thread(target=self._tail_log, daemon=True)
        self._tail_thread.start()
        if not self._session_ready.wait(timeout=boot_timeout):
            raise TimeoutError(f"DST nullrenderer failed to reach live session within {boot_timeout}s")
        # Locate passable land center and initialize minimal HEADLESS namespace expected by client_e2e_sandbox.
        init_lua = (
            'HEADLESS = HEADLESS or {}; '
            'local x, z = 0, 0; '
            'for r = 0, 200, 12 do '
            '  for a = 0, 315, 45 do '
            '    local rad = math.rad(a); local cx, cz = r * math.cos(rad), r * math.sin(rad); '
            '    if TheWorld and TheWorld.Map and TheWorld.Map:IsPassableAtPoint(cx, 0, cz) then x, z = cx, cz; break end '
            '  end '
            'end; '
            'HEADLESS.ax, HEADLESS.az = math.floor(x + 0.5), math.floor(z + 0.5); '
            'HEADLESS.owner = AllPlayers and AllPlayers[1] or nil; '
            'print(string.format("[HEADLESS]READY {\\"x\\":%d,\\"z\\":%d}", HEADLESS.ax, HEADLESS.az))'
        )
        self.send_lua(init_lua)
        return self.wait_event("READY", timeout=15.0)

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            try:
                self.send_lua("c_shutdown(false)")
                self.proc.wait(timeout=6)
            except Exception:
                self.proc.kill()
        self._stop_tail.set()
        if self._tail_thread:
            self._tail_thread.join(timeout=2)

    def __enter__(self) -> "HeadlessDSTSandbox":
        self.start()
        return self

    def __exit__(self, *args) -> None:
        self.stop()
