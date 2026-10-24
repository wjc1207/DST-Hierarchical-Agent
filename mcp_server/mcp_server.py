"""STDIO MCP game service with an authenticated loopback-only Lua bridge."""
from __future__ import annotations

import argparse
import asyncio
import json
import secrets
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if __package__ in {None, ""}:
    sys.path.insert(0, str(PROJECT_ROOT))

from mcp_server.game_bridge import GameBridge, serve_bridge, check_bridge_port, BridgePortUnavailable


def create_server(bridge):
    from mcp.server import MCPServer
    from mcp.server.mcpserver.exceptions import ToolError
    def invoke(function, *args):
        try:
            return function(*args)
        except ValueError as exc:
            raise ToolError(str(exc)) from None
    server = MCPServer("decisionLimitedDST", version="1.8.8", instructions=(
        "Read get_state and get_available_actions before acting. Pass its observation_id. "
        "Queued/running is NOT completed. Poll get_action_status. One action owns the executor. "
        "Submit the original observation_id/choice that informed your decision; do not retarget silently. "
        "Lua validates live feasibility and reports rejection reasons; Python checks ownership and expiry. "
        "Cached snapshots expose their age; use refresh=true to request new Lua facts when needed. "
        "Heartbeats only freshen the connection/control status, not world facts. "
        "Task boards auto-execute only when explicitly requested; unsupported navigation/prerequisites yield."))

    @server.tool()
    async def get_state(refresh: bool = False) -> dict[str, Any]:
        """Get the latest event snapshot with its age; refresh=true requests new Lua facts.

        Refresh waits at most three seconds, without blocking other MCP calls.
        Disconnected games error; heartbeats do not reset snapshot age.
        """
        return await asyncio.to_thread(invoke, bridge.get_state, True) if refresh else invoke(bridge.get_state)

    @server.tool()
    async def get_available_actions(refresh: bool = False) -> dict[str, Any]:
        """Get cached Lua options, snapshot age and an observation ID; refresh=true requests a new snapshot."""
        return await asyncio.to_thread(invoke, bridge.get_available_actions, True) if refresh else invoke(bridge.get_available_actions)

    @server.tool()
    def start_action(choice: str, observation_id: str) -> dict[str, Any]:
        """Queue one offered action. Returns an action ID, not completion. No arbitrary code or coordinates."""
        return invoke(bridge.start_action, choice, observation_id)

    @server.tool()
    def get_action_status(action_id: str) -> dict[str, Any]:
        """Read queued/running/cancel_requested/completed/failed/interrupted/cancelled and connection status."""
        return invoke(bridge.get_action_status, action_id)

    @server.tool()
    def cancel_action(action_id: str) -> dict[str, Any]:
        """Request a stop. Cancellation is confirmed only after Lua acknowledges it."""
        return invoke(bridge.cancel_action, action_id)

    @server.tool()
    def submit_task_board(board: dict, observation_id: str, auto_execute: bool = False) -> dict[str, Any]:
        """Replace the task board; optionally allow Lua to run supported visible local tasks.

        Use auto_execute=false to pause a board. Missing sources, danger and unsupported
        prerequisites yield to the host; this is not a full automatic acquisition solver.
        """
        return invoke(bridge.submit_task_board, board, observation_id, auto_execute)

    return server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=PROJECT_ROOT / "logs" / "mcp_bridge.json")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--write-mod-config", type=Path,
                        help="Explicit output path for the installed Mod's scripts/jev_bridge_config.lua")
    parser.add_argument("--bridge-only", action="store_true", help="Connectivity testing without an MCP host")
    parser.add_argument("--configure-only", action="store_true", help="Write configuration and exit, without serving")
    args = parser.parse_args()
    if not args.configure_only:
        try:
            check_bridge_port(args.port)
        except BridgePortUnavailable as exc:
            print(f"mcp_startup_error {exc}", file=sys.stderr)
            return 2
    if args.config.exists():
        config = json.loads(args.config.read_text(encoding="utf8"))
        token = config["token"]
    else:
        token = secrets.token_hex(32)
        args.config.parent.mkdir(parents=True, exist_ok=True)
        args.config.write_text(json.dumps({"token": token}), encoding="utf8")
    if args.write_mod_config:
        # Only ASCII literals are written; never load model API credentials into the Mod.
        args.write_mod_config.parent.mkdir(parents=True, exist_ok=True)
        args.write_mod_config.write_text(
            'return { enabled = true, url = "http://127.0.0.1:' + str(args.port)
            + '/v1/exchange", token = ' + json.dumps(token) + ' }\n', encoding="utf8")
        print("Mod bridge configuration written; re-enter the world to enable it.", file=sys.stderr)
    if args.configure_only:
        return
    bridge = GameBridge(token)
    try:
        mcp = None if args.bridge_only else create_server(bridge)
    except ModuleNotFoundError:
        parser.error("MCP SDK missing. Install requirements-mcp.txt using this same Python interpreter.")
    try:
        server = serve_bridge(bridge, args.port, metrics_path=args.config.parent / "mcp_transport.jsonl")
    except BridgePortUnavailable as exc:
        print(f"mcp_startup_error {exc}", file=sys.stderr)
        return 2
    print(f"DST Lua bridge listening on 127.0.0.1:{server.server_port}; no model APIs or keyboard input.", file=sys.stderr)
    print(f"HTTP timing log: {args.config.parent / 'mcp_transport.jsonl'}", file=sys.stderr)
    try:
        if mcp:
            mcp.run(transport="stdio")
        else:
            import threading
            threading.Event().wait()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    raise SystemExit(main())
