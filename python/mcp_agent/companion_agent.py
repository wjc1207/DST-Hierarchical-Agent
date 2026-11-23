"""Run YOUR local DeepSeek/external planner CLI for YOUR server-side companion team."""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.config import load_dotenv, resolve_env_path
from common.pairing import ensure_bridge_token, format_pair_command
from common.paths import PROJECT_ROOT, PYTHON_ROOT
from executor.team_runner import CycleDiagnostics
from mcp_agent.planning_core import DEFAULT_API_URL, DEFAULT_MODEL
from transport.http_bridge import check_bridge_port


async def run(args, planner):
    from mcp import Client, StdioServerParameters
    check_bridge_port(args.port)
    server_args = [
        str(PYTHON_ROOT / "mcp_server" / "companion_server.py"),
        "--config", str(args.config.resolve()),
        "--port", str(args.port),
        "--poll", str(args.poll),
    ]
    parameters = StdioServerParameters(command=sys.executable, args=server_args, cwd=str(PROJECT_ROOT))
    async with Client(parameters, read_timeout_seconds=10) as client:
        from mcp_agent.planning_client import run_planning_loop
        diagnostics = CycleDiagnostics(None)
        is_external = hasattr(planner, "plan_companion")
        await run_planning_loop(
            client, poll=args.poll, max_cycles=args.max_cycles,
            companion_model=None if is_external else planner,
            external_backend=planner if is_external else None,
            idle_tool="buddy_observe",
            on_error=diagnostics.error,
            on_recovered=diagnostics.recovered,
        )


def main():
    from mcp_agent.external_backends import BACKENDS, ExternalAgentBackend
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", type=Path)
    parser.add_argument("--config", type=Path, default=PROJECT_ROOT / "logs/companion_bridge.json")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--poll", type=float, default=.2)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--max-cycles", type=int, default=0)
    parser.add_argument("--backend", choices=BACKENDS, default="deepseek")
    parser.add_argument("--backend-cmd", help="Custom executable/command for claude/codex/dsh backend")
    parser.add_argument("--backend-model", help="Optional model name passed to external CLI backend")
    parser.add_argument("--dsh-url", help="Local DSH HTTP callback URL for --backend dsh-http")
    parser.add_argument(
        "--print-pair-command", action="store_true",
        help="Print the in-game /buddy pair command for Steam Workshop pairing and exit",
    )
    args = parser.parse_args()
    if not .1 <= args.poll <= 1 or not 1 <= args.port <= 65535 or args.max_cycles < 0:
        parser.error("Invalid poll, port or max-cycles")
    if args.print_pair_command:
        token, _ = ensure_bridge_token(args.config)
        print(format_pair_command(token, args.port, prefix="/buddy", default_port=8766))
        return 0
    planner = None
    if args.execute:
        if args.backend == "deepseek":
            if args.env or not os.environ.get("DEEPSEEK_API_KEY"):
                load_dotenv(resolve_env_path(args.env))
            key = os.environ.get("DEEPSEEK_API_KEY")
            if not key:
                parser.error("DEEPSEEK_API_KEY is required on THIS player's computer")
            from mcp_agent.companion_planner import CompanionPlanner
            planner = CompanionPlanner(
                os.getenv("DEEPSEEK_API_URL", DEFAULT_API_URL), key,
                os.getenv("DEEPSEEK_MODEL", DEFAULT_MODEL),
            )
        else:
            planner = ExternalAgentBackend(
                args.backend, role="companion", command=args.backend_cmd,
                model=args.backend_model, dsh_url=args.dsh_url,
            )
    print(f"Companion mode (backend={args.backend}): server executes; this computer alone pays for its model calls.")
    try:
        asyncio.run(run(args, planner))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
