"""Building-relative placement policy shared by board and command validation."""
import math
import re
from common.task_timing import number

CAMP_STRUCTURES = {"firepit", "cookpot", "treasurechest", "researchlab2", "lightning_rod", "icebox"}
BUILD_ACTIONS = {"place", "build_campfire", "build_sciencemachine"}


def validate(raw):
    if not isinstance(raw, dict) or set(raw) - {"anchor_id", "radius"}:
        raise ValueError("Placement requires anchor_id and optional radius only")
    anchor = raw.get("anchor_id")
    radius = raw.get("radius", 10)
    if not isinstance(anchor, str) or not re.fullmatch(r"building:[0-9]+", anchor):
        raise ValueError("Placement requires an observed building:<GUID> anchor")
    if not number(radius) or not 4 <= radius <= 20:
        raise ValueError("Placement radius must be 4-20 world units")
    return {"anchor_id": anchor, "radius": radius}


def resolve(raw, state):
    policy = validate(raw)
    building = next((b for b in (state.get("places") or {}).get("buildings", [])
                     if b.get("id") == policy["anchor_id"]), None)
    if (not building or building.get("status") == "missing"
            or not all(number(building.get(k)) for k in ("x", "z"))):
        raise ValueError("Placement anchor missing or unavailable; query_places again")
    return {**policy, "x": building["x"], "z": building["z"]}


def inside(point, area):
    return (isinstance(point, dict) and all(number(point.get(k)) for k in ("x", "z"))
            and math.hypot(point["x"]-area["x"], point["z"]-area["z"]) <= area["radius"])


def progress(target, state):
    try:
        area = resolve(target["placement"], state)
    except ValueError as exc:
        return False, str(exc)
    position = state.get("player", {}).get("position") or {}
    candidates = [*(state.get("places") or {}).get("buildings", []), *state.get("recipe_entities", [])]
    for entity in state.get("nearby", []):
        candidates.append({**entity, **{k:entity.get(k, position.get(k, 0)+entity["d"+k]
            if number(position.get(k)) and number(entity.get("d"+k)) else None) for k in ("x", "z")}})
    for entity in candidates:
        prefab = entity.get("semantic_prefab") or entity.get("prefab")
        if prefab == "researchlab":
            prefab = "sciencemachine"
        if prefab == target["prefab"] and entity.get("status") != "missing" and inside(entity, area):
            return True, ""
    return False, ""


def camp_anchor_available(places):
    places = places or {}
    home_anchor = (places.get("home") or {}).get("anchor_id")
    return any(b.get("status") != "missing" and (
        b.get("id") == home_anchor or b.get("prefab") in {"researchlab", "sciencemachine"})
        for b in places.get("buildings", []))
