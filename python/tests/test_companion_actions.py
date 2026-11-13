import asyncio
import copy
import json
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from test_companion import state, TOKEN, FakePeer, ImmediateWorker
from mcp_agent.companion_agent import CompanionAgent
from mcp_agent.companion_tasks import CompanionTasks, validate_board
from mcp_agent.companion_planner import validate_plan, request_plan, CompanionPlanner
from mcp_agent import planning_core
from mcp_server.companion_bridge import CompanionBridge
from mcp_server.companion_server import create_server
from mcp_server.game_bridge import BridgeError


def recipe(identity="axe", *, placement=False, ready=True, count=1):
    return {"recipe_id": identity, "product": identity, "supported": True, "ready": ready, "placement": placement,
            "output_count": count, "ingredients": [], "reason": "ready" if ready else "ingredients_missing"}


def snapshot():
    observed = state()["companion"]
    observed["capabilities"] += ["recipes", "equip", "work", "craft", "place"]
    observed.update(recipes={"axe": recipe(), "researchlab": recipe("researchlab", placement=True)}, equipment=[], structures=[],
                    work_targets=[{"guid": 100, "prefab": "evergreen", "work": "CHOP", "products": ["log"], "remaining": 2, "distance": 2}])
    return observed


def board(*targets):
    return validate_board({"goal": "Follow request", "reply": "开始执行。", "tasks": [
        {"id": f"t{i}", "title": f"Task {i}", "target": target, "depends_on": [f"t{i-1}"] if i else []}
        for i, target in enumerate(targets)]}, 1)


class NativeBridgeTests(unittest.TestCase):
    def setUp(self):
        self.bridge = CompanionBridge(TOKEN, clock=lambda: 0)
        self.state = state();self.state["companion"] = snapshot();self.seq = 0;self.exchange()
    def exchange(self, results=None):
        self.seq += 1
        return self.bridge.exchange({"token": TOKEN, "session_id": "local", "sequence": self.seq,
                                     "state": self.state, "results": results or []})
    def action(self, op, args):
        return self.bridge.action(op, self.bridge.observe()["observation_id"], args)

    def test_work_and_equipment_must_be_offered_by_this_actor(self):
        with self.assertRaises(BridgeError): self.action("work", {"target_guid": 5, "work": "CHOP"})
        with self.assertRaises(BridgeError): self.action("work", {"target_guid": 100, "work": "MINE"})
        with self.assertRaises(BridgeError): self.action("equip", {"item_guid": 200})
        result = self.action("work", {"target_guid": 100, "work": "CHOP"})
        self.assertEqual(result["status"], "queued")

    def test_crafting_and_placement_are_separate_and_recheck_live_readiness(self):
        with self.assertRaises(BridgeError): self.action("craft", {"recipe_id": "researchlab"})
        with self.assertRaises(BridgeError): self.action("place", {"recipe_id": "researchlab", "x": float("nan"), "z": 0})
        self.state["companion"]["recipes"]["axe"]["ready"] = False;self.exchange()
        with self.assertRaises(BridgeError): self.action("craft", {"recipe_id": "axe"})
        self.assertEqual(self.action("place", {"recipe_id": "researchlab", "x": 0, "z": 0})["op"], "place")

    def test_query_data_survives_the_native_result_and_an_expired_query_releases_the_queue(self):
        result = self.action("recipes", {"recipe_id": "axe"})
        self.exchange([{"id": result["id"], "status": "completed", "revision": 1, "data": {"recipes": [recipe()], "total": 1}}])
        self.assertEqual(self.bridge.result(result["id"])["data"]["recipes"][0]["product"], "axe")
        another = self.action("recipes", {"query": "boards"});self.bridge.expire_query(another["id"])
        self.assertEqual(self.bridge.result(another["id"])["reason"], "recipe_query_timeout")
        self.assertIsNone(self.bridge.active)

    def test_old_servers_cannot_receive_new_actions_and_empty_lua_catalogues_normalize(self):
        self.state["companion"].update(capabilities=["collect"], recipes=[]);self.exchange()
        self.assertEqual(self.bridge.observe()["state"]["companion"]["recipes"], {})
        with self.assertRaisesRegex(BridgeError, "update"):
            self.action("craft", {"recipe_id": "axe"})


class NativeTaskTests(unittest.TestCase):
    def test_log_goal_equips_then_chops_then_collects_before_completion(self):
        observed = snapshot();observed["equipment"] = [{"guid": 80, "prefab": "axe", "tools": ["CHOP"], "usable": True, "equipped": False}]
        runtime = CompanionTasks();runtime.adopt(board({"type": "inventory", "prefab": "log", "count": 2}), observed)
        self.assertEqual(runtime.next_action(observed)[1:], ("equip", {"item_guid": 80}))
        observed["equipment"][0]["equipped"] = True
        self.assertEqual(runtime.next_action(observed)[1:], ("work", {"target_guid": 100, "work": "CHOP"}))
        observed["nearby"].append({"guid": 101, "product": "log", "distance": 1})
        self.assertEqual(runtime.next_action(observed)[1], "collect")
        observed["buddy"]["inventory"]["log"] = 2
        self.assertEqual(runtime.next_action(observed)[1], "completed")

    def test_missing_tools_and_materials_request_prerequisite_planning(self):
        observed = snapshot();runtime = CompanionTasks()
        runtime.adopt(board({"type": "inventory", "prefab": "log", "count": 1}), observed)
        self.assertEqual(runtime.next_action(observed)[1:], ("needs_replan", {"reason": "tool_required:CHOP"}))
        runtime = CompanionTasks();observed["recipes"]["axe"]["ready"] = False
        runtime.adopt(board({"type": "inventory", "prefab": "axe", "count": 1, "recipe_id": "axe"}), observed)
        self.assertEqual(runtime.next_action(observed)[1], "needs_replan")

    def test_crafting_uses_the_actual_output_count_and_requires_inventory_evidence(self):
        observed = snapshot();observed["recipes"]["rope"] = recipe("rope", count=3)
        runtime = CompanionTasks();runtime.adopt(board({"type": "inventory", "prefab": "rope", "recipe_id": "rope", "count": 3}), observed)
        self.assertEqual(runtime.next_action(observed)[1:], ("craft", {"recipe_id": "rope"}))
        observed["buddy"]["inventory"]["rope"] = 3
        self.assertEqual(runtime.next_action(observed)[1], "completed")

    def test_structure_completion_is_scoped_to_the_requested_area(self):
        observed = snapshot();runtime = CompanionTasks()
        target = {"type": "structure", "prefab": "researchlab", "recipe_id": "researchlab", "x": 0, "z": 0, "radius": 6}
        observed["structures"] = [{"guid": 2, "prefab": "researchlab", "x": 15, "z": 0}]
        runtime.adopt(board(target), observed)
        self.assertEqual(runtime.next_action(observed)[1], "place")
        observed["structures"].append({"guid": 3, "prefab": "researchlab", "x": 2, "z": 0})
        self.assertEqual(runtime.next_action(observed)[1], "completed")

    def test_planner_rejects_invented_recipe_products_even_if_the_recipe_exists(self):
        context = {"interaction": {"mode": "instruction"}, "observation": snapshot()}
        invalid = board({"type": "inventory", "prefab": "goldnugget", "recipe_id": "axe", "count": 1})
        with self.assertRaisesRegex(ValueError, "matching queried"):
            validate_plan(invalid, context)

    def test_model_recipe_tool_can_expand_beyond_the_initial_observation(self):
        context = {"interaction": {"mode": "instruction"}, "observation": snapshot()}
        result_board = board({"type": "inventory", "prefab": "boards", "recipe_id": "boards", "count": 1})
        messages = [{"choices": [{"finish_reason": "tool_calls", "message": {"tool_calls": [
            {"id": "q", "type": "function", "function": {"name": "query_recipes", "arguments": '{"recipe_id":"boards"}'}}]}}]},
            {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(result_board)}}]}]
        class Response:
            def __enter__(self): return self
            def __exit__(self, *_): pass
            def read(self): return json.dumps(messages.pop(0)).encode()
        with patch.object(planning_core.urllib.request, "urlopen", side_effect=lambda *_a, **_k: Response()):
            result = request_plan("https://offline.invalid", "fake", "fake", context, [], threading.Event(),
                                  recipe_query=lambda *_: {"recipes": [recipe("boards")], "total": 1})
        self.assertEqual(result["tasks"][0]["target"]["recipe_id"], "boards")


class NativePeer(FakePeer):
    def __init__(self):
        super().__init__();self.state["companion"] = snapshot();self.exchange()
    async def call_tool(self, name, args):
        if name != "buddy_action" or args["op"] not in {"craft", "equip", "work", "collect", "place"}:
            return await super().call_tool(name, args)
        self.calls.append((name, args))
        record = self.bridge.action(**args);observed = self.state["companion"];inv = observed["buddy"]["inventory"]
        op, action = args["op"], args.get("args") or {}
        if op == "craft":
            inv["axe"] = inv.get("axe", 0)+1
            observed["equipment"] = [{"guid": 80, "prefab": "axe", "tools": ["CHOP"], "usable": True, "equipped": False}]
        elif op == "equip": observed["equipment"][0]["equipped"] = True
        elif op == "work":
            observed["work_targets"][0]["remaining"] -= 1
            if observed["work_targets"][0]["remaining"] == 0:
                observed["work_targets"] = []
                observed["nearby"].extend({"guid": 101+i, "product": "log", "kind": "pickup", "distance": 1} for i in range(2))
        elif op == "collect":
            row = next(r for r in observed["nearby"] if r["guid"] == action["target_guid"])
            inv[row["product"]] = inv.get(row["product"], 0)+1
            if row.get("kind") == "pickup": observed["nearby"].remove(row)
        elif op == "place": observed["structures"].append({"guid": 103, "prefab": action["recipe_id"], "x": action["x"]+2, "z": action["z"]})
        self.exchange([{"id": record["id"], "revision": 1, "status": "completed"}])
        return SimpleNamespace(is_error=False, structured_content=record)


class NativeAgentTests(unittest.IsolatedAsyncioTestCase):
    async def test_planning_worker_queries_the_real_mcp_without_blocking_observation(self):
        from mcp import Client
        from mcp_agent.companion_team_agent import ActorClient
        from test_companion_team import single_member_state
        from mcp_server.companion_team_bridge import CompanionTeamBridge

        bridge = CompanionTeamBridge(TOKEN, clock=lambda: 0)
        data = state();data["companion"] = snapshot();seq = [0]
        def exchange(results=None):
            seq[0] += 1
            return bridge.exchange({"token": TOKEN, "session_id": "local", "sequence": seq[0], "state": single_member_state(data),
                                    "results": [{**row, "buddy_id": "buddy1"} for row in results or []]})
        exchange()
        final_board = board({"type": "inventory", "prefab": "boards", "recipe_id": "boards", "count": 1})
        messages = [{"choices": [{"finish_reason": "tool_calls", "message": {"tool_calls": [
            {"id": "q", "type": "function", "function": {"name": "query_recipes", "arguments": '{"recipe_id":"boards"}'}}]}}]},
            {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(final_board)}}]}]
        class Response:
            def __enter__(self): return self
            def __exit__(self, *_): pass
            def read(self): return json.dumps(messages.pop(0)).encode()
        async def pump():
            for _ in range(400):
                response = exchange();command = next(iter(response["commands"]), None)
                if command:
                    self.assertEqual(command["op"], "recipes")
                    row = recipe("boards")
                    data["companion"]["recipes"]["boards"] = row
                    exchange([{"id": command["id"], "revision": 1, "status": "completed", "data": {"recipes": [row], "total": 1}}])
                    return
                await asyncio.sleep(.01)
            self.fail("Worker did not issue the recipe query")
        async with Client(create_server(bridge), read_timeout_seconds=5) as client:
            with patch.object(planning_core.urllib.request, "urlopen", side_effect=lambda *_a, **_k: Response()):
                actor_client = ActorClient(SimpleNamespace(client=client, claims={}), "buddy1")
                agent = CompanionAgent(actor_client, CompanionPlanner("https://offline.invalid", "fake", "fake"), execute=True)
                try:
                    await agent.cycle()
                    game = asyncio.create_task(pump())
                    for _ in range(400):
                        if agent.future.done(): break
                        await agent.rpc("buddy_observe")
                        await asyncio.sleep(.01)
                    await game
                    result = agent.future.result(timeout=0)
                    self.assertEqual(result["tasks"][0]["target"]["recipe_id"], "boards")
                    self.assertEqual(data["companion"]["buddy"]["inventory"], {})
                finally: await agent.close()

    async def test_craft_equip_chop_pickup_place_return_and_verify_use_the_shared_board(self):
        peer = NativePeer();calls = []
        def planner(context, *_):
            calls.append(context)
            if len(calls) == 1:
                return board({"type": "inventory", "prefab": "axe", "recipe_id": "axe", "count": 1},
                             {"type": "inventory", "prefab": "log", "count": 2},
                             {"type": "structure", "recipe_id": "researchlab", "prefab": "researchlab", "x": 0, "z": 0},
                             {"type": "return_owner"})
            return {"kind": "agent_reply", "outcome": "completed", "reply": "完成。", "evidence": "背包有木头，科学机器在附近，已归队。"}
        with patch("mcp_agent.companion_agent.ThreadPoolExecutor", ImmediateWorker):
            agent = CompanionAgent(peer, planner, execute=True)
        try:
            for _ in range(24): await agent.cycle()
            ops = [a["op"] for name, a in peer.calls if name == "buddy_action"]
            self.assertEqual(ops, ["craft", "equip", "work", "work", "collect", "collect", "place", "follow"])
            self.assertEqual(len(calls), 2)
            self.assertTrue(any(row["finish"] for row in peer.bridge.feedback.values()))
        finally: await agent.close()

    async def test_live_recipe_mcp_roundtrip_is_read_only_and_rejects_old_intents(self):
        from mcp import Client
        from test_companion_team import single_member_state
        from mcp_server.companion_team_bridge import CompanionTeamBridge

        bridge = CompanionTeamBridge(TOKEN, clock=lambda: 0)
        data = state();data["companion"] = snapshot();seq = [0]
        def exchange(results=None):
            seq[0] += 1
            return bridge.exchange({"token": TOKEN, "session_id": "local", "sequence": seq[0], "state": single_member_state(data),
                                    "results": [{**row, "buddy_id": "buddy1"} for row in results or []]})
        exchange()
        async def pump():
            while True:
                response = exchange();command = next(iter(response["commands"]), None)
                if command:
                    self.assertEqual(command["op"], "recipes")
                    exchange([{"id": command["id"], "revision": 1, "status": "completed", "data": {"recipes": [recipe()], "total": 1}}])
                    return
                await asyncio.sleep(.01)
        async with Client(create_server(bridge), read_timeout_seconds=3) as client:
            task = asyncio.create_task(pump())
            result = await client.call_tool("buddy_recipes", {"revision": 1, "binding_token": "buddy:1", "recipe_id": "axe"})
            await task
            self.assertEqual(result.structured_content["recipes"][0]["product"], "axe")
            rejected = await client.call_tool("buddy_recipes", {"revision": 0, "binding_token": "buddy:1"})
            self.assertTrue(rejected.is_error)
            self.assertEqual(data["companion"]["buddy"]["inventory"], {})
