"""Flowers are optional collectables, never substituted for hunger food."""
import copy
import unittest
from pathlib import Path
from unittest.mock import patch

import controller
import jev_agent
from action_buffer import predict_success, counts
from exploration_events import ExplorationEvents
from test_exploration_events import available
from test_regressions import grass, state


class FlowerTests(unittest.TestCase):
    def flower(self, pickable=True):
        return {**grass(22, 0.8, pickable), "prefab": "flower"}

    def test_flower_requires_explicit_pickability_and_reachability(self):
        for pickable, route, expected in ((True, "ready", True), (False, "ready", False),
                                          (None, "ready", False), (True, "unknown", False)):
            flower = self.flower(pickable)
            flower["collect_path"]["status"] = route
            criteria, actions = jev_agent.build_candidates(state([flower]))
            self.assertEqual("collect_flower" in criteria, expected)
            if expected:
                self.assertEqual(actions["collect_flower"]["guid"], 22)
        evil = {**self.flower(), "prefab": "flower_evil"}
        self.assertNotIn("collect_flower_evil", jev_agent.build_candidates(state([evil]))[0])

    def test_new_flower_interrupts_exploration_once(self):
        detector = ExplorationEvents(state([]), available, set())
        actual = state([self.flower()])
        event = detector.check(actual, night_unsafe=False, food_available=False)
        self.assertEqual((event["reason"], event["prefab"]), ("new_resource", "flower"))
        self.assertIsNone(detector.check(actual, night_unsafe=False, food_available=False))

    def test_pick_verifies_petals_gain_and_prediction_removes_flower(self):
        before = state([self.flower()])
        after = copy.deepcopy(before)
        after["player"]["inventory"]["items"] = [{"prefab": "petals", "count": 1}]
        predicted = predict_success(before, {"kind": "collect", "prefab": "flower", "guid": 22})
        self.assertEqual(counts(predicted)["petals"], 1)
        self.assertEqual(predicted["nearby"], [])
        with (
            patch.object(controller, "latest_state", return_value=(before, 100)),
            patch.object(controller, "find_game_window", return_value=123),
            patch.object(controller, "focus_game"), patch.object(controller, "tap"),
            patch.object(controller, "release_movement_keys"),
            patch.object(controller, "wait_for_fresh_state", return_value=(before, 101)),
            patch.object(controller, "wait_for_state_condition", return_value=(after, 102)) as verified,
        ):
            self.assertTrue(controller.collect(Path("unused.log"), "flower", preferred_guid=22))
        self.assertFalse(verified.call_args.args[2](before))
        self.assertTrue(verified.call_args.args[2](after))
        self.assertTrue(controller.collect_target_completed(self.flower(), None))

    def test_loose_petals_are_collectible_but_not_hunger_food(self):
        petals = {"prefab": "petals", "guid": 30, "distance": 0.5, "dx": 0.5,
                  "dz": 0, "tags": ["_inventoryitem"]}
        self.assertIn("collect_petals", jev_agent.build_candidates(state([petals]))[0])
        carried = state([])
        carried["player"]["vitals"]["hunger"] = 3
        carried["player"]["inventory"]["items"] = [{"prefab": "petals", "count": 10}]
        self.assertEqual(controller.safe_food_count(carried), 0)
        self.assertNotIn("eat_safe_food", jev_agent.build_candidates(carried)[0])
