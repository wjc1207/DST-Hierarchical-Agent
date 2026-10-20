"""State-change detection for a continuing exploration intent; no game I/O."""
from __future__ import annotations

MIN_RESOURCE_EVENT_DISTANCE = 4.0
RESOURCE_TYPES = {
    "grass": "cutgrass", "sapling": "twigs", "sapling_moon": "twigs",
    "berrybush": "berries", "berrybush2": "berries",
    "berrybush_juicy": "berries_juicy", "carrot_planted": "carrot", "flower": "petals",
}


def resource_types(state: dict, resource_available) -> set[str]:
    """One entry per available product type, independent of GUID and quantity."""
    return {RESOURCE_TYPES.get(e["prefab"], e["prefab"]) for e in state.get("nearby", [])
            if e.get("prefab") and resource_available(state, e)}


def region_id(state: dict):
    nav = state.get("navigation", {})
    return (nav.get("graph") or {}).get("current_node") or nav.get("current_region")


class ExplorationEvents:
    def __init__(self, initial: dict, resource_available, target_node_id: str | None = None):
        self.initial = initial
        self.available = resource_available
        self.resources = resource_types(initial, resource_available)
        self.target = target_node_id
        self.initial_region = region_id(initial)
        nav = initial.get("navigation", {})
        self.known_nodes = int((nav.get("graph") or {}).get("known_nodes") or 0)
        self.threats = {e.get("guid"): float(e.get("distance", 999))
                        for e in initial.get("nearby", []) if e.get("potentialThreat")}

    def check(self, state: dict, *, night_unsafe: bool, food_available: bool,
              distance: float = 0.0) -> dict | None:
        player = state.get("player", {})
        vitals = player.get("vitals", {})
        if vitals.get("dead"):
            return {"reason": "player_dead"}
        if player.get("guid") != self.initial.get("player", {}).get("guid"):
            return {"reason": "player_changed"}
        health = vitals.get("health")
        initial_health = self.initial.get("player", {}).get("vitals", {}).get("health")
        if health is not None and initial_health is not None and float(health) < float(initial_health):
            return {"reason": "health_lost"}
        for entity in state.get("nearby", []):
            if entity.get("activeThreat"):
                return {"reason": "targeted_by_hostile", "prefab": entity.get("prefab"),
                        "guid": entity.get("guid")}
            threat_distance = float(entity.get("distance", 999))
            if entity.get("potentialThreat") and (entity.get("guid") not in self.threats
                    or (threat_distance < 14
                        and threat_distance < self.threats[entity.get("guid")] - 0.5)):
                return {"reason": "potential_threat", "prefab": entity.get("prefab"),
                        "guid": entity.get("guid")}
        if night_unsafe:
            return {"reason": "night_light_lost"}
        world, original = state.get("world", {}), self.initial.get("world", {})
        if (world.get("day"), world.get("phase")) != (original.get("day"), original.get("phase")):
            return {"reason": "phase_changed"}
        hunger = float(vitals.get("hunger") or 0)
        maximum = max(float(vitals.get("hunger_max") or 1), 1)
        if hunger / maximum < 0.30 and food_available:
            return {"reason": "critical_hunger_with_food"}
        current_region = region_id(state)
        if self.target is not None and current_region == self.target:
            return {"reason": "destination_reached", "region_id": current_region}
        if current_region != self.initial_region:
            return {"reason": "region_changed", "region_id": current_region}
        nav = state.get("navigation", {})
        if int((nav.get("graph") or {}).get("known_nodes") or 0) > self.known_nodes:
            return {"reason": "region_discovered"}
        current = resource_types(state, self.available)
        if distance >= MIN_RESOURCE_EVENT_DISTANCE and current != self.resources:
            event = {"reason": "resource_types_changed", "before_types": sorted(self.resources),
                     "resource_types": sorted(current), "added_types": sorted(current - self.resources),
                     "removed_types": sorted(self.resources - current)}
            self.resources = current
            return event
        return None
