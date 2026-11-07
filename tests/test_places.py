"""Building/home goals, snapshot queries and MCP routing without a game/model."""
import asyncio
import copy
import json
import unittest
from unittest.mock import patch

from common.places import query_places, target_progress
from common.task_board import board_view, validate_board
from mcp_agent import daily_planner
from mcp_agent.priority_board import PriorityBoardExecutor, validate_goals, merge_goals
from mcp_agent.navigation_intents import dispatch_for_navigation
from mcp_agent.model_state import model_game_state
from mcp_agent.mcp_agent import MCPAgent
from test_mcp_agent import snapshot
from test_recipes import Response


PLACES = {"scope": "world_player_session", "buildings": [
    {"id": "building:42", "guid": 42, "prefab": "researchlab", "semantic_prefab": "sciencemachine",
     "status": "unconfirmed", "arrived": False, "x": 40, "z": 0, "node_id": "A"},
    {"id": "building:43", "guid": 43, "prefab": "chest", "status": "missing", "arrived": False}],
    "anchors": [{"id": "building:42", "building_id": "building:42", "ready": False, "x": 40, "z": 0, "node_id": "A"}],
    "home": {"id": "home:1", "anchor_id": "building:40", "x": 20, "z": 0, "arrived": False}}


def board(kind="visit_building", value="building:42", identity="visit"):
    field = {"visit_building": "building_id", "set_home": "anchor_id", "return_home": "home_id"}[kind]
    return validate_goals({"goal": "Visit", "tasks": [{"id": identity, "title": identity,
        "target": {"type": kind, field: value}}]}, 1)


class PlacesTests(unittest.TestCase):
    def actual(self):
        actual = snapshot(); actual["places"] = copy.deepcopy(PLACES)
        return actual

    def test_queries_are_bounded_searchable_and_do_not_change_memory(self):
        page = query_places(PLACES, query="sciencemachine", limit=1)
        self.assertEqual(page["buildings"][0]["id"], "building:42")
        self.assertEqual(query_places(PLACES, building_id="building:43")["buildings"][0]["status"], "missing")
        self.assertEqual(query_places(PLACES, limit=1)["next_offset"], 1)
        page["home"]["id"] = "changed"
        self.assertEqual(PLACES["home"]["id"], "home:1")
        for kwargs in ({"limit": 25}, {"limit": True}, {"offset": -1}, {"query": []}):
            with self.assertRaises(ValueError):
                query_places(PLACES, **kwargs)

    def test_place_targets_require_ids_and_reject_coordinates_as_ids(self):
        for kind, key in (("visit_building", "building_id"), ("set_home", "anchor_id"), ("return_home", "home_id")):
            for value in (None, {"x": 1, "z": 2}, "some invented location", "x" * 101):
                with self.assertRaises(ValueError):
                    validate_board({"goal": "Go", "tasks": [{"id": "go", "title": "Go",
                        "target": {"type": kind, key: value}}]}, 1)
        for value in ("here:1", "building:madeup", "home:1"):
            with self.assertRaises(ValueError):
                board("set_home", value)

    def test_set_home_travels_to_stable_building_then_sets_home_after_arrival(self):
        actual = self.actual(); goals = board("set_home", "building:42")
        runtime = PriorityBoardExecutor()
        travel = {"kind":"explore", "planner_only":True, "building_id":"building:42", "mode":"place"}
        self.assertEqual(runtime.view(goals, actual)["tasks"][0]["status"], "ready")
        self.assertEqual(runtime.choose(goals, actual, {"visit":travel}, None)[1], "visit")
        actual["player"]["position"]["x"] = 38
        actual["places"]["buildings"][0].update(status="observed", arrived=True)
        action = {"kind":"set_home", "anchor_id":"building:42", "planner_only":True}
        self.assertEqual(runtime.choose(goals, actual, {"set":action}, None)[1], "set")
        actual["places"]["home"] = {"id":"home:2", "anchor_id":"building:42"}
        self.assertEqual(runtime.view(goals, actual)["status"], "completed")

    def test_arrival_requires_observed_building_and_home_identity(self):
        actual = self.actual(); goal = board()
        self.assertEqual(board_view(goal, actual)["tasks"][0]["status"], "ready")
        actual["places"]["buildings"][0]["arrived"] = True
        self.assertEqual(board_view(goal, actual)["tasks"][0]["status"], "ready")
        actual["places"]["buildings"][0]["status"] = "observed"
        self.assertEqual(board_view(goal, actual)["tasks"][0]["status"], "done")
        home_goal = board("return_home", "home:1")
        actual["places"]["home"]["id"] = "home:2"
        self.assertEqual(board_view(home_goal, actual)["tasks"][0]["blocked_reason"], "home unset or changed")

    def test_missing_building_and_expired_home_anchor_are_explicitly_blocked(self):
        self.assertIn("missing", target_progress(board(value="building:43")["tasks"][0]["target"], PLACES)[1])
        self.assertIn("missing", target_progress(board("set_home", "building:43")["tasks"][0]["target"], PLACES)[1])
        self.assertIn("telemetry", target_progress(board()["tasks"][0]["target"], None)[1])

    def test_invalid_home_anchor_releases_focus_to_independent_rope_craft(self):
        goals = validate_goals({"goal": "Set home and craft rope", "tasks": [
            {**board("set_home", identity="camp")["tasks"][0], "priority": 1, "focus": True},
            {"id": "rope", "title": "Craft rope", "priority": 2,
             "target": {"type": "inventory", "prefab": "rope", "recipe_id": "rope", "count": 1}},
        ]}, 1)
        actions = {"recipe_rope": {"kind": "craft", "product": "rope", "recipe_id": "rope", "planner_only": True}}
        for unavailable in ("destroyed", "forgotten"):
            with self.subTest(unavailable=unavailable):
                actual = self.actual()
                runtime = PriorityBoardExecutor()
                self.assertEqual(runtime.view(goals, actual)["focus_task_ids"], ["camp"])
                # Missing telemetry also must not release focus.
                complete = actual.pop("places")
                self.assertEqual(runtime.view(goals, actual)["focus_task_ids"], ["camp"])
                actual["places"] = complete
                if unavailable == "destroyed":
                    complete["buildings"][0]["status"] = "missing"
                else:
                    complete["buildings"].pop(0)
                view = runtime.view(goals, actual)
                self.assertEqual(view["tasks"][0]["status"], "failed")
                self.assertIn("query places again", view["tasks"][0]["blocked_reason"])
                self.assertEqual(view["focus_task_ids"], [])
                self.assertEqual(view["execution_task_ids"], ["rope"])
                self.assertEqual(runtime.choose(goals, actual, actions, None)[1], "recipe_rope")

    def test_task_executor_selects_exact_destination_but_does_local_work_first(self):
        actual = self.actual(); goals = board()
        travel = {"kind": "explore", "planner_only": True, "building_id": "building:42", "mode": "place", "x": 4, "z": 0}
        actions = {"visit_building:42": travel, "wrong": {**travel, "building_id": "building:99"}}
        runtime = PriorityBoardExecutor()
        self.assertEqual(runtime.choose(goals, actual, actions, None)[1], "visit_building:42")
        self.assertTrue(runtime.navigation)
        goals["tasks"].append({"id": "grass", "title": "Grass", "priority": 5, "depends_on": [],
                               "target": {"type": "inventory", "prefab": "cutgrass", "count": 1}})
        actions["collect_grass"] = {"kind": "collect", "product": "cutgrass", "guid": 1}
        self.assertEqual(runtime.choose(goals, actual, actions, None)[1], "collect_grass")

    def test_repeat_visit_uses_a_new_task_identity_after_leaving(self):
        actual = self.actual(); actual["places"]["buildings"][0].update(status="observed", arrived=True)
        runtime = PriorityBoardExecutor(); first = board()
        previous = runtime.view(first, actual)
        actual["places"]["buildings"][0].update(status="unconfirmed", arrived=False)
        incoming = board(identity="visit_again")
        # Keep another unfinished task to exercise patch merge rather than reset.
        previous["status"] = "active"
        merged = merge_goals(previous, incoming)
        self.assertIn("visit_again", [t["id"] for t in merged["tasks"]])
        again = next(t for t in runtime.view(merged, actual)["tasks"] if t["id"] == "visit_again")
        self.assertEqual(again["status"], "ready")

    def test_jev_and_unprompted_exploration_do_not_receive_place_actions(self):
        actual = self.actual()
        actions = {"wait": {"kind": "wait"}, "set_home_building:42": {
            "kind": "set_home", "anchor_id": "building:42", "planner_only": True},
            "return_home": {"kind": "explore", "home_id": "home:1", "planner_only": True}}
        actual["bridge"]["available_actions"] = actions
        facts = model_game_state(actual)
        self.assertNotIn("places", facts)
        self.assertEqual(set(facts["control"]["available_actions"]), {"wait"})
        self.assertEqual(dispatch_for_navigation(actions, actual), {})
        agent = MCPAgent(None, lambda *_: self.fail("No new JEV decision expected"))
        try:
            self.assertEqual(asyncio.run(agent.choose_local({"state": actual, "actions": actions}))[0], "wait")
        finally:
            agent.worker.shutdown()

    def test_deepseek_can_query_places_without_a_recipe_catalog(self):
        context = daily_planner.planning_context(self.actual(), [])
        responses = [Response({"tool_calls": [{"id": "q1", "type": "function", "function": {
            "name": "query_places", "arguments": '{"query":"sciencemachine"}'}}]}, "tool_calls"),
            Response({"content": json.dumps(board())})]
        with patch.object(daily_planner.urllib.request, "urlopen", side_effect=responses) as send:
            result = daily_planner.request_plan("https://example.invalid", "fake", "model", 1, context)
        self.assertEqual(result["tasks"][0]["target"]["building_id"], "building:42")
        initial = json.loads(send.call_args_list[0].args[0].data)
        self.assertEqual([t["function"]["name"] for t in initial["tools"]], ["query_places"])
        self.assertNotIn("_place_catalog", initial["messages"][-1]["content"])

    def test_deepseek_rejects_invented_building_and_stale_home(self):
        for goal in (board(value="building:999"), board("return_home", "home:99"), board("set_home", "building:999")):
            with patch.object(daily_planner.urllib.request, "urlopen", return_value=Response({"content": json.dumps(goal)})):
                with self.assertRaises(RuntimeError):
                    daily_planner.request_plan("https://example.invalid", "fake", "model", 1,
                                              daily_planner.planning_context(self.actual(), []))


class PlacesProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_queries_and_all_new_action_kinds_cross_the_actual_mcp_bridge(self):
        try:
            from mcp import Client
        except ImportError:
            self.skipTest("MCP SDK not installed")
        from mcp_server.game_bridge import GameBridge
        from mcp_server.mcp_server import create_server
        for kind in ("set_home", "explore", "craft", "place"):
            bridge = GameBridge("offline-place-test-token-" * 2)
            actual = snapshot(); actual["places"] = copy.deepcopy(PLACES)
            actual["bridge"]["available_actions"] = {"new_action": {"kind": kind, "planner_only": True}}
            bridge.exchange({"token": bridge.token, "session_id": "places", "sequence": 1, "state": actual})
            async with Client(create_server(bridge), read_timeout_seconds=3) as client:
                result = await client.call_tool("query_places", {"building_id": "building:42"})
                self.assertFalse(result.is_error)
                self.assertEqual(result.structured_content["buildings"][0]["id"], "building:42")
                self.assertIsNone(bridge.active)
                packet = (await client.call_tool("get_available_actions", {})).structured_content
                self.assertIn("new_action", packet["actions"])
                result = await client.call_tool("start_action", {"choice": "new_action", "observation_id": packet["observation_id"]})
                self.assertFalse(result.is_error)
                self.assertEqual(result.structured_content["status"], "queued")
