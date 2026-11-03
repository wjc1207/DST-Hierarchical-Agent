"""Explicit refueling goals, verified progress, boundaries and MCP routing."""
import copy
import asyncio
import io
import json
import unittest
from unittest.mock import patch

from common.refueling import validate_target
from common.residence import allowed
from mcp_agent import daily_planner
from mcp_agent.priority_board import PriorityBoardExecutor, validate_goals, merge_goals, progress_signatures_match, task_signature
from mcp_agent.navigation_intents import ExplorationExecutor, local_candidates
from mcp_agent.model_state import model_game_state
from mcp_agent.mcp_agent import MCPAgent
from mcp_server.game_bridge import GameBridge, BridgeError
from test_mcp_agent import snapshot, FakeClient, FakePlanner


def actual():
    s = snapshot()
    s['world']['time'] = .1
    s['nearby'] = [{'guid':40,'prefab':'firepit','dx':2,'dz':0,'distance':2,'tags':['fire']}]
    s['places'] = {'buildings':[{'id':'building:40','guid':40,'prefab':'firepit','status':'observed'}]}
    s['refueling'] = {'available':True,'options':[]}
    return s


def action():
    return {'kind':'add_fuel','guid':40,'prefab':'firepit','building_id':'building:40',
            'fuel_prefab':'log','fuel_guid':20,'native_action':'ADDFUEL','planner_only':True}


def plan(count=2, **extra):
    return {**validate_goals({'goal':'Keep camp lit','tasks':[{'id':'fuel','title':'Add logs',
        'target':{'type':'refuel','building_id':'building:40','fuel_prefab':'log','count':count},**extra}]},1),
        'board_id':'refuel:1'}


def distant_fire():
    s=actual();s['player']['inventory']['items']=[{'prefab':'log','count':2}]
    s['recipe_inventory']={'log':2}
    route={'kind':'explore','mode':'place','building_id':'building:40','x':3.5,'z':0,
           'planner_only':True,'refuel_approach':True}
    s['places']['buildings'][0].update(x=6,z=0,arrived=False,
        route={'status':'ready','distance':3.5,'action':copy.deepcopy(route)})
    s['nearby'][0].update(dx=6,distance=6)
    s['bridge']['available_actions']={'visit_building:40':route,'wait':{'kind':'wait'}}
    return s


class RefuelingTests(unittest.TestCase):
    def test_target_validates_identity_count_and_explicit_common_fuel(self):
        self.assertEqual(validate_target({'building_id':'building:40','fuel_prefab':'log'})['count'],1)
        for changes in ({'building_id':'here:40'},{'fuel_prefab':'livinglog'},{'fuel_prefab':[]},
                        {'count':True},{'count':0},{'count':11}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate_target({**plan()['tasks'][0]['target'],**changes})

    def test_progress_requires_completed_native_additions_and_survives_review(self):
        runner=PriorityBoardExecutor(); s=actual(); p=plan(); nav=ExplorationExecutor()
        runner.adopt(p)
        self.assertEqual(runner.choose(p,s,{'add_fuel:40:log':action()},nav)[1],'add_fuel:40:log')
        runner.on_result('fuel','add_fuel:40:log',action(),'failed','timeout',nav,s)
        self.assertEqual(runner.view(p,s)['tasks'][0]['progress']['current'],0)
        runner.on_result('fuel','add_fuel:40:log',action(),'completed','observed_effect',nav,s)
        self.assertEqual(runner.view(p,s)['tasks'][0]['progress'],{'current':1,'required':2})
        revised=plan(focus=True); runner.adopt(revised)
        self.assertEqual(runner.view(revised,s)['tasks'][0]['progress']['current'],1)
        runner.on_result('fuel','add_fuel:40:log',action(),'completed','observed_effect',nav,s)
        completed=runner.view(revised,s)
        self.assertEqual(completed['tasks'][0]['status'],'done')
        repeated=plan(); repeated['tasks'][0]['id']='fuel_again'
        repeated=merge_goals(completed,repeated)
        self.assertEqual(repeated['tasks'][0]['id'],'fuel_again')
        self.assertEqual(runner.view(repeated,s)['tasks'][0]['progress']['current'],0)

    def test_increasing_completed_total_only_adds_the_difference(self):
        s=actual();runner=PriorityBoardExecutor();nav=ExplorationExecutor();p=plan(3)
        runner.adopt(p)
        for _ in range(3):
            runner.on_result('fuel','add_fuel:40:log',action(),'completed','observed_effect',nav,s)
        completed=runner.view(p,s)
        self.assertEqual(completed['tasks'][0]['status'],'done')
        revised=merge_goals(completed,plan(5));runner.adopt(revised)
        view=runner.view(revised,s)['tasks'][0]
        self.assertEqual(view['progress'],{'current':3,'required':5})
        self.assertEqual(view['status'],'in_progress')
        additions=0
        while runner.choose(revised,s,{'fuel':action()},nav):
            additions+=1
            self.assertLessEqual(additions,2,'must not add the new total again')
            runner.on_result('fuel','fuel',action(),'completed','observed_effect',nav,s)
        self.assertEqual(additions,2)
        self.assertEqual(runner.view(revised,s)['tasks'][0]['progress'],{'current':5,'required':5})

    def test_lowering_then_increasing_total_preserves_consumed_items(self):
        s=actual();runner=PriorityBoardExecutor();nav=ExplorationExecutor();runner.adopt(plan(5))
        for _ in range(3):runner.on_result('fuel','fuel',action(),'completed','observed_effect',nav,s)
        runner.adopt(plan(2))
        task=runner.view(plan(2),s)['tasks'][0]
        self.assertEqual(task['progress'],{'current':3,'required':2})
        self.assertEqual(task['status'],'done')
        runner.adopt(plan(4))
        self.assertEqual(runner.view(plan(4),s)['tasks'][0]['progress'],{'current':3,'required':4})
        self.assertEqual(runner.view(plan(4),s)['tasks'][0]['status'],'in_progress')

    def test_new_planning_batch_and_retired_board_do_not_erase_same_refuel_id(self):
        s=actual();runner=PriorityBoardExecutor();nav=ExplorationExecutor();runner.adopt(plan(1))
        runner.on_result('fuel','fuel',action(),'completed','observed_effect',nav,s)
        runner.adopt(plan(3),new_batch=True)
        self.assertEqual(runner.view(plan(3),s)['tasks'][0]['progress']['current'],1)
        other=validate_goals({'goal':'Collect','tasks':[{'id':'grass','title':'Grass','target':{
            'type':'inventory','prefab':'cutgrass','count':1}}]},1)
        runner.adopt(other);runner.adopt(plan(3))
        self.assertEqual(runner.view(plan(3),s)['tasks'][0]['progress']['current'],1)

    def test_new_id_stays_distinct_even_when_old_refuel_is_unfinished(self):
        s=actual();runner=PriorityBoardExecutor();nav=ExplorationExecutor();p=plan(3);runner.adopt(p)
        runner.on_result('fuel','fuel',action(),'completed','observed_effect',nav,s)
        fresh=plan(3);fresh['tasks'][0]['id']='fuel_fresh'
        merged=merge_goals(runner.view(p,s),fresh)
        self.assertEqual({t['id'] for t in merged['tasks']},{'fuel','fuel_fresh'})
        runner.adopt(merged)
        counts={t['id']:t['progress']['current'] for t in runner.view(merged,s)['tasks']}
        self.assertEqual(counts,{'fuel':1,'fuel_fresh':0})

    def test_building_or_fuel_change_starts_distinct_progress_and_rejects_old_results(self):
        runner=PriorityBoardExecutor();s=actual();nav=ExplorationExecutor();p=plan();runner.adopt(p)
        runner.on_result('fuel','fuel',action(),'completed','observed_effect',nav,s)
        for change in ({'building_id':'building:41'},{'fuel_prefab':'twigs'}):
            with self.subTest(change=change):
                revised=plan();revised['tasks'][0]['target'].update(change)
                self.assertFalse(progress_signatures_match(task_signature(p['tasks'][0]),task_signature(revised['tasks'][0])))
                runner.adopt(revised)
                runner.on_result('fuel','fuel',action(),'completed','observed_effect',nav,s)
                self.assertEqual(runner.records['fuel']['fuel_added'],0)
        self.assertTrue(progress_signatures_match(task_signature(p['tasks'][0]),task_signature(plan(5)['tasks'][0])))
        self.assertFalse(progress_signatures_match(None,task_signature(p['tasks'][0])))

    def test_only_requested_fuel_and_building_match_and_jev_cannot_select_it(self):
        a=action(); task=plan()['tasks'][0]
        for changes in ({'building_id':'building:41'},{'fuel_prefab':'twigs'}):
            self.assertFalse(PriorityBoardExecutor.local_choices(task,{'fuel':{**a,**changes}}))
        self.assertFalse(local_candidates({}, {'fuel':a})[1])
        s=actual(); s['bridge']['available_actions']['fuel']=a
        self.assertNotIn('fuel',model_game_state(s)['control']['available_actions'])

    def test_unknown_or_wrong_building_blocks_instead_of_faking_completion(self):
        for buildings in ([],[{'id':'building:40','prefab':'researchlab','status':'observed'}],
                          [{'id':'building:40','prefab':'firepit','status':'missing'}]):
            with self.subTest(buildings=buildings):
                s=actual();s['places']['buildings']=buildings
                self.assertEqual(PriorityBoardExecutor().view(plan(),s)['tasks'][0]['status'],'blocked')

    def test_scheduled_refuel_does_not_run_or_count_early(self):
        s=actual(); p=plan(start_at={'day':1,'phase':'night'}); runner=PriorityBoardExecutor()
        self.assertEqual(runner.view(p,s)['tasks'][0]['status'],'scheduled')
        self.assertIsNone(runner.choose(p,s,{'fuel':action()},ExplorationExecutor()))

    def test_residence_checks_target_and_entire_short_approach(self):
        s=actual(); area={'x':0,'z':0,'radius':12}
        self.assertTrue(allowed(action(),s,area))
        s['nearby'][0]['dx']=12
        self.assertFalse(allowed(action(),s,area))

    def test_bridge_preserves_exact_fuel_and_disallows_native_board_auto_execution(self):
        s=actual(); s['bridge']['available_actions']['add_fuel:40:log']=action()
        bridge=GameBridge('offline-token-'*4)
        bridge.exchange({'token':bridge.token,'session_id':'world','sequence':1,'state':s})
        packet=bridge.get_available_actions()
        result=bridge.start_action('add_fuel:40:log',packet['observation_id'])
        self.assertEqual(bridge.actions[result['action_id']]['action']['fuel_guid'],20)
        with self.assertRaises(BridgeError):
            bridge.submit_task_board(plan(),packet['observation_id'],True)

    def test_approaches_exact_fire_and_arrival_does_not_count_as_fuel(self):
        s=distant_fire();runner=PriorityBoardExecutor();p=plan();nav=ExplorationExecutor()
        selected=runner.choose(p,s,s['bridge']['available_actions'],nav)
        self.assertEqual(selected[1],'visit_building:40')
        runner.on_result('fuel',selected[1],selected[2],'completed','destination_observed',nav,s)
        self.assertEqual(runner.view(p,s)['tasks'][0]['progress']['current'],0)
        self.assertEqual(runner.choose(p,s,{'add_fuel:40:log':action()},nav)[1],'add_fuel:40:log')
        wrong={'visit_building:41':{**selected[2],'building_id':'building:41'}}
        self.assertIsNone(runner.choose(p,s,wrong,nav))

    def test_no_fuel_or_schedule_prevents_approach(self):
        s=distant_fire();s['recipe_inventory']['log']=0
        self.assertIsNone(PriorityBoardExecutor().choose(plan(),s,s['bridge']['available_actions'],ExplorationExecutor()))
        s=distant_fire()
        self.assertIsNone(PriorityBoardExecutor().choose(plan(start_at={'day':1,'phase':'night'}),
            s,s['bridge']['available_actions'],ExplorationExecutor()))

    def test_residence_allows_only_whole_path_inside_home_for_fire_approach(self):
        s=distant_fire();route=s['bridge']['available_actions']['visit_building:40']
        area={'home_id':'home:1','x':0,'z':0,'radius':12}
        self.assertTrue(allowed(route,s,area))
        self.assertFalse(allowed({**route,'refuel_approach':False},s,area))
        self.assertFalse(allowed({**route,'mode':'graph'},s,area),'region route estimates cannot prove a bounded local path')
        s['places']['buildings'][0]['route']['distance']=15
        self.assertFalse(allowed(route,s,area),'a detour must not escape despite its endpoint being inside')
        s['places']['buildings'][0]['route']['distance']=3.5
        s['places']['buildings'][0]['x']=11
        self.assertFalse(allowed(route,s,area))

    def test_residence_scheduler_can_approach_compatible_fire(self):
        s=distant_fire();s['places']['home']={'id':'home:1','x':0,'z':0,'arrived':True}
        p=plan();p['tasks'].append({'id':'hold','title':'Stay home','target':{
            'type':'stay_home','home_id':'home:1','radius':12},'duration':.25})
        p={**validate_goals(p,1),'board_id':'residence:1'}
        selected=PriorityBoardExecutor().choose(p,s,s['bridge']['available_actions'],ExplorationExecutor())
        self.assertEqual(selected[1],'visit_building:40')

    def test_planner_accepts_known_fire_and_rejects_wrong_building(self):
        for prefab, accepted in (('firepit',True),('campfire',True),('researchlab',False)):
            s=actual(); s['places']['buildings'][0]['prefab']=prefab
            context=daily_planner.planning_context(s,[])
            payload=json.dumps({'choices':[{'finish_reason':'stop','message':{'content':json.dumps(plan())}}]}).encode()
            with self.subTest(prefab=prefab), patch.object(daily_planner.urllib.request,'urlopen',
                    side_effect=lambda *_a,**_k:io.BytesIO(payload)), patch.object(daily_planner,'record_plan_error'):
                if accepted:
                    result=daily_planner.request_plan('https://example.invalid','key','offline',1,context)
                    self.assertEqual(result['tasks'][0]['target']['type'],'refuel')
                else:
                    with self.assertRaisesRegex(RuntimeError,'campfire and firepit'):
                        daily_planner.request_plan('https://example.invalid','key','offline',1,context)


class RefuelApproachAgentTests(unittest.IsolatedAsyncioTestCase):
    async def test_host_approaches_then_adds_two_items_without_jev(self):
        client=FakeClient();client.state=distant_fire();seen=set()
        def after_start(peer):
            if not peer.bridge.active or peer.bridge.active in seen:
                return
            identity=peer.bridge.active;seen.add(identity)
            a=peer.bridge.actions[identity]['action']
            if a['kind']=='explore':
                peer.state['player']['position']['x']=3.5
                peer.state['nearby'][0].update(dx=2.5,distance=2.5)
                peer.state['places']['buildings'][0].update(arrived=True,route={'status':'arrived'})
                peer.state['bridge']['available_actions']={'add_fuel:40:log':action(),'wait':{'kind':'wait'}}
            elif a['kind']=='add_fuel':
                peer.state['recipe_inventory']['log']-=1
        client.hook=after_start
        agent=MCPAgent(client,lambda *_:self.fail('Refuel approach must not ask JEV'),
                       planner=FakePlanner(plan()),execute=True,poll=.001)
        try:
            for _ in range(3):
                await asyncio.wait_for(agent.cycle(),2)
            choices=[args['choice'] for name,args in client.calls if name=='start_action']
            self.assertEqual(choices,['visit_building:40','add_fuel:40:log','add_fuel:40:log'])
            self.assertEqual(agent.goals.view(agent.plan,client.state)['tasks'][0]['progress'],{'current':2,'required':2})
            self.assertEqual(agent.goals.view(agent.plan,client.state)['tasks'][0]['status'],'done')
        finally:await agent.shutdown()

    async def test_quantity_review_during_native_action_does_not_lose_its_completion(self):
        client=FakeClient();client.state=actual()
        client.state['bridge']['available_actions']={'add_fuel:40:log':action(),'wait':{'kind':'wait'}}
        planner=FakePlanner(plan(3));seen=set();revised=False
        agent=MCPAgent(client,lambda *_:self.fail('Fueling should not ask JEV'),planner=planner,execute=True,poll=.001)
        def review_inflight(peer):
            nonlocal revised
            if not peer.bridge.active or peer.bridge.active in seen:return
            seen.add(peer.bridge.active)
            if not revised:
                revised=True
                planner.plan=plan(5)
                agent.goals.adopt(planner.plan)
        client.hook=review_inflight
        try:
            await asyncio.wait_for(agent.cycle(),2)
            self.assertEqual(agent.goals.view(agent.plan,client.state)['tasks'][0]['progress'],{'current':1,'required':5})
            for _ in range(4):await asyncio.wait_for(agent.cycle(),2)
            starts=[args for name,args in client.calls if name=='start_action']
            self.assertEqual(len(starts),5)
            self.assertEqual(agent.goals.view(agent.plan,client.state)['tasks'][0]['progress'],{'current':5,'required':5})
        finally:await agent.shutdown()
