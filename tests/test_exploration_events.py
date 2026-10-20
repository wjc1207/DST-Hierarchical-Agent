"""Exploration continues without model calls until something meaningful changes."""
import copy
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import controller
import jev_agent
from exploration_events import ExplorationEvents, resource_types
from test_exploration import observed
from test_regressions import grass, state


def available(value, entity):
    return (entity.get("prefab") in controller.EXPLORATION_RESOURCE_PREFABS
            and controller.collect_target_available(entity)
            and controller.collect_target_reachable(value, entity))


def berry(guid=22):
    entity = grass(guid, 2, True)
    entity["prefab"] = "berrybush"
    return entity


class EventTests(unittest.TestCase):
    def test_discovering_a_road_no_longer_interrupts_exploration(self):
        initial = observed()
        detector = ExplorationEvents(initial, available)
        changed = copy.deepcopy(initial)
        changed["navigation"]["road"] = {"status": "ready"}
        changed["navigation"]["on_road"] = True
        self.assertIsNone(detector.check(changed, night_unsafe=False, food_available=False))

    def test_more_berries_or_replacement_guids_do_not_change_the_set(self):
        initial = state([berry(1)])
        detector = ExplorationEvents(initial, available)
        for nearby in ([berry(1)], [berry(1), berry(2)], [berry(3)], [berry(i) for i in range(100)]):
            self.assertIsNone(detector.check(state(nearby), night_unsafe=False,
                                             food_available=False, distance=100))

    def test_types_appear_and_disappear_with_deterministic_set_details(self):
        detector = ExplorationEvents(state([berry(1)]), available)
        changed = state([berry(2), grass(3, 2, True)])
        event = detector.check(changed, night_unsafe=False, food_available=False, distance=4)
        self.assertEqual(event, {"reason": "resource_types_changed", "before_types": ["berries"],
            "resource_types": ["berries", "cutgrass"], "added_types": ["cutgrass"], "removed_types": []})
        self.assertIsNone(detector.check(changed, night_unsafe=False, food_available=False, distance=4))
        removed = detector.check(state([grass(4, 2, True)]), night_unsafe=False,
                                 food_available=False, distance=5)
        self.assertEqual(removed["removed_types"], ["berries"])
        self.assertEqual(removed["added_types"], [])

    def test_sources_and_loose_products_share_a_semantic_type(self):
        sapling = {**grass(1, 2, True), "prefab": "sapling"}
        loose = {**grass(2, 2, True), "prefab": "twigs", "tags": ["_inventoryitem"]}
        carrot = {**grass(3, 2, True), "prefab": "carrot_planted"}
        self.assertEqual(resource_types(state([sapling, loose, berry(4), carrot]), available),
                         {"twigs", "berries", "carrot"})
        detector = ExplorationEvents(state([sapling]), available)
        self.assertIsNone(detector.check(state([loose]), night_unsafe=False, food_available=False, distance=4))
        actual = state([sapling, loose, berry(4), carrot])
        self.assertEqual(jev_agent.compact_game_state(actual)["exploration_resource_types"],
                         ["berries", "carrot", "twigs"])

    def test_resource_types_do_not_require_identity_or_count_metadata(self):
        plant = berry()
        plant.pop("guid")
        self.assertEqual(resource_types(state([plant, {**plant, "stack_count": 100}]), available), {"berries"})

    def test_resource_change_and_four_unit_floor_are_both_required(self):
        detector = ExplorationEvents(state([]), available)
        changed = state([berry()])
        for distance in (0, 1, 3.99):
            self.assertIsNone(detector.check(changed, night_unsafe=False,
                                             food_available=False, distance=distance))
        event = detector.check(changed, night_unsafe=False, food_available=False, distance=4)
        self.assertEqual(event["added_types"], ["berries"])
        self.assertIsNone(detector.check(changed, night_unsafe=False, food_available=False, distance=40))

    def test_short_lived_change_before_floor_is_not_a_stale_pending_event(self):
        detector = ExplorationEvents(state([]), available)
        self.assertIsNone(detector.check(state([berry()]), night_unsafe=False,
                                         food_available=False, distance=2))
        self.assertIsNone(detector.check(state([]), night_unsafe=False, food_available=False, distance=4))

    def test_depleted_and_unreachable_resources_do_not_interrupt(self):
        detector = ExplorationEvents(state([]), available)
        depleted, unreachable = berry(1), berry(2)
        depleted["pickable"] = False
        unreachable["collect_path"] = {"status": "unknown"}
        self.assertIsNone(detector.check(state([depleted, unreachable]),
                                         night_unsafe=False, food_available=False))
        self.assertEqual(detector.check(state([berry(1)]), night_unsafe=False,
                                        food_available=False, distance=4)["reason"], "resource_types_changed")

    def test_known_hound_starting_to_target_player_is_an_event(self):
        hound = {"guid": 80, "prefab": "hound", "potentialThreat": True,
                 "activeThreat": False, "distance": 10}
        initial = state([hound])
        detector = ExplorationEvents(initial, available)
        self.assertIsNone(detector.check(initial, night_unsafe=False, food_available=False))
        changed = copy.deepcopy(initial)
        changed["nearby"][0]["activeThreat"] = True
        event = detector.check(changed, night_unsafe=False, food_available=False)
        self.assertEqual(event["reason"], "targeted_by_hostile")
        self.assertEqual(event["guid"], 80)

    def test_urgent_events_bypass_the_distance_floor(self):
        initial = state([])
        for expected, edit, night_unsafe, food in (
            ("health_lost", lambda s: s["player"]["vitals"].update(health=140), False, False),
            ("critical_hunger_with_food", lambda s: s["player"]["vitals"].update(hunger=1), False, True),
            ("night_light_lost", lambda s: None, True, False),
            ("potential_threat", lambda s: s["nearby"].append({"guid": 30, "prefab": "hound",
                "potentialThreat": True, "distance": 10}), False, False),
        ):
            current = copy.deepcopy(initial)
            edit(current)
            with self.subTest(expected=expected):
                self.assertEqual(ExplorationEvents(initial, available).check(current,
                    night_unsafe=night_unsafe, food_available=food, distance=0)["reason"], expected)

    def test_regrowth_only_matters_when_available_type_set_changes(self):
        for prefab in ("grass", "sapling", "berrybush"):
            with self.subTest(prefab=prefab):
                plant=berry()
                plant["prefab"]=prefab
                detector=ExplorationEvents(state([plant]),available)
                for _ in range(2):
                    empty=copy.deepcopy(plant)
                    empty["pickable"]=False
                    removed=detector.check(state([empty]),night_unsafe=False,food_available=False,distance=4)
                    self.assertEqual(removed["resource_types"],[])
                    event=detector.check(state([plant]),night_unsafe=False,food_available=False,distance=4)
                    self.assertEqual(event["reason"],"resource_types_changed")
                    self.assertTrue(event["added_types"])
                    self.assertIsNone(detector.check(state([plant]),night_unsafe=False,food_available=False,distance=4))

    def test_one_depleted_plant_among_same_available_type_does_not_interrupt(self):
        detector=ExplorationEvents(state([berry(1),berry(2)]),available)
        empty=berry(1); empty["pickable"]=False
        self.assertIsNone(detector.check(state([empty,berry(2)]),night_unsafe=False,
                                         food_available=False,distance=10))
        self.assertIsNone(detector.check(state([berry(1),berry(2)]),night_unsafe=False,
                                         food_available=False,distance=10))

    def test_mixed_ground_change_is_not_a_region_change(self):
        initial = observed()
        detector = ExplorationEvents(initial, available)
        changed = copy.deepcopy(initial)
        changed["navigation"]["current_biome"] = "grassland"
        self.assertIsNone(detector.check(changed, night_unsafe=False, food_available=False))

    def test_survival_discovery_and_arrival_events(self):
        initial = observed()
        for expected, change in (
            ("health_lost", lambda s: s["player"]["vitals"].update(health=140)),
            ("phase_changed", lambda s: s["world"].update(phase="dusk")),
            ("region_discovered", lambda s: s["navigation"]["graph"].update(known_nodes=3)),
            ("destination_reached", lambda s: s["navigation"]["graph"].update(current_node="B")),
            ("player_dead", lambda s: s["player"]["vitals"].update(dead=True)),
        ):
            with self.subTest(expected=expected):
                detector = ExplorationEvents(initial, available, "B")
                changed = copy.deepcopy(initial)
                change(changed)
                self.assertEqual(detector.check(changed, night_unsafe=False,
                                                 food_available=False)["reason"], expected)


class ContinuousExecutionTests(unittest.TestCase):
    def setUp(self):
        self.output = patch("builtins.print")
        self.output.start()
        self.addCleanup(self.output.stop)

    def run_motion(self, event_after=4, event_kind="resource", mid_move=False,
                   step=3, expected_after=None, initial_nearby=None, **limits):
        current = observed()
        if initial_nearby is not None:
            current["nearby"] = state(initial_nearby)["nearby"]
        current["player"]["guid"] = 500
        current["observed_at"] = 1
        current["navigation"]["graph"]["known_nodes"] = 2
        action = jev_agent.build_candidates(current)[1]["explore_other_region"]
        outer = Mock()
        frames = []
        expected_after = event_after if expected_after is None else expected_after

        def move(*_args, **_kwargs):
            nonlocal current
            next_state = copy.deepcopy(current)
            next_state["observed_at"] += 1
            next_state["player"]["position"]["z"] += step
            next_state["navigation"]["search"]["leg"]["z"] += step
            frames.append(next_state)
            if event_kind == "same_type":
                next_state["nearby"] = state([berry(100 + len(frames))])["nearby"]
            if len(frames) == event_after:
                if event_kind == "resource":
                    next_state["nearby"] = state([berry()])["nearby"]
                elif event_kind == "hound":
                    next_state["nearby"] = [{"guid": 91, "prefab": "hound", "activeThreat": True}]
                elif event_kind == "blocked":
                    next_state["navigation"]["search"]["status"] = "temporarily_blocked"
                elif event_kind == "phase":
                    next_state["world"]["phase"] = "dusk"
            if mid_move:
                callback = controller._action_observer.get()
                keep_moving = callback(next_state, "movement")
                self.assertEqual(keep_moving, len(frames) != expected_after)
            current = next_state
            return True

        with (patch.object(controller, "latest_state", side_effect=lambda _: (current, 10)),
              patch.object(controller, "explore_leg", side_effect=move) as leg,
              patch.object(controller, "send_key") as keys,
              patch.object(jev_agent, "call_jev") as api):
            with controller.observe_action(outer):
                self.assertTrue(controller.follow_exploration(Path("unused"), action, **limits))
            keys.assert_not_called()
            api.assert_not_called()
        self.assertEqual(leg.call_count, expected_after)
        outer.assert_called_once()
        self.assertEqual(outer.call_args.args[1], "exploration_event")
        return current

    def test_many_segments_continue_without_any_per_segment_decisions(self):
        # Exceeds all previous default budgets: 20 legs, 60 units, 100 seconds.
        with patch.object(controller.time, "monotonic", side_effect=range(0, 10000, 100)):
            self.run_motion(event_after=20)

    def test_new_berry_stops_mid_segment_even_without_prefetch(self):
        self.run_motion(event_after=3, mid_move=True)

    def test_early_new_type_waits_for_four_units_even_mid_segment(self):
        self.run_motion(event_after=1, step=2, expected_after=2, mid_move=True)

    def test_same_type_does_not_yield_even_after_many_new_guids_and_four_units(self):
        self.run_motion(event_after=1, event_kind="same_type", initial_nearby=[berry()],
                        max_legs=6, expected_after=6)

    def test_hound_targeting_stops_mid_segment_without_attacking_or_fleeing_automatically(self):
        self.run_motion(event_after=1, step=1, event_kind="hound", mid_move=True)

    def test_blocked_path_and_phase_change_return_to_jev(self):
        for kind in ("blocked", "phase"):
            with self.subTest(kind=kind):
                self.run_motion(event_after=1, step=1, event_kind=kind)

    def test_exception_restores_observer_and_does_not_start_other_actions(self):
        value = observed()
        action = jev_agent.build_candidates(value)[1]["explore_other_region"]
        parent = Mock()
        with (patch.object(controller, "latest_state", return_value=(value, 10)),
              patch.object(controller, "explore_leg", side_effect=KeyboardInterrupt),
              controller.observe_action(parent)):
            with self.assertRaises(KeyboardInterrupt):
                controller.follow_exploration(Path("unused"), action)
            self.assertIs(controller._action_observer.get(), parent)
        parent.assert_not_called()

    def test_resource_arriving_while_jev_was_answering_is_deferred_until_four_units(self):
        baseline = observed()
        current = copy.deepcopy(baseline)
        current["nearby"] = state([berry()])["nearby"]
        action = jev_agent.build_candidates(current)[1]["explore_other_region"]
        def move(*_args, **_kwargs):
            nonlocal current
            current = copy.deepcopy(current)
            current["player"]["position"]["z"] += 2
            return True
        with (patch.object(controller, "latest_state", side_effect=lambda *_: (current, 10)),
              patch.object(controller, "explore_leg", side_effect=move) as leg):
            self.assertTrue(controller.follow_exploration(Path("unused"), action, baseline=baseline, max_legs=3))
        self.assertEqual(leg.call_count,2)

    def test_four_units_means_displacement_from_actual_execution_start_not_path_length(self):
        baseline = observed()
        current = copy.deepcopy(baseline)
        current["nearby"] = state([berry()])["nearby"]
        # JEV observed z=0, but execution starts at z=10. Neither previous
        # movement nor a 3-unit outward-and-back loop may satisfy the floor.
        current["player"]["position"]["z"] = 10
        action = jev_agent.build_candidates(current)[1]["explore_other_region"]
        positions = iter((13, 10, 13, 14))
        def move(*_args, **_kwargs):
            nonlocal current
            current = copy.deepcopy(current)
            current["player"]["position"]["z"] = next(positions)
            return True
        with patch.object(controller, "latest_state", side_effect=lambda *_: (current, 10)), \
                patch.object(controller, "explore_leg", side_effect=move) as leg:
            with controller.timing.CycleTrace(1, emit_console=False) as trace:
                self.assertTrue(controller.follow_exploration(Path("unused"), action,
                    baseline=baseline, max_legs=5))
        self.assertEqual(leg.call_count, 4)
        self.assertEqual(trace.record["exploration_event"]["reason"], "resource_types_changed")
        self.assertEqual(trace.record["exploration_displacement"], 4)
        self.assertEqual(trace.record["exploration_distance"], 10)

    def test_real_key_hold_releases_early_when_observer_sees_targeting_hound(self):
        current = observed()
        action = jev_agent.build_candidates(current)[1]["explore_other_region"]
        hound = copy.deepcopy(current)
        hound["nearby"] = [{"guid": 77, "prefab": "hound", "activeThreat": True}]
        held = set()
        def key(vk, down):
            held.add(vk) if down else held.discard(vk)
        def move(*_args, **_kwargs):
            controller.tap([controller.VK["up"]], 0.5)
            self.assertEqual(held, set())
            return True
        with (patch.object(controller, "latest_state", return_value=(current, 10)),
              patch.object(controller, "wait_for_fresh_state", return_value=(hound, 11)),
              patch.object(controller, "explore_leg", side_effect=move) as leg,
              patch.object(controller, "send_key", side_effect=key),
              patch.object(controller.time, "sleep") as sleep):
            controller.follow_exploration(Path("unused"), action)
        self.assertEqual(leg.call_count, 1)
        sleep.assert_called_once()  # no remainder of the original half-second hold

    def test_observation_timeout_aborts_motion_and_restores_parent_observer(self):
        current = observed()
        action = jev_agent.build_candidates(current)[1]["explore_other_region"]
        parent = Mock()
        def move(*_args, **_kwargs):
            self.assertFalse(controller._action_observer.get()(None, "movement"))
            return True
        with (patch.object(controller, "latest_state", return_value=(current, 10)),
              patch.object(controller, "wait_for_fresh_state", side_effect=RuntimeError("timeout")),
              patch.object(controller, "explore_leg", side_effect=move) as leg,
              controller.observe_action(parent)):
            controller.follow_exploration(Path("unused"), action)
            self.assertIs(controller._action_observer.get(), parent)
        self.assertEqual(leg.call_count, 1)


if __name__ == "__main__":
    unittest.main()
