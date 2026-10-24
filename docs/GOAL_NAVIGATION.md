# Persistent priority goals and source navigation

[中文使用说明](MCP.md) · [English setup](MCP.EN.md)

## Model contract

```json
{
  "goal": "Gather basic supplies",
  "tasks": [
    {"id":"grass","title":"Gather grass","priority":1,
     "target":{"type":"inventory","prefab":"cutgrass","count":12},
     "search_budget_seconds":120,"search_budget_distance":240},
    {"id":"stone","title":"Gather rocks","priority":2,
     "target":{"type":"inventory","prefab":"rocks","count":6}},
    {"id":"torch","title":"Prepare portable light","priority":1,
     "target":{"type":"inventory","prefab":"torch","count":1},
     "depends_on":["grass"],"reason":"Prepare light before night"}
  ],
  "cancel_task_ids":[]
}
```

One to six tasks; priority 1 is highest. The host adds `kind=task_board`,
`schema=2`, `execution_policy=priority_goals` and a revision ID. The example's
torch still needs enough twigs in actual inventory; add a twig goal/dependency
if not already satisfied. No implicit recipe solver creates prerequisites.

Supported targets:

| Type | Completion condition |
| --- | --- |
| inventory | Current carried/equipped stock reaches `prefab,count` |
| structure | Campfire or Science Machine observed/verified built |
| hunger_ratio | Observed hunger fraction reaches `at_least` |
| visit_region | Player actually enters known `node_id`; historical entered flag is insufficient |
| survey_region | Reveal `area` additional observed area in known `node_id`, or finish its boundary survey |
| known_regions | Observed topology-node count reaches absolute `count` |

Completed goals latch from observed state only; consuming ingredients does not
restart them. A changed target reopens a goal. Native crafting currently offers
a torch/axe/pickaxe only when none of that prefab is carried: these goals must
request count=1. Campfire building waits for night and no nearby lit campfire;
unavailable recipes are not evidence that materials disappeared. Equip/unequip
and emergency lighting remain local safety decisions, not timed wait goals.

## Scheduling and navigation

1. Scan all ready goals by priority for an offered native collection/work/craft,
   construction or eating action. A missing priority-1 source does not block a
   visible priority-2 goal. Real dependencies still block dependent tasks.
2. If none is executable locally, navigate for the first ready navigable goal.
   Known concrete source positions in the current room precede room surveys.
3. Prefer recorded source rooms, then Klei room-generation priors for known nodes,
   then unknown topology branches. Verified graph destinations are ordered by
   BFS hops and next-exit distance, not an invented exact full-route cost.
4. Re-evaluate after native resource/region events. Safe movement legs continue
   inside Lua; ordinary board pickups do not request JEV. A new TYPE/capability
   of off-board opportunity may request one bounded JEV choice. Repeated GUIDs
   of the same type do not each request inference.
5. Threat, health, hunger and light interruptions retain precedence. Lua owns
   live legality, passability and movement verification; Python never invents
   coordinates or bypasses the offered-action snapshot.

Lua source memory retains at most 512 observed entities per world session and
offers diverse current-room resource routes (at most 24 types). It records positions,
pickability and source identity. A complete local scan can invalidate a disappeared
source; capped scans cannot. Regrowth requires fresh positive observation.
Out-of-room concrete targets are reached through room routing first. No save-file
or whole-world entity inspection reveals undiscovered resources.

## Search bounds and recovery

Per-task navigation budget defaults to 120 seconds / 240 distance units, with
allowed ranges 10–600 seconds / 8–2000 units. Gathering, planning waits and safety
handling do not deliberately consume that budget. A per-room resource survey is
deprioritized after 30 navigation seconds or 256 additional observed area. This is
negative evidence for that resource, not a ban on transit or other resources.
Fresh obtainable evidence can restore a source room's priority.

A transient native failure pauses its exact choice/target for 15 seconds and
lets routing or another visible goal proceed. It does not terminate the board or
prove the resource absent. Exhausted task budgets appear as blocked and request
review; a justified task revision can renew the budget. A 15-second idle interval
also requests review without discarding goals. Missing paths are not completion.

## Asynchronous review

Every quarter is a background review opportunity, not an expiry. The active board
continues while DeepSeek responds. Reviews merge tasks by stable ID/target;
omitted tasks remain and cancellation requires `cancel_task_ids`. Dependencies are
revalidated after merge. Completed milestones and unchanged task budgets survive
reviews. Completed or wholly blocked boards request replanning, throttled to
avoid a busy request loop. Reply acceptance checks current player/session and
observation continuity, not quarter equality; actual progress is re-evaluated.

Both models receive the same fact projection, current native capability limits
and room-resource priors. The planner sees current/previous board progress and
exact native failure reasons; it must not equate route rejection with no resource.

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
`goal_navigation`, `goal_opportunity`, `goal_safety`, and `goal_idle`.

Upgrade the full project Python files (including `mcp_server/`, `mcp_agent/` and `common/`)
and the complete `data/` directory (knowledge and worldgen priors), plus Lua
`modmain.lua`, `scripts/jev_exploration.lua`, `scripts/jev_native_executor.lua`,
new `scripts/jev_resource_memory.lua`, and `modinfo.lua`. Re-enter the world and
restart Python. Preserve `.env` and the installed bridge token/config. New goal
scheduling is MCP-host-only; legacy sequence and schema-1 board clients remain
compatibility paths, not the new DeepSeek output.
