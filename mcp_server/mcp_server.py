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
    def invoke(function, *args, **kwargs):
        try:
            return function(*args, **kwargs)
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
    async def query_recipes(query: str = "", recipe_id: str | None = None,
                            offset: int = 0, limit: int = 12, refresh: bool = False) -> dict[str, Any]:
        """Read/search live recipe facts; bounded pages, no model call or game action."""
        from common.recipes import query_recipes as search
        packet = await asyncio.to_thread(invoke, bridge.get_state, True) if refresh else invoke(bridge.get_state)
        return {"session_id": packet["session_id"], "age_seconds": packet["age_seconds"],
                "index": packet["state"].get("recipe_index"),
                **invoke(search, packet["state"].get("recipes", {}), query, recipe_id, offset, limit,
                         epoch=packet["state"].get("recipe_epoch"))}

    @server.tool()
    async def query_places(query: str = "", building_id: str | None = None,
                           offset: int = 0, limit: int = 12, refresh: bool = False) -> dict[str, Any]:
        """Read observed building memory, current home and legal home anchors; no game action."""
        from common.places import query_places as search
        packet = await asyncio.to_thread(invoke, bridge.get_state, True) if refresh else invoke(bridge.get_state)
        return {"session_id": packet["session_id"], "age_seconds": packet["age_seconds"],
                **invoke(search, packet["state"].get("places", {}), query, building_id, offset, limit)}

    @server.tool()
    async def query_storage(building_id: str | None = None, refresh: bool = False) -> dict[str, Any]:
        """Read observed chest and Crock Pot states; closed contents are unknown. Never opens containers."""
        from common.camp_work import query_storage as search
        packet = await asyncio.to_thread(invoke, bridge.get_state, True) if refresh else invoke(bridge.get_state)
        return {"session_id": packet["session_id"], "age_seconds": packet["age_seconds"],
                **invoke(search, packet["state"].get("camp_work") or {}, building_id)}

    @server.tool()
    async def query_meals(product: str | None = None, refresh: bool = False) -> dict[str, Any]:
        """Read supported Crock Pot ingredient combinations; never consumes ingredients."""
        from common.camp_work import query_meals as search
        packet = await asyncio.to_thread(invoke, bridge.get_state, True) if refresh else invoke(bridge.get_state)
        return {"session_id": packet["session_id"], "age_seconds": packet["age_seconds"],
                **invoke(search, packet["state"].get("camp_work") or {}, product)}

    @server.tool()
    def send_agent_feedback(text: str, revision: int, message_id: str, finish: bool = False) -> dict[str, Any]:
        """Queue one DeepSeek reply through native player chat. finish stops instruction mode only."""
        return invoke(bridge.send_agent_feedback, text, revision, message_id, finish)

    @server.tool()
    def start_action(choice: str, observation_id: str,
                     navigation_targets: list[dict[str, Any]] | None = None,
                     residence: dict[str, Any] | None = None,
                     destination: dict[str, Any] | None = None,
                     placement: dict[str, Any] | None = None) -> dict[str, Any]:
        """Queue one offered action; returns an ID, not completion. No arbitrary code.

        For navigation only, navigation_targets watches up to six local inventory,
        structure or hunger goals. Only an offered action advancing one may yield
        for resources. [] ignores ordinary resources; omission keeps resource-set
        events for planning gaps. Safety/region/path interruptions always apply.
        residence={home_id,radius} restricts ordinary work to the current observed
        home area, with native checks; no caller-supplied coordinates are accepted.
        For choice=move_to only, destination={x,z,arrival_radius?} selects observed
        passable land; equivalent to the move_to tool. Lua resolves the actual route.
        placement={anchor_id:building:<GUID>,radius:4..20(default10)} bounds building
        actions to the observed anchor; travel there first. Lua selects a legal
        point inside this area and verifies it again before actual placement.
        """
        return invoke(bridge.start_action, choice, observation_id, navigation_targets, residence, destination, placement)

    @server.tool()
    def move_to(x: float, z: float, observation_id: str, arrival_radius: float = 2) -> dict[str, Any]:
        """Travel to observed land in world x/z coordinates; Lua validates the live safe route.

        Returns an action ID, not proof of arrival. Does not set home, teleport,
        explore unknown coordinates or override lighting/safety. Cancel normally.
        """
        return invoke(bridge.start_action,"move_to",observation_id,[],None,
                      {"x":x,"z":z,"arrival_radius":arrival_radius})

    @server.tool()
    def add_fuel(building_id: str, fuel_prefab: str, observation_id: str) -> dict[str, Any]:
        """Add ONE specified common fuel item to a nearby observed campfire/firepit.

        Use an offered add_fuel:<GUID>:<fuel> action. Native legality, exact target,
        fuel item, safe proximity and actual effect are rechecked; supports wet fuel
        and relighting an extinguished firepit. Returns an action ID, not completion.
        """
        from common.refueling import validate_target
        target = invoke(validate_target, {"building_id":building_id,"fuel_prefab":fuel_prefab})
        return invoke(bridge.start_action, "add_fuel:" + target["building_id"].split(":")[1] + ":" + target["fuel_prefab"], observation_id)

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
        Tasks accept focus=true/false (default false), at most two focused tasks.
        Unfinished focused tasks and their prerequisites exclude ordinary work.
        Resubmit a task with focus changed to promote or demote it.
        survey_region targets use cells:1..256 (default16), meaning ADDITIONAL
        observed map grid cells, not walking steps or world area. Legacy area
        parameters are converted by /16 and normalized to cells.
        All tasks may have start_at:{day,progress|phase}. stay_home targets require
        exactly one task-level duration (game days) or end_at. Timing errors are
        returned per task in task_errors. Timed/residence execution requires the
        Python priority scheduler; auto_execute=true is unsupported for these tasks.
        refuel targets {building_id,fuel_prefab,count:1..10} also require Python;
        each verified native addition advances progress by one.
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
