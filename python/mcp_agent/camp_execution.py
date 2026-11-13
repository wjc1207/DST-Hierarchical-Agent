"""Deterministic scheduling for camp tasks, using only native action offers."""
from collections import Counter

from common.camp_work import fixture, loaded
from common.task_timing import number


def progress(task, record, state):
    target = task["target"]
    kind = target["type"]
    row = fixture(state, target.get("building_id")) or {}
    phase = row.get("phase")
    count = record.get("camp_count", 0)
    task["progress"]["current"] = count
    if count >= task["progress"]["required"]:
        task["status"] = "done"
    elif kind == "inspect_storage" and row.get("contents_known"):
        task.update(status="done", progress={"current": 1, "required": 1})
    elif kind == "cook_meal":
        due = record.get("meal_due")
        if due is not None:
            now = state.get("observed_at")
            task["meal_started"] = True
            if phase == "ready":
                task["status"] = "in_progress"
            elif phase in {"open", "closed", "burnt"} or (number(now) and now > due + 90):
                task.update(status="failed", blocked_reason="started meal lost; inspect the pot before retrying")
            elif phase == "unavailable" or not number(now) or now < due:
                task["status"] = "maintaining"
            else:
                task["status"] = "in_progress"
        elif phase in {"ready", "unavailable"}:
            task.update(status="blocked", blocked_reason="pot occupied by another or untracked meal")
        elif row.get("contents_known") and loaded(row) - Counter(target["ingredients"]):
            task.update(status="blocked", blocked_reason="pot contains different ingredients; no changes made")
    elif count:
        task["status"] = "in_progress"


def choices(task, actions, state):
    target = task["target"]
    kind = target["type"]
    if kind == "ignite_tree":
        guid = target["tree_guid"]
        matches = [(k, a) for k, a in actions.items() if a.get("kind") == kind and a.get("guid") == guid]
        ignitable = any(r.get("guid") == guid and r.get("status") == "ignitable"
                        for r in (state.get("camp_work") or {}).get("trees", []))
        return matches or ([("equip_ignition_torch", actions["equip_ignition_torch"])]
                           if ignitable and "equip_ignition_torch" in actions else [])
    identity = target["building_id"]
    row = fixture(state, identity) or {}
    offers = [(k, a) for k, a in actions.items() if a.get("building_id") == identity and a.get("planner_only")]
    if kind == "cook_meal" and task.get("meal_started"):
        wanted = [(k, a) for k, a in offers if a["kind"] == "harvest_meal"]
    elif kind == "cook_meal" and row.get("contents_known"):
        missing = Counter(target["ingredients"]) - loaded(row)
        wanted = [(k, a) for k, a in offers if (
            (a["kind"] == "store_item" and missing.get(a.get("product"), 0) > 0)
            or (a["kind"] == "start_meal" and not missing and a.get("product") == target["product"]))]
    elif kind in {"deposit", "withdraw"} and row.get("contents_known"):
        transfer = "store_item" if kind == "deposit" else "take_item"
        wanted = [(k, a) for k, a in offers if a["kind"] == transfer and a.get("product") == target["prefab"]]
    else:
        wanted = [(k, a) for k, a in offers if a["kind"] == "open_container"]
    return wanted or [(k, a) for k, a in offers if a["kind"] == "explore"]


def on_completed(target, record, action, reason, state):
    kind = target["type"]
    operation = action.get("kind")
    if ((kind == "ignite_tree" and operation == "ignite_tree" and reason == "tree_ignited")
            or (kind in {"deposit", "withdraw"} and operation == {"deposit": "store_item", "withdraw": "take_item"}[kind]
                and reason == "one_item_transferred")
            or (kind == "cook_meal" and operation == "harvest_meal" and reason == "meal_harvested:" + target["product"])):
        record["camp_count"] = record.get("camp_count", 0) + 1
    if kind == "cook_meal" and operation == "harvest_meal" and reason != "meal_harvested:" + target["product"]:
        record["camp_error"] = "harvest produced a different dish"
    if kind == "cook_meal" and operation == "start_meal" and reason == "pot_started":
        stamp = state.get("observed_at")
        record["meal_due"] = (stamp if number(stamp) else 0) + action.get("cook_seconds", 30)


def burning(state):
    return any(row.get("status") == "burning" for row in (state.get("camp_work") or {}).get("trees", []))
