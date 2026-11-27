"""Incremental observation-only topological world graph with two-level routing and persistence."""
from __future__ import annotations

from collections import OrderedDict, deque
import json
import math
from pathlib import Path
import re
import time

from common.task_timing import number
from executor.region_resources import room_resources

CELL = 4
NEIGHBORS = ((1, 0), (-1, 0), (0, 1), (0, -1))
MAX_CELLS = 16384
MAX_SOURCES = 1024
MAX_POIS = 128
SAVE_INTERVAL_SECONDS = 15.0

POI_ROOM_KEYWORDS = {
    "pigking": ("PigKingdom", "Deciduous"),
    "pighouse": ("PigVillage", "PigKingdom", "PigTown", "Forest", "Deciduous"),
    "beefalo": ("Savanna", "BeefalowPlain", "Plain"),
    "walrus_camp": ("MacTusk", "Walrus", "Forest", "Clearing"),
    "pond": ("BGGrass", "Pond", "Frog"),
    "pond_mos": ("Marsh", "Swamp"),
    "mermhouse": ("Marsh", "Swamp", "Merm"),
    "spiderden": ("Spider", "Forest", "Rockyland", "Marsh"),
    "beehive": ("Bee", "BGGrass", "Flower"),
    "wasphive": ("KillerBee", "Bee"),
    "tallbirdnest": ("Tallbird", "Rockyland", "Rocky"),
    "catcoonden": ("Deciduous", "Birchnut"),
    "statueglommer": ("Deciduous", "PigKingdom"),
}


def cell_at(x: float, z: float) -> tuple[int, int]:
    return math.floor(x / CELL + 0.5), math.floor(z / CELL + 0.5)


def _safe_world_dir(root: Path, world_id: str) -> Path | None:
    if not isinstance(world_id, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", world_id):
        return None
    return root / "worlds" / world_id


class WorldGraph:
    """Shared per-world incremental region graph built solely from observed 4x4 cells."""

    def __init__(self, *, persist_root: Path | None = None, clock=time.monotonic):
        self.persist_root = Path(persist_root) if persist_root is not None else None
        self.clock = clock
        self.world_id: str | None = None
        self.cells: OrderedDict[tuple[int, int], dict] = OrderedDict()
        self.sources: OrderedDict = OrderedDict()
        self.pois: OrderedDict[str, dict] = OrderedDict()
        # node_id -> {room_type, cells(set), frontier(set), sum_x, sum_z, products(set), pois(set), roads(int)}
        self.nodes: dict[str, dict] = {}
        # node_id -> {neighbor_node_id: {x, z, gx, gz}}
        self.edges: dict[str, dict[str, dict]] = {}
        self._dirty = False
        self._last_save = -math.inf

    def reset(self, world_id: str | None = None) -> None:
        self.world_id = world_id
        self.cells.clear()
        self.sources.clear()
        self.pois.clear()
        self.nodes.clear()
        self.edges.clear()
        self._dirty = False
        if world_id and self.persist_root is not None:
            self._load(world_id)

    def _ensure_world(self, observed: dict) -> None:
        wid = (observed.get("world") or {}).get("world_id")
        if isinstance(wid, str) and wid and wid != self.world_id:
            if self.world_id and self._dirty:
                self.save(force=True)
            self.reset(wid)

    def _ensure_node(self, node_id: str, room_type: str | None) -> dict:
        node = self.nodes.get(node_id)
        if node is None:
            node = {
                "room_type": room_type or (node_id.rsplit(":", 1)[-1] if ":" in node_id else node_id),
                "cells": set(),
                "frontier": set(),
                "sum_x": 0.0,
                "sum_z": 0.0,
                "products": set(),
                "pois": set(),
                "roads": 0,
            }
            self.nodes[node_id] = node
            self.edges.setdefault(node_id, {})
        elif room_type and not node.get("room_type"):
            node["room_type"] = room_type
        return node

    def _update_frontier_for(self, key: tuple[int, int]) -> None:
        cell = self.cells.get(key)
        if not cell or not cell.get("land"):
            return
        node_id = cell.get("node_id")
        if not node_id or node_id not in self.nodes:
            return
        gx, gz = key
        has_unseen = any((gx + dx, gz + dz) not in self.cells for dx, dz in NEIGHBORS)
        frontier = self.nodes[node_id]["frontier"]
        if has_unseen:
            frontier.add(key)
        else:
            frontier.discard(key)

    def _add_cell(self, row: dict) -> bool:
        gx, gz, land = row.get("gx"), row.get("gz"), row.get("land")
        if type(gx) is not int or type(gz) is not int or type(land) is not bool:
            return False
        key = (gx, gz)
        existing = self.cells.get(key)
        clean = {"gx": gx, "gz": gz, "land": land}
        if row.get("room_type"):
            clean["room_type"] = row["room_type"]
        if row.get("node_id"):
            clean["node_id"] = row["node_id"]
        elif existing and existing.get("node_id"):
            clean["node_id"] = existing["node_id"]
        if row.get("road") or (existing and existing.get("road")):
            clean["road"] = True
        if row.get("tile") is not None:
            clean["tile"] = row["tile"]

        is_new = existing is None
        self.cells[key] = clean
        self.cells.move_to_end(key)
        while len(self.cells) > MAX_CELLS:
            evicted_key, evicted = self.cells.popitem(last=False)
            enid = evicted.get("node_id")
            if enid and enid in self.nodes:
                self.nodes[enid]["frontier"].discard(evicted_key)

        node_id = clean.get("node_id")
        if land and node_id:
            node = self._ensure_node(node_id, clean.get("room_type"))
            if key not in node["cells"]:
                node["cells"].add(key)
                node["sum_x"] += gx * CELL
                node["sum_z"] += gz * CELL
                if clean.get("road"):
                    node["roads"] += 1
                self._dirty = True
            for dx, dz in NEIGHBORS:
                nkey = (gx + dx, gz + dz)
                ncell = self.cells.get(nkey)
                if ncell and ncell.get("land"):
                    nnid = ncell.get("node_id")
                    if nnid and nnid != node_id:
                        self._ensure_node(nnid, ncell.get("room_type"))
                        if nnid not in self.edges[node_id]:
                            portal = {"x": (gx + nkey[0]) * CELL * 0.5, "z": (gz + nkey[1]) * CELL * 0.5, "gx": gx, "gz": gz}
                            self.edges[node_id][nnid] = portal
                            self.edges[nnid][node_id] = {**portal, "gx": nkey[0], "gz": nkey[1]}
                            self._dirty = True

        self._update_frontier_for(key)
        for dx, dz in NEIGHBORS:
            self._update_frontier_for((gx + dx, gz + dz))
        return is_new

    def observe(self, observed: dict) -> None:
        self._ensure_world(observed)
        perception = observed.get("perception") or {}
        if perception.get("cell_size") != CELL:
            return
        for row in perception.get("cells", []):
            self._add_cell(row)
        # Record observed resource products on their containing region node.
        for row in observed.get("nearby", []):
            if number(row.get("x")) and number(row.get("z")) and row.get("product"):
                nid = (self.cells.get(cell_at(row["x"], row["z"])) or {}).get("node_id")
                if nid and nid in self.nodes and row["product"] not in self.nodes[nid]["products"]:
                    self.nodes[nid]["products"].add(row["product"])
                    self._dirty = True
        for row in observed.get("work_targets") or []:
            if number(row.get("x")) and number(row.get("z")):
                nid = (self.cells.get(cell_at(row["x"], row["z"])) or {}).get("node_id")
                if nid and nid in self.nodes:
                    for product in row.get("products", []):
                        if product not in self.nodes[nid]["products"]:
                            self.nodes[nid]["products"].add(product)
                            self._dirty = True
        for row in perception.get("pois") or []:
            self._add_poi(row)
        if self._dirty and self.persist_root is not None and self.clock() - self._last_save >= SAVE_INTERVAL_SECONDS:
            self.save()

    def _add_poi(self, row: dict) -> None:
        if not isinstance(row, dict):
            return
        prefab = row.get("prefab")
        if not isinstance(prefab, str) or not prefab or not (number(row.get("x")) and number(row.get("z"))):
            return
        x, z = round(float(row["x"]), 1), round(float(row["z"]), 1)
        guid = row.get("guid")
        if prefab == "beefalo":
            # Cluster roaming herd sightings by coarse 24-unit cell so one herd does not spam POIs.
            key = f"beefalo:{int(math.floor(x / 24 + 0.5))}:{int(math.floor(z / 24 + 0.5))}"
        elif isinstance(guid, int) and guid > 0:
            key = f"poi:{guid}"
        else:
            key = f"{prefab}:{int(round(x))}:{int(round(z))}"
        nid = row.get("node_id") or self.node_at(x, z)
        entry = {
            "id": key,
            "prefab": prefab,
            "kind": row.get("kind") or "landmark",
            "x": x,
            "z": z,
            **({"node_id": nid} if nid else {}),
        }
        prev = self.pois.get(key)
        if prev != entry:
            self.pois[key] = entry
            self.pois.move_to_end(key)
            self._dirty = True
        while len(self.pois) > MAX_POIS:
            self.pois.popitem(last=False)
        if nid and nid in self.nodes and prefab not in self.nodes[nid]["pois"]:
            self.nodes[nid]["pois"].add(prefab)
            self._dirty = True

    @staticmethod
    def room_matches_poi(room_type: str | None, poi_prefabs: set[str] | None) -> bool:
        if not room_type or not poi_prefabs:
            return False
        rt_lower = room_type.lower()
        for prefab in poi_prefabs:
            for kw in POI_ROOM_KEYWORDS.get(prefab, ()):
                if kw.lower() in rt_lower:
                    return True
        return False

    def node_at(self, x: float, z: float) -> str | None:
        cell = self.cells.get(cell_at(x, z))
        if cell and cell.get("node_id"):
            return cell["node_id"]
        # Fallback to immediate 4-neighbors in case actor stands on a cell edge.
        gx, gz = cell_at(x, z)
        for dx, dz in NEIGHBORS:
            ncell = self.cells.get((gx + dx, gz + dz))
            if ncell and ncell.get("node_id"):
                return ncell["node_id"]
        return None

    def nearest_node_to(self, x: float, z: float) -> str | None:
        direct = self.node_at(x, z)
        if direct:
            return direct
        best_nid, best_d = None, math.inf
        for nid, node in self.nodes.items():
            cnt = len(node["cells"])
            if cnt <= 0:
                continue
            cx, cz = node["sum_x"] / cnt, node["sum_z"] / cnt
            d = math.hypot(cx - x, cz - z)
            if d < best_d:
                best_nid, best_d = nid, d
        return best_nid

    def route_nodes(self, start_node: str, target_nodes: set[str]) -> list[str] | None:
        """Shortest hop path in the observed region graph from start_node to any node in target_nodes."""
        if not start_node or start_node not in self.edges or not target_nodes:
            return None
        if start_node in target_nodes:
            return [start_node]
        queue = deque([(start_node, [start_node])])
        visited = {start_node}
        while queue:
            current, path = queue.popleft()
            for neighbor in self.edges.get(current, ()):
                if neighbor in visited:
                    continue
                next_path = path + [neighbor]
                if neighbor in target_nodes:
                    return next_path
                visited.add(neighbor)
                queue.append((neighbor, next_path))
        return None

    def next_portal_toward(self, start_node: str, target_nodes: set[str]) -> tuple[dict, str, int] | None:
        """Return (portal_dict, destination_node_id, hops) for the first hop toward target_nodes."""
        path = self.route_nodes(start_node, target_nodes)
        if not path or len(path) < 2:
            return None
        portal = self.edges[path[0]].get(path[1])
        if not portal:
            return None
        return portal, path[-1], len(path) - 1

    def frontier_nodes(self, *, sources: set[str] | None = None, poi_prefabs: set[str] | None = None,
                       exclude: set[str] | None = None) -> tuple[set[str], set[str]]:
        """Return (prior_matching_frontier_nodes, any_frontier_nodes)."""
        priors = room_resources()
        prior_nodes, any_nodes = set(), set()
        for nid, node in self.nodes.items():
            if exclude and nid in exclude:
                continue
            if not node["frontier"]:
                continue
            any_nodes.add(nid)
            rt = node.get("room_type")
            if (sources and sources.intersection(priors.get(rt, ()))) or self.room_matches_poi(rt, poi_prefabs):
                prior_nodes.add(nid)
        return prior_nodes, any_nodes

    def product_nodes(self, product: str, *, exclude: set[str] | None = None) -> set[str]:
        return {
            nid for nid, node in self.nodes.items()
            if product in node["products"] and (not exclude or nid not in exclude)
        }

    def summary(self, current_node: str | None = None, *, limit: int = 12) -> dict:
        hops_map: dict[str, int] = {}
        if current_node and current_node in self.edges:
            hops_map[current_node] = 0
            q = deque([current_node])
            while q:
                cur = q.popleft()
                for nb in self.edges.get(cur, ()):
                    if nb not in hops_map:
                        hops_map[nb] = hops_map[cur] + 1
                        q.append(nb)
        rows = []
        for nid, node in self.nodes.items():
            cnt = len(node["cells"])
            if cnt <= 0:
                continue
            rows.append({
                "node_id": nid,
                "room_type": node["room_type"],
                "hops": hops_map.get(nid),
                "cells": cnt,
                "has_frontier": bool(node["frontier"]),
                "roads": node["roads"],
                "products": sorted(node["products"])[:6],
                **({"pois": sorted(node["pois"])[:4]} if node["pois"] else {}),
                "center": [round(node["sum_x"] / cnt, 1), round(node["sum_z"] / cnt, 1)],
            })
        rows.sort(key=lambda r: (r["hops"] if r["hops"] is not None else 999, -r["cells"], r["node_id"]))
        poi_rows = []
        for entry in self.pois.values():
            pnid = entry.get("node_id")
            poi_rows.append({
                "prefab": entry["prefab"],
                "kind": entry["kind"],
                "x": entry["x"],
                "z": entry["z"],
                **({"node_id": pnid, "hops": hops_map.get(pnid)} if pnid else {}),
            })
        poi_rows.sort(key=lambda r: (r.get("hops") if r.get("hops") is not None else 999, r["prefab"]))
        return {
            "current_node": current_node,
            "current_room_type": (self.nodes.get(current_node) or {}).get("room_type") if current_node else None,
            "total_nodes": len(self.nodes),
            "total_cells": len(self.cells),
            "regions": rows[:limit],
            **({"pois": poi_rows[:12]} if poi_rows else {}),
        }

    def save(self, *, force: bool = False) -> bool:
        if self.persist_root is None or not self.world_id:
            return False
        if not force and not self._dirty:
            return False
        wdir = _safe_world_dir(self.persist_root, self.world_id)
        if wdir is None:
            return False
        wdir.mkdir(parents=True, exist_ok=True)
        path = wdir / "world_graph.json"
        payload = {
            "version": 1,
            "world_id": self.world_id,
            "cells": [
                {k: v for k, v in cell.items() if k in ("gx", "gz", "land", "node_id", "room_type", "road")}
                for cell in self.cells.values()
            ],
            "products": {nid: sorted(node["products"]) for nid, node in self.nodes.items() if node["products"]},
            "pois": list(self.pois.values()),
        }
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        tmp.replace(path)
        self._dirty = False
        self._last_save = self.clock()
        return True

    def _load(self, world_id: str) -> bool:
        if self.persist_root is None:
            return False
        wdir = _safe_world_dir(self.persist_root, world_id)
        if wdir is None:
            return False
        path = wdir / "world_graph.json"
        if not path.is_file():
            return False
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        if data.get("version") != 1 or data.get("world_id") != world_id:
            return False
        for row in data.get("cells") or []:
            if isinstance(row, dict):
                self._add_cell(row)
        for nid, prods in (data.get("products") or {}).items():
            if nid in self.nodes and isinstance(prods, list):
                self.nodes[nid]["products"].update(p for p in prods if isinstance(p, str))
        for poi in data.get("pois") or []:
            if isinstance(poi, dict):
                self._add_poi(poi)
        self._dirty = False
        return True
