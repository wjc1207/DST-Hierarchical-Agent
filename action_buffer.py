"""Conservative successful-outcome predictions for a single buffered decision."""
from __future__ import annotations

import copy

PICK_YIELDS = {"flower": ("petals", 1), "grass": ("cutgrass", 1), "sapling": ("twigs", 1),
               "sapling_moon": ("twigs", 1), "berrybush": ("berries", 1),
               "berrybush2": ("berries", 1), "berrybush_juicy": ("berries_juicy", 3)}
TOOL_RECIPES = {"axe": {"twigs": 1, "flint": 1},
                "pickaxe": {"twigs": 2, "flint": 2}}


def counts(state):
    inventory = state.get("player", {}).get("inventory", {})
    result = {}
    for group in ("items", "equipped"):
        for item in inventory.get(group, []):
            name = item.get("prefab")
            result[name] = result.get(name, 0) + int(item.get("count", 1))
    return result


def predict_success(before: dict, action: dict) -> dict | None:
    """No input, fabricated GUIDs, future map discoveries, or multi-step rollout."""
    projected = copy.deepcopy(before)
    delta = {}
    if action.get("kind") == "collect":
        target = next((e for e in projected.get("nearby", [])
                       if e.get("guid") == action.get("guid")
                       and e.get("prefab") == action.get("prefab")), None)
        if target is None:
            return None
        prefab = target["prefab"]
        if prefab in PICK_YIELDS:
            if target.get("pickable") is not True:
                return None
            product, quantity = PICK_YIELDS[prefab]
            if prefab == "flower":
                projected["nearby"].remove(target)
            else:
                target["pickable"] = False
                target["tags"] = [tag for tag in target.get("tags", []) if tag != "pickable"]
        elif "_inventoryitem" in target.get("tags", []) and target.get("stack_count", 0) > 0:
            product, quantity = prefab, int(target["stack_count"])
            projected["nearby"].remove(target)
        else:
            return None
        delta[product] = quantity
    elif action.get("kind") in {"craft_axe", "craft_pickaxe"}:
        product = action["kind"][6:]
        recipe = TOOL_RECIPES[product]
        carried = counts(before)
        if not before.get("crafting", {}).get(product) or any(carried.get(k, 0) < v for k, v in recipe.items()):
            return None
        delta = {k: -v for k, v in recipe.items()}
        delta[product] = 1
    else:
        return None

    inventory = projected["player"]["inventory"]
    for prefab, amount in delta.items():
        if amount < 0:
            remaining = -amount
            for group in ("items", "equipped"):
                for item in inventory.get(group, []):
                    if item.get("prefab") == prefab:
                        used = min(remaining, int(item.get("count", 1)))
                        item["count"] = int(item.get("count", 1)) - used
                        remaining -= used
                inventory[group] = [item for item in inventory.get(group, []) if item.get("count", 1) > 0]
            if remaining:
                return None
        else:
            item = {"prefab": prefab, "count": amount}
            group = "items"
            if prefab in TOOL_RECIPES:
                item.update(weapon=True, weapon_damage=27, durability_percent=1)
                # The shipped builder equips a newly crafted tool only when
                # its equipment slot is empty; never replace a held item.
                if not any(e.get("slot") == "hands" for e in inventory.get("equipped", [])):
                    item["slot"] = "hands"
                    group = "equipped"
            inventory.setdefault(group, []).append(item)
    # Never retain CanBuild=True after spending the last required ingredient.
    recipes = {**TOOL_RECIPES, "torch": {"twigs": 2, "cutgrass": 2},
               "campfire": {"log": 2, "cutgrass": 3},
               "sciencemachine": {"log": 4, "rocks": 4, "goldnugget": 1}}
    carried = counts(projected)
    for name, recipe in recipes.items():
        if any(carried.get(k, 0) < v for k, v in recipe.items()):
            projected.setdefault("crafting", {})[name] = False
    projected["_jev_execution_context"] = {
        "current_action": action["kind"], "stage": "confirmed_started",
        "decision_applies": "only_after_verified_success",
        "predicted_success": True, "inventory_delta": delta,
        "assumption": "This state predicts successful completion of the running action; it is not yet observed.",
    }
    return projected


def started(state: dict, action: dict, *, since: float = 0) -> bool:
    execution = state.get("execution") or {}
    if not execution.get("doing") and not execution.get("confirmed_start"):
        return False
    if execution.get("issued_at") is not None and float(execution["issued_at"]) < float(since):
        return False
    if action.get("kind") == "collect":
        return (execution.get("action") in {"PICK", "PICKUP"}
                and execution.get("target_guid") == action.get("guid"))
    if action.get("kind") in {"craft_axe", "craft_pickaxe"}:
        return execution.get("action") == "BUILD" and execution.get("recipe") == action["kind"][6:]
    return False
