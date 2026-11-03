"""Grid counts preserve exploration extent across legacy area-based plans."""
import copy
import unittest
from common.exploration_metrics import observed_cells, survey_cells
from common.task_board import format_board
from mcp_agent.priority_board import validate_goals, PriorityBoardExecutor
from mcp_agent.action_sequence import validate_sequence, SequenceExecutor
from mcp_agent.model_state import model_game_state
from test_priority_board import state, board, task


class GridUnitTests(unittest.TestCase):
    def test_default_limits_and_legacy_area_preserve_physical_extent(self):
        self.assertEqual(survey_cells({}),16)
        for area,cells in ((16,1),(256,16),(512,32),(4096,256),(513,33)):
            self.assertEqual(survey_cells({'area':area}),cells)
        for raw in ({'cells':True},{'cells':0},{'cells':257},{'cells':3.5},{'cells':32,'area':512}):
            with self.assertRaises(ValueError):survey_cells(raw)
        raw={'goal':'Survey','tasks':[{'id':'survey','title':'Survey','target':{'type':'survey_region','node_id':'A:FlowerPatch','area':512}}]}
        clean=validate_goals(raw,1)
        self.assertEqual(clean['tasks'][0]['target'],{'type':'survey_region','node_id':'A:FlowerPatch','cells':32})

    def test_32_new_cells_finish_same_goal_as_512_area(self):
        b=validate_goals({'goal':'Survey','tasks':[{'id':'survey','title':'Survey',
            'target':{'type':'survey_region','node_id':'A:FlowerPatch','cells':32}}]},1)
        s=state();region=s['navigation']['graph']['current'];region['observed_cells']=114
        runner=PriorityBoardExecutor();runner.view(b,s)
        region['observed_cells']=145
        view=runner.view(b,s);self.assertEqual(view['tasks'][0]['progress'],{'current':31,'required':32})
        self.assertNotEqual(view['status'],'completed');self.assertIn('31/32',format_board(view))
        self.assertNotIn('格',format_board(view))
        region['observed_cells']=146
        self.assertEqual(runner.view(b,s)['status'],'completed')

    def test_legacy_state_and_model_projection_never_double_scale_cells(self):
        self.assertEqual(observed_cells({'explored_area':512}),32)
        self.assertEqual(observed_cells({'observed_cells':32,'explored_area':512}),32)
        self.assertIsNone(observed_cells({}));self.assertIsNone(observed_cells({'observed_cells':True}))
        s=state();s['navigation']['graph']['current']={'explored_area':512}
        s['navigation']['graph']['routes']=[{'node_id':'B','explored_area':256}]
        original=copy.deepcopy(s)
        facts=model_game_state(s)
        self.assertEqual(facts['navigation']['graph']['current']['observed_cells'],32)
        self.assertEqual(facts['navigation']['graph']['routes'][0]['observed_cells'],16)
        self.assertNotIn('explored_area',facts['navigation']['graph']['current'])
        self.assertEqual(s,original)

    def test_search_budget_stays_16_cells_and_time_budget_is_unchanged(self):
        b=board(task('grass','cutgrass'));s=state();runner=PriorityBoardExecutor()
        runner.room_search[('grass','A:FlowerPatch')]={'seconds':0.,'cells':4}
        runner.navigation_mode='region'
        from mcp_agent.priority_board import task_signature
        signature=task_signature(b['tasks'][0]);targets=runner.navigation_targets(b,s)
        s['navigation']['graph']['current']['observed_cells']=19
        self.assertIsNone(runner.navigation_review_reason(b,s,'grass',signature,targets))
        s['navigation']['graph']['current']['observed_cells']=20
        self.assertEqual(runner.navigation_review_reason(b,s,'grass',signature,targets),'room_search_budget')
        s['navigation']['graph']['current']['observed_cells']=4
        runner.room_search[('grass','A:FlowerPatch')]['seconds']=30
        self.assertEqual(runner.navigation_review_reason(b,s,'grass',signature,targets),'room_search_budget')

    def test_old_sequence_is_normalized_and_missing_counts_do_not_finish(self):
        seq=validate_sequence({'goal':'Survey','steps':[{'action':'explore_current_region','area':512}]},1)
        self.assertEqual(seq['steps'][0]['cells'],32);self.assertNotIn('area',seq['steps'][0])
        s=state();s['navigation']['graph']['current']={'observed_cells':10}
        runner=SequenceExecutor();runner.view(seq,s)
        s['navigation']['graph']['current']['observed_cells']=42
        self.assertEqual(runner.view(seq,s)['status'],'completed')
        b=validate_goals({'goal':'Survey','tasks':[{'id':'survey','title':'Survey',
            'target':{'type':'survey_region','node_id':'A:FlowerPatch','cells':32}}]},1)
        s['navigation']['graph']['current']={}
        view=PriorityBoardExecutor().view(b,s)
        self.assertEqual(view['tasks'][0]['status'],'blocked')
