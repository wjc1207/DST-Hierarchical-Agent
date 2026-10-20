"""One shared game-fact projection for DeepSeek and JEV; no game or API I/O."""

from __future__ import annotations

from copy import deepcopy

import controller
from exploration_events import resource_types


def model_game_state(state: dict) -> dict:
    """Use the same fields and visibility for either model, without choosing actions."""
    world = state.get("world", {})
    player = state.get("player", {})
    vitals = player.get("vitals", {})
    inventory_details = {section: player.get("inventory", {}).get(section, [])
                         for section in ("items", "equipped")}
    items, equipped = inventory_details["items"], inventory_details["equipped"]
    counts = {}
    for item in items + equipped:
        prefab = str(item.get("prefab", "unknown"))
        counts[prefab] = counts.get(prefab, 0) + int(item.get("count", 1))
    nearby = sorted(state.get("nearby", []), key=lambda e: float(e.get("distance", 999)))
    fires = [
        {"guid": e.get("guid"), "prefab": e.get("prefab"), "distance": e.get("distance"),
         "lit": "fire" in e["tags"] if e.get("tags") is not None else None,
         "cooker": "cooker" in e["tags"] if e.get("tags") is not None else None}
        for e in nearby if e.get("prefab") in {"campfire", "firepit"}
    ]
    torch_equipped = any(item.get("prefab") == "torch" for item in equipped)
    weapons = [item for item in equipped + items if controller.is_weapon_item(item)]
    best_weapon = max(weapons, key=lambda i: float(i.get("weapon_damage") or 0), default=None)
    work = state.get("work") or {}
    execution_context = state.get("_jev_execution_context")
    facts = {
        "observed_at": state.get("observed_at"),
        "state_basis": "predicted" if (execution_context or {}).get("predicted_success") else "observed",
        "world": world,
        "player_guid": player.get("guid"),
        "character": player.get("prefab"),
        "vitals": vitals,
        "position": player.get("position"),
        "inventory": counts,
        "inventory_details": inventory_details,
        "lighting": {"torch_equipped": torch_equipped, "observed_fires": fires},
        "crafting": state.get("crafting", {}),
        "navigation": state.get("navigation", {}),
        # No model-specific nearest-20 cut: retain all entities reported by the Mod,
        # including threats, work costs, stack sizes and complete collection paths.
        "nearby": [
            {**entity,
             "collect_path_status": (entity.get("collect_path") or {}).get("status"),
             "collect_path_reason": (entity.get("collect_path") or {}).get("reason")}
            for entity in nearby
        ],
        "work": work,
        "execution": state.get("execution"),
        "execution_context": execution_context,
        # Existing JEV convenience fields remain, and are now also sent to DeepSeek.
        "day": world.get("day"),
        "phase": world.get("phase"),
        "world_time": world.get("time"),
        "health": vitals.get("health"),
        "hunger": vitals.get("hunger"),
        "hunger_max": vitals.get("hunger_max"),
        "hunger_fraction": round(float(vitals.get("hunger") or 0)
                                 / max(float(vitals.get("hunger_max") or 1), 1), 3),
        "sanity": vitals.get("sanity"),
        "dead": vitals.get("dead"),
        "safe_food_count": sum(counts.get(p, 0) for p in controller.SAFE_FOOD_PREFABS),
        "cookable_raw_count": sum(counts.get(p, 0) for p in controller.RAW_COOKABLE_FOOD_PREFABS),
        "nearby_cooker": any(f["lit"] is True and f["cooker"] is True
                             and float(f["distance"] if f["distance"] is not None else 999) <= 6
                             for f in fires),
        "last_work": {"action": work.get("action"), "status": work.get("status")} if work else None,
        "torch_equipped": torch_equipped,
        "nearby_lit_fire": any(f["lit"] is True
                               and float(f["distance"] if f["distance"] is not None else 999) <= 8
                               for f in fires),
        "exploration_resource_types": sorted(resource_types(state, controller.exploration_resource_available)),
        "weapon_equipped": any(controller.is_weapon_item(item) for item in equipped),
        "weapon_carried": bool(weapons),
        "best_weapon": best_weapon.get("prefab") if best_weapon else None,
        **{f"can_craft_{recipe}": state.get("crafting", {}).get(recipe, False)
           for recipe in ("torch", "axe", "pickaxe", "campfire")},
    }
    # Neither consumers nor subsequent telemetry/board updates may rewrite a snapshot.
    return deepcopy(facts)
