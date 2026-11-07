"""A single model plans assignments; each assignment reuses the actor contract."""
import copy
import json

from common.recipes import RECIPE_TOOL,query_recipes
from common.places import PLACE_TOOL,query_places
from common.companion_transfer import STORAGE_TOOL,query_storage
from mcp_agent.companion_planner import CompanionPlanner,CompanionPlanningError,companion_profile,validate_plan
from mcp_agent.planning_core import PLANNING_CONTRACT,request_json_plan,recent_exchanges


def validate_team_plan(raw,context,catalogs=None):
    if not isinstance(raw,dict) or raw.get("kind")!="team_plan":raise ValueError("Return kind=team_plan with assignments")
    if "reply" in raw:
        raise ValueError("team_plan.reply is unsupported; put each companion's first-person speech in assignments[].plan.reply")
    assignments=raw.get("assignments")
    requested=set(context["request_ids"])
    if not isinstance(assignments,list) or len(assignments)!=len(requested):raise ValueError("Provide one assignment for each request_id")
    clean=[];seen=set()
    for assignment in assignments:
        if not isinstance(assignment,dict):raise ValueError("Assignment must be an object")
        identity=assignment.get("buddy_id")
        if identity not in requested or identity in seen:raise ValueError("Only requested companions may be modified, once each")
        actor=context["members"][identity]
        if type(assignment.get("base_revision")) is not int or assignment["base_revision"]!=actor["interaction"]["revision"]:
            raise ValueError("Copy each actor's current base_revision")
        local={**actor,"_recipe_catalog":(catalogs or {}).get(identity) or actor["observation"].get("recipes",{})}
        try:plan=validate_plan(assignment.get("plan"),local)
        except ValueError as exc:raise ValueError(f"assignments[{identity}].plan: {exc}") from None
        if plan.get("kind")!="agent_reply" and not plan.get("reply"):
            raise ValueError(f"assignments[{identity}].plan.reply: provide this companion's own first-person reply")
        clean.append({"buddy_id":identity,"base_revision":assignment["base_revision"],"plan":plan});seen.add(identity)
    outcome=raw.get("outcome","working")
    if outcome not in {"working","completed","blocked","reply"}:raise ValueError("Invalid team outcome")
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
        if "members" not in context:return super().__call__(context,history,cancel)
        prompt=(
            "You are ONE coordinator for up to two DST companions owned by one human. Return JSON only. "
            "Top-level schema: {kind:'team_plan',outcome:'working|completed|blocked|reply',"
            "assignments:[{buddy_id:'buddy1',base_revision:CURRENT_MEMBER_REVISION,plan:LOCAL_PLAN}]}. "
            "Provide exactly one assignment per request_ids entry. Other companions are read-only and CONTINUE executing; do not stop, rename or modify them. "
            "LOCAL_PLAN is the existing task_board or agent_reply schema described below. There is NO global shared action queue. "
            "Every local task_board requires its own nonempty goal string; a team-level goal does not replace it. "
            "There is NO top-level reply or team speaker. Every LOCAL_PLAN must include its own reply: concise Chinese first-person speech by that actor. "
            "The matching companion speaks through its native actor and chat; the coordinator never speaks to the player. "
            "For shared work each requested actor states only its own contribution, not a repeated full-team summary. "
            "For a named request only that actor speaks; do not give dialogue assignments to read-only companions. "
            "Completion, limitations and requests for materials are also spoken by the relevant companion through its local agent_reply or board reply. "
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
            "A LOCAL_PLAN completed reply confirms only that actor's assigned contribution; only the TEAM outcome confirms the whole request. "
            "outcome=completed requires every enabled actor's local completed reply with observed evidence. "
            "When only your requested actor finishes while a read-only teammate still works, return its local completed reply with team outcome working. "
            "A player's @ scope is resolved to request_ids by code, never by you. Invented actor IDs and assignments outside that scope are invalid. "
            "Inventory, technology, task dependencies and at most two focus tasks are PER ACTOR. Never add cross-actor depends_on IDs. "
            "Each actor has at most six local tasks. Existing task IDs/counts and native handoff receipts must survive replans. "
            "Buildings reported by teammates can be shared; they do not share an inventory, recipe unlocks or physical proximity. "
            "No direct buddy-to-buddy transfer exists yet. Actors can use chests or hand materials to their human owner. "
            "All query tools require buddy_id. Query the actor you plan to assign the task to. "
            "The following common rules and capability profile apply INSIDE each assignment's LOCAL_PLAN:\n"
            +PLANNING_CONTRACT+companion_profile()+
            "\nFINAL OUTPUT REMINDER: output ONE team_plan envelope, with assignments for request_ids only; do not return a bare task_board."
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
        try:
            result=request_json_plan(self.api_url,self.key,body,lambda raw,_:validate_team_plan(raw,context,catalogs),functions=functions,
                cancel_event=cancel,on_error=lambda stage,detail:self.error_handler(stage,safe(detail)) if self.error_handler else None,
                log_prefix="companion_team_plan",repair_instruction=(
                    "Return ONE team_plan JSON envelope with exactly one assignment per request_ids entry and matching base_revision. "
                    "Omit top-level reply; put each companion's own Chinese first-person speech in its assignments[].plan.reply. "
                    "Each local task_board needs its own goal; a completed agent_reply needs nonempty string reply AND evidence (single line, <=256 characters). "))
            return {**result,"_catalogs":catalogs}
        except RuntimeError as exc:raise CompanionPlanningError(type(exc).__name__,safe(exc)) from None
