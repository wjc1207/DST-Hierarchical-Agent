"""Synthetic observations for the MCP-only regression suite."""
from common import game_rules as rules
from mcp_agent.navigation_intents import dispatch_for_navigation

def grass(guid: int, distance: float, pickable: bool) -> dict:
    return {
        "guid": guid,
        "prefab": "grass",
        "distance": distance,
        "dx": distance,
        "dz": 0,
        "tags": ["pickable"] if pickable else [],
        "pickable": pickable,
        "collect_path": {"status": "ready", "reason": "direct", "distance": distance,
                         "waypoint": {"x": distance, "z": 0}},
    }


def hostile(guid: int, distance: float) -> dict:
    return {
        "guid": guid,
        "prefab": "hound",
        "distance": distance,
        "dx": distance,
        "dz": 0,
        "tags": ["hostile", "monster"],
        "attackable": True,
        "activeThreat": True,
    }


def lit_campfire(guid: int = 40, distance: float = 3.0) -> dict:
    return {
        "guid": guid,
        "prefab": "campfire",
        "distance": distance,
        "dx": distance,
        "dz": 0,
        "tags": ["campfire", "fire", "cooker"],
        "attackable": False,
    }


def state(nearby: list[dict], cutgrass: int = 0) -> dict:
    nearby = [
        {**entity, "collect_path": {"status": "ready", "reason": "direct",
             "distance": entity.get("distance", 0), "waypoint": {
                 "x": entity.get("dx", entity.get("distance", 0)), "z": entity.get("dz", 0)}}}
        if rules.collect_target_available(entity) and "collect_path" not in entity else entity
        for entity in nearby
    ]
    items = []
    if cutgrass:
        items.append({"prefab": "cutgrass", "count": cutgrass})
    return {
        "world": {"day": 1, "phase": "day", "time": 0.2},
        "player": {
            "vitals": {"health": 150, "hunger": 140, "hunger_max": 150, "sanity": 200},
            "inventory": {"items": items, "equipped": []},
            "position": {"x": 0.0, "y": 0.0, "z": 0.0},
        },
        "crafting": {"torch": False, "axe": False, "pickaxe": False, "campfire": False},
        "nearby": nearby,
        "navigation": {
            "mode": "frontier",
            "status": "ready",
            "target": {
                "x": 0.0,
                "z": 12.0,
                "distance": 12.0,
                "information_gain": 4,
                "visits": 0,
                "score": 34.0,
            },
            "leg": {"x": 0.0, "z": 4.0, "dx": 0.0, "dz": 4.0, "distance": 4.0},
        },
        "camera": {
            "right": {"x": 1, "z": 0},
            "forward": {"x": 0, "z": 1},
        },
    }

def route(node="B", name="grassland 2"):
    return {"node_id": node, "name": name, "biome": "grassland", "status": "ready",
            "explored_area": 240, "boundary_complete": False, "entered": True,
            "hops": 2, "distance": 8,
            "target": {"x": 8, "z": 0}, "leg": {"x": 4, "z": 0, "distance": 4}}


def observed():
    value = state([])
    value["navigation"].update({
        "current_region": "A", "current_biome": "forest",
        "graph": {"current_node": "A", "current": {
            "id": "A", "name": "mixed 1", "explored_area": 640,
            "boundary_complete": False}, "routes": [route()]},
        "search": {"status": "ready", "strategy": "directional", "boundary_complete": False,
                   "target": {"x": 0, "z": 20, "distance": 20},
                   "leg": {"x": 0, "z": 4, "distance": 4}},
    })
    return value


def native_navigation_dispatch(state):
    # Fake only the already-offered navigation surface; Lua availability has its own tests.
    if any(e.get("activeThreat") for e in state.get("nearby", [])):
        return {}
    if state.get("world",{}).get("phase")=="night" and not any(i.get("prefab")=="torch" for i in state.get("player",{}).get("inventory",{}).get("equipped",[])):
        return {}
    nav=state.get("navigation",{}); actions={}
    for key,area,mode in (("explore_current_region",nav,"region"),("explore_other_region",nav.get("search",{}),"search")):
        if area.get("status")=="ready" and area.get("leg"):
            actions[key]={"kind":"explore","mode":mode,**area["leg"]}
    for i,r in enumerate((nav.get("graph") or {}).get("routes",[]),1):
        if r.get("status")=="ready" and r.get("leg"):
            actions[f"travel_region_{i}"]={"kind":"explore","mode":"graph","node_id":r["node_id"],**r["leg"]}
    return dispatch_for_navigation(actions,state)
