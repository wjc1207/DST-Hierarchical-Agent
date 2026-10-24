"""Standalone MCP connectivity probe. Read-only unless --execute-choice is explicit."""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if __package__ in {None, ""}:
    sys.path.insert(0, str(PROJECT_ROOT))


async def probe(args):
    from mcp import Client, StdioServerParameters
    root = PROJECT_ROOT
    parameters = StdioServerParameters(command=sys.executable,
        args=[str(root / "mcp_server" / "mcp_server.py"), "--config", str(args.config.resolve()), "--port", str(args.port)],
        cwd=str(root))
    async with Client(parameters, read_timeout_seconds=5) as client:
        deadline = time.monotonic() + args.timeout
        while True:
            result = await client.call_tool("get_state", {})
            if not result.is_error:
                data = result.structured_content
                if data["state"].get("bridge", {}).get("connected"):
                    break
            if time.monotonic() >= deadline:
                raise RuntimeError("No connected game: check installed Mod configuration/token, re-enter the world, and close other bridge instances")
            await asyncio.sleep(0.5)
        state = data["state"]
        print(f"connected player={state['player'].get('prefab')} day={state['world'].get('day')} "
              f"phase={state['world'].get('phase')} session={data['session_id']}")
        result = await client.call_tool("get_available_actions", {})
        if result.is_error:
            raise RuntimeError(str(result.content))
        actions = result.structured_content
        print(json.dumps({k: v for k, v in actions.items() if k != "state"}, ensure_ascii=False, indent=2))
        if not args.execute_choice:
            print("read_only: no game action sent")
            return
        result = await client.call_tool("start_action", {"choice": args.execute_choice,
            "observation_id": actions["observation_id"]})
        if result.is_error:
            raise RuntimeError(str(result.content))
        action = result.structured_content
        deadline = time.monotonic() + args.timeout
        while action["status"] not in {"completed", "failed", "interrupted", "cancelled"}:
            if time.monotonic() >= deadline:
                await client.call_tool("cancel_action", {"action_id": action["action_id"]})
                raise RuntimeError("Action timed out; cancellation requested, not yet confirmed")
            await asyncio.sleep(0.3)
            result = await client.call_tool("get_action_status", {"action_id": action["action_id"]})
            if result.is_error:
                raise RuntimeError(str(result.content))
            action = result.structured_content
        print(json.dumps(action, ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=PROJECT_ROOT / "logs" / "mcp_bridge.json")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--timeout", type=float, default=60)
    parser.add_argument("--execute-choice", help="Explicitly execute ONE offered choice; omitted means read-only")
    args = parser.parse_args()
    if not 0 < args.timeout <= 120:
        parser.error("--timeout must be greater than zero and at most 120 seconds")
    try:
        asyncio.run(probe(args))
    except KeyboardInterrupt:
        print("probe_stopped: bridge process closing; Lua stops any remaining work after lease expiry", file=sys.stderr)
    except (ImportError, RuntimeError) as exc:
        print(f"probe_error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
