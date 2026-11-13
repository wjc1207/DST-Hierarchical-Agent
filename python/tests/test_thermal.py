"""Thermal safety owns immediate reactions without widening JEV decisions."""
import unittest
from unittest.mock import AsyncMock, patch
from common.thermal import heat_risk, refuel_pause
from common import timing_log as timing
from mcp_agent.navigation_intents import ExplorationExecutor, local_candidates
from mcp_agent.priority_board import PriorityBoardExecutor
from mcp_agent.mcp_agent import MCPAgent
from mcp_agent.model_state import model_game_state
from test_mcp_agent import FakeClient, FakePlanner, snapshot, board
from test_refueling import actual, action, plan


class ThermalFactsTests(unittest.TestCase):
    def test_unknown_and_invalid_temperature_is_not_heat(self):
        s=snapshot()
        self.assertFalse(heat_risk(s))
        for value in (None,True,float('nan'),'71'):
            s['player']['vitals'].update(temperature=value,heat_warning_threshold=65)
            self.assertFalse(heat_risk(s))
        s['player']['vitals']['overheating']=True
        self.assertTrue(heat_risk(s))

    def test_hysteresis_and_model_facts(self):
        s=snapshot();s['thermal']={'active':True,'status':'cooling'}
        s['player']['vitals'].update(temperature=63,heat_warning_threshold=65,overheating=False)
        self.assertTrue(heat_risk(s))
        facts=model_game_state(s)
        self.assertEqual(facts['thermal'],s['thermal'])
        self.assertEqual(facts['vitals']['temperature'],63)

    def test_sufficient_fire_pauses_without_erasing_fuel_total(self):
        s=actual();p=plan();nav=ExplorationExecutor();runner=PriorityBoardExecutor();runner.adopt(p)
        runner.on_result('fuel','add_fuel:40:log',action(),'completed','observed_effect',nav,s)
        s['refueling']['targets']=[{'building_id':'building:40','reason':'fire_already_sufficient'}]
        task=runner.view(p,s)['tasks'][0]
        self.assertEqual(task['status'],'blocked');self.assertEqual(task['progress']['current'],1)
        s['refueling']['targets'][0]['reason']=''
        self.assertEqual(runner.choose(p,s,{'add_fuel:40:log':action()},nav)[1],'add_fuel:40:log')
        self.assertEqual(runner.view(p,s)['tasks'][0]['progress']['current'],1)

    def test_heat_overrides_other_fuel_reason_and_settling_waits(self):
        s=actual();p=plan();s['refueling']['targets']=[{'building_id':'building:40','reason':'fuel_settling'}]
        runner=PriorityBoardExecutor();runner.adopt(p)
        self.assertIsNone(runner.choose(p,s,{'wait':{'kind':'wait'}},ExplorationExecutor()))
        self.assertEqual(runner.reason,'refuel_settling')
        s['player']['vitals'].update(temperature=66,heat_warning_threshold=65)
        self.assertEqual(refuel_pause(p['tasks'][0]['target'],s),'temperature_too_high')

    def test_cooling_never_enters_jev_options_even_if_marker_missing(self):
        _,choices=local_candidates({}, {'cool_down':{'kind':'cool_down'},'wait':{'kind':'wait'}})
        self.assertNotIn('cool_down',choices)

    def test_hot_time_does_not_count_as_safe_residence(self):
        from mcp_agent.residence import update_residences
        s=snapshot();s['world'].update(day=1,time=.1);s['places']={'home':{'id':'building:40','x':0,'z':0,'arrived':True}}
        task={'id':'stay','status':'ready','target':{'type':'stay_home','home_id':'building:40','radius':12},'duration':.25}
        records={'stay':{'done':False}};view={'state_basis':'observed','tasks':[task]}
        update_residences(view,s,records)
        s['world']['time']=.2;s['thermal']={'active':True};update_residences(view,s,records)
        self.assertEqual(records['stay']['residence']['elapsed'],0)
        s['world']['time']=.3;s['thermal']['active']=False;update_residences(view,s,records)
        s['world']['time']=.4;update_residences(view,s,records)
        self.assertAlmostEqual(records['stay']['residence']['elapsed'],.1)

    def test_cooling_does_not_consume_search_budget(self):
        from test_mcp_agent import board
        s=snapshot();runner=PriorityBoardExecutor(clock=lambda:20);p=board();runner.adopt(p)
        runner.view(p,s);runner.navigation=True;runner.selected='grass';runner.last_tick=10
        runner.last_position={'x':0,'z':0};s['player']['position']['x']=4;s['thermal']={'active':True}
        runner.view(p,s)
        self.assertEqual(runner.records['grass']['seconds'],0)
        self.assertEqual(runner.records['grass']['distance'],0)


class ThermalAgentTests(unittest.IsolatedAsyncioTestCase):
    async def test_safety_bypasses_focus_residence_and_local_model(self):
        client=FakeClient();agent=MCPAgent(client,lambda *_:self.fail('must not ask JEV'),poll=.001)
        agent.plan=board();agent.plan['tasks'][0]['focus']=True;agent.goals.adopt(agent.plan)
        agent.goals.residence={'holding':True,'radius':1,'x':0,'z':0,'task_ids':['stay']}
        s=snapshot();s['thermal']={'active':True};s['player']['vitals']['temperature']=66
        packet={'state':s,'actions':{'cool_down':{'kind':'cool_down','x':4,'z':0}}}
        agent.perform=AsyncMock()
        try:
            self.assertTrue(await agent.handle_thermal(packet))
            agent.perform.assert_awaited_once_with('cool_down',packet,safety=True)
        finally: await agent.shutdown()

    async def test_no_route_holds_and_hostile_yields(self):
        agent=MCPAgent(FakeClient(),lambda *_:self.fail('must not ask JEV'),poll=.001);agent.perform=AsyncMock()
        s=snapshot();s['thermal']={'active':True};packet={'state':s,'actions':{'wait':{'kind':'wait'}}}
        try:
            self.assertTrue(await agent.handle_thermal(packet));agent.perform.assert_not_awaited()
            s['nearby']=[{'activeThreat':True}]
            self.assertFalse(await agent.handle_thermal(packet))
        finally: await agent.shutdown()

    async def test_cooling_cycle_uses_native_action_without_goal_bounds(self):
        client=FakeClient();client.state['thermal']={'active':True};client.state['player']['vitals'].update(temperature=66,overheating=False)
        client.state['bridge']['available_actions']={'cool_down':{'kind':'cool_down','x':4,'z':0,'planner_only':True}}
        agent=MCPAgent(client,lambda *_:self.fail('must not ask JEV'),execute=True,poll=.001)
        try:
            await agent.cycle()
            args=next(args for name,args in client.calls if name=='start_action')
            self.assertEqual(args['choice'],'cool_down');self.assertNotIn('residence',args)
        finally: await agent.shutdown()

    async def test_observation_logs_temperature_and_fire_state(self):
        client=FakeClient();client.state['player']['vitals'].update(temperature=67,overheating=False,freezing=False,heat_warning_threshold=65)
        client.state['thermal']={'active':True,'status':'cooling','reason':'move_away_from_heat'}
        client.state['refueling']={'targets':[{'building_id':'building:40','fire_section':4,'reason':'temperature_too_high'}]}
        agent=MCPAgent(client,lambda *_:None,poll=.001)
        try:
            with patch.object(timing,'annotate') as annotate:
                await agent.observe()
            fields={k:v for call in annotate.call_args_list for k,v in call.kwargs.items()}
            self.assertEqual(fields['temperature'],67)
            self.assertEqual(fields['thermal_reason'],'move_away_from_heat')
            self.assertEqual(fields['fire_state'][0]['fire_section'],4)
        finally: await agent.shutdown()

    async def test_legacy_auto_board_releases_ownership_before_cooling(self):
        client=FakeClient();client.state['thermal']={'active':True}
        client.state['bridge']['available_actions']={'cool_down':{'kind':'cool_down','x':4,'z':0,'planner_only':True}}
        agent=MCPAgent(client,lambda *_:self.fail('must not ask JEV'),execute=True,poll=.001)
        # Use the already supported legacy board fixture rather than priority auto execution.
        agent.uploaded={'kind':'task_board','board_id':'legacy:1','goal':'Wait','tasks':[]}
        agent.uploaded_source='legacy:1';agent.auto_enabled=True;agent.board_acked=True
        agent.sync_board=AsyncMock()
        async def release(*_): agent.auto_enabled=False
        agent.sync_board.side_effect=release
        agent.perform=AsyncMock()
        try:
            packet=await agent.observe()
            self.assertTrue(await agent.handle_thermal(packet))
            agent.sync_board.assert_awaited_once_with(None,False)
            self.assertEqual(agent.perform.await_args.args[0],'cool_down')
        finally: await agent.shutdown()
