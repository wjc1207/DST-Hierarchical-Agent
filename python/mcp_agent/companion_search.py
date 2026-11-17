"""Local frontier/memory adapter. Server offers facts; shared tasks own budgets."""
import heapq
import math
import time
from collections import OrderedDict

from common.task_timing import number
from mcp_agent.navigation_intents import WORK_SOURCES, sources_for
from mcp_agent.region_resources import room_resources


CELL = 4
NEIGHBORS = ((1, 0), (-1, 0), (0, 1), (0, -1))
LOCAL_ROUTE_LIMIT = 80


def cell_at(x, z):
    return math.floor(x/CELL+.5), math.floor(z/CELL+.5)


class ResourceSearch:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.cells, self.sources = OrderedDict(), OrderedDict()
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
        now = self.clock()
        for row in perception.get("cells", []):
            if type(row.get("gx")) is int and type(row.get("gz")) is int and type(row.get("land")) is bool:
                identity = (row["gx"], row["gz"])
                self.cells[identity] = dict(row)
                self.cells.move_to_end(identity)
        while len(self.cells) > 8192:
            self.cells.popitem(last=False)
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
            self.sources[identity] = {"guid": identity, "product": row["product"], "x": row["x"], "z": row["z"],
                                      "seen_at": now, "available": True}
            self.sources.move_to_end(identity)
        work_targets = observed.get("work_targets")
        for row in work_targets or []:
            if not all(number(row.get(k)) for k in ("guid", "x", "z")):
                continue
            for product in row.get("products", []):
                identity = (row["guid"], product)
                seen.add(identity)
                self.sources[identity] = {"guid": row["guid"], "product": product,
                                          "work": row["work"], "x": row["x"], "z": row["z"],
                                          "seen_at": now, "available": True}
                self.sources.move_to_end(identity)
        radius = perception.get("resource_radius", 15)
        for identity, source in self.sources.items():
            complete = (work_targets is not None and len(work_targets) < 24 if source.get("work")
                        else observed.get("resources_complete") is True)
            if (identity not in seen and complete
                    and math.hypot(source["x"]-position[0], source["z"]-position[1]) <= radius):
                source["available"] = False
        while len(self.sources) > 256:
            self.sources.popitem(last=False)
        self.paused = {k: until for k, until in self.paused.items() if until > now}

    def select(self, product, observed, *, max_distance=20):
        self.observe(observed)
        self.strategy = None
        perception = observed.get("perception") or {}
        if not self.position or perception.get("cell_size") != CELL:
            return None
        tools = {work for item in observed.get("equipment", []) if item.get("usable")
                 for work in item.get("tools", [])}
        sources = {source for source in sources_for(product)
                   if source not in WORK_SOURCES or ("CHOP" if WORK_SOURCES[source][0] == "axe" else "MINE") in tools}
        memory = [r for r in self.sources.values() if r["available"] and r["product"] == product
                  and (not r.get("work") or r["work"] in tools) and self.clock()-r["seen_at"] <= 300]
        return self.route_to_frontier(perception.get("points", []), memory, sources, max_distance)

    def route_to_frontier(self, points, memory, sources, max_distance):
        # Rank destinations along observed land. Only dispatch the first,
        # server-offered leg; later legs are revalidated from the new position.
        origin = cell_at(*self.position)
        now = self.clock()
        first_legs, queue = {}, []
        for point in points:
            identity = point.get("id")
            if (not isinstance(identity, str) or identity in self.paused
                    or self.recent_legs.get((origin, identity), -math.inf) > now
                    or not all(number(point.get(k)) for k in ("x", "z"))):
                continue
            distance = math.hypot(point["x"]-self.position[0], point["z"]-self.position[1])
            cell = cell_at(point["x"], point["z"])
            if not 3 <= distance <= min(20, max_distance) or not self.cells.get(cell, {}).get("land"):
                continue
            first_legs[identity] = point
            heapq.heappush(queue, (distance, -distance, identity, cell))
        source_cells = {cell_at(row["x"], row["z"]) for row in memory}
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
            neighbors = [(gx+dx, gz+dz) for dx, dz in NEIGHBORS]
            if cell in source_cells:
                self.strategy = "known_source"
                return first_legs[first]
            if any(next_cell not in self.cells for next_cell in neighbors):
                if prior_route is None and sources.intersection(priors.get(self.cells[cell].get("room_type"), ())):
                    prior_route = first
                if fallback is None:
                    fallback = first
            if distance + CELL <= LOCAL_ROUTE_LIMIT:
                for next_cell in neighbors:
                    if next_cell not in visited and self.cells.get(next_cell, {}).get("land"):
                        heapq.heappush(queue, (distance+CELL, first_distance, first, next_cell))
        if prior_route is not None:
            self.strategy = "room_prior"
            return first_legs[prior_route]
        if fallback is not None:
            self.strategy = "unknown_frontier"
            return first_legs[fallback]
        return None

    def start_leg(self, task_id, args):
        origin = cell_at(*self.position) if self.position else None
        self.inflight = {"task_id": task_id, "origin_cell": origin, **args}

    def result(self, status, reason):
        if self.inflight:
            identity = self.inflight["point_id"]
            until = self.clock()+30
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
        return {"observed_cells": len(self.cells), "remembered_sources": sum(r["available"] for r in self.sources.values()),
                "paused_points": len(self.paused), "sources": [
                    {"product": r["product"], "x": r["x"], "z": r["z"], "available_when_seen": r["available"],
                     "age_seconds": round(max(0, self.clock()-r["seen_at"]), 1)}
                    for r in list(self.sources.values())[-8:]]}
