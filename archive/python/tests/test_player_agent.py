"""Autonomous MCP host regression tests: fake game and models, no keys/inputs."""
import asyncio
import copy
import threading
import time
import unittest
from concurrent.futures import Future
from types import SimpleNamespace
from unittest.mock import patch

from mcp_server.player_bridge import GameBridge, BridgeError
from mcp_agent.player_agent import MCPAgent, MCPRejected, PlayerDead, SessionChanged, IncompatibleBridge
from mcp_agent.model_state import model_game_state
from mcp_agent.priority_board import validate_goals
from mcp_agent.player_planner import DailyPlanner
from common import timing_log as timing


def snapshot():
    return {"world": {"day": 1, "phase": "day"},
            "player": {"guid": 1, "position": {"x": 0, "z": 0},
                       "vitals": {"health": 150, "hunger": 100, "hunger_max": 150},
                       "inventory": {"items": [], "equipped": []}},
            "nearby": [], "navigation": {"graph": {"current_node": "A"}},
            "bridge": {"enabled": True, "protocol": 4, "events": [], "executor_busy": False,
                       "board_revision": 0, "board_auto_execute": False, "board_ready": False,
                       "available_actions": {"collect_grass": {"kind": "collect", "guid": 20,
                                                "prefab": "grass", "product": "cutgrass"},
                                             "wait": {"kind": "wait"}}}}


def board():
    return {"kind": "task_board", "board_id": "planner:1", "day": 1,
            "goal": "Get grass", "tasks": [{"id": "grass", "priority": 1,
                "title": "Get grass", "depends_on": [],
                "target": {"type": "inventory", "prefab": "cutgrass", "count": 2}}]}


class FakePlanner:
    def __init__(self, plan=None):
        self.plan, self.updates, self.events = plan, 0, []

    def update(self, _state):
        self.updates += 1
        return self.plan

    def record_action(self, _state, choice, outcome):
        self.events.append((choice, outcome))


class FakeNavigator:
    def __init__(self, choice=None, handoff=False):
        self.choice, self.handoff, self.strategy = choice, handoff, "offline_route"
        self.events = []

    def choose(self, _s, actions, _p):
        return (self.choice, actions[self.choice]) if self.choice in actions else None

    def after_local_action(self, choice):
        self.handoff = choice != "wait"

    def on_event(self, event, _state):
        self.events.append(event)


class FakeClient:
    def __init__(self):
        self.state, self.calls, self.sequence = snapshot(), [], 0
        self.auto_complete = True
        self.session, self.ack, self.hook = "offline-world", True, None
        self.bridge = GameBridge("offline-token-" * 4, clock=lambda: self.sequence * 0.01)

    async def call_tool(self, name, arguments):
        self.calls.append((name, copy.deepcopy(arguments)))
        if name == "get_available_actions":
            if self.ack:
                self.state["bridge"].update(board_revision=self.bridge.board_revision,
                    board_auto_execute=bool(self.bridge.board and self.bridge.board["auto_execute"]))
            if self.hook:
                self.hook(self)
            self.sequence += 1
            report = None
            if self.bridge.active and self.auto_complete:
                report = {"action_id": self.bridge.active, "status": "completed", "reason": "observed_effect"}
            self.bridge.exchange({"token": self.bridge.token, "session_id": self.session,
                                  "sequence": self.sequence, "state": self.state, "action_report": report})
        if name in {"get_action_status", "cancel_action"}:
            aid = arguments["action_id"]
            record = self.bridge.actions[aid]
            if record["status"] not in {"completed", "cancelled"} and (name=="cancel_action" or self.auto_complete):
                status = "cancelled" if name == "cancel_action" else "completed"
                self.sequence += 1
                self.bridge.exchange({"token": self.bridge.token, "session_id": self.session,
                    "sequence": self.sequence, "state": self.state,
                    "action_report": {"action_id": aid, "status": status, "reason": "observed_effect"}})
        try:
            # This fake peer uploads a fresh snapshot on every read already.
            # Real refresh handshakes are exercised by bridge/SDK tests.
            if name == "get_available_actions":
                arguments = {k: v for k, v in arguments.items() if k != "refresh"}
            value = getattr(self.bridge, name)(**arguments)
            return SimpleNamespace(is_error=False, structured_content=value)
        except BridgeError as exc:
            return SimpleNamespace(is_error=True, content=[SimpleNamespace(text=str(exc))])


class AgentTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = FakeClient()
        self.agents = []

    def agent(self, **options):
        instance = MCPAgent(self.client, poll=0.001, navigator=options.pop("navigator", FakeNavigator()), **options)
        self.agents.append(instance)
        return instance

    async def asyncTearDown(self):
        for agent in self.agents:
            await agent.shutdown()

    async def test_atomic_state_shared_projection_and_dry_run_is_read_only(self):
        agent = self.agent()
        await agent.cycle()
        self.assertEqual({name for name, _ in self.client.calls}, {"get_available_actions"})
        self.assertEqual(agent.packet["state"]["bridge"]["available_actions"], agent.packet["actions"])
        facts = model_game_state(agent.packet["state"])
        self.assertEqual(facts["control"]["execution_owner"], "lua")
        self.assertEqual(facts["control"]["available_actions"], agent.packet["actions"])

    async def test_native_execution_records_once_and_polls_planner(self):
        planner = FakePlanner()
        agent = self.agent(planner=planner, execute=True)
        packet = await agent.observe()
        await agent.perform("collect_grass", packet)
        starts = [args for name, args in self.client.calls if name == "start_action"]
        self.assertEqual(len(starts), 1)
        self.assertEqual(starts[0]["choice"], "collect_grass")
        self.assertEqual(planner.events, [("collect_grass", "executed")])
        self.assertGreaterEqual(planner.updates, 2)
        self.assertIsNone(agent.active_id)
        agent.record({"action_id": next(iter(agent.seen_actions)), "choice": "collect_grass", "status": "completed"}, self.client.state)
        self.assertEqual(len(planner.events), 1)


    async def test_lua_board_ack_then_pause(self):
        planner = FakePlanner(board())
        agent = self.agent(planner=planner, execute=True)
        await agent.cycle()
        modes = [args["auto_execute"] for name, args in self.client.calls if name == "submit_task_board"]
        self.assertEqual(modes, [True, False])
        self.assertTrue(agent.board_acked)
        self.assertFalse(agent.auto_enabled)
        await agent.cycle()
        self.assertEqual([args["auto_execute"] for name, args in self.client.calls if name == "submit_task_board"], modes)

    async def test_legacy_board_runs_many_actions(self):
        planner = FakePlanner(board())
        count = 0
        def tick(client):
            nonlocal count
            b = client.state["bridge"]
            if b["board_auto_execute"] and count < 2:
                count += 1
                b["events"].append({"action_id": f"auto:{count}", "choice": "collect_grass",
                                      "status": "completed", "reason": "observed_effect"})
                b["board_ready"] = count < 2
            if count == 2:
                planner.plan = None
                b["available_actions"] = {"wait": {"kind": "wait"}}
        self.client.hook = tick
        agent = self.agent(planner=planner, execute=True)
        await agent.cycle()
        self.assertEqual(count, 2)
        self.assertEqual(planner.events, [("collect_grass", "executed")] * 2)
        self.assertFalse(agent.auto_enabled)

    async def test_priority_board_executes_visible_lower_goal_without_lua_board_upload(self):
        plan = validate_goals({"goal":"Get gold and grass","tasks":[
            {"id":"gold","title":"Gold","priority":1,"target":{"type":"inventory","prefab":"goldnugget","count":1}},
            {"id":"grass","title":"Grass","priority":2,"target":{"type":"inventory","prefab":"cutgrass","count":2}}]}, 1)
        plan["board_id"] = "goals:1"
        agent = self.agent(planner=FakePlanner(plan), execute=True)
        await agent.cycle(); await agent.cycle()
        self.assertEqual([a["choice"] for n,a in self.client.calls if n=="start_action"], ["collect_grass"]*2)
        self.assertFalse(any(n=="submit_task_board" for n,_ in self.client.calls))

    async def test_priority_board_builds_science_machine_then_records_normalized_real_entity(self):
        plan=validate_goals({"goal":"Research","tasks":[{"id":"science","title":"Science Machine",
            "target":{"type":"structure","prefab":"sciencemachine"}}]},1)
        plan["board_id"]="science:1"
        self.client.state["bridge"]["available_actions"]={"build_sciencemachine":{"kind":"build_sciencemachine", "planner_only":True}}
        planner=FakePlanner(plan)
        def replicate(client):
            if client.bridge.active:
                client.state["nearby"]=[{"prefab":"sciencemachine","game_prefab":"researchlab","guid":42}]
        self.client.hook=replicate
        agent=self.agent(planner=planner,execute=True)
        await agent.cycle()
        self.assertEqual([a["choice"] for n,a in self.client.calls if n=="start_action"],["build_sciencemachine"])
        self.assertEqual(planner.events[-1],("build_sciencemachine","executed"))
        self.assertEqual(agent.goals.view(plan,self.client.state)["tasks"][0]["status"],"done")
        self.client.state["nearby"]=[]
        self.assertEqual(agent.goals.view(plan,self.client.state)["tasks"][0]["status"],"done")


    async def test_priority_board_navigation_never_requests_off_board_opportunities(self):
        plan = validate_goals({"goal":"Gold","tasks":[{"id":"gold","title":"Gold","target":{"type":"inventory","prefab":"goldnugget","count":1}}]}, 1)
        plan["board_id"] = "goals:1"
        self.client.state["bridge"]["available_actions"]["explore_other_region"] = {"kind":"explore","mode":"search","x":4,"z":0}
        agent = self.agent(planner=FakePlanner(plan), execute=True, navigator=FakeNavigator("explore_other_region"))
        await agent.cycle()
        self.client.state["bridge"]["available_actions"]["collect_berries"] = {
            "kind":"collect", "prefab":"berries", "product":"berries", "guid":30}
        await agent.cycle()
        self.client.state["bridge"]["available_actions"].pop("collect_grass")
        await agent.cycle()
        self.assertEqual([a["choice"] for n,a in self.client.calls if n=="start_action"], ["explore_other_region"]*3)
        for name, arguments in self.client.calls:
            if name == "start_action":
                self.assertEqual(arguments["navigation_targets"], [{"type":"inventory","prefab":"goldnugget","count":1}])
        self.assertIn("wait", self.client.state["bridge"]["available_actions"])

    async def test_resource_event_reschedules_newly_visible_board_work(self):
        plan = validate_goals({"goal":"Gold","tasks":[{"id":"gold","title":"Gold","target":{"type":"inventory","prefab":"goldnugget","count":1}}]}, 1)
        plan["board_id"] = "goals:1"
        replacement = validate_goals({"goal":"Grass","tasks":[{"id":"grass","title":"Grass","target":{"type":"inventory","prefab":"cutgrass","count":2}}]}, 1)
        replacement["board_id"] = "goals:2"
        planner = FakePlanner(plan)
        self.client.state["bridge"]["available_actions"]["explore_other_region"] = {"kind":"explore","mode":"search","x":4,"z":0}
        agent = self.agent(planner=planner, execute=True, navigator=FakeNavigator("explore_other_region"))
        await agent.cycle()
        planner.plan = replacement
        await agent.cycle()
        self.assertEqual(agent.plan["board_id"], "goals:2")
        self.assertEqual([a["choice"] for n,a in self.client.calls if n=="start_action"],
                         ["explore_other_region", "collect_grass"])

    async def test_changed_board_cancels_continuous_navigation_and_waits_for_ack_before_rescheduling(self):
        plan = validate_goals({"goal":"Gold","tasks":[{"id":"gold","title":"Gold",
            "target":{"type":"inventory","prefab":"goldnugget","count":1}}]},1)
        plan["board_id"]="goals:1"
        replacement = validate_goals({"goal":"Grass","tasks":[{"id":"grass","title":"Grass",
            "target":{"type":"inventory","prefab":"cutgrass","count":2}}]},1)
        replacement["board_id"]="goals:2"
        planner=FakePlanner(plan); self.client.auto_complete=False
        self.client.state["bridge"]["available_actions"]["explore_other_region"]={"kind":"explore","mode":"search","x":4,"z":0}
        def review(client):
            if client.bridge.active:
                planner.plan=replacement
        self.client.hook=review
        agent=self.agent(planner=planner,
                         execute=True,navigator=FakeNavigator("explore_other_region"))
        await asyncio.wait_for(agent.cycle(),1)
        self.assertIsNone(agent.active_id)
        self.assertEqual(next(iter(self.client.bridge.actions.values()))["status"],"cancelled")
        self.assertEqual(sum(n=="cancel_action" for n,_ in self.client.calls),1)
        self.client.auto_complete=True
        await agent.cycle()
        self.assertEqual([a["choice"] for n,a in self.client.calls if n=="start_action"],
                         ["explore_other_region","collect_grass"])

    async def test_blocked_focus_waits_without_unrelated_navigation(self):
        plan = validate_goals({"goal":"Light first", "tasks":[
            {"id":"torch", "title":"Torch", "focus":True, "target":{"type":"inventory", "prefab":"torch", "count":1}},
            {"id":"grass", "title":"Grass", "target":{"type":"inventory", "prefab":"cutgrass", "count":2}}]}, 1)
        plan["board_id"] = "focus:1"
        self.client.state["bridge"]["available_actions"]["explore_other_region"] = {"kind":"explore", "mode":"search", "x":4, "z":0}
        agent = self.agent(planner=FakePlanner(plan), execute=True, navigator=FakeNavigator("explore_other_region"))
        with timing.CycleTrace(1, None, emit_console=False) as trace:
            await agent.cycle()
        self.assertEqual(trace.record["decision_source"], "waiting_for_board")
        self.assertFalse(any(n == "start_action" for n, _ in self.client.calls))

    async def test_focus_navigation_targets_exclude_visible_ordinary_grass(self):
        plan = validate_goals({"goal":"Gold first", "tasks":[
            {"id":"gold", "title":"Gold", "focus":True, "target":{"type":"inventory", "prefab":"goldnugget", "count":1}},
            {"id":"grass", "title":"Grass", "target":{"type":"inventory", "prefab":"cutgrass", "count":2}}]}, 1)
        plan["board_id"] = "focus:1"
        self.client.state["bridge"]["available_actions"]["explore_other_region"] = {"kind":"explore", "mode":"search", "x":4, "z":0}
        agent = self.agent(planner=FakePlanner(plan),
                           execute=True, navigator=FakeNavigator("explore_other_region"))
        await agent.cycle()
        starts = [a for n, a in self.client.calls if n == "start_action"]
        self.assertEqual(starts[0]["choice"], "explore_other_region")
        self.assertEqual(starts[0]["navigation_targets"], [{"type":"inventory", "prefab":"goldnugget", "count":1}])

    async def test_priority_board_safety_preempts_visible_work_then_resumes(self):
        plan = validate_goals({"goal":"Grass","tasks":[{"id":"grass","title":"Grass","target":{"type":"inventory","prefab":"cutgrass","count":2}}]}, 1)
        plan["board_id"] = "goals:1"
        self.client.state["nearby"] = [{"prefab":"hound","activeThreat":True}]
        self.client.state["bridge"]["available_actions"] = {"flee_from_nearest_hostile":{"kind":"flee_from_nearest_hostile"}}
        agent = self.agent(planner=FakePlanner(plan), execute=True)
        await agent.cycle()
        self.client.state["nearby"] = []
        self.client.state["bridge"]["available_actions"] = snapshot()["bridge"]["available_actions"]
        await agent.cycle()
        self.assertEqual([a["choice"] for n,a in self.client.calls if n=="start_action"], ["flee_from_nearest_hostile","collect_grass"])

    async def test_priority_board_native_failure_keeps_goal(self):
        plan = validate_goals({"goal":"Grass","tasks":[{"id":"grass","title":"Grass","target":{"type":"inventory","prefab":"cutgrass","count":2}}]}, 1)
        plan["board_id"] = "goals:1"
        def tick(client):
            if client.bridge.active:
                client.sequence += 1
                client.bridge.exchange({"token":client.bridge.token,"session_id":client.session,"sequence":client.sequence,
                    "state":client.state,"action_report":{"action_id":client.bridge.active,"status":"failed","reason":"action_unavailable"}})
        self.client.hook = tick
        agent = self.agent(planner=FakePlanner(plan), execute=True)
        await agent.cycle(); await agent.cycle()
        self.assertEqual(agent.goals.view(plan,self.client.state)["status"], "active")
        self.assertEqual(sum(n=="start_action" for n,_ in self.client.calls),1)


    async def test_idle_boards_request_ds_before_quarter_and_do_not_loop(self):
        scheduled = {"id":"later", "title":"Later", "start_at":{"day":1,"progress":.9},
                     "target":{"type":"inventory","prefab":"twigs","count":2}}
        blocked = {"id":"regions", "title":"Regions",
                   "target":{"type":"known_regions","count":3}}
        for tasks, reason in (([scheduled], "only_scheduled_tasks"),
                              ([blocked], "search_or_prerequisite_blocked"),
                              ([scheduled, blocked], "no_actionable_tasks")):
            with self.subTest(reason=reason):
                planner = DailyPlanner("offline", board_log_dir=None)
                self.addCleanup(planner.close)
                plan = validate_goals({"goal":"Idle", "tasks":copy.deepcopy(tasks)}, 1)
                planner.plan = {**plan, "board_id":"idle:1"}
                planner.requested_slot = (1, 0); planner.goal_retry_at = time.monotonic() + 100
                pending = Future()
                agent = self.agent(planner=planner, execute=True)
                with patch.object(planner.executor, "submit", return_value=pending) as submit:
                    await agent.cycle()
                    self.assertEqual(submit.call_args.args[5]["replan_reason"], reason)
                    pending.set_result((copy.deepcopy(plan), .5, None))
                    await agent.cycle(); await agent.cycle()
                self.assertEqual(submit.call_count, 1)
                self.assertTrue(all(t["status"] in {"scheduled", "blocked"} for t in agent.plan["tasks"]))

    async def test_rejected_action_is_not_replaced_with_input_fallback(self):
        agent = self.agent(execute=True)
        packet = await agent.observe()
        self.client.bridge.state["player"]["guid"] = 99
        with self.assertRaises(MCPRejected):
            await agent.perform("collect_grass", packet)
        self.assertIsNone(agent.active_id)

    async def test_board_ack_timeout_retries_handshake_without_competing_work(self):
        agent = self.agent(execute=True)
        self.client.ack = False
        clock = [0]
        async def advance(_):
            clock[0] += 5
        with patch("mcp_agent.player_agent.time.monotonic", side_effect=lambda: clock[0]), patch("mcp_agent.player_agent.asyncio.sleep", advance):
            with self.assertRaises(MCPRejected):
                await agent.sync_board(board(), True)
            self.assertFalse(agent.board_acked)
            self.client.ack = True
            await agent.sync_board(board(), True)
        self.assertTrue(agent.board_acked)
        self.assertEqual(sum(n == "submit_task_board" for n, _ in self.client.calls), 1)

    async def test_death_old_protocol_and_session_changes_stop(self):
        agent = self.agent()
        await agent.observe()
        self.client.state["player"]["vitals"]["dead"] = True
        with self.assertRaises(PlayerDead):
            await agent.cycle()
        self.client.state["player"]["vitals"]["dead"] = False
        self.client.state["bridge"]["protocol"] = 1
        with self.assertRaises(IncompatibleBridge):
            await agent.observe()
        self.client.state["bridge"]["protocol"] = 4
        agent.session = "other-world"
        with self.assertRaises(SessionChanged):
            await agent.observe()

    async def test_shutdown_cancels_active_native_action_and_waits_for_ack(self):
        agent = self.agent(execute=True)
        packet = await agent.observe()
        result = await agent.rpc("start_action", choice="wait", observation_id=packet["observation_id"])
        agent.active_id = result["action_id"]
        await agent.shutdown()
        self.assertIsNone(agent.active_id)
        self.assertEqual(self.client.bridge.actions[result["action_id"]]["status"], "cancelled")

    async def test_completion_and_facts_are_polled_once_without_status_read_race(self):
        agent = self.agent(execute=True)
        packet = await agent.observe()
        with timing.CycleTrace(1, emit_console=False) as trace:
            await agent.perform("collect_grass", packet)
        self.assertFalse(any(name == "get_action_status" for name, _ in self.client.calls))
        marks = trace.record["events"]
        self.assertEqual([m["stage"] for m in marks], ["action.command_submitted",
                         "action.command_queued", "action.terminal_observed"])
        self.assertEqual(marks[1]["action_id"], marks[2]["action_id"])
        self.assertLessEqual(marks[1]["at_ms"], marks[2]["at_ms"])

    async def test_handoff_timings_keep_lua_and_host_clock_domains_separate(self):
        agent = self.agent()
        with timing.CycleTrace(1, emit_console=False) as trace:
            agent.record({"action_id": "a", "choice": "collect_grass", "status": "completed",
                "started_at": 10, "confirmed_started_at": 10.3, "finished_at": 12,
                "handoff_gap_ms": 450, "previous_action_id": "previous",
                "timing": {"queue_to_delivery_ms": 150, "terminal_cache_ms": 20}},
                {"bridge": {"sampled_at": 12.2}})
        mark = trace.record["events"][0]
        self.assertEqual(mark["lua_execution_ms"], 2000)
        self.assertEqual(mark["lua_handoff_gap_ms"], 450)
        self.assertEqual(mark["lua_finish_to_exchange_ms"], 200)
        self.assertEqual(mark["queue_to_delivery_ms"], 150)
        self.assertEqual(mark["terminal_cache_ms"], 20)
        self.assertLess(mark["at_ms"], 1000)  # Never subtract Lua time from host time.

if __name__ == "__main__":
    unittest.main()
