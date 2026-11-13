"""Persistent hunger windows: real game time, repeated demand, focus and host interruptions."""
import copy
import asyncio
import unittest
from common.task_board import board_view, focus_scope, format_board
from mcp_agent.priority_board import PriorityBoardExecutor, validate_goals, merge_goals, task_signature, idle_review_reason
from mcp_agent.navigation_intents import ExplorationExecutor
from mcp_agent.mcp_agent import MCPAgent
from mcp_server.game_bridge import GameBridge, BridgeError
from test_mcp_agent import FakeClient, FakePlanner, snapshot


def hunger(**extra):
    return {'id':'eat','title':'Maintain food','priority':1,
            'target':{'type':'hunger_ratio','at_least':.7},**extra}


def work(**extra):
    return {'id':'grass','title':'Gather grass','priority':3,
            'target':{'type':'inventory','prefab':'cutgrass','count':2},**extra}


def board(*tasks):
    return {**validate_goals({'goal':'Maintain hunger','tasks':list(tasks)},1),'board_id':'food:1'}


def actual(t=.1, ratio=.8, day=1):
    s=snapshot();s['world'].update(time=t,day=day)
    s['player']['vitals'].update(hunger=ratio*150,hunger_max=150)
    return s


def task_view(r,b,s):
    return next(t for t in r.view(b,s)['tasks'] if t['id']=='eat')


class HungerWindowTests(unittest.TestCase):
    def test_optional_duration_keeps_one_shot_completion(self):
        b=board(hunger());r=PriorityBoardExecutor()
        self.assertEqual(task_view(r,b,actual())['status'],'done')
        self.assertEqual(task_view(r,b,actual(.2,.5))['status'],'done')

    def test_invalid_duration_and_end_are_task_local_errors(self):
        for fields in ({'duration':0},{'duration':True},{'duration':float('nan')},{'duration':8},
                       {'end_at':{'day':2,'phase':'day'}},
                       {'duration':.25,'end_at':{'day':2,'phase':'day'}}):
            with self.subTest(fields=fields):
                b=board(hunger(**fields),work())
                v=PriorityBoardExecutor().view(b,actual())
                self.assertEqual(next(t for t in v['tasks'] if t['id']=='eat')['status'],'failed')
                self.assertEqual(len(b['task_errors']),1)
        for kind in ('inventory','structure'):
            t=work(duration=.25) if kind=='inventory' else {'id':'fire','title':'Fire','target':{'type':'structure','prefab':'campfire'},'duration':.25}
            self.assertIn('timing_error',board(t)['tasks'][0])

    def test_meeting_target_monitors_and_demand_reopens_repeatedly(self):
        b=board(hunger(duration=.4));r=PriorityBoardExecutor()
        self.assertEqual(task_view(r,b,actual())['status'],'maintaining')
        for t in (.15,.25):
            self.assertEqual(task_view(r,b,actual(t,.65))['status'],'in_progress')
            self.assertEqual(task_view(r,b,actual(t+.02,.8))['status'],'maintaining')
        self.assertFalse(r.records['eat']['done'])
        self.assertEqual(task_view(r,b,actual(.5,.8))['status'],'done')
        self.assertEqual(task_view(r,b,actual(.6,.2))['status'],'done')

    def test_clock_starts_on_activation_while_still_hungry(self):
        b=board(hunger(duration=.25,start_at={'day':1,'progress':.4}));r=PriorityBoardExecutor()
        self.assertEqual(task_view(r,b,actual(.1))['status'],'scheduled')
        self.assertNotIn('maintenance',r.records['eat'])
        task=task_view(r,b,actual(.45,.5))
        self.assertEqual(task['maintenance']['started_at'],.45)
        self.assertAlmostEqual(task['maintenance']['deadline'],.7)
        self.assertEqual(task_view(r,b,actual(.7,.8))['status'],'done')

    def test_dependencies_delay_timer_and_wait_for_final_window_completion(self):
        b=board(hunger(duration=.25,depends_on=['grass']),work());r=PriorityBoardExecutor()
        self.assertEqual(task_view(r,b,actual(.1))['status'],'blocked')
        self.assertNotIn('maintenance',r.records['eat'])
        s=actual(.3);s['player']['inventory']['items']=[{'prefab':'cutgrass','count':2}]
        self.assertEqual(task_view(r,b,s)['maintenance']['started_at'],.3)
        dependent=board(hunger(duration=.25),work(depends_on=['eat']));r=PriorityBoardExecutor()
        self.assertEqual(r.view(dependent,actual())['tasks'][1]['status'],'blocked')
        self.assertEqual(r.view(dependent,actual(.35))['tasks'][1]['status'],'ready')

    def test_focus_releases_when_full_and_reacquires_when_hungry(self):
        b=board(hunger(duration=.5,focus=True),work());r=PriorityBoardExecutor();nav=ExplorationExecutor()
        actions={'eat_safe_food':{'kind':'eat_safe_food'},'grass':{'kind':'collect','product':'cutgrass','guid':20}}
        s=actual();self.assertEqual(r.choose(b,s,actions,nav)[0]['id'],'grass')
        v=r.view(b,s);self.assertEqual(v['focus_task_ids'],[]);self.assertIsNone(focus_scope(v))
        self.assertEqual(r.navigation_targets(b,s),[b['tasks'][1]['target']])
        low=actual(.2,.6);self.assertEqual(r.choose(b,low,actions,nav)[0]['id'],'eat')
        self.assertEqual(r.view(b,low)['focus_task_ids'],['eat'])
        self.assertEqual(r.navigation_review_reason(b,low,'grass',task_signature(b['tasks'][1]),[b['tasks'][1]['target']]),'goal_changed_or_blocked')

    def test_monitoring_is_active_and_holds_without_a_jev_gap(self):
        b=board(hunger(duration=.25));r=PriorityBoardExecutor();s=actual()
        self.assertIsNone(idle_review_reason(r.view(b,s)))
        self.assertIsNone(r.choose(b,s,{'wait':{'kind':'wait'}},ExplorationExecutor()))
        self.assertEqual(r.reason,'maintenance_holding')
        self.assertIn('maintaining',format_board(r.view(b,s)))
        self.assertIn('remaining=0.25d',format_board(r.view(b,s)))

    def test_deadline_expires_below_target_and_cannot_recover_late(self):
        b=board(hunger(duration=.25));r=PriorityBoardExecutor();task_view(r,b,actual())
        t=task_view(r,b,actual(.35,.6));self.assertEqual(t['status'],'expired')
        self.assertIn('below hunger target',t['blocked_reason'])
        self.assertEqual(task_view(r,b,actual(.4,.9))['status'],'expired')

    def test_safety_and_day_boundary_do_not_extend_window(self):
        b=board(hunger(duration=.25));r=PriorityBoardExecutor();task_view(r,b,actual(.9))
        s=actual(.05,.6,day=2);s['nearby']=[{'activeThreat':True}];s['thermal']={'active':True}
        self.assertAlmostEqual(task_view(r,b,s)['maintenance']['remaining_days'],.1)
        self.assertEqual(task_view(r,b,actual(.15,.8,day=2))['status'],'done')

    def test_review_and_focus_edits_keep_original_deadline(self):
        b=board(hunger(duration=.4));r=PriorityBoardExecutor();r.adopt(b);task_view(r,b,actual())
        updated=merge_goals(r.view(b,actual(.2)),board(hunger(duration=.4,focus=True,priority=2)))
        r.adopt(updated);t=task_view(r,updated,actual(.3))
        self.assertEqual(t['maintenance']['started_at'],.1)
        self.assertEqual(t['maintenance']['deadline'],.5)
        renamed=board({**hunger(duration=.4),'id':'renamed'})
        self.assertEqual(merge_goals(r.view(updated,actual(.3)),renamed)['tasks'][0]['id'],'eat')

    def test_missing_telemetry_does_not_start_or_fake_completion(self):
        b=board(hunger(duration=.25));r=PriorityBoardExecutor();s=actual();del s['world']['time']
        self.assertEqual(task_view(r,b,s)['status'],'blocked');self.assertNotIn('maintenance',r.records['eat'])
        for val in (None,True,float('nan')):
            s=actual();s['player']['vitals']['hunger']=val
            self.assertEqual(task_view(r,b,s)['status'],'blocked')
        task_view(r,b,actual())
        s=actual(.4);del s['player']['vitals']['hunger']
        self.assertEqual(task_view(r,b,s)['status'],'expired')

    def test_predicted_state_cannot_start_complete_or_move_clock(self):
        b=board(hunger(duration=.25));r=PriorityBoardExecutor();s=actual(.5);s['_jev_execution_context']={'predicted_success':True}
        task_view(r,b,s);self.assertNotIn('maintenance',r.records['eat'])
        task_view(r,b,actual());task_view(r,b,s)
        self.assertEqual(task_view(r,b,actual(.2))['status'],'maintaining')
        self.assertEqual(r.records['eat']['maintenance']['last'],.2)

    def test_backwards_game_time_fails_instead_of_extending_timer(self):
        b=board(hunger(duration=.25));r=PriorityBoardExecutor();task_view(r,b,actual(.2))
        self.assertEqual(task_view(r,b,actual(.1))['status'],'failed')
        self.assertEqual(task_view(r,b,actual(.3))['status'],'failed')

    def test_waiting_does_not_reset_other_task_search_budgets(self):
        b=board(hunger(duration=.5),work());r=PriorityBoardExecutor();task_view(r,b,actual(.1,.6))
        r.records['eat'].update(seconds=120,distance=240,blocked='search_budget_exhausted',retry_at=100000000000)
        t=task_view(r,b,actual(.2,.8));self.assertEqual(t['status'],'maintaining')
        self.assertEqual(r.records['eat']['seconds'],0)
        self.assertEqual(task_view(r,b,actual(.3,.6))['status'],'in_progress')

    def test_mcp_auto_execution_rejects_duration_only_hunger_task(self):
        bridge=GameBridge('hunger-token-'*4);s=actual()
        bridge.exchange({'token':bridge.token,'session_id':'h','sequence':1,'state':s})
        observation=bridge.get_available_actions()['observation_id'];b=board(hunger(duration=.25))
        with self.assertRaisesRegex(BridgeError,'Python'):
            bridge.submit_task_board(b,observation,True)
        result=bridge.submit_task_board(b,observation,False)
        self.assertEqual(result['task_board']['tasks'][0]['status'],'maintaining')

    def test_active_clock_does_not_pause_for_missing_food_or_game_pause(self):
        b=board(hunger(duration=.25));r=PriorityBoardExecutor()
        t=task_view(r,b,actual(.1,.5));self.assertEqual(t['maintenance']['remaining_days'],.25)
        self.assertEqual(task_view(r,b,actual(.1,.5))['maintenance']['remaining_days'],.25)
        r.records['eat'].update(blocked='search_budget_exhausted',seconds=120,retry_at=100000000000)
        self.assertEqual(task_view(r,b,actual(.35,.5))['status'],'expired')


class HungerHostTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self): self.client=FakeClient();self.agents=[]
    async def asyncTearDown(self):
        for agent in self.agents: await agent.shutdown()
    def agent(self,b):
        a=MCPAgent(self.client,lambda *_:self.fail('JEV must not arbitrate food maintenance'),planner=FakePlanner(b),execute=True,poll=.001)
        self.agents.append(a);return a

    async def test_monitor_then_replenish_then_monitor_again(self):
        self.client.state=actual();self.client.state['bridge']['available_actions']={'eat_safe_food':{'kind':'eat_safe_food'},'wait':{'kind':'wait'}}
        a=self.agent(board(hunger(duration=.5,focus=True)))
        await a.cycle();self.assertFalse(any(n=='start_action' for n,_ in self.client.calls))
        self.client.state['world']['time']=.2;self.client.state['player']['vitals']['hunger']=95
        await a.cycle();self.assertEqual([v['choice'] for n,v in self.client.calls if n=='start_action'],['eat_safe_food'])
        self.client.state['world']['time']=.21;self.client.state['player']['vitals']['hunger']=120
        await a.cycle();self.assertEqual(len([n for n,_ in self.client.calls if n=='start_action']),1)
        self.client.state['world']['time']=.4;self.client.state['player']['vitals']['hunger']=95
        await a.cycle();self.assertEqual(len([n for n,_ in self.client.calls if n=='start_action']),2)

    async def test_hunger_demand_interrupts_unfinished_other_work(self):
        self.client.state=actual();self.client.auto_complete=False
        self.client.state['bridge']['available_actions']={'collect_grass':{'kind':'collect','guid':20,'product':'cutgrass'}}
        def become_hungry(client):
            if client.bridge.active: client.state['player']['vitals']['hunger']=95
        self.client.hook=become_hungry
        a=self.agent(board(hunger(duration=.5,focus=True),work()))
        await asyncio.wait_for(a.cycle(),2)
        self.assertTrue(any(n=='cancel_action' for n,_ in self.client.calls))
        self.assertEqual(next(iter(self.client.bridge.actions.values()))['status'],'cancelled')
        self.assertEqual(task_view(a.goals,a.plan,self.client.state)['status'],'in_progress')

    async def test_deadline_interrupts_unfinished_food_search(self):
        self.client.state=actual(.1,.5);self.client.auto_complete=False
        self.client.state['nearby']=[{'guid':20,'prefab':'carrot','tags':['_inventoryitem'],'distance':1}]
        self.client.state['bridge']['available_actions']={'carrot':{'kind':'collect','guid':20,'prefab':'carrot','product':'carrot'}}
        def reach_deadline(client):
            if client.bridge.active: client.state['world']['time']=.4
        self.client.hook=reach_deadline
        a=self.agent(board(hunger(duration=.25)))
        await asyncio.wait_for(a.cycle(),2)
        self.assertTrue(any(n=='cancel_action' for n,_ in self.client.calls))
        self.assertEqual(task_view(a.goals,a.plan,self.client.state)['status'],'expired')
