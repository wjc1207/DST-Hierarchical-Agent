"""Autonomous MCP host regression tests: fake game and models, no keys/inputs."""
import asyncio
import copy
import threading
import time
import unittest
from concurrent.futures import Future
from types import SimpleNamespace
from unittest.mock import patch

from mcp_server.game_bridge import GameBridge, BridgeError
from mcp_agent.mcp_agent import MCPAgent, MCPRejected, PlayerDead, SessionChanged, IncompatibleBridge
from mcp_agent.mcp_agent import dispatch_for_navigation
from mcp_agent.model_state import model_game_state
from mcp_agent.action_sequence import validate_sequence
from mcp_agent.priority_board import validate_goals
from mcp_agent.jev_client import CONTINUE_MAIN_TASK
from mcp_agent.daily_planner import DailyPlanner
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


def answer(_s, criteria, _plan):
    choice = "collect_grass" if "collect_grass" in criteria else next(iter(criteria))
    return {"answer": {"choice": choice, "confidence": 0.1}, "response": {"model": "offline"}}


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

    def agent(self, decide=answer, **options):
        instance = MCPAgent(self.client, decide, poll=0.001, navigator=options.pop("navigator", FakeNavigator()), **options)
        self.agents.append(instance)
        return instance

    async def asyncTearDown(self):
        for agent in self.agents:
            await agent.shutdown()

    async def test_atomic_state_shared_projection_and_dry_run_is_read_only(self):
        seen = []
        def decide(s, c, p):
            seen.append((s, c))
            return answer(s, c, p)
        agent = self.agent(decide)
        await agent.cycle()
        self.assertEqual({name for name, _ in self.client.calls}, {"get_available_actions"})
        self.assertEqual(seen[0][0]["bridge"]["available_actions"], agent.packet["actions"])
        facts = model_game_state(seen[0][0])
        self.assertEqual(facts["control"]["execution_owner"], "lua")
        self.assertEqual(facts["control"]["available_actions"], agent.packet["actions"])

    async def test_native_execution_records_once_and_polls_planner(self):
        planner = FakePlanner()
        agent = self.agent(planner=planner, execute=True)
        await agent.cycle()
        starts = [args for name, args in self.client.calls if name == "start_action"]
        self.assertEqual(len(starts), 1)
        self.assertEqual(starts[0]["choice"], "collect_grass")
        self.assertEqual(planner.events, [("collect_grass", "executed")])
        self.assertGreaterEqual(planner.updates, 3)
        self.assertIsNone(agent.active_id)
        agent.record({"action_id": next(iter(agent.seen_actions)), "choice": "collect_grass", "status": "completed"}, self.client.state)
        self.assertEqual(len(planner.events), 1)

    async def test_navigation_is_executor_owned_and_not_jev(self):
        def forbidden(*_):
            raise AssertionError("exploration must not call JEV")
        nav = {"kind": "explore", "mode": "search", "x": 4, "z": 0}
        self.client.state["bridge"]["available_actions"]["explore_other_region"] = nav
        agent = self.agent(forbidden, navigator=FakeNavigator("explore_other_region"))
        await agent.cycle()
        dispatch = dispatch_for_navigation(self.client.state["bridge"]["available_actions"], self.client.state)
        self.assertEqual(dispatch["explore_other_region"]["search_region_id"], "A")

    async def test_lua_board_ack_then_pause_before_manual_decision(self):
        planner = FakePlanner(board())
        def decide(s, c, p):
            self.assertFalse(s["bridge"]["board_auto_execute"])
            self.assertTrue(agent.board_acked)
            return answer(s, c, p)
        agent = self.agent(decide, planner=planner, execute=True)
        await agent.cycle()
        modes = [args["auto_execute"] for name, args in self.client.calls if name == "submit_task_board"]
        self.assertEqual(modes, [True, False])
        await agent.cycle()
        self.assertEqual([args["auto_execute"] for name, args in self.client.calls if name == "submit_task_board"], modes)

    async def test_board_runs_many_actions_without_per_plant_jev_requests(self):
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
        agent = self.agent(lambda *_: self.fail("board work should not ask JEV"), planner=planner, execute=True)
        await agent.cycle()
        self.assertEqual(count, 2)
        self.assertEqual(planner.events, [("collect_grass", "executed")] * 2)
        self.assertFalse(agent.auto_enabled)

    async def test_game_changes_keep_one_request_and_deliver_original_target_to_lua(self):
        for field in ("phase", "guid", "health", "inventory", "threat", "unavailable"):
            self.client = FakeClient()
            started, release = threading.Event(), threading.Event()
            def decide(s, c, p):
                started.set()
                release.wait(2)
                return answer(s, c, p)
            def change(client):
                if started.is_set():
                    if field == "phase":
                        client.state["world"]["phase"] = "night"
                    elif field == "guid":
                        client.state["bridge"]["available_actions"]["collect_grass"]["guid"] = 21
                    elif field == "health":
                        client.state["player"]["vitals"]["health"] -= 1
                    elif field == "inventory":
                        client.state["player"]["inventory"]["items"] = [{"prefab": "cutgrass", "count": 3}]
                    elif field == "threat":
                        client.state["nearby"] = [{"guid": 99, "activeThreat": True}]
                    else:
                        client.state["bridge"]["available_actions"].pop("collect_grass", None)
                    release.set()
            self.client.hook = change
            agent = self.agent(decide, execute=True)
            try:
                with timing.CycleTrace(1, emit_console=False) as trace:
                    await agent.cycle()
            finally:
                release.set()
            starts = [args for n, args in self.client.calls if n == "start_action"]
            self.assertEqual(len(starts), 1, field)
            record = next(iter(self.client.bridge.actions.values()))
            self.assertEqual(record["action"]["guid"], 20, "must not silently switch to GUID 21")
            self.assertNotIn("model_discard_reason", trace.record)
            self.assertEqual(sum(e["stage"] == "decision.submitted" for e in trace.record["events"]), 1)

    async def test_flee_request_survives_repeated_damage_and_changing_hostiles(self):
        started, release = threading.Event(), threading.Event()
        self.client.state["bridge"]["available_actions"] = {"flee_from_nearest_hostile": {
            "kind": "flee_from_nearest_hostile", "x": 6, "z": 0}}
        polls = 0
        def decide(s, criteria, plan):
            started.set()
            release.wait(2)
            return answer(s, criteria, plan)
        def damage(client):
            nonlocal polls
            if started.is_set():
                polls += 1
                client.state["player"]["vitals"]["health"] -= 5
                client.state["nearby"] = [{"guid": 99+polls, "activeThreat": True}]
                if polls >= 3: release.set()
        self.client.hook = damage
        agent = self.agent(decide, execute=True)
        try:
            with timing.CycleTrace(1, emit_console=False) as trace:
                await agent.cycle()
        finally:
            release.set()
        self.assertGreaterEqual(polls, 3)
        self.assertEqual([args["choice"] for n, args in self.client.calls if n == "start_action"],
                         ["flee_from_nearest_hostile"])
        self.assertNotIn("model_discard_reason", trace.record)

    async def test_lifecycle_changes_during_request_never_deliver_an_answer(self):
        for field, error in (("player", SessionChanged), ("session", SessionChanged), ("dead", PlayerDead)):
            self.client = FakeClient()
            started, release = threading.Event(), threading.Event()
            def decide(*args):
                started.set()
                release.wait(2)
                return answer(*args)
            def change(client):
                if not started.is_set(): return
                if field == "player": client.state["player"]["guid"] = 99
                elif field == "session":
                    client.bridge.received = -999
                    client.session = "another-world"
                else: client.state["player"]["vitals"]["dead"] = True
                release.set()
            self.client.hook = change
            agent = self.agent(decide, execute=True)
            try:
                with self.subTest(field=field), self.assertRaises(error): await agent.cycle()
            finally:
                release.set()
            self.assertFalse(any(n == "start_action" for n, _ in self.client.calls))

    async def test_model_cannot_invent_a_choice_outside_its_request(self):
        for choice in ("craft_torch", CONTINUE_MAIN_TASK):
            with self.subTest(choice=choice):
                agent = self.agent(lambda *_, chosen=choice: {"answer": {"choice": chosen}}, execute=True)
                with self.assertRaisesRegex(RuntimeError, "outside the requested options"):
                    await agent.cycle()
                self.assertFalse(any(n == "start_action" for n, _ in self.client.calls))

    async def test_old_actionable_snapshot_refreshes_once_before_decision_not_during_inference(self):
        original = self.client.call_tool
        async def cached(name, arguments):
            result = await original(name, arguments)
            if name == "get_available_actions": result.structured_content["state_age_ms"] = 5000
            return result
        self.client.call_tool = cached
        agent = self.agent(execute=True)
        await agent.cycle()
        refreshes = [args for n, args in self.client.calls if n == "get_available_actions" and args.get("refresh")]
        self.assertEqual(len(refreshes), 1)
        self.assertTrue(any(n == "start_action" for n, _ in self.client.calls))

    async def test_old_wait_only_snapshot_does_not_force_full_upload_on_each_idle_poll(self):
        self.client.state["bridge"]["available_actions"] = {"wait": {"kind": "wait"}}
        original = self.client.call_tool
        async def cached(name, arguments):
            result = await original(name, arguments)
            if name == "get_available_actions": result.structured_content["state_age_ms"] = 15000
            return result
        self.client.call_tool = cached
        agent = self.agent(lambda *_: self.fail("wait-only does not need a model"), execute=True)
        await agent.cycle()
        await agent.cycle()
        self.assertFalse(any(args.get("refresh") for n, args in self.client.calls if n == "get_available_actions"))

    async def test_expired_model_observation_is_not_silently_renewed(self):
        started, release = threading.Event(), threading.Event()
        def decide(*args):
            started.set()
            release.wait(2)
            return answer(*args)
        def expire(client):
            if started.is_set():
                identity = next(iter(client.bridge.observations))
                session, _, owner, actions = client.bridge.observations[identity]
                client.bridge.observations[identity] = (session, -100, owner, actions)
                release.set()
        self.client.hook = expire
        agent = self.agent(decide, execute=True)
        try:
            with self.assertRaisesRegex(MCPRejected, "Observation expired"):
                await agent.cycle()
        finally:
            release.set()
        self.assertFalse(self.client.bridge.actions)

    async def test_wait_handoff_to_navigation_uses_a_new_observation(self):
        started, release = threading.Event(), threading.Event()
        def decide(*_):
            started.set()
            release.wait(2)
            return {"answer": {"choice": "wait"}, "response": {"model": "offline"}}
        def discover(client):
            if started.is_set():
                client.state["bridge"]["available_actions"]["explore_other_region"] = {
                    "kind": "explore", "mode": "search", "x": 4, "z": 0}
                release.set()
        self.client.hook = discover
        agent = self.agent(decide, execute=True,
                           navigator=FakeNavigator("explore_other_region", handoff=True))
        try:
            await agent.cycle()
        finally:
            release.set()
        record = next(iter(self.client.bridge.actions.values()))
        self.assertEqual(record["choice"], "explore_other_region")
        self.assertNotIn("navigation_targets", record["action"], "gap/no-plan navigation retains resource events")

    async def test_pending_planning_does_not_block_fill_gap_action(self):
        planner = FakePlanner(None)
        agent = self.agent(planner=planner, execute=True)
        await agent.cycle()
        self.assertTrue(any(n == "start_action" for n, _ in self.client.calls))
        self.assertFalse(any(n == "submit_task_board" for n, _ in self.client.calls))

    async def test_priority_board_executes_visible_lower_goal_without_jev_or_lua_board_upload(self):
        plan = validate_goals({"goal":"Get gold and grass","tasks":[
            {"id":"gold","title":"Gold","priority":1,"target":{"type":"inventory","prefab":"goldnugget","count":1}},
            {"id":"grass","title":"Grass","priority":2,"target":{"type":"inventory","prefab":"cutgrass","count":2}}]}, 1)
        plan["board_id"] = "goals:1"
        agent = self.agent(lambda *_: self.fail("visible goal must not ask JEV"), planner=FakePlanner(plan), execute=True)
        await agent.cycle(); await agent.cycle()
        self.assertEqual([a["choice"] for n,a in self.client.calls if n=="start_action"], ["collect_grass"]*2)
        self.assertFalse(any(n=="submit_task_board" for n,_ in self.client.calls))

    async def test_priority_board_builds_science_machine_then_records_normalized_real_entity(self):
        plan=validate_goals({"goal":"Research","tasks":[{"id":"science","title":"Science Machine",
            "target":{"type":"structure","prefab":"sciencemachine"}}]},1)
        plan["board_id"]="science:1"
        self.client.state["bridge"]["available_actions"]={"build_sciencemachine":{"kind":"build_sciencemachine"}}
        planner=FakePlanner(plan)
        def replicate(client):
            if client.bridge.active:
                client.state["nearby"]=[{"prefab":"sciencemachine","game_prefab":"researchlab","guid":42}]
        self.client.hook=replicate
        agent=self.agent(lambda *_:self.fail("ready Science Machine goal must execute without JEV"),
                         planner=planner,execute=True)
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
        def decide(*_):
            self.fail("off-board resources must not request JEV")
        agent = self.agent(decide, planner=FakePlanner(plan), execute=True, navigator=FakeNavigator("explore_other_region"))
        await agent.cycle()
        self.client.state["bridge"]["available_actions"]["collect_berries"] = {
            "kind":"collect", "prefab":"berries", "product":"berries", "guid":30}
        await agent.cycle()
        self.client.state["bridge"]["available_actions"].pop("collect_grass")
        await agent.cycle()
        self.assertEqual(agent.request_index, 0)
        self.assertEqual([a["choice"] for n,a in self.client.calls if n=="start_action"], ["explore_other_region"]*3)
        for name, arguments in self.client.calls:
            if name == "start_action":
                self.assertEqual(arguments["navigation_targets"], [{"type":"inventory","prefab":"goldnugget","count":1}])
        self.assertIn("wait", self.client.state["bridge"]["available_actions"])

    async def test_resource_event_reschedules_newly_visible_board_work_without_jev(self):
        plan = validate_goals({"goal":"Gold","tasks":[{"id":"gold","title":"Gold","target":{"type":"inventory","prefab":"goldnugget","count":1}}]}, 1)
        plan["board_id"] = "goals:1"
        replacement = validate_goals({"goal":"Grass","tasks":[{"id":"grass","title":"Grass","target":{"type":"inventory","prefab":"cutgrass","count":2}}]}, 1)
        replacement["board_id"] = "goals:2"
        planner = FakePlanner(plan)
        self.client.state["bridge"]["available_actions"]["explore_other_region"] = {"kind":"explore","mode":"search","x":4,"z":0}
        agent = self.agent(lambda *_: self.fail("board navigation/work must not ask JEV"),
                           planner=planner, execute=True, navigator=FakeNavigator("explore_other_region"))
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
        agent=self.agent(lambda *_:self.fail("board review must not call JEV"),planner=planner,
                         execute=True,navigator=FakeNavigator("explore_other_region"))
        await asyncio.wait_for(agent.cycle(),1)
        self.assertIsNone(agent.active_id)
        self.assertEqual(next(iter(self.client.bridge.actions.values()))["status"],"cancelled")
        self.assertEqual(sum(n=="cancel_action" for n,_ in self.client.calls),1)
        self.client.auto_complete=True
        await agent.cycle()
        self.assertEqual([a["choice"] for n,a in self.client.calls if n=="start_action"],
                         ["explore_other_region","collect_grass"])

    async def test_low_vitals_request_safety_without_carried_food_and_wait_is_not_repeated(self):
        for name, maximum in (("sanity", 200), ("hunger", 150), ("health", 150)):
            with self.subTest(vital=name):
                self.client = FakeClient()
                plan = validate_goals({"goal":"Grass","tasks":[{"id":"grass","title":"Grass",
                    "target":{"type":"inventory","prefab":"cutgrass","count":10}}]}, 1)
                plan["board_id"] = "goals:1"
                self.client.state["player"]["vitals"].update({name:maximum*.29, name+"_max":maximum})
                seen = []
                def decide(_s, criteria, requested):
                    seen.append(requested["handoff_reason"])
                    self.assertIn("wait", criteria)
                    self.assertNotIn(CONTINUE_MAIN_TASK, criteria)
                    return {"answer":{"choice":"wait"},"response":{}}
                agent = self.agent(decide, planner=FakePlanner(plan), execute=True)
                await agent.cycle(); await agent.cycle()
                self.assertEqual(seen, ["safety"])
                self.assertEqual([a["choice"] for n,a in self.client.calls if n=="start_action"],
                                 ["wait", "collect_grass"])
                # A newly reachable remedy and a fresh native damage event both re-arm safety.
                self.client.state["bridge"]["available_actions"]["collect_flower"] = {
                    "kind":"collect","product":"petals","guid":40}
                await agent.cycle()
                agent.goals.needs_safety_decision = True
                await agent.cycle()
                self.assertEqual(seen, ["safety"]*3)
                self.client.state["player"]["vitals"][name] = maximum*.5
                await agent.cycle()
                self.client.state["player"]["vitals"][name] = maximum*.29
                await agent.cycle()
                self.assertEqual(seen, ["safety"]*4)

    async def test_priority_board_safety_preempts_visible_work_then_resumes(self):
        plan = validate_goals({"goal":"Grass","tasks":[{"id":"grass","title":"Grass","target":{"type":"inventory","prefab":"cutgrass","count":2}}]}, 1)
        plan["board_id"] = "goals:1"
        self.client.state["nearby"] = [{"prefab":"hound","activeThreat":True}]
        self.client.state["bridge"]["available_actions"] = {"flee_from_nearest_hostile":{"kind":"flee_from_nearest_hostile"}}
        seen = []
        def decide(_s, _c, p):
            seen.append(p["handoff_reason"])
            return {"answer":{"choice":"flee_from_nearest_hostile"},"response":{}}
        agent = self.agent(decide, planner=FakePlanner(plan), execute=True)
        await agent.cycle()
        self.client.state["nearby"] = []
        self.client.state["bridge"]["available_actions"] = snapshot()["bridge"]["available_actions"]
        await agent.cycle()
        self.assertEqual(seen, ["safety"])
        self.assertEqual([a["choice"] for n,a in self.client.calls if n=="start_action"], ["flee_from_nearest_hostile","collect_grass"])

    async def test_priority_board_native_failure_keeps_goal_and_does_not_call_jev_to_replace_it(self):
        plan = validate_goals({"goal":"Grass","tasks":[{"id":"grass","title":"Grass","target":{"type":"inventory","prefab":"cutgrass","count":2}}]}, 1)
        plan["board_id"] = "goals:1"
        def tick(client):
            if client.bridge.active:
                client.sequence += 1
                client.bridge.exchange({"token":client.bridge.token,"session_id":client.session,"sequence":client.sequence,
                    "state":client.state,"action_report":{"action_id":client.bridge.active,"status":"failed","reason":"action_unavailable"}})
        self.client.hook = tick
        agent = self.agent(lambda *_: self.fail("goal failure must stay local"), planner=FakePlanner(plan), execute=True)
        await agent.cycle(); await agent.cycle()
        self.assertEqual(agent.goals.view(plan,self.client.state)["status"], "active")
        self.assertEqual(sum(n=="start_action" for n,_ in self.client.calls),1)

    async def test_unavailable_board_requests_ds_once_and_jev_acts_without_idle_delay(self):
        planner = DailyPlanner("offline", board_log_dir=None)
        self.addCleanup(planner.close)
        planner.plan = validate_goals({"goal":"Light","tasks":[{"id":"campfire","title":"Fire",
            "target":{"type":"structure","prefab":"campfire"}}]}, 1)
        planner.plan["board_id"] = "goals:fire"
        planner.requested_slot = (1, 0)
        planner.goal_retry_at = time.monotonic() + 100
        pending = Future(); seen = []
        def decide(s, criteria, plan):
            seen.append(plan["handoff_reason"])
            self.assertNotIn(CONTINUE_MAIN_TASK, criteria)
            return answer(s, criteria, plan)
        agent = self.agent(decide, planner=planner, execute=True)
        with patch.object(planner.executor, "submit", return_value=pending) as submit:
            await agent.cycle(); await agent.cycle()
        self.assertEqual(submit.call_count, 1)
        self.assertEqual(submit.call_args.args[5]["replan_reason"], "no_feasible_action_or_route")
        self.assertEqual(seen, ["planning_gap", "planning_gap"])
        self.assertEqual([a["choice"] for n,a in self.client.calls if n=="start_action"], ["collect_grass"]*2)
        self.assertEqual(agent.plan["tasks"][0]["id"], "campfire")

    async def test_ds_ready_during_gap_decision_discards_temporary_choice_and_resumes_board(self):
        planner = DailyPlanner("offline", board_log_dir=None)
        self.addCleanup(planner.close)
        planner.plan = validate_goals({"goal":"Light","tasks":[{"id":"campfire","title":"Fire",
            "target":{"type":"structure","prefab":"campfire"}}]}, 1)
        planner.plan["board_id"] = "goals:fire"
        planner.requested_slot = (1, 0)
        planner.goal_retry_at = time.monotonic() + 100
        replacement = validate_goals({"goal":"Grass","tasks":[{"id":"grass","title":"Grass",
            "target":{"type":"inventory","prefab":"cutgrass","count":2}}]}, 1)
        pending = Future(); started, release = threading.Event(), threading.Event()
        self.client.state["bridge"]["available_actions"]["collect_flower"] = {"kind":"collect","prefab":"flower","product":"petals","guid":30}
        def decide(_s, _c, plan):
            self.assertEqual(plan["handoff_reason"], "planning_gap")
            started.set(); release.wait(2)
            return {"answer":{"choice":"collect_flower"},"response":{}}
        def finish_review(_client):
            if started.is_set() and not pending.done():
                pending.set_result((replacement, 1., None))
                release.set()
        self.client.hook = finish_review
        agent = self.agent(decide, planner=planner, execute=True)
        with patch.object(planner.executor, "submit", return_value=pending) as submit:
            try:
                await agent.cycle()
            finally:
                release.set()
            self.assertFalse(any(n=="start_action" for n,_ in self.client.calls))
            await agent.cycle()
        self.assertEqual(submit.call_count, 1)
        self.assertEqual([a["choice"] for n,a in self.client.calls if n=="start_action"], ["collect_grass"])

    async def test_priority_board_daylight_change_gets_one_safety_choice_before_visible_work(self):
        plan = validate_goals({"goal":"Grass","tasks":[{"id":"grass","title":"Grass","target":{"type":"inventory","prefab":"cutgrass","count":2}}]}, 1)
        plan["board_id"]="goals:1"
        agent=self.agent(lambda *_: self.fail("initial local goal must not call JEV"),planner=FakePlanner(plan),execute=True)
        await agent.cycle()
        seen=[]
        def decide(_s,criteria,p):
            seen.append(p["handoff_reason"])
            self.assertIn("wait", criteria)
            self.assertNotIn(CONTINUE_MAIN_TASK, criteria)
            return {"answer":{"choice":"wait"},"response":{}}
        agent.decide=decide
        self.client.state["world"]["phase"]="dusk"
        await agent.cycle(); await agent.cycle()
        self.assertEqual(seen,["safety"])
        self.assertEqual([a["choice"] for n,a in self.client.calls if n=="start_action"],["collect_grass","wait","collect_grass"])

    async def test_sequence_collects_batch_then_crafts_without_per_item_jev_or_board_sync(self):
        plan = validate_sequence({"goal": "Make torch", "steps": [
            {"id": "grass", "action": "collect", "prefab": "cutgrass", "count": 2},
            {"id": "torch", "action": "craft", "prefab": "torch"}]}, 1)
        plan["sequence_id"] = "sequence:1"
        handled, choices = set(), []
        self.client.state["bridge"]["available_actions"]["craft_torch"] = {"kind": "craft_torch"}
        def tick(client):
            aid = client.bridge.active
            if not aid or aid in handled:
                return
            handled.add(aid)
            choice = client.bridge.actions[aid]["choice"]
            choices.append(choice)
            inventory = client.state["player"]["inventory"]
            if choice == "collect_grass":
                inventory["items"] = [{"prefab": "cutgrass", "count": len(choices)}]
            else:
                inventory["items"] = [{"prefab": "torch", "count": 1}]
        self.client.hook = tick
        agent = self.agent(lambda *_: self.fail("sequence should not call JEV"), planner=FakePlanner(plan), execute=True)
        for _ in range(4):
            await agent.cycle()
        self.assertEqual(choices, ["collect_grass", "collect_grass", "craft_torch"])
        self.assertEqual(agent.sequence.status, "completed")
        self.assertFalse(any(n == "submit_task_board" for n, _ in self.client.calls))

    async def test_sequence_survey_ignores_new_resources_and_reaches_area_goal(self):
        plan = validate_sequence({"goal": "Survey", "steps": [
            {"action": "explore_current_region", "area": 256}]}, 1)
        plan["sequence_id"] = "sequence:survey"
        graph = self.client.state["navigation"]["graph"]
        graph["current"] = {"explored_area": 0}
        actions = self.client.state["bridge"]["available_actions"]
        actions["explore_current_region"] = {"kind": "explore", "mode": "region", "x": 4, "z": 0}
        handled = set()
        def tick(client):
            aid = client.bridge.active
            if aid and aid not in handled:
                handled.add(aid)
                graph["current"]["explored_area"] += 64
                actions["collect_berrybush"] = {"kind": "collect", "guid": 99, "prefab": "berrybush", "product": "berries"}
        self.client.hook = tick
        agent = self.agent(lambda *_: self.fail("resource discoveries should not call JEV"), planner=FakePlanner(plan), execute=True)
        for _ in range(5):
            await agent.cycle()
        self.assertEqual(agent.sequence.status, "completed")
        self.assertEqual(len(handled), 4)
        self.assertTrue(all(a["choice"] == "explore_current_region" for n, a in self.client.calls if n == "start_action"))

    async def test_sequence_pauses_for_threat_then_resumes_same_step(self):
        plan = validate_sequence({"goal": "Get grass", "steps": [
            {"action": "collect", "prefab": "cutgrass", "count": 2}]}, 1)
        plan["sequence_id"] = "sequence:danger"
        self.client.state["nearby"] = [{"guid": 99, "activeThreat": True}]
        actions = self.client.state["bridge"]["available_actions"]
        actions["flee_from_nearest_hostile"] = {"kind": "flee_from_nearest_hostile"}
        def flee(*_):
            return {"answer": {"choice": "flee_from_nearest_hostile"}, "response": {"model": "offline"}}
        agent = self.agent(flee, planner=FakePlanner(plan), execute=True)
        await agent.cycle()
        self.assertEqual(agent.sequence.index, 0)
        self.client.state["nearby"] = []
        await agent.cycle()
        choices = [a["choice"] for n, a in self.client.calls if n == "start_action"]
        self.assertEqual(choices, ["flee_from_nearest_hostile", "collect_grass"])

    async def test_sequence_native_failure_stops_before_later_step(self):
        plan = validate_sequence({"goal": "Get grass then twig", "steps": [
            {"action": "collect", "prefab": "cutgrass", "count": 2},
            {"action": "collect", "prefab": "twigs", "count": 2}]}, 1)
        plan["sequence_id"] = "sequence:failed"
        self.client.state["bridge"]["available_actions"]["collect_sapling"] = {
            "kind": "collect", "guid": 21, "prefab": "sapling", "product": "twigs"}
        def tick(client):
            if client.bridge.active:
                client.sequence += 1
                client.bridge.exchange({"token": client.bridge.token, "session_id": client.session,
                    "sequence": client.sequence, "state": client.state,
                    "action_report": {"action_id": client.bridge.active, "status": "failed", "reason": "verification_timeout"}})
        self.client.hook = tick
        agent = self.agent(lambda *_: self.fail("failed sequence must replan, not ask JEV to skip"),
                           planner=FakePlanner(plan), execute=True)
        await agent.cycle(); await agent.cycle()
        self.assertEqual(agent.sequence.status, "failed")
        self.assertEqual([a["choice"] for n, a in self.client.calls if n == "start_action"], ["collect_grass"])

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
        with patch("mcp_agent.mcp_agent.time.monotonic", side_effect=lambda: clock[0]), patch("mcp_agent.mcp_agent.asyncio.sleep", advance):
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

    async def test_model_worker_has_independent_trace_and_result_notice_measurement(self):
        def decide(s, c, p):
            with timing.span("api.fake_request"):
                return answer(s, c, p)
        agent = self.agent(decide)
        with timing.CycleTrace(1, emit_console=False) as trace:
            await agent.cycle()
        events = trace.record["events"]
        submitted = next(e for e in events if e["stage"] == "decision.submitted")
        worker = next(e for e in events if e["stage"] == "decision.worker_completed")
        notice = next(e for e in events if e["stage"] == "decision.result_observed")
        self.assertEqual(submitted["request_id"], worker["request_id"])
        self.assertEqual(worker["request_id"], notice["request_id"])
        self.assertIn("api.fake_request", worker["worker_timing"]["totals"])
        self.assertNotIn("api.fake_request", trace.record["totals"])
        self.assertGreaterEqual(worker["worker_queue_ms"], 0)
        self.assertGreaterEqual(notice["result_ready_to_notice_ms"], 0)
        self.assertNotIn("inventory", str(worker))

    async def test_model_failure_is_measured_without_changing_exception_or_logging_text(self):
        def fail(*_):
            raise RuntimeError("secret-body-do-not-log")
        agent = self.agent(fail)
        with timing.CycleTrace(1, emit_console=False) as trace:
            with self.assertRaisesRegex(RuntimeError, "secret-body-do-not-log"):
                await agent.cycle()
        worker = next(e for e in trace.record["events"] if e["stage"] == "decision.worker_completed")
        self.assertEqual(worker["worker_timing"]["error_type"], "RuntimeError")
        self.assertNotIn("secret-body-do-not-log", str(trace.record))


if __name__ == "__main__":
    unittest.main()
