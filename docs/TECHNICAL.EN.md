# Technical reference (1.8.8, MCP-only)

[README](../README.EN.md) · [中文](TECHNICAL.md)

The only game-control path is MCP → native Lua execution. Keyboard simulation,
window discovery/focus, game-log polling, speculative action buffers and their
obsolete tests have been removed. Logs remain diagnostic files, not a Python
control transport. There is no retained keyboard runner.

## Package boundaries

```text
mcp_agent/   Models, planning, persistent goals, search intents, probe
mcp_server/  MCP tools, state cache, action ownership and loopback HTTP
common/      Snapshot rules, board contracts, config paths and timing
scripts/     Lua perception, pathfinding, native execution and live validation
data/        DSTKnowledge.txt and generated worldgen resource priors
tests/       Current MCP/shared-rule regression tests and Lua checks
tools/       Klei resource extraction
docs/        Technical documentation
```

The root retains Mod entry points, READMEs, requirements and the test runner.
`.env`, `logs/` and the installed bridge token/config retain their locations.

- `mcp_agent/daily_planner.py`: DeepSeek needs, priorities, dependencies and reviews.
- `mcp_agent/jev_client.py`: bounded local choices for safety and opportunities.
- `mcp_agent/priority_board.py`: visible-first scheduling and search budgets.
- `mcp_agent/navigation_intents.py`: source/region destinations and unknown-branch
  search. It chooses only navigation options already offered by Lua.
- `mcp_agent/model_state.py`: the same fact projection for both models.
- `common/game_rules.py`: supplied snapshot facts without OS input.
- `modmain.lua` and `scripts/jev_exploration.lua`: passability, frontiers, obstacle
  avoidance, safe movement legs and continuous native movement.
- `scripts/jev_native_executor.lua`: live legality, approach, collection/crafting,
  effect verification and safety interrupts.

Python chooses WHY/WHERE to search; Lua computes and executes HOW to get there.
See [goal scheduling and Klei priors](GOAL_NAVIGATION.md) and
[MCP setup and protocol](MCP.EN.md).

## Execution

DeepSeek reviews in the background without expiring goals every quarter. Visible
feasible goals run first. Missing sources trigger remembered-position,
known-source-room, worldgen-prior and unknown-branch search. Lua executes movement
and native actions continuously; JEV handles meaningful local opportunities and
safety rather than every planned plant.

Commands retain the original offered choice and observation ID. The server checks
ownership/expiry; Lua validates current feasibility. Queued/running is not
completion. Pure semantic sequences and explicit MCP board tools remain compatible;
neither includes keyboard fallbacks.

## Paths and startup

From the project root:

```powershell
python .\mcp_agent\mcp_probe.py
python .\mcp_agent\mcp_agent.py --loop --execute
```

The host starts its own `mcp_server/mcp_server.py`. Stop other same-port services
and Codex's DST MCP connection first. `python -m mcp_agent` and
`python -m mcp_server` also work from the root.

Credentials stay in root `.env`; `--env` selects another existing file. Bridge
credentials stay in `logs/mcp_bridge.json` and installed
`scripts/jev_bridge_config.lua`. Knowledge is in
[data/DSTKnowledge.txt](../data/DSTKnowledge.txt); priors are in
[data/worldgen_resources.json](../data/worldgen_resources.json).

Plans use `logs/task_board.json[l]`. Timing uses `logs/mcp_timing.jsonl` and
`logs/mcp_transport.jsonl`. MCP reads never poll `client_log.txt`. The Mod's old
keypad/function-key action entry points are removed; use MCP `cancel_action`.

Upgrade `mcp_agent/`, `mcp_server/`, `common/`, `data/`, Mod entry points and
`scripts/`; preserve `.env`, logs and generated bridge credentials. Re-enter the
world and restart Python. Never overwrite installed credentials with another copy.

## Tests

Run `python run_tests.py` for fake-model/game and local protocol checks without
real model calls or game inputs. Fengari runs `tests/exploration_spec.lua`;
Lua checks call native handles directly without keyboard handlers.
