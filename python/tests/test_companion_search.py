import math
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from test_companion import FakePeer, ImmediateWorker, state, TOKEN
from mcp_agent.companion_tasks import CompanionTasks, validate_board
from mcp_agent.companion_search import ResourceSearch
from mcp_agent.companion_agent import CompanionAgent
from mcp_server.companion_bridge import CompanionBridge
from mcp_server.game_bridge import BridgeError


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
        with patch("mcp_agent.companion_agent.ThreadPoolExecutor", ImmediateWorker):
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
        with patch("mcp_agent.companion_agent.ThreadPoolExecutor", ImmediateWorker):
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
