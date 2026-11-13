"""Bounded strategic tasks, evaluated from facts; never dispatches actions."""
from __future__ import annotations

import copy
import math
import re
import unicodedata
from common.places import HOME_ANCHOR_UNAVAILABLE, PLACE_TARGETS, target_progress
from common.camp_work import CAMP_TARGETS, SAFE_MEALS, validate_target as validate_camp_target, target_error as camp_error
from common.refueling import validate_target as validate_refuel, target_error as refuel_error, maintenance_state as fire_maintenance_state
from common.placement import validate as validate_placement, progress as placement_progress
from common.task_timing import validate_timing, reached, TERMINAL_TASKS, number
from common.coordinates import validate_point, arrived as coordinate_arrived
from common.exploration_metrics import survey_cells, observed_cells

MAX_TASKS = 6
MAX_FOCUS_TASKS = 2


def focus_scope(board):
    """Unfinished focused goals and their unfinished prerequisite closure.

    None means ordinary scheduling; an empty ready subset must not release focus.
    """
    tasks = {t["id"]: t for t in board.get("tasks", [])}
    roots = [t["id"] for t in tasks.values() if t.get("focus") and t.get("status") not in TERMINAL_TASKS | {"scheduled", "maintaining"}]
    if not roots:
        return None
    scope = set()

    def include(identity):
        if identity in scope or identity not in tasks or tasks[identity].get("status") == "done":
            return
        scope.add(identity)
        for dependency in tasks[identity].get("depends_on", []):
            include(dependency)

    for identity in roots:
        include(identity)
    return scope


# Inventory products understood by the shipped collection/crafting/food bridge.
# This is deliberately not a list of world entities (grass, sapling, rock1, ...).
INVENTORY_TARGET_PREFABS = frozenset({
    "cutgrass", "twigs", "flint", "log", "rocks", "goldnugget", "nitre",
    "pinecone", "acorn", "twiggy_nut", "charcoal", "marble", "moonrocknugget", "livinglog",
    "ice", "redgem", "bluegem", "purplegem", "thulecite", "thulecite_pieces",
    "petals", "berries", "berries_cooked", "berries_juicy", "berries_juicy_cooked",
    "carrot", "carrot_cooked", "seeds", "seeds_cooked", "meat", "smallmeat",
    "cookedmeat", "cookedsmallmeat", "axe", "pickaxe", "torch", *SAFE_MEALS,
})
INVENTORY_PREFAB_ALIASES = {"logs": "log"}


def normalize_inventory_prefab(value):
    """Normalize explicit aliases, never guess by stripping plural suffixes."""
    prefab = value.strip().lower() if isinstance(value, str) else ""
    if not re.fullmatch(r"[a-z0-9_]{1,60}", prefab):
        raise ValueError("Invalid inventory task prefab")
    prefab = INVENTORY_PREFAB_ALIASES.get(prefab, prefab)
    if prefab not in INVENTORY_TARGET_PREFABS:
        raise ValueError(f"Unsupported inventory task prefab: {prefab}")
    return prefab


def validate_navigation_targets(targets):
    """Bounded local goals watched during one native navigation action."""
    if not isinstance(targets, list) or len(targets) > MAX_TASKS:
        raise ValueError("Navigation targets require a list of at most six targets")
    if not targets:
        return []
    if any(not isinstance(t, dict) or t.get("type") not in {"inventory", "structure", "hunger_ratio"}
           for t in targets):
        raise ValueError("Navigation targets must be local inventory, structure or hunger goals")
    board = validate_board({"goal": "Watch board work", "tasks": [{"id": str(i), "title": "Navigation interrupt", "target": target}
                                      for i, target in enumerate(targets)]}, 1)
    return [task["target"] for task in board["tasks"]]


def text(value, limit=120):
    return " ".join(value.split())[:limit] if isinstance(value, str) else ""


def validate_board(raw: dict, day: int, *, target_validator=None) -> dict:
    tasks = raw.get("tasks")
    if not isinstance(tasks, list) or not 1 <= len(tasks) <= MAX_TASKS:
        raise ValueError("Task board requires 1-6 tasks")
    result = []
    for task in tasks:
        if not isinstance(task, dict):
            raise ValueError("Task must be an object")
        identity, title = text(task.get("id"), 40), text(task.get("title"), 100)
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", identity) or not title:
            raise ValueError("Task requires an ID and title")
        priority = task.get("priority", 3)
        if type(priority) is not int or not 1 <= priority <= 5:
            raise ValueError("Task priority must be 1-5 (1 highest)")
        focus = task.get("focus", False)
        if type(focus) is not bool:
            raise ValueError("Task focus must be a boolean")
        target = task.get("target")
        if not isinstance(target, dict):
            raise ValueError("Task requires a machine-checkable target")
        kind = target.get("type")
        cleaned = {"type": kind}
        if target_validator is not None:
            try:
                cleaned = target_validator(target)
            except ValueError as exc:
                raise ValueError(f"task {identity}: {exc}") from None
        else:
            if "placement" in target:
                if kind != "structure":
                    raise ValueError("Placement applies only to structure targets")
                cleaned["placement"] = validate_placement(target["placement"])
            if kind in {"inventory", "known_regions"}:
                count = target.get("count")
                if type(count) is not int or not 1 <= count <= 1000:
                    raise ValueError("Task count must be 1-1000")
                cleaned["count"] = count
            if kind in {"inventory", "structure"}:
                recipe_id = target.get("recipe_id")
                if recipe_id is not None:
                    if not isinstance(recipe_id, str) or not re.fullmatch(r"[a-z0-9_]{1,100}", recipe_id):
                        raise ValueError("Invalid recipe ID")
                    cleaned["recipe_id"] = recipe_id
                prefab = (normalize_inventory_prefab(target.get("prefab")) if kind == "inventory" and recipe_id is None
                          else text(target.get("prefab"), 60))
                if not re.fullmatch(r"[a-z0-9_]{1,60}", prefab):
                    raise ValueError("Invalid task prefab")
                if kind == "structure" and recipe_id is None and prefab not in {"campfire", "firepit", "sciencemachine"}:
                    raise ValueError("Unsupported structure task")
                cleaned["prefab"] = prefab
                if "allow_ignite" in target:
                    if kind != "inventory" or prefab != "charcoal" or type(target["allow_ignite"]) is not bool:
                        raise ValueError("allow_ignite is only valid for charcoal inventory goals")
                    if target["allow_ignite"]:
                        cleaned["allow_ignite"] = True
            elif kind in CAMP_TARGETS:
                cleaned.update(validate_camp_target(target))
            elif kind == "refuel":
                cleaned.update(validate_refuel(target))
            elif kind == "move_to":
                cleaned.update(validate_point(target))
            elif kind in PLACE_TARGETS:
                field = {"visit_building": "building_id", "set_home": "anchor_id", "return_home": "home_id", "stay_home": "home_id"}[kind]
                value = target.get(field)
                if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_:.-]{1,100}", value):
                    raise ValueError("Place target requires an observed " + field)
                if kind == "set_home" and not re.fullmatch(r"building:[0-9]+", value):
                    raise ValueError("Set home requires a building:<GUID> anchor")
                cleaned[field] = value
                if kind == "stay_home":
                    radius = target.get("radius", 12)
                    if type(radius) not in {int, float} or not math.isfinite(radius) or not 6 <= radius <= 30:
                        raise ValueError("stay_home radius must be 6-30 world units")
                    cleaned["radius"] = radius
            elif kind == "hunger_ratio":
                ratio = target.get("at_least")
                if type(ratio) not in {int, float} or not math.isfinite(ratio) or not 0 < ratio <= 1:
                    raise ValueError("Invalid hunger target")
                cleaned["at_least"] = float(ratio)
            elif kind in {"visit_region", "survey_region"}:
                cleaned["node_id"] = text(target.get("node_id"), 160)
                if not cleaned["node_id"]:
                    raise ValueError("Visit task requires a known node ID")
                if kind == "survey_region":
                    cleaned["cells"] = survey_cells(target)
            elif kind != "known_regions":
                raise ValueError("Unsupported task target type")
        dependencies = task.get("depends_on", [])
        if (not isinstance(dependencies, list) or len(dependencies) > MAX_TASKS
                or any(not isinstance(d, str) for d in dependencies)):
            raise ValueError("Invalid task dependencies")
        hints = task.get("action_hints", [])
        if not isinstance(hints, list):
            raise ValueError("Invalid task action hints")
        result.append({"id": identity, "title": title, "priority": priority, "focus": focus,
                       "target": cleaned, "depends_on": list(dict.fromkeys(dependencies)),
                       "action_hints": [text(h, 60) for h in hints[:4] if isinstance(h, str)],
                       "reason": text(task.get("reason"), 120)})
        timing, error = validate_timing(task, day)
        result[-1].update(timing)
        if error:
            result[-1]["timing_error"] = error
    by_id = {task["id"]: task for task in result}
    if sum(t["focus"] for t in result if not t.get("timing_error")) > MAX_FOCUS_TASKS:
        raise ValueError("Task board supports at most two focused tasks")
    if len(by_id) != len(result):
        raise ValueError("Duplicate task IDs")
    visited, active = set(), set()

    def visit(identity):
        if identity not in by_id or identity in active:
            raise ValueError("Unknown or cyclic task dependency")
        if identity in visited:
            return
        active.add(identity)
        for dependency in by_id[identity]["depends_on"]:
            visit(dependency)
        active.remove(identity)
        visited.add(identity)
    for identity in by_id:
        visit(identity)
    goal = text(raw.get("goal"), 160)
    if not goal:
        raise ValueError("Task board has no goal")
    errors = [{"task_id": t["id"], "reason": t["timing_error"]} for t in result if t.get("timing_error")]
    return {"kind": "task_board", "schema": 1, "day": day, "goal": goal,
            **({"task_errors": errors} if errors else {}),
            "tasks": result, "replan_if": [text(v, 100) for v in raw.get("replan_if", [])[:3]
                                          if isinstance(v, str)] if isinstance(raw.get("replan_if", []), list) else []}


def board_view(board: dict, state: dict, *, verified_structures=None, target_evaluator=None, completed_task_ids=()) -> dict:
    """Re-evaluate a copy, including for predictive JEV input; never fake progress."""
    result = copy.deepcopy(board)
    inventory = state.get("player", {}).get("inventory", {})
    counts = {}
    for group in ("items", "equipped"):
        for item in inventory.get(group, []):
            name = item.get("prefab")
            counts[name] = counts.get(name, 0) + int(item.get("count", 1))
    graph = state.get("navigation", {}).get("graph") or {}
    vitals = state.get("player", {}).get("vitals", {})
    structures = set(verified_structures if verified_structures is not None else board.get("verified_structures", []))
    result["verified_structures"] = sorted(structures)
    world = state.get("world", {})
    progress = float(world.get("time") or 0)
    quarter = min(4, max(1, int(progress * 4) + 1)) if math.isfinite(progress) else 1
    expired = board.get("execution_policy") != "priority_goals" and (board.get("day") != world.get("day")
               or (board.get("quarter") is not None and board["quarter"] != quarter))
    result["status"] = "expired" if expired else "active"
    result["state_basis"] = "predicted_success" if state.get("_jev_execution_context", {}).get("predicted_success") else "observed"
    result["observed_at"] = state.get("observed_at")
    for task in result["tasks"]:
        if task.get("timing_error"):
            task.update(status="failed", blocked_reason=task["timing_error"], progress={"current":0,"required":1})
            continue
        if task.get("start_at"):
            started = reached(task["start_at"], state)
            if started is not True:
                task.update(status="scheduled",
                            blocked_reason="waiting for start_at" if started is False else "game time unavailable",
                            progress={"current":0,"required":1})
                continue
        target, current, needed, known = task["target"], 0, 1, True
        kind = target["type"]
        missing_reason = "required telemetry missing"
        if target_evaluator is not None:
            current, needed, known, missing_reason = target_evaluator(target, state)
        elif kind == "inventory":
            current, needed = counts.get(target["prefab"], 0), target["count"]
            if target.get("recipe_id"):
                current = state.get("recipe_inventory", {}).get(target["prefab"], current)
        elif kind in CAMP_TARGETS:
            needed = target.get("count", 1)
            missing_reason = camp_error(target, state)
            known = not missing_reason
        elif kind == "refuel":
            needed = target.get("at_least",target.get("count"))
            missing_reason = (refuel_error(target, state.get("places")) or
                              ("refueling telemetry unavailable; update Mod" if not state.get("refueling", {}).get("available") else ""))
            known = not bool(missing_reason)
        elif kind == "move_to":
            done, reason = coordinate_arrived(target, state)
            current, known = int(done), not bool(reason)
            if reason:
                missing_reason = reason
        elif kind in PLACE_TARGETS:
            done, reason = target_progress(target, state.get("places"))
            if kind == "set_home" and reason == HOME_ANCHOR_UNAVAILABLE:
                # No route can restore a missing site ID. DeepSeek must choose a new anchor.
                task.update(status="failed", blocked_reason=reason, progress={"current": 0, "required": 1})
                continue
            current, known = (0 if kind == "stay_home" else int(done)), not bool(reason)
            if reason:
                missing_reason = reason
        elif kind == "known_regions":
            known = graph.get("known_nodes") is not None
            current, needed = int(graph.get("known_nodes") or 0), target["count"]
        elif kind == "hunger_ratio":
            known = number(vitals.get("hunger")) and number(vitals.get("hunger_max")) and vitals["hunger_max"] > 0
            current = vitals["hunger"] / vitals["hunger_max"] if known else 0
            needed = target["at_least"]
        elif kind == "visit_region":
            known = (graph.get("current_node") == target["node_id"] or any(
                r.get("node_id") == target["node_id"] for r in graph.get("routes", [])))
            current = int(graph.get("current_node") == target["node_id"] or (board.get("execution_policy") != "priority_goals" and any(
                r.get("node_id") == target["node_id"] and r.get("entered") for r in graph.get("routes", [])))
            )
        elif kind == "structure":
            if target.get("placement"):
                done, missing_reason = placement_progress(target,state)
                current, known = int(done), not bool(missing_reason)
                if not state.get("telemetry", {}).get("placement_constraints"):
                    known, missing_reason = False, "Placement constraints require updated Mod"
            else:
                current = int(target["prefab"] in structures or any(
                    e.get("prefab") == target["prefab"] for e in state.get("nearby", []) + state.get("recipe_entities", [])))
        elif kind == "survey_region":
            area = (graph.get("current") or {}) if graph.get("current_node") == target["node_id"] else next(
                (r for r in graph.get("routes", []) if r.get("node_id") == target["node_id"]), {})
            cells = observed_cells(area,state.get("navigation",{}).get("cell_size",4))
            known = cells is not None
            current, needed = cells or 0, survey_cells(target)
        task["progress"] = {"current": round(current, 3), "required": needed}
        task["status"] = "done" if known and current >= needed else "in_progress" if known and current > 0 else "ready"
        if kind == "hunger_ratio" and "duration" in task and task["status"] == "done":
            task["status"] = "maintaining"
        task["blocked_reason"] = "" if known else missing_reason
        if kind == "refuel" and "at_least" in target:
            fire = fire_maintenance_state(target,state)
            bounds=fire["bounds"]
            task["fuel_interval"]=bounds
            task["progress"]={"current":bounds["min"] if bounds else 0,"required":target["at_least"]}
            if fire["available"] and fire["satisfied"]:
                task.update(status="maintaining" if "duration" in task else "done",blocked_reason="")
            elif fire["reason"] not in {"", "fuel_settling"}:
                task.update(status="blocked",blocked_reason=fire["reason"])
        if not known:
            task["status"] = "blocked"
    by_id = {task["id"]: task for task in result["tasks"]}
    confirmed = set(completed_task_ids)
    for _ in result["tasks"]:
        for task in result["tasks"]:
            waiting = [d for d in task["depends_on"] if d not in confirmed and by_id[d]["status"] != "done"]
            if task["status"] not in {"failed", "expired", "scheduled"} and waiting:
                task["status"], task["blocked_reason"] = "blocked", "dependencies: " + ",".join(waiting)
    for task in result["tasks"]:
        if expired:
            task["status"] = "expired"
        elif vitals.get("dead") and task["status"] not in TERMINAL_TASKS:
            task["status"], task["blocked_reason"] = "blocked", "player dead"
    result["tasks"].sort(key=lambda t: (t["priority"], t["id"]))
    result["ready_task_ids"] = [t["id"] for t in result["tasks"] if t["status"] in {"ready", "in_progress"}]
    return result


def format_board(board: dict, width: int = 100) -> str:
    """Plain scrolling CLI table: no terminal clearing or extra UI dependency."""
    def clean(value):
        return " ".join("".join(c for c in str(value)
                                if unicodedata.category(c) not in {"Cc", "Cf", "Cs"}).split())

    def size(value):
        return sum(0 if unicodedata.combining(c) else 2 if unicodedata.east_asian_width(c) in "WF" else 1
                   for c in value)

    def fit(value, limit):
        value = clean(value)
        if size(value) > limit:
            clipped = ""
            for char in value:
                if size(clipped + char) > limit - 3:
                    break
                clipped += char
            value = clipped + "..."
        return value + " " * max(0, limit - size(value))

    width = max(80, min(width, 120))
    columns = [12, 3, 11, 12, width - 54]
    border = "+" + "+".join("-" * (n + 2) for n in columns) + "+"

    def row(values):
        return "| " + " | ".join(fit(v, n) for v, n in zip(values, columns)) + " |"

    lines = [f"TASK BOARD day={board.get('day')} quarter={board.get('quarter', '?')}/4 "
             f"[{clean(board.get('status', 'active'))}] id={clean(board.get('board_id', '-'))}",
             "Goal: " + fit(board.get("goal", ""), width - 6).rstrip(), border,
             row(["ID", "Pri", "Status", "Progress", "Task"]), border]
    site = board.get("camp_site")
    if site:
        lines.insert(2,"Camp site: " + fit(f"({site['x']}, {site['z']}) {site.get('reason','')}",width-11).rstrip())
        if site.get("backup"):
            backup=site["backup"]
            lines.insert(3,"Backup: " + fit(f"({backup['x']}, {backup['z']}) {backup.get('reason','')}",width-8).rstrip())
    for task in board.get("tasks", []):
        progress = task.get("progress", {})
        current, required = progress.get("current", "?"), progress.get("required", "?")
        if task.get("target", {}).get("type") == "hunger_ratio" or (task.get("target",{}).get("type")=="refuel" and "at_least" in task["target"]):
            current, required = f"{current:.0%}", f"{required:.0%}"
        lines.append(row([task["id"], task["priority"], task["status"],
                          f"{current}/{required}", ("[FOCUS] " if task.get("focus") else "") + task["title"]]))
        if task.get("target", {}).get("placement"):
            placement = task["target"]["placement"]
            lines.append("  " + fit(f"{task['id']}: near {placement['anchor_id']} radius={placement['radius']}", width-2).rstrip())
        if task.get("fuel_interval"):
            interval=task["fuel_interval"]
            label="fuel" if interval["exact"] else "fuel estimate"
            start="[" if interval["lower_inclusive"] else "("
            end="]" if interval["upper_inclusive"] else ")"
            lines.append("  " + fit(f"{task['id']}: {label} {start}{interval['min']:.0%}, {interval['max']:.0%}{end}",width-2).rstrip())
        if task.get("maintenance"):
            timer=task["maintenance"]
            remaining=timer.get("remaining_days")
            if remaining is not None:
                lines.append("  " + fit(f"{task['id']}: maintain remaining={remaining:g}d",width-2).rstrip())
        if task.get("blocked_reason"):
            lines.append("  " + fit(f"{task['id']}: {task['blocked_reason']}", width - 2).rstrip())
    lines.append(border)
    return "\n".join(lines)
