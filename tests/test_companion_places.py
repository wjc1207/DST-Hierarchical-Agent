import unittest
import threading
from types import SimpleNamespace
from unittest.mock import patch

from test_companion import state, TOKEN, FakePeer, ImmediateWorker
from test_companion_actions import recipe
from mcp_agent.companion_tasks import CompanionTasks, validate_board
from mcp_agent.companion_planner import validate_plan, request_plan
from mcp_agent.companion_agent import CompanionAgent
from mcp_server.companion_bridge import CompanionBridge


def observation():
    observed = state()["companion"]
    observed["capabilities"] += ["visit_building", "craft", "recipes"]
    observed["buddy"]["position"] = {"x": 200, "z": 0}
    observed["places"] = {"scope": "companion_observed_buildings", "buildings": [
        {"id": "building:123456", "guid": 123456, "prefab": "researchlab", "name": "Science Machine", "x": 0, "z": 0,
         "status": "unconfirmed", "arrived": False, "distance": 200, "last_seen_at": 1}]}
    observed["recipes"] = {"rope": {**recipe("rope"), "ready": False, "reason": "technology_or_unlock_required"}}
    return observed


def board():
    return validate_board({"goal": "Return to science machine and craft rope", "reply": "回到科学机器旁制作。", "tasks": [
        {"id": "lab", "title": "Return to lab", "target": {"type": "visit_building", "building_id": "building:123456"}},
        {"id": "rope", "title": "Make rope", "depends_on": ["lab"], "target": {"type": "inventory", "prefab": "rope", "count": 1, "recipe_id": "rope"}}]}, 1)


class BuildingTasksTests(unittest.TestCase):
    def test_native_arrival_receipt_latches_completion_even_when_next_snapshot_is_outside_radius(self):
        observed=observation();runtime=CompanionTasks();runtime.adopt(board(),observed)
        row=observed["places"]["buildings"][0];row.update(status="observed",arrived=False,distance=3.2)
        observed["recipes"]["rope"]["ready"]=True
        action={"status":"completed","reason":"building_arrived","data":{"building_id":"building:123456","arrived":True,"distance":2.9}}
        self.assertTrue(runtime.on_result("lab",action,observed))
        self.assertEqual(runtime.next_action(observed)[1],"craft")
        task=next(t for t in runtime.view()["tasks"] if t["id"]=="lab")
        self.assertEqual((task["status"],task["progress"]),("done",{"current":1,"required":1}))

    def test_wrong_building_or_unconfirmed_results_cannot_complete_a_visit(self):
        for action in (
            {"status":"completed","reason":"building_arrived","data":{"building_id":"building:999","arrived":True}},
            {"status":"completed","reason":"building_arrived","data":{"building_id":"building:123456","arrived":False}},
            {"status":"failed","reason":"building_arrived","data":{"building_id":"building:123456","arrived":True}}):
            observed=observation();runtime=CompanionTasks();runtime.adopt(board(),observed)
            runtime.on_result("lab",action,observed)
            self.assertFalse(runtime.goals.records["lab"]["done"])
            self.assertEqual(runtime.next_action(observed)[1],"visit_building")

    def test_remote_unconfirmed_memory_is_a_valid_destination_and_arrival_releases_crafting(self):
        observed = observation();runtime = CompanionTasks();runtime.adopt(board(), observed)
        self.assertEqual(runtime.next_action(observed)[1:], ("visit_building", {"building_id": "building:123456"}))
        self.assertEqual(next(t for t in runtime.view()["tasks"] if t["id"] == "rope")["status"], "blocked")
        observed["places"]["buildings"][0].update(status="observed", arrived=True, distance=2)
        observed["recipes"]["rope"]["ready"] = True;observed["version"] += 1
        self.assertEqual(runtime.next_action(observed)[1], "craft")

    def test_arrival_without_live_technology_does_not_claim_recipe_is_ready(self):
        observed = observation();observed["places"]["buildings"][0].update(status="observed", arrived=True)
        runtime = CompanionTasks();runtime.adopt(board(), observed)
        self.assertEqual(runtime.next_action(observed)[1:], ("needs_replan", {"reason": "technology_or_unlock_required", "recipe_id": "rope"}))

    def test_current_memory_is_required_and_missing_is_distinct_from_out_of_range(self):
        observed = observation()
        self.assertEqual(validate_plan(board(), {"observation": observed})["tasks"][0]["target"]["type"], "visit_building")
        observed["places"]["buildings"][0]["status"] = "missing"
        with self.assertRaisesRegex(ValueError, "remembered"):
            validate_plan(board(), {"observation": observed})
        runtime = CompanionTasks();runtime.adopt(board(), observed)
        self.assertEqual(runtime.next_action(observed)[1], "idle")

    def test_forged_building_ids_and_old_capabilities_are_rejected(self):
        plan = board();plan["tasks"][0]["target"]["building_id"] = "lab somewhere"
        with self.assertRaises(ValueError): validate_board(plan, 1)
        observed = observation();observed["capabilities"].remove("visit_building")
        with self.assertRaises(ValueError): CompanionTasks().adopt(board(), observed)


class BuildingBridgeTests(unittest.TestCase):
    def test_query_is_readonly_and_remote_visits_only_accept_remembered_ids(self):
        data = state();data["companion"] = observation();bridge = CompanionBridge(TOKEN, clock=lambda: 0)
        bridge.exchange({"token": TOKEN, "session_id": "local", "sequence": 1, "state": data})
        result = bridge.places(query="researchlab")
        self.assertEqual(result["buildings"][0]["distance"], 200)
        self.assertEqual(result["buildings"][0]["status"], "unconfirmed")
        self.assertIsNone(bridge.active)
        for args in ({"building_id": "building:999"}, {"building_id": "building:123456", "x": 0}, {"building_id": True}):
            with self.assertRaises(ValueError): bridge.action("visit_building", bridge.observe()["observation_id"], args)
        self.assertEqual(bridge.action("visit_building", bridge.observe()["observation_id"], {"building_id": "building:123456"})["status"], "queued")

    def test_common_place_query_preserves_filters_pagination_and_snapshot_isolation(self):
        data = state();data["companion"] = observation();bridge = CompanionBridge(TOKEN, clock=lambda: 0)
        bridge.exchange({"token": TOKEN, "session_id": "local", "sequence": 1, "state": data})
        result = bridge.places(building_id="building:123456", limit=1)
        result["buildings"][0]["x"] = 999
        self.assertEqual(bridge.places()["buildings"][0]["x"], 0)
        self.assertEqual(bridge.places(offset=1)["buildings"], [])


class BuildingPlannerTests(unittest.TestCase):
    def test_place_tool_reuses_remembered_facts_and_prompt_prescribes_visit_for_technology(self):
        def request(url,key,body,validator,*,functions,**kwargs):
            self.assertEqual(functions["query_places"](query="researchlab")["total"], 1)
            self.assertIn("visit_building", body["messages"][0]["content"])
            self.assertIn("20-unit limit", body["messages"][0]["content"])
            self.assertIn("query_places", {t["function"]["name"] for t in body["tools"]})
            return validator(board(), None)
        with patch("mcp_agent.companion_planner.request_json_plan", request):
            request_plan("https://offline.invalid", "fake", "fake", {"observation": observation()}, [], threading.Event())


class BuildingPeer(FakePeer):
    def __init__(self):
        super().__init__();self.state["companion"] = observation();self.exchange()
    async def call_tool(self,name,args):
        if name != "buddy_action" or args["op"] not in {"visit_building", "craft"}:
            return await super().call_tool(name,args)
        self.calls.append((name,args));record=self.bridge.action(**args);observed=self.state["companion"]
        if args["op"] == "visit_building":
            observed["buddy"]["position"] = {"x": 2, "z": 0}
            observed["places"]["buildings"][0].update(status="observed",arrived=True,distance=2)
            observed["recipes"]["rope"]["ready"] = True;reason="building_arrived"
        else:
            observed["buddy"]["inventory"]["rope"] = 1;reason="inventory_increased"
        observed["version"] += 1
        self.exchange([{"id":record["id"],"revision":1,"status":"completed","reason":reason}])
        return SimpleNamespace(is_error=False,structured_content=record)


class BuildingAgentTests(unittest.IsolatedAsyncioTestCase):
    async def test_remote_return_then_craft_reuses_the_lab_without_searching_or_rebuilding(self):
        peer=BuildingPeer();contexts=[]
        def planner(context,*_):
            contexts.append(context)
            return board() if len(contexts)==1 else {"kind":"agent_reply","outcome":"completed","reply":"回到科学机器旁做好绳子了。","evidence":"抵达已观察科学机器，真实库存一根绳子。"}
        with patch("mcp_agent.companion_agent.ThreadPoolExecutor",ImmediateWorker):
            agent=CompanionAgent(peer,planner,execute=True)
        try:
            for _ in range(12):await agent.cycle()
            self.assertEqual([args["op"] for name,args in peer.calls if name=="buddy_action"],["visit_building","craft"])
            self.assertEqual(len(contexts),2)
            self.assertEqual(peer.state["companion"]["buddy"]["inventory"]["rope"],1)
        finally:await agent.close()


if __name__ == "__main__": unittest.main()
