"""Shared facts and configuration without windows, keyboard or file-log control."""
import ast
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from common import game_rules as rules
from common.config import load_dotenv
from common.paths import DATA_DIR, PROJECT_ROOT
from mcp_agent import jev_client


class RuleTests(unittest.TestCase):
    def test_survival_thresholds_require_known_vitals_and_include_no_food_hunger(self):
        for name in ("health", "hunger", "sanity"):
            for value, maximum, expected in ((29,100,True),(30,100,False),(0,0,False),
                                              (0,None,False),(float("nan"),100,False)):
                with self.subTest(vital=name,value=value,maximum=maximum):
                    s={"player":{"vitals":{name:value,name+"_max":maximum}}}
                    self.assertEqual("critical_"+name in rules.survival_dangers(s), expected)
        self.assertFalse(rules.survival_dangers({}))

    def test_pickable_sources_require_explicit_positive_evidence(self):
        for prefab in rules.PICKABLE_SOURCE_PREFABS:
            for pickable, expected in ((None,False),(False,False),(True,True)):
                with self.subTest(prefab=prefab,pickable=pickable):
                    self.assertEqual(rules.collect_target_available({"prefab":prefab,"pickable":pickable,"tags":["pickable"]}), expected)

    def test_loose_resources_and_paths_are_independent_facts(self):
        loose={"prefab":"goldnugget","tags":["_inventoryitem"]}
        self.assertTrue(rules.collect_target_available(loose))
        self.assertFalse(rules.collect_target_reachable({},loose))
        loose["collect_path"]={"status":"ready","waypoint":{"x":1,"z":2}}
        self.assertTrue(rules.collect_target_reachable({},loose))
        loose["collect_path"]["waypoint"]["x"]=float("nan")
        self.assertFalse(rules.collect_target_reachable({},loose))

    def test_juicy_berries_flowers_and_tools_keep_their_meanings(self):
        self.assertEqual(rules.COLLECT_PRODUCTS["berrybush_juicy"],"berries_juicy")
        self.assertEqual(rules.COLLECT_PRODUCTS["flower"],"petals")
        self.assertIn("berries_juicy",rules.RAW_COOKABLE_FOOD_PREFABS)
        self.assertNotIn("petals",rules.SAFE_FOOD_PREFABS)
        self.assertFalse(rules.is_weapon_item({"prefab":"torch","weapon":True}))
        self.assertTrue(rules.is_weapon_item({"prefab":"pickaxe"}))

    def test_tool_rounding_uses_whole_target_conservative_uses_not_recipe_availability(self):
        state={"crafting":{"pickaxe":True},"player":{"inventory":{"items":[],"equipped":[]}}}
        self.assertEqual(rules.conservative_tool_uses(state,"pickaxe",33),0)
        state["player"]["inventory"]["equipped"]=[{"prefab":"pickaxe","durability_percent":.15}]
        self.assertEqual(rules.conservative_tool_uses(state,"pickaxe",33),5)
        state["player"]["inventory"]["items"]=[{"prefab":"pickaxe","durability_percent":.18}]
        self.assertEqual(rules.conservative_tool_uses(state,"pickaxe",33),6)
        self.assertIn("goldnugget",rules.MINE_PRODUCTS["rock2"])
        self.assertNotIn("goldnugget",rules.MINE_PRODUCTS["rock1"])

    def test_configuration_preserves_existing_environment_without_logging_values(self):
        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT/"tests") as folder:
            path=Path(folder)/"test.env"
            path.write_text('# ignored\nDST_TEST_KEY="example"\nDST_TEST_OTHER=value\n',encoding="utf-8")
            with patch.dict(os.environ,{"DST_TEST_KEY":"existing"}),patch("builtins.print") as printed:
                load_dotenv(path)
                self.assertEqual(os.environ["DST_TEST_KEY"],"existing")
                self.assertEqual(os.environ["DST_TEST_OTHER"],"value")
                printed.assert_not_called()

    def test_knowledge_assets_live_under_data_and_old_entry_points_are_gone(self):
        for name in ("DSTKnowledge.txt","worldgen_resources.json"):
            self.assertTrue((DATA_DIR/name).is_file())
            self.assertFalse((PROJECT_ROOT/name).exists())
        for name in ("controller.py","jev_agent.py","board_executor.py","telemetry.py","watch_state.py","action_buffer.py","decision_prefetch.py"):
            self.assertFalse((PROJECT_ROOT/name).exists(),name)

    def test_runtime_import_graph_has_no_retired_or_windows_input_modules(self):
        forbidden={"controller","jev_agent","telemetry","board_executor","decision_prefetch","action_buffer","ctypes","win32api","pyautogui","pynput"}
        for package in ("common","mcp_agent","mcp_server"):
            for path in (PROJECT_ROOT/package).glob("*.py"):
                for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                    names=([n.name for n in node.names] if isinstance(node,ast.Import) else
                           [node.module or ""] if isinstance(node,ast.ImportFrom) else [])
                    self.assertFalse({n.split('.')[0] for n in names}&forbidden,str(path))
        result=subprocess.run([sys.executable,"-c","import sys; import mcp_agent.mcp_agent; assert not ({'controller','jev_agent','telemetry','decision_prefetch','action_buffer'} & set(sys.modules))"],
                              cwd=PROJECT_ROOT,capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr)

    def test_model_client_filters_all_navigation_keys_without_a_keyboard_candidate_builder(self):
        class Response:
            def __enter__(self):return self
            def __exit__(self,*_):pass
            def read(self):return b'{"answers":{"next_action":{"choice":"wait"}}}'
        state={"world":{"day":1,"phase":"day"},"player":{"inventory":{"items":[],"equipped":[]}},"nearby":[]}
        criteria={"wait":"Wait","explore_current_region":"Explore","travel_region_B":"Travel","navigate_resource_4":"Source"}
        with patch.object(jev_client.urllib.request,"urlopen",return_value=Response()) as send:
            jev_client.call_jev("https://offline.invalid","offline","mock",state,criteria)
        body=json.loads(send.call_args.args[0].data)
        self.assertEqual(body["questions"]["next_action"]["criteria"],{"wait":"Wait"})


if __name__=="__main__":
    unittest.main()
