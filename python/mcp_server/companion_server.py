"""Local companion MCP service, separate from the original player controller."""
from __future__ import annotations
import argparse
import asyncio
import json
import secrets
import sys
import threading
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.paths import PROJECT_ROOT
from mcp_server.task_service import TaskService
from mcp_server.companion_team_bridge import CompanionTeamBridge
from mcp_server.game_bridge import serve_bridge


def create_server(bridge, *, poll=.2, timing_path=None):
    tasks = TaskService(bridge, companion=True, poll=poll, timing_path=timing_path)
    from mcp.server import MCPServer
    from mcp.server.mcpserver.exceptions import ToolError
    server = MCPServer("DSTCompanion", version="0.7.0", lifespan=tasks.lifespan, instructions=(
        "Companion tools control up to two named bound Wilsons, not your human player. Observe first. Without buddy_id, buddy_observe returns the team; specify buddy_id for other tools when two members exist. Use buddy_reply for each companion's own first-person speech. A member finish ends only that contribution. "
        "Use only offered collection target GUIDs. Poll buddy_result: queued/running are not success. "
        "follow completes when follow mode is set, not when arrival occurs. Read owner_distance for arrival. "
        "Use buddy_recipes for current server recipes. Native work, equip/unequip, crafting and land placement are supported when advertised. "
        "unequip requires a currently equipped item GUID and stows it without dropping overflow; can_unequip reports capacity/restrictions. "
        "When search is advertised, perception offers local terrain, hazards and validated short search points. "
        "Search accepts only offered points; resource_found is not inventory completion. "
        "Use buddy_storage for nearby observed chest contents. give_owner, drop_item, deposit and withdraw move one unit per action; "
        "task_key and total_count preserve a cumulative receipt across retries. Never take items from owner inventory. "
        "Use buddy_places for buildings actually seen by the companion. visit_building={building_id} returns over long distances; "
        "unconfirmed means outside local observation, not missing. Ordinary move still has a 20-unit limit. "
        "Players can give the companion items using native give. Combat, cooking and a full-world region graph are not yet supported."))
    def invoke(fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except ValueError as exc:
            raise ToolError(str(exc)) from None

    def member(method,buddy_id,*args,**kwargs):
        return invoke(bridge.member_call,method,buddy_id,*args,**kwargs)

    @server.tool()
    def buddy_observe(buddy_id: str | None = None) -> dict[str, Any]:
        """Read the owner, companion, offered resources, local perception, intent and a short-lived observation ID."""
        return invoke(bridge.observe) if buddy_id is None else member("observe",buddy_id)

    @server.tool()
    def buddy_action(op: str, observation_id: str, args: dict[str, Any] | None = None, buddy_id: str | None = None) -> dict[str, Any]:
        """Queue a native action. visit_building={building_id} accepts remembered buildings only, with up to 300 seconds of travel. work={target_guid,work:CHOP|MINE}, equip/unequip={item_guid}, craft={recipe_id}, place={recipe_id,x,z,radius?}, search={point_id,x,z,products:[prefab],timeout_seconds:0.2..8}. give_owner/drop_item={prefab,total_count:1..100,task_key}; deposit/withdraw additionally require container_guid. Poll actual results."""
        if op not in {"summon", "bind", "recipes"}:
            invoke(tasks.check_manual_action, buddy_id)
        return member("action",buddy_id,op,observation_id,args)

    @server.tool()
    def buddy_result(action_id: str, buddy_id: str | None = None) -> dict[str, Any]:
        """Read an action's real native status; follow/stay report mode selection, not arrival."""
        return member("result",buddy_id,action_id)

    @server.tool()
    def buddy_storage(container_guid: int | None = None, buddy_id: str | None = None) -> dict[str, Any]:
        """Read current local chest slots/availability; unknown or busy is not empty. Does not open or move items."""
        return member("storage",buddy_id,container_guid)

    @server.tool()
    def buddy_places(query: str = "", building_id: str | None = None, offset: int = 0, limit: int = 12, buddy_id: str | None = None) -> dict[str, Any]:
        """Query buildings actually seen by this companion. Remote unconfirmed is remembered, not missing; no world scan or home management."""
        return member("places",buddy_id,query,building_id,offset,limit)

    @server.tool()
    async def buddy_recipes(revision: int, binding_token: str, query: str = "", recipe_id: str | None = None,
                            offset: int = 0, limit: int = 12, buddy_id: str | None = None) -> dict[str, Any]:
        """Query the bound companion's live ingredients/technology. Read-only; no crafting or owner resources."""
        packet = member("observe",buddy_id)
        state = packet["state"]
        if (state["control"]["revision"] != revision or (state.get("companion") or {}).get("token") != binding_token):
            raise ToolError("Companion intent changed before recipe query")
        args = {"query": query, "offset": offset, "limit": limit}
        if recipe_id is not None:
            args["recipe_id"] = recipe_id
        record = member("action",buddy_id,"recipes",packet["observation_id"],args)
        try:
            for _ in range(140):
                result = member("result",buddy_id,record["id"])
                if result["status"] == "completed":
                    return result.get("data") or {"recipes": [], "total": 0}
                if result["status"] in {"failed", "interrupted"}:
                    raise ToolError("Recipe query failed: " + result.get("reason", result["status"]))
                await asyncio.sleep(.05)
            raise ToolError("Recipe query timed out")
        finally:
            member("expire_query",buddy_id,record["id"])

    @server.tool()
    def buddy_reply(text: str, revision: int, message_id: str, finish: bool = False, buddy_id: str | None = None,
                    dialogue_revision: int | None = None, keep_mode: bool = False) -> dict[str, Any]:
        """Speak via the bound Wilson and owner chat. finish ends this actor's current work."""
        return member("reply",buddy_id,text,revision,message_id,finish,dialogue_revision,keep_mode)

    tasks.register(server)
    return server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=PROJECT_ROOT / "logs/companion_bridge.json")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--write-mod-config", type=Path)
    parser.add_argument("--configure-only", action="store_true")
    parser.add_argument("--bridge-only", action="store_true")
    parser.add_argument("--poll", type=float, default=.2)
    args = parser.parse_args()

    if not 1 <= args.port <= 65535:
        parser.error("Invalid port")
    if args.config.exists():
        token = json.loads(args.config.read_text(encoding="utf-8-sig"))["token"]
    else:
        token = secrets.token_hex(32)
        args.config.parent.mkdir(parents=True, exist_ok=True)
        args.config.write_text(json.dumps({"token": token}), encoding="utf-8")
    bridge = CompanionTeamBridge(token)
    if args.write_mod_config:
        args.write_mod_config.parent.mkdir(parents=True, exist_ok=True)
        args.write_mod_config.write_text('return {enabled=true,url="http://127.0.0.1:' + str(args.port)
                                         + '/v1/exchange",token=' + json.dumps(token) + '}\n', encoding="utf-8")
        print("Local companion transport configured; re-enter the world.", file=sys.stderr)
    if args.configure_only:
        return 0
    service = None if args.bridge_only else create_server(bridge, poll=args.poll)
    listener = serve_bridge(bridge, args.port, metrics_path=args.config.parent / "companion_transport.jsonl")
    print(f"Companion bridge: 127.0.0.1:{listener.server_port}; no model API calls.", file=sys.stderr)
    try:
        if service:
            service.run(transport="stdio")
        else:
            threading.Event().wait()
    except KeyboardInterrupt:
        pass
    finally:
        listener.shutdown()
        listener.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
