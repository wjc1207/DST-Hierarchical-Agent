"""Coordinate routing, map transport and real multimodal request shape, offline."""
import asyncio
import base64
import copy
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from PIL import Image
from common.coordinates import observed_point, arrived, move_action, validate_point
from common.task_board import board_view
from mcp_agent import daily_planner as dp
from mcp_agent.map_view import render_map
from mcp_agent.priority_board import validate_goals, PriorityBoardExecutor, merge_goals
from mcp_agent.navigation_intents import ExplorationExecutor, local_candidates
from mcp_agent.mcp_agent import MCPAgent
from mcp_server.game_bridge import GameBridge, BridgeError
from test_mcp_agent import FakeClient, FakePlanner, snapshot


def state():
    s=snapshot();s['world']['time']=.25
    s['bridge']['coordinate_navigation']=True
    s['world_map']={'version':1,'cell_size':4,'cells':{
        f'{x}:0':[x,0,1,'grassland','A',0] for x in range(4)},'sources':[]}
    return s


def board():
    return {**validate_goals({'goal':'Choose a camp','tasks':[
        {'id':'site','title':'Inspect camp site','focus':True,'target':{'type':'move_to','x':12,'z':0}},
        {'id':'build','title':'Build camp','depends_on':['site'],'target':{'type':'structure','prefab':'sciencemachine'}}]},1),
        'board_id':'map:1'}


class CoordinatesTests(unittest.TestCase):
    def test_only_finite_observed_land_and_bounded_radius(self):
        s=state()
        self.assertEqual(observed_point({'x':12,'z':0},s['world_map'])['x'],12)
        for p in ({'x':True,'z':0},{'x':float('nan'),'z':0},{'x':0,'z':0,'arrival_radius':8}):
            with self.assertRaises(ValueError):validate_point(p)
        for p in ({'x':50,'z':0},{'x':0,'z':40}):
            with self.assertRaises(ValueError):observed_point(p,s['world_map'])
        s['world_map']['cells']['3:0'][2]=0
        with self.assertRaises(ValueError):move_action({'x':12,'z':0},s)

    def test_arrival_is_world_distance_not_region_entry_and_is_latched(self):
        s=state();b=board();runner=PriorityBoardExecutor()
        self.assertFalse(arrived(b['tasks'][0]['target'],s)[0])
        self.assertEqual(next(t for t in runner.view(b,s)['tasks'] if t['id']=='site')['status'],'ready')
        s['player']['position']['x']=11
        view=runner.view(b,s)
        self.assertEqual(next(t for t in view['tasks'] if t['id']=='site')['status'],'done')
        s['player']['position']['x']=0
        self.assertEqual(next(t for t in runner.view(b,s)['tasks'] if t['id']=='site')['status'],'done')
        newer=copy.deepcopy(b);newer['tasks'][0]['id']='site_again';newer['tasks'][1]['depends_on']=['site_again']
        merged=merge_goals(view,newer)
        self.assertIn('site_again',[t['id']for t in merged['tasks']])

    def test_planner_only_coordinate_trip_preserves_prerequisites(self):
        s=state();b=board();runner=PriorityBoardExecutor()
        s['bridge']['available_actions']['build_sciencemachine']={'kind':'build_sciencemachine','planner_only':True}
        selected=runner.choose(b,s,s['bridge']['available_actions'],ExplorationExecutor())
        self.assertEqual(selected[1],'move_to')
        self.assertEqual(selected[2]['coordinate_target'],{'x':12,'z':0,'arrival_radius':2})
        self.assertFalse(local_candidates({}, {'move_to':selected[2]})[1])

    def test_unknown_map_and_old_mod_do_not_offer_coordinate_travel(self):
        s=state();s['bridge'].pop('coordinate_navigation')
        with self.assertRaises(ValueError):move_action({'x':12,'z':0},s)
        del s['world_map']
        view=board_view(board(),s)
        self.assertEqual(next(t for t in view['tasks'] if t['id']=='site')['status'],'blocked')

    def test_bridge_coordinate_command_keeps_intent_and_cancellation(self):
        s=state();bridge=GameBridge('offline-token-'*4)
        bridge.exchange({'token':bridge.token,'session_id':'world','sequence':1,'state':s})
        packet=bridge.get_available_actions()
        with self.assertRaises(ValueError):bridge.start_action('move_to',packet['observation_id'],destination={'x':100,'z':0})
        result=bridge.start_action('move_to',packet['observation_id'],[],destination={'x':12,'z':0})
        action=bridge.actions[result['action_id']]['action']
        self.assertEqual(action['mode'],'coordinate');self.assertEqual(action['coordinate_target']['x'],12)
        self.assertEqual(bridge.cancel_action(result['action_id'])['status'],'cancel_requested')
        with self.assertRaises(BridgeError):bridge.submit_task_board(board(),packet['observation_id'],True)

    def test_map_transport_patch_cached_cells_and_resync(self):
        bridge=GameBridge('offline-token-'*4);s=state()
        def send(sequence):return bridge.exchange({'token':bridge.token,'session_id':'world','sequence':sequence,'state':s})
        send(1)
        s['world_map']={'version':2,'cell_size':4,'patch':{'base_version':1,'updates':{'4:0':[4,0,1,'savanna','A',1]},'removed':['0:0']}}
        result=send(2);self.assertFalse(result['need_world_map'])
        self.assertIn('4:0',bridge.state['world_map']['cells']);self.assertNotIn('0:0',bridge.state['world_map']['cells'])
        s['world_map']={'version':2,'cell_size':4};send(3)
        self.assertIn('4:0',bridge.state['world_map']['cells'])
        s['world_map']={'version':9,'cell_size':4,'patch':{'base_version':8,'updates':{},'removed':[]}}
        self.assertTrue(send(4)['need_world_map']);self.assertEqual(bridge.state['world_map']['cells'],{})


class MapVisionTests(unittest.TestCase):
    def test_png_projection_axes_and_preview_artifact(self):
        context=dp.planning_context(state(),[])['_map_context']
        context['world_map']['sources']=[{'x':8,'z':8,'prefab':'evergreen'}]
        with tempfile.TemporaryDirectory() as tmp:
            meta,url=render_map(context,center_x=0,center_z=0,span=64,output_dir=tmp)
            image=Image.open(io.BytesIO(base64.b64decode(url.split(',',1)[1])))
            self.assertEqual(image.size,(1120,1360));self.assertEqual(meta['bounds']['min_z'],-32)
            self.assertTrue((Path(tmp)/'planner_map.png').is_file())
            self.assertTrue((Path(tmp)/'planner_map.json').is_file())
            # World x=12,z=0 cell is right of the origin; z increases upwards.
            self.assertEqual(image.getpixel((747,544)),(137,152,90))
            self.assertEqual(meta['resource_groups'],1,'evergreen trees must be visible to camp selection')
            self.assertNotEqual(image.getpixel((684,430)),(137,152,90),'tree image must cover its mapped position')
        with self.assertRaises(ValueError):render_map(context,center_x=10)

    def test_raw_map_not_repeated_in_text_or_history(self):
        context=dp.planning_context(state(),[])
        text=dp.snapshot_message(context)
        self.assertNotIn('_map_context',text);self.assertNotIn('data:image',text)
        self.assertEqual(json.loads(text)['map_summary']['observed_cells'],4)

    def request_without_map(self, plan, context, *, accepted=True):
        payload = json.dumps({'choices':[{'finish_reason':'stop',
            'message':{'content':json.dumps(plan)}}]}).encode()
        with patch.object(dp.urllib.request, 'urlopen', side_effect=lambda *_a, **_k: io.BytesIO(payload)) as send, \
                patch.object(dp, 'record_plan_error'):
            if accepted:
                result = dp.request_plan('https://example.invalid', 'test-key', 'offline', 1, context)
                self.assertEqual(send.call_count, 1, 'reusing a site must not need a retry or map tool round')
                return result
            with self.assertRaises(RuntimeError) as failure:
                dp.request_plan('https://example.invalid', 'test-key', 'offline', 1, context)
            self.assertEqual(send.call_count, 2)
            return str(failure.exception)

    def camp_plan(self):
        plan = board()
        plan['camp_site'] = {'x':12, 'z':0, 'reason':'Established camp',
                             'backup':{'x':8, 'z':0, 'reason':'Existing backup'}}
        return plan

    def test_accepted_camp_coordinates_reused_without_map_query(self):
        previous = self.camp_plan()
        context = dp.planning_context(state(), [])
        context['current_task_board'] = copy.deepcopy(previous)
        original_context = copy.deepcopy(context)
        for remove_backup in (False, True):
            with self.subTest(remove_backup=remove_backup):
                plan = copy.deepcopy(previous)
                plan['camp_site'].update(reason='Return here and wait for night', arrival_radius=3)
                if remove_backup:
                    plan['camp_site'].pop('backup')
                else:
                    plan['camp_site']['backup'].update(reason='Still the same backup', arrival_radius=1)
                result = self.request_without_map(plan, context)
                self.assertEqual(result['camp_site']['x'], 12)
                self.assertEqual(result['camp_site']['arrival_radius'], 3)
        self.assertEqual(context, original_context, 'validation must not mutate the frozen planning context')

    def test_first_selection_or_changed_camp_coordinates_still_require_map(self):
        previous = self.camp_plan()
        cases = [('first_selection', None, copy.deepcopy(previous))]
        for label, point in (('primary', 'camp_site'), ('backup', 'backup')):
            plan = copy.deepcopy(previous)
            selected = plan['camp_site'] if point == 'camp_site' else plan['camp_site']['backup']
            selected['x'] -= .1  # A move within the same map cell/radius is still a new selection.
            cases.append((label, previous, plan))
        without_backup = copy.deepcopy(previous); without_backup['camp_site'].pop('backup')
        cases.append(('added_backup', without_backup, previous))
        for label, accepted, incoming in cases:
            with self.subTest(label=label):
                context = dp.planning_context(state(), [])
                if accepted:
                    context['current_task_board'] = accepted
                error = self.request_without_map(incoming, context, accepted=False)
                self.assertIn('Call query_map', error)

    def test_previous_camp_only_used_when_no_current_board_exists(self):
        plan = self.camp_plan()
        context = dp.planning_context(state(), [])
        context['previous_task_board'] = {**copy.deepcopy(plan), 'status':'completed'}
        self.request_without_map(plan, context)
        for current in (board(), self.camp_plan()):
            with self.subTest(current_has_site='camp_site' in current):
                if 'camp_site' in current:
                    current['camp_site']['x'] = 8
                context['current_task_board'] = current
                error = self.request_without_map(plan, context, accepted=False)
                self.assertIn('Call query_map', error)

    def test_reused_site_still_requires_current_observed_passable_land(self):
        plan = self.camp_plan()
        for label in ('blocked_primary', 'unobserved_backup', 'no_map'):
            with self.subTest(label=label):
                s = state()
                if label == 'blocked_primary':
                    s['world_map']['cells']['3:0'][2] = 0
                elif label == 'unobserved_backup':
                    del s['world_map']['cells']['2:0']
                else:
                    s.pop('world_map')
                context = dp.planning_context(s, [])
                context['current_task_board'] = copy.deepcopy(plan)
                error = self.request_without_map(plan, context, accepted=False)
                self.assertNotIn('Call query_map', error)
                self.assertIn('observed', error.lower())

    def test_model_receives_real_image_after_all_tool_replies(self):
        class Response:
            def __init__(self,message):self.message=message
            def __enter__(self):return self
            def __exit__(self,*_):pass
            def read(self):return json.dumps({'choices':[{'finish_reason':'stop','message':self.message}]}).encode()
        first={'content':None,'tool_calls':[{'id':'map1','type':'function','function':{'name':'query_map','arguments':'{}'}}]}
        plan=board();plan['camp_site']={'x':12,'z':0,'reason':'Open observed grassland','backup':{'x':8,'z':0,'reason':'Nearby option'}}
        responses=[Response(first),Response({'content':json.dumps(plan)})]
        with patch.object(dp.urllib.request,'urlopen',side_effect=responses) as send, tempfile.TemporaryDirectory() as tmp, patch.object(dp,'LOG_DIR',Path(tmp)):
            result=dp.request_plan('https://example.invalid','test-key','deepseek-flash',1,dp.planning_context(state(),[]))
        messages=json.loads(send.call_args.args[0].data)['messages']
        self.assertEqual(messages[-2]['role'],'tool');self.assertEqual(messages[-1]['role'],'user')
        parts=messages[-1]['content'];self.assertEqual(parts[1]['type'],'image_url')
        self.assertTrue(parts[1]['image_url']['url'].startswith('data:image/png;base64,'))
        self.assertEqual(result['camp_site']['backup']['x'],8)


class CoordinateAgentTests(unittest.IsolatedAsyncioTestCase):
    async def test_host_sends_dynamic_target_through_the_bridge(self):
        client=FakeClient();client.state=state()
        agent=MCPAgent(client,lambda *_:self.fail('coordinate navigation must not call JEV'),
                       planner=FakePlanner(board()),execute=True,poll=.001)
        try:
            await asyncio.wait_for(agent.cycle(),2)
            args=next(a for name,a in client.calls if name=='start_action')
            self.assertEqual(args['choice'],'move_to');self.assertEqual(args['destination']['x'],12)
            self.assertEqual(next(iter(client.bridge.actions.values()))['action']['coordinate_target']['z'],0)
        finally:await agent.shutdown()
