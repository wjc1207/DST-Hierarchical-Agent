"""Bounded strategic tasks, evaluated from facts; never dispatches actions."""
from __future__ import annotations

import copy
import math
import re
import unicodedata

MAX_TASKS = 6
# Inventory products understood by the shipped collection/crafting/food bridge.
# This is deliberately not a list of world entities (grass, sapling, rock1, ...).
INVENTORY_TARGET_PREFABS = frozenset({
    "cutgrass", "twigs", "flint", "log", "rocks", "goldnugget", "nitre",
    "pinecone", "acorn", "charcoal", "marble", "moonrocknugget", "livinglog",
    "ice", "redgem", "bluegem", "purplegem", "thulecite", "thulecite_pieces",
    "petals", "berries", "berries_cooked", "berries_juicy", "berries_juicy_cooked",
    "carrot", "carrot_cooked", "seeds", "seeds_cooked", "meat", "smallmeat",
    "cookedmeat", "cookedsmallmeat", "axe", "pickaxe", "torch",
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


def text(value, limit=120):
    return " ".join(value.split())[:limit] if isinstance(value, str) else ""


def validate_board(raw: dict, day: int) -> dict:
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
        target = task.get("target")
        if not isinstance(target, dict):
            raise ValueError("Task requires a machine-checkable target")
        kind = target.get("type")
        cleaned = {"type": kind}
        if kind in {"inventory", "known_regions"}:
            count = target.get("count")
            if type(count) is not int or not 1 <= count <= 1000:
                raise ValueError("Task count must be 1-1000")
            cleaned["count"] = count
        if kind in {"inventory", "structure"}:
            prefab = (normalize_inventory_prefab(target.get("prefab")) if kind == "inventory"
                      else text(target.get("prefab"), 60))
            if not re.fullmatch(r"[a-z0-9_]{1,60}", prefab):
                raise ValueError("Invalid task prefab")
            if kind == "structure" and prefab not in {"campfire", "firepit", "sciencemachine"}:
                raise ValueError("Unsupported structure task")
            cleaned["prefab"] = prefab
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
                area = target.get("area", 256)
                if type(area) is not int or not 16 <= area <= 4096:
                    raise ValueError("Invalid additional survey area")
                cleaned["area"] = area
        elif kind != "known_regions":
            raise ValueError("Unsupported task target type")
        dependencies = task.get("depends_on", [])
        if (not isinstance(dependencies, list) or len(dependencies) > MAX_TASKS
                or any(not isinstance(d, str) for d in dependencies)):
            raise ValueError("Invalid task dependencies")
        hints = task.get("action_hints", [])
        if not isinstance(hints, list):
            raise ValueError("Invalid task action hints")
        result.append({"id": identity, "title": title, "priority": priority,
                       "target": cleaned, "depends_on": list(dict.fromkeys(dependencies)),
                       "action_hints": [text(h, 60) for h in hints[:4] if isinstance(h, str)],
                       "reason": text(task.get("reason"), 120)})
    by_id = {task["id"]: task for task in result}
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
    return {"kind": "task_board", "schema": 1, "day": day, "goal": goal,
            "tasks": result, "replan_if": [text(v, 100) for v in raw.get("replan_if", [])[:3]
                                          if isinstance(v, str)] if isinstance(raw.get("replan_if", []), list) else []}


def board_view(board: dict, state: dict, *, verified_structures=None) -> dict:
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
        target, current, needed, known = task["target"], 0, 1, True
        kind = target["type"]
        if kind == "inventory":
            current, needed = counts.get(target["prefab"], 0), target["count"]
        elif kind == "known_regions":
            known = graph.get("known_nodes") is not None
            current, needed = int(graph.get("known_nodes") or 0), target["count"]
        elif kind == "hunger_ratio":
            known = vitals.get("hunger") is not None and float(vitals.get("hunger_max") or 0) > 0
            current = float(vitals.get("hunger") or 0) / max(1, float(vitals.get("hunger_max") or 1))
            needed = target["at_least"]
        elif kind == "visit_region":
            known = (graph.get("current_node") == target["node_id"] or any(
                r.get("node_id") == target["node_id"] for r in graph.get("routes", [])))
            current = int(graph.get("current_node") == target["node_id"] or (board.get("execution_policy") != "priority_goals" and any(
                r.get("node_id") == target["node_id"] and r.get("entered") for r in graph.get("routes", [])))
            )
        elif kind == "structure":
            current = int(target["prefab"] in structures or any(
                e.get("prefab") == target["prefab"] for e in state.get("nearby", [])))
        elif kind == "survey_region":
            area = (graph.get("current") or {}) if graph.get("current_node") == target["node_id"] else next(
                (r for r in graph.get("routes", []) if r.get("node_id") == target["node_id"]), {})
            known = area.get("explored_area") is not None
            current, needed = area.get("explored_area", 0), target["area"]
        task["progress"] = {"current": round(current, 3), "required": needed}
        task["status"] = "done" if known and current >= needed else "in_progress" if known and current > 0 else "ready"
        task["blocked_reason"] = "" if known else "required telemetry missing"
        if not known:
            task["status"] = "blocked"
    by_id = {task["id"]: task for task in result["tasks"]}
    for _ in result["tasks"]:
        for task in result["tasks"]:
            waiting = [d for d in task["depends_on"] if by_id[d]["status"] != "done"]
            if task["status"] != "done" and waiting:
                task["status"], task["blocked_reason"] = "blocked", "dependencies: " + ",".join(waiting)
    for task in result["tasks"]:
        if expired:
            task["status"] = "expired"
        elif vitals.get("dead") and task["status"] != "done":
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
    for task in board.get("tasks", []):
        progress = task.get("progress", {})
        current, required = progress.get("current", "?"), progress.get("required", "?")
        if task.get("target", {}).get("type") == "hunger_ratio":
            current, required = f"{current:.0%}", f"{required:.0%}"
        lines.append(row([task["id"], task["priority"], task["status"],
                          f"{current}/{required}", task["title"]]))
        if task.get("blocked_reason"):
            lines.append("  " + fit(f"{task['id']}: {task['blocked_reason']}", width - 2).rstrip())
    lines.append(border)
    return "\n".join(lines)
