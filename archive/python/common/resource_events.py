"""Resource-type sets from snapshots; entity-count changes do not trigger decisions."""
RESOURCE_TYPES = {
    "grass": "cutgrass", "sapling": "twigs", "sapling_moon": "twigs",
    "berrybush": "berries", "berrybush2": "berries",
    "berrybush_juicy": "berries_juicy", "carrot_planted": "carrot", "flower": "petals",
}

def resource_types(state: dict, resource_available) -> set[str]:
    """One entry per available product type, independent of GUID and quantity."""
    return {RESOURCE_TYPES.get(e["prefab"], e["prefab"]) for e in state.get("nearby", [])
            if e.get("prefab") and resource_available(state, e)}
