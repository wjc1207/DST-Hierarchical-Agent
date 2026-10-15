"""Collect only verified land routes; pause failed GUIDs without global backoff."""

import copy
import math
import unittest
from pathlib import Path
from unittest.mock import patch

import controller
import jev_agent
from test_regressions import grass, state


class CollectionPathTests(unittest.TestCase):
    def setUp(self):
        controller.COLLECTION_TARGET_PAUSES.clear()
        self.addCleanup(controller.COLLECTION_TARGET_PAUSES.clear)

    def test_unknown_or_blocked_near_target_does_not_hide_reachable_far_target(self):
        for status in ("unknown", "blocked"):
            with self.subTest(status=status):
                near, far = grass(10, 2.57, True), grass(20, 4, True)
                near["collect_path"] = {"status": status, "reason": "no_observed_land_path"}
                observed = state([near, far])
                _, dispatch = jev_agent.build_candidates(observed)
                self.assertEqual(dispatch["collect_grass"]["guid"], 20)
                self.assertEqual(controller.find_collectible_target(observed, "grass")["guid"], 20)
                self.assertFalse(controller.collect_target_completed(near, near))

    def test_missing_path_is_hidden_and_later_observation_can_make_it_available(self):
        target = grass(10, 2, True)
        observed = state([target])
        target.pop("collect_path")
        self.assertNotIn("collect_grass", jev_agent.build_candidates(observed)[0])
        target["collect_path"] = {"status": "ready", "waypoint": {"x": 2, "z": 0}}
        self.assertIn("collect_grass", jev_agent.build_candidates(observed)[0])

    def test_logged_seed_stall_exits_after_two_short_pulses_and_releases_keys(self):
        seed=grass(106216,12.91,True)
        seed.update(prefab="seeds",tags=["_inventoryitem"])
        observed=state([seed])
        with (patch.object(controller,"latest_state",return_value=(observed,100)),
              patch.object(controller,"wait_for_fresh_state",return_value=(observed,101)),
              patch.object(controller,"find_game_window",return_value=1),
              patch.object(controller,"focus_game"),patch.object(controller,"tap") as tap,
              patch.object(controller,"release_movement_keys") as release,patch("builtins.print")):
            self.assertFalse(controller.collect(Path("unused"),"seeds",preferred_guid=106216))
        self.assertEqual(tap.call_count,2)
        self.assertLessEqual(sum(c.args[1] for c in tap.call_args_list),1)
        release.assert_called_once()
        self.assertNotIn("collect_seeds",jev_agent.build_candidates(observed)[0])

    def test_approach_feedback_timeout_pauses_target_without_global_retry(self):
        observed=state([grass(10,8,True)])
        with (patch.object(controller,"latest_state",return_value=(observed,100)),
              patch.object(controller,"wait_for_fresh_state",side_effect=[
                  (observed,101),RuntimeError("Timed out waiting for fresh telemetry")]),
              patch.object(controller,"find_game_window",return_value=1),
              patch.object(controller,"focus_game"),patch.object(controller,"tap") as tap,
              patch.object(controller,"release_movement_keys") as release,patch("builtins.print")):
            self.assertFalse(controller.collect(Path("unused"),"grass"))
        self.assertEqual(tap.call_count,1)
        release.assert_called_once()
        self.assertEqual(controller.COLLECTION_TARGET_PAUSES[(None,10)]["reason"],"approach_feedback_timeout")

    def test_failed_approach_pauses_guid_and_exposes_another_patch(self):
        observed = state([grass(108149, 2.57, True), grass(20, 4, True)])
        with (
            patch.object(controller, "latest_state", return_value=(observed, 100)),
            patch.object(controller, "wait_for_fresh_state", return_value=(observed, 101)),
            patch.object(controller, "find_game_window", return_value=123),
            patch.object(controller, "focus_game"), patch.object(controller, "tap") as tap,
            patch.object(controller, "release_movement_keys"), patch("builtins.print"),
        ):
            self.assertFalse(controller.collect(Path("unused.log"), "grass", preferred_guid=108149))
        self.assertEqual(tap.call_count, 2)
        _, dispatch = jev_agent.build_candidates(observed)
        self.assertEqual(dispatch["collect_grass"]["guid"], 20)
        self.assertEqual(controller.COLLECTION_TARGET_PAUSES[(None, 108149)]["reason"], "approach_not_converging")

    def test_pause_expires_or_is_rechecked_from_a_new_position_and_is_player_scoped(self):
        before = state([grass(10, 2, True)])
        before["player"]["guid"] = 1
        with patch.object(controller.time, "monotonic", return_value=10), patch("builtins.print"):
            controller.pause_collection_target(before, before["nearby"][0], "test")
            self.assertFalse(controller.collect_target_reachable(before, before["nearby"][0]))
            other_player = copy.deepcopy(before)
            other_player["player"]["guid"] = 2
            self.assertTrue(controller.collect_target_reachable(other_player, other_player["nearby"][0]))
        with patch.object(controller.time, "monotonic", return_value=41):
            self.assertTrue(controller.collect_target_reachable(before, before["nearby"][0]))
        with patch.object(controller.time, "monotonic", return_value=50), patch("builtins.print"):
            controller.pause_collection_target(before, before["nearby"][0], "test")
            moved = copy.deepcopy(before)
            moved["player"]["position"]["x"] = 6
            moved["nearby"][0]["dx"] = -4
            self.assertTrue(controller.collect_target_reachable(moved, moved["nearby"][0]))

    def test_detour_follows_waypoints_even_when_euclidean_distance_initially_increases(self):
        def snapshot(px, pz, wx, wz):
            target = grass(10, math.hypot(4 - px, pz), True)
            target.update(dx=4 - px, dz=-pz, collect_path={
                "status": "ready", "reason": "detour", "distance": 10,
                "waypoint": {"x": wx, "z": wz},
            })
            result = state([target])
            result["player"]["position"].update(x=px, z=pz)
            return result
        before = snapshot(0, 0, 0, 3)
        first = snapshot(0, 2.8, 4, 3)
        second = snapshot(3.8, 3, 4, 0)
        near = snapshot(4, 0.5, 4, 0)
        done = state([grass(10, 0.5, False)], cutgrass=1)
        with (
            patch.object(controller, "latest_state", side_effect=[(before, 100), (before, 102), (first, 104), (second, 106)]),
            patch.object(controller, "wait_for_fresh_state", side_effect=[(before, 101), (first, 103), (second, 105), (near, 107)]),
            patch.object(controller, "wait_for_state_condition", return_value=(done, 108)),
            patch.object(controller, "find_game_window", return_value=123),
            patch.object(controller, "focus_game"), patch.object(controller, "tap") as tap,
            patch.object(controller, "release_movement_keys"), patch("builtins.print"),
        ):
            self.assertTrue(controller.collect(Path("unused.log"), "grass"))
        self.assertEqual(tap.call_args_list[0].args[0], [controller.VK["up"]])
        self.assertEqual(tap.call_count, 4)
        self.assertEqual(controller.COLLECTION_TARGET_PAUSES, {})


if __name__ == "__main__":
    unittest.main()
