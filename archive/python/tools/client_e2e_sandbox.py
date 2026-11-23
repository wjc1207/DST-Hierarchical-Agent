"""Small-window real DST client E2E sandbox connected to a local -offline nullrenderer server.

Tests the unmodified client-only mod (mods/dst_player_agent) end-to-end with REAL:
  - ThePlayer, playercontroller, replica.inventory, SendRPCToServer
  - TheSim:GetPersistentString Workshop pairing & live HTTP GameBridge
  - Real RakNet UDP client <-> server networking on 127.0.0.1:11009

Fully isolated from the user's real DST saves/settings via -persistent_storage_root
(800x450 muted window under logs/client_sandbox/).

Usage:
    python python/tools/client_e2e_sandbox.py --suite
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

ARCHIVE_PYTHON_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = ARCHIVE_PYTHON_ROOT.parents[1]
if str(ARCHIVE_PYTHON_ROOT) not in sys.path:
    sys.path.insert(0, str(ARCHIVE_PYTHON_ROOT))

from mcp_server.player_bridge import PlayerBridge as GameBridge, serve_bridge
from tools._nullrenderer import HeadlessDSTSandbox, find_nullrenderer

HARNESS_MOD_NAME = "jev_client_e2e_harness"

HARNESS_MODINFO = """\
name = "JEV Client E2E Harness (Temporary)"
description = "Temporary automated E2E auto-join helper. Safe to delete."
author = "Test Harness"
version = "0.1.0"
api_version = 10
dst_compatible = true
client_only_mod = true
all_clients_require_mod = false
"""

HARNESS_MODMAIN = """\
local G = GLOBAL
local json = require("json")
local cfg = require("jev_e2e_config")

-- Persist Workshop-style pairing credentials in this isolated client_save so
-- dst_player_agent exercises its real GetPersistentString loader.
if G.TheSim and G.TheSim.SetPersistentString and cfg.bridge_url and cfg.bridge_token then
    G.TheSim:SetPersistentString(
        "jev_player_bridge_v1",
        json.encode({enabled = true, url = cfg.bridge_url, token = cfg.bridge_token}),
        false
    )
    print("[CLIENT_E2E]PERSISTENT_PAIR_WRITTEN")
end

local function enforce_mute()
    if G.TheMixer then
        G.TheMixer:SetLevel("set_sfx", 0)
        G.TheMixer:SetLevel("set_music", 0)
        G.TheMixer:SetLevel("set_ambience", 0)
        G.TheMixer:SetLevel("master", 0)
    end
    if G.Profile and G.Profile.SetVolume then
        G.Profile:SetVolume(0, 0, 0)
    end
end
enforce_mute()

local joined = false
local spawned = false

AddClassPostConstruct("screens/redux/mainscreen", function(screen)
    enforce_mute()
    screen.inst:DoTaskInTime(0.6, function()
        enforce_mute()
        if not joined and screen.OnLoginButton then
            print("[CLIENT_E2E]AUTO_LOGIN_OFFLINE")
            screen:OnLoginButton(true)
        end
    end)
end)

AddClassPostConstruct("screens/redux/multiplayermainscreen", function(screen)
    screen.inst:DoTaskInTime(0.8, function()
        if not joined and G.TheNet and G.TheNet.StartClient then
            joined = true
            print("[CLIENT_E2E]START_CLIENT " .. tostring(cfg.ip) .. ":" .. tostring(cfg.port))
            G.TheNet:StartClient(cfg.ip, cfg.port, 0)
        end
    end)
end)

AddClassPostConstruct("screens/redux/lobbyscreen", function(screen)
    screen.inst:DoTaskInTime(0.8, function()
        if not spawned and screen.cb then
            spawned = true
            print("[CLIENT_E2E]AUTO_SPAWN_WILSON")
            screen.cb("wilson", "wilson_none", "", "", "", "")
        end
    end)
end)

AddPlayerPostInit(function(player)
    if not player then return end
    player:ListenForEvent("playeractivated", function()
        if player ~= G.ThePlayer then return end
        enforce_mute()
        local task
        task = player:DoPeriodicTask(0.25, function()
            if not player:IsValid() or player ~= G.ThePlayer then
                if task then task:Cancel() end
                return
            end
            local st = player._jev_autonomy
            if type(st) == "table" then
                st.mode = "autonomous"
                st.enabled = true
                st.paused = false
                st.revision = (st.revision or 0) + 1
                st.dialogue_revision = nil
                if st.on_change then st.on_change("autonomy_enabled") end
                print("[CLIENT_E2E]PLAYER_ACTIVE guid=" .. tostring(player.GUID) .. " autonomy=enabled")
                if task then task:Cancel() end
            end
        end)
    end)
end)
"""


def find_client_binary(game_dir: Path) -> Path:
    for rel in (
        Path("bin64/dontstarve_steam_x64.exe"),
        Path("bin/dontstarve_steam.exe"),
    ):
        candidate = game_dir / rel
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(f"Cannot locate dontstarve_steam executable under {game_dir}")


def sync_player_mod_to_game(game_dir: Path) -> None:
    """Copy workspace mods/dst_player_agent into <game_dir>/mods/dst_player_agent, preserving local config."""
    src = PROJECT_ROOT / "archive" / "mods" / "dst_player_agent"
    dst = game_dir / "mods" / "dst_player_agent"
    saved_cfg: bytes | None = None
    cfg_file = dst / "scripts" / "jev_bridge_config.lua"
    if cfg_file.is_file():
        saved_cfg = cfg_file.read_bytes()
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)
    if saved_cfg is not None:
        cfg_file.write_bytes(saved_cfg)


def install_temp_harness_mod(game_dir: Path, *, ip: str, server_port: int, bridge_port: int, token: str) -> Path:
    mod_dir = game_dir / "mods" / HARNESS_MOD_NAME
    if mod_dir.exists():
        shutil.rmtree(mod_dir)
    (mod_dir / "scripts").mkdir(parents=True, exist_ok=True)
    (mod_dir / "modinfo.lua").write_text(HARNESS_MODINFO, encoding="utf-8")
    (mod_dir / "modmain.lua").write_text(HARNESS_MODMAIN, encoding="utf-8")
    (mod_dir / "scripts" / "jev_e2e_config.lua").write_text(
        f'return {{ ip = {json.dumps(ip)}, port = {server_port}, '
        f'bridge_url = "http://127.0.0.1:{bridge_port}/v1/exchange", '
        f'bridge_token = {json.dumps(token)} }}\n',
        encoding="utf-8",
    )
    return mod_dir


def prepare_isolated_client_storage(client_root: Path) -> Path:
    conf = client_root / "DoNotStarveTogether"
    save = conf / "client_save"
    save.mkdir(parents=True, exist_ok=True)
    # DST reads client.ini (not settings.ini) for window geometry, graphics, and audio.
    (conf / "client.ini").write_text(
        "[MISC]\n"
        "texture_streaming = true\n"
        "use_threaded_renderer = true\n"
        "console_enabled = true\n"
        "autocompiler_enabled = true\n"
        "modswarning = false\n"
        "autologin_enabled = true\n"
        "\n"
        "[GRAPHICS]\n"
        "fullscreen = false\n"
        "window_x = 40\n"
        "window_y = 40\n"
        "windowed_width = 800\n"
        "windowed_height = 450\n"
        "netbook_mode = true\n"
        "smalltextures = true\n"
        "bloom = false\n"
        "distortion = false\n"
        "\n"
        "[audio]\n"
        "volume_ambient = 0\n"
        "volume_sfx = 0\n"
        "volume_music = 0\n"
        "volume_muteonfocuslost = true\n"
        "\n"
        "[ACCOUNT]\n"
        "encoded_user_cache = true\n",
        encoding="utf-8",
    )
    (save / "modindex").write_text(
        "return {\n"
        "    known_api_version = 10,\n"
        "    known_mods = {\n"
        '        ["dst_player_agent"] = { enabled = true },\n'
        f'        ["{HARNESS_MOD_NAME}"] = {{ enabled = true }},\n'
        "    },\n"
        "}\n",
        encoding="utf-8",
    )
    log_file = conf / "client_log.txt"
    if log_file.exists():
        log_file.unlink()
    return log_file


def inv_counts(state: dict[str, Any]) -> tuple[dict[str, int], dict[str, int]]:
    inv = (state.get("player") or {}).get("inventory") or {}
    items: dict[str, int] = {}
    equipped: dict[str, int] = {}
    for row in inv.get("items") or []:
        p = row.get("prefab")
        if p:
            items[p] = items.get(p, 0) + int(row.get("count") or 1)
    for row in inv.get("equipped") or []:
        p = row.get("prefab")
        if p:
            equipped[p] = equipped.get(p, 0) + int(row.get("count") or 1)
    return items, equipped


def wait_offered_action(
    bridge: GameBridge,
    predicate,
    *,
    timeout: float = 10.0,
    label: str = "action",
) -> tuple[str, dict[str, Any]]:
    deadline = time.monotonic() + timeout
    last_keys: list[str] = []
    while time.monotonic() < deadline:
        obs = bridge.get_available_actions(refresh=True)
        actions = obs.get("actions") or {}
        last_keys = sorted(actions.keys())
        for key, spec in actions.items():
            if predicate(key, spec):
                return key, obs
        time.sleep(0.25)
    raise RuntimeError(f"Expected {label} not offered within {timeout:.1f}s; offered={last_keys}")


def wait_action(bridge: GameBridge, choice: str, *, timeout: float = 25.0, offer_timeout: float = 8.0, **kwargs) -> dict[str, Any]:
    _, obs = wait_offered_action(bridge, lambda k, _: k == choice, timeout=offer_timeout, label=choice)
    rec = bridge.start_action(choice, obs["observation_id"], **kwargs)
    action_id = rec["action_id"]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = bridge.get_action_status(action_id)
        if status["status"] in {"completed", "failed", "interrupted", "cancelled"}:
            return status
        time.sleep(0.15)
    raise TimeoutError(f"Timed out waiting for client action {choice} ({action_id})")


def wait_matching_action(
    bridge: GameBridge,
    predicate,
    *,
    label: str,
    timeout: float = 25.0,
    offer_timeout: float = 10.0,
    **kwargs,
) -> tuple[str, dict[str, Any]]:
    choice, obs = wait_offered_action(bridge, predicate, timeout=offer_timeout, label=label)
    rec = bridge.start_action(choice, obs["observation_id"], **kwargs)
    action_id = rec["action_id"]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = bridge.get_action_status(action_id)
        if status["status"] in {"completed", "failed", "interrupted", "cancelled"}:
            return choice, status
        time.sleep(0.15)
    raise TimeoutError(f"Timed out waiting for client action {choice} ({action_id})")


def reset_client_arena(server: HeadlessDSTSandbox, *, tag: str, phase: str = "day", extra_lua: str = "") -> None:
    lua = (
        'local p; '
        'for _, cand in ipairs(AllPlayers) do '
        '  if cand ~= HEADLESS.owner then p = cand; break end '
        'end; '
        'if p then '
        '  TheWorld:PushEvent("ms_setclocksegs", {day = 8, dusk = 4, night = 4}); '
        f'  TheWorld:PushEvent("ms_setphase", {json.dumps(phase)}); '
        '  local ax, az = HEADLESS.ax, HEADLESS.az; '
        '  if HEADLESS.owner and HEADLESS.owner:IsValid() then '
        '    HEADLESS.owner.Transform:SetPosition(ax - 14, 0, az - 14); '
        '  end; '
        '  for _, e in ipairs(TheSim:FindEntities(ax, 0, az, 16, nil, {"INLIMBO"})) do '
        '    if e ~= p and e ~= HEADLESS.owner and not e:HasTag("player") '
        '      and e.prefab ~= "multiplayer_portal" and not e:HasTag("CLASSIFIED") then e:Remove() end '
        '  end; '
        '  local inv = p.components and p.components.inventory; '
        '  if inv then '
        '    for _, slot in pairs(EQUIPSLOTS or {}) do '
        '      local eq = inv:Unequip(slot); if eq and eq:IsValid() then eq:Remove() end '
        '    end; '
        '    for _, it in ipairs(inv:FindItems(function() return true end)) do '
        '      if it and it:IsValid() then inv:RemoveItem(it, true); it:Remove() end '
        '    end '
        '  end; '
        '  if p.components.health then p.components.health:SetPercent(1) end; '
        '  if p.components.hunger then p.components.hunger:SetPercent(0.8) end; '
        '  if p.components.sanity then p.components.sanity:SetPercent(1) end; '
        '  if p.components.temperature then p.components.temperature:SetTemperature(25) end; '
        '  p:ClearBufferedAction(); '
        '  if p.components.locomotor then p.components.locomotor:Stop() end; '
        '  if p.Physics then p.Physics:Teleport(ax, 0, az) else p.Transform:SetPosition(ax, 0, az) end; '
        '  local function give(prefab, n) '
        '    local rem = n or 1; '
        '    while rem > 0 do '
        '      local item = SpawnPrefab(prefab); if not item then break end; '
        '      if item.components and item.components.stackable then '
        '        local take = math.min(rem, item.components.stackable.maxsize or 20); '
        '        item.components.stackable:SetStackSize(take); rem = rem - take; '
        '      else rem = rem - 1 end; '
        '      inv:GiveItem(item); '
        '    end '
        '  end; '
        f'  {extra_lua} '
        f'  print("[HEADLESS]{tag} player=" .. tostring(p.GUID) .. " phase=" .. tostring(TheWorld.state.phase)); '
        'end'
    )
    server.send_lua(lua)
    server.wait_event(tag, timeout=10.0)
    time.sleep(1.5)


def run_client_suite(args: argparse.Namespace) -> int:
    game_dir, _ = find_nullrenderer(args.game_dir)
    client_bin = find_client_binary(game_dir)
    sandbox_root = (PROJECT_ROOT / "logs" / "client_sandbox").resolve()
    server_root = sandbox_root / "server"
    client_root = sandbox_root / "client"
    server_port = args.server_port
    token = secrets.token_hex(32)

    if args.sync_mod:
        print("[CLIENT_E2E] Syncing workspace mods/dst_player_agent -> game mods/ ...", flush=True)
        sync_player_mod_to_game(game_dir)

    sandbox_root.mkdir(parents=True, exist_ok=True)
    bridge = GameBridge(token)
    http_server = serve_bridge(bridge, args.bridge_port, metrics_path=sandbox_root / "player_transport.jsonl")
    bridge_port = http_server.server_port
    print(f"[CLIENT_E2E] Player GameBridge listening on 127.0.0.1:{bridge_port}", flush=True)

    # Diagnostic tap: if the real client ever hits HTTP 400, report exact redacted cause once.
    base_handler = http_server.RequestHandlerClass
    diag_logged = {"count": 0}

    class DiagnosticHandler(base_handler):
        def do_POST(self):
            orig_read = self.rfile.read
            captured: list[bytes] = []

            def tapped_read(n=-1):
                data = orig_read(n)
                captured.append(data)
                return data

            self.rfile.read = tapped_read
            orig_send_response = self.send_response
            resp_status = {"code": 0}

            def tapped_send_response(code, message=None):
                resp_status["code"] = code
                return orig_send_response(code, message)

            self.send_response = tapped_send_response
            try:
                return super().do_POST()
            finally:
                if resp_status["code"] >= 400 and diag_logged["count"] < 2:
                    diag_logged["count"] += 1
                    raw = b"".join(captured)
                    safe_text = raw.decode("utf-8", errors="replace").replace(token, "<REDACTED>")
                    try:
                        def _bad_const(val):
                            raise ValueError(f"Non-finite JSON constant: {val!r}")
                        parsed = json.loads(raw, parse_constant=_bad_const)
                        print(f"[CLIENT_E2E][DIAG] HTTP {resp_status['code']} after valid JSON ({len(raw)}B), keys={list(parsed.keys())[:10]}", flush=True)
                    except Exception as exc:
                        pos = getattr(exc, "pos", None)
                        snippet = safe_text[max(0, pos - 80): pos + 120] if isinstance(pos, int) else safe_text[:200]
                        print(f"[CLIENT_E2E][DIAG] HTTP {resp_status['code']} JSON parse error ({len(raw)}B): {exc}", flush=True)
                        print(f"[CLIENT_E2E][DIAG] Context around pos={pos}: {snippet!r}", flush=True)
                        (sandbox_root / "last_400_debug.txt").write_text(
                            f"status={resp_status['code']} len={len(raw)} err={exc}\npos={pos}\nsnippet={snippet!r}\n\n{safe_text[:4000]}\n",
                            encoding="utf-8",
                        )

    http_server.RequestHandlerClass = DiagnosticHandler

    harness_dir = install_temp_harness_mod(
        game_dir, ip="127.0.0.1", server_port=server_port, bridge_port=bridge_port, token=token
    )
    client_log = prepare_isolated_client_storage(client_root)
    client_proc: subprocess.Popen[str] | None = None
    stop_tail = threading.Event()
    client_errors: list[str] = []

    def tail_client() -> None:
        seen = 0
        while not stop_tail.is_set():
            if client_log.exists():
                try:
                    text = client_log.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    time.sleep(0.1)
                    continue
                if len(text) > seen:
                    chunk = text[seen:]
                    seen = len(text)
                    for line in chunk.splitlines():
                        if any(k in line for k in ("[CLIENT_E2E]", "[JEV_DST", "LUA ERROR", "is not declared", "CreateWindow:", "Small Textures:")):
                            print(f"[CLIENT_LOG] {line}", flush=True)
                        if "LUA ERROR" in line or "is not declared" in line:
                            client_errors.append(line)
            time.sleep(0.1)

    tail_thread = threading.Thread(target=tail_client, daemon=True)
    tail_thread.start()

    results: list[tuple[str, bool, float, str]] = []
    try:
        with HeadlessDSTSandbox(
            game_dir=game_dir,
            port=18766,
            time_scale=1.0,
            storage_root=server_root,
            reset_world=args.reset_world,
            server_port=server_port,
            auth_port=8777,
            master_port=27027,
            day_mode="default",
            transport="console",
            verbose=args.verbose,
        ) as server:
            print("[CLIENT_E2E] Launching 800x450 muted DST client (-offline) ...", flush=True)
            client_cmd = [
                str(client_bin),
                "-offline",
                "-persistent_storage_root", str(client_root),
                "-conf_dir", "DoNotStarveTogether",
            ]
            client_env = dict(os.environ)
            client_env["SteamAppId"] = "322330"
            client_env["SteamGameId"] = "322330"
            client_proc = subprocess.Popen(
                client_cmd,
                cwd=str(client_bin.parent),
                env=client_env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                text=True,
            )

            # Wait for real DST Player Agent to connect via persistent pairing + HTTP!
            t0 = time.monotonic()
            deadline = t0 + args.join_timeout
            connected_state: dict[str, Any] | None = None
            while time.monotonic() < deadline:
                if client_proc.poll() is not None:
                    raise RuntimeError(f"DST client exited early with code {client_proc.returncode}")
                try:
                    pkt = bridge.get_state()
                    st = pkt["state"]
                    if (st.get("player") or {}).get("guid") and (st.get("autonomy") or {}).get("enabled"):
                        connected_state = st
                        break
                except Exception:
                    pass
                time.sleep(0.25)

            if connected_state is None:
                raise TimeoutError(
                    f"Real DST client did not connect to Player GameBridge within {args.join_timeout}s. "
                    f"Check {client_log}"
                )
            join_s = time.monotonic() - t0
            results.append((
                "1. client_auto_join_and_persistent_pair",
                True,
                join_s,
                f"player_guid={connected_state['player']['guid']} day={connected_state['world'].get('day')} "
                f"actions={len((connected_state.get('bridge') or {}).get('available_actions') or {})}",
            ))
            print(f"[PASS] 1. client_auto_join_and_persistent_pair ({join_s:.1f}s)", flush=True)
            # Let the portal spawn-in animation settle before issuing the first movement RPC.
            time.sleep(2.5)

            def should_run(name: str) -> bool:
                return not args.only or args.only.lower() in name.lower()

            # Test 2: Real client-side collect (grass & sapling) via SendRPCToServer!
            if should_run("client_rpc_collect_grass_and_twigs"):
                t1 = time.monotonic()
                try:
                    reset_client_arena(
                        server,
                        tag="CLIENT_ARENA_COLLECT",
                        phase="day",
                        extra_lua=(
                            'local g = SpawnPrefab("grass"); g.Transform:SetPosition(ax + 3, 0, az); '
                            'local s = SpawnPrefab("sapling"); s.Transform:SetPosition(ax + 3, 0, az + 3);'
                        ),
                    )
                    r1 = wait_action(bridge, "collect_grass", timeout=20.0)
                    assert r1["status"] == "completed", f"collect_grass failed: {r1}"
                    r2 = wait_action(bridge, "collect_sapling", timeout=20.0)
                    assert r2["status"] == "completed", f"collect_sapling failed: {r2}"
                    st2 = bridge.get_state(refresh=True)["state"]
                    items, _ = inv_counts(st2)
                    assert items.get("cutgrass", 0) >= 1 and items.get("twigs", 0) >= 1, f"Unexpected client inv: {items}"
                    results.append(("2. client_rpc_collect_grass_and_twigs", True, time.monotonic() - t1, f"items={items}"))
                    print(f"[PASS] 2. client_rpc_collect_grass_and_twigs: {items}", flush=True)
                except Exception as exc:
                    results.append(("2. client_rpc_collect_grass_and_twigs", False, time.monotonic() - t1, str(exc)))
                    print(f"[FAIL] 2. client_rpc_collect_grass_and_twigs: {exc}", flush=True)

            # Test 3: Real client-side chop tree + replica inventory verification!
            if should_run("client_rpc_chop_nearest_tree"):
                t2 = time.monotonic()
                try:
                    reset_client_arena(
                        server,
                        tag="CLIENT_ARENA_CHOP",
                        phase="day",
                        extra_lua=(
                            'give("axe", 1); '
                            'local tr = SpawnPrefab("evergreen_short"); tr.Transform:SetPosition(ax + 5, 0, az - 2);'
                        ),
                    )
                    r3 = wait_action(bridge, "chop_nearest_tree", timeout=35.0)
                    assert r3["status"] == "completed", f"chop_nearest_tree failed: {r3}"
                    st3 = bridge.get_state(refresh=True)["state"]
                    items3, _ = inv_counts(st3)
                    assert items3.get("log", 0) >= 1, f"Expected log in client replica.inventory after chop: {items3}"
                    results.append(("3. client_rpc_chop_nearest_tree", True, time.monotonic() - t2, f"reason={r3.get('reason')} items={items3}"))
                    print(f"[PASS] 3. client_rpc_chop_nearest_tree: {r3.get('reason')} items={items3}", flush=True)
                except Exception as exc:
                    results.append(("3. client_rpc_chop_nearest_tree", False, time.monotonic() - t2, str(exc)))
                    print(f"[FAIL] 3. client_rpc_chop_nearest_tree: {exc}", flush=True)

            # Test 4: Real client-side craft_torch + night equip_torch + day unequip_torch!
            if should_run("client_rpc_craft_torch_and_night_light"):
                t3 = time.monotonic()
                try:
                    reset_client_arena(
                        server,
                        tag="CLIENT_ARENA_TORCH",
                        phase="day",
                        extra_lua='give("cutgrass", 2); give("twigs", 2);',
                    )
                    r_craft = wait_action(bridge, "craft_torch", timeout=20.0)
                    assert r_craft["status"] == "completed", f"craft_torch failed: {r_craft}"
                    st_crafted = bridge.get_state(refresh=True)["state"]
                    items_c, eq_c = inv_counts(st_crafted)
                    assert items_c.get("torch", 0) + eq_c.get("torch", 0) >= 1, f"No torch after craft: {items_c} / {eq_c}"

                    server.send_lua(
                        'TheWorld:PushEvent("ms_setclocksegs", {day = 8, dusk = 4, night = 4}); '
                        'TheWorld:PushEvent("ms_setphase", "night"); '
                        'print("[HEADLESS]PHASE_NIGHT_READY phase=" .. tostring(TheWorld.state.phase))'
                    )
                    server.wait_event("PHASE_NIGHT_READY", timeout=10.0)
                    r_eq = wait_action(bridge, "equip_torch", timeout=15.0)
                    assert r_eq["status"] == "completed", f"equip_torch failed: {r_eq}"
                    st_eq = bridge.get_state(refresh=True)["state"]
                    _, eq_n = inv_counts(st_eq)
                    assert eq_n.get("torch", 0) >= 1, f"Torch not equipped at night: {eq_n}"

                    server.send_lua(
                        'TheWorld:PushEvent("ms_setphase", "day"); '
                        'print("[HEADLESS]PHASE_DAY_READY phase=" .. tostring(TheWorld.state.phase))'
                    )
                    server.wait_event("PHASE_DAY_READY", timeout=10.0)
                    # dst_player_agent automatically unequips a lit torch when daytime returns;
                    # verify either automatic unequip or explicit unequip_torch RPC.
                    deadline_u = time.monotonic() + 8.0
                    unequipped_mode = "auto_day_unequip"
                    items_u: dict[str, int] = {}
                    eq_u: dict[str, int] = {}
                    while time.monotonic() < deadline_u:
                        obs_u = bridge.get_available_actions(refresh=True)
                        items_u, eq_u = inv_counts(obs_u["state"])
                        if eq_u.get("torch", 0) == 0 and items_u.get("torch", 0) >= 1:
                            break
                        if "unequip_torch" in (obs_u.get("actions") or {}):
                            r_uneq = wait_action(bridge, "unequip_torch", timeout=15.0)
                            assert r_uneq["status"] == "completed", f"unequip_torch failed: {r_uneq}"
                            unequipped_mode = "explicit_rpc_unequip"
                            st_uneq = bridge.get_state(refresh=True)["state"]
                            items_u, eq_u = inv_counts(st_uneq)
                            break
                        time.sleep(0.25)
                    assert eq_u.get("torch", 0) == 0 and items_u.get("torch", 0) >= 1, f"Torch not unequipped: {items_u} / {eq_u}"
                    results.append(("4. client_rpc_craft_torch_and_night_light", True, time.monotonic() - t3, f"craft->equip(night)->{unequipped_mode}"))
                    print(f"[PASS] 4. client_rpc_craft_torch_and_night_light: craft->equip(night)->{unequipped_mode}", flush=True)
                except Exception as exc:
                    results.append(("4. client_rpc_craft_torch_and_night_light", False, time.monotonic() - t3, str(exc)))
                    print(f"[FAIL] 4. client_rpc_craft_torch_and_night_light: {exc}", flush=True)

            # Test 5: Real client-side build_campfire (MakeRecipeAtPoint) + cook_food + eat_safe_food!
            if should_run("client_rpc_build_campfire_cook_and_eat"):
                t4 = time.monotonic()
                try:
                    reset_client_arena(
                        server,
                        tag="CLIENT_ARENA_CAMPFIRE",
                        phase="night",
                        extra_lua=(
                            'give("cutgrass", 3); give("log", 2); give("berries", 2); '
                            'p.components.hunger:SetPercent(0.5);'
                        ),
                    )
                    r_fire = wait_action(bridge, "build_campfire", timeout=25.0)
                    assert r_fire["status"] == "completed", f"build_campfire failed: {r_fire}"
                    r_cook = wait_action(bridge, "cook_food", timeout=20.0)
                    assert r_cook["status"] == "completed", f"cook_food failed: {r_cook}"
                    st_cooked = bridge.get_state(refresh=True)["state"]
                    items_ck, _ = inv_counts(st_cooked)
                    assert items_ck.get("berries_cooked", 0) >= 1, f"Expected berries_cooked after cook_food: {items_ck}"
                    hunger_before = float((st_cooked["player"].get("vitals") or {}).get("hunger") or 0)
                    r_eat = wait_action(bridge, "eat_safe_food", timeout=20.0)
                    assert r_eat["status"] == "completed", f"eat_safe_food failed: {r_eat}"
                    st_ate = bridge.get_state(refresh=True)["state"]
                    items_ate, _ = inv_counts(st_ate)
                    hunger_after = float((st_ate["player"].get("vitals") or {}).get("hunger") or 0)
                    assert items_ate.get("berries_cooked", 0) < items_ck["berries_cooked"] or hunger_after > hunger_before, (
                        f"eat_safe_food did not consume/restore: before={hunger_before} after={hunger_after} items={items_ate}"
                    )
                    results.append((
                        "5. client_rpc_build_campfire_cook_and_eat",
                        True,
                        time.monotonic() - t4,
                        f"hunger {hunger_before:.1f}->{hunger_after:.1f}",
                    ))
                    print(f"[PASS] 5. client_rpc_build_campfire_cook_and_eat: hunger {hunger_before:.1f}->{hunger_after:.1f}", flush=True)
                except Exception as exc:
                    results.append(("5. client_rpc_build_campfire_cook_and_eat", False, time.monotonic() - t4, str(exc)))
                    print(f"[FAIL] 5. client_rpc_build_campfire_cook_and_eat: {exc}", flush=True)

            # Test 6: Real client-side visit_building + set_home + open_container + store_item + take_item!
            if should_run("client_rpc_container_store_take_and_set_home"):
                t5 = time.monotonic()
                try:
                    reset_client_arena(
                        server,
                        tag="CLIENT_ARENA_CHEST",
                        phase="day",
                        extra_lua=(
                            'give("flint", 4); '
                            'local ch = SpawnPrefab("treasurechest"); ch.Transform:SetPosition(ax - 2.5, 0, az);'
                        ),
                    )
                    # Visit the remembered chest building first if not yet arrived at its home anchor.
                    visit_or_home_key, obs_v = wait_offered_action(
                        bridge,
                        lambda k, a: a.get("kind") == "set_home" or k.startswith("visit_building:"),
                        label="visit_building or set_home",
                        timeout=10.0,
                    )
                    if visit_or_home_key.startswith("visit_building:"):
                        r_visit = wait_action(bridge, visit_or_home_key, timeout=20.0)
                        assert r_visit["status"] == "completed", f"{visit_or_home_key} failed: {r_visit}"

                    home_key, r_home = wait_matching_action(
                        bridge,
                        lambda _, a: a.get("kind") == "set_home",
                        label="set_home",
                        timeout=15.0,
                    )
                    assert r_home["status"] == "completed", f"{home_key} failed: {r_home}"
                    st_home = bridge.get_state(refresh=True)["state"]
                    assert (st_home.get("places") or {}).get("home"), f"Home not recorded in places: {st_home.get('places')}"

                    open_key, r_open = wait_matching_action(
                        bridge,
                        lambda _, a: a.get("kind") == "open_container" and a.get("prefab") == "treasurechest",
                        label="open_container(treasurechest)",
                        timeout=20.0,
                    )
                    assert r_open["status"] == "completed", f"{open_key} failed: {r_open}"

                    store_key, r_store = wait_matching_action(
                        bridge,
                        lambda _, a: a.get("kind") == "store_item" and a.get("product") == "flint",
                        label="store_item(flint)",
                        timeout=20.0,
                        transfer_count=2,
                    )
                    assert r_store["status"] == "completed", f"{store_key} failed: {r_store}"
                    st_stored = bridge.get_state(refresh=True)["state"]
                    items_st, _ = inv_counts(st_stored)
                    assert items_st.get("flint", 0) == 2, f"Expected 2 flint left after storing 2: {items_st}"

                    take_key, r_take = wait_matching_action(
                        bridge,
                        lambda _, a: a.get("kind") == "take_item" and a.get("product") == "flint",
                        label="take_item(flint)",
                        timeout=20.0,
                        transfer_count=1,
                    )
                    assert r_take["status"] == "completed", f"{take_key} failed: {r_take}"
                    st_taken = bridge.get_state(refresh=True)["state"]
                    items_tk, _ = inv_counts(st_taken)
                    assert items_tk.get("flint", 0) == 3, f"Expected 3 flint after taking 1 back: {items_tk}"
                    results.append((
                        "6. client_rpc_container_store_take_and_set_home",
                        True,
                        time.monotonic() - t5,
                        "visit + set_home + open + store(2) + take(1) -> flint=3",
                    ))
                    print("[PASS] 6. client_rpc_container_store_take_and_set_home: visit + set_home + open + store(2) + take(1) -> flint=3", flush=True)
                except Exception as exc:
                    results.append(("6. client_rpc_container_store_take_and_set_home", False, time.monotonic() - t5, str(exc)))
                    print(f"[FAIL] 6. client_rpc_container_store_take_and_set_home: {exc}", flush=True)

    finally:
        if client_proc and client_proc.poll() is None:
            client_proc.terminate()
            try:
                client_proc.wait(timeout=6)
            except subprocess.TimeoutExpired:
                client_proc.kill()
        stop_tail.set()
        tail_thread.join(timeout=2)
        http_server.shutdown()
        http_server.server_close()
        if harness_dir.exists():
            shutil.rmtree(harness_dir, ignore_errors=True)

    print("\n================ REAL CLIENT E2E SUMMARY ================")
    passed = sum(1 for _, ok, _, _ in results if ok)
    for name, ok, elapsed, detail in results:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name:48s} {elapsed:5.1f}s  {detail}")
    if client_errors:
        print(f"  [WARN] Captured {len(client_errors)} client Lua error(s).")
    print(f"Total: {passed}/{len(results)} passed")
    return 0 if passed == len(results) and len(results) > 0 and not client_errors else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", action="store_true", help="Run the automated small-window real-client E2E suite")
    parser.add_argument("--game-dir", type=Path, help="Path to Don't Starve Together installation")
    parser.add_argument("--bridge-port", type=int, default=18765, help="Local Player GameBridge port (default: 18765)")
    parser.add_argument("--server-port", type=int, default=11010, help="Isolated local DST server UDP port (default: 11010)")
    parser.add_argument("--join-timeout", type=float, default=120.0, help="Seconds to wait for client auto-join")
    parser.add_argument("--reset-world", action="store_true", help="Regenerate the isolated client-sandbox server world")
    parser.add_argument("--only", type=str, help="Run only scenario(s) matching this substring")
    parser.add_argument("--no-sync-mod", dest="sync_mod", action="store_false", help="Do not sync workspace dst_player_agent")
    parser.add_argument("--verbose", action="store_true", help="Verbose server logs")
    args = parser.parse_args()
    return run_client_suite(args)


if __name__ == "__main__":
    raise SystemExit(main())
