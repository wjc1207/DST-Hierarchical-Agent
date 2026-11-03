"""Conservative bounds for ordinary work during residence; safety is exempt."""
import math
from common.task_timing import number


def inside(point, area, margin=0):
    return (isinstance(point, dict) and all(number(point.get(k)) and number(area.get(k)) for k in ("x","z"))
            and area["radius"] >= margin
            and math.hypot(point["x"]-area["x"],point["z"]-area["z"]) <= area["radius"]-margin)


def allowed(action, state, area):
    position = state.get("player", {}).get("position")
    if not inside(position, area, .5):
        return False
    kind = action.get("kind")
    if kind == "explore" and action.get("refuel_approach") is True:
        building = next((b for b in (state.get("places") or {}).get("buildings", [])
                         if b.get("id")==action.get("building_id")), None)
        route = (building or {}).get("route") or {}
        length = route.get("distance")
        return (bool(building) and building.get("prefab") in {"campfire","firepit"}
                and building.get("status")!="missing" and route.get("status")=="ready"
                and action.get("mode")=="place"
                and inside(building,area,2) and inside(action,area,2)
                and number(length) and length>=0 and inside(position,area,length+2))
    if kind == "explore" or kind == "flee_from_nearest_hostile":
        return False
    if kind == "place":
        return inside(action, area, 2)
    if kind in {"build_campfire", "build_sciencemachine"}:
        return inside(position, area, 6)
    if kind in {"collect", "chop_nearest_tree", "mine_nearest_rock", "add_fuel"}:
        entity = next((e for e in state.get("nearby", []) if e.get("guid") == action.get("guid")), None)
        if not entity:
            return False
        point = {axis:entity.get(axis,position[axis]+entity["d"+axis] if number(entity.get("d"+axis)) else None)
                 for axis in ("x","z")}
        if kind == "add_fuel":
            length = entity.get("distance")
            return inside(point, area, 2) and number(length) and 0 <= length <= 3 and inside(position, area, length+2)
        margin = 6 if kind != "collect" or entity.get("prefab") == "berrybush_juicy" else 2
        path = entity.get("collect_path") or {}
        length = path.get("distance")
        # Bound the entire approach by path length, not just its endpoint.
        return (inside(point, area, margin) and number(length) and length >= 0
                and inside(position, area, length+2))
    return kind in {"wait","craft","craft_torch","craft_axe","craft_pickaxe","eat_safe_food",
                    "cook_food","equip_torch","unequip_torch","set_home"}


def validate_area(raw, state):
    if not isinstance(raw, dict) or set(raw) != {"home_id","radius"}:
        raise ValueError("Residence requires home_id and radius only")
    home = (state.get("places") or {}).get("home") or {}
    if home.get("id") != raw["home_id"] or not all(number(home.get(k)) for k in ("x","z")):
        raise ValueError("Residence home unset, changed or unavailable")
    radius = raw["radius"]
    if not number(radius) or not 6 <= radius <= 30:
        raise ValueError("Residence radius must be 6-30 world units")
    return {"home_id":raw["home_id"],"radius":radius,"x":home["x"],"z":home["z"]}
