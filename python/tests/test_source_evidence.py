"""Source prerequisites and stale regional evidence must not create return loops."""
import copy
import unittest
from unittest.mock import patch

from mcp_agent import jev_client as jev_agent
from mcp_agent.navigation_intents import ExplorationExecutor, usable_sources_for
from fixtures import observed, route, native_navigation_dispatch
from test_exploration_executor import board
from fixtures import grass


class SourceEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch("builtins.print"))
        self.clock = self.enterContext(patch("mcp_agent.navigation_intents.time.monotonic", return_value=100))
        self.navigator = ExplorationExecutor()
        self.plan = board({"type": "inventory", "prefab": "flint", "count": 2})

    def choose(self, actual, plan=None):
        return self.navigator.choose(actual, native_navigation_dispatch(actual), plan or self.plan)

    def at(self, node="A", seen=(), complete=False):
        actual = observed()
        actual["player"]["guid"] = 6001
        actual["player"]["inventory"]["items"] = [{"prefab": "flint", "count": 1}]
        graph = actual["navigation"]["graph"]
        graph.update(current_node=node, routes=[])
        graph["current"].update(id=node, resources_seen=list(seen), boundary_complete=complete)
        if complete:
            actual["navigation"].update(status="no_frontier", search={"status": "boundary_complete"})
        return actual

    def failed_flint_room(self):
        actual = self.at("B", ["flint", "rock1"], True)
        self.assertIsNone(self.choose(actual))  # Record absence even with no movement options.
        self.assertIn(("B", "flint"), self.navigator.source_failures)
        return actual

    def remote_flint_room(self):
        actual = self.at("A", complete=True)
        actual["navigation"]["graph"]["routes"] = [
            {**route("B"), "hops": 1, "resources_seen": ["flint", "rock1"], "boundary_complete": True},
            {**route("C"), "hops": 1, "entered": False, "resources_seen": [], "boundary_complete": False},
        ]
        return actual

    def test_no_tool_or_craftability_is_not_a_harvestable_source(self):
        actual = self.at()
        actual["crafting"].update(pickaxe=True, axe=True)
        self.assertEqual(usable_sources_for("flint", actual), {"flint"})
        self.assertEqual(usable_sources_for("goldnugget", actual), {"goldnugget"})
        self.assertEqual(usable_sources_for("log", actual), {"log"})

    def test_tool_condition_requires_enough_whole_target_uses_in_bag_or_hand(self):
        for section in ("items", "equipped"):
            for percent, usable in ((None, False), (0.15, False), (0.18, True), (1, True)):
                actual = self.at()
                tool = {"prefab": "pickaxe", "count": 1}
                if percent is not None:
                    tool["durability_percent"] = percent
                actual["player"]["inventory"][section].append(tool)
                sources = usable_sources_for("flint", actual)
                self.assertEqual("rock1" in sources, usable, (section, percent))
                self.assertEqual("rock2" in sources, usable, (section, percent))
        for percent, usable in ((0.14, False), (0.15, True)):
            actual = self.at()
            actual["player"]["inventory"]["items"].append({"prefab": "axe", "durability_percent": percent})
            self.assertEqual("evergreen" in usable_sources_for("log", actual), usable)

    def test_follows_loose_flint_not_closer_rocks_and_invalidates_an_old_tool_dependent_intent(self):
        actual = self.at()
        actual["navigation"]["graph"]["routes"] = [
            {**route("B"), "hops": 1, "distance": 1, "resources_seen": ["rock1"]},
            {**route("D"), "hops": 1, "distance": 20, "resources_seen": ["flint"]},
        ]
        self.assertEqual(self.choose(actual)[1]["target_node_id"], "D")
        actual["player"]["inventory"]["items"].append({"prefab": "pickaxe", "durability_percent": 1})
        self.navigator.intent = None
        self.assertEqual(self.choose(actual)[1]["target_node_id"], "B")
        actual["player"]["inventory"]["items"].pop()
        self.assertEqual(self.choose(actual)[1]["target_node_id"], "D")

    def test_failed_history_is_not_reenabled_by_time_board_changes_or_unrelated_discoveries(self):
        self.failed_flint_room()
        remote = self.remote_flint_room()
        remote["navigation"]["graph"]["routes"][0]["resources_seen"].append("flower")
        new_plan = board({"type": "inventory", "prefab": "flint", "count": 3})
        new_plan["board_id"] = "new-quarter"
        self.clock.return_value = 100000
        key, action = self.choose(remote, new_plan)
        self.assertEqual(action["target_node_id"], "C")
        self.assertNotEqual(self.navigator.strategy, "known_source_bfs")
        self.assertIn(("B", "flint"), self.navigator.source_failures)

    def test_fresh_reachable_flint_restores_failed_source_but_unreachable_evidence_does_not(self):
        failed = self.failed_flint_room()
        loose = {**grass(701, 1, True), "prefab": "flint", "tags": ["_inventoryitem"]}
        blocked = copy.deepcopy(loose)
        blocked["collect_path"]["status"] = "blocked"
        failed["nearby"] = [blocked]
        self.choose(failed)
        self.assertIn(("B", "flint"), self.navigator.source_failures)
        failed["nearby"] = [loose]
        self.choose(failed)
        self.assertNotIn(("B", "flint"), self.navigator.source_failures)

    def test_newly_usable_recorded_source_restores_priority_after_acquiring_a_tool(self):
        self.failed_flint_room()
        remote = self.remote_flint_room()
        remote["player"]["inventory"]["items"].append({"prefab": "pickaxe", "durability_percent": 1})
        self.assertEqual(self.choose(remote)[1]["target_node_id"], "B")
        self.assertEqual(self.navigator.strategy, "known_source_bfs")
        self.assertNotIn(("B", "flint"), self.navigator.source_failures)

    def test_no_empty_room_conclusion_at_entrance_or_from_unsafe_or_temporary_blockage(self):
        for complete, status in ((False, "ready"), (True, "ready"),
                                 (False, "no_frontier"), (True, "hazard"),
                                 (True, "temporarily_blocked")):
            actual = self.at("B", ["flint"], complete)
            actual["navigation"]["status"] = status
            self.choose(actual)
            self.assertNotIn(("B", "flint"), self.navigator.source_failures, (complete, status))
        self.navigator = ExplorationExecutor()
        current = self.at("B", ["flint"])
        self.choose(current)
        self.assertEqual(self.navigator.strategy, "source_room")
        current["navigation"].update(status="no_frontier", search={"status": "boundary_complete"})
        current["navigation"]["graph"]["current"]["boundary_complete"] = True
        self.choose(current)
        self.assertIn(("B", "flint"), self.navigator.source_failures)

    def test_failure_is_product_specific_and_world_reset_discards_old_evidence(self):
        self.failed_flint_room()
        remote = self.remote_flint_room()
        remote["navigation"]["graph"]["routes"][0]["resources_seen"].append("berrybush")
        berries = board({"type": "inventory", "prefab": "berries", "count": 2})
        self.assertEqual(self.choose(remote, berries)[1]["target_node_id"], "B")
        self.assertEqual(self.navigator.strategy, "known_source_bfs")
        remote["player"]["guid"] = 6002
        self.assertEqual(self.choose(remote)[1]["target_node_id"], "B")
        self.assertEqual(self.navigator.source_failures, {})


if __name__ == "__main__":
    unittest.main()
