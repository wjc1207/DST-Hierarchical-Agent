"""Out-of-order observations and residence-return ownership regressions."""
import asyncio
import copy
import unittest
from mcp_agent.priority_board import PriorityBoardExecutor,validate_goals,task_signature
from mcp_agent.navigation_intents import ExplorationExecutor
from mcp_agent.mcp_agent import MCPAgent
from test_task_timing import actual,task,board
from test_mcp_agent import FakeClient,FakePlanner


def state(stamp,progress,x=0,arrived=True):
    s=actual(progress,x=x,arrived=arrived);s['observed_at']=stamp
    return s


class ObservationTests(unittest.TestCase):
    def test_older_observation_cannot_fail_residence_or_rewind_elapsed_time(self):
        b=board(task(duration=.5));r=PriorityBoardExecutor()
        old=state(100,.1);r.view(b,old);r.view(b,state(110,.2))
        v=r.view(b,old)
        self.assertEqual(v['tasks'][0]['status'],'in_progress')
        self.assertEqual(v['observed_at'],110)
        self.assertAlmostEqual(v['tasks'][0]['residence']['elapsed_days'],.1)
        self.assertEqual(r.stale_observations_ignored,1)

    def test_older_observation_cannot_fail_hunger_or_fire_maintenance(self):
        for target in [{'type':'hunger_ratio','at_least':.7},
                       {'type':'refuel','building_id':'building:40','fuel_prefab':'log','at_least':.3}]:
            with self.subTest(target=target):
                b={**validate_goals({'goal':'Maintain','tasks':[{'id':'m','title':'Maintain','target':target,'duration':.5}]},1),'board_id':'m:1'}
                def current(stamp,time):
                    s=state(stamp,time);s['player']['vitals'].update(hunger=120,hunger_max=150)
                    s['places']['buildings']=[{'id':'building:40','prefab':'firepit','status':'observed'}]
                    s['refueling']={'available':True,'ratio_available':True,'targets':[{'building_id':'building:40','fire_section':3,'lit':True}]}
                    return s
                r=PriorityBoardExecutor();old=current(100,.1);r.view(b,old);r.view(b,current(110,.2))
                t=r.view(b,old)['tasks'][0]
                self.assertEqual(t['status'],'maintaining');self.assertAlmostEqual(t['maintenance']['remaining_days'],.4)

    def test_newer_observation_with_real_backwards_game_time_still_fails(self):
        b=board(task(duration=.5));r=PriorityBoardExecutor();r.view(b,state(100,.3))
        self.assertEqual(r.view(b,state(110,.2))['tasks'][0]['status'],'failed')

    def test_old_inventory_cannot_complete_new_work_and_guard_survives_batch_reset(self):
        b=board({'id':'grass','title':'Grass','target':{'type':'inventory','prefab':'cutgrass','count':2}})
        old=state(100,.1);old['player']['inventory']['items']=[{'prefab':'cutgrass','count':2}]
        fresh=state(110,.2);r=PriorityBoardExecutor();r.view(b,fresh);r.adopt(b,new_batch=True)
        self.assertNotEqual(r.view(b,old)['tasks'][0]['status'],'done')

    def test_predicted_future_does_not_replace_last_real_observation(self):
        b=board(task(duration=.5));r=PriorityBoardExecutor();r.view(b,state(100,.1))
        predicted=state(200,.8);predicted['_jev_execution_context']={'predicted_success':True}
        r.view(b,predicted)
        self.assertEqual(r.view(b,state(110,.2))['tasks'][0]['status'],'in_progress')
        self.assertEqual(r.latest_observed_state['observed_at'],110)

    def test_residence_return_uses_same_scope_for_dispatch_and_review(self):
        b=board(task(duration=.25),{'id':'grass','title':'Focused grass','focus':True,
                                  'target':{'type':'inventory','prefab':'cutgrass','count':2}})
        s=actual(.1,x=20,arrived=False);r=PriorityBoardExecutor();actions={'return_home':{'kind':'explore','home_id':'home:1','planner_only':True,'mode':'place','x':0,'z':0}}
        chosen=r.choose(b,s,actions,ExplorationExecutor());self.assertEqual(chosen[0]['id'],'stay')
        targets=r.navigation_targets(b,s)
        self.assertIsNone(r.navigation_review_reason(b,s,'stay',task_signature(chosen[0]),targets))
        # Unrelated focus goals and target-watch lists can change while the same residence still requires returning.
        self.assertIsNone(r.navigation_review_reason(b,s,'stay',task_signature(chosen[0]),[]))
        self.assertIsNotNone(r.navigation_review_reason(b,actual(.4,x=0,arrived=True),'stay',task_signature(chosen[0]),targets))

    def test_return_home_rule_does_not_allow_unrelated_ordinary_tasks_through_focus(self):
        ordinary={'id':'home','title':'Return','target':{'type':'return_home','home_id':'home:1'}}
        b=board(ordinary,{'id':'grass','title':'Focused grass','focus':True,'target':{'type':'inventory','prefab':'cutgrass','count':2}})
        r=PriorityBoardExecutor();self.assertEqual(r.navigation_review_reason(b,actual(.1,x=20,arrived=False),'home',task_signature(b['tasks'][0]),[]),'goal_changed_or_blocked')


class ObservationHostTests(unittest.IsolatedAsyncioTestCase):
    async def test_jev_original_credentials_do_not_rewind_future_task_timer(self):
        b=board(task(start_at={'day':1,'phase':'night'},end_at={'phase':'day','next':True}))
        class Planner(FakePlanner):
            def __init__(self):super().__init__(b);self.goals=PriorityBoardExecutor()
            def update(self,s):self.goals.view(b,s);return b
        c=FakeClient();c.state=state(100,.2)
        c.state['bridge']['available_actions']={'eat_safe_food':{'kind':'eat_safe_food'},'wait':{'kind':'wait'}}
        def advance(client):client.state['world']['time']+=.001;client.state['observed_at']+=.48
        c.hook=advance;p=Planner()
        a=MCPAgent(c,lambda *_:{'answer':{'choice':'eat_safe_food'},'response':{'model':'offline'}},planner=p,execute=True,poll=.001)
        try:
            await asyncio.wait_for(a.cycle(),3)
            self.assertEqual(p.goals.view(b,c.state)['tasks'][0]['status'],'scheduled')
            self.assertTrue(any(n=='start_action' for n,_ in c.calls))
        finally:await a.shutdown()

    async def test_focused_other_work_does_not_cancel_residence_return_in_a_loop(self):
        b=board(task(duration=.25),{'id':'grass','title':'Focused grass','focus':True,'target':{'type':'inventory','prefab':'cutgrass','count':2}})
        c=FakeClient();c.state=state(100,.1,x=20,arrived=False);c.auto_complete=False
        c.state['bridge']['available_actions']={'return_home':{'kind':'explore','home_id':'home:1','planner_only':True,'mode':'place','x':0,'z':0}}
        polls=0
        def arrive(client):
            nonlocal polls
            if client.bridge.active:
                polls+=1;client.state['player']['position']['x']-=2
                if polls>=3:
                    client.state['player']['position']['x']=0;client.state['places']['home']['arrived']=True;client.auto_complete=True
        c.hook=arrive
        a=MCPAgent(c,lambda *_:self.fail('JEV must not arbitrate residence return'),planner=FakePlanner(b),execute=True,poll=.001)
        try:
            await asyncio.wait_for(a.cycle(),3)
            self.assertGreaterEqual(polls,3)
            self.assertFalse(any(n=='cancel_action' for n,_ in c.calls))
            self.assertEqual(len([n for n,_ in c.calls if n=='start_action']),1)
        finally:await a.shutdown()
