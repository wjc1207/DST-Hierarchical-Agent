"""Unit tests for incremental WorldGraph, two-level routing, long move_to, persistence, and sharing."""
import tempfile
import unittest
from pathlib import Path

from test_companion_search import board, observation, relocate, task
from executor.companion_tasks import CompanionTasks
from executor.world_graph import WorldGraph


def _chain_world_cells():
    """Build a 3-region observed land chain: R0 (gx 0..3), R1 (gx 4..9), R2 (gx 10..13), all at gz=0."""
    cells = []
    for gx in range(0, 14):
        if gx <= 3:
            nid, rt = "Make a pick:0:BGGrass", "BGGrass"
        elif gx <= 9:
            nid, rt = "Make a pick:1:Forest", "Forest"
        else:
            nid, rt = "Make a pick:2:Rockyland", "Rockyland"
        cells.append({"gx": gx, "gz": 0, "land": True, "node_id": nid, "room_type": rt})
    return cells


class WorldGraphTests(unittest.TestCase):
    def test_region_graph_edges_hops_products_and_persistence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            graph = WorldGraph(persist_root=root, clock=lambda: 100.0)
            obs = observation()
            obs["world"] = {"day": 1, "phase": "day", "world_id": "world_abc"}
            obs["perception"]["cells"] = _chain_world_cells()
            obs["work_targets"] = [
                {"guid": 77, "prefab": "rock2", "work": "MINE", "products": ["goldnugget"], "x": 48, "z": 0, "distance": 4},
            ]
            graph.observe(obs)

            self.assertEqual(len(graph.nodes), 3)
            self.assertIn("Make a pick:1:Forest", graph.edges["Make a pick:0:BGGrass"])
            self.assertIn("Make a pick:2:Rockyland", graph.edges["Make a pick:1:Forest"])
            self.assertNotIn("Make a pick:2:Rockyland", graph.edges["Make a pick:0:BGGrass"])
            self.assertEqual(
                graph.route_nodes("Make a pick:0:BGGrass", {"Make a pick:2:Rockyland"}),
                ["Make a pick:0:BGGrass", "Make a pick:1:Forest", "Make a pick:2:Rockyland"],
            )
            summary = graph.summary("Make a pick:0:BGGrass")
            hops = {r["node_id"]: r["hops"] for r in summary["regions"]}
            self.assertEqual(hops["Make a pick:0:BGGrass"], 0)
            self.assertEqual(hops["Make a pick:1:Forest"], 1)
            self.assertEqual(hops["Make a pick:2:Rockyland"], 2)
            rock_region = next(r for r in summary["regions"] if r["node_id"] == "Make a pick:2:Rockyland")
            self.assertIn("goldnugget", rock_region["products"])

            # Save and reload into a brand-new WorldGraph instance
            self.assertTrue(graph.save(force=True))
            reloaded = WorldGraph(persist_root=root, clock=lambda: 200.0)
            reloaded.reset("world_abc")
            self.assertEqual(len(reloaded.nodes), 3)
            self.assertIn("goldnugget", reloaded.nodes["Make a pick:2:Rockyland"]["products"])
            self.assertEqual(
                reloaded.route_nodes("Make a pick:0:BGGrass", {"Make a pick:2:Rockyland"}),
                ["Make a pick:0:BGGrass", "Make a pick:1:Forest", "Make a pick:2:Rockyland"],
            )

    def test_cross_region_long_distance_move_to_and_distant_known_resource(self):
        # Build a 3-region chain spanning 400 units (beyond LOCAL_ROUTE_LIMIT=80 and KNOWN_ROUTE_LIMIT=320)
        graph = WorldGraph()
        cells = []
        for gx in range(0, 105):  # x = 0 .. 416
            if gx < 35:
                nid, rt = "W:0:BGGrass", "BGGrass"
            elif gx < 70:
                nid, rt = "W:1:Forest", "Forest"
            else:
                nid, rt = "W:2:Rockyland", "Rockyland"
            cells.append({"gx": gx, "gz": 0, "land": True, "node_id": nid, "room_type": rt})
        obs = observation()
        obs["perception"]["cells"] = cells
        obs["perception"]["points"] = [
            {"id": "4:0", "x": 16, "z": 0},
            {"id": "-2:0", "x": -8, "z": 0},
        ]
        # Record a goldnugget in R2 (x=400, z=0)
        obs["work_targets"] = [
            {"guid": 900, "prefab": "rock2", "work": "MINE", "products": ["goldnugget"], "x": 400, "z": 0, "distance": 400},
        ]
        runtime = CompanionTasks(graph=graph)
        runtime.search.observe(obs)
        obs["work_targets"] = []
        obs["equipment"] = [{"guid": 1, "prefab": "pickaxe", "usable": True, "tools": ["MINE"]}]

        # 1) Long-distance move_to (x=400, z=0) routes eastward via region portal without burning blind budget
        runtime.adopt(board({"id": "go_far", "title": "Go far east", "target": {"type": "move_to", "x": 400, "z": 0}}), obs)
        selected, op, args = runtime.next_action(obs)
        self.assertEqual((selected["id"], op, args["point_id"], args["products"]), ("go_far", "search", "4:0", []))
        self.assertEqual(runtime.search.strategy, "waypoint")
        runtime.note_action(selected, op, args)
        self.assertFalse(runtime.goals.navigation)

        # 2) Distant known resource (>320u away in R2) routes via region graph without burning blind budget
        runtime2 = CompanionTasks(graph=graph)
        runtime2.adopt(board(task("gold", "goldnugget", search_budget_distance=8)), obs)
        sel2, op2, args2 = runtime2.next_action(obs)
        self.assertEqual((sel2["id"], op2, args2["point_id"]), ("gold", "search", "4:0"))
        self.assertEqual(runtime2.search.strategy, "known_source")

        # 3) Unseen distant coordinate with no known nodes returns needs_replan instead of crashing 20u limit
        empty_runtime = CompanionTasks()
        empty_obs = observation()
        empty_runtime.adopt(board({"id": "go_unseen", "title": "Go unseen", "target": {"type": "move_to", "x": 500, "z": 500}}), empty_obs)
        _, op3, info3 = empty_runtime.next_action(empty_obs)
        self.assertEqual((op3, info3["reason"]), ("needs_replan", "distant_move_to_unreachable"))

    def test_explore_escapes_enclosed_dead_end_via_region_graph_and_follows_roads(self):
        # 1) Road-following bonus when no direction is specified
        obs_road = observation()
        obs_road["perception"]["cells"] = [
            {"gx": gx, "gz": 0, "land": True, "road": gx > 0, "node_id": "W:0:BGGrass", "room_type": "BGGrass"}
            for gx in range(-3, 5)
        ]
        obs_road["perception"]["points"] = [
            {"id": "-2:0", "x": -8, "z": 0},   # closer (8u), off-road
            {"id": "3:0", "x": 12, "z": 0},    # slightly further (12u), on-road
        ]
        rt_road = CompanionTasks()
        rt_road.adopt(board({"id": "scout", "title": "Scout", "target": {"type": "explore", "cells": 12}}), obs_road)
        _, op_r, args_r = rt_road.next_action(obs_road)
        self.assertEqual((op_r, args_r["point_id"]), ("search", "3:0"))
        self.assertEqual(rt_road.search.strategy, "explore_road")

        # 2) Dead-end escape: R0 is completely surrounded by water except a bridge to R1 at gx=25 (100u > LOCAL_ROUTE_LIMIT=80)
        graph = WorldGraph()
        cells = []
        # Enclose R0 (gx -2..24, gz -1..1) with water on top/bottom/left so NO local frontier exists within 80u of origin (0,0)
        for gx in range(-3, 25):
            for gz in (-2, -1, 0, 1, 2):
                is_land = (-2 <= gx <= 24) and (gz == 0)
                cells.append({"gx": gx, "gz": gz, "land": is_land, "node_id": "W:0:Peninsula" if is_land else None, "room_type": "BGGrass"})
        # R1 starts at gx=25 (x=100) and has an open frontier at gx=26!
        cells.append({"gx": 25, "gz": 0, "land": True, "node_id": "W:1:Mainland", "room_type": "Forest"})
        obs_dead = observation()
        obs_dead["perception"]["cells"] = cells
        obs_dead["perception"]["points"] = [
            {"id": "4:0", "x": 16, "z": 0},
            {"id": "-2:0", "x": -8, "z": 0},
        ]
        rt_dead = CompanionTasks(graph=graph)
        rt_dead.adopt(board({"id": "escape", "title": "Escape peninsula", "target": {"type": "explore", "cells": 12}}), obs_dead)
        _, op_d, args_d = rt_dead.next_action(obs_dead)
        self.assertEqual((op_d, args_d["point_id"]), ("search", "4:0"))
        self.assertEqual(rt_dead.search.strategy, "explore_region_hop")

    def test_shared_world_graph_shares_regions_and_sources_across_team_members(self):
        shared = WorldGraph()
        buddy1 = CompanionTasks(graph=shared)
        buddy2 = CompanionTasks(graph=shared)

        obs1 = observation()
        obs1["perception"]["cells"] = _chain_world_cells()
        obs1["nearby"] = [{"guid": 42, "prefab": "grass", "product": "cutgrass", "x": 48, "z": 0, "distance": 2}]
        buddy1.search.observe(obs1)

        # buddy2 starts at (0, 0) with empty local nearby[], but immediately benefits from buddy1's world graph
        obs2 = observation()
        obs2["perception"]["cells"] = _chain_world_cells()[:4]
        obs2["perception"]["points"] = [{"id": "3:0", "x": 12, "z": 0}, {"id": "-2:0", "x": -8, "z": 0}]
        buddy2.adopt(board(task("grass", "cutgrass")), obs2)
        sel, op, args = buddy2.next_action(obs2)
        self.assertEqual((sel["id"], op, args["point_id"]), ("grass", "search", "3:0"))
        self.assertEqual(buddy2.search.strategy, "known_source")
        view2 = buddy2.view(obs2)
        self.assertEqual(view2["world_map"]["total_nodes"], 3)

    def test_natural_poi_memory_clustering_persistence_and_explore_stop_on_pois(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            graph = WorldGraph(persist_root=root, clock=lambda: 100.0)
            obs = observation()
            obs["world"] = {"day": 1, "phase": "day", "world_id": "world_poi"}
            obs["perception"]["cells"] = [
                {"gx": gx, "gz": 0, "land": True, "node_id": "W:0:Savanna" if gx < 0 else "W:1:PigKingdom",
                 "room_type": "Savanna" if gx < 0 else "PigKingdom"}
                for gx in range(-4, 5)
            ]
            obs["perception"]["points"] = [
                {"id": "4:0", "x": 16, "z": 0},
                {"id": "-4:0", "x": -16, "z": 0},
            ]
            # Multiple beefalo near each other cluster into one herd POI
            obs["perception"]["pois"] = [
                {"guid": 1001, "prefab": "beefalo", "kind": "herd", "x": -10, "z": 0},
                {"guid": 1002, "prefab": "beefalo", "kind": "herd", "x": -8, "z": 1},
                {"guid": 2001, "prefab": "wormhole", "kind": "portal", "x": 4, "z": 0},
            ]
            runtime = CompanionTasks(graph=graph)
            runtime.adopt(board({"id": "find_king", "title": "Find Pig King",
                                 "target": {"type": "explore", "cells": 40, "stop_on_pois": ["pigking"]}}), obs)
            # Biome prior steers toward PigKingdom (east: 4:0) over Savanna (west: -4:0)
            sel, op, args = runtime.next_action(obs)
            self.assertEqual((sel["id"], op, args["point_id"], args["products"]), ("find_king", "search", "4:0", []))
            self.assertEqual(runtime.search.strategy, "explore_prior")

            # Verify clustered beefalo + wormhole in world_map.pois and persistence
            wmap = runtime.view(obs)["world_map"]
            poi_prefabs = [p["prefab"] for p in wmap.get("pois", [])]
            self.assertEqual(poi_prefabs.count("beefalo"), 1)
            self.assertIn("wormhole", poi_prefabs)
            self.assertTrue(graph.save(force=True))
            reloaded = WorldGraph(persist_root=root, clock=lambda: 200.0)
            reloaded.reset("world_poi")
            self.assertEqual(len(reloaded.pois), 2)

            # Once pigking enters perception.pois, explore completes immediately with found_poi
            obs["version"] += 1
            obs["perception"]["pois"].append({"guid": 3001, "prefab": "pigking", "kind": "landmark", "x": 16, "z": 0})
            done_view = runtime.view(obs)
            self.assertEqual(done_view["status"], "completed")
            self.assertEqual(done_view["tasks"][0]["found_poi"]["prefab"], "pigking")
