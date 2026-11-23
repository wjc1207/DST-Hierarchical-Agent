"""Archived player priority-board review tests and shared helpers."""
from concurrent.futures import Future
import unittest
from unittest.mock import patch

from mcp_agent import player_planner
from mcp_agent.priority_board import validate_goals


def task(identity, prefab, priority=1, count=2, **extra):
    return {"id": identity, "title": identity, "priority": priority,
            "target": {"type": "inventory", "prefab": prefab, "count": count}, **extra}


def board(*tasks):
    return {**validate_goals({"goal": "Gather", "tasks": list(tasks)}, 1), "board_id": "goals:1"}


def state(**items):
    return {"observed_at": 1, "world": {"day": 1, "phase": "day", "time": .1},
            "player": {"guid": 1, "position": {"x": 0, "z": 0},
                       "vitals": {"hunger": 100, "hunger_max": 150, "health": 150},
                       "inventory": {"items": [{"prefab": p, "count": n, "durability_percent": 1} for p, n in items.items()], "equipped": []}},
            "nearby": [], "navigation": {"graph": {"current_node": "A:FlowerPatch",
                "known_nodes": 2, "current": {"id": "A:FlowerPatch", "observed_cells": 4}, "routes": []}}}


class PendingWorker:
    def __init__(self, **_): self.calls = []; self.futures = []
    def submit(self, _fn, *args):
        self.calls.append(args); f = Future(); self.futures.append(f); return f
    def shutdown(self, **_): pass


class ReviewTests(unittest.TestCase):
    def planner(self):
        with patch.object(player_planner, "ThreadPoolExecutor", PendingWorker):
            p = player_planner.DailyPlanner("offline", board_log_dir=None)
        self.addCleanup(p.close)
        return p

    def test_late_quarter_response_accepted_and_running_goals_preserved_during_review(self):
        p = self.planner(); s = state(); s["world"]["time"] = .24
        p.update(s)
        s["world"]["time"] = .26
        p.executor.futures[0].set_result((board(task("grass", "cutgrass")), 1., None))
        with patch.object(player_planner.time, "monotonic", return_value=10000):
            active = p.update(s)
        self.assertEqual(active["status"], "active")
        self.assertEqual(len(p.conversation), 1)
        s["world"]["time"] = .51
        p.goal_retry_at = 0
        reviewed = p.update(s)
        self.assertEqual(active["board_id"], reviewed["board_id"])
        self.assertEqual(p.executor.calls[-1][4]["replan_reason"], "quarter_review")
        self.assertIsNotNone(p.executor.calls[-1][4]["current_task_board"])

    def test_foreign_session_result_rejected_without_discarding_current_board(self):
        p = self.planner(); s = state(); p.update(s)
        p.executor.futures[0].set_result((board(task("grass", "cutgrass")), .5, None))
        s["player"]["guid"] = 2
        self.assertIsNone(p.update(s)); self.assertEqual(len(p.conversation), 0)

    def test_completed_board_requests_new_plan_with_verified_progress(self):
        p = self.planner(); p.plan = board(task("grass", "cutgrass"))
        p.goal_retry_at = player_planner.time.monotonic() + 100
        result = p.update(state(cutgrass=2))
        self.assertEqual(result["status"], "completed")
        self.assertEqual(p.executor.calls[-1][4]["replan_reason"], "all_tasks_completed")
        self.assertEqual(p.executor.calls[-1][4]["current_task_board"]["tasks"][0]["status"], "done")

    def test_scheduled_and_mixed_idle_boards_review_immediately(self):
        cases = [
            ([task("later", "twigs", start_at={"day": 1, "progress": .7})],
             "only_scheduled_tasks", ["scheduled"]),
            ([task("done", "cutgrass"), task("later", "twigs", start_at={"day": 1, "progress": .7})],
             "only_scheduled_tasks", ["done", "scheduled"]),
            ([task("later", "twigs", start_at={"day": 1, "progress": .7}),
              task("dependent", "flint", depends_on=["later"])],
             "no_actionable_tasks", ["blocked", "scheduled"]),
            ([task("done", "cutgrass"), task("later", "twigs", start_at={"day": 1, "progress": .7}),
              task("dependent", "flint", depends_on=["later"])],
             "no_actionable_tasks", ["blocked", "done", "scheduled"]),
        ]
        for tasks, reason, statuses in cases:
            with self.subTest(reason=reason, statuses=statuses):
                p = self.planner(); p.plan = board(*tasks)
                p.requested_slot = (1, 0); p.goal_retry_at = 999
                with patch.object(player_planner.time, "monotonic", return_value=100):
                    view = p.update(state(cutgrass=2))
                    for _ in range(3):
                        p.update(state(cutgrass=2))
                self.assertEqual(len(p.executor.calls), 1)
                context = p.executor.calls[0][4]
                self.assertEqual(context["replan_reason"], reason)
                self.assertEqual(sorted(t["status"] for t in view["tasks"]), statuses)
                self.assertEqual(context["current_task_board"], view)

    def test_ready_work_prevents_idle_review(self):
        p = self.planner(); p.plan = board(task("now", "cutgrass"),
            task("later", "twigs", start_at={"day": 1, "progress": .7}))
        p.requested_slot = (1, 0)
        with patch.object(player_planner.time, "monotonic", return_value=100):
            p.update(state())
        self.assertEqual(p.executor.calls, [])

    def test_idle_response_acknowledges_wait_across_revisions_and_telemetry(self):
        p = self.planner()
        tasks = [task("later", "twigs", start_at={"day": 1, "progress": .7}),
                 task("dependent", "flint", depends_on=["later"])]
        p.plan = board(*tasks); p.requested_slot = (1, 0)
        with patch.object(player_planner.time, "monotonic", return_value=100):
            p.update(state())
            p.executor.futures[0].set_result((board(*tasks), .5, None))
            adopted = p.update(state())
        self.assertNotEqual(adopted["board_id"], "goals:1")
        later = state(); later["observed_at"] = 2; later["world"]["time"] = .2
        with patch.object(player_planner.time, "monotonic", return_value=200):
            p.update(later)
        self.assertEqual(len(p.executor.calls), 1)
        # When the schedule opens, normal execution re-arms the idle transition.
        later["world"]["time"] = .8; p.requested_slot = (1, 3)
        p.update(later)
        self.assertIsNone(p.goal_terminal)
        later["player"]["vitals"]["dead"] = True
        p.update(later)
        self.assertEqual(len(p.executor.calls), 2)
        self.assertEqual(p.executor.calls[-1][4]["replan_reason"], "search_or_prerequisite_blocked")

    def test_scheduled_review_failure_keeps_backoff(self):
        p = self.planner(); p.plan = board(task("later", "twigs", start_at={"day": 1, "progress": .7}))
        p.requested_slot = (1, 0)
        with patch.object(player_planner.time, "monotonic", return_value=100):
            p.update(state())
            p.executor.futures[0].set_result((None, .5, RuntimeError("offline failure")))
            p.update(state()); p.update(state())
            self.assertEqual(len(p.executor.calls), 1)
            self.assertEqual(p.goal_replan_reason, "review_retry")
        with patch.object(player_planner.time, "monotonic", return_value=116):
            p.update(state())
        self.assertEqual(len(p.executor.calls), 2)

    def test_unavailable_board_review_is_immediate_deduplicated_and_keeps_error_backoff(self):
        p = self.planner(); p.plan = board({"id":"campfire","title":"Night fire",
            "target":{"type":"structure","prefab":"campfire"}})
        p.requested_slot = (1, 0)
        p.goal_retry_at = 999
        with patch.object(player_planner.time, "monotonic", return_value=100):
            view = p.request_review(state(), "no_feasible_action_or_route", immediate=True)
            p.request_review(state(), "no_feasible_action_or_route")
            self.assertEqual(len(p.executor.calls), 1)
            self.assertEqual(view["tasks"][0]["id"], "campfire")
            self.assertEqual(p.executor.calls[0][4]["replan_reason"], "no_feasible_action_or_route")
            p.executor.futures[0].set_result((None, 1., RuntimeError("offline failure")))
            p.update(state())
            p.request_review(state(), "no_feasible_action_or_route", immediate=True)
            self.assertEqual(len(p.executor.calls), 1)
            self.assertEqual(p.goal_replan_reason, "review_retry")
        with patch.object(player_planner.time, "monotonic", return_value=116):
            p.update(state())
        self.assertEqual(len(p.executor.calls), 2)
        self.assertIsNotNone(p.plan)

    def test_empty_runtime_board_requests_evaluation_instead_of_waiting_for_quarter(self):
        p = self.planner(); p.plan = {**board(task("grass", "cutgrass")), "tasks": []}
        p.requested_slot = (1, 0)
        p.goal_retry_at = player_planner.time.monotonic() + 100
        self.assertEqual(p.update(state())["status"], "completed")
        self.assertEqual(len(p.executor.calls), 1)
        self.assertEqual(p.executor.calls[0][4]["current_task_board"]["tasks"], [])

    def test_exact_native_failure_feedback_is_preserved_without_invalidating_board(self):
        p = self.planner(); p.plan = board(task("grass", "cutgrass"))
        p.record_action(state(), "travel_B", "failed", "action_unavailable")
        p.record_action(state(), "travel_B", "failed", "action_unavailable")
        self.assertIsNotNone(p.plan)
        self.assertEqual(p.history[-1]["reason"], "action_unavailable")


if __name__ == "__main__":
    unittest.main()
