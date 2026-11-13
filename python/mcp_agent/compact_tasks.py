"""Expand flat model task rows into the shared internal board format."""
from itertools import count

from common.task_board import MAX_TASKS
from common.task_timing import TIME_FIELDS


TASK_OPTIONS = {
    "id", "title", "priority", "focus", "depends_on", "action_hints", "reason",
    "search_budget_seconds", "search_budget_distance", *TIME_FIELDS,
}


def expand_tasks(tasks, previous_tasks):
    if not isinstance(tasks, list) or not 1 <= len(tasks) <= MAX_TASKS:
        raise ValueError("Plan requires 1-6 tasks")
    used_ids = {task["id"] for task in previous_tasks if "id" in task}
    for index, task in enumerate(tasks, 1):
        if not isinstance(task, dict) or not isinstance(task.get("type"), str):
            raise ValueError(f"tasks[{index}] requires a top-level type")
        if "target" in task:
            raise ValueError(f"tasks[{index}]: place target arguments beside type")
        if "id" in task:
            if not isinstance(task["id"], str):
                raise ValueError(f"tasks[{index}].id must be a string")
            used_ids.add(task["id"])

    available_ids = (f"task_{i}" for i in count(1) if f"task_{i}" not in used_ids)
    expanded = []
    for index, task in enumerate(tasks, 1):
        kind = task["type"]
        target = {key: value for key, value in task.items() if key not in TASK_OPTIONS}
        if kind == "collect":
            if "recipe_id" in target:
                raise ValueError(f"tasks[{index}]: use inventory for crafting with recipe_id")
            target["type"] = "inventory"
        row = {key: value for key, value in task.items() if key in TASK_OPTIONS}
        if "id" not in row:
            row["id"] = next(available_ids)
        label = target.get("prefab")
        row.setdefault("title", f"{kind} {label}" if isinstance(label, str) else kind)
        row["target"] = target
        expanded.append(row)
    return expanded
