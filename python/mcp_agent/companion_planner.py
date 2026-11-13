"""Companion planning profile; request policy and task scheduling are shared."""
import json
import copy
import re

from common import autonomy
from common.paths import DATA_DIR
from common.recipes import RECIPE_TOOL, query_recipes
from common.companion_transfer import TRANSFER_TARGETS, STORAGE_TARGETS, STORAGE_TOOL, query_storage
from common.places import PLACE_TOOL, query_places
from mcp_agent.planning_core import (PLANNING_CONTRACT, request_json_plan, validate_reply,
                                     recent_exchanges, load_knowledge, PLAN_MAX_TOKENS)
from mcp_agent.companion_tasks import (RETURN_ALERTS, CompanionTasks, collection_prefab, capabilities, validate_board, validate_target)
from mcp_agent.priority_board import task_signature, merge_goals


RETURN_WORDING = r"回(?:来|(?:到)?(?:我|主人|玩家)?(?:身边|旁边))"
NIGHT_RETURN = re.compile(r"(?:天黑|入夜|今晚|晚上|夜间)(?:的时候|时|之前|以前|前|之后|以后|后|了)?"
                          r"(?:\s|再|就|才|都|一起|各自)*" + RETURN_WORDING)
RETURN_CLAUSE = re.compile(RETURN_WORDING)
TIMED_RETURN_FORMAT = (
    'Timed return example: {"id":"back","title":"Return at night",'
    '"target":{"type":"return_owner"},"at":{"day":1,"phase":"night"}}. '
    'return_owner.target contains only type; at belongs to the task alongside target. '
)


def explicitly_return_at_night(interaction):
    # Only lock a literal phase attached to a return request. Later return
    # instructions supersede it; other messages leave it in force.
    texts = [interaction.get("instruction"), *(row["text"] for row in interaction.get("messages", []))]
    for text in reversed(texts):
        if not text:
            continue
        clauses = re.findall(r"([^，,。；;！？!?]+)([，,。；;！？!?]?)", text)
        for clause, punctuation in reversed(clauses):
            if punctuation not in {"?", "？"} and RETURN_CLAUSE.search(clause):
                return bool(NIGHT_RETURN.search(clause)) and not re.search(r"不要|不用|不必|别", clause)
    return False


class CompanionPlanningError(RuntimeError):
    """Safe diagnostic only; no raw request or API credentials."""
    def __init__(self, kind, detail, *, http_status=None):
        self.http_status = http_status
        self.safe_detail = f"{kind}: {detail}"
        super().__init__(self.safe_detail)


def validate_plan(raw, context=None):
    """Accept the common board/reply contract; translate first-prototype plans at the boundary."""
    if not isinstance(raw, dict):
        raise ValueError("Companion plan must be an object")
    context = context if context is not None else {"interaction": {"mode": "instruction"}}
    observed = context.get("observation")
    day = ((observed or {}).get("world") or {}).get("day", 1)
    raw = dict(raw)
    if "done" in raw:
        if type(raw["done"]) is not bool:
            raise ValueError("Companion done must be a boolean")
        if raw["done"] or raw.get("blocked") is True:
            raw = {"kind": "agent_reply", "outcome": "completed" if raw["done"] else "blocked",
                   "reply": raw.get("reply"), "evidence": raw.get("evidence")}
        else:
            rows = []
            for task in raw.get("tasks", []):
                kind = task.get("type")
                target = {"type": {"collect": "inventory", "move": "move_to"}.get(kind, kind)}
                for key in ("prefab", "count", "x", "z"):
                    if key in task:
                        target[key] = task[key]
                rows.append({"id": task.get("id"), "title": task.get("title", kind), "target": target,
                             "depends_on": [rows[-1]["id"]] if rows else []})
            raw = {"goal": raw.get("reply"), "reply": raw.get("reply"), "tasks": rows}
    reply = validate_reply(raw, context)
    previous = context.get("current_task_board") or {}
    reason = context.get("replan_reason")
    protected = {task["id"]: task_signature(task) for task in previous.get("tasks", [])
                 if task.get("at") and task.get("status") not in {"done", "failed", "expired"}}
    if reason in {None, "player_message", "player_dialogue", "autonomous_goal"}:
        protected = {}
    if protected and reply and reply["outcome"] == "blocked":
        raise ValueError("Keep the pending return and use outcome reply to report other blocked work")
    if reason in RETURN_ALERTS.values() and (not reply or reply["outcome"] != "reply"):
        raise ValueError("Return timing advisory requires agent_reply with outcome reply; the existing return continues")
    if reply is not None:
        return reply
    # Canonicalize entity names before they become stable task signatures.
    if not isinstance(raw.get("tasks"), list) or any(not isinstance(t, dict) for t in raw["tasks"]):
        raise ValueError("Plan tasks must be a list of objects")
    raw["tasks"] = [dict(task) for task in raw["tasks"]]
    for task in raw["tasks"]:
        target = task.get("target") or {}
        if isinstance(target, dict) and target.get("type") in {"inventory"} | (TRANSFER_TARGETS-{"inspect_storage"}) and observed is not None:
            task["target"] = {**target, "prefab": collection_prefab(target.get("prefab"), observed)}
    plan = validate_board(raw, day, observed=observed)
    if observed is not None:
        catalog = context.get("_recipe_catalog") or observed.get("recipes") or {}
        for task in plan["tasks"]:
            target = task["target"]
            if task.get("at") and "timed_return" not in capabilities(observed):
                raise ValueError("Timed return requires an updated companion mod with live clock and speed")
            historical_done = any(old.get("status")=="done" and old.get("id")==task["id"]
                                  and task_signature(old)==task_signature(task)
                                  for old in (context.get("current_task_board") or {}).get("tasks",[]))
            if historical_done:
                continue  # Retained history needs no current chest/recipe discovery; it will not execute again.
            if target["type"] in STORAGE_TARGETS and not any(r["guid"] == target["container_guid"] for r in observed.get("containers", [])):
                raise ValueError("Storage task requires a currently observed chest GUID")
            if target["type"] == "visit_building" and not any(r["id"] == target["building_id"] and r.get("status") not in {"missing", "unavailable"}
                    for r in (observed.get("places") or {}).get("buildings", [])):
                raise ValueError("Visit task requires a remembered available building ID")
            if target.get("recipe_id"):
                row = catalog.get(target["recipe_id"], {})
                if (not row.get("supported") or row.get("product") != target["prefab"]
                        or bool(row.get("placement")) != (target["type"] == "structure")):
                    raise ValueError("Task requires a matching queried supported recipe: " + target["recipe_id"])
    if "reply" in raw:
        try:
            plan["reply"] = autonomy.validate_text(raw["reply"])
        except ValueError as exc:
            raise ValueError(f"task_board.reply: {exc}") from None
    if observed is not None:
        # This is an authoritative runtime view, including latched completion/receipts.
        merged = merge_goals(previous, plan, target_validator=lambda t: validate_target(t,supported=capabilities(observed)))
        signatures = {task["id"]: task_signature(task) for task in merged["tasks"]}
        if any(signatures.get(key) != signature for key, signature in protected.items()):
            raise ValueError("Preserve the pending arrival time unless the player explicitly changes the return")
        if explicitly_return_at_night(context.get("interaction") or {}) and "timed_return" in capabilities(observed):
            completed = {task["id"]: task_signature(task) for task in previous.get("tasks", [])
                         if task.get("status") == "done"}
            for task in merged["tasks"]:
                if (task["target"]["type"] == "return_owner" and not task.get("timing_error")
                        and completed.get(task["id"]) != signatures[task["id"]]
                        and (task.get("at") or {}).get("phase") != "night"):
                    raise ValueError("Player specified the return time: use return_owner with at.phase=night")
    return plan


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
        "query_storage reads local ordinary/dragonfly chest slots. Busy or absent chest contents are unknown, never empty. "
        "A busy chest pauses its task locally up to 60 seconds and automatically resumes on closing; do not replan merely for short occupancy. "
        "Use only observed container_guid values; after building a new chest, observe/review before planning its interactions. "
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
        "Structure x/z is a nearby observed placement CENTER within 12 units of buddy; executor chooses a legal land site within radius. "
        "An existing structure completes only within that area. Home management is not supported yet. "
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
        "Do not create unrelated move_to tasks to bypass search budgets. No full-world region graph, unseen-building search, cooking or combat exists yet. "
        "No JEV fallback exists. Explain unsupported requests using agent_reply blocked. "
        "Never invent GUIDs or unseen coordinates; move_to is limited to 20 units, with land checked by the server. "
        "return_owner tracks the moving human and completes within 4 units; follow/stay complete on observed mode. "
        "When timed_return is advertised, return_owner accepts task-level at:{day,phase:day|dusk|night} or at:{day,progress:0..1 exclusive} "
        "for a target arrival time. Earlier arrival counts as complete. "
        + ("" if compact else TIMED_RETURN_FORMAT) +
        "Use at.phase=night for '天黑回到身边', '晚上回来', and '天黑前回来'. "
        "Use the requested game day. The executor estimates travel from live distance and the clock "
        "and starts the return in time. Return on arrival regardless of lateness, "
        "and preserve a pending arrival time in reviews unless the player changes it. Keep a time-sensitive return independent of optional gathering. "
        "world.next_phase, seconds_until_next_phase and phase_duration_seconds describe this world's current clock. "
        "For return_at_risk or return_deadline_missed, the companion is already returning. "
        "Use agent_reply with outcome reply to explain the delay and optionally ask the owner to meet halfway. "
        "Use depends_on for genuinely ordered work, e.g. gather then return_owner. Otherwise priorities may interleave. "
        "Keep at most two focus tasks. For ordinary tasks other than return_owner, start_at schedules a game-time start. "
        "duration/end_at are unsupported for these targets. "
        "For player_dialogue, the companion keeps working during planning. Use active_task_id and observation.action as context. "
        "For greetings and status questions, return agent_reply with outcome reply; keep the board and mode. "
        "If the latest message changes the work, patch the existing board and explicitly cancel obsolete tasks. "
        "For ordered work, keep the required prerequisite task and make the new task depend on it. "
        "The runtime applies updates between native actions using its latest observation. "
        "After completing the WHOLE instruction, reply with observed evidence; the native actor then follows the owner. "
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


class CompanionPlanner:
    """Bind the shared DS request profile to the current host's read-only MCP queries."""
    def __init__(self, api_url, key, model):
        self.api_url, self.key, self.model = api_url, key, model
        self.recipe_query = None
        self.error_handler = None

    def set_query_handler(self, handler):
        self.recipe_query = handler

    def set_error_handler(self, handler):
        self.error_handler = handler

    def __call__(self, context, history, cancel):
        return request_plan(self.api_url, self.key, self.model, context, history, cancel,
                            recipe_query=self.recipe_query, on_error=self.error_handler)


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
