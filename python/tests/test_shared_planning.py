"""Exercise both actor profiles against the real shared planning/task policies."""
import copy
import json
import threading
import unittest
from unittest.mock import patch

from common.task_board import validate_board, board_view
from mcp_agent import daily_planner, companion_planner, planning_core
from mcp_agent.companion_tasks import CompanionTasks, actor_state, validate_board as companion_board
from mcp_agent.priority_board import PriorityBoardExecutor, idle_review_reason


def observation():
    return {"capabilities": ["collect", "move", "follow", "stay"], "version": 1,
            "world": {"day": 1, "phase": "day", "time": .2}, "mode": "stay",
            "buddy": {"guid": 2, "position": {"x": 0, "z": 0}, "owner_distance": 8,
                      "inventory": {}, "dead": False},
            "nearby": [{"guid": 7, "prefab": "grass", "product": "cutgrass", "distance": 2}]}


def task(identity, prefab="cutgrass", **fields):
    return {"id": identity, "title": identity, "target": {"type": "inventory", "prefab": prefab, "count": 3}, **fields}


def board(*tasks):
    return {"goal": "Gather supplies", "reply": "我去收集。", "tasks": list(tasks)}


class Response:
    def __init__(self, content, finish="stop"):
        self.payload = {"choices": [{"finish_reason": finish, "message": {"content": json.dumps(content)}}]}
    def __enter__(self): return self
    def __exit__(self, *_): pass
    def read(self): return json.dumps(self.payload).encode("utf8")


class SharedTaskTests(unittest.TestCase):
    def test_a_satisfied_target_cannot_latch_before_its_dependencies_are_finished(self):
        raw = board(task("grass"), task("twigs", "twigs", depends_on=["grass"]))
        observed = observation();observed["buddy"]["inventory"] = {"twigs": 3}
        for runtime, clean in ((PriorityBoardExecutor(), daily_planner.validate_goals(raw, 1)),
                               (CompanionTasks().goals, companion_board(raw, 1))):
            runtime.adopt(clean)
            first = runtime.view(clean, actor_state(observed))
            self.assertEqual(next(t for t in first["tasks"] if t["id"] == "twigs")["status"], "blocked")
            self.assertFalse(runtime.records["twigs"]["done"])
            observed["buddy"]["inventory"] = {"cutgrass": 3}
            runtime.view(clean, actor_state(observed))
            # Latched observed prerequisites remain valid after the stock is spent.
            observed["buddy"]["inventory"] = {"twigs": 3}
            final = runtime.view(clean, actor_state(observed))
            self.assertEqual(final["status"], "completed")
            observed["buddy"]["inventory"] = {"twigs": 3}

    def test_same_inventory_goal_has_same_progress_dependencies_and_schedule_for_both_actors(self):
        raw = board(task("grass"), task("twigs", "twigs", depends_on=["grass"], start_at={"day": 1, "phase": "dusk"}))
        observed = observation()
        partner = CompanionTasks();partner.adopt(companion_board(raw, 1), observed)
        player = PriorityBoardExecutor()
        client_board = daily_planner.validate_goals(raw, 1);player.adopt(client_board)
        for inventory, phase in (({}, "day"), ({"cutgrass": 3}, "day"), ({"cutgrass": 3}, "dusk")):
            observed["buddy"]["inventory"] = inventory;observed["world"]["phase"] = phase
            observed["version"] += 1
            left, right = player.view(client_board, actor_state(observed)), partner.view(observed)
            project = lambda view: [(t["id"], t["status"], t["progress"]) for t in view["tasks"]]
            self.assertEqual(project(left), project(right))

    def test_priority_focus_and_dependency_selection_use_the_shared_scheduler(self):
        observed = observation()
        observed["nearby"].append({"guid": 8, "product": "twigs", "distance": 1})
        raw = board(task("low", "twigs", priority=1), task("focused", focus=True, priority=5))
        runtime = CompanionTasks();runtime.adopt(companion_board(raw, 1), observed)
        self.assertEqual(runtime.next_action(observed)[0]["id"], "focused")
        observed["buddy"]["inventory"]["cutgrass"] = 3
        self.assertEqual(runtime.next_action(observed)[0]["id"], "low")

    def test_scheduled_task_waits_without_native_actions_and_activates_on_game_time(self):
        observed = observation()
        runtime = CompanionTasks()
        runtime.adopt(companion_board(board(task("night", start_at={"day": 1, "phase": "night"})), 1), observed)
        self.assertEqual(runtime.next_action(observed)[1], "idle")
        self.assertEqual(idle_review_reason(runtime.view()), "only_scheduled_tasks")
        observed["world"]["phase"] = "night"
        self.assertEqual(runtime.next_action(observed)[1], "collect")

    def test_invalid_timing_fails_only_its_task_and_dependencies_do_not_execute(self):
        observed = observation()
        runtime = CompanionTasks()
        raw = board(task("bad", duration=.25, end_at={"day": 2, "phase": "day"}),
                    task("dependent", depends_on=["bad"]))
        runtime.adopt(companion_board(raw, 1), observed)
        statuses = {t["id"]: t["status"] for t in runtime.view()["tasks"]}
        self.assertEqual(statuses, {"bad": "failed", "dependent": "blocked"})
        self.assertEqual(runtime.next_action(observed)[1], "idle")

    def test_merge_preserves_unfinished_work_and_explicit_cancellation_removes_it(self):
        observed = observation()
        runtime = CompanionTasks();runtime.adopt(companion_board(board(task("grass")), 1), observed)
        incoming = companion_board(board(task("twigs", "twigs")), 1)
        runtime.adopt(incoming, observed)
        self.assertEqual({t["id"] for t in runtime.plan["tasks"]}, {"grass", "twigs"})
        incoming["cancel_task_ids"] = ["grass"]
        runtime.adopt(incoming, observed)
        self.assertEqual([t["id"] for t in runtime.plan["tasks"]], ["twigs"])

    def test_native_acceptance_cannot_complete_a_move_or_return_trip(self):
        observed = observation();runtime = CompanionTasks()
        raw = board({"id": "return", "title": "Return", "target": {"type": "return_owner"}})
        runtime.adopt(companion_board(raw, 1), observed)
        self.assertEqual(runtime.next_action(observed)[1], "follow")
        observed["mode"] = "follow"
        self.assertEqual(runtime.next_action(observed)[1], "waiting")
        observed["buddy"]["owner_distance"] = 3
        self.assertEqual(runtime.next_action(observed)[1], "completed")

    def test_companion_capabilities_are_enforced_without_expanding_the_client_schema(self):
        observed = observation();observed["capabilities"] = ["follow"]
        with self.assertRaisesRegex(ValueError, "capability"):
            companion_board(board(task("grass")), 1, observed=observed)
        with self.assertRaises(ValueError):
            validate_board(board({"id": "follow", "title": "Follow", "target": {"type": "follow"}}), 1)
        with self.assertRaises(ValueError):
            companion_board(board({"id": "build", "title": "Build", "target": {"type": "structure", "prefab": "campfire"}}), 1)

    def test_raw_state_is_not_mutated_and_no_unobserved_fields_are_fabricated(self):
        observed = observation();original = copy.deepcopy(observed)
        normalized = actor_state(observed)
        self.assertNotIn("world_map", normalized)
        self.assertNotIn("hunger_max", normalized["player"]["vitals"])
        board_view(daily_planner.validate_goals(board(task("grass")), 1), normalized)
        self.assertEqual(observed, original)


class SharedPlannerTests(unittest.TestCase):
    def test_both_profiles_share_contract_token_budget_and_repair_a_truncated_response(self):
        raw = board(task("grass"))
        context = {"interaction": {"mode": "instruction", "instruction": "收集三根草"}, "observation": observation()}
        for profile in ("player", "companion"):
            with self.subTest(profile=profile):
                with patch.object(planning_core.urllib.request, "urlopen", side_effect=[Response({}, "length"), Response(raw)]) as send:
                    if profile == "player":
                        result = daily_planner.request_plan("https://offline.invalid", "fake", "fake", 1, context)
                    else:
                        result = companion_planner.request_plan("https://offline.invalid", "fake", "fake", context, [], threading.Event())
                self.assertEqual(result["kind"], "task_board")
                self.assertEqual(send.call_count, 2)
                body = json.loads(send.call_args.args[0].data)
                self.assertEqual(body["max_tokens"], planning_core.PLAN_MAX_TOKENS)
                self.assertIn(planning_core.PLANNING_RULES, body["messages"][0]["content"])
                self.assertIn("finish_reason=length", body["messages"][-1]["content"])

    def test_completion_rejection_is_identical_for_both_profiles(self):
        context = {"interaction": {"mode": "instruction"}, "observation": observation(),
                   "current_task_board": {"tasks": [{"status": "scheduled"}]}}
        raw = {"kind": "agent_reply", "outcome": "completed", "reply": "完成", "evidence": "完成了"}
        for validate in (lambda: daily_planner.validate_agent_response(raw, 1, context),
                         lambda: companion_planner.validate_plan(raw, context)):
            with self.assertRaisesRegex(ValueError, "unfinished"):
                validate()

    def test_companion_model_cannot_bypass_backend_capabilities_and_can_repair_its_plan(self):
        context = {"interaction": {"mode": "instruction"}, "observation": observation()}
        unsupported = board({"id": "build", "title": "Build", "target": {"type": "structure", "prefab": "campfire"}})
        with patch.object(planning_core.urllib.request, "urlopen", side_effect=[Response(unsupported), Response(board(task("grass")))]) as send:
            result = companion_planner.request_plan("https://offline.invalid", "fake", "fake", context, [], threading.Event())
        self.assertEqual(result["tasks"][0]["target"]["type"], "inventory")
        self.assertEqual(send.call_count, 2)
        body = json.loads(send.call_args.args[0].data)
        self.assertIn("Unsupported companion", body["messages"][-1]["content"])

    def test_chat_reply_keeps_the_common_conversation_contract(self):
        context = {"interaction": {"mode": "instruction"}}
        raw = {"kind": "agent_reply", "outcome": "reply", "reply": "你好。"}
        self.assertEqual(daily_planner.validate_agent_response(raw, 1, context), companion_planner.validate_plan(raw, context))


if __name__ == "__main__":
    unittest.main()
