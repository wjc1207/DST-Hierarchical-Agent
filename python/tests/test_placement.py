"""Camp buildings bind construction and completion to an observed building."""
import asyncio
import copy
import io
import json
import unittest
from unittest.mock import patch
from common.placement import resolve, validate
from common.task_board import board_view, format_board
from mcp_agent import daily_planner
from mcp_agent.priority_board import PriorityBoardExecutor, validate_goals
from mcp_agent.navigation_intents import ExplorationExecutor
from mcp_agent.mcp_agent import MCPAgent
from mcp_server.game_bridge import GameBridge, BridgeError
from test_mcp_agent import FakeClient, FakePlanner, snapshot


def state(x=30):
    s=snapshot();s['world']['time']=.1;s['player']['position']['x']=x
    s['telemetry']={'placement_constraints':True}
    s['places']={'buildings':[{'id':'building:40','guid':40,'prefab':'researchlab','status':'observed','x':0,'z':0}]}
    s['bridge']['available_actions'].update({
        'recipe_firepit':{'kind':'place','recipe_id':'firepit','product':'firepit','x':x+2,'z':0,
                          'rotation':0,'output_count':1,'planner_only':True},
        'visit_building:40':{'kind':'explore','mode':'place','building_id':'building:40','x':0,'z':0,'planner_only':True}})
    s['recipes']={'firepit':{'recipe_id':'firepit','product':'firepit','placement':True,'supported':True}}
    return s


def plan(**changes):
    target={'type':'structure','prefab':'firepit','recipe_id':'firepit',
            'placement':{'anchor_id':'building:40','radius':10},**changes}
    return {**validate_goals({'goal':'Build camp at the machine','tasks':[
        {'id':'fire','title':'Camp firepit','focus':True,'target':target}]},1),'board_id':'placement:1'}


class PlacementTests(unittest.TestCase):
    def test_validate_requires_building_anchor_and_bounded_radius(self):
        self.assertEqual(validate({'anchor_id':'building:40'})['radius'],10)
        for raw in ({'anchor_id':'here:40'},{'anchor_id':'building:40','radius':True},
                    {'anchor_id':'building:40','radius':3},{'anchor_id':'building:40','radius':21},
                    {'anchor_id':'building:40','x':0}):
            with self.subTest(raw=raw),self.assertRaises(ValueError):validate(raw)
        with self.assertRaises(ValueError):
            validate_goals({'goal':'Invalid','tasks':[{'id':'x','title':'X','target':{
                'type':'inventory','prefab':'log','count':1,'placement':{'anchor_id':'building:40'}}}]},1)

    def test_remote_and_global_verified_structures_do_not_complete_scoped_goal(self):
        s=state();s['recipe_entities']=[{'guid':50,'prefab':'firepit','x':30,'z':0}]
        s['places']['buildings'].append({'id':'building:50','prefab':'firepit','status':'observed','x':30,'z':0})
        self.assertEqual(board_view(plan(),s,verified_structures=['firepit'])['tasks'][0]['status'],'ready')
        s['places']['buildings'].append({'id':'building:51','prefab':'firepit','status':'unconfirmed','x':8,'z':0})
        self.assertEqual(board_view(plan(),s)['tasks'][0]['status'],'done')

    def test_actual_world_distance_not_region_or_prefab_only_proves_completion(self):
        s=state(x=8);s['nearby']=[{'guid':50,'prefab':'firepit','dx':3,'dz':0}]
        self.assertEqual(board_view(plan(),s)['tasks'][0]['status'],'ready')
        s['nearby'][0]['dx']=2
        self.assertEqual(board_view(plan(),s)['tasks'][0]['status'],'done')
        rendered=format_board(board_view(plan(),s))
        self.assertIn('near building:40 radius=10',rendered)

    def test_missing_anchor_and_old_mod_block_without_spending_materials(self):
        for change in ('missing','old_mod'):
            with self.subTest(change=change):
                s=state()
                if change=='missing':s['places']['buildings'][0]['status']='missing'
                else:s.pop('telemetry')
                self.assertEqual(PriorityBoardExecutor().view(plan(),s)['tasks'][0]['status'],'blocked')

    def test_scheduler_visits_anchor_before_building_and_scope_changes_reset_completion(self):
        s=state();runner=PriorityBoardExecutor();nav=ExplorationExecutor();p=plan()
        self.assertEqual(runner.choose(p,s,s['bridge']['available_actions'],nav)[1],'visit_building:40')
        s['player']['position']['x']=2
        self.assertEqual(runner.choose(p,s,s['bridge']['available_actions'],nav)[1],'recipe_firepit')
        s['recipe_entities']=[{'guid':50,'prefab':'firepit','x':8,'z':0}]
        self.assertEqual(runner.view(p,s)['tasks'][0]['status'],'done')
        self.assertEqual(runner.view(plan(placement={'anchor_id':'building:40','radius':6}),s)['tasks'][0]['status'],'ready')

    def test_bridge_rejects_outside_area_and_freezes_anchor_coordinates(self):
        for x in (30,2):
            s=state(x);bridge=GameBridge('offline-token-'*4)
            bridge.exchange({'token':bridge.token,'session_id':'world','sequence':1,'state':s})
            packet=bridge.get_available_actions()
            if x==30:
                with self.assertRaisesRegex(BridgeError,'Outside placement'):
                    bridge.start_action('recipe_firepit',packet['observation_id'],placement={'anchor_id':'building:40'})
            else:
                result=bridge.start_action('recipe_firepit',packet['observation_id'],placement={'anchor_id':'building:40'})
                self.assertEqual(bridge.actions[result['action_id']]['action']['placement'],
                                 {'anchor_id':'building:40','radius':10,'x':0,'z':0})
        bridge.active=None
        with self.assertRaisesRegex(BridgeError,'Python priority-board scheduler'):
            bridge.submit_task_board(plan(),packet['observation_id'],True)

    def test_planner_rejects_permanent_camp_building_without_scope_but_accepts_bound_goal(self):
        for scoped in (False,True):
            incoming=plan()
            if not scoped:incoming['tasks'][0]['target'].pop('placement')
            payload=json.dumps({'choices':[{'finish_reason':'stop','message':{'content':json.dumps(incoming)}}]}).encode()
            with self.subTest(scoped=scoped),patch.object(daily_planner.urllib.request,'urlopen',
                side_effect=lambda *_a,**_k:io.BytesIO(payload)),patch.object(daily_planner,'record_plan_error'):
                context=daily_planner.planning_context(state(),[])
                if scoped:
                    result=daily_planner.request_plan('https://example.invalid','key','offline',1,context)
                    self.assertEqual(result['tasks'][0]['target']['placement']['anchor_id'],'building:40')
                else:
                    with self.assertRaisesRegex(RuntimeError,'requires placement'):
                        daily_planner.request_plan('https://example.invalid','key','offline',1,context)


class PlacementAgentTests(unittest.IsolatedAsyncioTestCase):
    async def test_host_passes_scope_to_native_build_command(self):
        client=FakeClient();client.state=state(x=2)
        agent=MCPAgent(client,lambda *_:self.fail('Scoped camp construction should not ask JEV'),
                       planner=FakePlanner(plan()),execute=True,poll=.001)
        try:
            await asyncio.wait_for(agent.cycle(),2)
            command=next(args for name,args in client.calls if name=='start_action')
            self.assertEqual(command['choice'],'recipe_firepit')
            self.assertEqual(command['placement'],{'anchor_id':'building:40','radius':10})
        finally:await agent.shutdown()
