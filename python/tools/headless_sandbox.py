"""Real-engine headless Don't Starve Together sandbox powered by Klei's nullrenderer.

Runs unmodified mods/dst_buddy_agent Lua code inside Klei's actual C++/Lua engine
(without opening a game window, touching Steam's mods/ folder, or touching Documents/Klei/).

Usage:
    # Run the automated 6-scenario real-engine E2E regression suite:
    python python/tools/headless_sandbox.py --suite

    # Run a headless DST server bridged to 127.0.0.1:8766 for companion_agent.py testing:
    python python/tools/headless_sandbox.py --serve --port 8766
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import secrets
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PYTHON_ROOT = PROJECT_ROOT / "python"
if str(PYTHON_ROOT) not in sys.path:
    sys.path.insert(0, str(PYTHON_ROOT))

from common.pairing import ensure_bridge_token
from mcp_agent.companion_planner import validate_plan
from mcp_agent.companion_tasks import CompanionTasks, capabilities
from mcp_server.companion_bridge import CompanionTeamBridge
from mcp_server.game_bridge import serve_bridge

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
    """Manages an isolated offline nullrenderer DST server + real CompanionTeamBridge."""

    def __init__(
        self,
        *,
        game_dir: Path | None = None,
        port: int = 18766,
        token: str | None = None,
        time_scale: float = 2.0,
        storage_root: Path | None = None,
        reset_world: bool = False,
        verbose: bool = False,
        external_bridge: bool = False,
        transport: str = "console",
        server_port: int = 11009,
        auth_port: int = 8776,
        master_port: int = 27026,
        day_mode: str = "onlyday",
    ):
        self.game_dir, self.binary = find_nullrenderer(game_dir)
        self.port = port
        self.token = token or secrets.token_hex(32)
        self.time_scale = time_scale
        self.storage_root = (storage_root or (PROJECT_ROOT / "logs" / "headless_sandbox")).resolve()
        self.cluster_dir = self.storage_root / "DoNotStarveTogether" / "TestSandbox"
        self.master_dir = self.cluster_dir / "Master"
        self.log_file = self.master_dir / "server_log.txt"
        self.reset_world = reset_world
        self.verbose = verbose
        self.external_bridge = external_bridge
        self.transport = "http" if external_bridge else transport
        self.server_port = server_port
        self.auth_port = auth_port
        self.master_port = master_port
        self.day_mode = day_mode

        self.bridge: CompanionTeamBridge | None = None
        self.http_server = None
        self.proc: subprocess.Popen[str] | None = None
        self._stdin_lock = threading.Lock()
        self._stop_tail = threading.Event()
        self._tail_thread: threading.Thread | None = None
        self._events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self._lua_errors: list[str] = []
        self._session_ready = threading.Event()

    def _prepare_cluster(self) -> None:
        if self.reset_world and self.master_dir.exists():
            save_dir = self.master_dir / "save"
            if save_dir.exists():
                shutil.rmtree(save_dir)
        self.master_dir.mkdir(parents=True, exist_ok=True)
        (self.cluster_dir / "cluster.ini").write_text(
            "[GAMEPLAY]\n"
            "game_mode = survival\n"
            "max_players = 2\n"
            "pvp = false\n"
            "pause_when_empty = false\n"
            "\n"
            "[NETWORK]\n"
            "cluster_name = DST_Agent_Headless_Sandbox\n"
            "cluster_description = Automated headless test sandbox\n"
            "cluster_intention = cooperative\n"
            "offline_cluster = true\n"
            "lan_only_cluster = true\n"
            "tick_rate = 30\n"
            "\n"
            "[MISC]\n"
            "console_enabled = true\n"
            "\n"
            "[SHARD]\n"
            "shard_enabled = false\n",
            encoding="utf-8",
        )
        (self.cluster_dir / "cluster_token.txt").write_text(
            "pds-g^KU_HEADLESS^OFFLINE_TEST_TOKEN\n", encoding="utf-8"
        )
        (self.master_dir / "server.ini").write_text(
            "[NETWORK]\n"
            f"server_port = {self.server_port}\n"
            "\n"
            "[SHARD]\n"
            "is_master = true\n"
            "\n"
            "[STEAM]\n"
            f"authentication_port = {self.auth_port}\n"
            f"master_server_port = {self.master_port}\n"
            "\n"
            "[ACCOUNT]\n"
            "encode_user_path = true\n",
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
        chunks: dict[str, tuple[str, int, dict[int, str]]] = {}
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
                        if "LUA ERROR" in line or "[HEADLESS]ERROR" in line or "is not declared" in line or "stack traceback:" in line:
                            self._lua_errors.append(line)
                            print(f"[DST_ERR] {line}", flush=True)
                        marker_idx = line.find("[HEADLESS]")
                        if marker_idx >= 0:
                            rest = line[marker_idx + len("[HEADLESS]"):].rstrip("\r\n\t")
                            parts = rest.split(" ", 1)
                            tag = parts[0]
                            raw_payload = parts[1] if len(parts) > 1 else "{}"
                            if tag == "CHUNK":
                                # Format: <cid> <target_tag> <idx>/<total> <slice>
                                cparts = raw_payload.split(" ", 3)
                                if len(cparts) < 4:
                                    continue
                                cid, target_tag, frac, piece = cparts
                                idx_s, tot_s = frac.split("/", 1)
                                idx, tot = int(idx_s), int(tot_s)
                                entry = chunks.setdefault(cid, (target_tag, tot, {}))
                                entry[2][idx] = piece
                                if len(entry[2]) < tot:
                                    continue
                                tag = target_tag
                                raw_payload = "".join(entry[2][i] for i in range(1, tot + 1))
                                chunks.pop(cid, None)
                            payload: Any = raw_payload
                            if isinstance(payload, str):
                                try:
                                    payload = json.loads(payload)
                                except json.JSONDecodeError:
                                    pass
                            if tag == "HTTP_REQ" and isinstance(payload, dict) and self.bridge is not None:
                                req_id = payload.get("req_id")
                                req_body = payload.get("payload")
                                try:
                                    reply = self.bridge.exchange(req_body)
                                    reply_json = json.dumps(reply, ensure_ascii=False)
                                    self.send_lua(f"HEADLESS.http_reply({int(req_id)}, {json.dumps(reply_json, ensure_ascii=False)})")
                                except Exception as exc:
                                    self._lua_errors.append(f"bridge_exchange_error: {exc}")
                                    self.send_lua(f"HEADLESS.http_reply({int(req_id)}, nil)")
                                continue
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
        if not self.external_bridge:
            self.bridge = CompanionTeamBridge(self.token)
            if self.transport == "http":
                self.http_server = serve_bridge(
                    self.bridge, self.port, metrics_path=self.storage_root / "headless_transport.jsonl"
                )
                self.port = self.http_server.server_port

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
            cmd,
            cwd=str(self.binary.parent),
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
        )
        self._tail_thread = threading.Thread(target=self._tail_log, daemon=True)
        self._tail_thread.start()

        t0 = time.monotonic()
        if not self._session_ready.wait(timeout=boot_timeout):
            raise TimeoutError(f"DST nullrenderer failed to reach live session within {boot_timeout}s")
        boot_s = time.monotonic() - t0
        print(f"[SANDBOX] DST nullrenderer session ready in {boot_s:.1f}s", flush=True)

        harness_lua = (PROJECT_ROOT / "python" / "tools" / "headless_harness.lua").as_posix()
        buddy_scripts = (PROJECT_ROOT / "mods" / "dst_buddy_agent" / "scripts").as_posix()
        url = f"http://127.0.0.1:{self.port}/v1/exchange"
        self.send_lua(
            f'dofile("{harness_lua}"); '
            f'HEADLESS.init({{repo_buddy_scripts="{buddy_scripts}", url="{url}", '
            f'token="{self.token}", time_scale={self.time_scale}, transport="{self.transport}"}})'
        )
        ready = self.wait_event("READY", timeout=15.0)
        print(f"[SANDBOX] Harness READY at ({ready.get('x')}, {ready.get('z')}) scale={ready.get('time_scale')}x transport={self.transport}", flush=True)

        if not self.external_bridge and self.bridge:
            deadline = time.monotonic() + 15.0
            while time.monotonic() < deadline:
                try:
                    snap = self.bridge.observe()
                    if snap["state"].get("team") is True:
                        break
                except Exception:
                    pass
                time.sleep(0.1)
            else:
                raise TimeoutError("Headless harness did not connect to local CompanionTeamBridge")
            print(f"[SANDBOX] CompanionTeamBridge connected ({self.transport})", flush=True)
        return ready

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
        if self.http_server:
            self.http_server.shutdown()
            self.http_server.server_close()

    def __enter__(self) -> "HeadlessDSTSandbox":
        self.start()
        return self

    def __exit__(self, *args) -> None:
        self.stop()

    def setup_arena(self, spec: dict[str, Any], *, timeout: float = 15.0) -> dict[str, Any]:
        tag = spec.get("tag", f"arena_{int(time.time() * 1000)}")
        spec = {**spec, "tag": tag}
        encoded = json.dumps(spec, ensure_ascii=False)
        self.send_lua(f"HEADLESS.setup_arena({json.dumps(encoded, ensure_ascii=False)})")
        return self.wait_event("ARENA_READY", predicate=lambda p: isinstance(p, dict) and p.get("tag") == tag, timeout=timeout)

    def inspect(self, tag: str = "check", *, timeout: float = 10.0) -> dict[str, Any]:
        self.send_lua(f"HEADLESS.inspect({json.dumps(tag, ensure_ascii=False)})")
        return self.wait_event("INSPECT", predicate=lambda p: isinstance(p, dict) and p.get("tag") == tag, timeout=timeout)

    def test_pairing(self) -> dict[str, Any]:
        self.send_lua("HEADLESS.test_pairing()")
        return self.wait_event("PAIRING_TEST", timeout=10.0)

    def ensure_summoned(self, name: str = "小威") -> dict[str, Any]:
        if self.bridge:
            try:
                pkt = self.bridge.member_call("observe", "buddy1")
                comp = pkt["state"].get("companion") or {}
                if comp.get("bound") and comp.get("token") and pkt["state"].get("ready"):
                    return pkt
            except Exception:
                pass
        return self.summon(name)

    def summon(self, name: str = "小威", *, timeout: float = 20.0) -> dict[str, Any]:
        self.send_lua(f"HEADLESS.command({json.dumps('summon ' + name, ensure_ascii=False)})")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.bridge:
                try:
                    pkt = self.bridge.member_call("observe", "buddy1")
                    comp = pkt["state"].get("companion") or {}
                    if comp.get("bound") and comp.get("token") and pkt["state"].get("ready"):
                        return pkt
                except Exception:
                    pass
            time.sleep(0.15)
        raise TimeoutError(f"Timed out waiting for buddy '{name}' to summon and bind")

    def activate_instruction(self, text: str, *, buddy_name: str | None = None, member_id: str = "buddy1", timeout: float = 15.0) -> dict[str, Any]:
        assert self.bridge is not None
        if buddy_name is None:
            buddy_name = "自己" if member_id == "self" else "小威"
        before = self.bridge.member_call("observe", member_id)["state"]["control"]["revision"]
        self.send_lua(f"HEADLESS.command({json.dumps(f'do @{buddy_name} {text}', ensure_ascii=False)})")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            pkt = self.bridge.member_call("observe", member_id)
            ctrl = pkt["state"]["control"]
            if ctrl["revision"] > before and ctrl.get("enabled") and pkt["state"].get("ready"):
                return pkt
            time.sleep(0.1)
        raise TimeoutError(f"Timed out waiting for instruction revision > {before}")

    def wait_observed(self, predicate, *, member_id: str = "buddy1", timeout: float = 8.0) -> dict[str, Any]:
        assert self.bridge is not None
        deadline = time.monotonic() + timeout
        last_obs: dict[str, Any] = {}
        while time.monotonic() < deadline:
            try:
                obs = self.bridge.member_call("observe", member_id)["state"]["companion"]
                last_obs = obs
                if predicate(obs):
                    return obs
            except Exception:
                pass
            time.sleep(0.15)
        raise TimeoutError(f"Timed out waiting for observed world state predicate (keys={list(last_obs.keys())})")

    def run_plan_tasks(
        self,
        instruction: str,
        tasks_spec: list[dict[str, Any]],
        *,
        member_id: str = "buddy1",
        buddy_name: str | None = None,
        timeout: float = 75.0,
    ) -> dict[str, Any]:
        """Run our real CompanionTasks scheduler against the live headless DST engine."""
        assert self.bridge is not None
        pkt = self.activate_instruction(instruction, buddy_name=buddy_name, member_id=member_id)
        observed = pkt["state"]["companion"]
        plan = validate_plan(
            {"kind": "task_board", "goal": instruction, "reply": "收到", "tasks": tasks_spec},
            {"interaction": {"mode": "instruction"}, "observation": observed},
        )
        sim_clock = [float(observed.get("sampled_at") or 0.0)]
        scheduler = CompanionTasks(clock=lambda: sim_clock[0])
        scheduler.adopt(plan, observed)

        active: dict[str, Any] | None = None
        deadline = time.monotonic() + timeout
        last_reason = "init"
        while time.monotonic() < deadline:
            pkt = self.bridge.member_call("observe", member_id)
            state = pkt["state"]
            observed = state["companion"]
            obs_id = pkt["observation_id"]
            if observed.get("sampled_at") is not None:
                sim_clock[0] = max(sim_clock[0], float(observed["sampled_at"]))

            if active is not None:
                res = self.bridge.member_call("result", member_id, active["action_id"])
                status = res["status"]
                if status in {"completed", "failed", "interrupted"}:
                    reason = res.get("reason") or status
                    if self.verbose:
                        print(f"  [ACTION_DONE] {active['op']} -> {status} ({reason})", flush=True)
                    scheduler.on_result(active["task_id"], {**res, "op": active["op"]}, observed)
                    active = None
                    if status == "failed" and reason == "unsupported_action":
                        raise AssertionError(f"Lua rejected action as unsupported_action: {res}")
                else:
                    time.sleep(0.08)
                    continue

            if not state.get("ready"):
                time.sleep(0.08)
                continue

            view = scheduler.view(observed)
            if view["status"] == "completed" or all(row["status"] == "done" for row in view["tasks"]):
                return {"view": view, "observed": observed}

            task, op, args = scheduler.next_action(observed)
            if op == "completed":
                return {"view": view, "observed": observed}
            if op in {"waiting", "waiting_storage", "awaiting_items", "waiting_resource"}:
                last_reason = f"{op}:{(args or {}).get('reason', '')}"
                time.sleep(0.12)
                continue
            if op == "idle" and any(
                row.get("status") in {"maintaining", "scheduled"} or row.get("meal_started")
                for row in view["tasks"]
            ):
                last_reason = "idle:maintaining"
                time.sleep(0.15)
                continue
            if op in {"idle", "blocked", "unsupported", "needs_replan"}:
                raise AssertionError(
                    f"Task board stopped with op={op} args={args} view={view['tasks']}"
                )
            if self.verbose:
                print(f"  [ACTION_START] task={task['id']} op={op} args={args}", flush=True)
            try:
                rec = self.bridge.member_call("action", member_id, op, obs_id, args)
            except ValueError as exc:
                if "stale" in str(exc).lower() or "ready" in str(exc).lower():
                    time.sleep(0.12)
                    continue
                raise
            scheduler.note_action(task, op, args)
            active = {
                "action_id": rec["id"],
                "task_id": task["id"],
                "op": op,
                "args": args,
            }
            time.sleep(0.06)

        raise TimeoutError(
            f"Timed out after {timeout:.1f}s running tasks (last_reason={last_reason}, "
            f"active={active}, view={scheduler.view(observed)['tasks']})"
        )


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _find_building_id(observed: dict[str, Any], prefab: str) -> str | None:
    for row in (observed.get("places") or {}).get("buildings") or []:
        if row.get("prefab") == prefab and row.get("id"):
            return row["id"]
    for row in (observed.get("camp_work") or {}).get("fixtures") or []:
        if row.get("prefab") == prefab and row.get("building_id"):
            return row["building_id"]
    for row in (observed.get("storage") or {}).get("containers") or []:
        if row.get("prefab") == prefab and row.get("building_id"):
            return row["building_id"]
    return None


def _find_container_guid(observed: dict[str, Any], prefab: str) -> int | None:
    for row in observed.get("containers") or []:
        if row.get("prefab") == prefab and row.get("guid"):
            return int(row["guid"])
    for row in (observed.get("storage") or {}).get("containers") or []:
        if row.get("prefab") == prefab and row.get("guid"):
            return int(row["guid"])
    for row in (observed.get("camp_work") or {}).get("fixtures") or []:
        if row.get("prefab") == prefab and row.get("guid"):
            return int(row["guid"])
    return None


def _find_tree_guid(observed: dict[str, Any], prefabs: set[str]) -> int | None:
    for row in (observed.get("camp_work") or {}).get("trees") or []:
        if row.get("prefab") in prefabs and row.get("status") == "ignitable" and row.get("guid"):
            return int(row["guid"])
    return None


def run_suite(args: argparse.Namespace) -> int:
    scenarios: list[tuple[str, Any]] = []

    def scenario_pairing(sb: HeadlessDSTSandbox) -> str:
        res = sb.test_pairing()
        assert res.get("ok") is True, f"Lua pairing test failed: {res}"
        return "jev_bridge_pairing.lua passed 8/8 real Lua 5.1 engine checks"

    def scenario_collect_and_work(sb: HeadlessDSTSandbox) -> str:
        sb.setup_arena({
            "tag": "collect_work",
            "clear_owner_inventory": True,
            "spawns": [
                {"prefab": "grass", "dx": 4, "dz": 0},
                {"prefab": "sapling", "dx": 4, "dz": 3},
                {"prefab": "evergreen_normal", "dx": 6, "dz": -3},
                {"prefab": "rock1", "dx": -5, "dz": 3},
            ],
        })
        sb.summon("小威")
        sb.send_lua('HEADLESS.give_buddy("axe", 1); HEADLESS.give_buddy("pickaxe", 1)')
        time.sleep(0.5)
        sb.run_plan_tasks(
            "采草、采树枝、砍树、挖矿",
            [
                {"id": "t_grass", "title": "Pick grass", "priority": 1, "target": {"type": "inventory", "prefab": "cutgrass", "count": 1}},
                {"id": "t_twigs", "title": "Pick twigs", "priority": 2, "target": {"type": "inventory", "prefab": "twigs", "count": 1}},
                {"id": "t_log", "title": "Chop log", "priority": 3, "target": {"type": "inventory", "prefab": "log", "count": 1}},
                {"id": "t_rocks", "title": "Mine rocks", "priority": 4, "target": {"type": "inventory", "prefab": "rocks", "count": 1}},
            ],
            timeout=65.0,
        )
        truth = sb.inspect("after_collect_work")
        inv = truth["buddy"]["inventory"]
        for item in ("cutgrass", "twigs", "log", "rocks"):
            assert inv.get(item, 0) >= 1, f"Expected >=1 {item} in buddy inventory, got {inv}"
        return f"inventory={ {k: inv.get(k, 0) for k in ('cutgrass', 'twigs', 'log', 'rocks')} }"

    def scenario_ignite_tree_charcoal(sb: HeadlessDSTSandbox) -> str:
        sb.ensure_summoned("小威")
        sb.setup_arena({
            "tag": "ignite_charcoal",
            "reset_buddy": True,
            "spawns": [
                # >7.5 units from owner (fire safety rule), <=12 units from buddy
                {"prefab": "deciduoustree_normal", "dx": 10, "dz": 0},
            ],
            "give_buddy": [
                {"prefab": "torch", "count": 1},
                {"prefab": "axe", "count": 1},
            ],
        })
        obs = sb.wait_observed(lambda o: _find_tree_guid(o, {"deciduoustree"}) is not None)
        tree_guid = _find_tree_guid(obs, {"deciduoustree"})
        assert tree_guid is not None
        sb.run_plan_tasks(
            "烧桦树制炭",
            [
                {"id": "burn", "title": "Ignite birchnut tree", "priority": 1, "target": {"type": "ignite_tree", "tree_guid": tree_guid}},
                {"id": "charcoal", "title": "Chop burnt tree for charcoal", "priority": 2, "depends_on": ["burn"],
                 "target": {"type": "inventory", "prefab": "charcoal", "count": 1}},
            ],
            timeout=90.0,
        )
        truth = sb.inspect("after_charcoal")
        inv = _as_dict(truth["buddy"]["inventory"])
        assert inv.get("charcoal", 0) >= 1, f"Expected >=1 charcoal, got {inv}"
        assert truth["buddy"]["health"] > 80, f"Buddy took heavy fire damage: {truth['buddy']}"
        return f"charcoal={inv.get('charcoal')}, buddy_hp={truth['buddy']['health']}"

    def scenario_storage_and_give_owner(sb: HeadlessDSTSandbox) -> str:
        sb.ensure_summoned("小威")
        sb.setup_arena({
            "tag": "storage_batch",
            "reset_buddy": True,
            "clear_owner_inventory": True,
            "spawns": [
                {"prefab": "treasurechest", "dx": 5, "dz": 0},
            ],
            "give_buddy": [
                {"prefab": "cutgrass", "count": 10},
            ],
        })
        obs = sb.wait_observed(lambda o: _find_container_guid(o, "treasurechest") is not None)
        chest_guid = _find_container_guid(obs, "treasurechest")
        assert chest_guid is not None
        sb.run_plan_tasks(
            "存5根草再取3根再给房主2根",
            [
                {"id": "dep", "title": "Deposit 5 cutgrass", "priority": 1,
                 "target": {"type": "deposit", "prefab": "cutgrass", "count": 5, "container_guid": chest_guid}},
                {"id": "wd", "title": "Withdraw 3 cutgrass", "priority": 2, "depends_on": ["dep"],
                 "target": {"type": "withdraw", "prefab": "cutgrass", "count": 3, "container_guid": chest_guid}},
                {"id": "give", "title": "Give owner 2 cutgrass", "priority": 3, "depends_on": ["wd"],
                 "target": {"type": "give_owner", "prefab": "cutgrass", "count": 2}},
            ],
            timeout=60.0,
        )
        truth = sb.inspect("after_storage")
        chest_row = next(c for c in truth["chests"] if c["prefab"] == "treasurechest")
        chest_grass = _as_dict(chest_row.get("contents")).get("cutgrass", 0)
        buddy_grass = _as_dict(truth["buddy"]["inventory"]).get("cutgrass", 0)
        owner_grass = _as_dict(truth.get("owner_inventory")).get("cutgrass", 0)
        assert chest_grass == 2, f"Expected chest cutgrass=2, got {chest_grass}"
        assert buddy_grass == 6, f"Expected buddy cutgrass=6, got {buddy_grass}"
        assert owner_grass == 2, f"Expected owner cutgrass=2, got {owner_grass}"
        return f"chest={chest_grass}, buddy={buddy_grass}, owner={owner_grass}"

    def scenario_refuel_and_cook_meatballs(sb: HeadlessDSTSandbox) -> str:
        sb.ensure_summoned("小威")
        sb.setup_arena({
            "tag": "cook_refuel",
            "reset_buddy": True,
            "spawns": [
                {"prefab": "firepit", "dx": 5, "dz": -3, "fuel_percent": 0.25},
                {"prefab": "cookpot", "dx": 5, "dz": 3},
            ],
            "give_buddy": [
                {"prefab": "log", "count": 2},
                {"prefab": "berries", "count": 3},
                {"prefab": "meat", "count": 1},
            ],
        })
        obs = sb.wait_observed(lambda o: _find_building_id(o, "firepit") is not None and _find_building_id(o, "cookpot") is not None)
        firepit_id = _find_building_id(obs, "firepit")
        cookpot_id = _find_building_id(obs, "cookpot")
        assert firepit_id and cookpot_id
        before = sb.inspect("before_cook_refuel")
        before_fuel = next(b["fuel"] for b in before["buildings"] if b["prefab"] == "firepit")
        sb.run_plan_tasks(
            "给火坑添一根木头并做一份肉丸",
            [
                {"id": "fuel", "title": "Refuel firepit", "priority": 1,
                 "target": {"type": "refuel", "building_id": firepit_id, "fuel_prefab": "log", "count": 1}},
                {"id": "cook", "title": "Cook meatballs", "priority": 2, "depends_on": ["fuel"],
                 "target": {"type": "cook_meal", "building_id": cookpot_id, "product": "meatballs",
                            "ingredients": ["berries", "berries", "berries", "meat"]}},
            ],
            timeout=85.0,
        )
        after = sb.inspect("after_cook_refuel")
        after_fuel = next(b["fuel"] for b in after["buildings"] if b["prefab"] == "firepit")
        inv = _as_dict(after["buddy"]["inventory"])
        assert after_fuel > before_fuel, f"Expected firepit fuel to rise ({before_fuel} -> {after_fuel})"
        assert inv.get("meatballs", 0) >= 1, f"Expected >=1 meatballs in buddy inventory, got {inv}"
        return f"fuel {before_fuel:.2f}->{after_fuel:.2f}, meatballs={inv.get('meatballs')}"

    def scenario_set_home_and_stay_home(sb: HeadlessDSTSandbox) -> str:
        sb.ensure_summoned("小威")
        arena = sb.setup_arena({
            "tag": "home_stay",
            "reset_buddy": True,
            "spawns": [
                {"prefab": "firepit", "dx": 5, "dz": 0, "fuel_percent": 0.9},
            ],
        })
        spawned_fp = next(r for r in arena["spawned"] if r["prefab"] == "firepit")
        firepit_id = f"building:{spawned_fp['guid']}"
        sb.wait_observed(
            lambda o: any(r.get("id") == firepit_id for r in (o.get("places") or {}).get("buildings") or [])
        )
        sb.run_plan_tasks(
            "把火坑设为家",
            [
                {"id": "set_home", "title": "Set home at firepit", "priority": 1,
                 "target": {"type": "set_home", "anchor_id": firepit_id}},
            ],
            timeout=25.0,
        )
        obs2 = sb.wait_observed(lambda o: bool(((o.get("places") or {}).get("home") or {}).get("id")))
        home = (obs2.get("places") or {}).get("home") or {}
        home_id = home.get("id")
        assert home_id, f"Expected places.home.id after set_home, got {obs2.get('places')}"
        # Stay home for 0.03 game days (~14.4 game seconds, ~7s at 2x scale) with no idle stall!
        sb.run_plan_tasks(
            "在家驻留一会儿",
            [
                {"id": "stay", "title": "Stay home briefly", "priority": 1,
                 "duration": 0.03,
                 "target": {"type": "stay_home", "home_id": home_id, "radius": 10}},
            ],
            timeout=35.0,
        )
        return f"home_id={home_id}, stay_home 12s completed cleanly"

    def scenario_combat_equip_and_slay_spider(sb: HeadlessDSTSandbox) -> str:
        sb.ensure_summoned("小威")
        arena = sb.setup_arena({
            "tag": "combat_spider",
            "reset_buddy": True,
            "give_buddy": [
                {"prefab": "spear", "count": 1},
                {"prefab": "armorwood", "count": 1},
                {"prefab": "footballhat", "count": 1},
            ],
            "spawns": [
                {"prefab": "spider", "dx": 5, "dz": 0},
            ],
        })
        spider_guid = next(r["guid"] for r in arena["spawned"] if r["prefab"] == "spider")
        sb.wait_observed(
            lambda o: "combat" in (o.get("capabilities") or [])
            and any(e.get("guid") == spider_guid for e in (o.get("enemies") or []))
            and ((o.get("buddy") or {}).get("inventory") or {}).get("spear", 0) >= 1
        )
        sb.run_plan_tasks(
            "穿上护甲拿长矛清理蜘蛛",
            [
                {"id": "slay1", "title": "Slay spider", "priority": 1,
                 "target": {"type": "attack", "target_guid": spider_guid}},
            ],
            timeout=30.0,
        )
        obs_after = sb.wait_observed(lambda o: not any(e.get("guid") == spider_guid for e in (o.get("enemies") or [])))
        equipped = {r["prefab"] for r in (obs_after.get("equipment") or []) if r.get("equipped")}
        assert {"spear", "armorwood", "footballhat"} <= equipped, f"Expected armor+helmet+spear equipped, got {equipped}"
        after = sb.inspect("after_combat_spider")
        buddy_hp = float((after.get("buddy") or {}).get("health") or 0)
        assert buddy_hp > 80, f"Buddy took unexpected damage fighting 1 spider with armor: hp={buddy_hp}"
        return f"spider_guid={spider_guid} slain with {sorted(equipped)}, buddy_hp={buddy_hp:.1f}"

    def scenario_cook_perogies_and_honeyham(sb: HeadlessDSTSandbox) -> str:
        sb.ensure_summoned("小威")
        arena = sb.setup_arena({
            "tag": "cook_perogies",
            "reset_buddy": True,
            "give_buddy": [
                {"prefab": "bird_egg", "count": 1},
                {"prefab": "carrot", "count": 1},
                {"prefab": "berries", "count": 1},
                {"prefab": "meat", "count": 3},
                {"prefab": "honey", "count": 2},
            ],
            "spawns": [
                {"prefab": "cookpot", "dx": 5, "dz": 0},
            ],
        })
        cookpot_id = f"building:{next(r['guid'] for r in arena['spawned'] if r['prefab'] == 'cookpot')}"
        sb.wait_observed(
            lambda o: any(r.get("id") == cookpot_id for r in (o.get("places") or {}).get("buildings") or [])
            and ((o.get("buddy") or {}).get("inventory") or {}).get("bird_egg", 0) >= 1
            and ((o.get("buddy") or {}).get("inventory") or {}).get("honey", 0) >= 2
        )
        sb.run_plan_tasks(
            "用烹饪锅做一份饺子和一份蜜汁火腿",
            [
                {"id": "cook_perogies", "title": "Cook perogies", "priority": 1,
                 "target": {"type": "cook_meal", "building_id": cookpot_id, "product": "perogies",
                            "ingredients": ["bird_egg", "carrot", "berries", "meat"]}},
                {"id": "cook_honeyham", "title": "Cook honeyham", "priority": 2,
                 "depends_on": ["cook_perogies"],
                 "target": {"type": "cook_meal", "building_id": cookpot_id, "product": "honeyham",
                            "ingredients": ["meat", "meat", "honey", "honey"]}},
            ],
            timeout=90.0,
        )
        after = sb.inspect("after_cook_perogies")
        inv = _as_dict(after["buddy"]["inventory"])
        assert inv.get("perogies", 0) >= 1, f"Expected >=1 perogies in buddy inventory, got {inv}"
        assert inv.get("honeyham", 0) >= 1, f"Expected >=1 honeyham in buddy inventory, got {inv}"
        return f"perogies={inv.get('perogies')}, honeyham={inv.get('honeyham')} verified in real DST cookpot"

    def scenario_self_pilot_combat_and_latency(sb: HeadlessDSTSandbox) -> str:
        sb.send_lua("HEADLESS.set_simulation(15, 150)")
        sb.send_lua('HEADLESS.command("self on")')
        try:
            sb.wait_observed(lambda o: bool(o.get("bound") and o.get("token")), member_id="self", timeout=15.0)
            arena = sb.setup_arena({
                "tag": "self_pilot",
                "clear_owner_inventory": True,
                "reset_owner": True,
                "give_owner": [
                    {"prefab": "spear", "count": 1},
                    {"prefab": "armorwood", "count": 1},
                    {"prefab": "footballhat", "count": 1},
                ],
                "spawns": [
                    {"prefab": "grass", "dx": 4, "dz": 0},
                    {"prefab": "spider", "dx": 6, "dz": 0},
                ],
            })
            spider_guid = next(r["guid"] for r in arena["spawned"] if r["prefab"] == "spider")
            obs0 = sb.wait_observed(
                lambda o: o.get("is_self") is True
                and "combat" in (o.get("capabilities") or [])
                and o.get("simulation") == {"scan_radius": 15, "action_delay_ms": 150}
                and ((o.get("buddy") or {}).get("inventory") or {}).get("spear", 0) >= 1
                and any(e.get("guid") == spider_guid for e in (o.get("enemies") or []))
                and any(n.get("product") == "cutgrass" for n in (o.get("nearby") or [])),
                member_id="self",
                timeout=12.0,
            )
            caps = set(obs0.get("capabilities") or [])
            assert not (caps & {"follow", "give_owner", "receive_item", "timed_return"}), (
                f"Self slot should omit companion-only capabilities, got {sorted(caps)}"
            )
            sb.run_plan_tasks(
                "AI托管我自己：采一株草并穿甲拿长矛清理蜘蛛",
                [
                    {"id": "self_slay", "title": "Slay spider", "priority": 1,
                     "target": {"type": "attack", "target_guid": spider_guid}},
                    {"id": "self_grass", "title": "Pick grass", "priority": 2,
                     "target": {"type": "inventory", "prefab": "cutgrass", "count": 1}},
                ],
                member_id="self",
                timeout=35.0,
            )
            obs_after = sb.wait_observed(
                lambda o: not any(e.get("guid") == spider_guid for e in (o.get("enemies") or []))
                and ((o.get("buddy") or {}).get("inventory") or {}).get("cutgrass", 0) >= 1,
                member_id="self",
            )
            equipped = {r["prefab"] for r in (obs_after.get("equipment") or []) if r.get("equipped")}
            assert {"spear", "armorwood", "footballhat"} <= equipped, f"Expected self armor+helmet+spear equipped, got {equipped}"
            after = sb.inspect("after_self_pilot")
            owner = after.get("owner") or {}
            owner_hp = float(owner.get("health") or 0)
            assert owner_hp > 80, f"Self-piloted owner took unexpected damage: hp={owner_hp}"
            assert _as_dict(owner.get("inventory")).get("cutgrass", 0) >= 1
        finally:
            sb.send_lua('HEADLESS.command("self off")')
            sb.send_lua("HEADLESS.set_simulation(20, 0)")

        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline:
            members = (sb.bridge.observe()["state"].get("members") or {})
            if "self" not in members:
                break
            time.sleep(0.15)
        assert "self" not in (sb.bridge.observe()["state"].get("members") or {}), "Expected self slot removed after release"
        return f"self cutgrass>=1, spider_guid={spider_guid} slain @ 150ms delay / r=15, owner_hp={owner_hp:.1f}, clean release"

    scenarios = [
        ("1. pairing_lua_engine", scenario_pairing),
        ("2. summon_collect_and_work", scenario_collect_and_work),
        ("3. ignite_tree_charcoal", scenario_ignite_tree_charcoal),
        ("4. storage_batch_and_give_owner", scenario_storage_and_give_owner),
        ("5. refuel_and_cook_meatballs", scenario_refuel_and_cook_meatballs),
        ("6. set_home_and_stay_home", scenario_set_home_and_stay_home),
        ("7. combat_equip_and_slay_spider", scenario_combat_equip_and_slay_spider),
        ("8. cook_perogies_and_honeyham", scenario_cook_perogies_and_honeyham),
        ("9. self_pilot_combat_and_latency", scenario_self_pilot_combat_and_latency),
    ]
    if args.only:
        scenarios = [s for s in scenarios if args.only in s[0]]

    results: list[tuple[str, bool, float, str]] = []
    with HeadlessDSTSandbox(
        game_dir=args.game_dir,
        port=args.port,
        time_scale=args.time_scale,
        reset_world=args.reset_world,
        verbose=args.verbose,
        transport=args.transport,
    ) as sb:
        for name, fn in scenarios:
            t0 = time.monotonic()
            print(f"\n[RUN] {name} ...", flush=True)
            try:
                detail = fn(sb)
                elapsed = time.monotonic() - t0
                results.append((name, True, elapsed, detail))
                print(f"[PASS] {name} ({elapsed:.1f}s): {detail}", flush=True)
            except Exception as exc:
                elapsed = time.monotonic() - t0
                results.append((name, False, elapsed, f"{type(exc).__name__}: {exc}"))
                print(f"[FAIL] {name} ({elapsed:.1f}s): {type(exc).__name__}: {exc}", flush=True)

        if sb._lua_errors:
            print(f"\n[WARN] Captured {len(sb._lua_errors)} Lua error(s) during run.", flush=True)

    print("\n================ HEADLESS REAL-ENGINE E2E SUMMARY ================")
    passed = 0
    for name, ok, elapsed, detail in results:
        status = "PASS" if ok else "FAIL"
        if ok:
            passed += 1
        print(f"  [{status}] {name:35s} {elapsed:5.1f}s  {detail}")
    print(f"Total: {passed}/{len(results)} passed")
    return 0 if passed == len(results) else 1


def run_serve(args: argparse.Namespace) -> int:
    token, _ = ensure_bridge_token(args.config)
    print(f"[SERVE] Using bridge token from {args.config} -> http://127.0.0.1:{args.port}/v1/exchange", flush=True)
    with HeadlessDSTSandbox(
        game_dir=args.game_dir,
        port=args.port,
        token=token,
        time_scale=args.time_scale,
        reset_world=args.reset_world,
        verbose=True,
        external_bridge=True,
    ) as sb:
        sb.setup_arena({
            "tag": "interactive",
            "spawns": [
                {"prefab": "grass", "dx": 5, "dz": 0},
                {"prefab": "grass", "dx": 6, "dz": 2},
                {"prefab": "sapling", "dx": 5, "dz": -3},
                {"prefab": "evergreen_normal", "dx": 8, "dz": -4},
                {"prefab": "deciduoustree_normal", "dx": 9, "dz": 4},
                {"prefab": "rock1", "dx": -6, "dz": 3},
                {"prefab": "treasurechest", "dx": -4, "dz": -2},
                {"prefab": "cookpot", "dx": -4, "dz": 3},
                {"prefab": "firepit", "dx": -1, "dz": -4, "fuel_percent": 0.5},
            ],
        })
        sb.send_lua('HEADLESS.command("summon 小威"); HEADLESS.set_listening(true)')
        print("[SERVE] Headless DST world is LIVE! Now run companion_agent.py --execute in another terminal.", flush=True)
        print("[SERVE] Type chat messages here (e.g. '@小威 砍树' or '/buddy status'), or Ctrl+C to quit:", flush=True)
        try:
            for line in sys.stdin:
                text = line.strip()
                if not text:
                    continue
                if text.startswith("/buddy "):
                    sb.send_lua(f"HEADLESS.command({json.dumps(text[7:].strip())})")
                else:
                    sb.send_lua(f"HEADLESS.chat({json.dumps(text)})")
        except KeyboardInterrupt:
            pass
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", action="store_true", help="Run the automated real-engine E2E regression suite")
    parser.add_argument("--serve", action="store_true", help="Keep headless DST running for companion_agent.py")
    parser.add_argument("--only", help="Run only suite scenarios whose name contains this substring")
    parser.add_argument("--game-dir", type=Path, help="Path to Don't Starve Together installation")
    parser.add_argument("--config", type=Path, default=PROJECT_ROOT / "logs/companion_bridge.json")
    parser.add_argument("--port", type=int, default=0, help="Local bridge port (default: 18766 suite, 8766 serve)")
    parser.add_argument("--time-scale", type=float, default=2.0, help="Simulation speed multiplier (default: 2.0)")
    parser.add_argument("--transport", choices=("console", "http"), default="console",
                        help="Bridge transport for --suite (default: console, zero sockets)")
    parser.add_argument("--reset-world", action="store_true", help="Regenerate the sandbox world save from scratch")
    parser.add_argument("--verbose", action="store_true", help="Print live headless traces and actions")
    args = parser.parse_args()

    if not args.suite and not args.serve:
        args.suite = True
    if args.port == 0:
        args.port = 8766 if args.serve else 18766
    return run_serve(args) if args.serve else run_suite(args)


if __name__ == "__main__":
    raise SystemExit(main())
