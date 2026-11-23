"""Companion planning profile; request policy and task scheduling are shared."""
import json
import copy

from common.paths import DATA_DIR
from common.recipes import RECIPE_TOOL, query_recipes
from common.camp_work import MEALS_TOOL, query_meals
from common.companion_transfer import STORAGE_TOOL, query_storage
from common.places import PLACE_TOOL, query_places
from executor.board_contract import expand_assignment, validate_plan, validate_team_plan
from executor.companion_tasks import CompanionTasks, capabilities, collection_prefab
from mcp_agent.planning_core import (PLANNING_CONTRACT, request_json_plan,
                                     recent_exchanges, load_knowledge, PLAN_MAX_TOKENS)


TIMED_RETURN_FORMAT = (
    'Timed return example: {"id":"back","title":"Return at night",'
    '"target":{"type":"return_owner"},"at":{"day":1,"phase":"night"}}. '
    'return_owner.target contains only type; at belongs to the task alongside target. '
)


class CompanionPlanningError(RuntimeError):
    """Safe diagnostic only; no raw request or API credentials."""
    def __init__(self, kind, detail, *, http_status=None):
        self.http_status = http_status
        self.safe_detail = f"{kind}: {detail}"
        super().__init__(self.safe_detail)


def companion_profile(*, compact=False):
    profile = (
        "Actor profile: server-side Wilson companion; owner is human, buddy is the actor. "
        + ("" if compact else "Every task_board must include kind:'task_board', a nonempty goal STRING, and tasks:[...]. ") +
        "For agent_reply use reply as a nonempty single-line STRING of at most 256 characters. "
        "A completed reply additionally requires evidence as a nonempty single-line STRING of at most 256 characters, not an object/list/null. "
        "The supported_capabilities in the FINAL snapshot are authoritative; never infer missing abilities. "
        "Supported targets: inventory {prefab,count,recipe_id?,allow_search?}, structure {prefab,recipe_id,x,z,radius:2..8(default6)}, "
        "equip {prefab}, unequip {prefab}, visit_building {building_id}, move_to {x,z,arrival_radius:2.5}, return_owner {}, follow {}, stay {}. All targets include type. "
        + ("" if compact else "Use only the fields listed for each target. Put timing, priorities, dependencies and search budgets on the task alongside target. ") +
        "When eat is advertised, use eat {prefab,count?:1} to eat a named carried food. "
        "Observation foods lists edible stacks with prefab, count, hunger_delta, sanity_delta and health_delta per bite. "
        "Negative values mean losses. These are estimates before caps and special effects. For named food, keep that exact prefab even if it has penalties. "
        "For a generic food request, choose carried food with suitable effects; do not guess that every inventory item is edible. "
        "Eating count is verified units consumed by this task; reuse its ID on retries and use a new ID for a new meal. "
        "Additional targets when advertised: give_owner {prefab,count}, drop_item {prefab,count}, "
        "deposit/withdraw {container_guid,prefab,count}, inspect_storage {container_guid}, receive_item {prefab,count,timeout_seconds?:10..600(default120)}. "
        "Transfer count is a cumulative number actually handed over/deposited/withdrawn/dropped for this task ID, NOT destination stock. "
        "Keep transfer task IDs stable during replans; count changes set a new cumulative total. Use a NEW task ID for a genuinely new repeated request. "
        "Completed tasks retained with the SAME ID and target are history, even if their ingredients were consumed or handed over. "
        "Do not re-gather consumed stock or repeat returning merely to keep historical milestones currently satisfied. "
        "Use NEW task IDs for genuinely new repeated work; omit completed history when it is no longer needed. "
        "receive_item means wait for the player to voluntarily give items, with count as total buddy stock; no owner inventory reading or taking exists. "
        "Ask for missing supplied materials in reply, then wait using receive_item with dependent craft steps. "
        "Players can give using native game interaction or drop resources for ordinary inventory goals to pick up. "
        "give_owner only gives to the actual owner, approaching them first. drop_item puts items at the buddy's feet and does NOT prove owner received them. "
        "Newly crafted tools can auto-equip. Outgoing transfers automatically unequip the requested tool when needed, then transfer it. "
        "Explicit unequip {prefab} stows the item in real inventory; can_unequip/unequip_reason report capacity or restrictions. "
        "Unequipping never intentionally drops overflow; free a slot if inventory is full. Backpacks requiring ground drops are not stowed. "
        "Transfers use unequipped main/backpack items; full/restricted destinations report failure and never deliberately drop overflow. "
        "query_storage reads local ordinary/dragonfly chest and icebox slots. Busy or absent container contents are unknown, never empty. "
        "A busy container pauses its task locally up to 60 seconds and automatically resumes on closing; do not replan merely for short occupancy. "
        "Use observed building_id (or legacy container_guid) values; after building a new container, observe/review before planning its interactions. "
        "Inventory count is TOTAL current stock, not additional items. For another 3 add current inventory. "
        "Use nearby[].product (cutgrass, twigs), not the source entity prefab (grass, sapling). "
        "Inventory goals gather nearby loose items and pickables, and use advertised work_targets to chop trees or mine rocks. "
        "The executor equips a carried suitable tool automatically. Plan making a missing axe/pickaxe when the job requires it. "
        "Twigs can be picked from saplings and flint picked up from the ground without a tool. "
        "If neither is in view, use an inventory goal with search enabled when the request allows it. "
        "Query query_recipes for live server recipe ingredients, product, technology, support and readiness. "
        "Set exact queried recipe_id for crafting inventory items and all structures, including intermediates rope/boards/cutstone. "
        "Use exact server products: researchlab for the science machine, never the old client alias sciencemachine. "
        "Plan missing ingredients, tools and technology prerequisites explicitly with depends_on; no automatic crafting dependency solver exists. "
        "A supported but not-ready recipe may be a future goal after prerequisites. Never invent a recipe or use the owner's materials. "
        "Structure supports either placement:{anchor_id,radius} or nearby x/z/radius within 12 units of buddy; executor chooses a legal land site within radius. "
        "An existing structure completes only within that area. "
        "When home capability is advertised, set_home {anchor_id}, return_home {home_id} and stay_home {home_id, radius?} with duration or end_at are supported. "
        "When refuel capability is advertised, refuel {building_id, fuel_prefab, count|at_least} supports campfire, firepit, coldfire and coldfirepit (nitre or burnable fuels) and optional duration maintenance with at_least. "
        "When hunger capability is advertised, hunger_ratio {at_least} eats carried safe food and supports optional duration maintenance. "
        "When heal capability is advertised, healers[] lists carried medical items (healingsalve, bandage, etc.); use heal {prefab, count?:1} for explicit usage or health_ratio {at_least} (with optional duration maintenance) to apply carried healers first and fall back to health-restoring foods. "
        "When thermal capability is advertised, world exposes season/remaining_days_in_season/world_temperature/israining/issnowing, buddy exposes temperature/moisture/freezing/overheating, and thermal exposes stones[] (heatrock guid/temperature/range/durability) plus equipped winter_insulation/summer_insulation/waterproof. "
        "Use thermal_comfort {min_temp?:15, max_temp?:55, charge_heatrock?:'warm'|'cool', max_moisture?:5..80} (with optional duration maintenance) to equip or craft ready seasonal gear (winterhat/earmuffshat/beefalohat, strawhat/umbrella/grass_umbrella, rainhat) and heatrock, place firepit/campfire or coldfirepit/coldfire when ready and none exists, refuel/warm/cool or roast/chill thermal stones, and dry off by a fire when wet. "
        "Plan 4-season preparation ahead of transitions: winter gear & heatrock/firepit in late autumn, waterproof protection/drying for spring rain, and strawhat/umbrella/coldfirepit/coldfire with nitre for summer heat. "
        "When cook_meal is advertised, query_meals lists verified Crock Pot combinations for cook_meal {building_id, product, ingredients} (including meatballs, ratatouille, perogies, baconeggs, bonestew, honeyham, honeynuggets, taffy, trailmix, dragonpie). "
        "When the owner asks you to cook a meal, NEVER plan an eat task to consume the cooked dish yourself unless the owner explicitly tells you to eat it; keep it in inventory, hand it over with give_owner if asked, or store it if asked. "
        "Do not plan eat unless the owner asks you to eat or your own hunger is genuinely low. "
        "When ignite_tree is advertised, ignite_tree {tree_guid} ignites an observed safe isolated tree (evergreen/deciduoustree/twiggytree) with a carried torch. "
        "When combat is advertised, observed enemies appear in enemies[] with targeting_owner/targeting_buddy/hostile/health. "
        "Use attack {target_guid} or attack {prefab, count} to hunt/slay enemies, or defend_owner {} (with optional task-level duration) to protect the owner and engage threats near them. "
        "The executor automatically equips carried armor, helmet, and best melee weapon before fighting, and safely retreats if health drops below min_health_ratio (default 0.35). "
        "places contains bounded memory of buildings actually seen by the buddy; query_places can filter it. "
        "An unconfirmed distant building is REMEMBERED, not missing. Use visit_building with its ID to return over long distances. "
        "Never use move_to for a distant remembered building: move_to still has a 20-unit limit and its center may be occupied. "
        "When technology is missing, prefer returning to an adequate remembered researchlab/researchlab2/etc before proposing another one. "
        "Use visit_building as an explicit prerequisite of prototype crafting. Arrival refreshes live technology, which must still permit the recipe. "
        "When search is advertised, inventory goals automatically use local terrain observations and memory to search for missing raw resources. "
        "Do not report blocked merely because nearby[] is empty. Set allow_search:false for requests explicitly limited to the current area. "
        "perception contains sampled local cells, hazards and server-validated short-leg points; it is not a world map or region graph. "
        "search_memory describes historical observations, never proof of current availability. "
        "Keep task IDs, search_budget_seconds and search_budget_distance stable so reviews do not reset spent search budgets. "
        "On search budget exhaustion, assess progress once; retain a useful task to resume after cooldown or explain blockage. "
        "Do not create unrelated move_to tasks to bypass search budgets. No full-world region graph or unseen-building search exists yet. "
        "Explain unsupported requests using agent_reply blocked. "
        "Never invent GUIDs or unseen coordinates; move_to is limited to 20 units, with land checked by the server. "
        "return_owner tracks the moving human and completes within 4 units; follow/stay complete on observed mode. "
        "When timed_return is advertised, return_owner accepts task-level at:{day,phase:day|dusk|night} or at:{day,progress:0..1 exclusive} "
        "for a target arrival time. Earlier arrival counts as complete. "
        + ("" if compact else TIMED_RETURN_FORMAT) +
        "Derive return commitments from the current instruction, applicable messages, or explicit autonomous goal. "
        "A gathering-only request does not imply a return deadline. Keep dusk or night return out of both the reply and the tasks unless requested. "
        "Preserve a pending return still required by the active request, but do not carry a return from an unrelated past request into a new one. "
        "Use at.phase=night only for explicit requests such as '天黑回到身边', '晚上回来', and '天黑前回来'. "
        "Use the requested game day. The executor estimates travel from live distance and the clock "
        "and starts the return in time. Return on arrival regardless of lateness, "
        "and preserve a pending arrival time in reviews unless the player changes it. Keep a time-sensitive return independent of optional gathering. "
        "world.next_phase, seconds_until_next_phase and phase_duration_seconds describe this world's current clock. "
        "For only_scheduled_tasks, keep pending tasks and their exact timing. "
        "Add immediate work only if it advances the active instruction or autonomous goal and leaves time for the scheduled work. "
        "If waiting is appropriate, use an ordinary reply without completing the instruction. "
        "For return_at_risk or return_deadline_missed, the companion is already returning. "
        "Use agent_reply with outcome reply to explain the delay and optionally ask the owner to meet halfway. "
        "Use depends_on for genuinely ordered work, e.g. gather then return_owner. Otherwise priorities may interleave. "
        "Keep at most two focus tasks. For ordinary tasks other than return_owner, start_at schedules a game-time start. "
        "stay_home requires duration or end_at; hunger_ratio and ratio-based refuel accept optional duration. "
        "For player_dialogue, the companion keeps working during planning. Use active_task_id and observation.action as context. "
        "For greetings and status questions, return agent_reply with outcome reply; keep the board and mode. "
        "If the latest message changes the work, patch the existing board and explicitly cancel obsolete tasks. "
        "For ordered work, keep the required prerequisite task and make the new task depend on it. "
        "The runtime applies updates between native actions using its latest observation. "
        "After completing the WHOLE instruction, reply with observed evidence; the native actor then follows the owner (or stays in place when is_self=true). "
        "If search is unavailable/forbidden or no safe new search point remains, explain the observed limit. "
        "Static game knowledge below is background only; it does not grant unsupported actions.\n"
    )
    if compact:
        profile = profile.replace("agent_reply", "local reply").replace("task-level at:", "task field at:")
    return profile + load_knowledge(DATA_DIR / "DSTKnowledge.txt")


def request_plan(api_url, key, model, context, history, cancel, *, recipe_query=None, on_error=None):
    observed = context.get("observation") or {}
    context = {**context, "supported_capabilities": capabilities(observed)}
    prompt = PLANNING_CONTRACT + companion_profile()
    body = {"model": model, "messages": [{"role": "system", "content": prompt},
            *[message for user, assistant in recent_exchanges(history)
              for message in ({"role": "user", "content": user}, {"role": "assistant", "content": assistant})],
            {"role": "user", "content": json.dumps(context, ensure_ascii=False)}],
            "response_format": {"type": "json_object"}, "max_tokens": PLAN_MAX_TOKENS,
            "reasoning_effort": "medium", "stream": False}
    catalog = dict(observed.get("recipes") or {})
    functions = {}
    if "visit_building" in capabilities(observed):
        functions["query_places"] = lambda **args: query_places(observed.get("places"), **args)
        tool = copy.deepcopy(PLACE_TOOL)
        tool["function"]["description"] = "Query buildings actually observed by this companion, including distant unconfirmed memory. Use remembered building IDs for visit_building; missing/unavailable is distinct from out of local range. No home management."
        body.setdefault("tools", []).append(tool)
    if "storage" in capabilities(observed):
        functions["query_storage"] = lambda **args: query_storage(observed, **args)
        body.setdefault("tools", []).append(STORAGE_TOOL)
    if "cook_meal" in capabilities(observed):
        functions["query_meals"] = lambda **args: query_meals(observed.get("camp_work") or {}, **args)
        body.setdefault("tools", []).append(MEALS_TOOL)
    if "recipes" in capabilities(observed):
        def query(**args):
            if cancel.is_set():
                raise RuntimeError("Planning superseded by newer player intent")
            result = recipe_query(args, context, cancel) if recipe_query else query_recipes(catalog, **args)
            for row in result.get("recipes", []):
                catalog[row["recipe_id"]] = row
            return result
        functions["query_recipes"] = query
        body.setdefault("tools", []).append(RECIPE_TOOL)
    validation_context = {**context, "_recipe_catalog": catalog}
    def clean(detail):
        text = str(detail)
        if key:
            text = text.replace(key, "[REDACTED]")
        return text[:1000]
    def report(stage, detail):
        if on_error:
            on_error(stage, clean(detail))
    try:
        return request_json_plan(api_url, key, body, lambda raw, _: validate_plan(raw, validation_context), functions=functions,
                                 cancel_event=cancel, day=(observed.get("world") or {}).get("day", 1),
                                 log_prefix="companion_plan", on_error=report)
    except RuntimeError as exc:
        raise CompanionPlanningError(type(exc).__name__, clean(exc), http_status=getattr(exc, "http_status", None)) from None


COMPACT_FORMAT = (
    'Return one JSON object with assignments keyed by requested buddy ID, e.g. '
    '{"assignments":{"buddy1":{"reply":"我去采草。","tasks":'
    '[{"type":"collect","prefab":"cutgrass","count":4}]},'
    '"buddy2":{"reply":"我去找树枝。","tasks":'
    '[{"type":"collect","prefab":"twigs","count":4}]}}}. '
    'For conversation, blockage or verified completion, use a local object with reply and '
    'outcome ("reply", "blocked" or "completed"); completed also requires evidence. '
    'A team outcome is optional (default working). For tasks, omit kind, schema, day, goal and title. '
    'Put type and its arguments directly on each task, with optional priority, focus, depends_on, '
    'start_at, duration, end_at, reason and search budgets on the same object. Default priority is 3, or 1 for a timed return. '
    'Use inventory for crafting with recipe_id and collect for raw collection. Both counts mean total carried stock. '
    'Only when the active player request or autonomous goal specifies returning at night, use a timed return such as {"type":"return_owner","at":"night"}. '
    'at may be a phase string for the next onset, or {day,phase} / {day,progress} for an explicit arrival. Reuse the pending return ID on reviews. '
    'To revise an existing task, reuse its ID. Assign IDs to prerequisites referenced by depends_on; '
    'include those prerequisites in the same assignment. Counted transfers and eat tasks require stable IDs across replans. '
    'Omitted tasks remain on the board; use cancel_task_ids for obsolete work. '
)


class CompanionPlanner:
    """Unified coordinator for one or more bound DST companions."""
    def __init__(self, api_url, key, model, *, memory_store=None, on_usage=None, preserve_history=False):
        self.api_url, self.key, self.model = api_url, key, model
        self.recipe_query = None
        self.error_handler = None
        self.memory_store = memory_store
        self.on_usage = on_usage
        self.preserve_history = preserve_history

    def set_memory_store(self, memory_store):
        self.memory_store = memory_store

    def set_usage_handler(self, handler):
        self.on_usage = handler

    def set_query_handler(self, handler):
        self.recipe_query = handler

    def set_error_handler(self, handler):
        self.error_handler = handler

    def __call__(self, context, history, cancel):
        if "members" not in context:
            return request_plan(self.api_url, self.key, self.model, context, history, cancel,
                                recipe_query=self.recipe_query, on_error=self.error_handler)
        prompt = (
            "You are ONE coordinator for up to two DST companions and optionally the owner's own piloted character (buddy_id=\"self\", is_self=true). Return JSON only. "
            + COMPACT_FORMAT +
            "When buddy_id=\"self\" (is_self=true) is requested, that actor IS the human owner's character: do NOT assign follow, return_owner, give_owner, or receive_item to \"self\"; after finishing, speak in first person as the player's autopilot instead of promising to follow. "
            "Provide exactly one assignment per request_ids entry. Other companions continue executing. "
            "Each requested companion speaks in concise single-line Chinese in its own reply. "
            "The matching companion speaks through its native actor and chat; the coordinator never speaks to the player. "
            "For shared work each requested actor states only its own contribution, not a repeated full-team summary. "
            "For a named request only that actor speaks; do not give dialogue assignments to read-only companions. "
            "Completion, limitations and requests for materials are also spoken by the relevant companion through its local reply. "
            "All local reply and completed evidence fields must be nonempty single-line strings <=256 characters; do not send null. "
            "Assign useful complementary work for a shared request, considering actual positions, carried tools and ingredients. "
            "Each member's interaction.mode decides its work. Autonomous members pursue the shared long_term_goal "
            "and need a new milestone after a completed board. Use working as the team outcome while autonomy is enabled. "
            "An instruction member executes its own interaction.instruction and messages before returning to autonomy; "
            "finish that contribution with a local completed reply when verified. Do not copy its temporary task to autonomous members. "
            "Use the member's current mode and messages, not older team chat, to decide whether a temporary request is still active. "
            "When no further autonomous work is useful or feasible with advertised capabilities, reply blocked for that member so it can wait. "
            "An idle unneeded requested actor may reply that it will follow; do not invent work for it. "
            "Check the whole human request against all actors' results. Completing one contribution does not finish the team. "
            "A completed local reply confirms only that actor's assigned contribution; only the TEAM outcome confirms the whole request. "
            "outcome=completed requires every enabled actor's local completed reply with observed evidence. "
            "When only your requested actor finishes while a read-only teammate still works, return its local completed reply with team outcome working. "
            "A player's @ scope is resolved to request_ids by code, never by you. Invented actor IDs and assignments outside that scope are invalid. "
            "Inventory, technology, task dependencies and at most two focus tasks are PER ACTOR. Never add cross-actor depends_on IDs. "
            "Buildings reported by teammates can be shared; they do not share an inventory, recipe unlocks or physical proximity. "
            "No direct buddy-to-buddy transfer exists yet. Actors can use chests or hand materials to their human owner. "
            "Game query tools (query_recipes, query_storage, query_places, query_meals) require buddy_id. Query the actor you plan to assign the task to. "
            "Memory tools (memory_search, memory_read, memory_write, memory_delete) do NOT take buddy_id. "
            "Use 1-6 tasks per working companion. Priority ranges from 1 (highest) to 5. "
            "Use start_at only for an explicit time requirement. Query tools at most four times per response. "
            "Use observed evidence before declaring the whole instruction complete. Plan essential survival and prerequisites "
            "without adding unrelated objectives. In autonomous mode continue toward the player's long-term goal. "
            + ("\n\n" + self.memory_store.index_prompt() + "\n\n" if self.memory_store is not None else "")
            + companion_profile(compact=True)
        )
        catalogs = {identity: dict(actor["observation"].get("recipes") or {}) for identity, actor in context["members"].items()}
        tools = []
        functions = {}
        def actor(identity):
            if identity not in context["members"]:
                raise ValueError("Unknown buddy_id")
            return context["members"][identity]
        def recipes(buddy_id, **args):
            row = actor(buddy_id)
            if "recipes" not in row["observation"].get("capabilities", []):
                raise ValueError("Recipe queries are not advertised for this buddy_id")
            if cancel.is_set():
                raise RuntimeError("Planning superseded by newer player intent")
            # Only actors waiting for this plan can use the native read-only queue.
            if self.recipe_query and buddy_id in context["request_ids"]:
                result = self.recipe_query({**args, "buddy_id": buddy_id}, context, cancel)
            else:
                result = query_recipes(catalogs[buddy_id], **args)
                result["source"] = "current_cached_catalog"
            for item in result.get("recipes", []):
                catalogs[buddy_id][item["recipe_id"]] = item
            return result
        functions["query_recipes"] = recipes
        def storage(buddy_id, **args):
            row = actor(buddy_id)
            if "storage" not in row["observation"].get("capabilities", []):
                raise ValueError("Storage queries are not advertised for this buddy_id")
            return query_storage(row["observation"], **args)
        def places(buddy_id, **args):
            row = actor(buddy_id)
            if "visit_building" not in row["observation"].get("capabilities", []):
                raise ValueError("Building queries are not advertised for this buddy_id")
            return query_places(row["observation"].get("places"), **args)
        def meals(buddy_id, **args):
            row = actor(buddy_id)
            if "cook_meal" not in row["observation"].get("capabilities", []):
                raise ValueError("Meal queries are not advertised for this buddy_id")
            return query_meals(row["observation"].get("camp_work") or {}, **args)
        functions["query_storage"] = storage
        functions["query_places"] = places
        functions["query_meals"] = meals
        for source in (RECIPE_TOOL, STORAGE_TOOL, PLACE_TOOL, MEALS_TOOL):
            tool = copy.deepcopy(source)
            params = tool["function"]["parameters"]
            params["properties"]["buddy_id"] = {"type": "string", "enum": list(context["members"])}
            params["required"] = list(dict.fromkeys([*params.get("required", []), "buddy_id"]))
            tools.append(tool)
        if self.memory_store is not None:
            functions.update(self.memory_store.tool_functions())
            tools.extend(self.memory_store.tool_specs())
        prior_turns = list(history) if self.preserve_history else recent_exchanges(history)
        body = {"model": self.model, "messages": [{"role": "system", "content": prompt},
            *[m for user, assistant in prior_turns for m in ({"role": "user", "content": user}, {"role": "assistant", "content": assistant})],
            {"role": "user", "content": json.dumps(context, ensure_ascii=False)}], "tools": tools,
            "response_format": {"type": "json_object"}, "max_tokens": 8192, "reasoning_effort": "medium", "stream": False}
        def safe(detail):
            return str(detail).replace(self.key, "[REDACTED]")[:1000] if self.key else str(detail)[:1000]
        accepted_response = None

        def validate(raw, _):
            nonlocal accepted_response
            result = validate_team_plan(raw, context, catalogs)
            accepted_response = json.dumps(raw, ensure_ascii=False)
            return result

        try:
            result = request_json_plan(self.api_url, self.key, body, validate, functions=functions,
                cancel_event=cancel, on_error=lambda stage, detail: self.error_handler(stage, safe(detail)) if self.error_handler else None,
                on_usage=self.on_usage, log_prefix="companion_team_plan", repair_instruction=(
                    COMPACT_FORMAT + "Provide exactly one assignment per request_ids entry. "
                    "Put each companion's speech in its own reply."))
            return {**result, "_catalogs": catalogs, "_assistant_message": accepted_response}
        except RuntimeError as exc:
            raise CompanionPlanningError(type(exc).__name__, safe(exc), http_status=getattr(exc, "http_status", None)) from None


CompanionTeamPlanner = CompanionPlanner


def next_action(plan, state, done):
    """Compatibility API for prototype callers; all evaluation uses the common scheduler."""
    runtime = CompanionTasks()
    runtime.adopt(plan, state)
    for identity in done:
        if identity in runtime.goals.records:
            runtime.goals.records[identity]["done"] = True
    result = runtime.next_action(state)
    done.update(task["id"] for task in runtime.view()["tasks"] if task["status"] == "done")
    return result
