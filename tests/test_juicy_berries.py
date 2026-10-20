"""Juicy berry handling uses the actual prefab names through the whole pipeline."""
import copy
import unittest
from pathlib import Path
from unittest.mock import patch

import controller
import jev_agent
from exploration_events import ExplorationEvents
from test_exploration_events import available
from test_regressions import grass, lit_campfire, state


class JuicyBerryTests(unittest.TestCase):
    def bush(self, pickable=True):
        return {**grass(22, 0.8, pickable), "prefab": "berrybush_juicy"}

    def test_candidates_require_explicit_pickability_and_ready_route(self):
        for pickable, route, expected in ((True, "ready", True), (False, "ready", False),
                                          (None, "ready", False), (True, "unknown", False)):
            with self.subTest(pickable=pickable, route=route):
                bush = self.bush(pickable)
                bush["collect_path"]["status"] = route
                criteria, dispatch = jev_agent.build_candidates(state([bush]))
                self.assertEqual("collect_berrybush_juicy" in criteria, expected)
                if expected:
                    self.assertEqual(dispatch["collect_berrybush_juicy"]["guid"], 22)

    def test_new_type_interrupts_once_and_regrowth_changes_the_available_set(self):
        detector = ExplorationEvents(state([]), available)
        ready = state([self.bush()])
        event = detector.check(ready, night_unsafe=False, food_available=False, distance=4)
        self.assertEqual(event["reason"], "resource_types_changed")
        self.assertEqual(event["added_types"], ["berries_juicy"])
        self.assertIsNone(detector.check(ready, night_unsafe=False, food_available=False))
        detector.check(state([self.bush(False)]), night_unsafe=False, food_available=False, distance=4)
        self.assertEqual(detector.check(ready, night_unsafe=False, food_available=False, distance=4)["added_types"],
                         ["berries_juicy"])

    def test_loose_raw_and_cooked_berries_are_collectible_and_edible(self):
        for prefab in ("berries_juicy", "berries_juicy_cooked"):
            with self.subTest(prefab=prefab):
                item = {"prefab": prefab, "guid": 30, "distance": 0.5,
                        "tags": ["_inventoryitem"], "dx": 0.5, "dz": 0}
                self.assertIn("collect_" + prefab, jev_agent.build_candidates(state([item]))[0])
                carried = state([])
                carried["player"]["inventory"]["items"] = [{"prefab": prefab, "count": 3}]
                carried["player"]["vitals"]["hunger"] = 3
                self.assertEqual(set(jev_agent.build_candidates(carried)[0]), {"eat_safe_food"})
                self.assertEqual(controller.safe_food_count(carried), 3)
                self.assertEqual(jev_agent.default_execution_threshold("eat_safe_food", carried), 0)

    def test_collect_verifies_actual_juicy_berry_inventory_gain(self):
        before = state([self.bush()])
        after = copy.deepcopy(before)
        after["player"]["inventory"]["items"] = [{"prefab": "berries_juicy", "count": 3}]
        with (
            patch.object(controller, "latest_state", return_value=(before, 100)),
            patch.object(controller, "find_game_window", return_value=123),
            patch.object(controller, "focus_game"), patch.object(controller, "tap"),
            patch.object(controller, "release_movement_keys"),
            patch.object(controller, "wait_for_fresh_state", return_value=(before, 101)),
            patch.object(controller, "wait_for_state_condition", return_value=(after, 102)) as verified,
        ):
            controller.collect(Path("unused.log"), "berrybush_juicy", preferred_guid=22)
        self.assertFalse(verified.call_args.args[2](before))
        self.assertTrue(verified.call_args.args[2](after))

    def test_cook_verifies_juicy_berries_and_eat_cooks_first_near_fire(self):
        raw = state([lit_campfire()])
        raw["player"]["inventory"]["items"] = [{"prefab": "berries_juicy", "count": 1}]
        cooked = copy.deepcopy(raw)
        cooked["player"]["inventory"]["items"] = [{"prefab": "berries_juicy_cooked", "count": 1}]
        self.assertEqual(jev_agent.COOKABLE_FOOD_PRODUCTS["berries_juicy"], "berries_juicy_cooked")
        self.assertIn("cook_food", jev_agent.build_candidates(raw)[0])
        with (
            patch.object(controller, "latest_state", return_value=(raw, 100)),
            patch.object(controller, "find_game_window", return_value=123),
            patch.object(controller, "focus_game"), patch.object(controller, "tap"),
            patch.object(controller, "wait_for_state_condition", return_value=(cooked, 101)) as verified,
        ):
            controller.cook_food(Path("unused.log"))
        self.assertFalse(verified.call_args.args[2](raw))
        self.assertTrue(verified.call_args.args[2](cooked))
        eaten = state([lit_campfire()])
        with (
            patch.object(controller, "latest_state", side_effect=[(raw, 100), (cooked, 101)]),
            patch.object(controller, "cook_food") as cook,
            patch.object(controller, "find_game_window", return_value=123),
            patch.object(controller, "focus_game"), patch.object(controller, "tap"),
            patch.object(controller, "wait_for_state_condition", return_value=(eaten, 102)) as verified,
        ):
            controller.eat_safe_food(Path("unused.log"))
        cook.assert_called_once()
        self.assertTrue(verified.call_args.args[2](eaten))
