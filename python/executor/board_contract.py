"""Pure task-board contract validation, compact-assignment expansion, and exchange trimming."""
from __future__ import annotations

from common import autonomy
from common.companion_transfer import COUNTED_TARGETS, STORAGE_TARGETS, TRANSFER_TARGETS
from common.task_board import TEAM_DEP_RE
from common.task_timing import PHASES, TERMINAL_TASKS
from executor.compact_tasks import expand_tasks
from executor.companion_tasks import (
    RETURN_ALERTS,
    capabilities,
    collection_prefab,
    validate_board,
    validate_target,
)
from executor.priority_board import merge_goals, task_signature

PLAN_HISTORY_TURNS = 3
PLAN_HISTORY_CHARS = 48000


def validate_reply(raw, context):
    """Return a checked conversational/completion reply, or None for a board."""
    if isinstance(raw, dict) and raw.get("kind") == "agent_reply":
        interaction = context.get("interaction") or {}
        if interaction.get("mode") not in {"instruction", "autonomous"}:
            raise ValueError("Chat replies require a dialogue-capable client")
        outcome = raw.get("outcome")
        if outcome not in {"reply", "completed", "blocked"}:
            raise ValueError("Invalid reply outcome")
        try:
            reply = autonomy.validate_text(raw.get("reply"))
        except ValueError as exc:
            raise ValueError(f"agent_reply.reply: {exc}") from None
        evidence = raw.get("evidence")
        if outcome == "completed":
            if interaction["mode"] != "instruction":
                raise ValueError("Autonomous mode continues planning after milestones")
            board = context.get("current_task_board")
            if board and any(task.get("status") != "done" for task in board.get("tasks", [])):
                raise ValueError("Cannot complete while accepted tasks remain unfinished; revise/cancel obsolete tasks first")
            try:
                autonomy.validate_text(evidence)
            except ValueError as exc:
                raise ValueError(f"agent_reply.evidence: {exc}") from None
        return {"kind": "agent_reply", "outcome": outcome, "reply": reply, "evidence": evidence}
    return None


def recent_exchanges(exchanges, *, turns=PLAN_HISTORY_TURNS, max_chars=PLAN_HISTORY_CHARS):
    """Keep a chronological suffix of complete user/assistant pairs, never fragments."""
    retained, size = [], 0
    for user, assistant in reversed(list(exchanges)[-turns:]):
        pair_size = len(user) + len(assistant)
        if size + pair_size > max_chars:
            break
        retained.append((user, assistant))
        size += pair_size
    return list(reversed(retained))


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
        if isinstance(target, dict) and target.get("type") in {"inventory"} | (TRANSFER_TARGETS - {"inspect_storage"}) and observed is not None:
            task["target"] = {**target, "prefab": collection_prefab(target.get("prefab"), observed)}
    plan = validate_board(raw, day, observed=observed)
    if observed is not None:
        catalog = context.get("_recipe_catalog") or observed.get("recipes") or {}
        for task in plan["tasks"]:
            target = task["target"]
            if task.get("at") and "timed_return" not in capabilities(observed):
                raise ValueError("Timed return requires an updated companion mod with live clock and speed")
            historical_done = any(old.get("status") == "done" and old.get("id") == task["id"]
                                  and task_signature(old) == task_signature(task)
                                  for old in (context.get("current_task_board") or {}).get("tasks", []))
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
        merged = merge_goals(previous, plan, target_validator=lambda t: validate_target(t, supported=capabilities(observed)))
        signatures = {task["id"]: task_signature(task) for task in merged["tasks"]}
        if any(signatures.get(key) != signature for key, signature in protected.items()):
            raise ValueError("Preserve the pending arrival time unless the player explicitly changes the return")
    return plan


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
            raise ValueError(f"tasks[{index}]: counted item tasks require a stable id")
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


def validate_team_plan(raw, context, catalogs=None):
    if not isinstance(raw, dict):
        raise ValueError("Return an object with assignments keyed by requested buddy ID")
    if "reply" in raw:
        raise ValueError("team_plan.reply is unsupported; put each companion's first-person speech in its assignment reply")
    assignments = raw.get("assignments")
    requested = set(context["request_ids"])
    compact = isinstance(assignments, dict)
    if compact:
        assignments = [{"buddy_id": identity, "plan": plan} for identity, plan in assignments.items()]
    elif raw.get("kind") != "team_plan":
        raise ValueError("Return assignments keyed by requested buddy ID")
    if not isinstance(assignments, list) or len(assignments) != len(requested):
        raise ValueError("Provide one assignment for each request_id")
    clean = []
    seen = set()
    for assignment in assignments:
        if not isinstance(assignment, dict):
            raise ValueError("Assignment must be an object")
        identity = assignment.get("buddy_id")
        if not isinstance(identity, str) or identity not in requested or identity in seen:
            raise ValueError("Only requested companions may be modified, once each")
        actor = context["members"][identity]
        local = {**actor, "_recipe_catalog": (catalogs or {}).get(identity) or actor["observation"].get("recipes", {})}
        try:
            raw_plan = assignment.get("plan")
            plan = validate_plan(expand_assignment(raw_plan, actor) if compact else raw_plan, local)
        except ValueError as exc:
            raise ValueError(f"assignments[{identity}].plan: {exc}") from None
        if plan.get("kind") != "agent_reply" and not plan.get("reply"):
            raise ValueError(f"assignments[{identity}].plan.reply: provide this companion's own first-person reply")
        clean.append({"buddy_id": identity, "plan": plan})
        seen.add(identity)
    # Validate cross-actor depends_on (Team DAG): existence + global acyclicity.
    team_tasks = {}
    for member_id, actor in (context.get("members") or {}).items():
        for row in ((actor.get("current_task_board") or {}).get("tasks") or []):
            if isinstance(row, dict) and isinstance(row.get("id"), str):
                team_tasks[f"{member_id}:{row['id']}"] = list(row.get("depends_on") or [])
    for item in clean:
        mid, plan = item["buddy_id"], item["plan"]
        if plan.get("kind") == "task_board":
            for row in plan.get("tasks", []):
                team_tasks[f"{mid}:{row['id']}"] = list(row.get("depends_on") or [])
    team_deps = {}
    for fq_id, deps in team_tasks.items():
        mid = fq_id.split(":", 1)[0]
        resolved = []
        for dep in deps:
            m = TEAM_DEP_RE.fullmatch(dep)
            if m:
                if m.group(1) == mid:
                    raise ValueError(f"{fq_id}: use unqualified local task id for same-actor dependency '{dep}'")
                if dep not in team_tasks:
                    raise ValueError(f"{fq_id}: unknown cross-actor dependency '{dep}'")
                resolved.append(dep)
            else:
                resolved.append(f"{mid}:{dep}")
        team_deps[fq_id] = resolved
    visited_t, active_t = set(), set()

    def _visit_team(node):
        if node in active_t:
            raise ValueError(f"Cyclic cross-actor task dependency involving '{node}'")
        if node in visited_t or node not in team_deps:
            return
        active_t.add(node)
        for dep in team_deps[node]:
            _visit_team(dep)
        active_t.remove(node)
        visited_t.add(node)

    for fq_id in team_deps:
        _visit_team(fq_id)
    outcome = raw.get("outcome", "working")
    if outcome not in ("working", "completed", "blocked", "reply"):
        raise ValueError("Invalid team outcome")
    if outcome == "completed":
        local_completed = {a["buddy_id"] for a in clean if a["plan"].get("kind") == "agent_reply" and a["plan"].get("outcome") == "completed"}
        if local_completed != requested:
            raise ValueError("Team completion requires every requested companion to confirm its completed contribution")
        for identity, actor in context["members"].items():
            if not actor["interaction"]["enabled"]:
                continue
            if identity not in local_completed:
                # A valid local completion must not retry or stop a read-only teammate.
                outcome = "working"
                break
        if context["interaction"].get("mode") == "autonomous":
            outcome = "working"
    return {"kind": "team_plan", "assignments": clean, "outcome": outcome}
