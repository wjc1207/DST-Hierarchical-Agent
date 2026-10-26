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
- `mcp_agent/jev_client.py`: bounded local choices for safety and planning gaps.
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

During daytime, the Lua executor automatically unequips a held torch to save
fuel without asking JEV. Maintenance requires a live control connection and
works while idle or fleeing without replacing the current action/report. If
the torch remains equipped, it retries once per second. Dusk and nighttime
torch decisions retain their existing rules.

DeepSeek reviews in the background without expiring goals every quarter. Visible
feasible goals run first. Missing sources trigger remembered-position,
known-source-room, worldgen-prior and unknown-branch search. Lua executes movement
and native actions continuously; JEV handles only safety and planning gaps.
Off-board opportunity arbitration is disabled. Known health, hunger or sanity below
30% triggers safety even without a carried remedy; native threshold crossings
interrupt running work. An unchanged situation acknowledged with `wait` resumes
safe board work until remedies, danger bands or native safety events change.

Board navigation sends ready local goals as optional `navigation_targets` in
`start_action`. Lua ignores unrelated resource changes and yields only when a
live offered action can advance a still-unmet watched goal after four units.
Pickup, tool/durability/path-checked tree/ore work, crafting, construction and
food are covered. Trees and ore need not change the collectible resource set.
The host checks planner changes and existing search budgets during movement,
cancelling with acknowledgement before rescheduling. DS gaps omit the filter
and keep their resource-event handoff. Safety remains immediate.

Known-region detours retain the committed physical entrance across incidental
room-label changes. They switch entrances after reaching the selected next-hop
room, rather than steering back whenever a detour crosses the previous room.
Progress history survives these transitions. Fixed crossing endpoints allow
three seconds for topology confirmation without repeated zero-distance walks;
`region_confirmation_timeout` pauses only the entrance, leaving alternatives
to the same destination available.

The public Science Machine goal/action remains `sciencemachine` /
`build_sciencemachine`. At the Mod boundary it maps to Klei's actual recipe and
entity ID `researchlab`; builder calls always use the native ID. Observations
use `prefab=sciencemachine, game_prefab=researchlab`, preserving existing board,
sequence and native completion semantics while retaining the raw ID. Buffered
recipes can be placed without spending ingredients again. Completion requires
observing the resulting entity; an observed manually built machine is also a
persistent verified milestone.

Once JEV selects escape, Lua redirects within that same action after about 0.8
seconds without 0.3 units of movement progress. Failed headings are avoided for
eight seconds within two units of the failure position; the whole flee action
never receives the ordinary 15-second failure cooldown. If every currently
passable heading was tried, the oldest failed heading is retried. Only an empty
live route set reports `no_safe_escape_route`. Reports/events retain
`escape_reroutes`; damage, phase changes, the existing action deadline and
explicit cancellation keep their previous semantics.

If the board is missing, empty, completed, or has no feasible offered action/route,
DeepSeek is asked to evaluate/update it immediately and JEV temporarily takes
local control. One planner request runs at a time, with the existing 15-second
error backoff and no intentional idle period before the first evaluation.
Future goals are preserved; automatic execution resumes when a task becomes
actionable. Pending gap choices yield to newly actionable board work before
dispatch, while already running native actions keep their bounded lifecycle.

DeepSeek requests start with stable `system` rules and static knowledge: DSTKnowledge,
the full Klei room-type/resource mapping, capabilities, canonical IDs/aliases and
fixed execution/planning policy. History follows; the final `user` carries live
state, progress, recent outcomes and the review reason. Replayed user snapshots
also omit copied references. Static room types are generation background, not
discovered world locations. API-provided cache token counts appear in
`daily_plan_usage`; missing counts are not treated as zero.

Commands retain the original offered choice and observation ID. The server checks
ownership/expiry; Lua validates current feasibility. Queued/running is not
completion. Pure semantic sequences and explicit MCP board tools remain compatible;
neither includes keyboard fallbacks.

Campfire siting uses `scripts/jev_campfire_siting.lua` and the Mod. A separate
scan covers combustible dropped items and entities without the normal nearby
whitelist/cap. Candidates lie 3–6 units from the current player position, require
a passable route avoiding known hazards, and keep at least 5 units clear of
combustibles; larger seasonal spread modifiers expand that clearance. Checks run
before buffering and again before placement. No safe site hides the build option;
there is no unsafe fallback. A buffered campfire remains available for later
placement without spending ingredients again.

Both models receive `construction.campfire` status, reason and clearance facts.
Timing includes `campfire_siting_ms`. This checks the construction site; it does
not change later cooking approach behavior or identify the cause of health loss.

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
