"""Explicit fire refueling targets; no automatic choice of valuable fuel."""
import re
from common.task_timing import number
from common.thermal import refuel_pause

FIRE_PREFABS = {"campfire", "firepit"}
FUEL_PREFABS = {"log", "charcoal", "pinecone", "acorn", "twiggy_nut", "twigs", "cutgrass"}


def validate_target(target):
    building = target.get("building_id")
    fuel = target.get("fuel_prefab")
    count = target.get("count", 1)
    ratio = target.get("at_least")
    if "at_least" in target and (not number(ratio) or not 0 < ratio <= 1):
        raise ValueError("Refuel at_least must be a fuel ratio in (0,1]")
    if "at_least" in target and "count" in target:
        raise ValueError("Refuel requires at_least or legacy count, not both")
    if not isinstance(building, str) or not re.fullmatch(r"building:[0-9]+", building):
        raise ValueError("Refuel requires an observed building:<GUID> ID")
    if not isinstance(fuel, str) or fuel not in FUEL_PREFABS:
        raise ValueError("Refuel requires a common fuel: " + ", ".join(sorted(FUEL_PREFABS)))
    if "at_least" in target:
        return {"type":"refuel","building_id":building,"fuel_prefab":fuel,"at_least":float(ratio)}
    if type(count) is not int or not 1 <= count <= 10:
        raise ValueError("Refuel count must be 1-10")
    return {"type": "refuel", "building_id": building, "fuel_prefab": fuel, "count": count}


def target_error(target, places):
    building = next((b for b in (places or {}).get("buildings", [])
                     if b.get("id") == target["building_id"]), None)
    if not building or building.get("status") == "missing":
        return "refuel building missing or unknown"
    if building.get("prefab") not in FIRE_PREFABS:
        return "refuel supports campfire and firepit only"
    return ""


def has_fuel(target, state):
    prefab = target["fuel_prefab"]
    counts = state.get("recipe_inventory") or {}
    if prefab in counts:
        return counts[prefab] > 0
    inventory = state.get("player", {}).get("inventory", {})
    return any(item.get("prefab")==prefab and item.get("count",1)>0
               for group in ("items", "equipped") for item in inventory.get(group, []))


def fuel_bounds(row):
    value = row.get("fuel_percent")
    if number(value) and 0 <= value <= 1:
        return {"min":value,"max":value,"exact":True,"lower_inclusive":True,"upper_inclusive":True}
    section = row.get("fire_section")
    if type(section) is int and 0 <= section <= 4:
        return {"min":max(0,(section-1)/4),"max":section/4,"exact":False,
                "lower_inclusive":section!=1,"upper_inclusive":section in {0,4}}
    return None


def maintenance_state(target, state):
    """Only a current fuel interval's lower bound can satisfy a ratio target."""
    error = target_error(target,state.get("places"))
    refueling = state.get("refueling") or {}
    if not error and not refueling.get("available"):
        error = "refueling telemetry unavailable; update Mod"
    if not error and refueling.get("ratio_available") is not True:
        error = "Fuel-ratio targets require updated Mod"
    row = next((r for r in refueling.get("targets",[]) if r.get("building_id")==target["building_id"]),None)
    bounds = fuel_bounds(row) if row is not None and not error else None
    satisfied = bool(bounds and bounds["min"] >= target["at_least"])
    reason = error
    if not reason and not satisfied:
        reason = refuel_pause(target,state)
        if not reason and row is not None and bounds is None:
            reason = "fuel_ratio_unavailable"
        if not reason and bounds and not bounds["exact"] and target["at_least"] > .75 and bounds["min"] >= .75:
            reason = "fuel_ratio_unverifiable"
    return {"available":not bool(error),"observed":bounds is not None,"satisfied":satisfied,
            "bounds":bounds,"needs_fuel":not satisfied,"reason":reason}
