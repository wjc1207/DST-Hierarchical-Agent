"""Local frontier/memory adapter. Server offers facts; shared tasks own budgets."""
import math
import time
from collections import OrderedDict

from common.task_timing import number
from mcp_agent.navigation_intents import sources_for


class ResourceSearch:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.cells, self.sources = OrderedDict(), OrderedDict()
        self.paused = {}
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
        if size != 4:
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
        if not self.position or perception.get("cell_size") != 4:
            return None
        # Reuse canonical resource/source relations from the existing explorer.
        sources = sources_for(product)
        memory = [r for r in self.sources.values() if r["available"] and r["product"] in sources
                  and self.clock()-r["seen_at"] <= 300]
        origin = self.position
        remembered = min(memory, key=lambda r: math.hypot(r["x"]-origin[0], r["z"]-origin[1])) if memory else None
        candidates = []
        for point in perception.get("points", []):
            if (not isinstance(point.get("id"), str) or point["id"] in self.paused
                    or not all(number(point.get(k)) for k in ("x", "z"))):
                continue
            dx, dz = point["x"]-origin[0], point["z"]-origin[1]
            distance = math.hypot(dx, dz)
            if not 3 <= distance <= min(20, max_distance):
                continue
            gx, gz = math.floor(point["x"]/4+.5), math.floor(point["z"]/4+.5)
            gain = sum((gx+x, gz+z) not in self.cells for x in range(-4, 5) for z in range(-4, 5) if x*x+z*z <= 16)
            approach = 0
            if remembered:
                approach = math.hypot(remembered["x"]-origin[0], remembered["z"]-origin[1])-math.hypot(remembered["x"]-point["x"], remembered["z"]-point["z"])
            if gain == 0 and approach <= 1:
                continue
            forward = (dx*self.heading[0]+dz*self.heading[1])/distance if self.heading else 0
            candidates.append((gain*4+approach*2+forward*3+distance*.05, point["id"], point))
        return max(candidates, key=lambda r: (r[0], r[1]))[2] if candidates else None

    def result(self, status, reason):
        if self.inflight and (status != "completed" or reason == "search_leg_timeout"):
            self.paused[self.inflight["point_id"]] = self.clock()+30
            if len(self.paused) > 256:
                self.paused.pop(next(iter(self.paused)))
        self.inflight = None

    def summary(self):
        return {"observed_cells": len(self.cells), "remembered_sources": sum(r["available"] for r in self.sources.values()),
                "paused_points": len(self.paused), "sources": [
                    {"product": r["product"], "x": r["x"], "z": r["z"], "available_when_seen": r["available"],
                     "age_seconds": round(max(0, self.clock()-r["seen_at"]), 1)}
                    for r in list(self.sources.values())[-8:]]}
