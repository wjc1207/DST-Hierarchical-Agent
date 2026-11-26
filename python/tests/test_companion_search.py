import math
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from test_companion import FakePeer, ImmediateWorker, state, TOKEN
from executor.companion_tasks import CompanionTasks, validate_board
from executor.companion_search import ResourceSearch
from executor.team_runner import CompanionAgent
from mcp_server.companion_bridge import CompanionBridge
from transport.http_bridge import BridgeError


def relocate(observed, x=0, z=0):
    observed["buddy"].update(position={"x": x, "z": z}, owner_distance=math.hypot(x, z))
    observed["version"] = observed.get("version", 0)+1
    gx, gz = math.floor(x/4+.5), math.floor(z/4+.5)
    observed["perception"] = {"cell_size": 4, "radius": 20, "resource_radius": 15, "hazards": [],
        "cells": [{"gx": gx+dx, "gz": gz+dz, "land": True} for dx in range(-4, 5) for dz in range(-4, 5)
                  if ((gx+dx)*4-x)**2+((gz+dz)*4-z)**2 <= 400],
        "points": [{"id": f"{gx+dx}:{gz+dz}", "x": (gx+dx)*4, "z": (gz+dz)*4}
                   for dx, dz in ((4, 0), (-4, 0), (0, 4), (0, -4), (2, 0), (-2, 0), (0, 2), (0, -2))]}


def observation():
    observed = state()["companion"]
    observed["capabilities"].append("search")
    observed.update(nearby=[], resources_complete=True)
    relocate(observed)
    return observed


def task(identity="grass", prefab="cutgrass", **fields):
    target = {"type": "inventory", "prefab": prefab, "count": 3}
    if "allow_search" in fields: target["allow_search"] = fields.pop("allow_search")
    return {"id": identity, "title": identity, "target": target, **fields}


def board(*tasks):
    return validate_board({"goal": "Gather resources", "reply": "我去找资源。", "tasks": list(tasks or [task()])}, 1)


class SearchTasksTests(unittest.TestCase):
    def test_empty_local_resources_search_but_an_explicit_local_only_goal_does_not(self):
        observed = observation();runtime = CompanionTasks();runtime.adopt(board(), observed)
        selected, op, args = runtime.next_action(observed)
        self.assertEqual((selected["id"], op, args["products"]), ("grass", "search", ["cutgrass"]))
        self.assertIn(args["point_id"], {p["id"] for p in observed["perception"]["points"]})
        runtime = CompanionTasks();runtime.adopt(board(task(allow_search=False)), observed)
        self.assertEqual(runtime.next_action(observed)[1], "blocked")

    def test_work_target_does_not_make_hand_gathered_tool_materials_require_a_tool(self):
        for product, prefab, action in (("twigs", "twiggytree", "CHOP"), ("flint", "rock1", "MINE")):
            with self.subTest(product=product):
                observed = observation()
                observed["capabilities"] += ["work", "equip"]
                observed["work_targets"] = [
                    {"guid": 100, "prefab": prefab, "work": action, "products": [product], "distance": 2}
                ]
                runtime = CompanionTasks()
                runtime.adopt(board(task(product, product, focus=True)), observed)

                selected, op, args = runtime.next_action(observed)
                self.assertEqual((selected["id"], op, args["products"]), (product, "search", [product]))

                local = CompanionTasks()
                local.adopt(board(task(product, product, allow_search=False)), observed)
                self.assertEqual(local.next_action(observed)[1], "blocked")

                observed["nearby"] = [{"guid": 101, "product": product, "distance": 1}]
                self.assertEqual(runtime.next_action(observed)[1:], ("collect", {"target_guid": 101}))

    def test_real_travel_is_charged_and_failed_points_are_not_immediately_retried(self):
        clock = [0.];observed = observation();runtime = CompanionTasks(clock=lambda: clock[0]);runtime.adopt(board(), observed)
        selected, op, args = runtime.next_action(observed);runtime.note_action(selected, op, args)
        clock[0] = 2;relocate(observed, 4, 0)
        self.assertTrue(runtime.on_result(selected["id"], {"status": "failed", "reason": "action_timeout"}, observed))
        self.assertEqual(runtime.goals.records["grass"]["distance"], 4)
        self.assertEqual(runtime.goals.records["grass"]["seconds"], 2)
        next_args = runtime.next_action(observed)[2]
        self.assertNotEqual(args["point_id"], next_args["point_id"])

    def test_focus_scope_excludes_unrelated_opportunistic_resources(self):
        observed = observation()
        observed["nearby"] = [{"guid": 9, "prefab": "sapling", "product": "twigs", "distance": 1, "x": 1, "z": 0}]
        runtime = CompanionTasks();runtime.adopt(board(task(focus=True, priority=1), task("twigs", "twigs", priority=3)), observed)
        selected, op, args = runtime.next_action(observed)
        self.assertEqual((selected["id"], op, args["products"]), ("grass", "search", ["cutgrass"]))
        runtime = CompanionTasks();runtime.adopt(board(task(priority=1), task("twigs", "twigs", priority=3)), observed)
        self.assertEqual(runtime.next_action(observed)[0]["id"], "twigs")
        self.assertEqual(runtime.next_action(observed)[1], "collect")

    def test_budget_cooldown_recovers_without_forgetting_explored_cells(self):
        clock = [0.];observed = observation();runtime = CompanionTasks(clock=lambda: clock[0])
        runtime.adopt(board(task(search_budget_seconds=10)), observed)
        selected, op, args = runtime.next_action(observed);runtime.note_action(selected, op, args)
        clock[0] = 10.1;relocate(observed, 8, 0)
        runtime.on_result("grass", {"status": "completed", "reason": "search_leg_timeout"}, observed)
        self.assertEqual(runtime.next_action(observed)[1], "idle")
        before = runtime.search.summary()["observed_cells"]
        clock[0] = 26
        self.assertEqual(runtime.next_action(observed)[1], "search")
        self.assertGreaterEqual(runtime.search.summary()["observed_cells"], before)
        self.assertGreater(runtime.goals.records["grass"]["attempt"], 1)

    def test_deferred_focus_resumes_when_a_fresh_resource_is_observed(self):
        observed = observation()
        runtime = CompanionTasks()
        runtime.adopt(board(task("flint", "flint", focus=True), task()), observed)
        runtime.defer_task("flint", "no_safe_unsearched_frontier")
        observed["perception"]["points"] = []
        observed["nearby"] = [{"guid": 9, "product": "cutgrass", "distance": 1, "x": 1, "z": 0}]
        self.assertEqual(runtime.next_action(observed)[0]["id"], "grass")
        observed["nearby"].append({"guid": 10, "product": "flint", "distance": 2, "x": 2, "z": 0})
        selected, op, args = runtime.next_action(observed)
        self.assertEqual((selected["id"], op, args), ("flint", "collect", {"target_guid": 10}))

    def test_fresh_resource_releases_a_search_budget_without_waiting_for_cooldown(self):
        observed = observation();runtime = CompanionTasks();runtime.adopt(board(), observed)
        runtime.goals.suspend_search("grass")
        observed["nearby"] = [{"guid": 9, "product": "cutgrass", "distance": 1, "x": 1, "z": 0}]
        self.assertEqual(runtime.next_action(observed)[1], "collect")

    def test_known_source_then_room_prior_then_nearest_unexplored_edge(self):
        observed = observation()
        perception = observed["perception"]
        perception["cells"] = [
            {"gx": x, "gz": z, "land": True, "room_type": "BGGrass" if x < 0 else "BGDirt"}
            for x in range(-4, 3) for z in range(-3, 4)
        ]
        perception["points"] = [
            {"id": "2:0", "x": 8, "z": 0}, {"id": "-4:0", "x": -16, "z": 0},
        ]
        observed["nearby"] = [{"guid": 7, "product": "cutgrass", "x": 8, "z": 0}]
        search = ResourceSearch()
        self.assertEqual(search.select("cutgrass", observed)["id"], "2:0")
        self.assertEqual(search.strategy, "known_source")
        observed["nearby"] = []
        self.assertEqual(search.select("cutgrass", observed)["id"], "-4:0")
        self.assertEqual(search.strategy, "room_prior")
        self.assertEqual(search.select("seeds", observed)["id"], "2:0")
        self.assertEqual(search.strategy, "unknown_frontier")

    def test_mining_priors_require_usable_tool_but_loose_flint_does_not(self):
        observed = observation()
        observed["perception"]["cells"] = [
            {"gx": x, "gz": 0, "land": True, "room_type": "BGDirt" if x < 0 else "BGImpassable"}
            for x in range(-4, 3)
        ]
        observed["perception"]["points"] = [
            {"id": "2:0", "x": 8, "z": 0}, {"id": "-4:0", "x": -16, "z": 0},
        ]
        search = ResourceSearch()
        self.assertEqual(search.select("flint", observed)["id"], "2:0")
        observed["equipment"] = [{"usable": True, "tools": ["MINE"]}]
        self.assertEqual(search.select("flint", observed)["id"], "-4:0")
        observed["equipment"] = []
        observed["perception"]["cells"][0]["room_type"] = "BGGrass"
        self.assertEqual(search.select("flint", observed)["id"], "-4:0")

    def test_remembered_work_target_requires_the_matching_tool(self):
        observed = observation()
        observed["perception"]["points"] = [{"id": "4:0", "x": 16, "z": 0}]
        observed["work_targets"] = [
            {"guid": 8, "work": "MINE", "products": ["flint"], "x": 16, "z": 0},
        ]
        search = ResourceSearch()
        search.observe(observed)
        observed["work_targets"] = []
        search.select("flint", observed)
        self.assertEqual(search.strategy, "unknown_frontier")
        observed["equipment"] = [{"usable": True, "tools": ["MINE"]}]
        search.select("flint", observed)
        self.assertEqual(search.strategy, "known_source")

    def test_known_land_can_lead_back_to_a_frontier_via_a_short_leg(self):
        observed = observation()
        perception = observed["perception"]
        perception["cells"] = [
            {"gx": x, "gz": z, "land": (z == 0 and -6 <= x <= 1) or (x == -6 and 0 <= z <= 5)}
            for x in range(-8, 6) for z in range(-5, 6)
        ]
        perception["points"] = [
            {"id": "1:0", "x": 4, "z": 0},
            {"id": "-2:0", "x": -8, "z": 0},
        ]
        clock = [0.]
        search = ResourceSearch(clock=lambda: clock[0])
        selected = search.select("flint", observed)
        self.assertEqual(selected["id"], "-2:0")
        self.assertIsNone(search.select("flint", observed, max_distance=7))
        search.start_leg("flint", {"point_id": selected["id"]})
        search.result("completed", "search_leg_arrived")
        self.assertIsNone(search.select("flint", observed))  # Avoid a repeated directed leg.
        clock[0] = 31
        self.assertEqual(search.select("flint", observed)["id"], "-2:0")

        barrier = next(cell for cell in perception["cells"] if (cell["gx"], cell["gz"]) == (-4, 0))
        barrier["land"] = False
        self.assertIsNone(search.select("flint", observed))  # The known route is disconnected.

    def test_remaining_distance_budget_limits_the_next_leg(self):
        observed = observation();runtime = CompanionTasks();runtime.adopt(board(task(search_budget_distance=8)), observed)
        _, op, args = runtime.next_action(observed)
        self.assertEqual(op, "search")
        self.assertLessEqual(math.hypot(args["x"], args["z"]), 8)
        runtime.goals.records["grass"]["distance"] = 7
        self.assertEqual(runtime.next_action(observed)[1], "idle")
        self.assertEqual(runtime.goals.records["grass"]["blocked"], "search_budget_exhausted")

    def test_incomplete_or_old_observations_do_not_erase_resource_memory(self):
        observed = observation();memory = ResourceSearch()
        observed["nearby"] = [{"guid": 9, "product": "cutgrass", "distance": 1, "x": 1, "z": 0}]
        memory.observe(observed)
        observed.update(nearby=[], resources_complete=False);memory.observe(observed)
        self.assertTrue(memory.sources[9]["available"])
        old = {**observed, "version": 0, "resources_complete": True};memory.observe(old)
        self.assertTrue(memory.sources[9]["available"])
        observed["resources_complete"] = True;memory.observe(observed)
        self.assertFalse(memory.sources[9]["available"])

    def test_only_offered_search_points_and_bounded_deadlines_can_cross_the_mcp_boundary(self):
        bridge = CompanionBridge(TOKEN, clock=lambda: 0);data = state();data["companion"] = observation()
        bridge.exchange({"token": TOKEN, "session_id": "local", "sequence": 1, "state": data})
        args = {"point_id": "4:0", "x": 16, "z": 0, "products": ["cutgrass"], "timeout_seconds": 8}
        for invalid in ({**args, "x": 999}, {**args, "point_id": "unknown"}, {**args, "timeout_seconds": 30}):
            with self.assertRaises(BridgeError): bridge.action("search", bridge.observe()["observation_id"], invalid)
        self.assertEqual(bridge.action("search", bridge.observe()["observation_id"], args)["status"], "queued")


class SearchPeer(FakePeer):
    def __init__(self):
        super().__init__();self.state["companion"] = observation();self.exchange()
    async def call_tool(self, name, args):
        if name != "buddy_action" or args["op"] not in {"search", "collect"}:
            return await super().call_tool(name, args)
        self.calls.append((name, args));record = self.bridge.action(**args)
        observed = self.state["companion"]
        if args["op"] == "search":
            relocate(observed, 8, 0)  # Resource discovered before the requested endpoint.
            observed["nearby"] = [{"guid": 99+i, "prefab": "grass", "product": "cutgrass", "kind": "pick", "distance": 1,
                                    "x": 9+i, "z": 0} for i in range(3)]
            reason = "resource_found"
        else:
            row = next(r for r in observed["nearby"] if r["guid"] == args["args"]["target_guid"])
            observed["buddy"]["inventory"][row["product"]] = observed["buddy"]["inventory"].get(row["product"], 0)+1
            observed["nearby"].remove(row);observed["version"] += 1;reason = "inventory_increased"
        self.exchange([{"id": record["id"], "revision": 1, "status": "completed", "reason": reason}])
        return SimpleNamespace(is_error=False, structured_content=record)


class SearchAgentTests(unittest.IsolatedAsyncioTestCase):
    async def test_empty_area_search_discover_collect_and_return_without_per_step_model_calls(self):
        peer = SearchPeer();contexts = []
        def planner(context, *_):
            contexts.append(context)
            if len(contexts) == 1:
                result = board(task(), {"id": "back", "title": "Return", "depends_on": ["grass"], "target": {"type": "return_owner"}})
                return result
            return {"kind": "agent_reply", "outcome": "completed", "reply": "找到了草，已收集并回来。", "evidence": "背包三根草，离主人两单位。"}
        with patch("executor.team_runner.ThreadPoolExecutor", ImmediateWorker):
            agent = CompanionAgent(peer, planner, execute=True)
        try:
            for _ in range(20): await agent.cycle()
            ops = [a["op"] for name, a in peer.calls if name == "buddy_action"]
            self.assertEqual(ops, ["search", "collect", "collect", "collect", "follow"])
            self.assertEqual(len(contexts), 2)
            self.assertEqual(peer.state["companion"]["buddy"]["inventory"]["cutgrass"], 3)
            self.assertGreater(contexts[-1]["current_task_board"]["search_memory"]["observed_cells"], 0)
            self.assertTrue(any(row["finish"] for row in peer.bridge.feedback.values()))
        finally: await agent.close()

    async def test_new_message_pauses_search_accounting_without_discarding_actor_memory(self):
        peer = SearchPeer()
        with patch("executor.team_runner.ThreadPoolExecutor", ImmediateWorker):
            agent = CompanionAgent(peer, lambda *_: board(), execute=True)
        try:
            await agent.cycle();await agent.cycle()
            memory = agent.tasks.search
            self.assertTrue(agent.tasks.goals.navigation)
            peer.state["control"]["revision"] = 2;peer.state["companion"]["revision"] = 2
            peer.state["control"]["messages"].append({"id": 2, "text": "先停一下"})
            await agent.cycle()
            self.assertFalse(agent.tasks.goals.navigation)
            self.assertIsNone(agent.tasks.search.inflight)
            self.assertIs(agent.tasks.search, memory)
            self.assertGreater(len(memory.cells), 0)
        finally: await agent.close()


class SearchEnhancementTests(unittest.TestCase):
    def test_known_source_revisit_exceeds_80_unit_limit_and_does_not_burn_blind_search_budget(self):
        clock = [0.0]
        observed = observation()
        # Build a 120-unit observed land corridor eastward (gx = 0..30, gz = 0)
        observed["perception"]["cells"] = [
            {"gx": gx, "gz": gz, "land": True}
            for gx in range(-2, 32) for gz in (-1, 0, 1)
        ]
        observed["perception"]["points"] = [
            {"id": "4:0", "x": 16, "z": 0},
            {"id": "-2:0", "x": -8, "z": 0},
        ]
        runtime = CompanionTasks(clock=lambda: clock[0])
        # Remember a cutgrass at x=120, z=0 (beyond LOCAL_ROUTE_LIMIT=80, within KNOWN_ROUTE_LIMIT=320)
        runtime.search.observe(observed)
        runtime.search.sources[501] = {
            "guid": 501, "prefab": "grass", "product": "cutgrass",
            "x": 120, "z": 0, "seen_at": 0.0, "available": True,
        }
        # Advance clock past old 300s TTL (to 500s, still within 900s KNOWN_SOURCE_TTL)
        clock[0] = 500.0
        # Give a tiny blind-search distance budget (8 units) so a 16-unit leg would fail if charged to blind budget
        runtime.adopt(board(task("grass", "cutgrass", search_budget_distance=8)), observed)
        selected, op, args = runtime.next_action(observed)
        self.assertEqual((selected["id"], op, args["point_id"]), ("grass", "search", "4:0"))
        self.assertEqual(runtime.search.strategy, "known_source")
        runtime.note_action(selected, op, args)
        # Revisit must not charge blind-search budget
        self.assertFalse(runtime.goals.navigation)
        clock[0] = 504.0
        relocate(observed, 16, 0)
        runtime.on_result("grass", {"status": "completed", "reason": "search_leg_arrived"}, observed)
        self.assertEqual(runtime.goals.records["grass"]["distance"], 0.0)
        self.assertEqual(runtime.goals.records["grass"]["seconds"], 0.0)

    def test_truncated_work_targets_cleans_stale_near_trees_but_preserves_far_memory(self):
        observed = observation()
        observed["work_targets_complete"] = False
        observed["work_targets"] = [
            {"guid": 200, "prefab": "evergreen", "work": "CHOP", "products": ["log"], "distance": 4, "x": 4, "z": 0},
            {"guid": 201, "prefab": "evergreen", "work": "CHOP", "products": ["log"], "distance": 12, "x": 12, "z": 0},
        ]
        memory = ResourceSearch()
        memory.observe(observed)
        # Simulate dense forest: 24 trees reported up to distance 10, guid=200 (at dist 4) is gone, guid=201 (at dist 12) is outside cutoff
        observed["version"] += 1
        observed["work_targets"] = [
            {"guid": 300 + i, "prefab": "evergreen", "work": "CHOP", "products": ["log"],
             "distance": 6.0 + (i % 5), "x": 6.0 + (i % 5), "z": 0}
            for i in range(24)
        ]
        memory.observe(observed)
        self.assertFalse(memory.sources[(200, "log")]["available"])
        self.assertTrue(memory.sources[(201, "log")]["available"])

    def test_inventory_default_heuristics_and_source_prefabs_filter(self):
        observed = observation()
        observed["capabilities"] += ["work", "equip"]
        observed["equipment"] = [{"guid": 50, "prefab": "axe", "usable": True, "equipped": True, "tools": ["CHOP"]}]
        observed["places"] = {"home": {"id": "home:1", "x": 0, "z": 0}}
        observed["work_targets"] = [
            # Inside home protection radius (<=10)
            {"guid": 10, "prefab": "evergreen", "work": "CHOP", "products": ["log"], "remaining": 15, "stage": 3, "distance": 3, "x": 3, "z": 0},
            # Outside home protection radius (>10): small vs tall
            {"guid": 11, "prefab": "deciduoustree", "work": "CHOP", "products": ["log"], "remaining": 5, "stage": 1, "distance": 11, "x": 11, "z": 0},
            {"guid": 12, "prefab": "deciduoustree", "work": "CHOP", "products": ["log"], "remaining": 15, "stage": 3, "distance": 13, "x": 13, "z": 0},
        ]
        # 1) Default heuristic (no extra LLM params): skips home tree (10) and prefers tall tree (12) over small tree (11)
        runtime = CompanionTasks()
        runtime.adopt(board(task("wood", "log")), observed)
        selected, op, args = runtime.next_action(observed)
        self.assertEqual((selected["id"], op, args), ("wood", "work", {"target_guid": 12, "work": "CHOP"}))

        # 2) source_prefabs=['evergreen'] filters out deciduoustree; if no evergreen exists, search omits excluded product
        observed_only_birch = observation()
        observed_only_birch["capabilities"] += ["work", "equip"]
        observed_only_birch["equipment"] = [{"guid": 50, "prefab": "axe", "usable": True, "equipped": True, "tools": ["CHOP"]}]
        observed_only_birch["work_targets"] = [
            {"guid": 12, "prefab": "deciduoustree", "work": "CHOP", "products": ["log"], "remaining": 15, "stage": 3, "distance": 6, "x": 6, "z": 0},
        ]
        t_ev = {"id": "ev_only", "title": "Only evergreen", "target": {"type": "inventory", "prefab": "log", "count": 4, "source_prefabs": ["evergreen"]}}
        runtime2 = CompanionTasks()
        runtime2.adopt(board(t_ev), observed_only_birch)
        _, op2, args2 = runtime2.next_action(observed_only_birch)
        self.assertEqual((op2, args2["products"]), ("search", []))

        # 3) Dropped pickup log on the ground is still collected even when source_prefabs=['deciduoustree']
        observed["nearby"] = [{"guid": 99, "prefab": "log", "product": "log", "kind": "pickup", "distance": 2, "x": 2, "z": 0}]
        t_birch = {"id": "birch", "title": "Chop birch", "target": {"type": "inventory", "prefab": "log", "count": 4, "source_prefabs": ["deciduoustree"]}}
        runtime3 = CompanionTasks()
        runtime3.adopt(board(t_birch), observed)
        self.assertEqual(runtime3.next_action(observed)[1:], ("collect", {"target_guid": 99}))

    def test_standalone_explore_task_direction_prior_progress_and_stop_on_products(self):
        observed = observation()
        observed["perception"]["cells"] = [
            {"gx": gx, "gz": gz, "land": True, "room_type": "BGGrass" if gx < 0 else "BGDirt"}
            for gx in range(-4, 5) for gz in range(-2, 3)
        ]
        observed["perception"]["points"] = [
            {"id": "4:0", "x": 16, "z": 0},
            {"id": "-4:0", "x": -16, "z": 0},
        ]
        # 1) Direction bias: east picks 4:0
        runtime = CompanionTasks()
        runtime.adopt(board({"id": "scout_e", "title": "Scout east", "target": {"type": "explore", "cells": 8, "direction": "east"}}), observed)
        selected, op, args = runtime.next_action(observed)
        self.assertEqual((selected["id"], op, args["point_id"], args["products"]), ("scout_e", "search", "4:0", []))
        self.assertEqual(runtime.search.strategy, "explore_direction")

        # Verify CompanionBridge accepts empty products list for pure explore
        bridge = CompanionBridge(TOKEN, clock=lambda: 0)
        data = state()
        data["companion"] = observed
        bridge.exchange({"token": TOKEN, "session_id": "local", "sequence": 1, "state": data})
        self.assertEqual(bridge.action("search", bridge.observe()["observation_id"], args)["status"], "queued")

        # 2) stop_on_products automatically routes via room_resources prior without needing room_types
        runtime2 = CompanionTasks()
        runtime2.adopt(board({"id": "scout_grass", "title": "Find grass", "target": {"type": "explore", "cells": 8, "stop_on_products": ["cutgrass"]}}), observed)
        _, op2, args2 = runtime2.next_action(observed)
        self.assertEqual((op2, args2["point_id"], args2["products"]), ("search", "-4:0", ["cutgrass"]))
        self.assertEqual(runtime2.search.strategy, "explore_prior")

        # 3) Cell progress completion
        relocate(observed, 16, 0)
        self.assertEqual(runtime.view(observed)["status"], "completed")

        # 4) stop_on_products completes immediately when goldnugget rock appears
        observed2 = observation()
        runtime3 = CompanionTasks()
        runtime3.adopt(board({"id": "find_gold", "title": "Find gold", "target": {"type": "explore", "cells": 40, "stop_on_products": ["goldnugget"]}}), observed2)
        _, op3, args3 = runtime3.next_action(observed2)
        self.assertEqual((op3, args3["products"]), ("search", ["goldnugget"]))
        observed2["version"] += 1
        observed2["work_targets"] = [{"guid": 88, "prefab": "rock2", "work": "MINE", "products": ["rocks", "flint", "goldnugget"], "distance": 5, "x": 5, "z": 0}]
        view3 = runtime3.view(observed2)
        self.assertEqual(view3["status"], "completed")
        self.assertEqual(view3["tasks"][0].get("found_product"), "goldnugget")
