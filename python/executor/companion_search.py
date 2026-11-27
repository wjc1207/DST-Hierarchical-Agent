"""Local frontier/memory adapter backed by WorldGraph two-level topological routing."""
import heapq
import math
import time
from collections import OrderedDict

from common.task_timing import number
from executor.navigation_intents import WORK_SOURCES, sources_for
from executor.region_resources import room_resources
from executor.world_graph import CELL, NEIGHBORS, MAX_SOURCES, WorldGraph, cell_at


LOCAL_ROUTE_LIMIT = 80
KNOWN_ROUTE_LIMIT = 320
KNOWN_SOURCE_TTL = 900
ROAD_EXPLORE_BONUS = 12.0
DIRECTIONS = {
    "east": (1.0, 0.0),
    "west": (-1.0, 0.0),
    "north": (0.0, 1.0),
    "south": (0.0, -1.0),
    "ne": (0.70710678, 0.70710678),
    "nw": (-0.70710678, 0.70710678),
    "se": (0.70710678, -0.70710678),
    "sw": (-0.70710678, -0.70710678),
}


class ResourceSearch:
    def __init__(self, clock=time.monotonic, *, graph: WorldGraph | None = None):
        self.clock = clock
        self.graph = graph if graph is not None else WorldGraph(clock=clock)
        self.cells = self.graph.cells
        self.sources = self.graph.sources
        self.paused = {}
        self.recent_legs = OrderedDict()
        self.last_version = None
        self.position = None
        self.strategy = None
        self.inflight = None

    def observe(self, observed):
        version = observed.get("version")
        if number(version) and number(self.last_version) and version < self.last_version:
            return
        self.last_version = version
        perception = observed.get("perception") or {}
        if not perception:
            return
        size = perception.get("cell_size")
        if size != CELL:
            return
        self.graph.observe(observed)
        now = self.clock()
        point = (observed.get("buddy") or {}).get("position") or {}
        if not all(number(point.get(k)) for k in ("x", "z")):
            return
        position = (point["x"], point["z"])
        self.position = position
        seen = set()
        for row in observed.get("nearby", []):
            if not all(number(row.get(k)) for k in ("guid", "x", "z")):
                continue
            identity = row["guid"]
            seen.add(identity)
            nid = self.graph.node_at(row["x"], row["z"])
            self.sources[identity] = {
                "guid": identity,
                "prefab": row.get("prefab"),
                "product": row["product"],
                "x": row["x"],
                "z": row["z"],
                "node_id": nid,
                "seen_at": now,
                "available": True,
            }
            self.sources.move_to_end(identity)
        work_targets = observed.get("work_targets")
        for row in work_targets or []:
            if not all(number(row.get(k)) for k in ("guid", "x", "z")):
                continue
            nid = self.graph.node_at(row["x"], row["z"])
            for product in row.get("products", []):
                identity = (row["guid"], product)
                seen.add(identity)
                self.sources[identity] = {
                    "guid": row["guid"],
                    "prefab": row.get("prefab"),
                    "product": product,
                    "work": row["work"],
                    "remaining": row.get("remaining"),
                    "stage": row.get("stage"),
                    "x": row["x"],
                    "z": row["z"],
                    "node_id": nid,
                    "seen_at": now,
                    "available": True,
                }
                self.sources.move_to_end(identity)
        radius = perception.get("resource_radius", 15)
        work_complete = observed.get("work_targets_complete")
        if work_complete is None:
            work_complete = work_targets is not None and len(work_targets) < 24
        work_cutoff = 0.0
        if work_targets and not work_complete:
            dists = [r["distance"] for r in work_targets if number(r.get("distance"))]
            if not dists:
                dists = [math.hypot(r["x"] - position[0], r["z"] - position[1])
                         for r in work_targets if number(r.get("x")) and number(r.get("z"))]
            work_cutoff = max(dists, default=0.0)
        for identity, source in self.sources.items():
            if identity in seen:
                continue
            dist = math.hypot(source["x"] - position[0], source["z"] - position[1])
            if source.get("work"):
                if (work_complete and dist <= radius) or (not work_complete and work_cutoff > 0.5 and dist < work_cutoff - 0.5):
                    source["available"] = False
            else:
                if observed.get("resources_complete") is True and dist <= radius:
                    source["available"] = False
        while len(self.sources) > MAX_SOURCES:
            self.sources.popitem(last=False)
        self.paused = {k: until for k, until in self.paused.items() if until > now}

    def _matching_memory(self, product, observed, *, source_prefabs=None):
        tools = {work for item in observed.get("equipment", []) if item.get("usable")
                 for work in item.get("tools", [])}
        prefab_filter = set(source_prefabs) if source_prefabs else None
        now = self.clock()
        matches = []
        for row in self.sources.values():
            if not row["available"] or row["product"] != product:
                continue
            if row.get("work") and row["work"] not in tools:
                continue
            if now - row["seen_at"] > KNOWN_SOURCE_TTL:
                continue
            if prefab_filter is not None and row.get("prefab") not in prefab_filter:
                continue
            matches.append(row)
        return matches

    def _offered_first_legs(self, points, max_distance):
        origin = cell_at(*self.position)
        now = self.clock()
        first_legs, queue = {}, []
        for point in points:
            identity = point.get("id")
            if (not isinstance(identity, str) or identity in self.paused
                    or self.recent_legs.get((origin, identity), -math.inf) > now
                    or not all(number(point.get(k)) for k in ("x", "z"))):
                continue
            distance = math.hypot(point["x"] - self.position[0], point["z"] - self.position[1])
            cell = cell_at(point["x"], point["z"])
            if not 3 <= distance <= min(20, max_distance) or not self.cells.get(cell, {}).get("land"):
                continue
            first_legs[identity] = point
            heapq.heappush(queue, (distance, -distance, identity, cell))
        return origin, first_legs, queue

    def _step_toward_point(self, points, target_x, target_z, max_distance):
        """Route along observed grid (<=320u) toward (target_x, target_z), else pick closest safe offered leg."""
        target_cell = cell_at(target_x, target_z)
        origin, first_legs, queue = self._offered_first_legs(points, max_distance)
        if not first_legs:
            return None
        visited = {origin}
        best_cell_dist = math.inf
        best_first = None
        while queue:
            distance, first_distance, first, cell = heapq.heappop(queue)
            if cell in visited:
                continue
            visited.add(cell)
            if cell == target_cell:
                return first_legs[first]
            d_to_target = math.hypot(cell[0] * CELL - target_x, cell[1] * CELL - target_z)
            if d_to_target < best_cell_dist:
                best_cell_dist = d_to_target
                best_first = first
            if distance + CELL <= KNOWN_ROUTE_LIMIT:
                gx, gz = cell
                for dx, dz in NEIGHBORS:
                    ncell = (gx + dx, gz + dz)
                    if ncell not in visited and self.cells.get(ncell, {}).get("land"):
                        heapq.heappush(queue, (distance + CELL, first_distance, first, ncell))
        if best_first is not None:
            return first_legs[best_first]
        return min(first_legs.values(), key=lambda p: math.hypot(p["x"] - target_x, p["z"] - target_z))

    def select_waypoint(self, target_x, target_z, observed, *, max_distance=20):
        """Two-level routing to any observed land coordinate (local grid <=320u, else region-graph portal)."""
        self.observe(observed)
        self.strategy = None
        perception = observed.get("perception") or {}
        if not self.position or perception.get("cell_size") != CELL:
            return None
        points = perception.get("points", [])
        target_cell = cell_at(target_x, target_z)
        # Level 1: direct local grid Dijkstra if target cell is known land within 320u
        if self.cells.get(target_cell, {}).get("land"):
            origin, first_legs, queue = self._offered_first_legs(points, max_distance)
            visited = {origin}
            while queue:
                distance, first_distance, first, cell = heapq.heappop(queue)
                if cell in visited:
                    continue
                visited.add(cell)
                if cell == target_cell:
                    self.strategy = "waypoint"
                    return first_legs[first]
                if distance + CELL <= KNOWN_ROUTE_LIMIT:
                    gx, gz = cell
                    for dx, dz in NEIGHBORS:
                        ncell = (gx + dx, gz + dz)
                        if ncell not in visited and self.cells.get(ncell, {}).get("land"):
                            heapq.heappush(queue, (distance + CELL, first_distance, first, ncell))
        # Level 2: cross-region topological graph routing
        start_node = self.graph.node_at(*self.position)
        target_node = self.graph.nearest_node_to(target_x, target_z)
        if start_node and target_node:
            if start_node == target_node:
                leg = self._step_toward_point(points, target_x, target_z, max_distance)
                if leg is not None:
                    self.strategy = "waypoint"
                    return leg
            else:
                hop = self.graph.next_portal_toward(start_node, {target_node})
                if hop is not None:
                    portal = hop[0]
                    leg = self._step_toward_point(points, portal["x"], portal["z"], max_distance)
                    if leg is not None:
                        self.strategy = "waypoint"
                        return leg
        return None

    def select_known(self, product, observed, *, max_distance=20, source_prefabs=None):
        self.observe(observed)
        self.strategy = None
        perception = observed.get("perception") or {}
        if not self.position or perception.get("cell_size") != CELL:
            return None
        points = perception.get("points", [])
        memory = self._matching_memory(product, observed, source_prefabs=source_prefabs)
        if memory:
            local = self.route_to_frontier(points, memory, set(), max_distance, mode="known_only")
            if local is not None:
                return local
        # Level 2 macro fallback: route via region graph to a distant region with matching memory or observed product.
        start_node = self.graph.node_at(*self.position)
        if not start_node:
            return None
        target_nodes = {r["node_id"] for r in memory if r.get("node_id") and r["node_id"] != start_node}
        if not target_nodes and not source_prefabs:
            target_nodes = self.graph.product_nodes(product, exclude={start_node})
        if not target_nodes:
            return None
        hop = self.graph.next_portal_toward(start_node, target_nodes)
        if hop is None:
            return None
        leg = self._step_toward_point(points, hop[0]["x"], hop[0]["z"], max_distance)
        if leg is not None:
            self.strategy = "known_source"
        return leg

    def select(self, product, observed, *, max_distance=20, source_prefabs=None, mode="any"):
        self.observe(observed)
        self.strategy = None
        perception = observed.get("perception") or {}
        if not self.position or perception.get("cell_size") != CELL:
            return None
        tools = {work for item in observed.get("equipment", []) if item.get("usable")
                 for work in item.get("tools", [])}
        sources = {source for source in sources_for(product)
                   if source not in WORK_SOURCES or ("CHOP" if WORK_SOURCES[source][0] == "axe" else "MINE") in tools}
        if source_prefabs:
            filtered = sources.intersection(source_prefabs)
            if filtered:
                sources = filtered
        memory = ([] if mode == "frontier_only"
                  else self._matching_memory(product, observed, source_prefabs=source_prefabs))
        points = perception.get("points", [])
        leg = self.route_to_frontier(points, memory, sources, max_distance, mode=mode)
        if leg is not None or mode == "known_only":
            return leg
        # Level 2 macro fallback when local 80u frontier is exhausted (e.g. dead-end peninsula).
        start_node = self.graph.node_at(*self.position)
        if not start_node:
            return None
        prior_nodes, any_nodes = self.graph.frontier_nodes(sources=sources, exclude={start_node})
        for target_nodes, strategy in ((prior_nodes, "room_prior"), (any_nodes, "unknown_frontier")):
            if not target_nodes:
                continue
            hop = self.graph.next_portal_toward(start_node, target_nodes)
            if hop is not None:
                portal_leg = self._step_toward_point(points, hop[0]["x"], hop[0]["z"], max_distance)
                if portal_leg is not None:
                    self.strategy = strategy
                    return portal_leg
        return None

    def select_explore(self, observed, *, max_distance=20, direction=None, stop_on_products=None, stop_on_pois=None):
        self.observe(observed)
        self.strategy = None
        perception = observed.get("perception") or {}
        if not self.position or perception.get("cell_size") != CELL:
            return None
        sources = set()
        if stop_on_products:
            tools = {work for item in observed.get("equipment", []) if item.get("usable")
                     for work in item.get("tools", [])}
            for product in stop_on_products:
                for source in sources_for(product):
                    if source not in WORK_SOURCES or ("CHOP" if WORK_SOURCES[source][0] == "axe" else "MINE") in tools:
                        sources.add(source)
        poi_set = set(stop_on_pois) if stop_on_pois else None
        points = perception.get("points", [])
        leg = self.route_to_explore(points, max_distance, direction=direction, sources=sources, poi_prefabs=poi_set)
        if leg is not None:
            return leg
        # Level 2 macro fallback: escape fully-explored dead-end region via region graph.
        start_node = self.graph.node_at(*self.position)
        if not start_node:
            return None
        prior_nodes, any_nodes = self.graph.frontier_nodes(sources=sources, poi_prefabs=poi_set, exclude={start_node})
        for target_nodes in (prior_nodes, any_nodes):
            if not target_nodes:
                continue
            hop = self.graph.next_portal_toward(start_node, target_nodes)
            if hop is not None:
                portal_leg = self._step_toward_point(points, hop[0]["x"], hop[0]["z"], max_distance)
                if portal_leg is not None:
                    self.strategy = "explore_region_hop"
                    return portal_leg
        return None

    def route_to_frontier(self, points, memory, sources, max_distance, *, mode="any"):
        # Rank destinations along observed land. Only dispatch the first,
        # server-offered leg; later legs are revalidated from the new position.
        origin, first_legs, queue = self._offered_first_legs(points, max_distance)
        source_cells = {cell_at(row["x"], row["z"]) for row in memory} if mode != "frontier_only" else set()
        route_limit = KNOWN_ROUTE_LIMIT if source_cells else LOCAL_ROUTE_LIMIT
        priors = room_resources()
        visited = {origin}
        fallback = None
        prior_route = None
        while queue:
            distance, first_distance, first, cell = heapq.heappop(queue)
            if cell in visited:
                continue
            visited.add(cell)
            gx, gz = cell
            neighbors = [(gx + dx, gz + dz) for dx, dz in NEIGHBORS]
            if cell in source_cells:
                self.strategy = "known_source"
                return first_legs[first]
            if mode != "known_only" and distance <= LOCAL_ROUTE_LIMIT and any(next_cell not in self.cells for next_cell in neighbors):
                if prior_route is None and sources.intersection(priors.get(self.cells[cell].get("room_type"), ())):
                    prior_route = first
                if fallback is None:
                    fallback = first
            if distance + CELL <= route_limit:
                for next_cell in neighbors:
                    if next_cell not in visited and self.cells.get(next_cell, {}).get("land"):
                        heapq.heappush(queue, (distance + CELL, first_distance, first, next_cell))
        if mode == "known_only":
            return None
        if prior_route is not None:
            self.strategy = "room_prior"
            return first_legs[prior_route]
        if fallback is not None:
            self.strategy = "unknown_frontier"
            return first_legs[fallback]
        return None

    def route_to_explore(self, points, max_distance, *, direction=None, sources=None, poi_prefabs=None):
        origin, first_legs, queue = self._offered_first_legs(points, max_distance)
        priors = room_resources()
        visited = {origin}
        frontiers = []
        while queue:
            distance, first_distance, first, cell = heapq.heappop(queue)
            if cell in visited:
                continue
            visited.add(cell)
            gx, gz = cell
            neighbors = [(gx + dx, gz + dz) for dx, dz in NEIGHBORS]
            if any(next_cell not in self.cells for next_cell in neighbors):
                info = self.cells[cell]
                room_type = info.get("room_type")
                matches_prior = bool(
                    (sources and sources.intersection(priors.get(room_type, ())))
                    or WorldGraph.room_matches_poi(room_type, poi_prefabs)
                )
                on_road = bool(info.get("road"))
                frontiers.append((first, cell, distance, matches_prior, on_road))
            if distance + CELL <= LOCAL_ROUTE_LIMIT:
                for next_cell in neighbors:
                    if next_cell not in visited and self.cells.get(next_cell, {}).get("land"):
                        heapq.heappush(queue, (distance + CELL, first_distance, first, next_cell))
        if not frontiers:
            return None
        px, pz = self.position
        dir_vec = DIRECTIONS.get(direction)

        def score(item):
            first, cell, distance, matches_prior, on_road = item
            tier = 0 if matches_prior else 1
            if dir_vec is not None:
                cx, cz = cell[0] * CELL, cell[1] * CELL
                vx, vz = cx - px, cz - pz
                norm = math.hypot(vx, vz) or 1.0
                alignment = (vx * dir_vec[0] + vz * dir_vec[1]) / norm
                return (tier, -round(alignment, 3), distance)
            eff_dist = distance - (ROAD_EXPLORE_BONUS if on_road else 0.0)
            return (tier, eff_dist, distance)

        chosen_first, _, _, matches_prior, on_road = min(frontiers, key=score)
        if matches_prior:
            self.strategy = "explore_prior"
        elif dir_vec is not None:
            self.strategy = "explore_direction"
        elif on_road:
            self.strategy = "explore_road"
        else:
            self.strategy = "explore_frontier"
        return first_legs[chosen_first]

    def start_leg(self, task_id, args):
        origin = cell_at(*self.position) if self.position else None
        self.inflight = {"task_id": task_id, "origin_cell": origin, "strategy": self.strategy, **args}

    def result(self, status, reason):
        if self.inflight:
            identity = self.inflight["point_id"]
            until = self.clock() + 30
            if status != "completed" or reason == "search_leg_timeout":
                self.paused[identity] = until
                if len(self.paused) > 256:
                    self.paused.pop(next(iter(self.paused)))
            elif reason == "search_leg_arrived" and self.inflight["origin_cell"] is not None:
                edge = (self.inflight["origin_cell"], identity)
                self.recent_legs[edge] = until
                self.recent_legs.move_to_end(edge)
                if len(self.recent_legs) > 32:
                    self.recent_legs.popitem(last=False)
        self.inflight = None

    def summary(self):
        return {
            "observed_cells": len(self.cells),
            "remembered_sources": sum(r["available"] for r in self.sources.values()),
            "paused_points": len(self.paused),
            "sources": [
                {
                    "product": r["product"],
                    **({"prefab": r["prefab"]} if r.get("prefab") else {}),
                    "x": r["x"],
                    "z": r["z"],
                    "available_when_seen": r["available"],
                    "age_seconds": round(max(0, self.clock() - r["seen_at"]), 1),
                }
                for r in list(self.sources.values())[-8:]
            ],
        }
