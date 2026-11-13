"""Game-time scheduling and residence; no game or model calls."""
import copy
import asyncio
import unittest
from common.task_board import board_view
from common.residence import allowed, validate_area
from mcp_agent.priority_board import PriorityBoardExecutor, validate_goals
from mcp_agent.navigation_intents import ExplorationExecutor
from test_mcp_agent import FakeClient, FakePlanner, FakeNavigator, snapshot
from mcp_agent.mcp_agent import MCPAgent
from mcp_server.game_bridge import GameBridge, BridgeError


def task(identity="stay", **extra):
    return {"id":identity,"title":identity,"target":{"type":"stay_home","home_id":"home:1","radius":12},**extra}


def board(*tasks):
    return {**validate_goals({"goal":"Test scheduling","tasks":list(tasks)},1),"board_id":"timer:1"}


def actual(progress=0, x=0, arrived=True, day=1, phase="day"):
    s=snapshot();s["world"].update(time=progress,day=day,phase=phase)
    s["player"]["position"]["x"]=x
    s["places"]={"home":{"id":"home:1","x":0,"z":0,"arrived":arrived}}
    s["bridge"]["residence_constraints"]=True
    return s


class TimingTests(unittest.TestCase):
    def test_conflict_fails_only_the_affected_task_and_returns_reason(self):
        b=board(task(duration=.25,end_at={"day":2,"phase":"day"}),
                {"id":"grass","title":"Grass","target":{"type":"inventory","prefab":"cutgrass","count":2}})
        self.assertEqual(b["task_errors"][0]["task_id"],"stay")
        r=PriorityBoardExecutor();v=r.view(b,actual())
        failed=next(t for t in v["tasks"] if t["id"]=="stay")
        self.assertEqual(failed["status"],"failed")
        self.assertIn("mutually exclusive",failed["blocked_reason"])
        action={"kind":"collect","product":"cutgrass","guid":2}
        self.assertEqual(r.choose(b,actual(),{"grass":action},ExplorationExecutor())[1],"grass")

    def test_bad_timer_parameters_are_task_local_errors(self):
        for fields in ({},{"duration":0},{"duration":True},{"duration":float("nan")},
                       {"end_at":{"phase":"unknown","next":True}},
                       {"duration":.25,"start_at":{"progress":1}},
                       {"start_at":{"day":2,"progress":.75},"end_at":{"day":1,"progress":.5}}):
            with self.subTest(fields=fields):
                self.assertTrue(board(task(**fields))["task_errors"])
        ordinary={"id":"fire","title":"Fire","duration":.2,"target":{"type":"structure","prefab":"campfire"}}
        self.assertIn("Ordinary tasks",board(ordinary)["task_errors"][0]["reason"])

    def test_future_focus_does_not_latch_completion_or_block_other_work(self):
        fire={"id":"fire","title":"Fire","focus":True,"start_at":{"day":1,"phase":"night"},
              "target":{"type":"structure","prefab":"campfire"}}
        grass={"id":"grass","title":"Grass","target":{"type":"inventory","prefab":"cutgrass","count":2}}
        b=board(fire,grass);r=PriorityBoardExecutor();s=actual(.5,phase="dusk")
        v=r.view(b,s)
        self.assertEqual(v["tasks"][0]["status"],"scheduled")
        self.assertEqual(v["focus_task_ids"],[])
        actions={"grass":{"kind":"collect","product":"cutgrass","guid":2},"build_campfire":{"kind":"build_campfire"}}
        self.assertEqual(r.choose(b,s,actions,ExplorationExecutor())[1],"grass")
        s["world"].update(time=.9,phase="night")
        self.assertEqual(r.choose(b,s,actions,ExplorationExecutor())[1],"build_campfire")

    def test_duration_starts_after_arrival_and_pause_or_escape_does_not_count(self):
        b=board(task(duration=.25));r=PriorityBoardExecutor()
        r.view(b,actual(0,x=20,arrived=False))
        v=r.view(b,actual(.2));self.assertEqual(v["tasks"][0]["residence"]["elapsed_days"],0)
        r.view(b,actual(.3));r.view(b,actual(.3))
        s=actual(.4,x=20,arrived=False);s["nearby"]=[{"activeThreat":True}]
        r.view(b,s);r.view(b,actual(.5,x=20,arrived=False));r.view(b,actual(.6))
        self.assertNotEqual(r.view(b,actual(.7))["status"],"completed")
        self.assertEqual(r.view(b,actual(.75))["status"],"completed")

    def test_next_day_phase_end_and_missed_fixed_deadline(self):
        b=board(task(end_at={"phase":"day","next":True}));r=PriorityBoardExecutor()
        self.assertNotEqual(r.view(b,actual(.75,day=2,phase="dusk"))["status"],"completed")
        self.assertNotEqual(r.view(b,actual(.95,day=2,phase="night"))["status"],"completed")
        self.assertEqual(r.view(b,actual(0,day=3))["status"],"completed")
        late=board(task(end_at={"day":1,"progress":.5}));r=PriorityBoardExecutor()
        v=r.view(late,actual(.6,x=20,arrived=False))
        self.assertEqual(v["tasks"][0]["status"],"expired")
        self.assertNotEqual(v["status"],"completed")
        r=PriorityBoardExecutor()
        self.assertEqual(r.view(late,actual(.6))["tasks"][0]["status"],"expired",
                         "being home after a missed deadline is not proof of past residence")

    def test_missing_time_and_predicted_snapshots_do_not_complete_residence(self):
        b=board(task(duration=.25));r=PriorityBoardExecutor();r.view(b,actual(.1))
        s=actual(.9);s["_jev_execution_context"]={"predicted_success":True}
        r.view(b,s)
        self.assertNotEqual(r.view(b,actual(.2))["status"],"completed")
        s=actual(.3);del s["world"]["time"]
        self.assertEqual(r.view(b,s)["tasks"][0]["status"],"blocked")
        future=board(task(duration=.25,focus=True,start_at={"day":1,"progress":.8}))
        v=PriorityBoardExecutor().view(future,s)
        self.assertEqual(v["tasks"][0]["status"],"scheduled")
        self.assertEqual(v["focus_task_ids"],[])

    def test_stable_timer_survives_review_and_backwards_time_fails(self):
        b=board(task(duration=.25));r=PriorityBoardExecutor();r.adopt(b)
        r.view(b,actual(.3));r.view(b,actual(.4));r.adopt(b)
        self.assertEqual(r.view(b,actual(.5))["tasks"][0]["residence"]["elapsed_days"],.2)
        v=r.view(b,actual(.1))
        self.assertEqual(v["tasks"][0]["status"],"failed")

    def test_residence_returns_first_then_allows_only_compatible_local_work(self):
        work={"id":"grass","title":"Grass","target":{"type":"inventory","prefab":"cutgrass","count":2}}
        b=board(task(duration=.25,focus=True),work);r=PriorityBoardExecutor();nav=ExplorationExecutor()
        actions={"return_home":{"kind":"explore","home_id":"home:1","planner_only":True,"mode":"place"},
                 "grass":{"kind":"collect","product":"cutgrass","guid":2}}
        self.assertEqual(r.choose(b,actual(0,x=20,arrived=False),actions,nav)[1],"return_home")
        s=actual(.1);s["nearby"]=[{"guid":2,"dx":3,"dz":0,"collect_path":{"status":"ready","distance":3}}]
        self.assertEqual(r.choose(b,s,actions,nav)[1],"grass")
        s["nearby"][0]["dx"]=20
        self.assertIsNone(r.choose(b,s,actions,nav))
        self.assertEqual(r.reason,"residence_holding")

    def test_bounds_cover_full_approach_cleanup_and_placement(self):
        s=actual();area=validate_area({"home_id":"home:1","radius":12},s)
        s["nearby"]=[{"guid":2,"dx":3,"dz":0,"collect_path":{"distance":20}}]
        self.assertFalse(allowed({"kind":"collect","guid":2},s,area))
        self.assertFalse(allowed({"kind":"place","x":20,"z":0},s,area))
        self.assertTrue(allowed({"kind":"craft"},s,area))

    def test_external_board_returns_task_errors_and_requires_python_scheduler(self):
        bridge=GameBridge("test-token-"*4);s=actual()
        bridge.exchange({"token":bridge.token,"session_id":"t","sequence":1,"state":s})
        observation=bridge.get_available_actions()["observation_id"]
        bad=board(task(duration=.25,end_at={"day":2,"phase":"day"}))
        result=bridge.submit_task_board(bad,observation,False)
        self.assertTrue(result["task_errors"])
        with self.assertRaises(BridgeError):
            bridge.submit_task_board(board(task(duration=.25)),observation,True)


class TimingAgentTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client=FakeClient();self.agents=[]

    async def asyncTearDown(self):
        for agent in self.agents:
            await agent.shutdown()

    def agent(self,plan,decide=None,**kwargs):
        agent=MCPAgent(self.client,decide or (lambda *_:self.fail("JEV should not arbitrate residence chores")),
                       planner=FakePlanner(plan),execute=True,poll=.001,**kwargs)
        self.agents.append(agent);return agent

    async def test_holding_does_not_hand_unrelated_exploration_to_jev(self):
        self.client.state=actual(.1)
        self.client.state["bridge"]["available_actions"]={"wait":{"kind":"wait"},
            "explore_other_region":{"kind":"explore","mode":"search","x":30,"z":0}}
        agent=self.agent(board(task(duration=.25)))
        await agent.cycle()
        self.assertFalse(any(n=="start_action" for n,_ in self.client.calls))

    async def test_residence_work_is_bound_to_the_authoritative_home(self):
        self.client.state=actual(.1)
        self.client.state["nearby"]=[{"guid":20,"dx":2,"dz":0,"collect_path":{"status":"ready","distance":2}}]
        work={"id":"grass","title":"Grass","target":{"type":"inventory","prefab":"cutgrass","count":2}}
        agent=self.agent(board(task(duration=.25),work))
        await agent.cycle()
        args=next(a for n,a in self.client.calls if n=="start_action")
        self.assertEqual(args["residence"],{"home_id":"home:1","radius":12})
        queued=next(iter(self.client.bridge.actions.values()))["action"]
        self.assertEqual(queued["residence"]["x"],0)

    async def test_start_time_interrupts_background_navigation(self):
        self.client.state=actual(.2)
        self.client.state["bridge"]["available_actions"]={"wait":{"kind":"wait"},
            "explore_other_region":{"kind":"explore","mode":"search","x":30,"z":0},
            "build_campfire":{"kind":"build_campfire"}}
        plan=board({"id":"fire","title":"Fire","start_at":{"day":1,"progress":.75},
                    "target":{"type":"structure","prefab":"campfire"}})
        self.client.auto_complete=False
        def start_timer(client):
            if client.bridge.active:
                client.state["world"].update(time=.8,phase="night")
        self.client.hook=start_timer
        agent=self.agent(plan,navigator=FakeNavigator("explore_other_region"))
        await asyncio.wait_for(agent.cycle(),2)
        self.assertTrue(any(n=="cancel_action" for n,_ in self.client.calls))
        self.assertEqual(next(iter(self.client.bridge.actions.values()))["status"],"cancelled")

    async def test_residence_end_interrupts_unfinished_compatible_work(self):
        self.client.state=actual(.1)
        self.client.state["nearby"]=[{"guid":20,"dx":2,"dz":0,"collect_path":{"status":"ready","distance":2}}]
        work={"id":"grass","title":"Grass","target":{"type":"inventory","prefab":"cutgrass","count":2}}
        self.client.auto_complete=False
        def advance(client):
            if client.bridge.active:
                client.state["world"]["time"]=.4
        self.client.hook=advance
        agent=self.agent(board(task(duration=.25),work))
        await asyncio.wait_for(agent.cycle(),2)
        self.assertTrue(any(n=="cancel_action" for n,_ in self.client.calls))
        view=agent.goals.view(agent.plan,self.client.state)
        self.assertEqual(next(t for t in view["tasks"] if t["id"]=="stay")["status"],"done")

    async def test_safety_can_leave_the_residence_area(self):
        self.client.state=actual(.1)
        self.client.state["nearby"]=[{"guid":99,"activeThreat":True}]
        self.client.state["bridge"]["available_actions"]={"wait":{"kind":"wait"},
            "flee_from_nearest_hostile":{"kind":"flee_from_nearest_hostile","dx":1,"dz":0,"distance":20}}
        agent=self.agent(board(task(duration=.25)),lambda *_:{"answer":{"choice":"flee_from_nearest_hostile"},"response":{}})
        await agent.cycle()
        args=next(a for n,a in self.client.calls if n=="start_action")
        self.assertNotIn("residence",args)
