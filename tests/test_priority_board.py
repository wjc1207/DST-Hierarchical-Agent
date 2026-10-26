"""Goal scheduling, source priors and asynchronous reviews; no game or model I/O."""
import copy
from concurrent.futures import Future
import unittest
from unittest.mock import patch

from mcp_agent import daily_planner
from mcp_agent.priority_board import PriorityBoardExecutor, validate_goals, merge_goals, task_signature
from mcp_agent.navigation_intents import ExplorationExecutor, dispatch_for_navigation
from mcp_agent.region_resources import expected_sources, room_resources
from tools.extract_worldgen_resources import extract


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
                "known_nodes": 2, "current": {"id": "A:FlowerPatch", "explored_area": 64}, "routes": []}}}


def collect(product, prefab="grass", guid=1):
    return {"kind": "collect", "product": product, "prefab": prefab, "guid": guid}


NAV = {"explore_current_region": {"kind": "explore", "mode": "region", "x": 4, "z": 0},
       "explore_other_region": {"kind": "explore", "mode": "search", "x": 4, "z": 0}}


class GoalTests(unittest.TestCase):
    def test_navigation_targets_include_all_ready_local_goals_but_not_done_or_blocked(self):
        goals = board(task("gold","goldnugget"), task("grass","cutgrass",priority=2),
                      task("twigs","twigs",depends_on=["grass"]), task("logs","log"),
                      task("rocks","rocks"), {"id":"food","title":"Food","priority":3,
                      "target":{"type":"hunger_ratio","at_least":.8}})
        runtime = PriorityBoardExecutor(); s = state(log=2)
        runtime.view(goals,s)  # Completion remains latched after spending the logs.
        s["player"]["inventory"]["items"] = []
        runtime._record(goals["tasks"][4])["blocked"] = "search_budget_exhausted"
        targets = runtime.navigation_targets(goals,s)
        self.assertEqual(targets, [{"type":"inventory","prefab":"goldnugget","count":2},
                                  {"type":"inventory","prefab":"cutgrass","count":2},
                                  {"type":"hunger_ratio","at_least":.8}])
        targets[0]["count"] = 99
        self.assertEqual(goals["tasks"][0]["target"]["count"],2)
        travel = board({"id":"visit","title":"Visit","target":{"type":"visit_region","node_id":"B"}})
        self.assertEqual(runtime.navigation_targets(travel,s), [])

    def hunger_board(self, **extra):
        return board({"id": "food", "title": "Recover hunger", "priority": 1,
                      "target": {"type": "hunger_ratio", "at_least": .6}, **extra})

    def test_continuous_navigation_reviews_changes_and_preserves_room_and_goal_budgets(self):
        clock = [0.]; runtime = PriorityBoardExecutor(clock=lambda:clock[0])
        goals, s = board(task("gold","goldnugget")), state()
        targets = runtime.navigation_targets(goals,s); signature = task_signature(goals["tasks"][0])
        runtime.navigation, runtime.navigation_mode, runtime.selected = True, "region", "gold"
        runtime.last_tick = 0
        runtime.room_search[("gold","A:FlowerPatch")] = {"seconds":0.,"area":64}
        self.assertIsNone(runtime.navigation_review_reason(goals,s,"gold",signature,targets))
        clock[0] = 30
        self.assertEqual(runtime.navigation_review_reason(goals,s,"gold",signature,targets),"room_search_budget")
        revised = board(task("gold","goldnugget"),task("grass","cutgrass",priority=2))
        self.assertEqual(runtime.navigation_review_reason(revised,s,"gold",signature,targets),"navigation_targets_changed")
        runtime.records["gold"]["blocked"] = "search_budget_exhausted"
        self.assertEqual(runtime.navigation_review_reason(goals,s,"gold",signature,targets),"goal_changed_or_blocked")
        self.assertEqual(runtime.navigation_review_reason(None,s,"gold",signature,targets),"board_changed")

    def test_hunger_goal_gathers_then_eats_and_finishes_from_observed_hunger(self):
        goals = self.hunger_board(); runtime = PriorityBoardExecutor(); nav = ExplorationExecutor()
        s = state(); s["player"]["vitals"]["hunger"] = 67
        s["nearby"] = [{"guid": 2, "distance": 13.05}, {"guid": 3, "distance": 1}]
        actions = {**NAV, "carrot": collect("carrot", "carrot_planted", 2),
                   "flower": collect("petals", "flower", 3), "meat": collect("meat", "meat", 4)}
        self.assertEqual(runtime.choose(goals, s, actions, nav)[1], "carrot")
        s["player"]["inventory"]["items"] = [{"prefab": "carrot", "count": 1}]
        actions["eat_safe_food"] = {"kind": "eat_safe_food"}
        self.assertEqual(runtime.choose(goals, s, actions, nav)[1], "eat_safe_food")
        self.assertEqual(runtime.view(goals, s)["status"], "active")
        s["player"]["vitals"]["hunger"] = 90
        self.assertIsNone(runtime.choose(goals, s, actions, nav))
        self.assertEqual(runtime.view(goals, s)["status"], "completed")
        s["player"]["vitals"]["hunger"] = 89
        self.assertEqual(runtime.view(goals, s)["status"], "completed")

    def test_hunger_search_uses_food_positions_rooms_priors_then_unknown_branches(self):
        cases = [
            ("remembered_source_position", "carrot_route", {}, [],
             {"carrot_route": {"kind": "explore", "mode": "resource", "prefab": "carrot_planted",
                               "resource_guid": 22, "node_id": "A:FlowerPatch", "distance": 20}}),
            ("source_room", "explore_current_region", {"resources_seen": ["berrybush"]}, [], {}),
            ("known_source_bfs", "travel_B", {},
             [{"node_id": "B:Unknown", "resources_seen": ["carrot_planted"], "hops": 1}],
             {"travel_B": {"kind": "explore", "mode": "graph", "node_id": "B:Unknown"}}),
            ("worldgen_source_bfs", "travel_B", {}, [{"node_id": "B:BGGrass", "hops": 1}],
             {"travel_B": {"kind": "explore", "mode": "graph", "node_id": "B:BGGrass"}}),
            ("dfs_unknown_exit", "explore_other_region", {}, [], {}),
        ]
        for strategy, choice, current, routes, extra in cases:
            with self.subTest(strategy=strategy):
                s = state(); s["player"]["vitals"]["hunger"] = 67
                s["navigation"]["graph"]["current"].update(current)
                s["navigation"]["graph"]["routes"] = routes
                actions = {**NAV, **extra, "grass_route": {"kind": "explore", "mode": "resource",
                           "prefab": "grass", "resource_guid": 23, "node_id": "A:FlowerPatch", "distance": 1}}
                nav = ExplorationExecutor()
                self.assertEqual(PriorityBoardExecutor().choose(self.hunger_board(), s, actions, nav)[1], choice)
                self.assertEqual(nav.strategy, strategy)

    def test_hunger_search_retains_visible_first_scheduling_and_search_budget(self):
        s = state(); s["player"]["vitals"]["hunger"] = 67
        goals = self.hunger_board(search_budget_seconds=10)
        goals = board(goals["tasks"][0], task("grass", "cutgrass", 2))
        now = [0.]; runtime = PriorityBoardExecutor(clock=lambda: now[0]); nav = ExplorationExecutor()
        self.assertEqual(runtime.choose(goals, s, {**NAV, "grass": collect("cutgrass")}, nav)[1], "grass")
        self.assertEqual(runtime.choose(goals, s, NAV, nav)[0]["id"], "food")
        now[0] = 11
        food = next(t for t in runtime.view(goals, s)["tasks"] if t["id"] == "food")
        self.assertEqual(food["blocked_reason"], "search_budget_exhausted")
        # No invented movement when Lua offers only lighting/safety options.
        self.assertIsNone(PriorityBoardExecutor().choose(self.hunger_board(), s,
                          {"equip_torch": {"kind": "equip_torch"}}, ExplorationExecutor()))

    def test_visible_lower_priority_before_missing_higher_and_priority_among_visible(self):
        goals = board(task("gold", "goldnugget"), task("grass", "cutgrass", 2), task("twigs", "twigs", 3))
        actions = {**NAV, "grass": collect("cutgrass"), "twigs": collect("twigs", "sapling")}
        runtime = PriorityBoardExecutor()
        self.assertEqual(runtime.choose(goals, state(), actions, ExplorationExecutor())[1], "grass")
        actions["gold"] = collect("goldnugget", "goldnugget")
        self.assertEqual(runtime.choose(goals, state(), actions, ExplorationExecutor())[1], "gold")

    def test_completed_goals_latch_across_consumption_but_changed_target_reopens(self):
        runtime = PriorityBoardExecutor(); goals = board(task("grass", "cutgrass"), task("torch", "torch", count=1, depends_on=["grass"]))
        runtime.view(goals, state(cutgrass=2))
        self.assertEqual(runtime.choose(goals, state(), {"craft_torch": {"kind": "craft_torch"}}, ExplorationExecutor())[1], "craft_torch")
        review = merge_goals(goals, board(task("grass", "cutgrass", count=4)))
        self.assertEqual(runtime.view(review, state())["tasks"][0]["status"], "ready")

    def test_navigation_after_completed_tool_dependency_uses_full_board_then_single_goal(self):
        goals=board(task("pickaxe","pickaxe",count=1), task("gold","goldnugget",depends_on=["pickaxe"]))
        runtime=PriorityBoardExecutor(); s=state(pickaxe=1)
        choice=runtime.choose(goals,s,NAV,ExplorationExecutor())
        self.assertEqual(choice[0]["id"],"gold")
        self.assertEqual(choice[1],"explore_other_region")

    def test_quarter_and_day_never_expire_priority_goals(self):
        goals = board(task("grass", "cutgrass")); s = state()
        s["world"].update(day=3, time=.9)
        self.assertEqual(PriorityBoardExecutor().view(goals, s)["status"], "active")

    def test_new_batch_can_request_same_stock_again_but_review_does_not_rewind(self):
        goals = board(task("grass", "cutgrass")); runtime = PriorityBoardExecutor()
        runtime.adopt(goals); runtime.view(goals, state(cutgrass=2))
        runtime.adopt(goals)
        self.assertEqual(runtime.view(goals,state())["status"],"completed")
        runtime.adopt(goals,new_batch=True)
        self.assertEqual(runtime.view(goals,state())["status"],"active")

    def test_prediction_does_not_latch_completion_or_start_survey(self):
        goals = board(task("grass", "cutgrass")); s=state(cutgrass=2)
        s["_jev_execution_context"]={"predicted_success":True}
        runtime=PriorityBoardExecutor()
        self.assertEqual(runtime.view(goals,s)["status"],"active")
        self.assertFalse(runtime.records["grass"]["done"])
        self.assertEqual(runtime.view(goals,state())["status"],"active")

    def test_survey_means_additional_area_and_navigates_only_current_region(self):
        goals=validate_goals({"goal":"Survey","tasks":[{"id":"survey","title":"Survey",
            "target":{"type":"survey_region","node_id":"A:FlowerPatch","area":64}}]},1)
        s=state(); s["navigation"]["graph"]["current"]["explored_area"]=1000
        runtime=PriorityBoardExecutor(); nav=ExplorationExecutor()
        self.assertEqual(runtime.view(goals,s)["status"],"active")
        self.assertEqual(runtime.choose(goals,s,NAV,nav)[1],"explore_current_region")
        self.assertEqual(nav.strategy,"board_survey")
        s["navigation"]["graph"]["current"]["explored_area"]=1064
        self.assertEqual(runtime.view(goals,s)["status"],"completed")

    def test_long_graph_transit_does_not_consume_local_room_survey_budget(self):
        now=[0.]; runtime=PriorityBoardExecutor(clock=lambda:now[0]); nav=ExplorationExecutor()
        s=state(); goals=board(task("grass","cutgrass",search_budget_seconds=120))
        s["navigation"]["graph"]["routes"]=[{"node_id":"B:BGGrass","hops":2,"distance":20,"entered":True}]
        actions={**NAV,"travel_B":{"kind":"explore","mode":"graph","node_id":"B:BGGrass"}}
        self.assertEqual(runtime.choose(goals,s,actions,nav)[1],"travel_B")
        now[0]=40; runtime.view(goals,s)
        runtime.choose(goals,s,actions,nav)
        self.assertNotIn(("A:FlowerPatch","cutgrass"),nav.source_failures)
        self.assertEqual(runtime.room_search[("grass","A:FlowerPatch")]["seconds"],0)

    def test_visit_requires_current_region_not_historical_entered(self):
        goals = validate_goals({"goal": "Visit", "tasks": [{"id": "visit", "title": "visit", "target": {"type": "visit_region", "node_id": "B"}}]}, 1)
        s = state(); s["navigation"]["graph"]["routes"] = [{"node_id": "B", "entered": True}]
        self.assertNotEqual(PriorityBoardExecutor().view(goals, s)["status"], "completed")

    def test_invalid_dependencies_torch_reserve_and_budget_rejected(self):
        for tasks in ([task("torch", "torch", count=2)], [task("x", "cutgrass", depends_on=["missing"])],
                      [task("x", "cutgrass", search_budget_seconds=float("nan"))],
                      [task("x", "cutgrass", search_budget_distance=-1)]):
            with self.subTest(tasks=tasks), self.assertRaises(ValueError):
                board(*tasks)

    def test_review_preserves_omitted_goals_and_renamed_semantic_goal(self):
        old = board(task("grass", "cutgrass"), task("gold", "goldnugget"))
        reviewed = merge_goals(old, board(task("renamed", "cutgrass", 2)))
        self.assertEqual({t["id"] for t in reviewed["tasks"]}, {"grass", "gold"})
        raw = {"goal": "Cancel gold because unreachable", "tasks": [task("grass", "cutgrass")], "cancel_task_ids": ["gold"]}
        self.assertEqual(len(merge_goals(reviewed, validate_goals(raw, 1))["tasks"]), 1)

    def test_source_route_failure_does_not_block_goal_and_skips_failed_choice(self):
        runtime = PriorityBoardExecutor(); goals = board(task("grass", "cutgrass")); nav = ExplorationExecutor(); s = state()
        chosen = runtime.choose(goals, s, NAV, nav)
        runtime.on_result("grass", chosen[1], chosen[2], "failed", "action_unavailable", nav, s)
        self.assertEqual(runtime.view(goals, s)["status"], "active")
        retry = runtime.choose(goals, s, NAV, nav)
        self.assertTrue(retry is None or retry[1] != chosen[1])

    def test_search_budget_counts_only_navigation_not_other_work_or_safety(self):
        now = [0.]; runtime = PriorityBoardExecutor(clock=lambda: now[0]); s = state()
        goals = board(task("grass", "cutgrass", search_budget_seconds=10))
        runtime.choose(goals, s, NAV, ExplorationExecutor()); now[0] = 6
        runtime.view(goals, s); runtime.pause_accounting(); now[0] = 100
        self.assertEqual(runtime.view(goals, s)["status"], "active")
        runtime.choose(goals, s, NAV, ExplorationExecutor()); now[0] = 105
        self.assertEqual(runtime.view(goals, s)["status"], "blocked")
        runtime.adopt(goals)
        review = copy.deepcopy(goals); review["tasks"][0]["reason"] = "Try an alternate known room"
        runtime.adopt(review)
        self.assertEqual(runtime.view(review, s)["status"], "active")

    def test_dusk_campfire_unavailable_is_not_whole_board_failure(self):
        goals = validate_goals({"goal": "Light", "tasks": [{"id": "fire", "title": "fire", "target": {"type": "structure", "prefab": "campfire"}}, task("grass", "cutgrass", 2)]}, 1)
        runtime = PriorityBoardExecutor()
        self.assertEqual(runtime.choose(goals, state(), {"grass": collect("cutgrass")}, ExplorationExecutor())[1], "grass")

    def test_priors_route_to_known_room_without_previously_observed_resource(self):
        s = state(); graph = s["navigation"]["graph"]
        graph["routes"] = [{"node_id": "B:BGGrass", "hops": 1, "distance": 6, "entered": True}]
        actions = {**NAV, "travel_grass": {"kind": "explore", "mode": "graph", "node_id": "B:BGGrass"}}
        nav = ExplorationExecutor()
        route = nav.choose(s, dispatch_for_navigation(actions, s), board(task("grass", "cutgrass")))
        self.assertEqual(route[0], "travel_grass")
        self.assertEqual(nav.strategy, "worldgen_source_bfs")

    def test_observed_source_beats_worldgen_prior_and_tool_gate_remains(self):
        s = state(pickaxe=1); graph = s["navigation"]["graph"]
        graph["routes"] = [{"node_id": "B:BGRocky", "hops": 1, "distance": 6},
                           {"node_id": "C:Unknown", "hops": 2, "distance": 8, "resources_seen": ["rock2"]}]
        actions = {**NAV, **{f"travel_{n}": {"kind": "explore", "mode": "graph", "node_id": n} for n in ("B:BGRocky", "C:Unknown")}}
        nav = ExplorationExecutor()
        self.assertEqual(nav.choose(s, dispatch_for_navigation(actions, s), board(task("gold", "goldnugget")))[0], "travel_C:Unknown")
        self.assertEqual(nav.strategy, "known_source_bfs")
        del s["player"]["inventory"]["items"][:]
        self.assertFalse(expected_sources({"id": "B:BGRocky"}, {"grass"}))

    def test_concrete_remembered_source_precedes_frontier_and_is_goal_specific(self):
        s = state(); actions = {**NAV, "remember": {"kind": "explore", "mode": "resource", "prefab": "grass", "resource_guid": 42, "node_id": "A", "distance": 20}}
        nav = ExplorationExecutor()
        self.assertEqual(nav.choose(s, dispatch_for_navigation(actions, s), board(task("grass", "cutgrass")))[0], "remember")
        self.assertEqual(nav.strategy, "remembered_source_position")
        other = nav.choose(s, dispatch_for_navigation(actions, s), board(task("twigs", "twigs")))
        self.assertNotEqual(other[0], "remember")

    def test_searching_one_resource_does_not_ban_room_for_other_resources_or_transit(self):
        nav = ExplorationExecutor(); s = state()
        nav.exhaust_source("B:BGGrass", "cutgrass", {}, "test_budget")
        self.assertNotIn("B:BGGrass", nav.closed)
        self.assertFalse(nav._prior_source("B:BGGrass", "cutgrass", {"grass"}, {}))
        self.assertTrue(nav._prior_source("B:BGGrass", "twigs", {"sapling"}, {"node_id": "B:BGGrass"}))

    def test_extracted_snapshot_is_room_specific_and_weights_are_not_probabilities(self):
        self.assertIn("grass", room_resources()["BGGrass"])
        self.assertNotIn("grass", room_resources()["FlowerPatch"])
        self.assertIn("rock2", room_resources()["BGRocky"])
        sample = '''-- AddRoom("Fake", {})
        AddRoom("Test", {contents={distributeprefabs={grass=.2, rock1=0,
          tree={weight=1,prefabs={"evergreen","deciduoustree"}}},
          countprefabs={goldnugget=1,unknown=function() return math.random(0,3) end}}})'''
        self.assertEqual(extract(sample), {"Test": ["deciduoustree", "evergreen", "goldnugget", "grass"]})


class PendingWorker:
    def __init__(self, **_): self.calls = []; self.futures = []
    def submit(self, _fn, *args):
        self.calls.append(args); f = Future(); self.futures.append(f); return f
    def shutdown(self, **_): pass


class ReviewTests(unittest.TestCase):
    def planner(self):
        with patch.object(daily_planner, "ThreadPoolExecutor", PendingWorker):
            p = daily_planner.DailyPlanner("offline", board_log_dir=None)
        self.addCleanup(p.close)
        return p

    def test_late_quarter_response_accepted_and_running_goals_preserved_during_review(self):
        p = self.planner(); s = state(); s["world"]["time"] = .24
        p.update(s)
        s["world"]["time"] = .26
        p.executor.futures[0].set_result((board(task("grass", "cutgrass")), 1., None))
        with patch.object(daily_planner.time, "monotonic", return_value=10000):
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
        p.goal_retry_at = daily_planner.time.monotonic() + 100
        result = p.update(state(cutgrass=2))
        self.assertEqual(result["status"], "completed")
        self.assertEqual(p.executor.calls[-1][4]["replan_reason"], "all_tasks_completed")
        self.assertEqual(p.executor.calls[-1][4]["current_task_board"]["tasks"][0]["status"], "done")

    def test_unavailable_board_review_is_immediate_deduplicated_and_keeps_error_backoff(self):
        p = self.planner(); p.plan = board({"id":"campfire","title":"Night fire",
            "target":{"type":"structure","prefab":"campfire"}})
        p.requested_slot = (1, 0)
        p.goal_retry_at = 999
        with patch.object(daily_planner.time, "monotonic", return_value=100):
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
        with patch.object(daily_planner.time, "monotonic", return_value=116):
            p.update(state())
        self.assertEqual(len(p.executor.calls), 2)
        self.assertIsNotNone(p.plan)

    def test_empty_runtime_board_requests_evaluation_instead_of_waiting_for_quarter(self):
        p = self.planner(); p.plan = {**board(task("grass", "cutgrass")), "tasks": []}
        p.requested_slot = (1, 0)
        p.goal_retry_at = daily_planner.time.monotonic() + 100
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
