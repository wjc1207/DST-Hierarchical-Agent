"""Klei room-type priors; never reads undiscovered map locations or entities."""
from functools import lru_cache
import json
from common.paths import DATA_DIR


@lru_cache(maxsize=1)
def room_resources():
    return json.loads((DATA_DIR / "worldgen_resources.json").read_text(encoding="utf-8"))["rooms"]


def room_type(area):
    # The topology ID contains task names (which can contain ':') and ends in room type.
    return area.get("room_type") or str(area.get("topology") or area.get("node_id") or area.get("id") or "").rsplit(":", 1)[-1]


def expected_sources(area, sources):
    return set(room_resources().get(room_type(area), ())).intersection(sources)


def knowledge_for_graph(graph):
    areas = [graph.get("current") or {}, *graph.get("routes", [])]
    return {room_type(a): room_resources().get(room_type(a), []) for a in areas if room_type(a)}
