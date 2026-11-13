"""A single model plans assignments; each assignment reuses the actor contract."""
import copy
import json
from common.task_timing import PHASES, TERMINAL_TASKS
from common.recipes import RECIPE_TOOL,query_recipes
from common.places import PLACE_TOOL,query_places
from common.companion_transfer import COUNTED_TARGETS,STORAGE_TOOL,query_storage
from mcp_agent.companion_planner import CompanionPlanner,CompanionPlanningError,companion_profile,validate_plan
from mcp_agent.compact_tasks import expand_tasks
from mcp_agent.planning_core import request_json_plan,recent_exchanges


COMPACT_FORMAT = (
    'Return one JSON object with assignments keyed by requested buddy ID, e.g. '
    '{"assignments":{"buddy1":{"reply":"我去采草，天黑回来。","tasks":'
    '[{"type":"collect","prefab":"cutgrass","count":4},{"type":"return_owner","at":"night"}]},'
    '"buddy2":{"reply":"我去找树枝，天黑回来。","tasks":'
    '[{"type":"collect","prefab":"twigs","count":4},{"type":"return_owner","at":"night"}]}}}. '
    'For conversation, blockage or verified completion, use a local object with reply and '
    'outcome ("reply", "blocked" or "completed"); completed also requires evidence. '
    'A team outcome is optional (default working). For tasks, omit kind, schema, day, goal and title. '
    'Put type and its arguments directly on each task, with optional priority, focus, depends_on, '
    'start_at, reason and search budgets on the same object. Default priority is 3, or 1 for a timed return. '
    'Use inventory for crafting with recipe_id and collect for raw collection. Both counts mean total carried stock. '
    'at may be a phase string for the next onset, or {day,phase} / {day,progress} for an explicit arrival. Reuse the pending return ID on reviews. '
    'To revise an existing task, reuse its ID. Assign IDs to prerequisites referenced by depends_on; '
    'include those prerequisites in the same assignment. Counted transfers require stable IDs across replans. '
    'Omitted tasks remain on the board; use cancel_task_ids for obsolete work. '
)

def expand_assignment(raw, actor):
    """Translate model-facing tasks before the normal board and capability checks."""
    if not isinstance(raw, dict):
        raise ValueError("Assignment must be an object")
    if "tasks" not in raw:
        return {**raw, "kind": "agent_reply"}
    previous = actor.get("current_task_board") or {}
    old_tasks = {task["id"]: task for task in previous.get("tasks", []) if "id" in task}
    world = actor["observation"].get("world") or {}
    expanded = expand_tasks(raw["tasks"], old_tasks.values())
    for index, (task, row) in enumerate(zip(raw["tasks"], expanded), 1):
        kind = task["type"]
        if kind in COUNTED_TARGETS and "id" not in task:
            raise ValueError(f"tasks[{index}]: counted transfers require a stable id")
        if isinstance(row.get("at"), str):
            phase = row["at"]
            if kind != "return_owner" or phase not in PHASES:
                raise ValueError(f"tasks[{index}].at: use day, dusk or night for return_owner")
            if "id" in task:
                previous_returns = [old_tasks.get(row["id"], {})]
            elif actor.get("replan_reason") not in {None, "player_message", "player_dialogue", "autonomous_goal"}:
                previous_returns = old_tasks.values()
            else:
                previous_returns = ()
            old_at = next((old["at"] for old in previous_returns
                           if (old.get("at") or {}).get("phase") == phase
                           and old.get("status") not in TERMINAL_TASKS), None)
            current = world.get("phase")
            day = world.get("day", 1)
            if old_at:
                row["at"] = old_at
            else:
                if current in PHASES and PHASES[current] >= PHASES[phase]:
                    day += 1
                row["at"] = {"day": day, "phase": phase}
        if kind == "return_owner" and "at" in row:
            row.setdefault("priority", 1)
    return {**raw, "kind": "task_board", "goal": raw.get("goal", str(raw.get("reply", ""))[:160]),
            "tasks": expanded}


def validate_team_plan(raw,context,catalogs=None):
    if not isinstance(raw, dict):
        raise ValueError("Return an object with assignments keyed by requested buddy ID")
    if "reply" in raw:
        raise ValueError("team_plan.reply is unsupported; put each companion's first-person speech in its assignment reply")
    assignments=raw.get("assignments")
    requested=set(context["request_ids"])
    compact = isinstance(assignments, dict)
    if compact:
        assignments = [{"buddy_id": identity, "plan": plan} for identity, plan in assignments.items()]
    elif raw.get("kind") != "team_plan":
        raise ValueError("Return assignments keyed by requested buddy ID")
    if not isinstance(assignments,list) or len(assignments)!=len(requested):raise ValueError("Provide one assignment for each request_id")
    clean=[];seen=set()
    for assignment in assignments:
        if not isinstance(assignment,dict):raise ValueError("Assignment must be an object")
        identity=assignment.get("buddy_id")
        if not isinstance(identity, str) or identity not in requested or identity in seen:raise ValueError("Only requested companions may be modified, once each")
        actor=context["members"][identity]
        local={**actor,"_recipe_catalog":(catalogs or {}).get(identity) or actor["observation"].get("recipes",{})}
        try:
            raw_plan = assignment.get("plan")
            plan = validate_plan(expand_assignment(raw_plan, actor) if compact else raw_plan, local)
        except ValueError as exc:raise ValueError(f"assignments[{identity}].plan: {exc}") from None
        if plan.get("kind")!="agent_reply" and not plan.get("reply"):
            raise ValueError(f"assignments[{identity}].plan.reply: provide this companion's own first-person reply")
        clean.append({"buddy_id":identity,"plan":plan});seen.add(identity)
    outcome=raw.get("outcome","working")
    if outcome not in ("working","completed","blocked","reply"):raise ValueError("Invalid team outcome")
    if outcome=="completed":
        local_completed={a["buddy_id"] for a in clean if a["plan"].get("kind")=="agent_reply" and a["plan"].get("outcome")=="completed"}
        if local_completed!=requested:
            raise ValueError("Team completion requires every requested companion to confirm its completed contribution")
        for identity,actor in context["members"].items():
            if not actor["interaction"]["enabled"]:continue
            if identity not in local_completed:
                # A valid local completion must not retry or stop a read-only teammate.
                outcome="working";break
        if context["interaction"].get("mode")=="autonomous":
            outcome="working"
    return {"kind":"team_plan","assignments":clean,"outcome":outcome}


class CompanionTeamPlanner(CompanionPlanner):
    def __call__(self,context,history,cancel):
        prompt=(
            "You are ONE coordinator for up to two DST companions owned by one human. Return JSON only. "
            + COMPACT_FORMAT +
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
            "All query tools require buddy_id. Query the actor you plan to assign the task to. "
            "Use 1-6 tasks per working companion. Priority ranges from 1 (highest) to 5. "
            "Use start_at only for an explicit time requirement. Query tools at most four times per response. "
            "Use observed evidence before declaring the whole instruction complete. Plan essential survival and prerequisites "
            "without adding unrelated objectives. In autonomous mode continue toward the player's long-term goal. "
            + companion_profile(compact=True)
        )
        catalogs={identity:dict(actor["observation"].get("recipes") or {}) for identity,actor in context["members"].items()}
        tools=[];functions={}
        def actor(identity):
            if identity not in context["members"]:raise ValueError("Unknown buddy_id")
            return context["members"][identity]
        def recipes(buddy_id,**args):
            row=actor(buddy_id)
            if "recipes" not in row["observation"].get("capabilities",[]):raise ValueError("Recipe queries are not advertised for this buddy_id")
            if cancel.is_set():raise RuntimeError("Planning superseded by newer player intent")
            # Only actors waiting for this plan can use the native read-only queue.
            if self.recipe_query and buddy_id in context["request_ids"]:
                result=self.recipe_query({**args,"buddy_id":buddy_id},context,cancel)
            else:
                result=query_recipes(catalogs[buddy_id],**args)
                result["source"]="current_cached_catalog"
            for item in result.get("recipes",[]):catalogs[buddy_id][item["recipe_id"]]=item
            return result
        functions["query_recipes"]=recipes
        def storage(buddy_id,**args):
            row=actor(buddy_id)
            if "storage" not in row["observation"].get("capabilities",[]):raise ValueError("Storage queries are not advertised for this buddy_id")
            return query_storage(row["observation"],**args)
        def places(buddy_id,**args):
            row=actor(buddy_id)
            if "visit_building" not in row["observation"].get("capabilities",[]):raise ValueError("Building queries are not advertised for this buddy_id")
            return query_places(row["observation"].get("places"),**args)
        functions["query_storage"]=storage;functions["query_places"]=places
        for source in (RECIPE_TOOL,STORAGE_TOOL,PLACE_TOOL):
            tool=copy.deepcopy(source);params=tool["function"]["parameters"]
            params["properties"]["buddy_id"]={"type":"string","enum":list(context["members"])}
            params["required"]=list(dict.fromkeys([*params.get("required",[]),"buddy_id"]))
            tools.append(tool)
        body={"model":self.model,"messages":[{"role":"system","content":prompt},
            *[m for user,assistant in recent_exchanges(history) for m in ({"role":"user","content":user},{"role":"assistant","content":assistant})],
            {"role":"user","content":json.dumps(context,ensure_ascii=False)}],"tools":tools,
            "response_format":{"type":"json_object"},"max_tokens":8192,"reasoning_effort":"medium","stream":False}
        def safe(detail):return str(detail).replace(self.key,"[REDACTED]")[:1000] if self.key else str(detail)[:1000]
        accepted_response = None

        def validate(raw, _):
            nonlocal accepted_response
            result = validate_team_plan(raw, context, catalogs)
            accepted_response = json.dumps(raw, ensure_ascii=False)
            return result

        try:
            result=request_json_plan(self.api_url,self.key,body,validate,functions=functions,
                cancel_event=cancel,on_error=lambda stage,detail:self.error_handler(stage,safe(detail)) if self.error_handler else None,
                log_prefix="companion_team_plan",repair_instruction=(
                    COMPACT_FORMAT + "Provide exactly one assignment per request_ids entry. "
                    "Put each companion's speech in its own reply."))
            return {**result, "_catalogs": catalogs, "_assistant_message": accepted_response}
        except RuntimeError as exc:
            raise CompanionPlanningError(type(exc).__name__,safe(exc),http_status=getattr(exc,"http_status",None)) from None
