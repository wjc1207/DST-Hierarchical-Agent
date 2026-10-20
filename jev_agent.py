"""Executor-owned exploration with JEV local choices from fresh Mod telemetry."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Dict, Optional, Tuple

import controller
import timing_log as timing
from daily_planner import DailyPlanner
from decision_prefetch import DecisionPrefetch, current_action, compatible
from action_buffer import predict_success, started as buffered_action_started
from task_board import board_view
from board_executor import LocalBoardExecutor, task_progressed
from model_state import model_game_state
from exploration_executor import ExplorationExecutor, local_candidates, exploration_key
from exploration_events import resource_types


DEFAULT_API_URL = "https://api.typesafe.ai/v1/systemone"
DEFAULT_MODEL = "jev-latest"
DEFAULT_INSTALLED_ENV = Path(
    r"C:\Program Files (x86)\Steam\steamapps\common\Don't Starve Together\mods\jev_dst_agent\.env"
)
SAFE_COLLECT_PREFABS = {
    "grass": "Cut grass source; needed urgently for a torch.",
    "sapling": "Twig source; needed urgently for a torch.",
    "sapling_moon": "Twig source; needed for basic crafting.",
    "flower": "Optional flower picking for petals and potential sanity benefit; not a hunger resource. Some rose variants can hurt; weigh benefit against risk and survival needs.",
    "petals": "Loose petals from flowers; optional materials, not a hunger resource.",
    "twigs": "Loose twigs; needed urgently for a torch.",
    "cutgrass": "Loose cut grass; needed urgently for a torch.",
    "flint": "Loose flint; useful for early tools.",
    "log": "Loose log; needed for campfires and early structures.",
    "rocks": "Loose rocks; needed for early tools and structures.",
    "goldnugget": "Loose gold; useful for early tools and structures.",
    "nitre": "Loose nitre from mined rocks; useful for later crafting.",
    "pinecone": "Loose pine cone from a felled tree; can be replanted.",
    "acorn": "Loose birchnut from a felled tree; can be replanted.",
    "charcoal": "Loose charcoal from a burnt tree; useful for crafting.",
    "marble": "Loose marble from mining; useful for later crafting.",
    "moonrocknugget": "Loose moon rock from mining.",
    "berries": "Food that can reduce early hunger risk.",
    "berries_juicy": "Loose juicy berries; edible raw or cooked near a lit fire.",
    "berries_juicy_cooked": "Loose cooked juicy berries; ready to eat.",
    "berrybush": "Berry source if it currently offers a pick action.",
    "berrybush2": "Berry source if it currently offers a pick action.",
    "berrybush_juicy": "Juicy berry source if it currently offers a pick action.",
    "carrot": "Loose food.",
    "carrot_planted": "Food source.",
    "seeds": "Low-priority emergency food.",
}
SAFE_FOOD_PREFABS = {
    "cookedmeat",
    "cookedsmallmeat",
    "berries_cooked",
    "berries_juicy_cooked",
    "carrot_cooked",
    "berries",
    "berries_juicy",
    "carrot",
    "seeds_cooked",
    "seeds",
}
COOKABLE_FOOD_PRODUCTS = {
    "meat": "cookedmeat",
    "smallmeat": "cookedsmallmeat",
    "berries": "berries_cooked",
    "berries_juicy": "berries_juicy_cooked",
    "carrot": "carrot_cooked",
}
CRITICAL_HUNGER_RATIO = 0.30
TOOL_MAX_USES = {"axe": 100, "pickaxe": 33}
PICKABLE_SOURCE_PREFABS = {
    "flower",
    "grass",
    "sapling",
    "sapling_moon",
    "berrybush",
    "berrybush2",
    "berrybush_juicy",
}


def load_dotenv(path: Path) -> None:
    """Load a minimal KEY=VALUE dotenv file without logging secret values."""
    if not path.exists():
        raise RuntimeError(f".env file was not found: {path}")
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        name = name.strip()
        value = value.strip()
        if value and value[0:1] == value[-1:] and value.startswith(("'", '"')):
            value = value[1:-1]
        if name:
            os.environ.setdefault(name, value)


def resolve_env_path(explicit: Optional[Path]) -> Path:
    candidates = []
    if explicit is not None:
        candidates.append(explicit)
    candidates.extend(
        [
            Path(__file__).resolve().parent / ".env",
            Path.cwd() / ".env",
            DEFAULT_INSTALLED_ENV,
        ]
    )
    for candidate in candidates:
        if candidate.exists():
            return candidate
    raise RuntimeError(
        "No .env file found. Put it beside jev_agent.py or pass --env PATH."
    )


def inventory_counts(state: dict) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    inventory = state.get("player", {}).get("inventory", {})
    for item in inventory.get("items", []):
        prefab = str(item.get("prefab", "unknown"))
        counts[prefab] = counts.get(prefab, 0) + int(item.get("count", 1))
    for item in inventory.get("equipped", []):
        prefab = str(item.get("prefab", "unknown"))
        counts[prefab] = counts.get(prefab, 0) + int(item.get("count", 1))
    return counts


def is_weapon_item(item: dict) -> bool:
    return controller.is_weapon_item(item)


def weapon_status(state: dict) -> Tuple[bool, bool, Optional[str]]:
    inventory = state.get("player", {}).get("inventory", {})
    equipped = [item for item in inventory.get("equipped", []) if is_weapon_item(item)]
    carried = [item for item in inventory.get("items", []) if is_weapon_item(item)]
    best = max(
        equipped + carried,
        key=lambda item: float(item.get("weapon_damage") or 0),
        default=None,
    )
    return bool(equipped), bool(equipped or carried), None if best is None else best.get("prefab")


def has_equipped_torch(state: dict) -> bool:
    equipped = state.get("player", {}).get("inventory", {}).get("equipped", [])
    return any(item.get("prefab") == "torch" for item in equipped)


def has_nearby_lit_fire(state: dict, maximum_distance: float = 8.0) -> bool:
    return any(
        entity.get("prefab") in {"campfire", "firepit"}
        and "fire" in entity.get("tags", [])
        and float(entity.get("distance", 999)) <= maximum_distance
        for entity in state.get("nearby", [])
    )


def nearest_cookable_fire(state: dict, maximum_distance: float = 6.0) -> Optional[dict]:
    return min(
        (
            entity for entity in state.get("nearby", [])
            if entity.get("prefab") in {"campfire", "firepit"}
            and {"fire", "cooker"}.issubset(set(entity.get("tags", [])))
            and float(entity.get("distance", 999)) <= maximum_distance
        ),
        key=lambda entity: float(entity.get("distance", 999)),
        default=None,
    )


def conservative_tool_uses(state: dict, prefab: str) -> int:
    """Return the minimum possible uses represented by DST's rounded percent."""
    maximum = TOOL_MAX_USES[prefab]
    possible_uses = []
    inventory = state.get("player", {}).get("inventory", {})
    for item in inventory.get("items", []) + inventory.get("equipped", []):
        if item.get("prefab") != prefab or item.get("durability_percent") is None:
            continue
        reported = int(round(float(item["durability_percent"]) * 100))
        matching = [
            uses for uses in range(maximum + 1)
            if int(uses / maximum * 100 + 0.5) == reported
        ]
        if matching:
            possible_uses.append(min(matching))
    return max(possible_uses, default=0)


def has_immediate_threat(state: dict, threshold: float = 5.0) -> bool:
    return any(
        entity.get("activeThreat")
        and float(entity.get("distance", 999)) <= threshold
        for entity in state.get("nearby", [])
    )


def can_eat_safe_food(state: dict) -> bool:
    counts = inventory_counts(state)
    if any(counts.get(prefab, 0) > 0 for prefab in SAFE_FOOD_PREFABS):
        return True
    return nearest_cookable_fire(state) is not None and any(
        counts.get(prefab, 0) > 0 for prefab in COOKABLE_FOOD_PRODUCTS
    )


def action_allowed(state: dict, action_kind: str) -> tuple[bool, str]:
    """Return (allowed, reason_if_blocked). Filter actions that are meaningless or unsafe."""
    phase = state.get("world", {}).get("phase")
    torch_equipped = has_equipped_torch(state)
    nearby_lit_fire = has_nearby_lit_fire(state)
    immediate_threat = has_immediate_threat(state)
    if immediate_threat:
        allowed = {
            "flee_from_nearest_hostile",
            "equip_weapon",
            "attack_nearest_hostile",
        }
        if action_kind not in allowed:
            return False, "immediate threat: only flee, equip weapon, or attack are allowed"

    vitals = state.get("player", {}).get("vitals", {})
    hunger = float(vitals.get("hunger") or 0)
    hunger_max = max(float(vitals.get("hunger_max") or 1), 1)
    if (
        not immediate_threat
        and hunger / hunger_max < CRITICAL_HUNGER_RATIO
        and can_eat_safe_food(state)
    ):
        # Eating dominates optional actions, but escape and immediate light remain available.
        emergency_actions = {"eat_safe_food", "flee_from_nearest_hostile"}
        if phase == "night" and not torch_equipped and not nearby_lit_fire:
            emergency_actions.update({"equip_torch", "craft_torch", "build_campfire"})
        if action_kind not in emergency_actions:
            return False, "critical hunger: eat carried food before optional actions"

    # night policy: no chopping or mining
    if phase == "night" and action_kind in {
        "chop_nearest_tree", "mine_nearest_rock", "equip_weapon", "attack_nearest_hostile"
    }:
        return False, "chopping, mining, equipping weapons, and attacking are forbidden at night"

    # A stationary campfire is not portable light for travel to a target.
    if phase == "night" and action_kind in {"explore", "collect"} and not torch_equipped:
        return False, "exploration or collection at night requires an equipped torch, even beside a lit fire"

    # night no torch: no movement or collection
    if phase == "night" and not torch_equipped and not nearby_lit_fire:
        allowed = {
            "wait",
            "craft_torch",
            "equip_torch",
            "build_campfire",
            "flee_from_nearest_hostile",
            "eat_safe_food",
        }
        if action_kind not in allowed:
            return False, "movement or collection blocked at night without light"

    # day/dusk or nearby fire: no torch equip
    if action_kind in {"equip_torch", "build_campfire"} and (phase != "night" or nearby_lit_fire):
        return False, "torch should not be equipped in daylight or beside a lit fire"

    # night equip torch: no unequip torch
    if action_kind == "unequip_torch" and phase == "night" and not nearby_lit_fire:
        return False, "torch should not be unequipped at night without a lit fire"

    return True, ""

@timing.timed("decision.candidates")
def build_candidates(state: dict) -> Tuple[Dict[str, str], Dict[str, dict]]:
    """Return [criteria, dispatch]. Generate available actions. """
    criteria: Dict[str, str] = {}
    dispatch: Dict[str, dict] = {}
    nearest_by_prefab: Dict[str, dict] = {}

    for entity in state.get("nearby", []):
        prefab = str(entity.get("prefab", ""))
        if prefab not in SAFE_COLLECT_PREFABS:
            continue
        if not controller.collect_target_available(entity) or not controller.collect_target_reachable(state, entity):
            continue
        current = nearest_by_prefab.get(prefab)
        if current is None or float(entity.get("distance", 999)) < float(current.get("distance", 999)):
            nearest_by_prefab[prefab] = entity

    for prefab, entity in sorted(nearest_by_prefab.items()):
        action_id = f"collect_{prefab}"
        distance = float(entity.get("distance", 0))
        leftover_note = (
            " Chopping or mining already picks up nearby matching drops; use this for leftovers."
            if prefab in {"log", "rocks"} else ""
        )
        criteria[action_id] = (
            f"Approach and collect the nearest reachable observed {prefab}, {distance:.2f} world units away. "
            f"Verified land-route length: {float(entity['collect_path'].get('distance', distance)):.2f} units. "
            f"{SAFE_COLLECT_PREFABS[prefab]} Choose only when its benefit exceeds exploration risk."
            f"{leftover_note}"
        )
        dispatch[action_id] = {
            "kind": "collect",
            "prefab": prefab,
            "guid": entity.get("guid"),
        }

    navigation = state.get("navigation", {})
    biome = navigation.get("current_biome") or "unidentified terrain"
    topology = navigation.get("current_region") or "unknown"
    graph = navigation.get("graph") or {}
    current_area = graph.get("current") or {}
    area_name = current_area.get("name") or f"{biome} ({topology})"
    area_size = current_area.get("explored_area")
    coverage = (f" Observed area: {float(area_size):.0f} square world units; total area unknown."
                if area_size is not None else " Observed area not reported.")
    frontier_target = navigation.get("target")
    frontier_leg = navigation.get("leg")
    if navigation.get("status") == "ready" and frontier_target and frontier_leg:
        criteria["explore_current_region"] = (
            f"Continue searching the current topology region {area_name}; already here. "
            f"The local frontier target is at "
            f"({float(frontier_target.get('x', 0)):.2f}, {float(frontier_target.get('z', 0)):.2f}), "
            f"{float(frontier_target.get('distance', 0)):.2f} units away. Begin with the next "
            f"{float(frontier_leg.get('distance', 0)):.2f}-unit leg, then keep this intent while nothing new occurs. "
            "Return for a decision on new useful resources, threats, region/phase changes, "
            "or path failure; otherwise keep exploring without a time or distance decision timer. "
            f"Look for local resources rather than making sustained outward progress."
            + coverage
            + (" The next leg follows observed land around an obstruction." if frontier_leg.get("detour") else "")
        )
        dispatch["explore_current_region"] = {
            "kind": "explore",
            "navigation_mode": "region",
            "target_x": frontier_leg.get("x"),
            "target_z": frontier_leg.get("z"),
            "leg_distance": frontier_leg.get("distance"),
            "frontier_x": frontier_target.get("x"),
            "frontier_z": frontier_target.get("z"),
        }
    biome_resource_hints = {
        "forest": "trees and saplings",
        "grassland": "grass and saplings",
        "savanna": "grass; beefalo may be present",
        "swamp": "reeds, with significant hostile risk",
        "rocky": "rocks and flint; some rocks may yield gold",
        "deciduous_forest": "deciduous trees and scattered food",
        "desert": "desert resources, but do not assume an oasis",
    }
    # Legacy frames remain readable, but returning is never a separate decision.
    routes = graph.get("routes")
    if routes is None:
        routes = [*(graph.get("adjacent") or []), graph.get("return_route") or {}]
    seen_destinations = set()
    for index, route in enumerate(routes, start=1):
        if route.get("status") != "ready" or not route.get("target") or not route.get("leg"):
            continue
        if not controller.graph_route_available(str(route.get("node_id") or ""),
                route.get("gateway_id"), state.get("player", {}).get("guid")):
            continue
        if not route.get("node_id") or route["node_id"] in seen_destinations:
            continue
        seen_destinations.add(route["node_id"])
        destination = route.get("biome") or "unknown terrain"
        observed = ", ".join(route.get("resources_seen") or []) or "none recorded"
        terrain = route.get("terrain") or {}
        hint = biome_resource_hints.get(destination)
        if hint is None:
            hint = "; ".join(biome_resource_hints[k] for k in sorted(terrain)
                             if terrain[k] and k in biome_resource_hints) or "no specific terrain prior"
        action_id = f"travel_region_{index}"
        name = route.get("name") or route["node_id"]
        area = route.get("explored_area")
        explored = f"{float(area):.0f} square world units" if area is not None else "not reported"
        boundary = "fully observed" if route.get("boundary_complete") else "not fully observed"
        criteria[action_id] = (
            f"Go to known region {name} ({destination}; ID {route['node_id']}). "
            f"Observed area: {explored}; boundary {boundary}; "
            f"{'previously entered' if route.get('entered') else 'discovered, not yet entered'}. "
            f"The route has {int(route.get('hops', 1))} region connection(s); "
            f"about {float(route.get('distance', 0)):.1f} units to the next exit, not the final destination. "
            f"Resources actually seen there: {observed}. Terrain-based possibilities, "
            f"not observations: {hint}. Follow verified short legs toward this destination "
            "until arrival or a meaningful event, without periodically asking for another decision."
        )
        dispatch[action_id] = {
            "kind": "explore", "navigation_mode": "graph",
            "target_node_id": route["node_id"],
            "target_x": route["leg"]["x"], "target_z": route["leg"]["z"],
            "leg_distance": route["leg"]["distance"],
            "frontier_x": route["target"]["x"],
            "frontier_z": route["target"]["z"],
        }
    search = navigation.get("search") or {}
    search_target = search.get("target") or {}
    search_leg = search.get("leg") or {}
    if (search.get("status") == "ready" and search_target and search_leg
            and search.get("strategy") != "road"
            and not current_area.get("boundary_complete") and not search.get("boundary_complete")):
        criteria["explore_other_region"] = (
            f"Search for a NEW region by investigating unknown exits of {area_name}. "
            f"Current strategy: {search.get('strategy', 'directional')}; preserve the outward heading "
            "across intervening collection or crafting. Advance directionally; bypass obstacles and follow a safe "
            "boundary side when blocked, resuming the original heading when possible. "
            f"Next waypoint: {float(search_target.get('distance', 0)):.2f} units away. "
            f"Begin with the next {float(search_leg.get('distance', 0)):.2f}-unit leg, "
            "then continue the same intent without another decision unless a new resource, "
            "threat, region/phase change or path failure occurs. There is no periodic "
            "time/distance decision timer. Known destinations remain separate choices. " + coverage
        )
        dispatch["explore_other_region"] = {
            "kind": "explore",
            "navigation_mode": "search",
            "search_region_id": search.get("region_id") or current_area.get("id"),
            "target_x": search_leg.get("x"),
            "target_z": search_leg.get("z"),
            "leg_distance": search_leg.get("distance"),
            "frontier_x": search_target.get("x"),
            "frontier_z": search_target.get("z"),
        }

    counts = inventory_counts(state)
    torch_count = counts.get("torch", 0)
    torch_equipped = has_equipped_torch(state)
    if state.get("crafting", {}).get("torch", False):
        criteria["craft_torch"] = (
            f"Craft one additional torch; {torch_count} currently carried or equipped. "
            "This option is exposed because the game reports that the recipe is currently craftable. "
            "If DST auto-equips it during day or dusk, the controller immediately unequips it "
            "to save fuel. Consider current reserves and the task board before spending materials; "
            "prioritize preparing light before night when reserves are insufficient."
        )
        dispatch["craft_torch"] = {"kind": "craft_torch"}
    if torch_count > 0 and not torch_equipped:
        criteria["equip_torch"] = (
            "Equip the existing torch because it is night and no lit campfire or firepit is nearby."
        )
        dispatch["equip_torch"] = {"kind": "equip_torch"}
    if torch_equipped:
        criteria["unequip_torch"] = (
            "Unequip the torch from the hand slot and return it to the backpack to preserve fuel. "
            "Useful during day or dusk, or when remaining beside a lit fire at night. "
            "If travelling away from fire at night, keep the torch equipped."
        )
        dispatch["unequip_torch"] = {"kind": "unequip_torch"}

    crafting = state.get("crafting", {})
    if counts.get("axe", 0) == 0 and crafting.get("axe", False):
        criteria["craft_axe"] = (
            "Craft a basic axe. Exposed only because the recipe is currently craftable and no axe is held. "
            "Choose when nearby trees or a need for logs justify spending materials."
        )
        dispatch["craft_axe"] = {"kind": "craft_axe"}
    if counts.get("pickaxe", 0) == 0 and crafting.get("pickaxe", False):
        criteria["craft_pickaxe"] = (
            "Craft a basic pickaxe. Exposed only because the recipe is currently craftable and no pickaxe is held. "
            "Choose when nearby mineable rocks justify spending materials."
        )
        dispatch["craft_pickaxe"] = {"kind": "craft_pickaxe"}

    nearby = state.get("nearby", [])
    chop_targets = [
        entity for entity in nearby
        if "CHOP_workable" in entity.get("tags", []) and float(entity.get("distance", 999)) <= 6
    ]
    mine_targets = [
        entity for entity in nearby
        if "MINE_workable" in entity.get("tags", []) and float(entity.get("distance", 999)) <= 6
    ]
    attackable_targets = [
        entity for entity in nearby
        if entity.get("attackable") is True and float(entity.get("distance", 999)) <= 8
    ]
    pursuing_targets = [
        entity for entity in nearby
        if entity.get("activeThreat") is True and float(entity.get("distance", 999)) <= 12
    ]
    weapon_equipped, weapon_carried, best_weapon = weapon_status(state)
    axe_uses = conservative_tool_uses(state, "axe")
    pickaxe_uses = conservative_tool_uses(state, "pickaxe")
    eligible_chop_targets = [
        entity for entity in chop_targets
        if entity.get("work_required") is not None
        and axe_uses >= int(entity["work_required"])
    ]
    eligible_mine_targets = [
        entity for entity in mine_targets
        if entity.get("work_required") is not None
        and pickaxe_uses >= int(entity["work_required"])
    ]
    if  eligible_chop_targets:
        target = min(eligible_chop_targets, key=lambda entity: float(entity.get("distance", 999)))
        required = int(target["work_required"])
        criteria["chop_nearest_tree"] = (
            f"Complete the nearest tree-like target ({target.get('prefab')}) and then pick up "
            "nearby drops such as logs and pinecones within 4 units of that tree (up to 12 stacks). "
            f"It is {float(target.get('distance', 0)):.2f} units away and is budgeted for {required} chops; "
            f"the selected axe has at least {axe_uses} uses left. This is one longer action, "
            "not a single chop."
        )
        dispatch["chop_nearest_tree"] = {"kind": "chop_nearest_tree"}
    if  eligible_mine_targets:
        target = min(eligible_mine_targets, key=lambda entity: float(entity.get("distance", 999)))
        required = int(target["work_required"])
        criteria["mine_nearest_rock"] = (
            f"Complete the nearest rock target ({target.get('prefab')}) and then pick up "
            "nearby drops such as rocks, flint, nitre, and gold within 4 units of that rock (up to 12 stacks). "
            f"It is {float(target.get('distance', 0)):.2f} units away and requires at most {required} strikes; "
            f"the selected pickaxe has at least {pickaxe_uses} uses left. This is one longer action, "
            "not a single strike."
        )
        dispatch["mine_nearest_rock"] = {"kind": "mine_nearest_rock"}
    if pursuing_targets:
        target = min(pursuing_targets, key=lambda entity: float(entity.get("distance", 999)))
        threat_distance = float(target.get("distance", 999))
        criteria["flee_from_nearest_hostile"] = (
            f"Escape the nearest pursuing hostile ({target.get('prefab')}) at {threat_distance:.2f} units "
            "using passable directions that increase separation, including a sideways route when the direct "
            "away direction is blocked. Recheck after each leg; stop when pursuit clears or at the safety limit. "
            "Strongly prefer when unarmed, badly hurt, or fighting is unnecessary."
        )
        dispatch["flee_from_nearest_hostile"] = {
            "kind": "flee_from_nearest_hostile",
            "guid": target.get("guid"),
        }
    if attackable_targets:
        target = min(attackable_targets, key=lambda entity: float(entity.get("distance", 999)))
        threat_distance = float(target.get("distance", 999))
        if weapon_carried and not weapon_equipped:
            criteria["equip_weapon"] = (
                f"Equip the best carried weapon ({best_weapon}) before fighting. The hostile is "
                f"{threat_distance:.2f} units away. Prefer fleeing instead if there is no safe time to equip."
            )
            dispatch["equip_weapon"] = {"kind": "equip_weapon"}
        if weapon_equipped and threat_distance <= 6.0:
            vitals_for_combat = state.get("player", {}).get("vitals", {})
            health = float(vitals_for_combat.get("health") or 0)
            health_max = max(float(vitals_for_combat.get("health_max") or 1), 1)
            criteria["attack_nearest_hostile"] = (
                f"Attack the nearest hostile ({target.get('prefab')}) once with the equipped weapon at "
                f"{threat_distance:.2f} units. Health is {health:.1f}/{health_max:.1f}; prefer fleeing at low health "
                "or when combat is not necessary. Attackable alone does not prove a creature is hostile; "
                "do not choose this for a neutral target."
            )
            dispatch["attack_nearest_hostile"] = {"kind": "attack_nearest_hostile"}

    vitals = state.get("player", {}).get("vitals", {})
    hunger = float(vitals.get("hunger") or 0)
    hunger_max = max(float(vitals.get("hunger_max") or 1), 1)
    fire = nearest_cookable_fire(state)
    raw_food = [prefab for prefab in COOKABLE_FOOD_PRODUCTS if counts.get(prefab, 0) > 0]
    cook_then_eat = fire is not None and bool(raw_food) and not any(
        counts.get(prefab, 0) > 0 for prefab in COOKABLE_FOOD_PRODUCTS.values()
    )
    if can_eat_safe_food(state) and hunger / hunger_max < 0.80:
        urgency = (
            "Hunger is critical: eat immediately; waiting or optional collection risks starvation. "
            if hunger / hunger_max < CRITICAL_HUNGER_RATIO else ""
        )
        if cook_then_eat:
            eating_plan = (
                "With no preferred cooked food carried and a nearby lit cooker, cook one item first "
                "and then eat the cooked food as one action. If cooking fails, safely edible "
                "raw berries or carrots are a fallback; raw meat is not."
            )
        else:
            eating_plan = "Eat one available safe food item directly; no separate cooking step is needed."
        criteria["eat_safe_food"] = (
            f"{urgency}Hunger is {hunger:.1f}/{hunger_max:.1f}. {eating_plan}"
        )
        dispatch["eat_safe_food"] = {"kind": "eat_safe_food"}

    if fire is not None and raw_food:
        criteria["cook_food"] = (
            f"Cook one carried raw food item on the nearby lit {fire['prefab']} "
            f"{float(fire.get('distance', 0)):.2f} units away. Available raw foods: "
            f"{', '.join(raw_food)}. The action finishes only after a cooked item appears in inventory. "
            "Choose this to prepare food for later; when the immediate goal is to eat, "
            "choose eat_safe_food because it handles cooking if needed."
        )
        dispatch["cook_food"] = {"kind": "cook_food"}

    if crafting.get("campfire", False):
        criteria["build_campfire"] = (
            "Build a campfire at the nearest valid open position. Exposed only because the recipe is craftable, "
            "it is night, and no lit campfire or firepit is nearby."
        )
        dispatch["build_campfire"] = {"kind": "build_campfire"}

    if crafting.get("sciencemachine", False):
        criteria["build_sciencemachine"] = (
            "Build a science machine at the nearest valid open position. Exposed only because the recipe is craftable."
        )
        dispatch["build_sciencemachine"] = {"kind": "build_sciencemachine"}
    
    criteria["wait"] = (
        "No local action is needed now; return control to the exploration executor. "
        "It may continue safe navigation, or remain still when no safe route is available. "
        "Choose a local survival action instead when immediate intervention is needed."
    )
    dispatch["wait"] = {"kind": "wait"}

    # filter out any actions that are blocked by hard safety rules
    rejected = {}
    for key in list(criteria):
        allowed, reason = action_allowed(state, dispatch[key]["kind"])
        if allowed and dispatch[key]["kind"] == "explore":
            hazard = controller.exploration_hazard_on_leg(
                state, float(dispatch[key]["target_x"]), float(dispatch[key]["target_z"]))
            if hazard is not None:
                allowed, reason = False, "next leg approaches remembered or visible threat"
        if not allowed:
            rejected[key] = reason
            del criteria[key]
    timing.annotate(candidate_rejections=rejected)
    dispatch = {k: v for k, v in dispatch.items() if k in criteria}
    return criteria, dispatch


def build_jev_candidates(state):
    return local_candidates(*build_candidates(state))


def candidate_context(state: dict, keys) -> dict:
    """Explain absent movement without secrets, raw prompts or guessing a route."""
    nav = state.get("navigation", {})
    graph, search = nav.get("graph") or {}, nav.get("search") or {}
    routes = graph.get("routes")
    if routes is None:
        routes = [*(graph.get("adjacent") or []), *([graph["return_route"]] if graph.get("return_route") else [])]
    return {
        "keys": list(keys), "count": len(keys), "only_wait": set(keys) == {"wait"},
        "frontier_status": nav.get("status"), "frontier_count": nav.get("frontier_count"),
        "search_status": search.get("status"),
        "boundary_complete": (graph.get("current") or {}).get("boundary_complete"),
        "known_regions": graph.get("known_nodes"), "reported_routes": len(routes),
        "path_diagnostics": nav.get("diagnostics", {}),
    }


@timing.timed("decision.state_compact")
def compact_game_state(state: dict, daily_plan: Optional[dict] = None) -> dict:
    compact = {
        **model_game_state(state),
        "goal": "Survive indefinitely and steadily improve food, light, tools, resources, and safety.",
        "hard_rules": [
            "Choose only an action listed in the available criteria; unavailable actions cannot be executed.",
            "At night without an equipped torch, exploration and collection are blocked even beside a stationary fire; fleeing an immediate threat is an exception.",
            "A torch can be equipped only at night and only when no lit campfire or firepit is nearby. A campfire can be built only at night when no lit fire is nearby.",
        ],
        "decision_guidance": [
            "Exploration and known-region travel are owned by the executor, not by you. Choose a useful available local action or wait to return control; wait is not a request to stop safe exploration. Resource opportunities and survival changes can interrupt navigation, whose intent is preserved for resumption.",
            "Both models use the same game-state fields. inventory_details separates backpack items and equipped slots; durability_percent is a 0..1 remaining fraction (torch fuel), with missing values unknown. lighting.observed_fires reports current burning and distance, not remaining fire lifetime. state_basis marks observed versus predicted snapshots.",
            "Exploration watches the SET of currently collectible, reachable resource product types, not individual GUIDs or counts. Ordinary resource changes yield only after at least 4 world units of displacement from the execution start and while that set differs; threats and other safety/navigation events may interrupt earlier.",
            "Assess immediate threats, darkness, and starvation before optional progress; compare the remaining safe actions rather than following a fixed script.",
            "eat_safe_food is a complete eating action: if no preferred cooked food is carried and a lit cooker is nearby, it cooks one item and then eats; cook_food alone only prepares food for later.",
            "craft_torch is a complete crafting action: if DST auto-equips the new torch before night, the controller unequips it to preserve fuel.",
            "chop_nearest_tree and mine_nearest_rock include bounded pickup of nearby work drops, including secondary materials. Collect leftovers separately only when useful items remain visible.",
            "At night near a lit fire, unequip a torch to save fuel only if staying near that fire; keep it equipped when moving away.",
            "Prefer a useful safe action over repeated waiting, but do not take unnecessary risks for optional resources.",
            "A nearby potentialThreat may attack on approach even when activeThreat is false; avoid routes through remembered hazards.",
            "Regions are game topology rooms, not individual ground tiles; mixed terrain can belong to one region. Names have stable topology IDs, and observed area is not total area or a completion percentage.",
            "The executor selects local frontiers, unknown exits and known-region travel from the task board and observations. These are not your action choices; focus on worthwhile local actions or return control with wait.",
            "A task_board (or legacy daily_plan) is strategic advice from a separate planner, not a command. Prefer ready/in_progress tasks with higher priority (1 highest); ignore done, blocked or expired tasks. Choose the fastest safe available next action that advances a task. You may respond to new opportunities and emergencies instead; never generate a plan or wait merely because a hint is unavailable.",
            "When execution_context.predicted_success is true, the supplied state is the predicted successful outcome of the running action, not an observation. Choose just one next action conditional on that success; do not expand a plan. Otherwise use observed facts. The controller verifies the outcome and revalidates your choice before execution.",
        ],
    }
    if daily_plan is not None:
        if daily_plan.get("kind") == "task_board":
            compact["task_board"] = board_view(daily_plan, state)
        else:
            compact["daily_plan"] = daily_plan
    return compact


@timing.timed("decision.request")
def call_jev(
    api_url: str, api_key: str, model: str, state: dict,
    criteria: Dict[str, str], daily_plan: Optional[dict] = None,
) -> dict:
    # Also enforce the boundary for direct callers, not only the control loop.
    criteria = {k: v for k, v in criteria.items() if not exploration_key(k)}
    body = {
        "state": json.dumps(compact_game_state(state, daily_plan), ensure_ascii=False, separators=(",", ":")),
        "model": model,
        "questions": {
            "next_action": {
                "type": "choice",
                "instructions": (
                    "Choose exactly one action key from the supplied criteria. Treat each key as "
                    "one semantic action, even when the controller performs several verified substeps. "
                    "The executor owns exploration; choose only among local actions. "
                    "Decide the next action quickly from current state and criteria; do not plan ahead. "
                    "Obey hard rules and immediate survival needs. Treat task_board or legacy daily_plan, when present, "
                    "as optional strategic guidance, never as permission to ignore current facts. "
                    "Do not request unavailable or redundant substeps."
                ),
                "criteria": criteria,
            }
        },
    }
    request = urllib.request.Request(
        api_url,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "jev-dst-agent/0.1",
        },
        method="POST",
    )
    try:
        with timing.span("api.response_headers"):
            response = urllib.request.urlopen(request, timeout=20)
        with response:
            with timing.span("api.read_body"):
                response_body = response.read()
            with timing.span("api.parse_response"):
                payload = json.loads(response_body.decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:1000]
        raise RuntimeError(f"JEV API HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"JEV API connection failed: {exc.reason}") from exc

    answer = payload.get("answers", {}).get("next_action")
    if not isinstance(answer, dict) or not answer.get("choice"):
        raise RuntimeError("JEV response did not contain answers.next_action.choice")
    if answer["choice"] not in criteria:
        raise RuntimeError(f"JEV selected unavailable local action: {answer['choice']}")
    return {"response": payload, "answer": answer}


@timing.timed("action.execute")
def execute_action(action: dict, log_path: Path, move_seconds: float = 1.0) -> bool:
    kind = action["kind"]
    if kind == "collect":
        return controller.collect(log_path, action["prefab"], preferred_guid=action.get("guid"),
                                  strict_preferred=action.get("_strict_preferred", False))
    elif kind == "explore":
        if action.get("navigation_mode") == "road":
            raise ValueError("Road-following exploration has been removed; update the mod")
        return controller.follow_exploration(log_path, action, move_seconds,
                                             baseline=action.get("_decision_state"),
                                             on_event=action.get("_on_exploration_event"))
    elif kind == "wait":
        with timing.span("action.wait"):
            time.sleep(1.0)
    elif kind == "craft_torch":
        controller.craft_torch(log_path)
    elif kind == "equip_torch":
        controller.equip_torch(log_path)
    elif kind == "unequip_torch":
        controller.unequip_torch(log_path)
    elif kind == "craft_axe":
        controller.craft_inventory_item(log_path, "axe", "craft_axe")
    elif kind == "craft_pickaxe":
        controller.craft_inventory_item(log_path, "pickaxe", "craft_pickaxe")
    elif kind == "build_campfire":
        controller.build_campfire(log_path)
    elif kind == "build_sciencemachine":
        controller.build_sciencemachine(log_path)
    elif kind == "eat_safe_food":
        controller.eat_safe_food(log_path)
    elif kind == "cook_food":
        controller.cook_food(log_path)
    elif kind == "chop_nearest_tree":
        controller.complete_work_action(log_path, kind, "CHOP_workable")
    elif kind == "mine_nearest_rock":
        controller.complete_work_action(log_path, kind, "MINE_workable")
    elif kind == "attack_nearest_hostile":
        controller.force_attack(log_path)
    elif kind == "equip_weapon":
        controller.equip_weapon(log_path)
    elif kind == "flee_from_nearest_hostile":
        controller.flee_from_hostile(log_path, preferred_guid=action.get("guid"))
    else:
        raise RuntimeError(f"Unsupported bounded action kind: {kind}")
    return True


def default_execution_threshold(choice: str, state: Optional[dict] = None) -> float:
    """Only attacks retain a confidence gate; legality checks are separate."""
    return 0.45 if choice == "attack_nearest_hostile" else 0.0


def run_local_board_step(args, state, board, dispatch, planner, prefetch, executor):
    """One verified local action per loop; repeated loops need no model request."""
    local_dispatch = dict(dispatch)
    if can_eat_safe_food(state) and action_allowed(state, "eat_safe_food")[0]:
        local_dispatch.setdefault("eat_safe_food", {"kind": "eat_safe_food"})
    chosen = executor.choose(board, state, local_dispatch)
    if chosen is None:
        return False
    task, choice, selected = chosen
    if prefetch is not None:
        prefetch.discard("local_board_execution")
    timing.annotate(decision_source="board_auto", action=choice, current_action=choice,
                    board_task_id=task["id"], board_auto_action=choice)
    print(f"board_auto task={task['id']} action={choice} "
          f"progress={task['progress']['current']}/{task['progress']['required']}")
    finished = False
    attempted = False
    try:
        with timing.span("board.auto_execute"):
            with controller.observe_action(None), controller.guard_states(args.log, executor.guard(state, selected)):
                fresh, _ = controller.latest_state(args.log)
                # Match the currently legal target immediately before sending input.
                _, refreshed = build_candidates(fresh)
                if choice == "eat_safe_food" and can_eat_safe_food(fresh) and action_allowed(fresh, choice)[0]:
                    refreshed[choice] = {"kind": choice}
                fresh_chosen = executor.choose(board, fresh, refreshed)
                if fresh_chosen is None or executor.key(board, task, selected) != executor.key(board, fresh_chosen[0], fresh_chosen[2]):
                    raise controller.AutoExecutionInterrupted("task_or_target_changed")
                attempted = True
                executed = execute_action(fresh_chosen[2], args.log, args.move_seconds)
                completed, _ = controller.latest_state(args.log)
        if executed and task_progressed(board, task, fresh, completed):
            finished = True
            if planner is not None:
                planner.record_action(fresh, choice, "executed")
            timing.annotate(outcome="executed", board_auto_outcome="executed")
            print("board_auto_completed")
            return True
        executor.pause(board, task, selected)
        timing.annotate(board_auto_outcome="no_progress")
        print("board_auto_handoff reason=no_progress")
    except controller.AutoExecutionInterrupted as exc:
        timing.annotate(board_auto_outcome="interrupted", board_handoff_reason=str(exc))
        print(f"board_auto_handoff reason={exc}")
    except (RuntimeError, OSError, ValueError) as exc:
        executor.pause(board, task, selected)
        timing.annotate(board_auto_outcome="failed", board_handoff_reason=type(exc).__name__)
        if planner is not None:
            planner.record_action(state, choice, "failed")
        print(f"board_auto_handoff reason=execution_failed error={type(exc).__name__}")
    finally:
        controller.release_movement_keys()
        # Also runs on Ctrl+C. Stop queued work, but never send F4 to an
        # unrelated foreground application after a failed focus attempt.
        if attempted and not finished and selected["kind"] in {"chop_nearest_tree", "mine_nearest_rock"}:
            try:
                controller.focus_game(controller.find_game_window())
                controller.tap([controller.VK["cancel_work"]], 0.04)
            except (RuntimeError, OSError) as exc:
                print(f"board_auto_cancel_failed error={type(exc).__name__}")
    return False


def run_executor_exploration(args, state, board, dispatch, planner, prefetch, executor,
                             force=False):
    if not args.execute:
        return False
    executor.fresh_handoff = None
    selected = executor.choose(state, dispatch, board)
    if selected is None:
        return False
    local = {k for k, a in dispatch.items() if a["kind"] not in {"explore", "wait"}}
    if not force and executor.handoff and local:
        return False
    # Validate against an observation taken immediately before starting movement.
    fresh, _ = controller.latest_state(args.log)
    executor.fresh_handoff = fresh
    _, fresh_dispatch = build_candidates(fresh)
    selected = executor.choose(fresh, fresh_dispatch, board)
    if selected is None:
        return False
    fresh_local = {k for k, a in fresh_dispatch.items() if a["kind"] not in {"explore", "wait"}}
    new_resources = executor.resources != resource_types(state, controller.exploration_resource_available)
    if fresh_local and (new_resources or (not force and executor.handoff)):
        executor.handoff = True
        return False
    key, action = selected
    if prefetch is not None:
        prefetch.discard("executor_exploration")
    executor.handoff, executor.last_event = False, None
    timing.annotate(decision_source="exploration_auto", action=key, current_action=key,
                    exploration_owner="executor", exploration_strategy=executor.strategy,
                    exploration_goal=executor.goal)
    print(f"exploration_auto strategy={executor.strategy} goal={executor.goal} action={key}")
    try:
        with timing.span("exploration.auto_execute"), controller.observe_action(None):
            moved = execute_action({**action, "_on_exploration_event": executor.on_event},
                                   args.log, args.move_seconds)
        if not moved and executor.last_event is None:
            executor.on_event({"reason": "movement_stalled"}, fresh)
        if planner is not None:
            planner.record_action(fresh, key, "executed" if moved else "skipped")
        timing.annotate(outcome="executed" if moved else "skipped")
    finally:
        controller.release_movement_keys()
    return True


def run_decision_cycle(
    args, api_url: str, api_key: str, model: str,
    planner: Optional[DailyPlanner] = None,
    prefetch: Optional[DecisionPrefetch] = None,
    local_executor: Optional[LocalBoardExecutor] = None,
    exploration_executor: Optional[ExplorationExecutor] = None,
) -> bool:
    """Run one sense-decide-act cycle. Return True at a terminal game state."""
    with timing.span("cycle.observe"):
        state, _ = controller.latest_state(args.log)
    world = state.get("world", {})
    vitals = state.get("player", {}).get("vitals", {})
    timing.annotate(day=world.get("day"), phase=world.get("phase"), model=model)
    if vitals.get("dead"):
        timing.annotate(outcome="terminal")
        print("goal_failed: player is dead; control loop stopped")
        return True
    navigator = exploration_executor if exploration_executor is not None else ExplorationExecutor()
    with timing.span("planner.update"):
        daily_plan = planner.update(state) if planner is not None else None
    if daily_plan and daily_plan.get("kind") == "task_board":
        timing.annotate(task_board_id=daily_plan.get("board_id"),
                        ready_task_ids=daily_plan.get("ready_task_ids", []))
    criteria, dispatch = build_candidates(state)
    if args.execute and daily_plan and daily_plan.get("kind") == "task_board":
        if run_local_board_step(args, state, daily_plan, dispatch, planner, prefetch,
                                local_executor if local_executor is not None else LocalBoardExecutor()):
            return False
        # After any attempted auto action, hand off using actual latest state,
        # never the snapshot from before collection, damage or a phase change.
        state, _ = controller.latest_state(args.log)
        world = state.get("world", {})
        if state.get("player", {}).get("vitals", {}).get("dead"):
            timing.annotate(outcome="terminal")
            return True
        daily_plan = planner.update(state) if planner is not None else daily_plan
        timing.annotate(day=world.get("day"), phase=world.get("phase"),
                        task_board_id=(daily_plan or {}).get("board_id"),
                        ready_task_ids=(daily_plan or {}).get("ready_task_ids", []))
        criteria, dispatch = build_candidates(state)
    if run_executor_exploration(args, state, daily_plan, dispatch, planner, prefetch, navigator):
        return False
    if navigator.fresh_handoff is not None:
        state = navigator.fresh_handoff
        world = state.get("world", {})
        if state.get("player", {}).get("vitals", {}).get("dead"):
            timing.annotate(outcome="terminal")
            return True
        timing.annotate(day=world.get("day"), phase=world.get("phase"))
        criteria, dispatch = build_candidates(state)
    criteria, dispatch = local_candidates(criteria, dispatch)
    if args.execute and set(criteria) == {"wait"}:
        if prefetch is not None:
            prefetch.discard("only_local_wait")
        timing.annotate(decision_source="executor_idle", action="wait", current_action="wait", outcome="executed")
        execute_action({"kind": "wait"}, args.log, args.move_seconds)
        return False
    timing.annotate(decision_source="synchronous")
    cached = prefetch.take(state, dispatch, plan=daily_plan) if prefetch is not None else None
    if cached is None:
        if prefetch is not None:
            with timing.span("decision.prefetch_refresh"):
                state, _ = controller.latest_state(args.log)
                world = state.get("world", {})
                timing.annotate(day=world.get("day"), phase=world.get("phase"))
                if state.get("player", {}).get("vitals", {}).get("dead"):
                    timing.annotate(outcome="terminal")
                    return True
                criteria, dispatch = build_jev_candidates(state)
        request_started = time.monotonic()
        decision_state = state
        decision_candidates = list(criteria)
        result = call_jev(api_url, api_key, model, state, criteria, daily_plan)
        selected = dispatch.get(result["answer"]["choice"])
        decision_seconds = time.monotonic() - request_started
    else:
        result, selected, decision_seconds = cached
        decision_state = prefetch.source
        decision_candidates = list(prefetch.dispatch)
    answer = result["answer"]
    choice = answer["choice"]
    confidence = float(answer.get("confidence", 0))
    timing.annotate(action=choice, confidence=confidence)
    context = candidate_context(decision_state, decision_candidates)
    timing.annotate(decision_candidates=context)
    if context["only_wait"]:
        print("only_wait_available " + json.dumps(context, ensure_ascii=False, separators=(",", ":")))
    print(
        f"day={world.get('day')} phase={world.get('phase')} "
        f"model={result['response'].get('model', model)} "
        f"choice={choice} confidence={confidence:.3f} decision_time={decision_seconds:.2f}s"
    )
    if args.show_probabilities:
        print(json.dumps(answer.get("probabilities", {}), indent=2, sort_keys=True))

    if selected is None:
        raise RuntimeError(f"JEV selected unknown action: {choice}")
    if not args.execute:
        timing.annotate(outcome="dry_run")
        print("dry_run: no game input sent")
        return False

    # Both synchronous and prefetched responses can outlive their observations.
    # Rebuild legal actions, match the same semantic target, and execute the
    # current verified leg rather than stale coordinates or a reordered index.
    with timing.span("decision.revalidate"):
        state, _ = controller.latest_state(args.log)
        if state.get("player", {}).get("vitals", {}).get("dead"):
            timing.annotate(outcome="terminal")
            print("goal_failed: player died while awaiting the decision")
            return True
        if cached is not None and not compatible(prefetch.source, state):
            timing.annotate(outcome="skipped")
            print("action_skipped: state changed while awaiting prefetched response")
            return False
        _, fresh_dispatch = build_jev_candidates(state)
        match = current_action(selected, fresh_dispatch)
    if match is None:
        timing.annotate(outcome="skipped")
        print("action_skipped: chosen target or prerequisites changed; asking JEV again")
        return False
    current_choice, selected = match
    if selected["kind"] == "explore":
        selected = {**selected, "_decision_state": decision_state}
    timing.annotate(current_action=current_choice)

    threshold = (
        args.min_confidence
        if selected["kind"] == "attack_nearest_hostile" and args.min_confidence is not None
        else default_execution_threshold(current_choice, state)
    )
    print(f"execution_threshold={threshold:.2f}")
    if confidence < threshold:
        timing.annotate(outcome="abstained")
        print(f"abstained: confidence below {threshold:.2f}")
        return False

    if selected["kind"] == "wait":
        navigator.after_local_action("wait")
        _, navigation_dispatch = build_candidates(state)
        if run_executor_exploration(args, state, daily_plan, navigation_dispatch,
                                    planner, prefetch, navigator, force=True):
            timing.annotate(jev_local_choice="wait")
            return False
        if navigator.fresh_handoff is not None and navigator.handoff:
            timing.annotate(outcome="skipped")
            print("local handoff changed before exploration; requesting a fresh local decision")
            return False

    pending = None
    prediction_submitted = False

    def checkpoint(observed, reason):
        nonlocal pending, prediction_submitted
        kind = selected["kind"]
        if prefetch is None:
            return True
        if reason == "action_pending" and observed is not None:
            pending = (observed, observed.get("_pending_action", {}))
            return True
        predicting = False
        if reason == "action_progress":
            if prediction_submitted and pending is not None and observed is not None:
                if not compatible(pending[0], observed, check_inventory=False):
                    prefetch.discard("event_during_current_action")
                    timing.annotate(buffer_validation="event_during_current_action")
                return True
            if pending is None or prediction_submitted or observed is None:
                return True
            before, running = pending
            if not buffered_action_started(observed, running, since=float(before.get("observed_at") or 0)):
                return True
            if not compatible(before, observed, check_inventory=False):
                return True
            projected = predict_success(before, running)
            if projected is None:
                return True
            predicting = True
        elif prediction_submitted:
            # Do not queue a second request at verification; one action in
            # progress plus one advisory next-action response is the limit.
            return True
        # Exploration yields on new information. Request from the next cycle's
        # fresh observation, not an intermediate event snapshot during cleanup.
        eligible = (kind in {"collect", "eat_safe_food", "cook_food", "craft_axe", "craft_pickaxe"}
                    and reason == "verified")
        if not eligible and not predicting:
            return True
        if kind == "eat_safe_food" and observed is not None:
            # A composite eat can first verify cooking. Prefetch only once
            # hunger actually rises, after the food has been consumed.
            old_hunger = float(state.get("player", {}).get("vitals", {}).get("hunger") or 0)
            new_hunger = float(observed.get("player", {}).get("vitals", {}).get("hunger") or 0)
            if new_hunger <= old_hunger:
                return True
        try:
            with timing.span("decision.prefetch_checkpoint"):
                if observed is None:
                    _, position = controller.latest_state(args.log)
                    observed, _ = controller.wait_for_fresh_state(args.log, position, timeout=0.15)
                if (observed.get("player", {}).get("vitals", {}).get("dead")
                        or controller.nearest_hostile(observed, maximum_distance=12.0)
                        or controller.night_travel_without_torch(observed)):
                    prefetch.discard("danger_or_darkness")
                    return False
                request_state = projected if predicting else {**observed, "_jev_execution_context": {
                    "current_action": current_choice, "stage": reason,
                    "decision_applies": "after_current_bounded_action",
                }}
                upcoming, actions = build_jev_candidates(request_state)
                with timing.span("decision.prefetch_submit"):
                    accepted = prefetch.submit(request_state, upcoming, actions, daily_plan, predicted=predicting)
                if accepted and predicting:
                    prediction_submitted = True
                    timing.annotate(buffer_start_source=(observed.get("execution") or {}).get("start_source", "native_buffered"))
                    print(f"jev_buffer_predicted action={kind} start_source="
                          f"{(observed.get('execution') or {}).get('start_source', 'native_buffered')}")
        except (RuntimeError, OSError, ValueError) as exc:
            print(f"jev_prefetch_checkpoint_skipped error={type(exc).__name__}")
        return True

    try:
        with controller.observe_action(checkpoint if prefetch is not None else None):
            executed = execute_action(selected, args.log, args.move_seconds)
    except (RuntimeError, OSError, ValueError):
        if prefetch is not None:
            prefetch.discard("action_failed")
        if planner is not None:
            planner.record_action(state, choice, "failed")
        raise
    if prefetch is not None and prediction_submitted:
        if executed:
            with timing.span("decision.buffer_confirm"):
                completed, _ = controller.latest_state(args.log)
                prefetch.confirm(completed)
        else:
            prefetch.discard("action_not_completed")
    if planner is not None:
        planner.record_action(state, choice, "executed" if executed else "skipped")
    timing.annotate(outcome="executed" if executed else "skipped")
    if executed and selected["kind"] != "wait":
        navigator.after_local_action(selected["kind"])
    print("action_executed" if executed else "action_skipped: state changed before execution")
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", type=Path, help="Path to the .env file")
    parser.add_argument("--log", type=Path, default=controller.DEFAULT_LOG)
    parser.add_argument("--timing-log", type=Path, default=timing.DEFAULT_LOG,
                        help="Per-cycle JSONL timing log (default: logs/timing.jsonl beside this script)")
    parser.add_argument("--timing-details", action="store_true",
                        help="Also print individual timing spans after each cycle")
    parser.add_argument("--no-prefetch", action="store_true",
                        help="Disable next-decision prefetch for a serial timing baseline")
    parser.add_argument("--execute", action="store_true", help="Execute the selected action")
    parser.add_argument(
        "--min-confidence",
        type=float,
        help="Override attack confidence threshold only; other actions have no confidence gate",
    )
    parser.add_argument("--show-probabilities", action="store_true")
    parser.add_argument(
        "--loop",
        action="store_true",
        help="Continuously sense, ask JEV, and optionally execute until death or interruption",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=0.0,
        help="Seconds to wait after each completed cycle (default: 0, no cooldown)",
    )
    parser.add_argument(
        "--move-seconds",
        type=float,
        default=1.0,
        help="Exploration pulse duration scale and cap (default: 1.0, range: 0.2-2.0)",
    )
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()

    if args.self_test:
        sample = {
            "world": {"day": 1, "phase": "day", "time": 0.2},
            "player": {"vitals": {"health": 150, "hunger": 140, "sanity": 200}, "inventory": {"items": []}},
            "crafting": {"torch": False},
            "nearby": [
                {
                    "guid": 101,
                    "prefab": "grass",
                    "distance": 3.0,
                    "tags": ["pickable"],
                    "pickable": True,
                    "collect_path": {"status": "ready", "waypoint": {"x": 3.0, "z": 0}},
                }
            ],
        }
        criteria, dispatch = build_candidates(sample)
        assert "collect_grass" in criteria
        assert dispatch["collect_grass"] == {
            "kind": "collect",
            "prefab": "grass",
            "guid": 101,
        }
        depleted = dict(sample)
        depleted["nearby"] = [
            {"prefab": "grass", "distance": 1.0, "tags": [], "pickable": False},
            {"prefab": "sapling", "distance": 1.0, "tags": [], "pickable": False},
            {"prefab": "berrybush", "distance": 1.0, "tags": [], "pickable": False},
        ]
        depleted_criteria, _ = build_candidates(depleted)
        assert "collect_grass" not in depleted_criteria
        assert "collect_sapling" not in depleted_criteria
        assert "collect_berrybush" not in depleted_criteria
        loose_resources = dict(sample)
        loose_resources["nearby"] = [
            {"guid": 102, "prefab": "log", "distance": 2.0, "tags": ["_inventoryitem"],
             "collect_path": {"status": "ready", "waypoint": {"x": 2.0, "z": 0}}},
            {"guid": 103, "prefab": "rocks", "distance": 2.5, "tags": ["_inventoryitem"],
             "collect_path": {"status": "ready", "waypoint": {"x": 2.5, "z": 0}}},
        ]
        loose_criteria, loose_dispatch = build_candidates(loose_resources)
        assert loose_dispatch["collect_log"] == {"kind": "collect", "prefab": "log", "guid": 102}
        assert loose_dispatch["collect_rocks"] == {"kind": "collect", "prefab": "rocks", "guid": 103}
        assert compact_game_state(sample)["goal"].startswith("Survive")
        assert default_execution_threshold("explore_current_region") == 0.0
        assert default_execution_threshold("explore_other_region") == 0.0
        assert default_execution_threshold("collect_grass") == 0.0
        assert default_execution_threshold("craft_torch") == 0.0
        assert default_execution_threshold("build_campfire") == 0.0
        assert default_execution_threshold("attack_nearest_hostile") == 0.45
        assert default_execution_threshold("wait") == 0.0
        assert "craft_torch" not in criteria
        craftable = dict(sample)
        craftable["crafting"] = {"torch": True}
        craft_criteria, craft_dispatch = build_candidates(craftable)
        assert "craft_torch" in craft_criteria
        assert craft_dispatch["craft_torch"] == {"kind": "craft_torch"}
        equipped = {
            "world": {"day": 1, "phase": "day"},
            "crafting": {"torch": False},
            "player": {"inventory": {"items": [], "equipped": [{"prefab": "torch", "count": 1}]}},
            "nearby": [],
        }
        equipped_criteria, equipped_dispatch = build_candidates(equipped)
        assert "unequip_torch" in equipped_criteria
        assert equipped_dispatch["unequip_torch"] == {"kind": "unequip_torch"}
        equipped["world"]["phase"] = "night"
        night_criteria, _ = build_candidates(equipped)
        assert "unequip_torch" not in night_criteria
        survival = {
            "world": {"day": 1, "phase": "night"},
            "crafting": {"torch": False, "axe": True, "pickaxe": True, "campfire": True, "sciencemachine": False},
            "player": {
                "vitals": {"health": 150, "hunger": 50, "hunger_max": 150, "sanity": 200},
                "inventory": {"items": [{"prefab": "berries", "count": 2}], "equipped": []},
            },
            "nearby": [],
        }
        survival_criteria, _ = build_candidates(survival)
        assert "build_campfire" in survival_criteria
        assert "craft_torch" not in survival_criteria
        assert "build_sciencemachine" not in survival_criteria
        durable = {
            "world": {"day": 1, "phase": "day"},
            "crafting": {},
            "player": {
                "vitals": {"health": 150, "hunger": 100, "hunger_max": 150, "sanity": 200},
                "inventory": {
                    "items": [{"prefab": "axe", "count": 1, "durability_percent": 0.15}],
                    "equipped": [],
                },
            },
            "nearby": [
                {"prefab": "evergreen", "distance": 3, "tags": ["CHOP_workable"], "work_required": 15}
            ],
        }
        durable_criteria, _ = build_candidates(durable)
        assert "chop_nearest_tree" in durable_criteria
        durable["player"]["inventory"]["items"][0]["durability_percent"] = 0.14
        worn_criteria, _ = build_candidates(durable)
        assert "chop_nearest_tree" not in worn_criteria
        threat = {
            "world": {"day": 1, "phase": "day"},
            "crafting": {},
            "player": {
                "vitals": {"health": 150, "hunger": 100, "hunger_max": 150, "sanity": 200},
                "inventory": {"items": [], "equipped": []},
            },
            "nearby": [
                {"guid": 201, "prefab": "tallbird", "distance": 4.0, "tags": [], "activeThreat": True, "attackable": True},
            ],
        }
        threat_criteria, threat_dispatch = build_candidates(threat)
        assert "flee_from_nearest_hostile" in threat_criteria
        assert threat_dispatch["flee_from_nearest_hostile"]["guid"] == 201
        assert "equip_weapon" not in threat_criteria
        assert "attack_nearest_hostile" not in threat_criteria
        print("self-test passed")
        return 0

    env_path = resolve_env_path(args.env)
    load_dotenv(env_path)
    api_key = os.environ.get("JEV_API_KEY") or os.environ.get("TYPESAFE_API_KEY")
    if not api_key:
        raise RuntimeError(f"JEV_API_KEY is missing or empty in {env_path}")
    api_url = os.environ.get("JEV_API_URL", DEFAULT_API_URL)
    model = os.environ.get("JEV_MODEL", DEFAULT_MODEL)
    deepseek_key = os.environ.get("DEEPSEEK_API_KEY")
    planner = DailyPlanner(
        deepseek_key,
        os.environ.get("DEEPSEEK_API_URL", "https://api.deepseek.com/chat/completions"),
        os.environ.get("DEEPSEEK_MODEL", "deepseek-flash"),
    ) if deepseek_key and args.loop and args.execute else None
    if not 0.0 <= args.interval <= 60.0:
        raise ValueError("--interval must be between 0 and 60 seconds")
    if not 0.2 <= args.move_seconds <= 2.0:
        raise ValueError("--move-seconds must be between 0.2 and 2.0 seconds")

    if args.loop:
        mode = "execute" if args.execute else "dry-run"
        print(f"control_loop_started mode={mode} interval={args.interval:.1f}s")
        if planner is None and args.execute:
            print("daily_planner_disabled: DEEPSEEK_API_KEY not set")

    print(f"timing_log={args.timing_log.resolve()}")

    prefetch = DecisionPrefetch(
        lambda state, criteria, plan: call_jev(api_url, api_key, model, state, criteria, plan)
    ) if args.loop and args.execute and not args.no_prefetch else None
    print(f"jev_prefetch={'enabled' if prefetch is not None else 'disabled'}")

    try:
        return run_control_loop(args, api_url, api_key, model, planner, prefetch)
    finally:
        if prefetch is not None:
            prefetch.close()
        if planner is not None:
            planner.close()


def run_control_loop(args, api_url, api_key, model, planner, prefetch):
    cycle = 0
    local_executor = LocalBoardExecutor()
    exploration_executor = ExplorationExecutor()
    consecutive_errors = 0
    while True:
        cycle += 1
        if args.loop:
            print(f"cycle={cycle}")
        with timing.CycleTrace(cycle, args.timing_log, args.timing_details):
            try:
                terminal = run_decision_cycle(args, api_url, api_key, model, planner, prefetch,
                                              local_executor, exploration_executor)
                consecutive_errors = 0
            except (RuntimeError, OSError, ValueError, json.JSONDecodeError) as exc:
                timing.annotate(outcome="retry_error", error_type=type(exc).__name__)
                if prefetch is not None:
                    prefetch.discard("cycle_error")
                if not args.loop:
                    raise
                consecutive_errors += 1
                retry_base = max(args.interval, 1.0)
                retry_delay = min(retry_base * (2 ** min(consecutive_errors - 1, 3)), 30.0)
                print(
                    f"cycle_error={exc}; retrying_in={retry_delay:.1f}s",
                    file=sys.stderr,
                )
                with timing.span("loop.retry_backoff", seconds=retry_delay):
                    time.sleep(retry_delay)
                continue

            if terminal or not args.loop:
                return 0
            if args.interval > 0:
                with timing.span("loop.cooldown", seconds=args.interval):
                    time.sleep(args.interval)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        controller.release_movement_keys()
        print("control_loop_stopped: keyboard interrupt")
        raise SystemExit(130)
    except (RuntimeError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
