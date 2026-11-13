"""Recipe queries, grounded planning and planner-only action routing; no live API."""
import asyncio
import copy
import json
import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

from common.recipes import query_recipes
from common.task_board import board_view, validate_board
from mcp_agent import daily_planner
from mcp_agent.model_state import model_game_state
from mcp_agent.priority_board import PriorityBoardExecutor, validate_goals
from mcp_agent.mcp_agent import MCPAgent
from test_mcp_agent import snapshot


CATALOG = {
    "boards": {"recipe_id": "boards", "product": "boards", "name": "Wood boards",
               "supported": True, "placement": False, "ready": True, "output_count": 1,
               "ingredients": [{"prefab": "log", "count": 4, "missing": 0}]},
    "researchlab2": {"recipe_id": "researchlab2", "product": "researchlab2",
                     "supported": True, "placement": True, "ready": False,
                     "reason": "ingredients_missing"},
}


def goal(product="boards", recipe="boards", kind="inventory"):
    target = {"type": kind, "prefab": product, "recipe_id": recipe}
    if kind == "inventory":
        target["count"] = 2
    return {"goal": "Craft", "tasks": [{"id": "make", "title": "Make", "target": target}]}


class Response:
    def __init__(self, message, reason="stop"):
        self.payload = {"choices": [{"finish_reason": reason, "message": message}]}

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self):
        return json.dumps(self.payload).encode()


class RecipeTests(unittest.TestCase):
    def test_query_is_paginated_exact_searchable_and_detached(self):
        first = query_recipes(CATALOG, limit=1)
        self.assertEqual((first["total"], first["next_offset"]), (2, 1))
        self.assertEqual(query_recipes(CATALOG, query="WOOD")["recipes"][0]["recipe_id"], "boards")
        self.assertEqual(query_recipes(CATALOG, recipe_id="researchlab2")["recipes"][0]["reason"], "ingredients_missing")
        first["recipes"][0]["ingredients"].clear()
        self.assertTrue(CATALOG["boards"]["ingredients"])
        for kwargs in ({"limit": 1000}, {"offset": -1}, {"limit": True}, {"query": []}):
            with self.assertRaises(ValueError):
                query_recipes(CATALOG, **kwargs)

    def test_dynamic_targets_require_recipe_binding_and_match_exact_output(self):
        plan = validate_goals(goal(), 1)
        action = {"kind": "craft", "product": "boards", "recipe_id": "boards", "planner_only": True}
        task = plan["tasks"][0]
        self.assertEqual(PriorityBoardExecutor.local_choices(task, {"recipe_boards": action})[0][0], "recipe_boards")
        self.assertEqual(PriorityBoardExecutor.local_choices(task, {"other": {**action, "recipe_id": "other"}}), [])
        raw = goal(); del raw["tasks"][0]["target"]["recipe_id"]
        with self.assertRaises(ValueError):
            validate_board(raw, 1)

    def test_structure_and_overflow_completion_use_observations(self):
        actual = snapshot()
        actual["recipe_inventory"] = {"boards": 2}
        self.assertEqual(board_view(validate_goals(goal(), 1), actual)["tasks"][0]["status"], "done")
        plan = validate_goals(goal("researchlab2", "researchlab2", "structure"), 1)
        self.assertEqual(board_view(plan, actual)["tasks"][0]["status"], "ready")
        actual["recipe_entities"] = [{"guid": 99, "prefab": "researchlab2"}]
        self.assertEqual(board_view(plan, actual)["tasks"][0]["status"], "done")
        action = {"kind": "place", "recipe_id": "researchlab2", "product": "researchlab2"}
        self.assertEqual(PriorityBoardExecutor.local_choices(plan["tasks"][0], {"recipe_researchlab2": action})[0][0], "recipe_researchlab2")

    def test_jev_sees_neither_catalog_nor_generic_candidates(self):
        actual = snapshot(); actual["recipes"] = copy.deepcopy(CATALOG)
        actions = {"wait": {"kind": "wait"}, "recipe_boards": {
            "kind": "craft", "planner_only": True, "recipe_id": "boards", "product": "boards"}}
        actual["bridge"]["available_actions"] = actions
        facts = model_game_state(actual)
        self.assertNotIn("recipes", facts)
        self.assertEqual(set(facts["control"]["available_actions"]), {"wait"})
        agent = MCPAgent(None, lambda *_: self.fail("No JEV call needed for wait only"))
        try:
            choice, _ = asyncio.run(agent.choose_local({"state": actual, "actions": actions}))
            self.assertEqual(choice, "wait")
        finally:
            agent.worker.shutdown()

    def test_deepseek_queries_before_planning_without_full_catalog_in_prompt(self):
        actual = snapshot(); actual["recipes"] = copy.deepcopy(CATALOG)
        context = daily_planner.planning_context(actual, [])
        self.assertNotIn("_recipe_catalog", json.loads(daily_planner.snapshot_message(context)))
        responses = [Response({"content": None, "tool_calls": [{"id": "q1", "type": "function",
            "function": {"name": "query_recipes", "arguments": '{"recipe_id":"boards"}'}}]}, "tool_calls"),
            Response({"content": json.dumps(goal())})]
        with patch.object(daily_planner.urllib.request, "urlopen", side_effect=responses) as send:
            plan = daily_planner.request_plan("https://example.invalid", "fake", "model", 1, context)
        self.assertEqual(plan["tasks"][0]["target"]["recipe_id"], "boards")
        initial = json.loads(send.call_args_list[0].args[0].data)
        final = json.loads(send.call_args_list[1].args[0].data)
        self.assertIn("tools", initial)
        self.assertNotIn("_recipe_catalog", initial["messages"][-1]["content"])
        self.assertEqual(json.loads(final["messages"][-1]["content"])["total"], 1)

    def test_planner_rejects_unknown_mismatched_and_unsupported_recipes(self):
        for product, recipe in (("boards", "invented"), ("rope", "boards"), ("boards", "boards")):
            catalog = copy.deepcopy(CATALOG)
            if recipe == product == "boards":
                catalog[recipe]["supported"] = False
            context = {"_recipe_catalog": catalog}
            with patch.object(daily_planner.urllib.request, "urlopen",
                              return_value=Response({"content": json.dumps(goal(product, recipe))})):
                with self.assertRaises(RuntimeError):
                    daily_planner.request_plan("https://example.invalid", "fake", "model", 1, context)

    def test_recipe_tool_rounds_are_bounded(self):
        response = Response({"tool_calls": [{"id": "q", "type": "function", "function": {
            "name": "query_recipes", "arguments": '{}'}}]}, "tool_calls")
        with patch.object(daily_planner.urllib.request, "urlopen", return_value=response) as send:
            with self.assertRaises(RuntimeError):
                daily_planner.request_plan("https://example.invalid", "fake", "model", 1, {"_recipe_catalog": CATALOG})
        self.assertEqual(send.call_count, 8)

    def test_excess_tool_calls_receive_errors_without_exceeding_execution_limit(self):
        calls = [{"id":str(i), "type":"function", "function":{"name":"query_recipes", "arguments":"{}"}}
                 for i in range(6)]
        responses = [Response({"tool_calls":calls}, "tool_calls"), Response({"content":json.dumps(goal())})]
        with patch.object(daily_planner.urllib.request, "urlopen", side_effect=responses) as send, \
             patch.object(daily_planner, "query_recipes", wraps=query_recipes) as query:
            daily_planner.request_plan("https://example.invalid", "fake", "model", 1, {"_recipe_catalog":CATALOG})
        self.assertEqual(query.call_count, 4)
        body = json.loads(send.call_args_list[1].args[0].data)
        answers = [m for m in body["messages"] if m["role"] == "tool"]
        self.assertEqual(len(answers),6)
        self.assertTrue(all("error" in json.loads(m["content"]) for m in answers[4:]))

    def test_merge_overflow_is_sent_back_for_repair_in_the_same_request(self):
        def raw(ids):
            return {"goal":"Gather", "tasks":[{"id":i, "title":i, "target":{
                "type":"inventory", "prefab":"cutgrass", "count":n+1}} for n,i in enumerate(ids)]}
        old = validate_goals(raw(["old"]),1)
        # Different products prevent semantic renaming from folding new goals into old.
        invalid = raw(["a","b","c","d","e","f"])
        for t,prefab in zip(invalid["tasks"], ["twigs","flint","log","rocks","berries","goldnugget"]):
            t["target"]["prefab"] = prefab
        repaired = {**invalid, "cancel_task_ids":["old"]}
        responses = [Response({"content":json.dumps(invalid)}), Response({"content":json.dumps(repaired)})]
        with tempfile.TemporaryDirectory() as temp, \
             patch.object(daily_planner, "PLAN_ERROR_LOG", Path(temp)/"planner_errors.jsonl") as error_log, \
             patch.object(daily_planner.urllib.request, "urlopen", side_effect=responses) as send:
            plan=daily_planner.request_plan("https://example.invalid", "fake", "model", 1, {"current_task_board":old})
            event=json.loads(error_log.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(event["stage"],"response_validation")
        self.assertIn("Merged task board has 7 tasks",event["error"])
        self.assertEqual(plan["cancel_task_ids"],["old"])
        correction=json.loads(send.call_args_list[1].args[0].data)["messages"][-1]["content"]
        self.assertIn("Merged task board has 7 tasks",correction)
        self.assertIn("cancel_task_ids",correction)


class RecipeProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_mcp_query_returns_recipe_facts_without_acquiring_control(self):
        try:
            from mcp import Client
        except ImportError:
            self.skipTest("MCP SDK not installed")
        from mcp_server.game_bridge import GameBridge
        from mcp_server.mcp_server import create_server
        bridge = GameBridge("offline-recipe-test-token-" * 2)
        actual = snapshot(); actual["recipes"] = copy.deepcopy(CATALOG)
        bridge.exchange({"token": bridge.token, "session_id": "recipe-tests",
                         "sequence": 1, "state": actual})
        async with Client(create_server(bridge), read_timeout_seconds=3) as client:
            result = await client.call_tool("query_recipes", {"recipe_id": "boards"})
            self.assertFalse(result.is_error)
            self.assertEqual(result.structured_content["recipes"][0]["product"], "boards")
            self.assertIn("age_seconds", result.structured_content)
            self.assertIsNone(bridge.active)
            result = await client.call_tool("query_recipes", {"limit": 10000})
            self.assertTrue(result.is_error)
            self.assertIsNone(bridge.active)
