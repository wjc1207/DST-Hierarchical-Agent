"""Facts derived from a supplied snapshot. No game inputs, clocks or OS APIs."""
import math

COLLECT_PRODUCTS = {"flower": "petals", "grass": "cutgrass", "sapling": "twigs", "sapling_moon": "twigs",
                    "berrybush": "berries", "berrybush2": "berries", "berrybush_juicy": "berries_juicy",
                    "carrot_planted": "carrot"}
PICKABLE_SOURCE_PREFABS = frozenset(COLLECT_PRODUCTS)
EXPLORATION_RESOURCE_PREFABS = set(COLLECT_PRODUCTS) | {
    "twigs", "cutgrass", "flint", "log", "rocks", "goldnugget", "nitre", "pinecone", "acorn",
    "charcoal", "marble", "moonrocknugget", "berries", "berries_juicy", "berries_juicy_cooked",
    "carrot", "carrot_planted", "seeds", "petals",
}
SAFE_FOOD_PREFABS = ("cookedmeat", "cookedsmallmeat", "berries_cooked", "berries_juicy_cooked",
                     "carrot_cooked", "berries", "berries_juicy", "carrot", "seeds_cooked", "seeds")
RAW_COOKABLE_FOOD_PREFABS = ("meat", "smallmeat", "berries", "berries_juicy", "carrot")
MINE_PRODUCTS = {"rock1": {"rocks", "flint", "nitre"}, "rock2": {"rocks", "flint", "goldnugget"},
                 "rock_flintless": {"rocks"}, "rock_flintless_med": {"rocks"}, "rock_flintless_low": {"rocks"}}
WORK_PRODUCTS = {
    "log": ("chop_nearest_tree", {"evergreen", "evergreen_sparse", "deciduoustree", "twiggytree"}),
    **{product: ("mine_nearest_rock", {source for source, products in MINE_PRODUCTS.items() if product in products})
       for product in ("rocks", "flint", "nitre", "goldnugget")},
}
SAFETY_INTERRUPTS = {"targeted_by_hostile", "health_lost", "light_changed", "critical_hunger",
                     "critical_sanity", "critical_health", "night_light_requires_decision"}


def survival_dangers(state):
    """Known urgent facts, including low vitals without a carried remedy."""
    reasons = set()
    vitals = state.get("player", {}).get("vitals", {})
    for vital in ("health", "hunger", "sanity"):
        value, maximum = vitals.get(vital), vitals.get(vital + "_max")
        if (type(value) in (int, float) and type(maximum) in (int, float)
                and math.isfinite(value) and math.isfinite(maximum)
                and maximum > 0 and value / maximum < .30):
            reasons.add("critical_" + vital)
    nearby = state.get("nearby", [])
    if any(e.get("activeThreat") for e in nearby):
        reasons.add("targeted_by_hostile")
    torch = any(i.get("prefab") == "torch" for i in
                state.get("player", {}).get("inventory", {}).get("equipped", []))
    fire = any(e.get("prefab") in {"campfire", "firepit"} and "fire" in e.get("tags", [])
               and e.get("distance", 999) <= 8 for e in nearby)
    if state.get("world", {}).get("phase") == "night" and not torch and not fire:
        reasons.add("night_light_requires_decision")
    return reasons


def collect_target_available(target):
    if target is None:
        return False
    if target.get("prefab") in PICKABLE_SOURCE_PREFABS:
        return target.get("pickable") is True
    tags = set(target.get("tags") or [])
    return "_inventoryitem" in tags or "pickable" in tags


def collect_target_reachable(_state, target):
    """Lua supplies live path legality. There is no second Python path blacklist."""
    route = target.get("collect_path") or {}
    waypoint = route.get("waypoint") or {}
    return route.get("status") == "ready" and all(
        type(waypoint.get(axis)) in {int, float} and math.isfinite(waypoint[axis]) for axis in ("x", "z"))


def exploration_resource_available(state, entity):
    return (entity.get("prefab") in EXPLORATION_RESOURCE_PREFABS
            and collect_target_available(entity) and collect_target_reachable(state, entity))


def is_weapon_item(item):
    return item.get("prefab") != "torch" and (item.get("weapon") is True or item.get("prefab") in {
        "axe", "pickaxe", "spear", "spear_wathgrithr", "batbat", "tentaclespike", "hambat",
        "ruins_bat", "nightsword", "glasscutter"})


def conservative_tool_uses(state, prefab, maximum):
    candidates = []
    inventory = state.get("player", {}).get("inventory", {})
    for item in inventory.get("items", []) + inventory.get("equipped", []):
        if item.get("prefab") != prefab or item.get("durability_percent") is None:
            continue
        reported = int(round(float(item["durability_percent"]) * 100))
        matching = [uses for uses in range(maximum + 1) if int(uses / maximum * 100 + .5) == reported]
        if matching:
            candidates.append(min(matching))
    return max(candidates, default=0)
