# Persistent priority goals and source navigation

[中文使用说明](MCP.md) · [English setup](MCP.EN.md)

## Model contract

```json
{
  "goal": "Gather basic supplies",
  "tasks": [
    {"id":"grass","title":"Gather grass","priority":1,
     "target":{"type":"inventory","prefab":"cutgrass","count":2},
     "search_budget_seconds":120,"search_budget_distance":240},
    {"id":"stone","title":"Gather rocks","priority":2,
     "target":{"type":"inventory","prefab":"rocks","count":6}},
    {"id":"twigs","title":"Torch twigs","priority":1,
     "target":{"type":"inventory","prefab":"twigs","count":2}},
    {"id":"torch","title":"Prepare portable light","priority":1,"focus":true,
     "target":{"type":"inventory","prefab":"torch","count":1},
     "depends_on":["grass","twigs"],"reason":"Minimum light before reserves"}
  ],
  "cancel_task_ids":[]
}
```

One to six tasks; priority 1 is highest. The host adds `kind=task_board`,
`schema=2`, `execution_policy=priority_goals` and a revision ID.
No implicit recipe solver creates prerequisites. Keep urgent minimum ingredients
separate from optional reserves; a torch must not wait for campfire/spare stock.

Tasks accept `focus: true/false` (default false), with at most **two** focused
tasks in a submitted or merged board. While any focused task is unfinished,
only focused tasks and their unfinished transitive prerequisites may run or
interrupt navigation. Dependencies need not themselves be marked focused.
Blocked focus stays exclusive during review/cooldown; safety still overrides it.
When all focused tasks complete, normal opportunistic scheduling resumes.
Promote/demote a stable task by resubmitting it with `focus: true/false`;
omitted tasks retain their mode. Existing completion/search progress is retained.
The CLI marks `[FOCUS]`; logs include `focus_task_ids` and `execution_task_ids`.

Supported targets:

| Type | Completion condition |
| --- | --- |
| inventory | Current carried/equipped stock reaches `prefab,count` |
| structure | Campfire or Science Machine observed/verified built |
| hunger_ratio | Observed hunger fraction reaches `at_least` |
| visit_region | Player actually enters known `node_id`; historical entered flag is insufficient |
| survey_region | Reveal `cells` additional observed passable map cells in known `node_id`, or finish its boundary survey |
| known_regions | Observed topology-node count reaches absolute `count` |

Completed goals latch from observed state only; consuming ingredients does not
restart them. A changed target reopens a goal. Native crafting currently offers
a torch/axe/pickaxe only when none of that prefab is carried: these goals must
request count=1. Campfire building waits for night and no nearby lit campfire;
unavailable recipes are not evidence that materials disappeared. Equip/unequip
and emergency lighting remain local safety decisions, not timed wait goals.

## Scheduling and navigation

1. Restrict ready goals to the focus/prerequisite scope when focus is active.
   Otherwise scan all ready goals by priority for an offered native collection/work/craft,
   construction or eating action. A missing priority-1 source does not block a
   visible priority-2 goal. Real dependencies still block dependent tasks.
2. If none is executable locally, navigate for the first ready navigable goal.
   Known concrete source positions in the current room precede room surveys.
3. Prefer recorded source rooms, then Klei room-generation priors for known nodes,
   then unknown topology branches. Verified graph destinations are ordered by
   BFS hops and next-exit distance, not an invented exact full-route cost.
4. Board navigation carries `navigation_targets`: local targets from ALL ready
   goals in the current execution scope, excluding latched completion, unsatisfied dependencies and search blocks.
   Lua continues through unrelated resource appearances/disappearances. After at
   least four units of displacement, an offered local action that advances a
   still-unmet target yields with `goal_action_available`; this includes pickups,
   sufficient-tool/durability tree or ore work, crafting, construction and food.
   No collectible type-set change is required for tree/ore discovery. The host
   reselects by board priority without JEV. New room discovery alone does not stop
   a board walk; destination, path and safety events remain in force.
   While a walk runs, the host cancels and waits for acknowledgement when the
   goal/ready targets change or existing room/global search budgets require a
   review; continuous movement must not postpone a planner update for 120 seconds.
   A pending cancel survives still-running Lua reports and is redelivered until
   Lua reports a terminal outcome; it never releases ownership early.
   DS-gap navigation omits the target filter and retains resource-set events/JEV.
5. JEV runs only for planning gaps or safety: targeted enemies, damage, lighting
   changes/unsafe night light, and known health, hunger or sanity below 30%.
   Hunger triggers even without carried food; unknown vitals are not danger facts.
   Lua interrupts running work when a vital crosses the threshold. If JEV chooses
   native `wait`, an unchanged danger is acknowledged so safe board work can resume.
   New remedies, a worse 10% vital band, new targeted enemies or native safety
   interrupts re-arm the decision. Useful remedies continue to receive decisions.
   Lua owns
   live legality, passability and movement verification; Python never invents
   coordinates or bypasses the offered-action snapshot.

Lua source memory retains at most 512 observed entities per world session and
offers diverse current-room resource routes (at most 24 types). It records positions,
pickability and source identity. A complete local scan can invalidate a disappeared
source; capped scans cannot. Regrowth requires fresh positive observation.
Out-of-room concrete targets are reached through room routing first. No save-file
or whole-world entity inspection reveals undiscovered resources.

`hunger_ratio` includes gathering and source navigation when no edible food is
carried. It first uses offered `eat_safe_food`, otherwise collects offered safe
food (berries, planted carrots, seeds or edible drops), then searches for any safe
food source through the same position/room/prior/unknown-branch navigation. It
re-evaluates after each pickup/eat and completes only at the observed hunger
fraction. Search budgets and safety legality remain in force; visible work for
other goals still runs before navigation. This does not add hunting or relax
the safe-food list. Planted carrots export live `pickable` state, using replicated
tags on clients and `CanBePicked()` when the server component is available.

## Search bounds and recovery

Known-region travel validates both entrance endpoints and the full approach, using
safe observed-land detours when the direct approach is blocked. A safe short leg
alone does not establish that an entrance is reachable. Once travel starts, the
selected destination keeps its first hop while that entrance remains valid;
newly available shorter routes do not reverse the current trip. The commitment
retains the entrance's endpoint coordinates across incidental room crossings
during a detour. It resets on reaching the selected next-hop room or on a new
navigation goal, so passing back through a neighbouring room does not steer the
character back to the entrance it just left.

Lua retains a progress window across resource-event yields. If the remaining
path estimate to the next entrance has not decreased by two units for twelve
seconds of active navigation, and the body has travelled at least sixteen units
or remained nearly stationary, that entrance is paused for thirty seconds.
Model waits, local collection detours and topology-confirmation waits do not
consume this window. Improving detour path distance counts as progress even when
straight-line distance temporarily increases. `graph.route_pause` reports the
entrance and `route_no_progress` / `route_gateway_unavailable`. Room-label changes
along a committed detour do not reset the progress window. At the fixed crossing
endpoint, Lua waits up to three seconds for topology confirmation without
repeated zero-distance walks; expiry reports `region_confirmation_timeout` and
pauses only that entrance. A first failure allows other entrances to that
destination. Two entrance failures for the same goal/destination within 60 seconds
defer the destination for 30 seconds so other routes can run, then it becomes
eligible again. Neither entrance nor destination failures imply exhausted resources.

Per-task navigation budget defaults to 120 seconds / 240 distance units, with
allowed ranges 10–600 seconds / 8–2000 units. Gathering, planning waits and safety
handling do not deliberately consume that budget. A per-room resource survey is
deprioritized after 30 navigation seconds or 16 additional observed map cells. This is
negative evidence for that resource, not a ban on transit or other resources.
Fresh obtainable evidence can restore a source room's priority.

Exploration uses **grid-cell counts**, not walking steps or world area. Each cell
is 4x4 world units. A `survey_region` target accepts `cells:1..256`, default 16;
for example, former `area:512` is now `cells:32`. The priority executor counts
additional deduplicated observations from the task baseline, not already known
cells. Progress is displayed as `31/32`; a completed probe does not imply that
the whole region is explored. `boundary_complete` remains an independent fact.

Older `area` targets are normalized with `ceil(area/16)` into `cells`; supplying
both fields is rejected. Legacy ordered exploration steps use the same conversion.
Current Lua region snapshots expose `observed_cells` instead of `explored_area`.
Python can convert older area snapshots on upgrade and removes area counters from
model inputs, preventing double scaling. Seconds and route distance budgets retain
their existing units and values. Update Python/Lua together and restart both the
world and the Python controller after upgrading.

A transient native failure pauses its exact choice/target for 15 seconds and
lets routing or another visible goal proceed. It does not terminate the board or
prove the resource absent. Either time OR distance reaching its per-attempt limit
blocks further navigation for 15 seconds and requests review. Then the search
automatically retries without requiring a changed board or erasing surveys and
negative evidence; newly offered local work can resume sooner. Recovery releases
the retained intent and deprioritizes the current/intended resource room for 45
seconds, allowing alternate sources/branches first. Exact resource positions
always outrank a retained room-prior intent. `search_spent` is per attempt;
`search_retry` reports attempt, remaining cooldown and cumulative seconds/distance.

If no task can currently advance, the host immediately requests evaluation.
With unfinished focus, it retains control (`focus_pending`) and checks safety
while waiting for review/retry; without focus, JEV can take temporary local
control. Exact-choice failure pauses also apply during temporary control.
Missing paths are not completion.

## Asynchronous review

Every quarter is a background review opportunity, not an expiry. The active board
continues while DeepSeek responds. Reviews merge tasks by stable ID/target;
omitted tasks remain and cancellation requires `cancel_task_ids`. Dependencies are
revalidated after merge. Remaining tasks retain latched completion and unchanged
search budgets across reviews. A board with every task done, scheduled, failed or
blocked requests immediate review, including mixed states. Any ready/in-progress
task prevents this idle-board trigger. Reasons are `all_tasks_completed`,
`only_scheduled_tasks`, `no_actionable_tasks` (scheduled plus blocked work), or
`search_or_prerequisite_blocked`. Useful schedules remain while DS plans current
work, return travel or intentional residence. A reply that still leaves no runnable
work acknowledges waiting; new board revisions and changing telemetry do not
immediately trigger another request. Runnable work re-arms this check, and changed
idle task targets/timing/status can request a fresh review. Quarter reviews remain
available; failed requests retain the 15-second retry backoff. Reply acceptance checks current player/session and
observation continuity, not quarter equality; actual progress is re-evaluated.

Completed tasks omitted from a review leave the active board once no remaining
task depends on them. Explicitly retained completed tasks and completed
prerequisites stay; unfinished tasks always require explicit cancellation.
The **merged** board must fit six slots. The planner validates that merge before
accepting a response; errors include retained IDs and are sent back for one
bounded corrective retry, with the previous valid board kept on failure.

Query batches execute at most four calls. Excess calls receive tool error
results so the model can split them into later rounds, instead of aborting the
whole plan. Eight response rounds and the existing retry bound remain in force.
Validation failures, query-limit events and adoption/request errors are saved
to `logs/planner_errors.jsonl` as bounded diagnostic summaries without request
bodies or credentials.

Missing/empty/completed boards and boards with no feasible offered action or
route hand control to JEV while DeepSeek evaluates and updates the goals, unless
unfinished focus reserves control. A focus promoted during a pending optional
JEV request discards that response, even if the focused task is blocked. Only
one DeepSeek request runs at a time; failed requests retain the 15-second retry
backoff while local control continues. Useful future goals remain on the board.
Temporary navigation uses free exploration rather than a currently unexecutable
goal. When the old board becomes actionable or a usable update arrives, automatic
execution resumes after the current bounded action. A pending JEV gap choice
returns control without executing if the latest board can already advance.

Both models receive the same fact projection, current native capability limits
and room-resource priors. The planner sees current/previous board progress and
exact native failure reasons; it must not equate route rejection with no resource.

## Stable DeepSeek request prefix

The first `system` message contains planning rules and deterministic static
reference JSON: `DSTKnowledge.txt` (up to 12,000 characters), the complete Klei
room-type/resource mapping, supported target types, canonical item IDs/aliases,
execution limits, control ownership and quarter-review policy. The mapping is
generation background, not evidence that any room exists in the current world;
apply it only to room types in the observed navigation graph. Source hashes and
extraction metadata are not sent. JSON keys and resource lists have stable order.

Historical user/assistant pairs follow that prefix; the final `user` contains
the current game snapshot, day/quarter, request reason, board progress and recent
action results. Live available actions, protocol/revision, observed topology and
actual resource records remain dynamic. Older user snapshots are cleaned of
duplicated knowledge/contracts before replay and before applying history limits.
Both models retain the same live facts; DeepSeek reads static references from
`system`, while JEV keeps its compact observed-room priors in decision state.

When the API supplies usage numbers, the CLI prints `daily_plan_usage` with
`prompt_tokens`, `prompt_cache_hit_tokens` and `prompt_cache_miss_tokens`. Missing
fields remain unreported, not zero. Identical prefixes allow cache reuse but
do not guarantee hits; changing installed knowledge/contracts changes the prefix,
and rolling conversation history can limit reuse beyond the static portion.

## Klei data snapshot

`data/worldgen_resources.json` is generated from local forest room declarations in
`data/databundles/scripts.zip`. It records literal positive distribution/count
entries and explicit weighted-prefab alternatives, with per-source SHA-256 hashes.
It is NOT a full worldgen simulation: functions, static layouts, substitutions,
settings, mods and future version changes may alter actual resources. Unknown room
types have no inferred resources. Always verify actual entities and safety.

Regenerate after a game update (no Lua execution or model calls):

```powershell
python tools/extract_worldgen_resources.py "C:/path/to/Don't Starve Together/data/databundles/scripts.zip"
```

Logs: `TASK BOARD`, `task_board_action task=... priority=... choice=... reason=...`,
`exploration_source_failed`, `daily_plan_requested reason=...`, `daily_plan_ready`,
and `logs/task_board.json[l]`. Timing sources distinguish `goal_local`,
`goal_navigation`, `goal_safety`, `jev_planning_gap`,
`gap_navigation`, `goal_resume`, and `executor_idle`. The handoff is printed as
`task_board_handoff reason=... controller=jev_planning_gap`.
`navigation_target_count` records the active watch size; `goal_action_available`
marks a meaningful resource handoff. `navigation_review` / the timing event
`navigation.review_requested` explain host-driven cancellation for board/budget updates.

Upgrade the full project Python files (including `mcp_server/`, `mcp_agent/` and `common/`)
and the complete `data/` directory (knowledge and worldgen priors), plus Lua
`modmain.lua`, `scripts/jev_exploration.lua`, `scripts/jev_native_executor.lua`,
new `scripts/jev_resource_memory.lua`, and `modinfo.lua`. Re-enter the world and
restart Python. Preserve `.env` and the installed bridge token/config. New goal
scheduling is MCP-host-only; legacy sequence and schema-1 board clients remain
compatibility paths, not the new DeepSeek output.
