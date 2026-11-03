"""Read-only place queries and observed goal checks, shared by host and planner."""
from copy import deepcopy

PLACE_TARGETS = {"visit_building", "set_home", "return_home", "stay_home"}


def query_places(places, query="", building_id=None, offset=0, limit=12):
    if (not isinstance(query, str) or len(query) > 100
            or (building_id is not None and not isinstance(building_id, str))
            or type(offset) is not int or offset < 0
            or type(limit) is not int or not 1 <= limit <= 24):
        raise ValueError("Invalid place query (limit 1-24, offset >= 0)")
    places = places if isinstance(places, dict) else {}
    query = query.casefold().strip()
    rows = [row for row in places.get("buildings", []) if isinstance(row, dict)
            and (building_id is None or row.get("id") == building_id)
            and (not query or any(query in str(row.get(field, "")).casefold()
                                  for field in ("prefab", "id", "name", "semantic_prefab")))]
    rows.sort(key=lambda row: row.get("id", ""))
    return deepcopy({"scope": places.get("scope", "unavailable"),
                     "identity": places.get("identity", "legacy_guid"),
                     "home": places.get("home"), "anchors": places.get("anchors", []),
                     "total": len(rows), "offset": offset,
                     "next_offset": offset + limit if offset + limit < len(rows) else None,
                     "buildings": rows[offset:offset + limit]})


def target_progress(target, places):
    if not isinstance(places, dict):
        return False, "place telemetry unavailable"
    kind = target["type"]
    if kind == "visit_building":
        building = next((row for row in places.get("buildings", [])
                         if row.get("id") == target["building_id"]), None)
        if not building or building.get("status") == "missing":
            return False, "building missing or unknown"
        return bool(building.get("arrived") and building.get("status") == "observed"), ""
    home = places.get("home")
    if kind == "set_home":
        if home and home.get("anchor_id") == target["anchor_id"]:
            return True, ""
        building = next((b for b in places.get("buildings", []) if b.get("id") == target["anchor_id"]), None)
        return False, "" if building and building.get("status") != "missing" else "home building missing or unknown; query places again"
    if not home or home.get("id") != target["home_id"]:
        return False, "home unset or changed"
    return bool(home.get("arrived")), ""


PLACE_TOOL = {"type": "function", "function": {
    "name": "query_places",
    "description": "Query observed building memory (including missing/unconfirmed), current home and building-only home anchors. Building/anchor IDs are stable site IDs, not current client GUIDs; guid is the current execution binding. Anchor IDs survive movement and unambiguous replica reloads; ready means arrived and safe to set home now, otherwise travel to the building first. Arrival does not prove current light, safety or recipe technology. IDs only; no invented coordinates.",
    "parameters": {"type": "object", "properties": {
        "query": {"type": "string"}, "building_id": {"type": "string"},
        "offset": {"type": "integer", "minimum": 0},
        "limit": {"type": "integer", "minimum": 1, "maximum": 24}},
        "additionalProperties": False}}}
