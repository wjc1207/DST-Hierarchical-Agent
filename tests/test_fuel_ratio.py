"""Conservative fuel intervals and one-shot/persistent ratio targets."""
import asyncio
import copy
import unittest
from common.refueling import validate_target, fuel_bounds
from common.task_board import format_board
from mcp_agent.priority_board import PriorityBoardExecutor, validate_goals, merge_goals
from mcp_agent.navigation_intents import ExplorationExecutor, local_candidates
from mcp_agent.mcp_agent import MCPAgent
from test_refueling import actual as old_state, action as old_action, distant_fire
from test_mcp_agent import FakeClient, FakePlanner


def plan(ratio=.3, **extra):
    return {**validate_goals({'goal':'Maintain fire fuel','tasks':[{'id':'fuel','title':'Maintain fuel','priority':1,
        'target':{'type':'refuel','building_id':'building:40','fuel_prefab':'log','at_least':ratio},**extra}]},1),'board_id':'ratio:1'}


def actual(stage=2,t=.1,**extra):
    s=old_state();s['world']['time']=t
    s['player']['inventory']['items']=[{'prefab':'log','count':10}];s['recipe_inventory']={'log':10}
    row={'building_id':'building:40','lit':stage>0,'fire_section':stage,'reason':'fire_already_sufficient' if stage>=2 else '',
         'ratio_reason':'fuel_ratio_unverifiable' if stage==4 else ''}
    row.update(extra);s['refueling'].update(ratio_available=True,targets=[row]);return s


def action(): return {**old_action(),'ratio_refuel':True}


def view(r,p,s): return r.view(p,s)['tasks'][0]


class FuelRatioTests(unittest.TestCase):
    def test_target_validates_ratio_and_legacy_count_exclusivity(self):
        for value in [True,0,-1,1.1,float('nan'),'30']:
            with self.subTest(value=value),self.assertRaises(ValueError): plan(value)
        with self.assertRaises(ValueError): validate_target({**plan()['tasks'][0]['target'],'count':2})
        legacy={'id':'fuel','title':'fuel','duration':.25,'target':{'type':'refuel','building_id':'building:40','fuel_prefab':'log','count':2}}
        b=validate_goals({'goal':'Invalid legacy duration','tasks':[legacy]},1)
        self.assertIn('at_least',b['tasks'][0]['timing_error'])

    def test_interval_lower_bound_not_midpoint_is_used(self):
        for section,expected in enumerate([(0,0),(0,.25),(.25,.5),(.5,.75),(.75,1)]):
            b=fuel_bounds({'fire_section':section});self.assertEqual((b['min'],b['max']),expected);self.assertFalse(b['exact'])
        self.assertFalse(fuel_bounds({'fire_section':2})['upper_inclusive'])
        r=PriorityBoardExecutor();p=plan(.3)
        self.assertNotEqual(view(r,p,actual(2))['status'],'done')
        self.assertEqual(view(r,p,actual(3,.2))['status'],'done')
        self.assertEqual(view(r,p,actual(1,.3))['status'],'done')

    def test_exact_percentage_has_precedence_over_stage(self):
        t=view(PriorityBoardExecutor(),plan(.9),actual(4,fuel_percent=.92))
        self.assertEqual(t['status'],'done');self.assertTrue(t['fuel_interval']['exact'])
        self.assertEqual(t['progress']['current'],.92)

    def test_duration_monitors_and_replenishes_without_early_count_completion(self):
        p=plan(.3,duration=.25,focus=True);r=PriorityBoardExecutor();r.adopt(p);s=actual(3)
        self.assertEqual(view(r,p,s)['status'],'maintaining')
        self.assertEqual(r.view(p,s)['focus_task_ids'],[])
        s=actual(2,.2);self.assertEqual(view(r,p,s)['status'],'in_progress')
        choice=r.choose(p,s,{'add_fuel_ratio:40:log':action()},ExplorationExecutor())
        self.assertEqual(choice[1],'add_fuel_ratio:40:log')
        r.on_result('fuel',choice[1],action(),'completed','observed_effect',ExplorationExecutor(),s)
        self.assertEqual(view(r,p,s)['fuel_added'],1);self.assertFalse(r.records['fuel']['done'])
        self.assertEqual(view(r,p,actual(3,.25))['status'],'maintaining')
        self.assertEqual(view(r,p,actual(3,.35))['status'],'done')

    def test_does_not_use_legacy_action_for_ratio_goal(self):
        r=PriorityBoardExecutor();self.assertIsNone(r.choose(plan(),actual(),{'legacy':old_action()},ExplorationExecutor()))
        _,choices=local_candidates({}, {'add_fuel_ratio:40:log':action(),'wait':{'kind':'wait'}})
        self.assertNotIn('add_fuel_ratio:40:log',choices)

    def test_at_expiry_lit_but_below_ratio_is_expired(self):
        p=plan(.5,duration=.25);r=PriorityBoardExecutor();view(r,p,actual(3))
        t=view(r,p,actual(2,.35));self.assertEqual(t['status'],'expired');self.assertIn('below fuel ratio',t['blocked_reason'])
        self.assertEqual(view(r,p,actual(4,.4))['status'],'expired')

    def test_out_of_view_memory_cannot_prove_completion(self):
        p=plan(.3,duration=.25);r=PriorityBoardExecutor();view(r,p,actual(3))
        s=actual(3,.35);s['refueling']['targets']=[]
        self.assertEqual(view(r,p,s)['status'],'expired')
        self.assertIn('without current fire telemetry',view(r,p,s)['blocked_reason'])

    def test_old_peer_and_unknown_stage_do_not_prove_ratio(self):
        s=actual();del s['refueling']['ratio_available']
        self.assertIn('updated Mod',view(PriorityBoardExecutor(),plan(),s)['blocked_reason'])
        s=actual();s['refueling']['targets'][0].pop('fire_section')
        self.assertEqual(view(PriorityBoardExecutor(),plan(),s)['blocked_reason'],'fuel_ratio_unavailable')

    def test_estimated_stage_four_does_not_chase_unverifiable_target(self):
        p=plan(.9,duration=.25);r=PriorityBoardExecutor()
        t=view(r,p,actual(4));self.assertEqual(t['status'],'blocked');self.assertEqual(t['blocked_reason'],'fuel_ratio_unverifiable')
        self.assertIn('maintenance',t)
        self.assertEqual(view(r,p,actual(4,.35))['status'],'expired')

    def test_heat_pauses_fuel_and_does_not_extend_window(self):
        p=plan(.5,duration=.25);r=PriorityBoardExecutor();s=actual(2)
        s['thermal']={'active':True}
        t=view(r,p,s);self.assertEqual(t['blocked_reason'],'temperature_too_high')
        self.assertAlmostEqual(t['maintenance']['deadline'],.35)
        self.assertEqual(view(r,p,actual(2,.35))['status'],'expired')

    def test_settling_is_a_local_wait(self):
        s=actual();s['refueling']['targets'][0]['ratio_reason']='fuel_settling'
        r=PriorityBoardExecutor();self.assertIsNone(r.choose(plan(),s,{'wait':{'kind':'wait'}},ExplorationExecutor()))
        self.assertEqual(r.reason,'refuel_settling')

    def test_duration_waits_for_start_and_preserves_deadline_on_review(self):
        p=plan(.3,duration=.25,start_at={'day':1,'progress':.3});r=PriorityBoardExecutor();r.adopt(p)
        self.assertEqual(view(r,p,actual(3,.1))['status'],'scheduled')
        self.assertEqual(view(r,p,actual(3,.3))['maintenance']['started_at'],.3)
        q=merge_goals(r.view(p,actual(3,.4)),plan(.3,duration=.25,focus=True,start_at={'day':1,'progress':.3}))
        r.adopt(q);self.assertEqual(view(r,q,actual(3,.45))['maintenance']['deadline'],.55)

    def test_changing_ratio_reopens_a_completed_one_shot(self):
        p=plan(.25);r=PriorityBoardExecutor();r.adopt(p);self.assertEqual(view(r,p,actual(2))['status'],'done')
        q=plan(.5);r.adopt(q);self.assertNotEqual(view(r,q,actual(2,.2))['status'],'done')

    def test_approach_exact_fire_before_querying_missing_current_interval(self):
        s=distant_fire();s['refueling'].update(ratio_available=True,targets=[])
        r=PriorityBoardExecutor();choice=r.choose(plan(),s,s['bridge']['available_actions'],ExplorationExecutor())
        self.assertEqual(choice[1],'visit_building:40');self.assertFalse(r.records['fuel']['done'])

    def test_panel_displays_interval_and_target(self):
        text=format_board(PriorityBoardExecutor().view(plan(),actual(2)))
        self.assertIn('25%/30%',text);self.assertIn('fuel estimate [25%, 50%)',text)


class RatioHostTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self): self.client=FakeClient();self.agents=[]
    async def asyncTearDown(self):
        for a in self.agents: await a.shutdown()
    def agent(self,p):
        a=MCPAgent(self.client,lambda *_:self.fail('No JEV for ratio maintenance'),planner=FakePlanner(p),execute=True,poll=.001)
        self.agents.append(a);return a

    async def test_monitor_low_add_then_monitor_without_changing_deadline(self):
        self.client.state=actual(3);self.client.state['bridge']['available_actions']={'add_fuel_ratio:40:log':action()}
        a=self.agent(plan(duration=.5,focus=True));await a.cycle()
        self.assertFalse(any(n=='start_action' for n,_ in self.client.calls))
        self.client.state=actual(2,.2);self.client.state['bridge']['available_actions']={'add_fuel_ratio:40:log':action()}
        await a.cycle();self.assertEqual(a.goals.refuel_totals['fuel']['added'],1)
        self.client.state=actual(3,.3);await a.cycle()
        self.assertEqual(view(a.goals,a.plan,self.client.state)['maintenance']['deadline'],.6)
        self.assertEqual(len([n for n,_ in self.client.calls if n=='start_action']),1)

    async def test_adequate_fire_does_not_cancel_addition_before_native_confirmation(self):
        self.client.state=actual(2);self.client.auto_complete=False
        self.client.state['bridge']['available_actions']={'add_fuel_ratio:40:log':action()}
        polls=0
        def adequate_before_terminal(client):
            nonlocal polls
            if not client.bridge.active:return
            polls+=1;client.state['refueling']['targets'][0].update(fire_section=3)
            if polls>=3:client.auto_complete=True
        self.client.hook=adequate_before_terminal
        a=self.agent(plan(duration=.5));await asyncio.wait_for(a.cycle(),2)
        self.assertFalse(any(n=='cancel_action' for n,_ in self.client.calls))
        self.assertEqual(a.goals.refuel_totals['fuel']['added'],1)

    async def test_duration_end_still_cancels_unfinished_fuel_action(self):
        self.client.state=actual(2);self.client.auto_complete=False
        self.client.state['bridge']['available_actions']={'add_fuel_ratio:40:log':action()}
        def expire(client):
            if client.bridge.active:client.state['world']['time']=.5
        self.client.hook=expire
        a=self.agent(plan(duration=.25));await asyncio.wait_for(a.cycle(),2)
        self.assertTrue(any(n=='cancel_action' for n,_ in self.client.calls))
        self.assertEqual(view(a.goals,a.plan,self.client.state)['status'],'expired')
