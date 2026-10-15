"""State-change detection for one JEV-selected exploration intent; no game I/O."""
from __future__ import annotations


def region_id(state: dict):
    nav = state.get("navigation", {})
    return (nav.get("graph") or {}).get("current_node") or nav.get("current_region")


def resource_id(entity: dict):
    guid = entity.get("guid")
    return (guid, entity.get("prefab")) if guid is not None else None


def observe_depleted_resources(state: dict, seen: set, depleted: dict) -> None:
    """Only explicit pickable=False is depletion; absence/path failure is not."""
    observed_at = float(state.get("observed_at") or 0)
    for entity in state.get("nearby", []):
        identity = resource_id(entity)
        if identity in seen and entity.get("pickable") is False:
            depleted[identity] = max(observed_at, depleted.get(identity, observed_at))


class ExplorationEvents:
    def __init__(self, initial: dict, resource_available, seen: set,
                 target_node_id: str | None = None, *, depleted: dict | None = None):
        self.initial = initial
        self.available = resource_available
        self.seen = seen
        self.depleted = depleted if depleted is not None else {}
        self.target = target_node_id
        self.initial_region = region_id(initial)
        nav = initial.get("navigation", {})
        self.known_nodes = int((nav.get("graph") or {}).get("known_nodes") or 0)
        self.threats = {e.get("guid"): float(e.get("distance", 999))
                        for e in initial.get("nearby", []) if e.get("potentialThreat")}
        # Choosing exploration declines resources already available to JEV.
        self.remember(initial)

    def remember(self, state: dict):
        observe_depleted_resources(state, self.seen, self.depleted)
        observed_at = float(state.get("observed_at") or 0)
        for entity in state.get("nearby", []):
            identity = resource_id(entity)
            if identity is not None and self.available(state, entity):
                self.seen.add(identity)
                # A stale decision snapshot must not acknowledge a later
                # depletion/regrowth that happened while JEV was responding.
                if self.depleted.get(identity, observed_at) <= observed_at:
                    self.depleted.pop(identity, None)

    def check(self, state: dict, *, night_unsafe: bool, food_available: bool) -> dict | None:
        observe_depleted_resources(state, self.seen, self.depleted)
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
        new = [e for e in state.get("nearby", [])
               if resource_id(e) is not None
               and (resource_id(e) not in self.seen or resource_id(e) in self.depleted)
               and self.available(state, e)]
        if new:
            first = min(new, key=lambda e: float(e.get("distance", 999)))
            renewed = resource_id(first) in self.seen
            self.remember(state)
            return {"reason": "resource_available_again" if renewed else "new_resource", "prefab": first.get("prefab"),
                    "guid": first.get("guid"), "count": len(new)}
        return None
