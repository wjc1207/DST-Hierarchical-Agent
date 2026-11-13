"""Local frontier/memory adapter. Server offers facts; shared tasks own budgets."""
import heapq
import math
import time
from collections import OrderedDict

from common.task_timing import number
from mcp_agent.navigation_intents import sources_for


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
        self.position = self.heading = None
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
        if self.position:
            dx, dz = position[0]-self.position[0], position[1]-self.position[1]
            length = math.hypot(dx, dz)
            if length >= 1:
                self.heading = (dx/length, dz/length)
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
        radius = perception.get("resource_radius", 15)
        for identity, source in self.sources.items():
            if (identity not in seen and observed.get("resources_complete") is True
                    and math.hypot(source["x"]-position[0], source["z"]-position[1]) <= radius):
                source["available"] = False
        while len(self.sources) > 256:
            self.sources.popitem(last=False)
        self.paused = {k: until for k, until in self.paused.items() if until > now}

    def select(self, product, observed, *, max_distance=20):
        self.observe(observed)
        perception = observed.get("perception") or {}
        if not self.position or perception.get("cell_size") != CELL:
            return None
        # Reuse canonical resource/source relations from the existing explorer.
        sources = sources_for(product)
        memory = [r for r in self.sources.values() if r["available"] and r["product"] in sources
                  and self.clock()-r["seen_at"] <= 300]
        origin = self.position
        remembered = min(memory, key=lambda r: math.hypot(r["x"]-origin[0], r["z"]-origin[1])) if memory else None
        candidates, transit = [], []
        for point in perception.get("points", []):
            if (not isinstance(point.get("id"), str) or point["id"] in self.paused
                    or not all(number(point.get(k)) for k in ("x", "z"))):
                continue
            dx, dz = point["x"]-origin[0], point["z"]-origin[1]
            distance = math.hypot(dx, dz)
            if not 3 <= distance <= min(20, max_distance):
                continue
            gx, gz = cell_at(point["x"], point["z"])
            gain = sum((gx+x, gz+z) not in self.cells for x in range(-4, 5) for z in range(-4, 5) if x*x+z*z <= 16)
            approach = 0
            if remembered:
                approach = math.hypot(remembered["x"]-origin[0], remembered["z"]-origin[1])-math.hypot(remembered["x"]-point["x"], remembered["z"]-point["z"])
            if gain == 0 and approach <= 1:
                transit.append((distance, point))
                continue
            forward = (dx*self.heading[0]+dz*self.heading[1])/distance if self.heading else 0
            candidates.append((gain*4+approach*2+forward*3+distance*.05, point["id"], point))
        if candidates:
            return max(candidates, key=lambda r: (r[0], r[1]))[2]
        return self.route_to_frontier(transit, memory)

    def route_to_frontier(self, candidates, sources):
        # Only the first leg is dispatched. Later legs must be offered again
        # by the server after moving, so this map can guide us around a bend.
        origin = cell_at(*self.position)
        now = self.clock()
        first_legs, queue = {}, []
        for distance, point in candidates:
            identity = point["id"]
            cell = cell_at(point["x"], point["z"])
            if self.recent_legs.get((origin, identity), -math.inf) > now or not self.cells.get(cell, {}).get("land"):
                continue
            first_legs[identity] = point
            heapq.heappush(queue, (distance, identity, cell))
        source_cells = {cell_at(row["x"], row["z"]) for row in sources}
        visited = {origin}
        while queue:
            distance, first, cell = heapq.heappop(queue)
            if cell in visited:
                continue
            visited.add(cell)
            gx, gz = cell
            neighbors = [(gx+dx, gz+dz) for dx, dz in NEIGHBORS]
            if cell in source_cells or any(next_cell not in self.cells for next_cell in neighbors):
                return first_legs[first]
            if distance + CELL <= LOCAL_ROUTE_LIMIT:
                for next_cell in neighbors:
                    if next_cell not in visited and self.cells[next_cell]["land"]:
                        heapq.heappush(queue, (distance+CELL, first, next_cell))
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
