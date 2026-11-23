"""Companion facts/actions adapted to the same priority board used by the player."""
from collections import Counter
import copy
import math
import re
import time
import uuid

from common.camp_work import (fixture as camp_fixture, loaded as camp_loaded,
                              target_error as camp_error, validate_target as validate_camp_target)
from common.coordinates import validate_point
from common.task_timing import number, seconds_to
from common.places import PLACE_TARGETS, target_progress as building_progress
from common.placement import validate as validate_placement, resolve as placement_area, inside as inside_placement
from common.refueling import (validate_target as validate_refuel, maintenance_state as fire_maintenance_state,
                              target_error as refuel_error, has_fuel)
from common.thermal import seasonal_gear_equip, thermal_comfort_status, refuel_pause
from executor import camp_execution
from executor.priority_board import PriorityBoardExecutor, validate_goals, merge_goals
from executor.companion_search import ResourceSearch
from common.companion_transfer import (COUNTED_TARGETS, STORAGE_TARGETS, TRANSFER_TARGETS,
                                       container_guid_for, validate_target as validate_transfer)

CAPABILITIES = {"collect", "move", "follow", "stay", "recipes", "equip", "unequip", "work", "craft", "place", "search",
                "storage", "give_owner", "drop_item", "receive_item", "visit_building", "timed_return", "eat",
                "heal", "thermal", "refuel", "home", "hunger", "cook_meal", "ignite_tree", "combat"}
RESOURCE_NAMES = {"grass": "cutgrass", "sapling": "twigs", "berrybush": "berries", "berrybush2": "berries",
                  "carrot_planted": "carrot", "reeds": "cutreeds", "logs": "log"}
STORAGE_WAIT_SECONDS = 60
HAND_GATHERED_TOOL_MATERIALS = {"twigs", "flint"}
RETURN_RADIUS = 4
RETURN_DEPARTURE_BUFFER = 2
RETURN_ALERTS = {"at_risk": "return_at_risk", "missed": "return_deadline_missed"}


def collection_prefab(prefab, state):
    rows = state.get("nearby") or []
    products = {row.get("product") for row in rows}
    inventory = (state.get("buddy") or {}).get("inventory") or {}
    if prefab in products or prefab in inventory:
        return prefab
    matching = {row["product"] for row in rows if row.get("prefab") == prefab and row.get("product")}
    return matching.pop() if len(matching) == 1 else RESOURCE_NAMES.get(prefab, prefab)


def capabilities(observed):
    return sorted(CAPABILITIES.intersection(observed.get("capabilities") or []))


SEASONAL_GEAR_RECIPES = {
    "winter": ("beefalohat", "winterhat", "earmuffshat"),
    "summer": ("eyebrellahat", "icehat", "umbrella", "strawhat", "grass_umbrella"),
    "rain": ("eyebrellahat", "raincoat", "rainhat", "umbrella", "grass_umbrella"),
}
THERMAL_FIRE_RECIPES = {
    "heat": ("firepit", "campfire"),
    "cool": ("coldfirepit", "coldfire"),
}

_seasonal_gear_equip = seasonal_gear_equip


def _ready_recipe(observed, names, *, placement=False):
    recipes = observed.get("recipes") or {}
    for rid in names:
        row = recipes.get(rid) or {}
        if row.get("supported") and row.get("ready") and bool(row.get("placement")) == placement:
            return rid
    return None


def _seasonal_gear_craft(observed, need):
    if "craft" not in capabilities(observed):
        return None
    recipes_for_need = SEASONAL_GEAR_RECIPES.get(need)
    if not recipes_for_need:
        return None
    gear = [r for r in observed.get("equipment", []) if r.get("usable")]
    if need in {"winter", "summer"}:
        has_gear = any(
            ((r.get("insulation") or 0) > 0 and r.get("insulation_type") == need)
            or r.get("prefab") in recipes_for_need
            for r in gear
        )
    else:
        has_gear = any((r.get("waterproof") or 0) > 0 or r.get("prefab") in recipes_for_need for r in gear)
    if has_gear:
        return None
    return _ready_recipe(observed, recipes_for_need, placement=False)


def _place_thermal_fire(observed, kind):
    if "place" not in capabilities(observed):
        return None
    point = (observed.get("buddy") or {}).get("position") or {}
    if not (number(point.get("x")) and number(point.get("z"))):
        return None
    rid = _ready_recipe(observed, THERMAL_FIRE_RECIPES.get(kind, ()), placement=True)
    if rid is None:
        return None
    return "place", {"recipe_id": rid, "x": point["x"], "z": point["z"], "radius": 6}


def validate_target(target, *, supported=CAPABILITIES):
    kind = target.get("type")
    if kind in TRANSFER_TARGETS:
        required = "storage" if kind in STORAGE_TARGETS else kind
        if required not in supported:
            raise ValueError("Unsupported companion capability: " + required)
        return validate_transfer(target)
    if kind == "refuel":
        if "refuel" not in supported:
            raise ValueError("Unsupported companion capability: refuel")
        return validate_refuel(target)
    if kind in {"cook_meal", "ignite_tree"}:
        if kind not in supported:
            raise ValueError("Unsupported companion capability: " + kind)
        return {"type": kind, **validate_camp_target(target)}
    required = {"inventory": "craft" if "recipe_id" in target else "collect", "structure": "place", "equip": "equip",
                "unequip": "unequip", "visit_building": "visit_building", "set_home": "home", "return_home": "home",
                "stay_home": "home", "hunger_ratio": "hunger", "health_ratio": "heal", "thermal_comfort": "thermal",
                "attack": "combat", "defend_owner": "combat",
                "move_to": "move", "return_owner": "follow", "follow": "follow", "stay": "stay"}
    if kind not in required or required[kind] not in supported:
        raise ValueError("Unsupported companion task target/capability: " + str(kind))
    fields = {"inventory": {"type", "prefab", "count", "recipe_id", "allow_search"}, "move_to": {"type", "x", "z", "arrival_radius"},
              "structure": {"type", "prefab", "recipe_id", "x", "z", "radius", "placement"},
              "equip": {"type", "prefab"}, "unequip": {"type", "prefab"}, "visit_building": {"type", "building_id"},
              "set_home": {"type", "anchor_id"}, "return_home": {"type", "home_id"}, "stay_home": {"type", "home_id", "radius"},
              "hunger_ratio": {"type", "at_least"},
              "health_ratio": {"type", "at_least"},
              "thermal_comfort": {"type", "min_temp", "max_temp", "charge_heatrock", "max_moisture"},
              "attack": {"type", "target_guid", "prefab", "count", "min_health_ratio"},
              "defend_owner": {"type", "min_health_ratio"}}
    allowed = fields.get(kind, {"type"})
    extra = set(target) - allowed
    if extra:
        names = [name if isinstance(name, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,31}", name)
                 else "<invalid field name>" for name in extra]
        detail = f"Unsupported companion target fields for {kind}: {', '.join(sorted(names)[:6])}; allowed: {', '.join(sorted(allowed))}"
        if kind == "return_owner" and "at" in extra:
            detail += ". Move target.at to tasks[].at alongside target"
        raise ValueError(detail)
    if kind == "visit_building":
        identity = target.get("building_id")
        if not isinstance(identity, str) or not re.fullmatch(r"building:[0-9]{1,16}", identity):
            raise ValueError("Visit requires a remembered building_id")
        return {"type": kind, "building_id": identity}
    if kind in {"set_home", "return_home", "stay_home"}:
        field = "anchor_id" if kind == "set_home" else "home_id"
        value = target.get(field)
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_:.-]{1,100}", value):
            raise ValueError("Place target requires an observed " + field)
        if kind == "set_home" and not re.fullmatch(r"building:[0-9]+", value):
            raise ValueError("Set home requires a building:<GUID> anchor")
        clean = {"type": kind, field: value}
        if kind == "stay_home":
            radius = target.get("radius", 12)
            if type(radius) not in {int, float} or not math.isfinite(radius) or not 6 <= radius <= 30:
                raise ValueError("stay_home radius must be 6-30 world units")
            clean["radius"] = radius
        return clean
    if kind == "hunger_ratio":
        ratio = target.get("at_least")
        if not number(ratio) or not 0 < ratio <= 1:
            raise ValueError("Invalid hunger target")
        return {"type": kind, "at_least": float(ratio)}
    if kind == "health_ratio":
        ratio = target.get("at_least")
        if not number(ratio) or not 0 < ratio <= 1:
            raise ValueError("Invalid health target")
        return {"type": kind, "at_least": float(ratio)}
    if kind == "thermal_comfort":
        min_t = target.get("min_temp", 15)
        max_t = target.get("max_temp", 55)
        if not number(min_t) or not number(max_t) or not 5 <= float(min_t) < float(max_t) <= 65:
            raise ValueError("thermal_comfort requires 5 <= min_temp < max_temp <= 65")
        clean = {"type": kind, "min_temp": float(min_t), "max_temp": float(max_t)}
        if "charge_heatrock" in target:
            mode = target["charge_heatrock"]
            if mode not in {"warm", "cool"}:
                raise ValueError("charge_heatrock must be 'warm' or 'cool'")
            clean["charge_heatrock"] = mode
        if "max_moisture" in target:
            max_m = target["max_moisture"]
            if not number(max_m) or not 5 <= float(max_m) <= 80:
                raise ValueError("max_moisture must be between 5 and 80")
            clean["max_moisture"] = float(max_m)
        return clean
    if kind in {"attack", "defend_owner"}:
        clean = {"type": kind}
        if "min_health_ratio" in target:
            ratio = target["min_health_ratio"]
            if not number(ratio) or not 0.15 <= float(ratio) <= 0.85:
                raise ValueError("min_health_ratio must be between 0.15 and 0.85")
            clean["min_health_ratio"] = float(ratio)
        if kind == "attack":
            has_guid = "target_guid" in target
            has_prefab = "prefab" in target
            if has_guid == has_prefab:
                raise ValueError("attack target requires either target_guid or prefab")
            if has_guid:
                tguid = target["target_guid"]
                if type(tguid) is not int or not 0 < tguid <= 2**53 - 1:
                    raise ValueError("attack target_guid must be a positive integer")
                clean["target_guid"] = tguid
                clean["count"] = 1
            else:
                prefab = target["prefab"]
                if not isinstance(prefab, str) or not re.fullmatch(r"[a-z0-9_]{1,60}", prefab):
                    raise ValueError("attack prefab must be a canonical entity prefab")
                count = target.get("count", 1)
                if type(count) is not int or not 1 <= count <= 20:
                    raise ValueError("attack count must be 1..20")
                clean.update(prefab=prefab, count=count)
        return clean
    if kind == "inventory":
        prefab, count = target.get("prefab"), target.get("count")
        if (not isinstance(prefab, str) or not re.fullmatch(r"[a-z0-9_]{1,60}", prefab)
                or type(count) is not int or not 1 <= count <= 100):
            raise ValueError("Inventory target requires prefab and total count 1..100")
        clean = {"type": kind, "prefab": prefab, "count": count}
        if "allow_search" in target:
            if type(target["allow_search"]) is not bool:
                raise ValueError("allow_search must be a boolean")
            clean["allow_search"] = target["allow_search"]
        if "recipe_id" in target:
            if not isinstance(target["recipe_id"], str) or not re.fullmatch(r"[a-z0-9_]{1,100}", target["recipe_id"]):
                raise ValueError("Invalid companion recipe_id")
            clean["recipe_id"] = target["recipe_id"]
        return clean
    if kind in {"equip", "unequip"}:
        prefab = target.get("prefab")
        if not isinstance(prefab, str) or not re.fullmatch(r"[a-z0-9_]{1,60}", prefab):
            raise ValueError("Equipment target requires an item prefab")
        return {"type": kind, "prefab": prefab}
    if kind == "structure":
        clean = validate_target({"type": "inventory", "prefab": target.get("prefab"), "count": 1,
                                 "recipe_id": target.get("recipe_id")}, supported=CAPABILITIES)
        if "placement" in target:
            if any(k in target for k in ("x", "z", "radius")):
                raise ValueError("Structure target accepts either placement or x/z/radius, not both")
            return {"type": kind, "prefab": clean["prefab"], "recipe_id": clean["recipe_id"],
                    "placement": validate_placement(target["placement"])}
        point = validate_point(target)
        radius = target.get("radius", 6)
        if not number(radius) or not 2 <= radius <= 8:
            raise ValueError("Structure placement radius must be 2..8")
        return {"type": kind, "prefab": clean["prefab"], "recipe_id": clean["recipe_id"],
                "x": point["x"], "z": point["z"], "radius": radius}
    if kind == "move_to":
        point = validate_point({"arrival_radius": 2.5, **target})
        return {"type": kind, **point}
    return {"type": kind}


def validate_board(raw, day, *, observed=None):
    supported = capabilities(observed) if observed is not None else CAPABILITIES
    return validate_goals(raw, day, target_validator=lambda target: validate_target(target, supported=supported))


def actor_state(observed):
    """Only normalize observed facts; never invent a map, hunger maximum or recipes."""
    buddy = observed.get("buddy") or {}
    vitals = {"dead": buddy.get("dead")}
    for key in ("health", "health_max", "hunger", "hunger_max", "sanity", "sanity_max",
                "moisture", "moisture_max", "freezing", "overheating", "temperature"):
        if key in buddy:
            vitals[key] = buddy.get(key)
    inventory = buddy.get("inventory") or {}
    result = {"player": {"guid": buddy.get("guid"), "position": copy.deepcopy(buddy.get("position")),
                         "vitals": vitals, "inventory": {"items": [
                             {"prefab": name, "count": count} for name, count in inventory.items()]}},
              "recipe_inventory": dict(inventory),
              "world": copy.deepcopy(observed.get("world") or {}), "_companion": observed,
              "observed_at": observed.get("version")}
    for key in ("places", "refueling", "thermal", "camp_work"):
        if key in observed:
            result[key] = copy.deepcopy(observed[key])
    return result


def evaluate_target(target, state):
    observed = state["_companion"]
    buddy = observed.get("buddy") or {}
    kind = target["type"]
    if kind in COUNTED_TARGETS:
        return 0, target["count"], True, ""  # Only per-task confirmed receipts count.
    if kind in {"cook_meal", "ignite_tree"}:
        err = camp_error(target, state)
        return 0, 1, not bool(err), err
    if kind == "attack":
        return 0, target.get("count", 1), True, ""
    if kind == "defend_owner":
        return 0, 1, True, ""
    if kind in {"inventory", "receive_item"}:
        return (buddy.get("inventory") or {}).get(target["prefab"], 0), target["count"], True, ""
    if kind in PLACE_TARGETS:
        done, reason = building_progress(target, observed.get("places"))
        return (0 if kind == "stay_home" else int(done)), 1, not bool(reason), reason
    if kind == "hunger_ratio":
        hunger, maximum = buddy.get("hunger"), buddy.get("hunger_max")
        if number(hunger) and number(maximum) and maximum > 0:
            ratio = hunger / maximum
            return round(ratio, 3), target["at_least"], True, ""
        return 0, target["at_least"], False, "hunger telemetry missing"
    if kind == "health_ratio":
        health, maximum = buddy.get("health"), buddy.get("health_max")
        if number(health) and number(maximum) and maximum > 0:
            return round(health / maximum, 3), target["at_least"], True, ""
        return 0, target["at_least"], False, "health telemetry missing"
    if kind == "thermal_comfort":
        st = thermal_comfort_status(target, observed)
        return int(st["satisfied"]), 1, st["known"], "" if st["known"] else "temperature telemetry missing"
    if kind == "refuel":
        if "at_least" in target:
            status = fire_maintenance_state(target, state)
            return int(status["satisfied"]), 1, status["available"], status["reason"]
        error = refuel_error(target, observed.get("places"))
        return 0, target["count"], not bool(error), error
    if kind == "inspect_storage":
        cguid = container_guid_for(target, observed.get("places"))
        known = "containers" in observed
        done = cguid is not None and any(r["guid"] == cguid and r.get("contents_known") for r in observed.get("containers", []))
    elif kind == "equip":
        known = "equipment" in observed
        done = any(row["prefab"] == target["prefab"] and row.get("equipped") and row.get("usable") for row in observed.get("equipment", []))
    elif kind == "unequip":
        known = "equipment" in observed
        done = known and not any(row["prefab"] == target["prefab"] and row.get("equipped") for row in observed.get("equipment", []))
    elif kind == "structure":
        known = "structures" in observed
        if "placement" in target:
            try:
                area = placement_area(target["placement"], state)
            except ValueError as exc:
                return 0, 1, False, str(exc)
            done = any(row["prefab"] == target["prefab"] and inside_placement(row, area)
                       for row in observed.get("structures", []))
        else:
            done = any(row["prefab"] == target["prefab"] and math.hypot(row["x"]-target["x"], row["z"]-target["z"]) <= target["radius"]
                       for row in observed.get("structures", []))
    elif kind == "move_to":
        point = buddy.get("position") or {}
        known = all(number(point.get(k)) for k in ("x", "z"))
        done = known and math.hypot(point["x"]-target["x"], point["z"]-target["z"]) <= target["arrival_radius"]
    elif kind == "return_owner":
        known = number(buddy.get("owner_distance"))
        done = known and buddy["owner_distance"] <= RETURN_RADIUS
    else:
        known = observed.get("mode") is not None
        done = observed.get("mode") == kind
    return int(bool(done)), 1, known, "" if known else "companion telemetry missing"


class CompanionTasks:
    """Only native dispatch differs. Timing, dependencies, focus and progress are shared."""
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.goals = PriorityBoardExecutor(clock=clock, target_evaluator=evaluate_target, progress_evaluator=self.progress)
        self.search = ResourceSearch(clock)
        self.plan = None
        self.observed = None

    @staticmethod
    def return_progress(task, record, state):
        if record.get("return_receipt"):
            task["return_timing"] = record["return_receipt"]
            return
        remaining = seconds_to(task["at"], state["world"])
        observed = state["_companion"]
        buddy = observed.get("buddy") or {}
        distance, speed = buddy.get("owner_distance"), buddy.get("run_speed")
        near = number(distance) and distance <= RETURN_RADIUS
        if near and (record.get("return_departed") or (remaining is not None and remaining <= 0)):
            timing = {"remaining_seconds": round(remaining, 1) if remaining is not None else None,
                      "estimated_travel_seconds": 0, "owner_distance": round(distance, 1),
                      "state": "late" if remaining is not None and remaining < 0 else "arrived"}
            record["return_receipt"] = dict(timing)
            task.update(status="done", blocked_reason="", progress={"current":1,"required":1}, return_timing=timing)
            return
        if remaining is None or not number(distance) or not number(speed) or speed <= 0:
            task.update(status="scheduled", blocked_reason="return clock or speed unavailable", progress={"current":0,"required":1})
            return
        eta = max(0, distance-RETURN_RADIUS) / speed * 1.25
        timing = {"remaining_seconds": round(remaining, 1), "estimated_travel_seconds": round(eta, 1),
                  "owner_distance": round(distance, 1)}
        task["return_timing"] = timing
        if remaining < 0:
            timing["state"] = "missed"
        elif eta > remaining:
            timing["state"] = "at_risk"
        else:
            timing["state"] = "on_track"
        if near:
            task.update(status="scheduled", blocked_reason="waiting for return window")
        elif record.get("return_departed") or remaining <= eta + RETURN_DEPARTURE_BUFFER:
            task.update(status="in_progress" if record.get("return_departed") else "ready", blocked_reason="")
        else:
            task.update(status="scheduled", blocked_reason="waiting to depart")
        task["progress"] = {"current":0,"required":1}

    @staticmethod
    def progress(task, record, state):
        if task.get("timing_error"):
            return
        kind = task["target"]["type"]
        if kind == "return_owner" and task.get("at"):
            CompanionTasks.return_progress(task, record, state)
            return
        if kind == "visit_building" and record.get("arrival_receipt"):
            task.update(progress={"current": 1, "required": 1}, status="done", blocked_reason="")
            return
        if kind in {"cook_meal", "ignite_tree"}:
            camp_execution.progress(task, record, state)
            return
        if kind == "attack":
            kills = record.get("combat_kills", 0)
            req = task["target"].get("count", 1)
            task.update(progress={"current": min(kills, req), "required": req},
                        status="done" if kills >= req else "in_progress" if kills else task["status"])
            if kills >= req:
                task["blocked_reason"] = ""
            return
        if kind == "defend_owner":
            if task.get("duration") is None and task.get("end_at") is None:
                if record.get("defend_cleared"):
                    task.update(progress={"current": 1, "required": 1}, status="done", blocked_reason="")
                else:
                    task.update(progress={"current": 0, "required": 1})
            return
        if kind in {"hunger_ratio", "health_ratio", "thermal_comfort"}:
            record.setdefault("transfer_key", uuid.uuid4().hex)
            base_key = record["transfer_key"]
            for receipt in state["_companion"].get("transfer_receipts", []):
                tkey = receipt.get("task_key")
                if not isinstance(tkey, str) or type(receipt.get("count")) is not int:
                    continue
                if kind == "hunger_ratio" and tkey == base_key:
                    record["hunger_eaten"] = max(record.get("hunger_eaten", 0), receipt["count"])
                elif kind == "health_ratio" and tkey.startswith(f"{base_key}_h_"):
                    prefab = tkey[len(f"{base_key}_h_"):]
                    counts = record.setdefault("heal_counts", {})
                    counts[prefab] = max(counts.get(prefab, 0), receipt["count"])
                elif kind == "health_ratio" and tkey.startswith(f"{base_key}_e_"):
                    prefab = tkey[len(f"{base_key}_e_"):]
                    counts = record.setdefault("eat_counts", {})
                    counts[prefab] = max(counts.get(prefab, 0), receipt["count"])
                elif kind == "thermal_comfort" and tkey.startswith(f"{base_key}_f_"):
                    prefab = tkey[len(f"{base_key}_f_"):]
                    counts = record.setdefault("thermal_fuel_counts", {})
                    counts[prefab] = max(counts.get(prefab, 0), receipt["count"])
            return
        if kind == "refuel":
            record.setdefault("transfer_key", uuid.uuid4().hex)
            key = record["transfer_key"]
            for receipt in state["_companion"].get("transfer_receipts", []):
                if receipt.get("task_key") == key and type(receipt.get("count")) is int:
                    record["fuel_added"] = max(record.get("fuel_added", 0), receipt["count"])
            added = record.get("fuel_added", 0)
            task["fuel_added"] = added
            if "count" in task["target"]:
                required = task["target"]["count"]
                task["progress"] = {"current": added, "required": required}
                if added >= required:
                    task.update(status="done", blocked_reason="")
                elif task["status"] != "blocked" and added:
                    task["status"] = "in_progress"
            return
        if kind not in COUNTED_TARGETS:
            return
        if "transfer_key" not in record:
            record["transfer_key"] = uuid.uuid4().hex
        key = record["transfer_key"]
        for receipt in state["_companion"].get("transfer_receipts", []):
            if receipt.get("task_key") == key and type(receipt.get("count")) is int:
                record["camp_count"] = max(record.get("camp_count", 0), receipt["count"])
        current, required = record.get("camp_count", 0), task["target"]["count"]
        task.update(progress={"current": current, "required": required},
                    status="done" if current >= required else "in_progress" if current else "ready", blocked_reason="")

    @staticmethod
    def due_returns(view):
        return [task["id"] for task in (view or {}).get("tasks", [])
                if task.get("at") and task["status"] in {"ready", "in_progress"}]

    def next_return_alert(self, view):
        for task in (view or {}).get("tasks", []):
            timing = task.get("return_timing") or {}
            reason = RETURN_ALERTS.get(timing.get("state"))
            if not reason or task["status"] not in {"ready", "in_progress"}:
                continue
            reported = self.goals.records[task["id"]].setdefault("return_alerts", set())
            if reason not in reported:
                reported.add(reason)
                return task["id"], reason, timing
        return None

    def view(self, observed=None):
        if observed is not None:
            self.observed = observed
            self.search.observe(observed)
        if not self.plan or not self.observed:
            return None
        result = self.goals.view(self.plan, actor_state(self.observed))
        unavailable = {t["id"] for t in result["tasks"] if t["status"] in {"failed", "expired"}
                       or (t["status"] == "blocked" and self.goals.records[t["id"]]["blocked"])}
        if unavailable:
            # A focused branch with an unavailable prerequisite must not hold unrelated work.
            while True:
                dependents = {t["id"] for t in result["tasks"] if t["status"] == "blocked"
                              and unavailable.intersection(t["depends_on"])}
                if dependents <= unavailable:
                    break
                unavailable.update(dependents)
            scoped = {**result, "tasks": [
                {**t, "focus": False} if t["id"] in unavailable else t for t in result["tasks"]
            ]}
            result["focus_task_ids"] = [key for key in result["focus_task_ids"] if key not in unavailable]
            result["execution_task_ids"] = [t["id"] for t in self.goals.eligible_tasks(scoped)]
        # Once a scheduled return opens, available gathering must yield to it.
        if due := self.due_returns(result):
            result["execution_task_ids"] = due
        result["search_memory"] = self.search.summary()
        return result

    def defer_task(self, task_id, reason):
        self.goals.records[task_id].update(blocked=reason, companion_deferred=reason)

    def clear_task_deferrals(self, task_id=None):
        records = self.goals.records.values() if task_id is None else (self.goals.records[task_id],)
        for record in records:
            if record.pop("companion_deferred", None) == record["blocked"]:
                record["blocked"] = ""

    def pause_search(self):
        self.goals.pause_accounting()
        self.search.inflight = None
        for record in self.goals.records.values():
            self.clear_storage_wait(record)

    @staticmethod
    def clear_storage_wait(record):
        for key in ("storage_wait_started", "storage_retry_at", "storage_busy_version"):
            record.pop(key, None)

    def storage_wait(self, task, chest=None):
        record = self.goals.records[task["id"]]
        started = record.setdefault("storage_wait_started", self.clock())
        waited = max(0, self.clock()-started)
        return task, "needs_replan" if waited >= STORAGE_WAIT_SECONDS else "waiting_storage", {
            "reason": "chest_busy_timeout" if waited >= STORAGE_WAIT_SECONDS else "chest_busy",
            "container_guid": task["target"]["container_guid"], "waited_seconds": round(waited, 1),
            "busy_openers": (chest or {}).get("busy_openers", [])}

    def note_action(self, task, op, args):
        self.goals.pause_accounting()
        if op == "search":
            self.search.start_leg(task["id"], args)
            self.goals.selected, self.goals.navigation, self.goals.navigation_mode = task["id"], True, "search"

    def on_result(self, task_id, action, observed):
        if action.get("reason") == "enemy_targeting":
            if self.search.inflight:
                self.view(observed)
                self.search.inflight = None
                self.goals.pause_accounting()
            return True
        record = self.goals.records.get(task_id)
        data = action.get("data") or {}
        task = next((t for t in (self.plan or {}).get("tasks", []) if t["id"] == task_id), None)
        if record and isinstance(data.get("task_key"), str) and record.get("transfer_key") and type(data.get("count")) is int:
            base_key = record["transfer_key"]
            tkey = data["task_key"]
            if tkey == base_key:
                if task and task["target"]["type"] == "refuel":
                    record["fuel_added"] = max(record.get("fuel_added", 0), data["count"])
                elif task and task["target"]["type"] == "hunger_ratio":
                    record["hunger_eaten"] = max(record.get("hunger_eaten", 0), data["count"])
                else:
                    record["camp_count"] = max(record.get("camp_count", 0), data["count"])
            elif task and task["target"]["type"] == "health_ratio":
                if tkey.startswith(f"{base_key}_h_"):
                    prefab = tkey[len(f"{base_key}_h_"):]
                    counts = record.setdefault("heal_counts", {})
                    counts[prefab] = max(counts.get(prefab, 0), data["count"])
                elif tkey.startswith(f"{base_key}_e_"):
                    prefab = tkey[len(f"{base_key}_e_"):]
                    counts = record.setdefault("eat_counts", {})
                    counts[prefab] = max(counts.get(prefab, 0), data["count"])
            elif task and task["target"]["type"] == "thermal_comfort" and tkey.startswith(f"{base_key}_f_"):
                prefab = tkey[len(f"{base_key}_f_"):]
                counts = record.setdefault("thermal_fuel_counts", {})
                counts[prefab] = max(counts.get(prefab, 0), data["count"])
        if record and task and task.get("at") and action["status"] == "completed" and action.get("reason") == "follow":
            record["return_departed"] = True
        if (record and task and task["target"]["type"] == "visit_building" and action["status"] == "completed"
                and action.get("reason") == "building_arrived" and data.get("arrived") is True
                and data.get("building_id") == task["target"]["building_id"]):
            record.update(done=True, arrival_receipt=dict(data))
            return True
        if record and task and task["target"]["type"] in {"cook_meal", "ignite_tree"} and action["status"] == "completed":
            op = action.get("op") or ""
            synthetic = {"kind": op, **data}
            camp_execution.on_completed(task["target"], record, synthetic, action.get("reason", ""), actor_state(observed))
            return True
        if record and task and task["target"]["type"] in {"attack", "defend_owner"}:
            if action["status"] == "completed" and action.get("reason") == "target_killed":
                record["combat_kills"] = record.get("combat_kills", 0) + 1
                return True
            if action.get("reason") == "low_health_retreat":
                record["blocked"] = "low_health_retreat"
                return True
        if record and task and task["target"]["type"] in STORAGE_TARGETS:
            if action["status"] == "failed" and action.get("reason") == "chest_busy":
                record.setdefault("storage_wait_started", self.clock())
                record.update(storage_retry_at=self.clock()+.5, storage_busy_version=observed.get("version"))
                return True  # An occupancy race is a transient wait, not an action failure.
            if action["status"] == "completed" and (
                    action.get("reason") in {"one_item_transferred", "transfer_total_already_met"}
                    or str(action.get("reason", "")).startswith("items_transferred:")):
                self.clear_storage_wait(record)
        if not self.search.inflight or self.search.inflight["task_id"] != task_id:
            return False
        self.view(observed)  # Account real travel before releasing the search leg.
        self.search.result(action["status"], action.get("reason", ""))
        self.goals.pause_accounting()
        return True

    def adopt(self, plan, observed):
        previous = self.view(observed)
        clean = validate_board(plan, plan["day"], observed=observed)
        result = merge_goals(previous, clean, target_validator=lambda t: validate_target(t, supported=capabilities(observed)))
        if "reply" in plan:
            result["reply"] = plan["reply"]
        self.plan = result
        # A completed milestone can be repeated in a verification patch as history.
        # Same ID/target keeps its receipt; new IDs or changed targets create fresh work.
        self.goals.adopt(result)
        return result

    @staticmethod
    def recipe_action(target, observed):
        row = (observed.get("recipes") or {}).get(target["recipe_id"], {})
        placement = target["type"] == "structure"
        if (not row.get("supported") or row.get("product") != target["prefab"] or bool(row.get("placement")) != placement):
            return "needs_replan", {"reason": "query_supported_recipe:" + target["recipe_id"]}
        if not row.get("ready"):
            return "needs_replan", {"reason": row.get("reason", "recipe_not_ready"), "recipe_id": target["recipe_id"]}
        if not placement:
            return "craft", {"recipe_id": target["recipe_id"]}
        if "placement" in target:
            try:
                area = placement_area(target["placement"], actor_state(observed))
            except ValueError as exc:
                return "needs_replan", {"reason": str(exc)}
            return "place", {"recipe_id": target["recipe_id"], "x": area["x"], "z": area["z"],
                             "radius": min(8, max(2, area["radius"]))}
        return "place", {k: target[k] for k in ("recipe_id", "x", "z", "radius")}

    @staticmethod
    def unequip_action(item, observed):
        if "unequip" not in capabilities(observed):
            return "needs_replan", {"reason": "unequip_capability_unavailable:update_server_mod"}
        if item.get("can_unequip") is False:
            return "needs_replan", {"reason": item.get("unequip_reason") or "cannot_stow_equipment"}
        return "unequip", {"item_guid": item["guid"]}

    @staticmethod
    def _combat_gear_equip(observed):
        if "equip" not in capabilities(observed):
            return None
        gear = [r for r in observed.get("equipment", []) if r.get("usable")]
        # 1. Equip best body & head armor first
        for slot in ("body", "head"):
            slot_armors = [r for r in gear if r.get("slot") == slot and (r.get("armor") or 0) > 0]
            if slot_armors:
                best = max(slot_armors, key=lambda r: (r.get("armor") or 0, 1 if r.get("equipped") else 0))
                if not best.get("equipped"):
                    return best["guid"]
        # 2. Equip highest-damage melee weapon (damage > 10)
        weapons = [r for r in gear if r.get("slot") == "hands" and (r.get("damage") or 0) > 10]
        if weapons:
            best_w = max(weapons, key=lambda r: (r.get("damage") or 0, 1 if r.get("equipped") else 0))
            if not best_w.get("equipped"):
                return best_w["guid"]
        return None

    @staticmethod
    def _owner_threats(observed):
        owner_pos = (observed.get("owner") or {}).get("position") or {}
        ox, oz = owner_pos.get("x"), owner_pos.get("z")
        threats = []
        for enemy in observed.get("enemies", []):
            if enemy.get("targeting_owner") or enemy.get("targeting_buddy"):
                threats.append(enemy)
            elif enemy.get("hostile") and number(ox) and number(oz) and number(enemy.get("x")) and number(enemy.get("z")):
                if math.hypot(enemy["x"] - ox, enemy["z"] - oz) <= 12:
                    threats.append(enemy)
        return threats

    def _thermal_fire_step(self, task, observed, navigation, *, fire_prefabs, place_kind, fuels, missing_reason, fuel_reason, fuel_threshold):
        buildings = [r for r in (observed.get("places") or {}).get("buildings", []) if r.get("status") not in {"missing", "unavailable"}]
        refuel_rows = {r.get("building_id"): r for r in (observed.get("refueling") or {}).get("targets", []) if r.get("building_id")}
        inv = (observed.get("buddy") or {}).get("inventory") or {}
        fires = [r for r in buildings if r.get("prefab") in fire_prefabs]
        if not fires:
            placed = _place_thermal_fire(observed, place_kind)
            if placed is not None:
                return task, *placed
            return task, "needs_replan", {"reason": missing_reason}
        fire = max(fires, key=lambda r: (
            1 if (refuel_rows.get(r["id"]) or {}).get("lit") else 0,
            1 if r.get("prefab") == fire_prefabs[0] else 0,
            1 if r.get("arrived") else 0,
            -(r.get("distance") or 999),
        ))
        if not fire.get("arrived"):
            navigation.append((task, "visit_building", {"building_id": fire["id"]}))
            return None
        row = refuel_rows.get(fire["id"]) or {}
        fuel_pct = row.get("fuel_percent")
        needs_fuel = (not row.get("lit")) or (number(fuel_pct) and fuel_pct < fuel_threshold)
        if needs_fuel:
            fuel_prefab = next((f for f in fuels if inv.get(f, 0) > 0), None)
            if fuel_prefab:
                record = self.goals.records[task["id"]]
                base_key = record.setdefault("transfer_key", uuid.uuid4().hex)
                counts = record.setdefault("thermal_fuel_counts", {})
                return task, "refuel", {"building_id": fire["id"], "fuel_prefab": fuel_prefab,
                                        "total_count": counts.get(fuel_prefab, 0) + 1,
                                        "task_key": f"{base_key}_f_{fuel_prefab}"[:64]}
            if not row.get("lit"):
                return task, "needs_replan", {"reason": fuel_reason}
        navigation.append((task, "waiting", {}))
        return None

    def next_action(self, observed):
        view = self.view(observed)
        resumed = False
        offered = {row["product"] for row in observed.get("nearby", [])}
        tools = {work for item in observed.get("equipment", []) if item.get("usable") for work in item.get("tools", [])}
        workable = {product for row in observed.get("work_targets", []) if row["work"] in tools
                    for product in row.get("products", [])}
        for task in view["tasks"]:
            target = task["target"]
            if target["type"] != "inventory":
                continue
            product = target["prefab"]
            if product in offered:
                resumed = self.goals.resume_local_work(task["id"]) or resumed
            record = self.goals.records[task["id"]]
            reason = record.get("companion_deferred", "")
            if reason == record["blocked"] and (
                    reason == "no_safe_unsearched_frontier" or reason.startswith("no_nearby_source_for_")):
                if (product in offered or product in workable or (
                        reason == "no_safe_unsearched_frontier"
                        and self.search.select(product, observed, max_distance=task["search_budget_distance"]-record["distance"]))):
                    self.clear_task_deferrals(task["id"])
                    resumed = True
        if resumed:
            view = self.view(observed)
        if view["status"] == "completed":
            return None, "completed", {}
        eligible = [t for t in view["tasks"] if t["id"] in view["execution_task_ids"]]
        navigation = []
        for task in eligible:
            target = task["target"]
            kind = target["type"]
            required = {"inventory": "craft" if target.get("recipe_id") else "collect", "structure": "place",
                        "move_to": "move", "return_owner": "follow", "set_home": "home", "return_home": "home",
                        "stay_home": "home", "hunger_ratio": "hunger", "health_ratio": "heal", "thermal_comfort": "thermal",
                        "refuel": "refuel", "attack": "combat", "defend_owner": "combat",
                        **{t: "storage" for t in STORAGE_TARGETS}}.get(kind, kind)
            if required not in capabilities(observed):
                return task, "unsupported", {"reason": "capability_unavailable:" + required}
            if kind == "visit_building":
                building = next((r for r in (observed.get("places") or {}).get("buildings", []) if r["id"] == target["building_id"]), None)
                if not building or building.get("status") in {"missing", "unavailable"}:
                    return task, "needs_replan", {"reason": "building_missing_or_unavailable"}
                navigation.append((task, "visit_building", {"building_id": target["building_id"]}))
                continue
            if kind == "set_home":
                building = next((r for r in (observed.get("places") or {}).get("buildings", []) if r["id"] == target["anchor_id"]), None)
                if not building or building.get("status") in {"missing", "unavailable"}:
                    return task, "needs_replan", {"reason": "home_anchor_missing_or_unavailable"}
                if not building.get("arrived"):
                    navigation.append((task, "visit_building", {"building_id": target["anchor_id"]}))
                    continue
                return task, "set_home", {"anchor_id": target["anchor_id"]}
            if kind in {"return_home", "stay_home"}:
                home = (observed.get("places") or {}).get("home") or {}
                if home.get("id") != target["home_id"]:
                    return task, "needs_replan", {"reason": "home_unset_or_changed"}
                if kind == "stay_home" and (view.get("residence_constraint") or {}).get("holding"):
                    navigation.append((task, "waiting", {}))
                    continue
                anchor_id = home.get("anchor_id")
                if anchor_id and any(r.get("id") == anchor_id and r.get("status") not in {"missing", "unavailable"}
                                     for r in (observed.get("places") or {}).get("buildings", [])):
                    navigation.append((task, "visit_building", {"building_id": anchor_id}))
                elif number(home.get("x")) and number(home.get("z")):
                    navigation.append((task, "move", {"x": home["x"], "z": home["z"]}))
                else:
                    return task, "needs_replan", {"reason": "home_coordinates_unavailable"}
                continue
            if kind == "hunger_ratio":
                foods = [row for row in observed.get("foods", []) if number(row.get("hunger_delta")) and row["hunger_delta"] > 0]
                safe = [row for row in foods if (row.get("health_delta", 0) or 0) >= 0 and (row.get("sanity_delta", 0) or 0) >= 0]
                reserved = {
                    t["target"].get("product") or t["target"].get("prefab")
                    for t in view["tasks"]
                    if t["id"] != task["id"] and t["target"].get("type") in {"cook_meal", "give_owner", "deposit", "drop_item"}
                }
                unreserved = [row for row in (safe or foods) if row.get("prefab") not in reserved]
                chosen = max(unreserved or safe or foods, key=lambda r: r["hunger_delta"], default=None)
                if not chosen:
                    return task, "needs_replan", {"reason": "food_not_carried_or_inedible"}
                record = self.goals.records[task["id"]]
                record.setdefault("transfer_key", uuid.uuid4().hex)
                return task, "eat", {"prefab": chosen["prefab"], "total_count": record.get("hunger_eaten", 0) + 1,
                                     "task_key": record["transfer_key"]}
            if kind == "health_ratio":
                reserved = {
                    t["target"].get("product") or t["target"].get("prefab")
                    for t in view["tasks"]
                    if t["id"] != task["id"] and t["target"].get("type") in {"heal", "cook_meal", "give_owner", "deposit", "drop_item"}
                }
                healers = [row for row in observed.get("healers", []) if number(row.get("health_delta")) and row["health_delta"] > 0]
                unreserved_healers = [row for row in healers if row.get("prefab") not in reserved]
                best_healer = max(unreserved_healers or healers, key=lambda r: r["health_delta"], default=None)
                if best_healer:
                    record = self.goals.records[task["id"]]
                    base_key = record.setdefault("transfer_key", uuid.uuid4().hex)
                    prefab = best_healer["prefab"]
                    counts = record.setdefault("heal_counts", {})
                    return task, "heal", {"prefab": prefab, "total_count": counts.get(prefab, 0) + 1,
                                          "task_key": f"{base_key}_h_{prefab}"[:64]}
                heal_foods = [row for row in observed.get("foods", []) if number(row.get("health_delta")) and row["health_delta"] > 0]
                safe_heals = [row for row in heal_foods if (row.get("sanity_delta", 0) or 0) >= 0]
                unreserved_foods = [row for row in (safe_heals or heal_foods) if row.get("prefab") not in reserved]
                best_food = max(unreserved_foods or safe_heals or heal_foods, key=lambda r: r["health_delta"], default=None)
                if best_food:
                    record = self.goals.records[task["id"]]
                    base_key = record.setdefault("transfer_key", uuid.uuid4().hex)
                    prefab = best_food["prefab"]
                    counts = record.setdefault("eat_counts", {})
                    return task, "eat", {"prefab": prefab, "total_count": counts.get(prefab, 0) + 1,
                                         "task_key": f"{base_key}_e_{prefab}"[:64]}
                return task, "needs_replan", {"reason": "healing_item_missing"}
            if kind == "thermal_comfort":
                buddy = observed.get("buddy") or {}
                temp = buddy.get("temperature")
                if not number(temp):
                    return task, "needs_replan", {"reason": "temperature_telemetry_missing"}
                st = thermal_comfort_status(target, observed)
                stones = (observed.get("thermal") or {}).get("stones") or []
                charge = target.get("charge_heatrock")
                season = (observed.get("world") or {}).get("season")
                caps = capabilities(observed)
                need_heat = temp < target["min_temp"] or (charge == "warm" and not st["stone_ok"])
                need_cool = temp > target["max_temp"] or (charge == "cool" and not st["stone_ok"])
                if need_heat or need_cool:
                    need = "winter" if need_heat else "summer"
                    gear = _seasonal_gear_equip(observed, need)
                    if gear is not None:
                        return task, "equip", {"item_guid": gear}
                    craft_gear = _seasonal_gear_craft(observed, need)
                    if craft_gear is not None:
                        return task, "craft", {"recipe_id": craft_gear}
                    want_charge = (need_heat and charge == "warm") or (need_cool and charge == "cool")
                    if not stones and (want_charge or season == need):
                        if "craft" in caps and _ready_recipe(observed, ("heatrock",)):
                            return task, "craft", {"recipe_id": "heatrock"}
                        if want_charge:
                            return task, "needs_replan", {"reason": "heatrock_required"}
                    action = self._thermal_fire_step(
                        task, observed, navigation,
                        fire_prefabs=("firepit", "campfire") if need_heat else ("coldfirepit", "coldfire"),
                        place_kind="heat" if need_heat else "cool",
                        fuels=(("log", "charcoal", "twigs", "cutgrass", "pinecone", "acorn", "twiggy_nut") if need_heat
                               else ("nitre", "log", "charcoal", "twigs", "cutgrass", "pinecone", "acorn", "twiggy_nut")),
                        missing_reason="heat_source_required" if need_heat else "cooling_source_required",
                        fuel_reason="fuel_required_for_heat" if need_heat else "fuel_required_for_cooling",
                        fuel_threshold=0.45 if want_charge else 0.25,
                    )
                    if action is not None:
                        return action
                    continue
                if (observed.get("world") or {}).get("israining") or st["wet_issue"]:
                    gear = _seasonal_gear_equip(observed, "rain")
                    if gear is not None:
                        return task, "equip", {"item_guid": gear}
                    craft_gear = _seasonal_gear_craft(observed, "rain")
                    if craft_gear is not None:
                        return task, "craft", {"recipe_id": craft_gear}
                    if st["wet_issue"] and temp < target["max_temp"] - 5:
                        action = self._thermal_fire_step(
                            task, observed, navigation,
                            fire_prefabs=("firepit", "campfire"),
                            place_kind="heat",
                            fuels=("log", "charcoal", "twigs", "cutgrass", "pinecone", "acorn", "twiggy_nut"),
                            missing_reason="heat_source_required",
                            fuel_reason="fuel_required_for_heat",
                            fuel_threshold=0.25,
                        )
                        if action is not None:
                            return action
                        continue
                navigation.append((task, "waiting", {}))
                continue
            if kind == "refuel":
                state = actor_state(observed)
                err = refuel_error(target, observed.get("places"))
                if err:
                    return task, "needs_replan", {"reason": err}
                pause = refuel_pause(target, state)
                if pause == "fuel_settling":
                    navigation.append((task, "waiting", {}))
                    continue
                if pause:
                    return task, "needs_replan", {"reason": pause}
                if not has_fuel(target, state):
                    return task, "needs_replan", {"reason": "fuel_missing:" + target["fuel_prefab"]}
                building = next((r for r in (observed.get("places") or {}).get("buildings", []) if r["id"] == target["building_id"]), None)
                if not building or building.get("status") in {"missing", "unavailable"}:
                    return task, "needs_replan", {"reason": "refuel_building_missing_or_unavailable"}
                if not building.get("arrived"):
                    navigation.append((task, "visit_building", {"building_id": target["building_id"]}))
                    continue
                record = self.goals.records[task["id"]]
                record.setdefault("transfer_key", uuid.uuid4().hex)
                total = target["count"] if "count" in target else record.get("fuel_added", 0) + 1
                return task, "refuel", {"building_id": target["building_id"], "fuel_prefab": target["fuel_prefab"],
                                        "total_count": total, "task_key": record["transfer_key"]}
            if kind == "cook_meal":
                state = actor_state(observed)
                err = camp_error(target, state)
                if err:
                    return task, "needs_replan", {"reason": err}
                building = next((r for r in (observed.get("places") or {}).get("buildings", []) if r["id"] == target["building_id"]), None)
                if not building or building.get("status") in {"missing", "unavailable"}:
                    return task, "needs_replan", {"reason": "cookpot_missing_or_unavailable"}
                if not building.get("arrived"):
                    navigation.append((task, "visit_building", {"building_id": target["building_id"]}))
                    continue
                row = camp_fixture(state, target["building_id"]) or {}
                if task.get("meal_started"):
                    if row.get("phase") == "ready":
                        return task, "harvest_meal", {"building_id": target["building_id"], "product": target["product"]}
                    navigation.append((task, "waiting", {}))
                    continue
                if not row.get("contents_known"):
                    return task, "needs_replan", {"reason": "cookpot_unavailable"}
                missing = Counter(target["ingredients"]) - camp_loaded(row)
                if missing:
                    inv = observed["buddy"].get("inventory") or {}
                    prefab = next((name for name, need in sorted(missing.items()) if need > 0 and inv.get(name, 0) > 0), None)
                    if not prefab:
                        first = next(name for name, need in sorted(missing.items()) if need > 0)
                        return task, "needs_replan", {"reason": "meal_ingredient_missing:" + first}
                    return task, "cook_load", {"building_id": target["building_id"], "prefab": prefab}
                return task, "start_meal", {"building_id": target["building_id"], "product": target["product"]}
            if kind == "ignite_tree":
                trees = (observed.get("camp_work") or {}).get("trees", [])
                tree = next((r for r in trees if r.get("guid") == target["tree_guid"]), None)
                if not tree or tree.get("status") != "ignitable":
                    return task, "needs_replan", {"reason": "tree_not_ignitable"}
                torches = [r for r in observed.get("equipment", []) if r.get("prefab") == "torch" and r.get("usable")]
                if not torches:
                    return task, "needs_replan", {"reason": "torch_required_for_ignition"}
                equipped = next((r for r in torches if r.get("equipped")), None)
                if not equipped:
                    return task, "equip", {"item_guid": torches[0]["guid"]}
                return task, "ignite_tree", {"tree_guid": target["tree_guid"]}
            if kind in {"attack", "defend_owner"}:
                buddy = observed.get("buddy") or {}
                hp, hp_max = buddy.get("health"), buddy.get("health_max")
                min_hp = target.get("min_health_ratio", 0.35)
                if number(hp) and number(hp_max) and hp_max > 0 and hp / hp_max < min_hp:
                    return task, "needs_replan", {"reason": "low_health_retreat"}
                enemies = observed.get("enemies", [])
                chosen = None
                if kind == "attack":
                    if "target_guid" in target:
                        chosen = next((e for e in enemies if e.get("guid") == target["target_guid"]), None)
                    else:
                        matching = [e for e in enemies if e.get("prefab") == target["prefab"]]
                        if matching:
                            chosen = min(matching, key=lambda e: e.get("distance", 99))
                    if not chosen:
                        return task, "needs_replan", {"reason": "enemy_not_in_view"}
                else:
                    threats = self._owner_threats(observed)
                    if threats:
                        chosen = threats[0]
                    else:
                        if task.get("duration") is None and task.get("end_at") is None:
                            self.goals.records[task["id"]]["defend_cleared"] = True
                            return task, "waiting", {}
                        if not observed.get("is_self") and number(buddy.get("owner_distance")) and buddy["owner_distance"] > 5:
                            navigation.append((task, "follow", {}))
                        else:
                            navigation.append((task, "waiting", {}))
                        continue
                gear_guid = self._combat_gear_equip(observed)
                if gear_guid is not None:
                    return task, "equip", {"item_guid": gear_guid}
                args = {"target_guid": chosen["guid"]}
                if "min_health_ratio" in target:
                    args["min_health_ratio"] = target["min_health_ratio"]
                return task, "attack", args
            if kind in STORAGE_TARGETS:
                cguid = container_guid_for(target, observed.get("places"))
                chest = next((r for r in observed.get("containers", []) if r["guid"] == cguid), None)
                record = self.goals.records[task["id"]]
                retry_stale = (self.clock() < record.get("storage_retry_at", 0)
                               and observed.get("version") == record.get("storage_busy_version"))
                if (chest or {}).get("reason") == "chest_busy" or retry_stale:
                    waiting = self.storage_wait(task, chest)
                    if waiting[1] == "needs_replan":
                        return waiting
                    navigation.append(waiting)
                    continue
                if not chest or not chest.get("contents_known"):
                    building_id = target.get("building_id")
                    building = next((r for r in (observed.get("places") or {}).get("buildings", [])
                                     if building_id and r["id"] == building_id), None)
                    if building and building.get("status") not in {"missing", "unavailable"} and not building.get("arrived"):
                        navigation.append((task, "visit_building", {"building_id": building_id}))
                        continue
                    return task, "needs_replan", {"reason": "chest_unavailable"}
            if kind in COUNTED_TARGETS:
                if kind == "eat" and not any(row.get("prefab") == target["prefab"] for row in observed.get("foods", [])):
                    return task, "needs_replan", {"reason": "food_not_carried_or_inedible:" + target["prefab"]}
                if kind == "heal" and not any(row.get("prefab") == target["prefab"] for row in observed.get("healers", [])):
                    return task, "needs_replan", {"reason": "healer_not_carried:" + target["prefab"]}
                if kind in {"give_owner", "deposit", "drop_item"} and not (observed["buddy"].get("inventory") or {}).get(target["prefab"], 0):
                    return task, "needs_replan", {"reason": "transfer_source_missing:" + target["prefab"]}
                if kind in {"give_owner", "deposit", "drop_item"}:
                    gear = [r for r in observed.get("equipment", []) if r["prefab"] == target["prefab"]]
                    worn = next((r for r in gear if r.get("equipped")), None)
                    if worn and not any(not r.get("equipped") for r in gear):
                        return task, *self.unequip_action(worn, observed)
                if kind in STORAGE_TARGETS:
                    if kind == "withdraw" and not any(i["prefab"] == target["prefab"] and i["count"] > 0 for i in chest.get("items", [])):
                        return task, "needs_replan", {"reason": "chest_source_missing:" + target["prefab"]}
                if kind == "give_owner" and observed["buddy"].get("owner_distance", math.inf) > 15:
                    navigation.append((task, "waiting" if observed.get("mode") == "follow" else "follow", {}))
                    continue
                record = self.goals.records[task["id"]]
                return task, kind, {"prefab": target["prefab"], "total_count": target["count"],
                                   "task_key": record["transfer_key"],
                                   **({"container_guid": target["container_guid"]} if kind in STORAGE_TARGETS else {})}
            if kind == "receive_item":
                record = self.goals.records[task["id"]]
                started = record.setdefault("wait_started", self.clock())
                if self.clock()-started >= target["timeout_seconds"]:
                    return task, "needs_replan", {"reason": "waiting_for_player_items_timed_out:" + target["prefab"]}
                if observed["buddy"].get("owner_distance", math.inf) > 4:
                    navigation.append((task, "waiting" if observed.get("mode") == "follow" else "follow", {}))
                else:
                    navigation.append((task, "awaiting_items" if observed.get("mode") == "stay" else "stay", {}))
                continue
            if kind == "inspect_storage":
                return task, "needs_replan", {"reason": "chest_unavailable"}
            if kind == "inventory":
                candidates = [row for row in observed.get("nearby", []) if row["product"] == target["prefab"]]
                if candidates:
                    return task, "collect", {"target_guid": min(candidates, key=lambda r: r["distance"])["guid"]}
                if target.get("recipe_id"):
                    return task, *self.recipe_action(target, observed)
                works = [r for r in observed.get("work_targets", []) if target["prefab"] in r.get("products", [])]
                if works and {"work", "equip"}.issubset(capabilities(observed)):
                    work = min(works, key=lambda r: r["distance"])
                    tools = [r for r in observed.get("equipment", []) if r.get("usable") and work["work"] in r.get("tools", [])]
                    if tools:
                        equipped = next((r for r in tools if r.get("equipped")), None)
                        if not equipped:
                            return task, "equip", {"item_guid": tools[0]["guid"]}
                        return task, "work", {"target_guid": work["guid"], "work": work["work"]}
                    # Trees and rocks can yield tool ingredients, but saplings
                    # and loose flint can supply them before the first tool.
                    if target["prefab"] not in HAND_GATHERED_TOOL_MATERIALS:
                        return task, "needs_replan", {"reason": "tool_required:" + work["work"]}
                if not candidates:
                    if target["prefab"] == "charcoal" and camp_execution.burning(actor_state(observed)):
                        burning_trees = [
                            r for r in (observed.get("camp_work") or {}).get("trees", [])
                            if r.get("status") == "burning"
                        ]
                        nearest = min(burning_trees, key=lambda r: r.get("distance", 99), default=None)
                        pos = (observed.get("buddy") or {}).get("position") or {}
                        if (nearest and number(nearest.get("distance")) and nearest["distance"] < 5.0
                                and number(nearest.get("x")) and number(nearest.get("z"))
                                and number(pos.get("x")) and number(pos.get("z"))):
                            dx, dz = pos["x"] - nearest["x"], pos["z"] - nearest["z"]
                            norm = math.hypot(dx, dz) or 1.0
                            rx = round(nearest["x"] + (dx / norm) * 7.0, 2)
                            rz = round(nearest["z"] + (dz / norm) * 7.0, 2)
                            navigation.append((task, "move", {"x": rx, "z": rz}))
                        else:
                            navigation.append((task, "waiting" if observed.get("mode") == "stay" else "stay", {}))
                        continue
                    if (target["prefab"] == "charcoal" and "ignite_tree" in capabilities(observed)
                            and any(r.get("status") == "ignitable" for r in (observed.get("camp_work") or {}).get("trees", []))
                            and any(r.get("prefab") == "torch" and r.get("usable") for r in observed.get("equipment", []))):
                        return task, "needs_replan", {"reason": "ignitable_tree_found"}
                    if target["prefab"] in observed.get("_team_claimed_products",[]):
                        navigation.append((task,"waiting_resource",{}))
                        continue
                    if "search" in capabilities(observed) and target.get("allow_search", True):
                        record = self.goals.records[task["id"]]
                        point = self.search.select(target["prefab"], observed,
                            max_distance=task["search_budget_distance"]-record["distance"])
                        if point:
                            products = list(dict.fromkeys(t["target"]["prefab"] for t in eligible
                                if t["target"]["type"] == "inventory" and not t["target"].get("recipe_id")))[:6]
                            remaining = task["search_budget_seconds"]-record["seconds"]
                            navigation.append((task, "search", {"point_id": point["id"], "x": point["x"], "z": point["z"],
                                "products": products, "timeout_seconds": max(.2, min(8, remaining))}))
                        else:
                            if self.search.select(target["prefab"], observed):
                                self.goals.suspend_search(task["id"])
                            else:
                                navigation.append((task, "blocked", {"reason": "no_safe_unsearched_frontier", "prefab": target["prefab"]}))
                    else:
                        navigation.append((task, "blocked", {"reason": "no_nearby_source_for_" + target["prefab"], "prefab": target["prefab"]}))
            if kind == "structure":
                return task, *self.recipe_action(target, observed)
            if kind == "equip":
                items = [r for r in observed.get("equipment", []) if r["prefab"] == target["prefab"] and r.get("usable")]
                if not items:
                    return task, "needs_replan", {"reason": "carried_equipment_required:" + target["prefab"]}
                return task, "equip", {"item_guid": items[0]["guid"]}
            if kind == "unequip":
                worn = next((r for r in observed.get("equipment", []) if r["prefab"] == target["prefab"] and r.get("equipped")), None)
                if worn:
                    return task, *self.unequip_action(worn, observed)
                return task, "needs_replan", {"reason": "equipment_telemetry_missing"}
            if kind == "move_to":
                navigation.append((task, "move", {k: target[k] for k in ("x", "z")}))
                continue
            if kind == "return_owner":
                following = observed.get("mode") == "follow"
                if following and task.get("at"):
                    self.goals.records[task["id"]]["return_departed"] = True
                navigation.append((task, "waiting" if following else "follow", {}))
                continue
            if kind in {"follow", "stay"}:
                navigation.append((task, kind, {}))
        return navigation[0] if navigation else (None, "idle", {})
