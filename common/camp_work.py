"""Validated task targets and read-only queries for camp interactions."""
from collections import Counter
from copy import deepcopy
import re

STORAGE_TARGETS = {"inspect_storage", "deposit", "withdraw"}
CAMP_TARGETS = STORAGE_TARGETS | {"cook_meal", "ignite_tree"}
CAMP_ACTIONS = {"open_container", "store_item", "take_item", "start_meal", "harvest_meal", "ignite_tree"}
SAFE_MEALS = {"meatballs", "ratatouille"}


def prefab(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-z0-9_]{1,60}", value):
        raise ValueError("Expected a canonical item prefab")
    return value


def validate_target(target):
    kind = target["type"]
    if kind == "ignite_tree":
        guid = target.get("tree_guid")
        if type(guid) is not int or not 0 < guid <= 2**53 - 1:
            raise ValueError("Ignition requires an observed tree_guid")
        return {"tree_guid": guid}
    building_id = target.get("building_id")
    if not isinstance(building_id, str) or not re.fullmatch(r"building:[0-9]{1,30}", building_id):
        raise ValueError("Camp interaction requires an observed building_id")
    clean = {"building_id": building_id}
    if kind in {"deposit", "withdraw"}:
        count = target.get("count")
        if type(count) is not int or not 1 <= count <= 40:
            raise ValueError("Storage transfer count must be 1-40")
        clean.update(prefab=prefab(target.get("prefab")), count=count)
    elif kind == "cook_meal":
        product = prefab(target.get("product"))
        if product not in SAFE_MEALS:
            raise ValueError("Unsupported meal product; query_meals")
        ingredients = target.get("ingredients")
        if not isinstance(ingredients, list) or len(ingredients) != 4:
            raise ValueError("A meal requires exactly four ingredient prefabs")
        clean.update(product=product, ingredients=sorted(prefab(item) for item in ingredients))
    return clean


def fixture(state, building_id):
    return next((row for row in (state.get("camp_work") or {}).get("fixtures", [])
                 if row["building_id"] == building_id), None)


def recipe_option(state, target):
    return next((row for row in (state.get("camp_work") or {}).get("meals", [])
                 if row["product"] == target["product"] and sorted(row["ingredients"]) == target["ingredients"]), None)


def target_error(target, state):
    work = state.get("camp_work") or {}
    if work.get("available") is not True:
        return "camp interaction telemetry unavailable; update the mod"
    kind = target["type"]
    if kind == "ignite_tree":
        return ""
    expected = "cookpot" if kind == "cook_meal" else "treasurechest"
    found = next((row for row in (state.get("places") or {}).get("buildings", [])
                  if row.get("id") == target["building_id"]), None)
    if not found or found.get("prefab") != expected or found.get("status") == "missing":
        return f"target requires a known {expected}; query_places"
    if (fixture(state, target["building_id"]) or {}).get("phase") == "burnt":
        return "camp building burnt"
    if kind == "cook_meal" and not recipe_option(state, target):
        return "meal combination not verified; query_meals"
    return ""


def loaded(row):
    return Counter(item["prefab"] for item in row.get("items", []) for _ in range(item["count"]))


def query_storage(work, building_id=None):
    if building_id is not None and (not isinstance(building_id, str)
            or not re.fullmatch(r"building:[0-9]{1,30}", building_id)):
        raise ValueError("Invalid building_id")
    return {"fixtures": [deepcopy(row) for row in work.get("fixtures", [])
                         if building_id is None or row["building_id"] == building_id],
            "note": "Only contents_known=true is a current slot observation. An unlisted, closed or busy container has unknown contents."}


def query_meals(work, product=None):
    if product is not None:
        prefab(product)
    return {"meals": [deepcopy(row) for row in work.get("meals", [])
                      if product is None or row["product"] == product]}


def _tool(name, description, field):
    return {"type": "function", "function": {"name": name, "description": description,
            "parameters": {"type": "object", "properties": {field: {"type": "string"}},
                           "additionalProperties": False}}}


STORAGE_TOOL = _tool("query_storage", "Read current observed camp containers. Open a chest to inspect its slots; unknown is not empty.", "building_id")
MEALS_TOOL = _tool("query_meals", "List verified four-ingredient Crock Pot combinations for supported dishes. Separate from construction recipes.", "product")
