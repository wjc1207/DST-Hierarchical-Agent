"""Catalogue-delta correctness and bounded diagnostic fields; no live game."""
import copy
import math
import unittest

from mcp_server.game_bridge import GameBridge
from mcp_server.transport_metrics import lua_measurements
from test_game_bridge import state, TOKEN


class PerformanceProtocolTests(unittest.TestCase):
    def setUp(self):
        self.now = 0
        self.bridge = GameBridge(TOKEN, clock=lambda: self.now)
        self.sequence = 0

    def send(self, data, session="performance"):
        self.sequence += 1
        return self.bridge.exchange({"token": TOKEN, "session_id": session,
                                     "sequence": self.sequence, "state": data})

    def full(self, version=1):
        data = state()
        data.update(recipe_version=version, recipes={"boards": {"recipe_id": "boards", "ready": True}})
        return data

    def test_catalogue_delta_preserves_queries_and_fresh_observations(self):
        first = self.full()
        result = self.send(first)
        self.assertTrue(result["recipe_catalog_deltas"])
        self.assertFalse(result["need_recipe_catalog"])
        first["recipes"]["boards"]["ready"] = False
        delta = state(); delta["recipe_version"] = 1
        delta["player"]["position"]["x"] = 8
        result = self.send(delta)
        self.assertFalse(result["need_recipe_catalog"])
        observed = self.bridge.get_state()["state"]
        self.assertEqual(observed["player"]["position"]["x"], 8)
        self.assertTrue(observed["recipes"]["boards"]["ready"])
        observed["recipes"].clear()
        self.assertIn("boards", self.bridge.get_state()["state"]["recipes"])

    def test_missing_or_mismatched_catalogue_requests_resend(self):
        delta = state(); delta["recipe_version"] = 2
        self.assertTrue(self.send(delta)["need_recipe_catalog"])
        self.assertEqual(self.bridge.get_state()["state"]["recipes"], {})
        self.assertFalse(self.send(self.full(2))["need_recipe_catalog"])
        delta["recipe_version"] = 3
        self.assertTrue(self.send(delta)["need_recipe_catalog"])
        self.assertEqual(self.bridge.get_state()["state"]["recipes"], {})

    def test_new_world_cannot_inherit_an_old_worlds_catalogue(self):
        self.send(self.full())
        self.now = 4
        delta = state(); delta["recipe_version"] = 1
        result = self.send(delta, session="new-world")
        self.assertTrue(result["need_recipe_catalog"])
        self.assertEqual(self.bridge.get_state()["state"]["recipes"], {})

    def test_recipe_patches_update_remove_and_preserve_unchanged_rows(self):
        self.send(self.full())
        delta = state()
        delta.update(recipe_version=2, recipe_patch={"base_version": 1,
            "updates": {"rope": {"recipe_id": "rope", "ready": False}}, "removed": []})
        result = self.send(delta)
        self.assertTrue(result["recipe_catalog_patches"])
        observed = self.bridge.get_state()["state"]["recipes"]
        self.assertIn("boards", observed)
        self.assertIn("rope", observed)
        delta.update(recipe_version=3, recipe_patch={"base_version": 2,
            "updates": {}, "removed": ["boards"]})
        self.assertFalse(self.send(delta)["need_recipe_catalog"])
        self.assertEqual(set(self.bridge.get_state()["state"]["recipes"]), {"rope"})

    def test_wrong_patch_base_requests_full_catalogue_instead_of_applying(self):
        self.send(self.full())
        delta = state()
        delta.update(recipe_version=2, recipe_patch={"base_version": 99,
            "updates": {"invented": {}}, "removed": []})
        self.assertTrue(self.send(delta)["need_recipe_catalog"])
        self.assertEqual(self.bridge.get_state()["state"]["recipes"], {})

    def test_query_masks_stale_readiness_without_resending_all_rows(self):
        from common.recipes import query_recipes
        catalogue = {"boards": {"recipe_id": "boards", "ready": True, "fact_epoch": 1}}
        result = query_recipes(catalogue, epoch=2)["recipes"][0]
        self.assertFalse(result["fresh"])
        self.assertFalse(result["ready"])
        self.assertEqual(result["reason"], "recipe_refresh_pending")
        self.assertTrue(catalogue["boards"]["ready"])

    def test_legacy_snapshots_still_work(self):
        result = self.send(state())
        self.assertFalse(result["need_recipe_catalog"])
        self.assertNotIn("recipes", self.bridge.get_state()["state"])

    def test_fps_and_new_stages_are_logged_without_non_numeric_payloads(self):
        value = {"clock": "lua_walltime_seconds", "performance": {
            "source": "frontend_wall_update", "available": True, "fps": 24.5,
            "frame_ms": 40.8, "p95_frame_ms": 80, "max_frame_ms": 120,
            "frames": 123, "window_seconds": 5.02, "token": "secret",
            "age_seconds": -1, "sampled_at": math.nan},
            "sampling": {"ticks": 2, "sum_ms": {"recipes_ms": 4, "places_ms": 2}}}
        result = lua_measurements(value, 1)
        self.assertEqual(result["clock"], "lua_walltime_seconds")
        self.assertEqual(result["performance"]["fps"], 24.5)
        for key in ("token", "sampled_at", "age_seconds"):
            self.assertNotIn(key, result["performance"])
        self.assertEqual(result["sampling"]["sum_ms"], {"recipes_ms": 4, "places_ms": 2})

    def test_unknown_fps_is_not_logged_as_zero(self):
        result = lua_measurements({"performance": {"available": False, "source": "unknown"}}, 1)
        self.assertFalse(result["performance"]["available"])
        self.assertNotIn("fps", result["performance"])
