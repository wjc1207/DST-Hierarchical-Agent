"""Bounded read-only queries over the recipe facts reported by Lua."""
from copy import deepcopy


def query_recipes(catalog, query="", recipe_id=None, offset=0, limit=12, *, epoch=None):
    if (not isinstance(query, str) or len(query) > 100
            or (recipe_id is not None and not isinstance(recipe_id, str))
            or type(offset) is not int or offset < 0
            or type(limit) is not int or not 1 <= limit <= 24):
        raise ValueError("Invalid recipe query (limit 1-24, offset >= 0)")
    catalog = catalog if isinstance(catalog, dict) else {}
    query = query.casefold().strip()
    rows = [value for key, value in sorted(catalog.items())
            if isinstance(value, dict) and (recipe_id is None or key == recipe_id)
            and (not query or any(query in str(value.get(field, "")).casefold()
                                  for field in ("recipe_id", "product", "name")))]
    selected = deepcopy(rows[offset:offset + limit])
    if epoch is not None:
        for row in selected:
            row["fresh"] = row.get("fact_epoch") == epoch
            if not row["fresh"]:
                row["ready"] = False
                row["reason"] = "recipe_refresh_pending"
    return {"total": len(rows), "offset": offset,
            "next_offset": offset + limit if offset + limit < len(rows) else None,
            "recipes": selected}


RECIPE_TOOL = {"type": "function", "function": {
    "name": "query_recipes",
    "description": "Search observed game recipes by ID/product/localized name, or fetch exact recipe_id. Returns ingredients, technology, live feasibility and unsupported reasons. Paginated; not a crafting action.",
    "parameters": {"type": "object", "properties": {
        "query": {"type": "string"}, "recipe_id": {"type": "string"},
        "offset": {"type": "integer", "minimum": 0},
        "limit": {"type": "integer", "minimum": 1, "maximum": 24}},
        "additionalProperties": False}}}
