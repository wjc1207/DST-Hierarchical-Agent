"""Asynchronous task planning for local execution and fast JEV handoffs."""

from __future__ import annotations

import json
import math
import shutil
import time
import uuid
import urllib.error
import urllib.request
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
from typing import Optional
from common.paths import DATA_DIR, LOG_DIR
from common.recipes import query_recipes, RECIPE_TOOL
from common.places import query_places, PLACE_TOOL, PLACE_TARGETS, target_progress
from common.coordinates import observed_point, validate_point
from common.refueling import target_error as refuel_error
from common.placement import resolve as placement_area, camp_anchor_available, CAMP_STRUCTURES
from mcp_agent.map_view import MAP_TOOL, render_map
from mcp_agent.model_state import model_game_state
from mcp_agent.region_resources import room_resources
from common.task_board import (validate_board, board_view, format_board,
                        INVENTORY_TARGET_PREFABS, INVENTORY_PREFAB_ALIASES)
from mcp_agent.action_sequence import SequenceExecutor, is_sequence, validate_sequence, format_sequence
from mcp_agent.priority_board import PriorityBoardExecutor, is_priority_board, validate_goals, merge_goals, idle_review_reason, idle_review_marker


DEFAULT_API_URL = "https://api.deepseek.com/chat/completions"
DEFAULT_MODEL = "deepseek-flash"
PLAN_PREVIEW_CHARS = 240
KNOWLEDGE_PATH = DATA_DIR / "DSTKnowledge.txt"
MAX_KNOWLEDGE_CHARS = 12000
PLAN_MAX_TOKENS = 4096
BOARD_LOG_DIR = LOG_DIR
COMPLETION_REPLAN_INTERVAL = 15.0
PLAN_HISTORY_TURNS = 3
PLAN_HISTORY_CHARS = 48000
STATIC_REFERENCE_MARKER = "\n\nStatic reference JSON (background, not a live observation):\n"
PLANNING_POLICY = {"quarters_per_day": 4, "mode": "review_not_reset"}
CONTROL_CONTRACT = {
    "transport": "mcp", "execution_owner": "lua", "navigation_intent_owner": "python",
    "unsupported": ["attack", "equip_weapon", "automatic_tool_prerequisite_solver"],
}
PLAN_ERROR_LOG = LOG_DIR / "planner_errors.jsonl"


def record_plan_error(day, stage, detail):
    """Persist diagnostics without credentials, request bodies or full model replies."""
    try:
        PLAN_ERROR_LOG.parent.mkdir(parents=True, exist_ok=True)
        with PLAN_ERROR_LOG.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "day": day, "stage": stage, "error": str(detail)[:2000]}, ensure_ascii=False) + "\n")
    except OSError as exc:
        print(f"planner_error_log_unavailable={type(exc).__name__}")


SUPPORTED_CAPABILITIES = {
    "execution_policy": "priority_goals",
    "inventory_target_prefabs": sorted(INVENTORY_TARGET_PREFABS),
    "inventory_prefab_aliases": dict(INVENTORY_PREFAB_ALIASES),
    "target_types": ["inventory", "structure", "hunger_ratio", "visit_region", "survey_region", "known_regions",
                     "visit_building", "set_home", "return_home", "stay_home", "move_to", "refuel"],
    "constraints": {"max_carried_tools_or_torches": 1, "campfire_build_phase": "night", "max_focus_tasks": 2},
}


def planning_slot(state: dict) -> Optional[tuple[int, int]]:
    """Return (game day, zero-based quarter) from normalized DST world time."""
    world = state.get("world", {})
    day = world.get("day")
    if not isinstance(day, int) or day < 1:
        return None
    try:
        progress = float(world.get("time", 0))
    except (TypeError, ValueError):
        progress = 0.0
    if not math.isfinite(progress):
        progress = 0.0
    return day, min(3, max(0, int(progress * 4)))


def load_dst_knowledge(path: Path = KNOWLEDGE_PATH) -> str:
    return path.read_text(encoding="utf-8").strip()[:MAX_KNOWLEDGE_CHARS]


def planning_reference(knowledge_path: Path = KNOWLEDGE_PATH, *, knowledge: Optional[str] = None) -> dict:
    """Only installed knowledge/contracts enter the stable system prefix, never live state."""
    if knowledge is None:
        try:
            knowledge = load_dst_knowledge(knowledge_path)
        except (OSError, UnicodeError) as exc:
            knowledge = ""
            print(f"daily_plan_knowledge_unavailable={type(exc).__name__}")
    return deepcopy({
        "dst_knowledge": knowledge[:MAX_KNOWLEDGE_CHARS],
        "region_resource_priors": {room: sorted(prefabs) for room, prefabs in room_resources().items()},
        "supported_capabilities": SUPPORTED_CAPABILITIES,
        "control_contract": CONTROL_CONTRACT,
        "planning_policy": PLANNING_POLICY,
    })


def planning_snapshot(context: dict) -> dict:
    """Remove references repeated by older callers/history without changing live facts."""
    result = deepcopy(context)
    for key in ("dst_knowledge", "region_resource_priors", "supported_capabilities"):
        result.pop(key, None)
    if isinstance(result.get("control"), dict):
        for key in CONTROL_CONTRACT:
            result["control"].pop(key, None)
    if isinstance(result.get("planning_window"), dict):
        for key in PLANNING_POLICY:
            result["planning_window"].pop(key, None)
    return result


def snapshot_message(context: dict) -> str:
    snapshot = planning_snapshot(context)
    snapshot.pop("_recipe_catalog", None)
    snapshot.pop("_recipe_epoch", None)
    snapshot.pop("_place_catalog", None)
    snapshot.pop("_map_context", None)
    return json.dumps(snapshot, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def planning_context(state: dict, history: list[dict]) -> dict:
    """Summarize facts and recent action outcomes, not the repeating telemetry log."""
    return {
        **model_game_state(state),
        **({"_map_context":{"world_map":deepcopy(state["world_map"]),
              "character":state.get("player",{}).get("prefab"),
              "position":deepcopy(state.get("player",{}).get("position")),"world":deepcopy(state.get("world",{})),
              "places":deepcopy(state.get("places",{})),"nearby":deepcopy(state.get("nearby",[])),
              "hazards":deepcopy(state.get("navigation",{}).get("hazards",[])),"observed_at":state.get("observed_at")},
            "map_summary":{"available":True,"version":state["world_map"].get("version"),
              "observed_cells":len(state["world_map"].get("cells",{})),"truncated":state["world_map"].get("truncated",False),
              "instructions":"Use query_map before first camp selection or changing primary/backup coordinates; unchanged accepted sites need no repeat image."}}
           if isinstance(state.get("world_map"),dict) else {}),
        **({"_recipe_catalog": deepcopy(state["recipes"]), "_recipe_epoch": state.get("recipe_epoch")} if state.get("recipes") else {}),
        **({"_place_catalog": deepcopy(state["places"]),
            "place_summary": {"scope": state["places"].get("scope"),
                              "home": deepcopy(state["places"].get("home")),
                              "building_count": len(state["places"].get("buildings", [])),
                              "instructions": "Use query_places for building IDs and legal home anchors."}}
           if "places" in state else {}),
        "recipe_query": {"available": bool(state.get("recipes")),
                         "recipe_count": len(state.get("recipes", {})),
                         "index": deepcopy(state.get("recipe_index")),
                         "instructions": "Use query_recipes before planning new crafted products or structures; recipe_id is required for non-legacy targets."},
        "science_machine_built_this_run": any(
            event.get("action") == "build_sciencemachine" and event.get("outcome") == "executed"
            for event in history
        ),
        "recent_actions": history[-40:],
    }


def _short_text(value: object, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:limit]


def validate_plan(raw: object, day: int) -> dict:
    """Keep only a small, well-formed advisory plan in JEV's state."""
    if not isinstance(raw, dict):
        raise ValueError("DeepSeek plan must be a JSON object")
    if raw.get("kind") == "action_sequence" or (isinstance(raw.get("steps"), list)
            and raw["steps"] and isinstance(raw["steps"][0], dict)):
        return validate_sequence(raw, day)
    if "tasks" in raw:
        return validate_goals(raw, day) if is_priority_board(raw) else validate_board(raw, day)
    goal = _short_text(raw.get("goal"), 160)
    if not goal:
        raise ValueError("DeepSeek plan has no goal")
    steps = raw.get("steps")
    if not isinstance(steps, list):
        raise ValueError("DeepSeek plan has no steps array")
    cleaned_steps = [_short_text(step, 120) for step in steps[:4]]
    cleaned_steps = [step for step in cleaned_steps if step]
    if not cleaned_steps:
        raise ValueError("DeepSeek plan has no usable steps")
    return {
        "day": day,
        "goal": goal,
        "steps": cleaned_steps,
        "priority": _short_text(raw.get("priority"), 160),
        "replan_if": [
            text for condition in (raw.get("replan_if") or [])[:3]
            if (text := _short_text(condition, 100))
        ] if isinstance(raw.get("replan_if"), list) else [],
    }


def recent_exchanges(exchanges) -> list[tuple[str, str]]:
    """Keep a chronological suffix of complete user/assistant pairs, never fragments."""
    retained, size = [], 0
    for user, assistant in reversed(list(exchanges)[-PLAN_HISTORY_TURNS:]):
        pair_size = len(user) + len(assistant)
        if size + pair_size > PLAN_HISTORY_CHARS:
            break
        retained.append((user, assistant))
        size += pair_size
    return list(reversed(retained))


def camp_site_requires_map(site, previous_site):
    """Only an accepted, unchanged primary/backup location can reuse its review."""
    previous_site = previous_site if isinstance(previous_site, dict) else {}
    for point, previous in ((site, previous_site),
                            (site.get("backup"), previous_site.get("backup"))):
        if point is None:
            continue
        try:
            previous = validate_point(previous)
        except ValueError:
            return True
        if any(point[key] != previous[key] for key in ("x", "z")):
            return True
    return False


def request_plan(api_url: str, api_key: str, model: str, day: int, context: dict,
                 conversation: Optional[list[tuple[str, str]]] = None,
                 reference: Optional[dict] = None) -> dict:
    # Goal scheduling is deterministic. The model supplies needs, not movement legs.
    system_prompt = (
        "You maintain a priority task_board for Don't Starve Together. Return JSON only, not an action sequence. "
        "Use 1-6 tasks with stable id, title, priority (1 highest..5), focus (boolean, default false), target, depends_on, reason, "
        "Query tools at most FOUR times per response; split larger batches across responses. "
        "search_budget_seconds (10-600, default120), search_budget_distance (8-2000, default240). "
        "Any task may have start_at:{day,progress:0..1 exclusive} or start_at:{day,phase:day|dusk|night}. "
        "Before start_at it is scheduled and does not activate focus. Omit start_at for immediate eligibility after dependencies. "
        "Default to omitting start_at. Add it only for an explicit user schedule or a concrete phase/time requirement, "
        "and explain that requirement in reason. Low priority, non-urgent reserves, or a plan to return home do not by themselves "
        "justify postponing work until tomorrow. Keep existing immediately eligible goals eligible unless changed facts require a delay. "
        "Ordinary tasks accept start_at. hunger_ratio and refuel ratio targets optionally accept task-level duration (game days, >0..7) to maintain the target. "
        "Without duration hunger_ratio is a one-shot milestone. With duration, its clock starts when start_at/dependencies "
        "are satisfied and observed hunger/time are available, even if hungry; safety does not pause this game-time window. "
        "At target it is maintaining (not done), releases focus/execution to other tasks, and resumes eating below target. "
        "At expiry it is done only if currently satisfied, otherwise expired. Dependent tasks wait for final completion. "
        "Preserve the task ID and timing fields during reviews to keep the original deadline; do not reset duration each review. "
        "Use this for eat_dusk when food should be maintained over time. hunger_ratio/refuel accept no end_at. "
        "To wait near home, use target:{type:stay_home,home_id,radius:6..30(default12)}. "
        "stay_home requires EXACTLY ONE task-level duration (game days, >0..7) OR end_at:{day,progress|phase}; "
        "end_at:{phase:day|dusk|night,next:true} means the next such phase after activation. Never supply both duration and end_at. "
        "Invalid timing fails only that task and returns timing_error/task_errors. Dependents cannot run until it is repaired. "
        "Residence first returns to the queried home, then duration accumulates only while safely inside its radius; travel does not count. "
        "Absolute end_at never shifts for delays; missing it before safe arrival expires the task. "
        "Residence is not completed by arrival: it maintains the area until its time condition ends, allowing compatible local chores. "
        "If the intent is to remain home until dawn, use stay_home with end_at for the next day phase; "
        "return_home only completes a trip and does not prevent subsequent departure. "
        "Safety can flee; after escape return while the window remains. All time is observed GAME time, not wall seconds. "
        "For night-only campfire work use start_at with the appropriate night, rather than focusing on an unavailable campfire during dusk. "
        "Targets: inventory {prefab,count,recipe_id?} for CURRENT stock; structure {prefab,recipe_id?,placement?}; "
        "For camp construction use target.placement:{anchor_id:building:<GUID>,radius:4..20(default10)}. "
        "This binds BOTH the build location and completion to the area around that observed building. "
        "The executor travels to the anchor first if needed and selects a legal site within radius; it cannot build en route. "
        "Once a science machine/home anchor exists, permanent camp structures firepit,cookpot,treasurechest,researchlab2,"
        "lightning_rod,icebox MUST include placement; query_places for the chosen anchor ID. "
        "A distant existing structure cannot complete this scoped task. Camp_site text and a title saying 'at home' "
        "do not constrain building location. Use placement on campfire when it must be at camp; temporary emergency "
        "field campfires may omit it. Reuse known structures in range and refuel an existing firepit before building another. "
        "Use query_recipes to inspect live recipes. Recipes are progressively indexed; when recipe_query.index.complete is false, missing entries are not proof a recipe does not exist. Rows with fresh=false are awaiting refresh, not evidence of current craftability. For a non-legacy craft/place goal, set recipe_id to the exact "
        "queried ID and prefab to its product. Legacy recipes use target_prefab and omit recipe_id. Only supported recipes may be planned. "
        "Explicitly plan intermediate ingredients and technology prerequisites; no automatic dependency solver exists. "
        "A supported but not-ready recipe can be a future goal after prerequisites. Unknown/unsupported recipes cannot. "
        "hunger_ratio {at_least:0..1}; visit_region {node_id} for a known node; known_regions {count}. "
        "Place goals: visit_building {building_id}, set_home {anchor_id}, return_home {home_id}. "
        "Building IDs from query_places are stable opaque site IDs; guid is only the current client entity binding. "
        "Never reconstruct building_id/anchor_id from a guid; retain the returned id after replica reloads. "
        "Refuel target:{type:refuel,building_id,fuel_prefab,at_least:0..1(exclusive0)} maintains fuel above that ratio. "
        "Without task-level duration it completes once the observed fuel lower bound reaches at_least. With duration "
        "it monitors throughout that many game days, releases focus when adequate and refuels when below target. "
        "At expiry the lower bound must still meet the ratio, or the task expires. Do not provide count together with at_least. "
        "Client fire stages imply conservative ranges: 0=0%, 1=(0,25%), 2=[25%,50%), 3=[50%,75%), 4=[75%,100%]. "
        "Never report a stage as an exact fuel percentage. Use the LOWER bound, not midpoint/upper bound, for completion. "
        "When a range straddles the threshold, another single fuel item may be needed; actual fuel can overshoot the target. "
        "With estimated stage4, >75% cannot be confirmed: do not keep burning fuel to chase 100%. Prefer 25% or 50% targets. "
        "Missing current fire observations require visiting the exact building; remembered fire status does not prove fuel. "
        "ratio_available=true requires updated Mod. Legacy count targets remain compatible for existing boards only; do not create new count goals. "
        "Each physical addition is one item followed by a fresh observation; temperature and settling guards apply. "
        "refueling.targets reports observed fire_section/fuel_percent when available and pause reasons. "
        "For ratio targets use ratio_reason and fuel_min/max. For legacy count targets fire_already_sufficient pauses additions. temperature_too_high always stops fuel: reduce/cancel an unsafe refuel goal "
        "or schedule a later reassessment rather than blocking residence or renaming the task to force more fuel. "
        "Thermal safety overrides focus and residence, retreats along safe lit routes and pauses refueling. "
        "Standing beside a fire does NOT normally regenerate health. Attribute health loss using temperature, threats and "
        "recent outcomes; only actual healing effects prove recovery. Missing temperature or fire fuel readings are unknown. "
        "count is the cumulative TOTAL for that task ID, building and fuel, not extra items on each review. "
        "If progress is 3/3 and you revise count to 5, only TWO further items are added. Lowering count does not erase "
        "consumed fuel. A deliberate new refueling round requires a NEW task ID; cancel an obsolete unfinished round. "
        "Preserve completed facts from the current board even when revising an earlier planning snapshot. "
        "Common fuels only: log,charcoal,pinecone,acorn,twiggy_nut,twigs,cutgrass. Choose fuel explicitly. "
        "refueling.options lists current nearby native actions; each adds ONE item and needs verified consumption "
        "and the target burning. This is a bounded addition, not a fuel-percentage or overnight-duration guarantee. "
        "Refuel automatically approaches that exact building via observed safe routes before adding fuel; "
        "return_home is not arrival at the firepit. No separate visit/move_to task is required for refueling. "
        "Plan missing fuel acquisition explicitly. Night approach still requires an equipped torch under current rules; "
        "prefer refueling during daylight/dusk and do not assume a carried torch is equipped. "
        "Coordinate travel: target:{type:move_to,x,z,arrival_radius:0.5..4(default2)} uses GAME WORLD x/z, not image pixels. "
        "Only observed passable land is accepted; Lua rechecks actual routes, obstacles, threats and lighting. "
        "When first selecting a camp or changing primary/backup coordinates, call query_map to SEE the observed coordinate map first. "
        "You may keep camp_site from the current accepted board unchanged without calling query_map again. "
        "Changing only reasons or arrival_radius, or removing a backup, does not select a new location. "
        "Use the previous_task_board only when no current board exists; older conversation sites do not authorize relocation. "
        "Map axes are +x right and +z up, independent of camera heading. Dark blank cells are unobserved; "
        "resource icons can be historical and terrain alone does not prove safety or empty building space. "
        "Return optional top-level camp_site:{x,z,reason,backup:{x,z,reason}} with a first choice and a sensible backup when known. "
        "Evaluate access, open building space, observed threats, resources and return routes; an early camp is provisional. "
        "For a new site, add a move_to task (focus when committing to the trip), make camp construction depend on arrival, "
        "then query the resulting building ID on a later review and set_home with that BUILDING anchor. "
        "Coordinates are travel candidates, never home anchors. Inspect the site after arrival and revise if unsuitable. "
        "Reuse a suitable existing building if justified; do not build a second science machine just for a chosen site. "
        "Request map images only for spatial decisions such as camp selection or relocation, not every routine resource task. "
        "Use query_places for observed IDs. Never invent locations. Set home explicitly using a queried building:<GUID> anchor only; "
        "Current-position/here:* anchors are not supported. Building anchor identity survives movement during planning. "
        "A set_home task automatically travels to that building if needed, then sets home at a safe standing point after verified arrival. "
        "do not automatically call the first science machine home. Establish a home when committing to a useful camp, "
        "and preserve it across reviews unless facts justify relocation. Return_home binds the current home ID. "
        "Travel uses known safe region routes and standing points beside buildings. Home is a location, not guaranteed light or safety. "
        "Visits, residences and one-shot refuel targets complete once per task; use a NEW task ID for a later repeat, cancelling obsolete tasks as needed. "
        "A previous visit does not satisfy current technology: query the recipe again before crafting. "
        "If a recipe lacks technology, plan a visit to a suitable observed station before crafting; never assume a station's exact effects. "
        "survey_region {node_id,cells:1..256} reveals ADDITIONAL observed passable map CELLS in that room (default16). "
        "Count map grid cells, not area or walking steps; each map cell is 4x4 world units. "
        "Use observed_cells for current coverage; existing revealed cells do not advance a new survey. "
        "Reaching a cell target is a bounded probe, not proof the entire region is explored; boundary_complete is separate. "
        "At most TWO tasks may have focus=true. While any focused task is unfinished, ONLY focused tasks "
        "and their unfinished transitive prerequisites may execute or interrupt navigation; safety still overrides focus. "
        "Blocked focused goals remain exclusive while you review them, including search cooldowns; ordinary work does not take over. "
        "Use focus for urgent needs such as minimum night light. Promote/demote an existing stable task by resubmitting "
        "it with focus=true/false; omitted tasks retain their mode. Completed focus tasks release exclusivity. "
        "Without unfinished focus tasks, the executor FIRST performs feasible visible goals by priority, skipping unavailable higher ones, "
        "then navigates for the highest ready goal. Goals can be completed in interleaved order. "
        "A hunger_ratio goal eats carried safe food, otherwise gathers nearby safe food or searches for food sources until the hunger target is observed. "
        "Use depends_on only for real prerequisites such as a pickaxe before mining. "
        "Do not use dependencies to express priority or a preferred itinerary: gathering food and surveying do not normally "
        "require a campfire to have been built. A campfire pending for night must not block otherwise safe dusk work. "
        "It uses observed source positions, region resource memory, Klei room generation priors, then unknown branches. "
        "Priors are possible initial resources, not guarantees, remaining stock, or spawn probabilities. "
        "A route failure is not proof that a resource is absent. Safety always overrides execution; "
        "JEV handles emergencies and temporary planning gaps only; off-board opportunities do not trigger it. "
        "Include needed food, sanity recovery and resource reserves explicitly in the board. "
        "If no tasks remain or no offered action/route can advance the board, reassess and update it. "
        "Keep useful future goals, but include feasible work or resource search for the current phase; "
        "a night-only campfire must not be the sole remaining daytime work. "
        "A board whose tasks are all done, scheduled or blocked triggers immediate review, including mixtures. "
        "For only_scheduled_tasks or no_actionable_tasks, preserve useful timed goals and plan the interval before "
        "they start, allowing travel time to home or the destination; use stay_home for intentional residence. "
        "If waiting is justified, retain the schedule and explain why; an unchanged idle board does not trigger "
        "another immediate review. "
        "Dusk is still a working phase, distinct from night. Continue currently safe gathering, crafting or short exploration "
        "when return-route distance, threats and light preparedness permit it; shorten risky trips as darkness approaches. "
        "Do not infer remaining phase duration from quarter number or assume fixed day/dusk/night lengths. "
        "If current facts justify returning now, plan that explicitly; if staying home is intended, add residence rather than "
        "scheduling every remaining task for day 2 and leaving a planning gap. "
        "JEV temporarily handles local decisions while this evaluation is pending. "
        "Quarterly review is a PATCH, not a reset: preserve current goals, priorities, IDs and progress unless "
        "changed facts justify an update. Omitted tasks remain; explicitly cancel using cancel_task_ids with "
        "an explanation in goal/reasons. Do not raise targets just because time passed. "
        "Omitted completed tasks are retired when no remaining task depends on them; do not resubmit them just to keep history. "
        "The MERGED board, including retained unfinished tasks, must fit six slots. Cancel obsolete unfinished tasks explicitly. "
        "Search seconds and distance are per-attempt limits (EITHER reaching its limit triggers cooldown). "
        "Exhausted searches cool down for 15 seconds, then automatically retry without erasing resource/survey memory; "
        "newly actionable local work can resume sooner. search_retry reports attempts and cumulative cost. "
        "Use progress and precise failure reasons to revise goals/routes or explicitly demote/cancel an unsuitable focus, "
        "rather than repeatedly renaming or increasing targets to reset budgets. "
        "Current Lua limits: only one carried torch/axe/pickaxe can be requested, never count2; "
        "the legacy build_campfire action is offered only AT NIGHT, when no nearby lit fire exists; "
        "this is a runtime action limitation, not a general DST rule or a ban on other dusk work. "
        "Generic place actions follow their live queried recipe and placement conditions. "
        "tools must be carried with adequate durability before work. Craftability is not current action availability. "
        "No wait-second or equip-torch goals: illumination is handled by safety decisions when phase changes. "
        "Read the FINAL user snapshot as current truth; older user snapshots and assistant plans are "
        "history, not evidence of execution. Use observed facts, recent outcomes and DSTKnowledge. "
        "Static reference JSON below supplies DSTKnowledge, supported_capabilities, control_contract, "
        "planning_policy and the complete Klei room-type resource priors. A listed room type is not "
        "evidence that this world contains that room or that it has been discovered; use these priors "
        "only for room types in the observed navigation graph. Priors never override live resource facts. "
        "Use canonical inventory IDs from supported_capabilities.inventory_target_prefabs: log not logs, "
        "cutgrass not grass. Never invent GUIDs, coordinates, unknown node IDs or unsupported recipes. "
        "Use strategic knowledge for survival and exploration; use live recipe queries for crafting facts; "
        "alchemy engine is allowed only if its live recipe is supported. Do not build another science "
        "machine if science_machine_built_this_run is true. Assess torch fuel and equipped slots, "
        "world phase and actual lit fires. Missing fuel values or fire lifetime are unknown, not full or zero. "
        "Craftability is not demand. Completion or a new planning window is not itself a reason "
        "to raise stock targets. Eat_safe_food already cooks "
        "raw food first at a nearby fire. Make night light and food prerequisites before optional travel. "
        "For urgent light, query the chosen recipe and collect only its minimum missing ingredients first, craft the light, "
        "then plan optional reserves. Never make a torch depend on a combined campfire-plus-spares stock target. "
        'Example: {"goal":"Recover hunger","tasks":['
        '{"id":"food","title":"Find and eat safe food","priority":1,"focus":true,'
        '"target":{"type":"hunger_ratio","at_least":0.6}}]}'
    )
    if reference is None:
        # Compatibility for direct callers; runtime passes a separate local reference.
        reference = planning_reference(knowledge=context.get("dst_knowledge"))
    system_prompt += STATIC_REFERENCE_MARKER + json.dumps(
        reference, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    history = []
    for user, assistant in conversation or []:
        try:
            old_context = json.loads(user)
        except (ValueError, TypeError):
            old_context = None
        if isinstance(old_context, dict):
            user = snapshot_message(old_context)
        history.append((user, assistant))
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            *[message for user, assistant in recent_exchanges(history)
              for message in ({"role": "user", "content": user},
                              {"role": "assistant", "content": assistant})],
            {"role": "user", "content": snapshot_message(context)},
        ],
        "response_format": {"type": "json_object"},
        "reasoning_effort": "medium",
        "max_tokens": PLAN_MAX_TOKENS,
        "stream": False,
    }
    catalog = context.get("_recipe_catalog", {})
    place_catalog = context.get("_place_catalog")
    functions = {}
    map_images = []
    map_seen = False
    if isinstance(context.get("_map_context"),dict):
        def query_map(**args):
            metadata,url = render_map(context["_map_context"],output_dir=LOG_DIR,**args)
            map_images.append((metadata,url))
            return metadata
        functions["query_map"] = query_map
    if catalog:
        functions["query_recipes"] = lambda **args: query_recipes(catalog, epoch=context.get("_recipe_epoch"), **args)
    if isinstance(place_catalog, dict):
        functions["query_places"] = lambda **args: query_places(place_catalog, **args)
    if functions:
        body["tools"] = [tool for tool in (RECIPE_TOOL, PLACE_TOOL, MAP_TOOL) if tool["function"]["name"] in functions]
    failures = 0
    for attempt in range(8):
        if failures:
            # A short non-thinking retry leaves more of the budget for JSON output.
            body["reasoning_effort"] = "none"
        request = urllib.request.Request(
            api_url,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"DeepSeek planning HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"DeepSeek planning connection failed: {exc.reason}") from exc

        usage = payload.get("usage") or {}
        if not isinstance(usage, dict):
            usage = {}
        cache_counts = {key: usage[key] for key in (
            "prompt_tokens", "prompt_cache_hit_tokens", "prompt_cache_miss_tokens")
            if type(usage.get(key)) is int and usage[key] >= 0}
        if cache_counts:
            print(f"daily_plan_usage attempt={attempt + 1} " + " ".join(
                f"{key}={value}" for key, value in cache_counts.items()))
        choices = payload.get("choices") or []
        if not choices or not isinstance(choices[0], dict):
            raise RuntimeError("DeepSeek planning response has no choice")
        choice = choices[0]
        finish_reason = choice.get("finish_reason")
        message = choice.get("message") or {}
        calls = message.get("tool_calls") or []
        if calls:
            map_images.clear()
            if len(calls) > 4:
                record_plan_error(day, "query_limit", f"Received {len(calls)} tool calls; executing at most four")
            body["messages"].append({"role": "assistant", "content": message.get("content"),
                                     "tool_calls": calls,
                                     **({"reasoning_content": message["reasoning_content"]} if "reasoning_content" in message else {})})
            for index, call in enumerate(calls):
                try:
                    if index >= 4:
                        raise ValueError("At most four query calls are executed per response. Submit this query again in a later response if still needed.")
                    function = call["function"]
                    if function["name"] not in functions:
                        raise ValueError("Unknown planner tool")
                    result = functions[function["name"]](**json.loads(function["arguments"]))
                except (ValueError, TypeError, KeyError) as exc:
                    result = {"error": str(exc)}
                body["messages"].append({"role": "tool", "tool_call_id": call["id"],
                                         "content": json.dumps(result, ensure_ascii=False)})
            if map_images:
                map_seen = True
                # DeepSeek permits images in USER messages, not tool messages.
                # Complete every tool reply before adding the requested images.
                parts=[]
                for metadata,url in map_images:
                    parts.extend([{"type":"text","text":"Requested observed map from the SAME frozen planning snapshot: "+json.dumps(metadata)},
                                  {"type":"image_url","image_url":{"url":url}}])
                body["messages"].append({"role":"user","content":parts})
            continue
        content = message.get("content") or ""
        completion_tokens = usage.get("completion_tokens")
        if finish_reason == "stop" and content:
            try:
                plan = validate_goals(json.loads(content), day)
                world_map = (context.get("_map_context") or {}).get("world_map")
                if plan.get("camp_site"):
                    accepted_board = context.get("current_task_board") or context.get("previous_task_board") or {}
                    if not map_seen and camp_site_requires_map(plan["camp_site"], accepted_board.get("camp_site")):
                        raise ValueError("Call query_map before first camp selection or changing camp_site primary/backup coordinates; unchanged accepted coordinates need no repeat image")
                    observed_point(plan["camp_site"],world_map)
                    if plan["camp_site"].get("backup"):
                        observed_point(plan["camp_site"]["backup"],world_map)
                for task in plan["tasks"]:
                    if task.get("timing_error"):
                        continue
                    target = task["target"]
                    if target["type"] == "move_to":
                        observed_point(target,world_map)
                    if target.get("placement"):
                        placement_area(target["placement"],{"places":place_catalog})
                        if not context.get("control",{}).get("placement_constraints"):
                            raise ValueError("Placement constraints require updated Mod")
                    elif (target["type"]=="structure" and target.get("prefab") in CAMP_STRUCTURES
                          and camp_anchor_available(place_catalog)):
                        raise ValueError("Camp structure requires target.placement:{anchor_id:building:<GUID>,radius:10}; query_places and bind the camp anchor before building")
                    if target["type"] == "refuel":
                        error = (refuel_error(target, place_catalog) or
                                 ("Refueling requires updated Mod telemetry" if not context.get("refueling", {}).get("available") else ""))
                        if error:
                            raise ValueError(error)
                    if target["type"] in PLACE_TARGETS:
                        _, error = target_progress(target, place_catalog)
                        if error:
                            raise ValueError("Place goal has no observed destination: " + error)
                    if target.get("recipe_id"):
                        recipe = catalog.get(target["recipe_id"], {})
                        if (recipe.get("legacy") or not recipe.get("supported") or recipe.get("product") != target.get("prefab")
                                or bool(recipe.get("placement")) != (target["type"] == "structure")):
                            raise ValueError("Goal does not match a supported observed recipe")
                # Catch patch/retained-goal conflicts while the model can still
                # repair this response, rather than failing at adoption later.
                merge_goals(context.get("current_task_board") or context.get("previous_task_board"), plan)
                return plan
            except (ValueError, TypeError) as exc:
                reason = f"invalid_json_or_plan:{type(exc).__name__}:{str(exc)[:1200]}"
        else:
            reason = f"finish_reason={finish_reason} content_chars={len(content)}"
        record_plan_error(day, "response_validation", reason)
        retryable = finish_reason in {"stop", "length", "insufficient_system_resource", "aborted"}
        if failures == 0 and retryable:
            failures += 1
            print(f"daily_plan_retry day={day} reason={reason} attempt=2/2")
            if finish_reason == "stop" and content:
                body["messages"].append({"role":"assistant", "content":content})
            body["messages"].append({"role":"user", "content":
                "Your last reply could not be accepted: " + reason +
                ". Correct this error using the latest game snapshot above and return a valid nonempty priority task_board JSON. "
                "Keep the merged board within six tasks; preserve useful unfinished goals and explicitly cancel obsolete ones. "
                "Do not repeat an invalid reply or silently remove unfinished goals."})
            continue
        raise RuntimeError(
            f"DeepSeek planning response unusable: {reason} "
            f"completion_tokens={completion_tokens}"
        )
    raise RuntimeError("DeepSeek planning retry exhausted")


def timed_request_plan(api_url: str, api_key: str, model: str, day: int, context: dict,
                       conversation: Optional[list[tuple[str, str]]] = None,
                       reference: Optional[dict] = None) -> tuple:
    """Measure the worker's request, excluding the JEV cycle's polling delay."""
    started_at = time.monotonic()
    try:
        plan = request_plan(api_url, api_key, model, day, context, conversation, reference)
    except Exception as exc:
        return None, time.monotonic() - started_at, exc
    return plan, time.monotonic() - started_at, None


class DailyPlanner:
    def __init__(
        self, api_key: str, api_url: str = DEFAULT_API_URL, model: str = DEFAULT_MODEL,
        knowledge_path: Path = KNOWLEDGE_PATH,
        board_log_dir: Optional[Path] = BOARD_LOG_DIR,
    ):
        self.api_key = api_key
        self.api_url = api_url
        self.model = model
        self.knowledge_path = knowledge_path
        self.history: deque[dict] = deque(maxlen=80)
        # Immutable JSON snapshots, accepted replies only; scoped to this run.
        self.conversation: deque[tuple[str, str]] = deque(maxlen=PLAN_HISTORY_TURNS)
        self.pending_user_message: Optional[str] = None
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="dst-planner")
        self.future: Optional[Future] = None
        self.pending_slot: Optional[tuple[int, int]] = None
        self.requested_slot: Optional[tuple[int, int]] = None
        self.plan: Optional[dict] = None
        self.science_machine_built = False
        self.failed_action: Optional[str] = None
        self.failure_count = 0
        self.last_replan = 0.0
        self.previous_board = None
        self.board_revision = 0
        self.board_run_id = uuid.uuid4().hex[:12]
        self.board_log_dir = board_log_dir
        self.board_signature = None
        self.board_console_signature = None
        self.board_console_status = None
        self.board_console_at = 0.0
        self.completion_replan_slot = None
        self.last_completion_request = None
        self.sequence = SequenceExecutor()
        self.sequence_replan_reason = None
        self.goals = PriorityBoardExecutor()
        self.pending_basis = None
        self.goal_replan_reason = None
        self.goal_retry_at = 0.
        self.goal_terminal = None
        self.legacy_mode = False

    def _board(self, state):
        if is_priority_board(self.plan):
            plan = {**self.plan, "verified_structures": ["sciencemachine"] if self.science_machine_built else []}
            return self.goals.view(plan, state)
        if is_sequence(self.plan):
            plan = {**self.plan, "verified_structures": ["sciencemachine"] if self.science_machine_built else []}
            return self.sequence.view(plan, state)
        if self.plan is None or self.plan.get("kind") != "task_board":
            return self.plan
        facts = ["sciencemachine"] if self.science_machine_built else []
        return board_view(self.plan, state, verified_structures=facts)

    def _publish_board(self, board):
        if is_sequence(board):
            self._publish_sequence(board)
            return
        if not board or board.get("kind") != "task_board":
            return
        signature = json.dumps({k: v for k, v in board.items() if k != "observed_at"}, sort_keys=True, ensure_ascii=False)
        status = (board.get("board_id"), board.get("status"),
                  tuple((t["id"], t["status"], t.get("blocked_reason")) for t in board["tasks"]))
        now = time.monotonic()
        if (signature != self.board_console_signature
                and (status != self.board_console_status or now - self.board_console_at >= 3)):
            print(format_board(board, shutil.get_terminal_size((100, 24)).columns))
            self.board_console_signature, self.board_console_status = signature, status
            self.board_console_at = now
        if signature == self.board_signature:
            return
        self.board_signature = signature
        if self.board_log_dir is None:
            return
        try:
            self.board_log_dir.mkdir(parents=True, exist_ok=True)
            packet = {"run_id": self.board_run_id, "event": "board_updated", "task_board": board}
            temporary = self.board_log_dir / (".task_board_" + self.board_run_id + ".tmp")
            temporary.write_text(json.dumps(packet, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(self.board_log_dir / "task_board.json")
            with (self.board_log_dir / "task_board.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(packet, ensure_ascii=False, separators=(",", ":")) + "\n")
        except OSError as exc:
            print(f"task_board_log_unavailable error={type(exc).__name__}")

    def _publish_sequence(self, plan):
        signature = json.dumps(plan, sort_keys=True, ensure_ascii=False)
        if signature == self.board_signature:
            return
        self.board_signature = signature
        print(format_sequence(plan))
        if self.board_log_dir is None:
            return
        try:
            self.board_log_dir.mkdir(parents=True, exist_ok=True)
            packet = {"run_id": self.board_run_id, "event": "sequence_updated", "action_sequence": plan}
            temporary = self.board_log_dir / (".action_sequence_" + self.board_run_id + ".tmp")
            temporary.write_text(json.dumps(packet, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(self.board_log_dir / "action_sequence.json")
            with (self.board_log_dir / "action_sequence.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(packet, ensure_ascii=False, separators=(",", ":")) + "\n")
        except OSError as exc:
            print(f"action_sequence_log_unavailable error={type(exc).__name__}")

    def request_review(self, state: dict, reason: str, *, immediate: bool = False) -> Optional[dict]:
        """One asynchronous evaluation; repeated polls keep the existing request/backoff."""
        if self.future is None:
            self.goal_replan_reason = self.goal_replan_reason or reason
            if immediate and self.goal_replan_reason != "review_retry":
                self.goal_retry_at = 0.
        return self.update(state)

    def update(self, state: dict) -> Optional[dict]:
        # The Mod normalizes Klei's researchlab to the public structure name.
        # An observed existing machine is also a verified milestone.
        if any(e.get("prefab") == "sciencemachine" for e in state.get("nearby", [])):
            self.science_machine_built = True
        # Legacy clients/history retain their old semantics, never the new DS path.
        if self.legacy_mode or (self.plan is not None and not is_priority_board(self.plan)):
            self.legacy_mode = True
            return self._legacy_update(state)
        if self.plan is None and self.future is not None and self.future.done():
            try:
                pending = self.future.result()[0]
                if pending is not None and not is_priority_board(pending):
                    self.legacy_mode = True
                    return self._legacy_update(state)
            except Exception:
                pass
        slot = planning_slot(state)
        if slot is None:
            return None
        day, quarter = slot
        now = time.monotonic()
        acknowledged_idle = False
        if self.future is not None and self.future.done():
            future, user, basis = self.future, self.pending_user_message, self.pending_basis
            self.future = self.pending_user_message = self.pending_basis = self.pending_slot = None
            seconds = 0.
            try:
                result, seconds, error = future.result()
                if error:
                    raise error
                compatible = (not state.get("player", {}).get("vitals", {}).get("dead") and (not basis or (
                    basis[0] == state.get("player", {}).get("guid")
                    and basis[1] == state.get("bridge", {}).get("session_id")
                    and (basis[2] is None or state.get("observed_at", basis[2]) >= basis[2]))))
                if not compatible:
                    raise ValueError("Planner snapshot belongs to a different/reset player session")
                result = merge_goals(self._board(state) or self.previous_board, result)
                graph = state.get("navigation", {}).get("graph") or {}
                known = {graph.get("current_node"), *(r.get("node_id") for r in graph.get("routes", []))}
                if any(t["target"]["type"] in {"visit_region", "survey_region"} and t["target"]["node_id"] not in known for t in result["tasks"]):
                    raise ValueError("Visit target is not in the observed region graph")
                self.board_revision += 1
                result.update(board_id=f"{self.board_run_id}:{self.board_revision}", quarter=quarter + 1)
                self.plan = result
                new_batch = bool(user and json.loads(user).get("replan_reason") == "all_tasks_completed")
                self.goals.adopt(result, new_batch=new_batch)
                reviewed_reason = json.loads(user).get("replan_reason") if user else None
                acknowledged_idle = reviewed_reason in {
                    "all_tasks_completed", "only_scheduled_tasks", "no_actionable_tasks",
                    "search_or_prerequisite_blocked"}
                if user:
                    self.conversation = deque(recent_exchanges([*self.conversation,
                        (user, json.dumps(result, ensure_ascii=False, separators=(",", ":")))]), maxlen=PLAN_HISTORY_TURNS)
                preview = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
                print(f"daily_plan_ready day={day} quarter={quarter + 1}/4 response_time={seconds:.2f}s preview={preview[:PLAN_PREVIEW_CHARS]}{'…' if len(preview)>PLAN_PREVIEW_CHARS else ''}")
            except Exception as exc:
                print(f"daily_plan_error response_time={seconds:.2f}s error={type(exc).__name__}:{exc}")
                record_plan_error(day, "adoption_or_request", f"{type(exc).__name__}:{exc}")
                self.goal_retry_at = now + COMPLETION_REPLAN_INTERVAL
                self.goal_replan_reason = "review_retry"
        view = self._board(state) if self.plan else None
        idle_reason = idle_review_reason(view)
        if idle_reason:
            marker = idle_review_marker(view)
            # The response to an idle-board review may intentionally keep waiting.
            # A new board ID or updated counters must not create a request loop.
            if acknowledged_idle:
                self.goal_terminal = marker
                if self.goal_replan_reason in {
                        "all_tasks_completed", "only_scheduled_tasks", "no_actionable_tasks",
                        "search_or_prerequisite_blocked"}:
                    self.goal_replan_reason = None
            if marker != self.goal_terminal:
                self.goal_terminal = marker
                self.previous_board = view
                if self.goal_replan_reason != "review_retry":
                    self.goal_replan_reason = idle_reason
                    # A newly idle board needs evaluation now. Failed requests
                    # still retain the normal retry backoff.
                    if self.future is None:
                        self.goal_retry_at = now
                print(f"task_board_review_requested reason={self.goal_replan_reason}")
        else:
            self.goal_terminal = None
        reason = self.goal_replan_reason or ("quarter_review" if self.plan else "initial_plan")
        if self.future is None and now >= self.goal_retry_at and (self.requested_slot != slot or self.goal_replan_reason):
            context = planning_context(state, list(self.history))
            context.update(replan_reason=reason, current_task_board=view, previous_task_board=self.previous_board,
                           science_machine_built_this_run=self.science_machine_built,
                           planning_window={"day": day, "quarter": quarter + 1})
            reference = planning_reference(self.knowledge_path)
            context = planning_snapshot(context)
            self.pending_user_message = snapshot_message(context)
            self.pending_basis = (state.get("player", {}).get("guid"), state.get("bridge", {}).get("session_id"), state.get("observed_at"))
            self.future = self.executor.submit(timed_request_plan, self.api_url, self.api_key, self.model, day,
                                               context, list(self.conversation), reference)
            self.pending_slot = self.requested_slot = slot
            self.goal_replan_reason = None
            self.goal_retry_at = now + COMPLETION_REPLAN_INTERVAL
            print(f"daily_plan_requested day={day} quarter={quarter + 1}/4 reason={reason} knowledge_chars={len(reference['dst_knowledge'])} history_turns={len(self.conversation)}")
        self._publish_board(view)
        return view

    def _legacy_update(self, state: dict) -> Optional[dict]:
        """Poll without waiting; keep active sequences, replan on terminal progress."""
        slot = planning_slot(state)
        if slot is None:
            return None
        day, quarter = slot
        if self.plan is not None and not is_sequence(self.plan) and (
            self.plan["day"] != day or self.plan.get("quarter") != quarter + 1
        ):
            if self.plan.get("kind") == "task_board":
                self.previous_board = self._board(state)
                self._publish_board(self.previous_board)
            self.plan = None
        if self.future is not None and self.future.done():
            future = self.future
            completed_slot = self.pending_slot
            completed_user_message = self.pending_user_message
            self.future = None
            self.pending_slot = None
            self.pending_user_message = None
            try:
                result, response_seconds, error = future.result()
            except Exception as exc:
                # Planning is optional; even an unexpected planner failure must not stop JEV.
                print(f"daily_plan_error={exc}")
            else:
                if error is not None:
                    print(f"daily_plan_error response_time={response_seconds:.2f}s error={error}")
                elif completed_slot == slot and self.requested_slot == slot and result["day"] == day:
                    if completed_user_message is not None:
                        # Save before runtime quarter/board IDs and progress are added.
                        reply = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
                        self.conversation = deque(recent_exchanges([
                            *self.conversation, (completed_user_message, reply),
                        ]), maxlen=PLAN_HISTORY_TURNS)
                    result["quarter"] = quarter + 1
                    if result.get("kind") == "task_board":
                        self.board_revision += 1
                        result["board_id"] = f"{self.board_run_id}:{self.board_revision}"
                    elif is_sequence(result):
                        self.board_revision += 1
                        result["sequence_id"] = f"{self.board_run_id}:{self.board_revision}"
                    self.plan = result
                    serialized = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
                    preview = serialized[:PLAN_PREVIEW_CHARS]
                    if len(serialized) > PLAN_PREVIEW_CHARS:
                        preview += "…"
                    print(
                        f"daily_plan_ready day={day} quarter={quarter + 1}/4 "
                        f"response_time={response_seconds:.2f}s preview={preview}"
                    )

        view = self._board(state) if self.plan else None
        if is_sequence(view) and view["status"] in {"completed", "failed"}:
            self.previous_board = view
            self._publish_board(view)
            self.plan = None
            self.requested_slot = None
            self.sequence_replan_reason = "sequence_completed" if view["status"] == "completed" else "sequence_failed"
            self.completion_replan_slot = slot
            print(f"action_sequence_finished id={view.get('sequence_id')} status={view['status']} reason={view.get('reason', '')}")
        if (view and view.get("kind") == "task_board" and view.get("status") == "active"
                and view.get("state_basis") == "observed" and view.get("tasks")
                and all(t["status"] == "done" for t in view["tasks"])
                and not state.get("player", {}).get("vitals", {}).get("dead")):
            # Retire this revision once. Never keep it as active guidance while
            # waiting for a new board, or treat predicted completion as real.
            view["status"] = "completed"
            self.previous_board = view
            self._publish_board(view)
            self.plan = None
            self.requested_slot = None
            self.completion_replan_slot = slot
            print(f"task_board_completed id={view['board_id']} reason=all_tasks_completed")

        completing = self.completion_replan_slot == slot
        completion_ready = (not completing or self.last_completion_request is None
                            or time.monotonic() - self.last_completion_request >= COMPLETION_REPLAN_INTERVAL)
        if self.requested_slot != slot and self.future is None and completion_ready and not is_sequence(self.plan):
            self.requested_slot = slot
            reason = self.sequence_replan_reason or ("all_tasks_completed" if completing else "quarter_window_or_invalidation")
            self.sequence_replan_reason = None
            context = planning_context(state, list(self.history))
            context["replan_reason"] = reason
            context["science_machine_built_this_run"] = self.science_machine_built
            context["previous_task_board"] = self._board(state) or self.previous_board
            context["previous_action_sequence"] = self.previous_board if is_sequence(self.previous_board) else None
            context["planning_window"] = {"day": day, "quarter": quarter + 1}
            reference = planning_reference(self.knowledge_path)
            # Snapshot the actual request, not a later observation or mutated board.
            context = planning_snapshot(context)
            self.pending_user_message = snapshot_message(context)
            self.future = self.executor.submit(
                timed_request_plan, self.api_url, self.api_key, self.model, day, context,
                list(self.conversation), reference,
            )
            self.pending_slot = slot
            if completing:
                self.last_completion_request = time.monotonic()
            self.completion_replan_slot = None
            print(
                f"daily_plan_requested day={day} quarter={quarter + 1}/4 "
                f"knowledge_chars={len(reference['dst_knowledge'])} "
                f"history_turns={len(self.conversation)} reason={reason}"
            )
        view = self._board(state) if self.plan and self.plan["day"] == day else None
        self._publish_board(view)
        return view

    def record_action(self, state: dict, action: str, outcome: str, reason: str = "") -> None:
        vitals = state.get("player", {}).get("vitals", {})
        self.history.append({
            "day": state.get("world", {}).get("day"),
            "phase": state.get("world", {}).get("phase"),
            "action": action,
            "outcome": outcome,
            "reason": reason,
            "hunger": vitals.get("hunger"),
            "health": vitals.get("health"),
            "temperature": vitals.get("temperature"),
            "overheating": vitals.get("overheating"),
            "freezing": vitals.get("freezing"),
        })
        if outcome == "failed":
            self.failure_count = self.failure_count + 1 if self.failed_action == action else 1
            self.failed_action = action
        else:
            self.failure_count = 0
            self.failed_action = None
        major_change = outcome == "executed" and action == "build_sciencemachine"
        if major_change:
            self.science_machine_built = True
        if is_priority_board(self.plan):
            if major_change:
                self.goal_replan_reason = "science_machine_completed"
            # Route failure is handled locally; do not invalidate the board.
            return
        if is_sequence(self.plan):
            # SequenceExecutor owns exact step failures/completion. A milestone
            # must not discard the remaining ordered steps during native work.
            return
        repeated_failure = self.failure_count >= 2
        if (major_change or repeated_failure) and time.monotonic() - self.last_replan >= 60:
            self.last_replan = time.monotonic()
            self.requested_slot = None
            if self.plan and self.plan.get("kind") == "task_board":
                self.previous_board = self._board(state)
                self.previous_board["status"] = "invalidated"
                for task in self.previous_board["tasks"]:
                    if task["status"] != "done":
                        task["status"] = "invalidated"
                self._publish_board(self.previous_board)
            self.plan = None
            self.failure_count = 0

    def close(self) -> None:
        self.executor.shutdown(wait=False, cancel_futures=True)
