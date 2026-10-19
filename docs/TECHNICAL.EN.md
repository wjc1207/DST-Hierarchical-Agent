# JEV DST Agent technical reference (1.7.0)

[Project README](../README.EN.md) · [中文技术说明](TECHNICAL.md)

Run all commands below from the project root (`jev_dst_agent`).

### 1.7.0 automatic local board execution

Execute mode first advances locally feasible board tasks by priority, dependencies,
and observed progress. Each bounded action is followed by a fresh evaluation;
consecutive gathers and switches to another feasible task need no JEV request or
predictive prefetch. JEV resumes when no local task is executable. New resources
alone do not interrupt local board execution; off-board opportunities remain JEV
decisions. Dry-run mode never automatically executes board tasks.

Supported local goals include inventory collection, explicit axe/pickaxe/torch
crafting goals, eating, and existing legal structure actions. Log/mineral goals
may use an existing sufficiently durable tool on a known target within 6 units,
including drop pickup, but do not invent missing-tool subtasks. Work targets need
a safe reachable path and the new Mod's `telemetry.cancel_work` capability. No
automatic exploration, region navigation or off-board collection is added.
Existing night travel, critical hunger and recipe safety rules still apply:
fixed campfire light alone does not permit night collection outside its range.

Immediately before execution, legal actions and the exact task/target are checked
again. Observed hostile targeting, health loss, phase changes or changed night
light sources hand control back to JEV without failure backoff. Movement holds
check the latest received telemetry every 0.1 seconds; responsiveness still
depends on the Mod's reporting rate. Interruption releases movement keys; F4
also cancels queued Mod work dispatch, including on Ctrl+C. A server-side action
animation already started may still finish. Failure or no measured increase
toward the selected target pauses that board/task/target combination for 15
seconds before handing back. Unrelated inventory changes or natural hunger loss
are not progress.

Console markers are `board_auto task=… action=… progress=…`,
`board_auto_completed`, and `board_auto_handoff reason=…`. Timing JSON includes
`decision_source=board_auto`, `board_task_id`, `board_auto_action`,
`board_auto_outcome`, and `board.auto_execute`; successful automatic cycles have
no JEV request time. Update Python including the new `board_executor.py`, restart
it, and update/reload `modmain.lua` and `modinfo.lua`, keeping the previously
required `scripts/` files. Schema stays at 10. An older Mod disables automatic
chopping/mining, while other legal local tasks can still run.

### 1.6.2 ordinary flower collection

`flower` now participates in explicit pickability, verified-path filtering,
`collect_flower`, region resource memory and new-resource events. Collection
verifies `petals` inventory gain or the original flower disappearing. Prediction
adds one petal and removes the flower; very fast picks can still use post-success
prefetch. Loose petals are collectible, but are not hunger food or cookable
inputs. JEV chooses whether picking is worthwhile; the hint notes that rose
variants sharing this prefab may hurt. Other prefabs such as `flower_evil` remain
outside the collection whitelist. Update/reload `modmain.lua` and `modinfo.lua`,
and sync `controller.py`, `jev_agent.py`, and `action_buffer.py`.

### 1.6.1 non-predicting client start acknowledgement

With movement prediction disabled, a client may have neither a local stategraph
nor a buffered action. `scripts/jev_execution.lua` observes the actual native
`RemoteActionButton` and `RemoteMakeRecipeFromMenu` calls without changing their
arguments or return values. A matching local action, or a subsequent transition
to doing via `IsDoingOrWorking()`/replicated tags, confirms the requested target
or recipe. Requests alone, pre-existing busy state, and unrelated buffered
actions never acknowledge a start.

A temporary 30 Hz observer emits telemetry when a start is confirmed. It retains
the acknowledgement for 0.8 seconds after doing ends; unacknowledged requests
expire after three seconds. Python rejects requests older than its current
interaction snapshot. Success still requires separate outcome verification.
Terminal `jev_buffer_predicted ... start_source=replicated_transition` and timing
`buffer_start_source` reveal this path. Update `modmain.lua`, `modinfo.lua`, new
`scripts/jev_execution.lua`, `action_buffer.py`, and `jev_agent.py`; reload the Mod
and restart Python. Schema remains 10.

### 1.5.5 juicy berries

The correct bush prefab is `berrybush_juicy`, not `juicyberrybush`. The Mod now
exports explicit pickability using `CanBePicked()` when available and the
replicated `pickable` tag. Candidates require affirmative pickability and a
verified route. Collection verifies `berries_juicy` inventory gain; discovery
and observed regrowth participate in exploration events. Loose raw and cooked
juicy berries are collectible. Both `berries_juicy` and `berries_juicy_cooked`
are safe food; raw juicy berries can be cooked, including by the existing
cook-then-eat composite near a lit fire. Update Python and reload the updated Mod.

### 1.5.4 boundary and gateway fixes

Frontier candidates and boundary completeness share one spatial definition. From
observed land in the current topology room, a flood through adjacent observed,
passable unlabelled land finds cells bordering genuinely unobserved space. It
never crosses sea or another known room. Transit land receives no fabricated
room ID and does not inflate room area. Fully observed unlabelled land is not
itself new space; completeness remains grid-relative spatial coverage, not a
guarantee that all topology labels are resolved.

Gateway IDs retain an approach/crossing phase. Reaching the source side or
passing it within the entrance corridor latches movement toward the destination
side, rather than turning back when the source-side arrival radius is exceeded.
Moving more than eight units from the source resets approach; a confirmed room
change clears old phases. Terrain and hazard checks still apply to every leg;
a blocked crossing is not replaced by a reverse leg toward the source. Update
`modmain.lua`, `modinfo.lua` and `scripts/jev_exploration.lua`, then reload the Mod.
Telemetry remains schema 10.

This package connects JEV decisions to a bounded Don't Starve Together client Mod.
It exports game state and can execute only the actions explicitly listed below.

## What it exports

Once per second, the client mod writes one JSON record to `client_log.txt` with:

- day, phase and world time;
- health, hunger and sanity;
- inventory and equipped items;
- whether a torch or campfire can be crafted;
- nearby useful items, resources, light sources and threats.
- observed regions, persistent exploration headings, targets and current legs.

Short records start with `[JEV_DST_STATE]`. Records longer than the safe game-log
line size are emitted as numbered `[JEV_DST_CHUNK]` lines and reassembled by
Python. A trailing tab added by the game log is removed from each part first.
The controller and watcher accept only complete, valid JSON records;
truncated lines left by older Mod versions are ignored.

### Action handoff and latency

Version 1.4.3 advertises `telemetry.on_demand`. After observing this capability,
the controller uses the reserved F2 key to request an immediate snapshot instead
of waiting for the next heartbeat. A fresh-state wait requests one snapshot;
semantic completion checks request updates at most about every 0.2 seconds and
poll the log every 0.02 seconds. Older mods continue using their periodic records.

The log reader parses only appended data, retains partial lines and chunked
frames, and detects truncation or replacement. Focusing an already foreground
game no longer adds a delay. Collection waits for an actual outcome after one
Space input so rapid snapshots do not cause repeated interaction during an
unfinished animation. Movement still requires a post-release observation.

Version 1.4.4 enables next-decision prefetch in execution loops. Single decisions
and dry runs remain sequential. `decision_time` reports full request latency;
`cycle_timing` reports actual blocking time. The loop cooldown remains zero and
DeepSeek planning runs in the background. Update and reload the mod for snapshots
and coastline detours.

### Next-decision prefetch

Exploration is now event-driven: movement checkpoints obtain fresh observations
for safety and event detection only, not per-leg model requests. Exploration does
not prefetch, including in its completion callback. After an event and cleanup,
the next cycle observes fresh state before requesting JEV, including newly found
resources in its candidates.
`--no-prefetch` does not disable event detection.
Collection, eating, cooking and basic tool crafting may also submit after their
completion condition is observed; short actions provide little overlap. Long
work actions do not prefetch when chopping or mining first starts. Background
threads never send game inputs. A prefetched decision may still choose exploration;
the restriction concerns when a request starts, not which next action JEV chooses.

Version 1.6.0 adds a two-stage pipeline: one executing action plus one buffered
next-action request. `execution.doing` (or a recent 1.6.1 `confirmed_start`) and native PICK/PICKUP target GUID or BUILD
recipe must confirm the action actually started, not merely a keypress or an
approach. `action_buffer.py` predicts the successful inventory/target outcome
from the pre-input snapshot and explicitly labels that input as a prediction.
Supported predictions are grass, saplings, normal/juicy berry bushes, loose items
with known stack sizes, and axes/pickaxes. Exploration, torch/fire construction,
chopping/mining, and food composites are not predicted. Missing start telemetry
falls back to existing verified-completion prefetch.

A predicted result cannot be consumed before the running action succeeds and
actual inventory quantities/equipment placement match. Stack merges are harmless;
unexpected character-specific yields, recipe costs, or auto-equipping invalidate
the prediction. New meaningful resources or observed regrowth, region discoveries,
damage, threats, phase changes, and crossing the critical-hunger boundary also
invalidate it; simple re-entry of a previously known resource does not.
Logs expose `jev_buffer_predicted`, `jev_buffer_confirmed`, discard reasons,
`buffer_stage`, and `buffer_validation`, alongside existing overlap timing.
Update `action_buffer.py`, `decision_prefetch.py`, `controller.py`, `jev_agent.py`
and reload the 1.6.0 Mod (`modmain.lua`, `modinfo.lua`). Schema 10 gains optional
`execution` and loose-item `stack_count` fields; older Mods cannot confirm starts.

At most one prefetch is retained, with a three-second lifetime from submission. Phase, inventory,
health, threat and position changes invalidate it. Before execution, both normal
and prefetched decisions are matched against freshly generated legal candidates
by collection GUID, destination node or local frontier identity. Unknown-exit
search is matched by its source region rather than a changing temporary waypoint.
An area's
list index cannot silently change the chosen destination. Invalid results fall
back to a new JEV request; the controller never selects a replacement action.
Use `--no-prefetch` for a serial baseline.

Timing records include `decision_source`, `prefetch_request_ms`, remaining
`prefetch_wait_ms`, and overlapping `prefetch_hidden_ms`. `prefetch_timing` stores
the worker's separate API breakdown; do not add it to main-thread elapsed time.
Slow requests and frequent state changes can still cause pauses.

### Collection paths and temporarily unavailable targets

Version 1.4.5 emits schema 9. Collectible entities carry `collect_path`, including
`status`, `reason`, verified route `distance` and the next `waypoint`. `ready`
requires a continuous passable land route, not just a passable destination.
`blocked` means the target's ground is impassable; `unknown` means no safe route
was found in current observations. A blocked direct segment can detour through
observed passable cells across terrain types while avoiding known threats. The
2048-cell search tree is built at most once per snapshot for all collectible
targets. Unknown routes are reconsidered on later observations.

Only ready, unpaused targets become JEV collection candidates. A nearby blocked
target does not hide a farther reachable item of the same prefab. Candidates
include both straight-line and route distances; JEV and DeepSeek state summaries
include route status and reason. Execution follows live waypoints and measures
progress toward the waypoint, allowing detours to temporarily increase distance
to the resource. Automatic work-drop pickup uses the same filtering.

Two stalled movements, backtracking, exhausting the approach step budget, or an
interaction verification timeout pause the player/target GUID pair for 30 seconds
and skip the action without global error backoff. Moving the player six units,
moving the target two units, or expiration allows reconsideration if a route is
ready. Logs include the pause reason and approach waypoints. Losing a route is
not treated as completing collection. Update both Mod and Python: targets with
missing route data are not exposed. Lack of a currently known route does not
prove permanent unreachability.

Approach pulses are capped at 0.5 seconds, with at most 1.5 seconds waiting for
post-release telemetry. A feedback timeout pauses the target with
`approach_feedback_timeout` rather than entering global retry backoff.

### Per-stage timing logs

Each cycle appends a record to `logs/timing.jsonl` beside the script and prints a
`cycle_timing` summary. Add `--timing-details` to print individual spans, or use
`--timing-log .\logs\my-run.jsonl` to change the destination.

Records include `run_id`, `cycle`, UTC timestamp, selected action, and timings for
observation, planner updates, candidates, JEV requests, execution, window lookup
and focus, keyboard holds, fresh-state waits, completion checks, cooldowns and
retry backoff. API timing separates response headers, body reading and JSON
parsing. Header latency includes connection, network and server processing;
it does not isolate model inference. Work actions distinguish acknowledgement,
completion and drop collection; collection approach spans include step, distance
and target GUID.

`totals` contains call counts, inclusive `total_ms`, exclusive `self_ms` and
`max_ms` per stage. `spans` contains start offsets, durations, parent IDs and
outcomes. Log polling is aggregated without a span per poll. Detail is capped at
512 spans per cycle; `dropped_spans` reports omitted detail. Parent times include
child times, so action, fresh-wait and verification times must not be added
together. Verification includes animation and state feedback. Cycle totals
include cooldown or backoff but exclude timing-log output itself.

Errors, timeouts, interrupts, dry runs, abstentions and skipped actions are also
recorded. Error records include exception types, not messages. Credentials,
headers, full state and model text are not recorded. Logs are Git-ignored;
file-write failure prints a warning without stopping control. Inspect the latest
cycle's aggregates with:

```powershell
(Get-Content .\logs\timing.jsonl -Tail 1 | ConvertFrom-Json).totals | Format-List
```

## Install the mod

Copy the entire `jev_dst_agent` folder into DST's `mods` directory. On this PC,
the detected destination is:

```text
C:\Program Files (x86)\Steam\steamapps\common\Don't Starve Together\mods\jev_dst_agent
```

Start DST, open **Mods**, enable **JEV DST Agent**, apply the changes, and enter a
local/private world.

## Watch live state

Open PowerShell in the project root and run:

```powershell
C:\Users\16425\anaconda3\python.exe .\watch_state.py
```

For the complete JSON records:

```powershell
C:\Users\16425\anaconda3\python.exe .\watch_state.py --full
```

## Bounded controls

`controller.py` supports movement, collection, crafting, work, eating, cooking and combat operations:

```powershell
C:\Users\16425\anaconda3\python.exe .\controller.py status
C:\Users\16425\anaconda3\python.exe .\controller.py move up --seconds 0.25
C:\Users\16425\anaconda3\python.exe .\controller.py interact
C:\Users\16425\anaconda3\python.exe .\controller.py collect grass
C:\Users\16425\anaconda3\python.exe .\controller.py cook
```

`collect` selects the nearest matching observed prefab, approaches it through
short WASD pulses, waits for fresh telemetry after every pulse, then presses the
ordinary action key. `stop` releases every movement key if a run is interrupted.
JEV's safe loose-resource candidates include flint, logs and rocks.
Collection approaches to within one world unit before pressing Space. Success is
verified when a source loses its `pickable` tag or a loose item disappears; if
the interaction does not complete, the controller retries instead of advancing
the JEV loop.

Grass, saplings and berry bushes expose an explicit live `pickable` value and
are omitted from JEV's candidates while empty. Immediately before Space, the
controller waits for a newer state and checks the same value again. A matching
inventory gain (cut grass, twigs or berries) also proves that collection has
finished, even though the harvested plant entity remains in the world.
The chosen collectible's GUID is carried into execution, so a nearer depleted
plant cannot replace the live target. If that GUID becomes unavailable before
execution, the controller falls back to the nearest currently collectible plant
of the same prefab.

## Offline automatic tests

Run the complete regression suite without launching DST, calling JEV or sending
any keyboard input:

```powershell
C:\Users\16425\anaconda3\python.exe .\run_tests.py
```

The suite uses simulated telemetry and mocked game input. It covers depleted
versus live resource selection, GUID handoff, fallback selection, and collection
completion when inventory increases while the harvested plant remains visible.

Collection movement uses distance-scaled pulses: up to one second while far
away, braking to 0.04 seconds near interaction range and requiring post-release
telemetry before correcting direction. An active pursuer within
five world units interrupts collection immediately. When pursued at close range,
an unarmed character receives only a flee action; a carried weapon can be
equipped, and attack is exposed only while a weapon is already equipped. Fleeing
remains available during night and critical hunger. Only entities marked
`activeThreat` expose fleeing. During pursuit, the Mod probes the eight actual
WASD directions with `IsPassableAtPoint` for up to six world units. The
controller picks a passable route that increases separation, including a
sideways route along a coastline when straight away is blocked. After each
bounded burst it checks real position change and avoids a direction that made
almost no progress. If no passable direction remains, it stops with an error
instead of repeatedly running into the boundary. It stops after two consecutive
clear observations or eight bursts. This requires Mod 1.4.1 telemetry with
`navigation.escape_routes`; updating Python alone is insufficient.

Exploration also avoids recently observed hostiles. The Mod marks entities
with DST's `hostile` tag as `potentialThreat` even before they attack and
remembers their positions for 90 seconds. Frontiers within 14 units are
excluded; those 14–26 units away receive a distance-based score penalty.
Dangerous persistent targets and known-region legs are withheld.
Immediately before every movement pulse, the controller checks the whole leg
against fresh and remembered threats, preventing a post-flee return toward the
same enemy. Hazard memories expire instead of blocking an area permanently.

Light management is phase-driven: the equip action is hidden during day and
dusk, while unequipping remains one option among JEV's other actions. At night,
a torch is equipped only when no lit campfire or firepit is within eight world
units. Campfires can be built only at night; while beside a lit campfire or
firepit, the equip action is hidden and unequipping remains available without
forcing it. Movement away from light while unequipped remains blocked unless
fleeing an immediate hostile.

Torch actions are semantic Mod operations with post-action verification:

```powershell
C:\Users\16425\anaconda3\python.exe .\controller.py craft-torch
C:\Users\16425\anaconda3\python.exe .\controller.py equip-torch
C:\Users\16425\anaconda3\python.exe .\controller.py unequip-torch
```

The JEV candidate list contains `craft_torch` while telemetry reports that the
recipe is craftable and hard safety rules allow it, including when torches are
already carried or equipped. Its description includes the current total so JEV
can weigh reserve needs against material costs. Local board execution can advance
a reserve goal from 1/2 to 2/2, then stops automatic crafting at the target.
`equip_torch` appears only
at night while a torch exists, is not equipped, and no lit campfire or firepit
is nearby. `unequip_torch` appears during day or dusk while a torch is equipped,
and at night when a lit campfire or firepit is nearby.
After `craft_torch` succeeds, the controller checks whether DST auto-equipped the
new torch. If it is still day or dusk, it unequips the torch within the same
action to save fuel; if night has begun, it leaves the torch equipped. JEV need
not choose a separate unequip action for this cleanup.

Additional bounded actions:

```powershell
C:\Users\16425\anaconda3\python.exe .\controller.py craft-axe
C:\Users\16425\anaconda3\python.exe .\controller.py craft-pickaxe
C:\Users\16425\anaconda3\python.exe .\controller.py chop
C:\Users\16425\anaconda3\python.exe .\controller.py mine
C:\Users\16425\anaconda3\python.exe .\controller.py eat
C:\Users\16425\anaconda3\python.exe .\controller.py cook
C:\Users\16425\anaconda3\python.exe .\controller.py build-campfire
C:\Users\16425\anaconda3\python.exe .\controller.py attack
C:\Users\16425\anaconda3\python.exe .\controller.py equip-weapon
C:\Users\16425\anaconda3\python.exe .\controller.py flee
```

Attack is exposed only when telemetry sees a valid hostile/monster target and a
weapon is already equipped, then sends DST's native `Ctrl+F` command; it
does not depend on mouse coordinates or the Mod's F10 bridge. Campfire construction follows DST's
native two-stage flow: buffer/craft the recipe, then place it. Chop and
mine now continue until the chosen target is completely harvested. Their JEV
actions are exposed only when telemetry proves that a matching basic tool has
enough remaining durability for the full job: normal trees are budgeted
conservatively at 15 chops; common rocks use their game-defined 6/4/2 strikes.
After a target is fully worked, the controller picks up up to twelve nearby
drop stacks within four units: known products such as logs, pinecones, acorns,
rocks, flint, nitre, and gold, plus other loose items newly seen after this
work action. Unrelated items already there are not automatically collected.
The Mod reports its actual
target GUID, position, sequence, and completion status in telemetry; the
controller no longer selects a separate target for verification. It stops
gathering for a nearby pursuer or unsafe night travel.
Eating uses a conservative early-game food whitelist, including cooked meat
and cooked morsels produced by the cooking action.

`cook_food` is exposed only when a lit campfire or firepit is within six units
and the inventory contains raw meat, morsels, berries, or carrots. The Mod
rechecks the `cookable` tag and invokes the game's native `COOK` action for one
item. The controller verifies that a cooked food item appeared in inventory.
Already-cooked and hazardous ingredients are not included.

When JEV chooses `eat_safe_food`, existing cooked food is eaten directly. If
only raw cookable food is carried and a lit cooker is within six units, the
controller cooks one item, verifies the cooked result, and then eats one item.
If cooking fails but safe raw berries or carrots remain, it eats those rather
than repeatedly waiting to cook. Raw meat is not used as a fallback.

## Candidate-pruning principle

Candidate pruning does not choose an action on JEV's behalf. Its purpose is to
remove actions that are unavailable, contextually meaningless, or excluded by
an explicit safety constraint. JEV still ranks and selects among all remaining
meaningful actions.

For example, an equipped torch during day or dusk exposes `unequip_torch`
because putting it away preserves fuel, while `equip_torch` is omitted when no
torch is equipped because carrying a lit torch in daylight provides no useful
benefit. At night the relationship reverses when there is no other light source;
beside a lit campfire or firepit, equipping a torch is again omitted as
redundant. Similarly, depleted plants, unavailable recipes, insufficiently
durable tools, and exploration legs rejected by the world map API are not shown as
candidates. The pruning layer determines which operations currently make
sense; it does not decide which sensible operation is best.

## JEV decision

JEV makes fast next-action decisions; it does not generate the daily plan.
With `DEEPSEEK_API_KEY` in `.env`, the `--loop --execute` controller requests a short DeepSeek plan
in the background for each quarter of a game day, using normalized `world.time`
windows 0–25%, 25–50%, 50–75%, and 75–100%. Starting mid-day requests only the
current window. The default model
is `deepseek-flash`; `DEEPSEEK_MODEL` and `DEEPSEEK_API_URL` are optional overrides.
The planner receives current telemetry, up to 40 recent action outcomes from
this Python run, and the local [DSTKnowledge.txt](../DSTKnowledge.txt) reference.
The reference contains common recipes checked against the installed game's
recipe script; it is reread for every plan and can be edited. The repeating
per-second game log is not sent. The structured board is passed as
`task_board` state once ready (legacy plans still use `daily_plan`). Local execution
can then advance feasible board tasks; JEV continues deciding while planning
is in progress; live facts, available actions, and hard safety rules take
precedence. Two consecutive failures of the same action or successfully building
a science machine trigger a same-window replan, at most once per 60 seconds.
Length truncation or empty JSON may trigger one bounded background retry;
if planning still fails, JEV continues without a plan and tries again the next quarter.
Previous-quarter plans are not passed to JEV, and late replies are discarded.
History is in memory only and resets when the controller restarts.
The terminal shows the day, quarter, loaded knowledge length, DeepSeek request
time and the first 240 characters of
the validated plan JSON; longer previews end with an ellipsis. Retries and
final errors show the API finish reason when available and the request time without logging the
raw response or API key.

When all board tasks are `done` in the latest observed state, the planner also
requests a new board immediately within the current quarter. It logs
`task_board_completed` then `daily_plan_requested ... reason=all_tasks_completed`.
The request includes current telemetry and the completed board as history, with
instructions to propose unmet goals. The old board is withdrawn from JEV's
guidance while remaining visible as completed history in CLI/logs. JEV continues
without waiting. Each revision triggers once and only one planning request may
be outstanding. Completion-triggered requests are at least 15 seconds apart
(the first is immediate), preventing repeated already-satisfied replies from
spamming the API; normal quarter changes bypass this guard. Predicted, empty,
partially complete, blocked, or dead-player boards do not trigger completion.
Failures retain the existing no-per-cycle-retry policy until a new window.

### CLI task board

DeepSeek emits `goal` and 1–6 `tasks`, each with an ID, title, priority (1 highest,
5 lowest), typed target, dependencies, and optional action hints/reason. Targets
cover current inventory, hunger ratio, known-region count, visiting known rooms,
and supported structures. Duplicate IDs, missing/cyclic dependencies, and
unsupported target types are rejected. Model-supplied statuses/progress are ignored.

Inventory targets use the supported product catalog in `task_board.py`, not world
entity names or English display names. Trimmed/lowercase names are checked, with
the explicit alias `logs` normalized to `log`. Valid plural IDs such as `rocks`
and `twigs` stay unchanged; no fuzzy singularization is performed. Unknown or
currently unsupported inventory names fail plan validation and use the existing
bounded retry policy instead of leaving an unmatchable task. IDs, titles and
dependencies are unchanged. DeepSeek receives the same catalog and alias map;
progress evaluation and execution use canonical names. Sync `task_board.py`,
`daily_planner.py` and `jev_agent.py`, then restart Python; no Mod update is needed
for these fixes.

The runtime derives ready/in_progress/blocked/done from telemetry; in_progress
means partial measured progress, not an instruction to execute a task. Missing
dependencies or telemetry block tasks, and quarter changes expire the board.
Inventory goals refer to current stock, not lifetime collection. Verified science
machine construction remains known for this run even after leaving it. The next
planner request includes the previous board and its progress.

The terminal renders a plain `TASK BOARD` table with IDs, priorities, statuses,
progress, titles, and blocker details. Chinese width is supported; it neither
clears the screen nor overwrites action logs, and needs no UI dependency. Status
and revision changes display immediately; numeric-only progress is throttled to
three seconds. Full snapshots go to `logs/task_board.json`, with changes appended
to `logs/task_board.jsonl`. Run/board IDs enable correlation without API secrets;
timing records include `task_board_id` and `ready_task_ids`.

Execute mode first advances local board tasks as described in 1.7.0. When no
local task is executable, or safety requires a handoff, JEV selects freely among
all legal actions; the board adds no candidate filter. Predictive buffering
recomputes a separate `state_basis=predicted_success` board view without mutating
observed progress or enabling automatic execution. A board revision/withdrawal
invalidates old buffered decisions, and starting local execution discards older
prefetches. Full-board completion still triggers replanning as described above.

The JEV prompt separates hard constraints from decision guidance and describes
each candidate as one complete bounded action. Chopping/mining include nearby
drop pickup; `eat_safe_food` can cook and then eat; `cook_food` alone prepares
food for later. The state includes hunger fraction, safe-food count and nearby
cooker availability. Guidance does not impose a fixed action ranking or choose
on JEV's behalf.

Put the TypeSafe key in `.env` (see `.env.example`). A decision is dry-run by
default:

```powershell
C:\Users\16425\anaconda3\python.exe .\jev_agent.py --show-probabilities
```

Add `--execute` only after reviewing a dry run:

```powershell
C:\Users\16425\anaconda3\python.exe .\jev_agent.py --execute
```

For continuous autonomous control, use:

```powershell
C:\Users\16425\anaconda3\python.exe .\jev_agent.py --loop --execute
```

The loop has no cooldown by default and senses again immediately after each
completed action. Use `--interval 1` or another value up to 60 seconds to add a
cooldown. Errors still use a minimum one-second exponential retry delay to avoid
a tight failure loop.
Long actions such as chopping, mining, collecting and building finish before the
next cycle begins, so actions never overlap. Day 2 is not a terminal state: the
loop continues indefinitely and stops only on death or when interrupted with
`Ctrl+C`. Transient errors retry with bounded exponential backoff.

### Topology regions and unknown-exit search (1.5.0)

Schema 10 keys regions by `GetTopologyIDAtPoint` alone, not topology plus tile
type. Mixed ground inside one room stays one region; separate rooms with the
same ground type are not merged. Only nearby terrain, topology and
passability are sampled, not the unexplored world map.

JEV can choose all three categories concurrently:

- `explore_current_region`: search the current topology room for local resources.
- `explore_other_region`: investigate unknown exits directionally, even
  when other regions have already been discovered.
- `travel_region_N`: go to a discovered region, including previously visited
  regions. There is no separate return action.

`navigation.graph.current` includes terrain composition, actual resources,
entries, observed area and boundary status. Names such as `forest 1` and
`grassland 2` retain assigned numbers when routes reorder. A second classified
terrain comprising at least 20% of observed terrain can refine a label to
`mixed N`; it then stays mixed. The topology ID never changes. Observed area
counts unique passable four-unit cells times 16 square world units, not square
meters, total area or a completion percentage.

`boundary_complete` means every neighbor of the region's observed land cells
is known at this grid resolution. Unknown cells and passable neighbors with
unidentified topology keep it incomplete. Failed routing or danger never
establish completeness. A complete boundary hides unknown-exit search, not
known-region travel. Narrow sub-grid exits can still be missed.

Observed passable gateways or verified crossings connect regions, with up to
three gateways per pair. `navigation.graph.routes` unifies known destinations,
including multi-hop routes through intermediate rooms. Choices distinguish
observed resources from terrain priors and report entered status, observed
area, boundary state and hops. Distance is to the NEXT exit, not a fabricated
total distance to a remote destination. Future historical connections may no
longer be safe; the next leg is revalidated at execution.

`scripts/jev_exploration.lua` retains an outward heading across collection and
crafting. An eligible frontier supplies the initial heading. When forward and
laterally offset paths are blocked, it follows a fixed
boundary side, resuming the original heading when forward opens. This is a
local geometric fallback, not proof of a coastline. Recent trajectory loops
pause the old target for 30 seconds and reconsider direction.
`navigation.search` reports heading, strategy
and cumulative displacement while the intent exists, including incidental
movement rather than only deliberate exploration.

JEV chooses the intent and destination; navigation only proposes executable
legs. One exploration choice continues through verified legs of at most four
units until an event, without periodic time, distance or leg-count decisions.
Legs are sensing/navigation checkpoints, not model-decision boundaries.

Events include an enemy targeting the player (even an already-visible enemy),
a new potential threat or an approaching known threat at dangerous range,
health loss, death, lost night lighting, phase changes, critical hunger with
safe food carried, region discovery/entry,
destination arrival, path failure, stalls and backtracking. Stopping never
automatically selects collecting, fighting or fleeing instead of JEV.

New resource events require a supported, currently collectible resource with
a verified reachable route. Player/resource GUID memory suppresses repeated
events from known berries or items leaving and re-entering view. Depleted plants
and unreachable items do not interrupt. Memory keeps up to four players and
approximately 8192 resources per player before rebuilding; process restarts or
regressing game time reset it. Discovery during a model request is compared
against what the model actually observed, not silently treated as declined.

Known plants explicitly observed with `pickable=false` later trigger one
`resource_available_again` event when collectible and reachable again. Normal
state reads record depletion during collecting/crafting as well as exploration.
Absence, unknown pickability and route failures do not imply depletion. Regrowth
waits for a usable route; regrowth already included in JEV's decision snapshot
is acknowledged rather than repeatedly interrupting. Depletion timestamps keep
stale decisions from consuming newer events. A wholly offscreen harvest/regrowth
cycle cannot be inferred without an observed depleted state.

`exploration_yield reason=... prefab=... guid=... legs=... distance=...` reports
why control returned. Timing includes `exploration_event`, leg count and distance.
`exploration_events.py` contains detection policy. This event-driven update only
requires the Python files; the schema 10 Mod remains compatible.

Local current-region frontiers are passable cells of the current topology room
adjacent to unknown cells. Their baseline score is:

```text
score = information_gain * 10 - distance * 0.5 - visits * 6
```

The highest-scoring reachable local target survives collection and crafting
until arrival, a region change or path failure. Unknown-exit search separately
scores heading alignment, information gain, visits, distance and boundary side,
trying at most 24 candidates per frame. Known-land detour trees visit at most
2048 cells. Both paths reject remembered threats.

Every leg is sampled at 0.75-unit intervals with `IsPassableAtPoint` and checked
again for threat distance and night lighting before input. Arrival allows 1.5
units rather than exact coordinate corrections. Movement uses a nominal six
units/second with braking; `--move-seconds` scales and caps pulses, default 1,
range 0.2–2.0. F1 feeds back stalled unknown-search waypoints; two stalled legs
pause the relevant search point without changing the local-survey target.
Known-region routes also detect repeated backtracking and pause for 30 seconds.
Road scanning, road graphs, road-waypoint following, F4 feedback and the
road-discovery interruption were removed in 1.5.2. Old road telemetry does not
create choices, and legacy road-following actions are rejected before input.

Timing records include region ID, name, observed area, search strategy and
movement endpoints. Memory resets on world reload. Update Python, `modmain.lua`,
`modinfo.lua` and the ENTIRE `scripts` directory together, then re-enter the world.
Python tests use `python.exe .\run_tests.py`. Lua simulations use
`npx.cmd --yes --package=fengari-node-cli fengari tests/exploration_spec.lua`
(first use downloads a runner). These verify pure policy and Lua compilation,
not actual DST-engine behavior.

### Diagnosing wait-only states (1.5.1)

Confidence 1.0 can mean `wait` was the only candidate, not that JEV preferred it
over useful alternatives. Such decisions print `only_wait_available`; timing
stores `decision_candidates` with actual supplied action keys, frontier/search
status, known-region/route counts and path diagnostics. `candidate_rejections`
records Python safety-filter reasons. Decisions with alternatives are not
misreported as wait-only.

`navigation.diagnostics` counts frontier, path-tree, gateway, exit-search and
hazard-memory outcomes. `direct_hazard`, `no_observed_start`,
`no_safe_observed_gateway_path` and `search_budget_exhausted` distinguish hazard
avoidance, missing observations, absent safe paths and bounded search; none
proves permanent physical unreachability.

Hazard-blocked direct legs now attempt safe detours too, not just terrain-blocked
legs. Frontier targets are not discarded merely because their straight line
crosses danger. Detours may cross observed topology rooms: labels are not walls,
and actual region entry still yields to JEV. Frontiers, gateways and collection
share a per-frame safe-land tree with the existing 2048-cell bound. Passability,
14-unit hazard clearance and 20-unit approach checks remain, with execution-side
revalidation. A truly unavailable safe path can still leave only waiting.

The existing 90-second hazard lifetime is unchanged. Earlier removal requires
reobserving the old position inside the scan radius with an untruncated entity
list and no matching GUID, or observing that the entity is no longer threatening.
Unseen remote enemies retain their memory. Dynamic danger is not misclassified
as terrain failure for the 30-second frontier blacklist. Update Mod (including
`scripts`) and Python together and re-enter the world for this fix.

### Path advancement and stable gateways (1.5.3)

Frontier, known-region and collection detours share ordered-path lookahead:
choose the furthest safely visible waypoint within eight units, not merely the
closest BFS root, which can be behind the player. Exploration legs remain at
most four units. Every shortcut verifies the complete segment for passability
and hazards; it cannot cut across water or enemies.

Each source/neighbor pair retains its last valid gateway through small distance
changes. Paths and safety are recalculated every snapshot, and an invalid gateway
is replaced by another valid candidate. Multi-hop routes carry `gateway_id`,
also recorded in timing. Controller backtracking/stall pauses now yield
`route_paused`, distinct from missing Mod paths (`path_unavailable`). Navigation
continues only JEV's chosen intent; it does not choose a replacement destination.

Equipping a torch uses the same inventory-tile action as manually clicking the
torch, rather than the action-system auto-equip helper.

Safe resource collection has no confidence gate. The JEV prompt also
explicitly prefers unequipping a torch during day and dusk to preserve its
durability.

Below 30% hunger with safe food already carried, waiting, exploration and
optional gathering are omitted as dominated choices. JEV still selects from
the remaining meaningful actions: eating, and escape or urgent night lighting
when applicable. Both ordinary and emergency eating have no confidence gate.
Without carried food, exploration remains available to seek it.

Only attacking retains a confidence threshold, defaulting to `0.45`.
All non-attack actions use `0.00`; legality, path and safety checks remain active.
Confidence is still logged. `--min-confidence` overrides attacks only.
Exploration and collection
without an equipped torch at night are blocked even beside a lit campfire: a
stationary fire does not illuminate the full route. The controller rechecks
during movement, stopping if night falls mid-route. Fleeing from an immediate
hostile remains permitted. Attack is unavailable while unarmed. `.env` is ignored by Git and
deliberately excluded from release archives.
