# MCP and native Lua execution (1.8.8)

[中文](MCP.md) · [README](../README.EN.md)

The control path is: **AI host → STDIO MCP server → authenticated loopback HTTP → client Mod**.
The server does not tail game logs, simulate input or call model APIs. Lua reports
legal actions, executes them natively and verifies effects. The included external
host, `mcp_agent/mcp_agent.py`, connects DeepSeek/JEV to this path. The keyboard runner has been removed; model calls use an independent API client.

## Setup

Python 3.11+ is required. From the project folder:

```powershell
python -m pip install -r requirements-mcp.txt
python .\mcp_server\mcp_server.py --configure-only --write-mod-config .\scripts\jev_bridge_config.lua
```

This creates an ignored token file at `logs/mcp_bridge.json` and matching ignored
Mod configuration. Never put model API keys in that configuration or share either
token file. Copy `modmain.lua`, `modinfo.lua` and `scripts/` to the installed Mod,
including the generated configuration, then re-enter the world. Do not copy `.env`,
`logs/` or `.venv-mcp/`. Start the service from the same project/token path.
When upgrading, preserve the existing token/configuration, update Python
files (including both `mcp_server/` and `mcp_agent/`), Mod entry files and `scripts/` (including
`jev_timing.lua`), then re-enter the world to load 1.8.6.
Upgrading from a complete 1.8.5 installation requires `mcp_agent/`, `mcp_server/`,
`scripts/jev_bridge.lua` and `modinfo.lua`;
restart the Python host and reload the world. Preserve installed credentials
and generated configuration.
Do not overwrite the installed `scripts/jev_bridge_config.lua` with configuration
from another directory; preserve it alongside the installed token file.
The updated autonomous host requires protocol 4 and rejects older Mod snapshots.
Update both sides together; measurement version remains 1.

Science Machine mapping updates require `modmain.lua` and
`mcp_agent/daily_planner.py`, followed by a world reload and Python-host restart.
Public goals/actions keep `sciencemachine` / `build_sciencemachine`; the Mod uses
`researchlab` for native recipes/entities and retains it in `game_prefab` on the
normalized observation. No task-board or action-name migration is required.

## Autonomous DeepSeek/JEV host

Stop bridge-only instances, probes and other same-port clients.
Using the Python environment with MCP dependencies installed, run:

```powershell
python .\mcp_agent\mcp_agent.py --loop --execute
```

The host launches its own STDIO service and loads existing `.env` model settings.
Without `--execute`, it observes one decision without game commands or DeepSeek
planning. `--no-planner` disables planning; `--env PATH` and `--config PATH` select
existing files. A missing DeepSeek key leaves JEV/exploration available.

The automatic host and an external MCP client must not own the same bridge port
at once. Startup preflights the port; the HTTP listener also binds exclusively
to close the check/start race. Windows uses `SO_EXCLUSIVEADDRUSE`; port sharing
is disabled. If Codex MCP, a probe, `--bridge-only` or another service already
owns 8765, startup prints `mcp_startup_error bridge_port_unavailable
address=127.0.0.1:8765` and exits with code 2 before observation retries. It does
not attach to or terminate the existing process. Stop the old client/service
before launching `mcp_agent/mcp_agent.py`; a client-managed service may respawn if only its
process is killed. Update both `mcp_server/` and `mcp_agent/`;
the Mod and existing token configuration are unchanged.

- DeepSeek maintains persistent priority goals, dependencies and search budgets,
  using the existing knowledge and conversation history.
- JEV handles safety and temporary gaps when no board
  task can advance, not each planned pickup. The host requests DeepSeek evaluation
  immediately for an empty/completed/unexecutable board; one request runs at a time.
  Off-board opportunity arbitration is disabled. Safety includes targeted enemies,
  damage, lighting and known health/hunger/sanity below 30%, even without a carried remedy.
- Python owns visible-first scheduling, latched completion, search budgets and DFS/BFS
  intent selection. It selects only offered actions; Lua owns game movement.
- Lua owns approach, continuous movement, native actions, effect verification
  and safety interruption. Legacy board clients remain compatible.
- The MCP server owns atomic observation/action snapshots and executor handoffs,
  not model calls. Both models use the same game-fact projection and capability facts.

The host waits for Lua acknowledgement before issuing work after a board pause.
Damage, inventory, phase and threat changes do not discard regular model requests
in Python. Temporary planning-gap choices yield back to automatic execution if
the latest board can advance before dispatch. For dispatched choices, submit the
original offered choice/observation; Lua validates
live feasibility. Death or a changed game/player session stops the host; disconnected
or expired observations cannot issue commands.
Ctrl+C requests cancellation and waits for acknowledgement; if unconfirmed,
closing the service lets the three-second Lua lease stop residual work.

Terminal output includes action results, cycle/JEV time and planning previews.
Timing details go to `logs/mcp_timing.jsonl`; new plans go to `logs/task_board.json`
and `logs/task_board.jsonl`. Old sequence logs do not identify the current plan.
The keyboard runner and its predictive buffer have been removed.

## Priority goals and resource navigation (1.8.7)

Server files (entry point, bridge and HTTP instrumentation) live in `mcp_server/`.
The automatic host and connectivity probe live in `mcp_agent/`. Models and search intents live in `mcp_agent/`; shared rules in `common/`; knowledge assets in `data/`. `.env`, `logs/` and the installed
Lua bridge token/config keep their existing locations. Update existing MCP client
launch configurations to `mcp_server/mcp_server.py`. From the project root,
`python -m mcp_server` and `python -m mcp_agent` are also supported.

DeepSeek returns `tasks`, validated as `schema=2, execution_policy=priority_goals`.
Finish feasible visible goals by priority before navigating for the first ready goal.
Klei room priors augment observed source memory; they do not reveal hidden entities.
Reviews patch the board without clearing progress at quarter/day boundaries.
See [goal schema, navigation policy, budgets and upgrade files](GOAL_NAVIGATION.md).
Update both Python and Lua, re-enter the world and restart Python; keep the existing
`.env`, bridge token and `scripts/jev_bridge_config.lua`. Protocol remains 4.

## Ordered action sequences (legacy compatibility)

The following describes the previous scheduler. New DeepSeek requests no longer
produce this format.

The legacy scheduler accepts `steps`, not priority-sorted `tasks`:

```json
{"kind":"action_sequence","goal":"Prepare light","steps":[
  {"id":"grass","action":"collect","prefab":"cutgrass","count":2},
  {"id":"twigs","action":"collect","prefab":"twigs","count":2},
  {"id":"torch","action":"craft","prefab":"torch","count":1},
  {"id":"survey","action":"explore_current_region","area":256}
]}
```

Each sequence contains 1-12 strictly ordered steps. Each step has a
`timeout_seconds` bound (default 120, allowed 5-300). `collect`/`craft` counts mean
desired current stock, not additional collection. Completed steps are latched:
spending ingredients later never rewinds the sequence. A failure or timeout
stops the batch and requests a new plan with progress and failure facts.
Active sequences survive quarter boundaries; without an active sequence the
existing planning windows remain. Completion/failure asynchronously requests the next batch.

Supported operations: `collect {prefab,count}`, `craft {prefab,count}` (axe,
pickaxe, torch), `build {prefab}` (campfire, sciencemachine),
`eat_safe_food {hunger_ratio}` (default 0.7), `cook_food {count}` (additional cooked
items), `equip_torch`, `unequip_torch`, and bounded `wait {seconds:1..30}`.
`travel_region {node_id}` requires actual entry into an observed topology node,
not a display index or historical entered flag. `explore_current_region {area}`
requires additional observed area (default 256), or a fully surveyed boundary.
`explore_other_region {new_regions}` discovers new nodes (default 1), not merely
another already-known region.

The current step repeats native actions without per-item JEV requests.
Ordinary resource discoveries resume the same step even if native navigation
yields; danger, damage, lighting and urgent hunger still hand off to safety
decisions. Only observed effects advance steps, never predicted state. The
executor does not invent missing-tool/recipe subtasks; the plan must include
prerequisites explicitly. Each native command still uses an original MCP
observation and Lua verification; this is not a blind batch of key presses.

Update the new `mcp_agent/action_sequence.py` and changed `mcp_agent/daily_planner.py`, `mcp_agent/mcp_agent.py`,
`mcp_agent/navigation_intents.py`, `mcp_agent/jev_client.py` and `data/DSTKnowledge.txt`, then restart Python.
The Lua protocol remains 4; existing `submit_task_board` tools are kept for older
clients. Pure semantic sequences remain MCP-compatible, without keyboard fallbacks.

## Other MCP clients and connection checks

Configure any STDIO MCP host using the Python that has the dependencies installed:

```json
{
  "mcpServers": {
    "dst": {
      "command": "python",
      "args": ["C:/path/to/jev_dst_agent/mcp_server/mcp_server.py"]
    }
  }
}
```

The host manages that process. Do not start a second instance on the same port.

Alternatively, `python mcp_agent/mcp_probe.py` starts a temporary service and checks the real
game connection through MCP without any third-party host. It waits for the Mod,
prints state/actions and exits, sending no game action by default. An explicit
`--execute-choice wait` tests one offered action. Do not run the probe alongside
another service instance; exiting closes its temporary server.
Only MCP uses stdout; diagnostics use stderr. The internal bridge binds solely to
`127.0.0.1:8765`. Regenerate Mod configuration after changing `--port`.
`--config` selects an existing token file. No model key is required.

For connectivity testing only, run `python mcp_server/mcp_server.py --bridge-only`; this does
not start MCP or send game actions. The Mod's enabled message alone is not proof
of connectivity: check `state.bridge.connected`. Stop this instance before
connecting an MCP host. Disabling the bridge does not enable another control transport.

## Tools and lifecycle

- `get_state(refresh=false)`: latest event snapshot and actual age; true requests new facts, waiting up to three seconds. Disconnected games error.
- `get_available_actions(refresh=false)`: cached facts/options plus latest control status, snapshot version/age, observation ID and board progress; true requests a fresh snapshot.
- `start_action(choice, observation_id, navigation_targets=None)`: queue one offered action;
  returns an ID, not success. For navigation, an optional list of at most six local
  targets (`inventory {prefab,count}`, `structure {prefab}`, `hunger_ratio {at_least}`)
  replaces resource-set interruption with live offered-action matching. The host
  sends all ready local board goals, excluding completed/blocked/dependent work.
  `[]` ignores ordinary resource changes; omission preserves unfiltered resource
  events for DS gaps and existing clients. This cannot alter coordinates, choose
  an unoffered action or suppress safety/region/path interruptions.
- `get_action_status(action_id)`: state, reason and connectivity.
- `cancel_action(action_id)`: request cancellation; wait for Lua acknowledgement.
- `submit_task_board(board, observation_id, auto_execute=false)`: validated board;
  local automation requires explicit opt-in. Resubmit with false to pause.

Read facts/actions, submit with the observation ID, then poll. States are queued,
running, cancel_requested, completed, failed, interrupted and cancelled. Reobserve
after handoffs. Observations bind the session/player and original offered actions
for up to ten seconds. Python enforces freshness, ownership, expiry, the original
option whitelist and exclusive executor control, not live game feasibility. It queues
the original target instead of replacing it with a fresh same-named action. Lua
validates that target against current facts. Up to 128 observations are retained;
heartbeat sequence changes alone do not invalidate a decision.

An enabled automatic board reserves ownership even between local actions. Pause it
by resubmitting with `auto_execute=false`, then wait for Lua to stop its active
action before submitting manual commands.

## Scope

Implemented: verified native collection with path approach; tools/torch crafting;
torch equipment and daylight cleanup; cooking/eating; campfire/Science Machine
placement; whole work targets and nearby reachable new drops; event-driven local
exploration, unknown exits and reported known-region routes; bounded escape via
reported passable routes; opt-in visible local board actions. Board navigation
yields after four units only for a live offered action advancing a watched goal,
with reason `goal_action_available`; unrelated resources do not stop movement.
Unfiltered DS-gap navigation retains resource-set changes after four units.
Safety events do not wait for that floor. The host still reviews changed board
targets and existing search budgets during native movement, cancelling with
acknowledgement before rescheduling; this does not ask JEV for ordinary opportunities.
No visible targeted hostile is not proof of permanently cleared aggro.

A blocked escape changes heading inside the current action after about 0.8
seconds without movement progress. Only that heading near the failure position
is temporarily avoided (eight seconds / two units). Failed flee actions do not
disable the entire flee option for 15 seconds. If all live passable headings were
tried, retry the oldest; never invent a route when the passability scan offers
none. Native reports/events expose `escape_reroutes` for diagnosis.

Region travel checks the complete entrance approach and keeps the active trip's
valid first hop and physical entrance across incidental room crossings during
a detour, instead of switching whenever a shorter route reappears. Lua
reports `graph.route_pause` and interrupts with `route_no_progress` or
`route_gateway_unavailable` when an entrance is temporarily rejected. A fixed
crossing endpoint waits at most three seconds for topology confirmation, then
reports `region_confirmation_timeout` without repeated zero-distance walks. The pause
is entrance-specific; other routes to the same region remain eligible. Progress
persists across resource-event yields and excludes model/local-work waits; see
[navigation recovery](GOAL_NAVIGATION.md#search-bounds-and-recovery).

The full DFS/BFS selector and failed-source memory have not moved into Lua: the
host uses `mcp_agent/navigation_intents.py` for destination scheduling, not keyboard control. Attacking/weapon equipment, automatic tool prerequisite solving
and Alchemy Engine actions are not offered. Missing/unsupported board goals yield
to the host; Lua does not call models itself. Arbitrary code, paths and custom
coordinates are not tools.

Lua sends lightweight heartbeats about every 0.5 seconds and keeps local sensing/
execution checks about every 0.2 seconds. Full facts are event-driven or on demand.
Starts, confirmed starts, terminal results and board ownership changes carry snapshots
and bypass the heartbeat delay. In-flight requests remain single-flight; pending
changes are sent on the next tick after the callback, without recursive sends.
Control metadata is refreshed after execution/board updates so terminal results
and busy flags agree. The host polls facts and lifecycle atomically instead of
separately querying an older status and then observing again.

After three seconds without a valid reply, it stops work
and pauses the board. Late callbacks, duplicate commands and old worlds cannot
restart work. Building callbacks respect cancellation generations. Ordinary actions
yield on danger; recovery actions have appropriate exceptions. During daytime,
Lua automatically unequips a held torch while the control connection is live,
including while idle or fleeing, without replacing the active action/report.
Unconfirmed unequip is retried at one-second intervals. Dusk and nighttime
lighting retain their existing rules. Completion requires observed effects,
not a sent command.

Keep the internal HTTP port private. Its token is in request bodies (for the game
HTTP API), never in diagnostics. The network bridge is not a public MCP endpoint.

### Handoff timing (1.8.2)

`logs/mcp_timing.jsonl` now includes bounded `events` marking host terminal
observation, decision readiness, submission/queueing, observed Lua acceptance and,
when supported by evidence, observed native starts. Board sync request/acknowledgement
markers are also included. Correlate action IDs across cycles; `at_ms` is relative
to its own host cycle, not a global timestamp.

- `lua_handoff_gap_ms` / terminal `accepted_gap`: previous completion to Lua
  accepting the next command, **not** an animation-start gap.
- `lua_started_at` means acceptance. `lua_confirmed_started_at` requires exact
  native action identity/replication or actual movement; unknown remains absent.
- `lua_execution_ms` includes approach and game execution until verified results.
- `lua_finish_to_exchange_ms` measures scheduling before the completion upload;
  `lua_previous_http_round_trip_ms` is the previous valid HTTP round trip. These
  use Lua game time, which is not wall-clock network latency while paused.
- Server-clock `queue_to_delivery_ms` ends when the response first includes the
  command; `delivery_to_running_report_ms` includes wire and acknowledgement wait;
  `terminal_cache_ms` ends at the current status read.
- Host-clock `host_observed_to_submit_ms` measures the gap after learning the
  previous terminal result and before submitting the next command.

Never subtract timestamps from different clock domains or sum overlapping fields.
`action.execute` remains host wait time, not pure character activity. This release
optimizes handoffs and adds measurement; MCP predictive buffering remains deferred.

### Detailed measurements (1.8.3, instrumentation only)

The startup command is unchanged. The service writes `mcp_transport.jsonl` next
to its `--config` file; the host retains `logs/mcp_timing.jsonl`. Diagnostics contain
only duration/byte counters and correlation IDs, never bodies, model text, keys or
tokens. Log-write failure warns once and does not change action results.

Lua measures entity scanning, navigation, collection paths, inventory/vitals,
crafting, candidate generation, native ticks and board steps. Sampling aggregates
tick counts plus sum/max durations since the preceding send; pre-send control
refresh is separate. `GetTimeRealSeconds` or `GetTimeReal()/1000` supplies real-time
durations; simulation ticks are not used for sub-frame work. Missing clocks remain
`unavailable`, not zero. Bridge protocol stays 3, measurement version is 1.

Each HTTP record is keyed by `(server_id, session_id, sequence)`. Lua uploads
completed `previous_exchanges` receipts in the next request (bounded to eight):
encoding, synchronous `QueryServer` call time, call-to-callback time, JSON decode,
callback dispatch and byte counts. Deduplicate by the receipt's own server/sequence,
not the envelope carrying it. The last receipt may not be uploaded before shutdown.
Call-to-callback includes engine queues, HTTP and callback scheduling, not just wire
latency. Decode and dispatch are separate.

Server phases cover body read, JSON decode, lock wait, state copy, exchange,
JSON encode, header/body socket writes and total handler time. Records are written
after the response; total excludes diagnostic file IO. Socket write completion is
not proof of client receipt. Exchange includes lock/copy; state sampling includes
its children. Do not sum parent and child stages or subtract different processes'
absolute timestamps.

JEV requests receive a `request_id` tied to the source session/state sequence and
preceding action. Worker queue time and an independent background trace are stored
in `decision.worker_completed.worker_timing`; packaging, response headers, body read
and parse are separate. Headers time includes connection/upload/remote processing,
not pure model inference. `decision.result_observed.result_ready_to_notice_ms`
measures result-notice delay. Do not add overlapping background time to host totals.
Failures/discarded completed requests retain timings without logging exception text.
DeepSeek retains its existing background total. This release changes no strategy,
poll intervals, executor policy or predictive-buffer behavior.

### HTTP upload handshake (1.8.4)

The loopback handler now uses HTTP/1.1 and sends an interim `100 Continue` before
reading a valid Expect-gated upload. The previous HTTP/1.0 default skipped that
handshake, so a waiting client could delay its body until a timeout. Final responses
explicitly close the connection; this does not enable keep-alive or change polling,
decision or execution policy. HTTP/1.0 and uploads without Expect remain supported.
The interim response is not authentication: the body token is still checked.
Routing, body length and unsupported/ambiguous framing are checked before Continue.

Transport rows add a content-free `http` object: `request_version`,
`response_version`, `expect` (`none`/`100-continue`/`other`) and `continue_sent`.
No arbitrary header values, Authorization, Cookie or tokens are recorded. Verify
the actual game's Expect/Continue flags and whether `body_read_ms` stops clustering
around 1000 ms. A passing simulated waiting-client test does not establish actual
game send/callback latency.

The 1.8.3 game run showed whole-second Lua real-clock results. Interface names and
units do not guarantee millisecond resolution: zero does not mean zero work and
1000 does not prove one second of CPU work. This release does not replace that
clock. Use Python's monotonic measurements for millisecond attribution until a
finer game clock is verified.

### Mod-owned live validation (1.8.5)

The Python host no longer compares entire decision contexts during/after inference.
JEV must answer one of the original options, but Lua alone determines whether the
intent remains executable. This also applies to other MCP clients. Target GUID,
prefab, action kind and navigation node/mode must match; current route coordinates
can be refreshed without silently switching targets or destination regions.

An escape choice survives damage and hostile-set changes and uses a freshly computed
passable route. Damage during escape does not cancel it. Cleared threats return
`threat_cleared`; missing escape routes return `no_safe_escape_route`, not a substitute
AI choice. Depleted/missing/replaced collection targets return `target_not_collectable`,
`target_missing` or `target_changed`; new hostile targeting returns `targeted_by_hostile`;
other unavailable prerequisites return `action_unavailable`. Rejection reports retain
the choice and reason so the host can reobserve and decide again.

Session/player changes, death, expired observations, disconnection, cancellation and
exclusive ownership remain protected. Lua retains leases, late-callback checks and
the latest 256 accepted/rejected/cancelled command IDs. This does not enable predictive
buffering or change board-handoff priority. Legacy keyboard-path prefetch checks are
unchanged.

### Event snapshots and control heartbeats (1.8.6, protocol 4)

`kind=snapshot` carries full facts on initial connection, reconnect, explicit refresh
and relevant events. `kind=heartbeat` omits state, entities, navigation, action
candidates and historical event rings; only control/report fields and numeric diagnostics
remain. Commands/cancellations still travel in responses; unchanged board bodies
are not repeatedly returned. Local sensing and native execution keep running.

Events include day/phase/quarter changes, health, threats, inventory/equipment counts,
available action types, fire state/range band and topology discoveries/transitions.
Ordinary positions, nearest same-type resource GUIDs, fuel ticks and small hunger ticks
are not upload events; hunger/sanity use bands. Failed uploads are retried; changes
during flight survive old callbacks. A restarted service requests full resynchronization
and cannot issue work from a heartbeat without any initial facts.

`state_version` identifies the last full snapshot; `transport_sequence` identifies the
latest exchange. `state_age_ms`/`age_seconds` measure full-snapshot age, while
`connection_age_ms` measures the last valid message. Heartbeats only update control
metadata, not world facts or snapshot age. Connection leases still expire after three
seconds; ten-second observations age from their source snapshot, not cache reads.

External clients can request `get_state(refresh=true)` or
`get_available_actions(refresh=true)`. Lua receives the request in a response and
prioritizes a full snapshot carrying its `snapshot_request_id`; heartbeats do not
satisfy refresh. Timeouts fail explicitly; async tool wrappers do not block other
MCP calls while waiting. The autonomous host refreshes snapshots older than one
second before actionable decisions or board submission, not on every model/action
poll or stable wait-only night cycle.

Transport diagnostics add `message_kind` and allowlisted `snapshot_reason`
(`initial`, `event`, `requested`, `resync`) for message/byte accounting. Continue
handling is retained. This changes communications/cache reads only, not opportunity
authorization, emergency model preemption, predictive buffering or local sensing cadence.

### Pickup handoff after work

Work approach hands over to the target-based native CHOP/MINE action once the
target is inside the six-unit action-button selection radius and its observed
path is directly safe. Native locomotion handles the final collision-aware
arrival distance; the bridge does not WALKTO a boulder's centre or require a
1.2-unit centre distance. Verified detours continue before handoff. This is a
dispatch radius, not a remote-work range. Tool/durability checks, safety,
completion verification and loot cleanup still apply; collection keeps its
existing approach logic.

After chopping or mining, animation recovery or native interaction cooldown may
temporarily leave the action picker empty. The executor retains the exact drop
GUID and retries every 0.2 seconds for up to 3 seconds, rather than raising a
`native_exception` or switching to another same-prefab target. Each retry checks
identity, collectability and the observed path. Threats, light changes and
cancellation still interrupt it. An exhausted retry window reports
`collection_action_unavailable` explicitly.

After dispatch, inventory gain is still required before collecting the next
drop. Each approach starts its own movement-progress clock, so a long chop is
not counted as a stalled walk toward loot. Accepted pickups without an inventory
effect are retried at one-second intervals while idle and fail after four seconds
with `loot_inventory_not_increased`, rather than waiting for the whole action timeout.

Work cleanup uses an uncapped scan within 6 units of the original tree/rock,
independent of the normal nearest-40 snapshot and the player's later movement.
Existing drops and partial/merged stacks remain eligible. It waits at least one
second for initial drop replication and 0.6 seconds of empty ground after the
last verified pickup. A blocked remaining drop interrupts with a path error;
an unavailable scan fails explicitly. Safety checks still use the full ordinary
snapshot before the private loot view, so the loot scan cannot hide enemies.

`scripts/jev_tree_work.lua` reads standard growth stages from replicated tree
animations, with server growable/work-left components when present. Unknown stages
reserve 15 axe uses. The offered chop target prefers a reachable tall tree with
enough durability, then normal, then short; distance breaks size ties. It can
approach an observed tree beyond 6 units before starting work; mining keeps its
existing proximity rule. Update `modmain.lua`, the complete `scripts/` directory
(including the new helper), and `data/DSTKnowledge.txt`; re-enter the world.

### Juicy berry harvest

`collect_berrybush_juicy` is one native operation: approach and pick the requested
bush, then collect fallen `berries_juicy` within 6 units of that bush using the
same uncapped loot scan, recovery retries and inventory verification as work
cleanup. The collection deadline is 30 seconds. A dispatched pick followed by
the bush becoming unpickable starts cleanup; it does not prove completion.
Existing carried fruit cannot satisfy verification of this harvest. After a
stable empty scan, at least one new fruit must have entered inventory, otherwise
the action reports `harvest_loot_not_received` after a short replication grace.
Remaining fruit keeps cleanup active even after the first inventory gain.
Safety changes and cancellation can interrupt any stage. Ordinary berry bushes
and standalone loose-fruit pickups retain direct inventory-gain verification.
Update `scripts/jev_native_executor.lua` and the knowledge file, then re-enter
the world; no new MCP tool or separate model decision is required per fruit.

## Tests

`python run_tests.py` runs offline regressions. With the optional SDK installed,
`python -m unittest discover -s tests -p test_game_bridge.py -v` additionally tests
real MCP discovery, a STDIO subprocess and loopback HTTP with a simulated Lua peer.
Without the SDK those integration cases are skipped. Lua tests are included in
`tests/exploration_spec.lua`. They do not replace testing `QueryServer` and native
actions/replication in the actual updated game client.
`python -m unittest discover -s tests -p test_mcp_agent.py -v` covers fake-model
autonomous handoffs, Mod-owned feasibility, lifecycle changes, board acknowledgements
and shutdown.
