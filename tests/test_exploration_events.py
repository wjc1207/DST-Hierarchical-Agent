"""Exploration continues without model calls until something meaningful changes."""
import copy
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import controller
import jev_agent
from exploration_events import ExplorationEvents
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
        detector = ExplorationEvents(initial, available, set())
        changed = copy.deepcopy(initial)
        changed["navigation"]["road"] = {"status": "ready"}
        changed["navigation"]["on_road"] = True
        self.assertIsNone(detector.check(changed, night_unsafe=False, food_available=False))

    def test_known_berries_do_not_interrupt_but_new_berries_do(self):
        initial = state([berry(1)])
        seen = set()
        detector = ExplorationEvents(initial, available, seen)
        self.assertIsNone(detector.check(initial, night_unsafe=False, food_available=False))
        changed = state([berry(1), berry(2)])
        event = detector.check(changed, night_unsafe=False, food_available=False)
        self.assertEqual((event["reason"], event["guid"]), ("new_resource", 2))
        self.assertIsNone(detector.check(changed, night_unsafe=False, food_available=False))

    def test_depleted_and_unreachable_resources_do_not_interrupt(self):
        detector = ExplorationEvents(state([]), available, set())
        depleted, unreachable = berry(1), berry(2)
        depleted["pickable"] = False
        unreachable["collect_path"] = {"status": "unknown"}
        self.assertIsNone(detector.check(state([depleted, unreachable]),
                                         night_unsafe=False, food_available=False))
        self.assertEqual(detector.check(state([berry(1)]), night_unsafe=False,
                                        food_available=False)["reason"], "new_resource")

    def test_known_hound_starting_to_target_player_is_an_event(self):
        hound = {"guid": 80, "prefab": "hound", "potentialThreat": True,
                 "activeThreat": False, "distance": 10}
        initial = state([hound])
        detector = ExplorationEvents(initial, available, set())
        self.assertIsNone(detector.check(initial, night_unsafe=False, food_available=False))
        changed = copy.deepcopy(initial)
        changed["nearby"][0]["activeThreat"] = True
        event = detector.check(changed, night_unsafe=False, food_available=False)
        self.assertEqual(event["reason"], "targeted_by_hostile")
        self.assertEqual(event["guid"], 80)

    def test_known_resource_stays_known_after_leaving_and_reentering_view(self):
        seen = set()
        ExplorationEvents(state([berry()]), available, seen)
        detector = ExplorationEvents(state([]), available, seen)
        self.assertIsNone(detector.check(state([berry()]), night_unsafe=False, food_available=False))

    def test_known_plants_trigger_once_per_observed_regrowth(self):
        for prefab in ("grass", "sapling", "berrybush"):
            with self.subTest(prefab=prefab):
                plant=berry()
                plant["prefab"]=prefab
                detector=ExplorationEvents(state([plant]),available,set())
                for _ in range(2):
                    empty=copy.deepcopy(plant)
                    empty["pickable"]=False
                    self.assertIsNone(detector.check(state([empty]),night_unsafe=False,food_available=False))
                    self.assertIsNone(detector.check(state([]),night_unsafe=False,food_available=False))
                    event=detector.check(state([plant]),night_unsafe=False,food_available=False)
                    self.assertEqual(event["reason"],"resource_available_again")
                    self.assertEqual(event["guid"],plant["guid"])
                    self.assertIsNone(detector.check(state([plant]),night_unsafe=False,food_available=False))

    def test_unknown_pickability_and_route_changes_are_not_depletion(self):
        initial=state([berry()])
        detector=ExplorationEvents(initial,available,set())
        for change in ("path", "unknown_pickability"):
            changed=copy.deepcopy(initial)
            if change == "path":
                changed["nearby"][0]["collect_path"]={"status":"unknown"}
            else:
                changed["nearby"][0]["pickable"]=None
            self.assertIsNone(detector.check(changed,night_unsafe=False,food_available=False))
            self.assertIsNone(detector.check(initial,night_unsafe=False,food_available=False))

    def test_regrowth_seen_by_jev_is_acknowledged_but_stale_decision_cannot_consume_it(self):
        for baseline_time,expected in ((1,"resource_available_again"),(3,None)):
            with self.subTest(baseline_time=baseline_time):
                baseline=state([berry()]); baseline["observed_at"]=baseline_time
                seen={(22,"berrybush")}; depleted={(22,"berrybush"):2}
                detector=ExplorationEvents(baseline,available,seen,depleted=depleted)
                current=state([berry()]); current["observed_at"]=3
                event=detector.check(current,night_unsafe=False,food_available=False)
                self.assertEqual(event["reason"] if event else None,expected)

    def test_depletion_during_other_actions_persists_until_regrowth_is_reachable(self):
        controller.EXPLORATION_SEEN_RESOURCES.clear()
        self.addCleanup(controller.EXPLORATION_SEEN_RESOURCES.clear)
        memory={"observed_at":1,"seen":{(22,"berrybush")},"depleted":{}}
        controller.EXPLORATION_SEEN_RESOURCES[99]=memory
        empty=berry(); empty["pickable"]=False
        snapshot=state([empty]); snapshot["player"]["guid"]=99; snapshot["observed_at"]=2
        with patch.object(controller.telemetry,"latest_state",return_value=(snapshot,100)):
            controller.latest_state(Path("unused"))
        self.assertIn((22,"berrybush"),memory["depleted"])
        baseline=state([]); baseline["player"]["guid"]=99; baseline["observed_at"]=3
        detector=ExplorationEvents(baseline,available,memory["seen"],depleted=memory["depleted"])
        blocked=state([berry()]); blocked["player"]["guid"]=99; blocked["observed_at"]=4
        blocked["nearby"][0]["collect_path"]={"status":"unknown"}
        self.assertIsNone(detector.check(blocked,night_unsafe=False,food_available=False))
        ready=state([berry()]); ready["player"]["guid"]=99; ready["observed_at"]=5
        self.assertEqual(detector.check(ready,night_unsafe=False,food_available=False)["reason"],
                         "resource_available_again")
        self.assertIsNone(detector.check(ready,night_unsafe=False,food_available=False))

    def test_mixed_ground_change_is_not_a_region_change(self):
        initial = observed()
        detector = ExplorationEvents(initial, available, set())
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
                detector = ExplorationEvents(initial, available, set(), "B")
                changed = copy.deepcopy(initial)
                change(changed)
                self.assertEqual(detector.check(changed, night_unsafe=False,
                                                 food_available=False)["reason"], expected)


class ContinuousExecutionTests(unittest.TestCase):
    def setUp(self):
        controller.EXPLORATION_SEEN_RESOURCES.clear()
        self.output = patch("builtins.print")
        self.output.start()
        self.addCleanup(self.output.stop)

    def run_motion(self, event_after=4, event_kind="resource", mid_move=False, **limits):
        current = observed()
        current["player"]["guid"] = 500
        current["observed_at"] = 1
        current["navigation"]["graph"]["known_nodes"] = 2
        action = jev_agent.build_candidates(current)[1]["explore_other_region"]
        outer = Mock()
        frames = []

        def move(*_args, **_kwargs):
            nonlocal current
            next_state = copy.deepcopy(current)
            next_state["observed_at"] += 1
            next_state["player"]["position"]["z"] += 3
            next_state["navigation"]["search"]["leg"]["z"] += 3
            frames.append(next_state)
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
                self.assertEqual(keep_moving, len(frames) != event_after)
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
        self.assertEqual(leg.call_count, event_after)
        outer.assert_called_once()
        self.assertEqual(outer.call_args.args[1], "exploration_event")
        return current

    def test_many_segments_continue_without_any_per_segment_decisions(self):
        # Exceeds all previous default budgets: 20 legs, 60 units, 100 seconds.
        with patch.object(controller.time, "monotonic", side_effect=range(0, 10000, 100)):
            self.run_motion(event_after=20)

    def test_new_berry_stops_mid_segment_even_without_prefetch(self):
        self.run_motion(event_after=3, mid_move=True)

    def test_hound_targeting_stops_mid_segment_without_attacking_or_fleeing_automatically(self):
        self.run_motion(event_after=2, event_kind="hound", mid_move=True)

    def test_blocked_path_and_phase_change_return_to_jev(self):
        for kind in ("blocked", "phase"):
            with self.subTest(kind=kind):
                controller.EXPLORATION_SEEN_RESOURCES.clear()
                self.run_motion(event_after=2, event_kind=kind)

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

    def test_resource_arriving_while_jev_was_answering_is_not_silently_ignored(self):
        baseline = observed()
        current = copy.deepcopy(baseline)
        current["nearby"] = state([berry()])["nearby"]
        action = jev_agent.build_candidates(current)[1]["explore_other_region"]
        with (patch.object(controller, "latest_state", return_value=(current, 10)),
              patch.object(controller, "explore_leg") as leg):
            self.assertFalse(controller.follow_exploration(Path("unused"), action, baseline=baseline))
        leg.assert_not_called()

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
