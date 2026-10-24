"""Navigation authority, persistence and model boundaries without game/API input."""
import copy
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from mcp_agent import jev_client as jev_agent
from common import timing_log as timing
from mcp_agent.navigation_intents import ExplorationExecutor, sources_for
from common.task_board import validate_board
from fixtures import observed, route, native_navigation_dispatch
from fixtures import grass


def board(target):
    return validate_board({"goal": "Get materials", "tasks": [
        {"id": "goal", "title": "Current goal", "priority": 1, "target": target},
    ]}, 1)


class ExplorationExecutorTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch("builtins.print"))
        self.navigator = ExplorationExecutor()

    def choose(self, actual, plan=None):
        return self.navigator.choose(actual, native_navigation_dispatch(actual), plan)

    def test_known_gold_sources_use_bfs_hops_not_ordinary_rocks(self):
        actual = observed()
        actual["navigation"]["graph"]["routes"] = [
            {**route("B"), "resources_seen": ["rock1"], "hops": 1, "distance": 1},
            {**route("C"), "resources_seen": ["rock2"], "hops": 2, "distance": 2},
            {**route("D"), "resources_seen": ["goldnugget"], "hops": 1, "distance": 10},
        ]
        _, action = self.choose(actual, board({"type": "inventory", "prefab": "goldnugget", "count": 1}))
        self.assertEqual(action["target_node_id"], "D")
        self.assertEqual(self.navigator.strategy, "known_source_bfs")
        self.assertNotIn("rock1", sources_for("goldnugget"))
        self.assertIn("rock2", sources_for("goldnugget"))

    def test_known_source_room_is_searched_locally_not_abandoned_at_entrance(self):
        actual = observed()
        actual["player"]["inventory"]["items"].append({"prefab": "pickaxe", "durability_percent": 1})
        actual["navigation"]["graph"]["current"]["resources_seen"] = ["rock2"]
        self.choose(actual, board({"type": "inventory", "prefab": "goldnugget", "count": 1}))
        self.assertEqual(self.navigator.strategy, "source_room")

    def test_explicit_visit_is_not_completed_by_an_old_entered_flag(self):
        actual = observed()  # B.entered is already true, but currently in A.
        plan = board({"type": "visit_region", "node_id": "B"})
        _, action = self.choose(actual, plan)
        self.assertEqual(action["target_node_id"], "B")
        self.assertEqual(self.navigator.strategy, "board_visit")
        arrived = copy.deepcopy(actual)
        arrived["navigation"]["graph"].update(current_node="B", routes=[])
        arrived["navigation"]["graph"]["current"]["id"] = "B"
        self.navigator.on_event({"reason": "destination_reached"}, arrived)
        self.choose(arrived, plan)
        self.assertNotEqual(self.navigator.strategy, "board_visit")

    def test_dfs_enters_unvisited_neighbor_and_backtracks_without_bouncing(self):
        actual = observed()
        actual["navigation"]["graph"]["routes"] = [{**route("B"), "hops": 1, "entered": False}]
        self.assertEqual(self.choose(actual)[1]["target_node_id"], "B")
        self.assertEqual(self.navigator.strategy, "dfs_neighbor")
        end = copy.deepcopy(actual)
        end["navigation"].update(status="no_frontier", search={"status": "boundary_complete"})
        end["navigation"]["graph"].update(current_node="B", routes=[{**route("A"), "hops": 1}])
        end["navigation"]["graph"]["current"].update(id="B", boundary_complete=True)
        self.navigator.on_event({"reason": "destination_reached"}, end)
        self.assertEqual(self.choose(end)[1]["target_node_id"], "A")
        self.assertEqual(self.navigator.strategy, "dfs_backtrack")
        root = copy.deepcopy(end)
        root["navigation"]["graph"].update(current_node="A", routes=[{**route("B"), "hops": 1}])
        root["navigation"]["graph"]["current"]["id"] = "A"
        self.navigator.on_event({"reason": "destination_reached"}, root)
        self.assertIsNone(self.choose(root))

    def test_navigation_intent_survives_local_collection_and_reordered_routes(self):
        actual = observed()
        plan = board({"type": "visit_region", "node_id": "B"})
        self.choose(actual, plan)
        self.navigator.on_event({"reason": "resource_types_changed"}, actual)
        self.navigator.after_local_action("collect")
        changed = copy.deepcopy(actual)
        changed["navigation"]["graph"]["routes"].insert(0, route("C"))
        key, action = self.choose(changed, plan)
        self.assertEqual(key, "travel_region_2")
        self.assertEqual(action["target_node_id"], "B")

    def test_paused_route_is_not_retried_and_not_marked_exhausted(self):
        actual = observed()
        plan = board({"type": "visit_region", "node_id": "B"})
        self.choose(actual, plan)
        self.navigator.on_event({"reason": "movement_stalled"}, actual)
        selected = self.choose(actual, plan)
        self.assertNotEqual(selected[1].get("target_node_id"), "B")
        self.assertFalse(actual["navigation"]["graph"]["current"]["boundary_complete"])

    def test_entered_but_unfinished_room_remains_a_pending_branch(self):
        actual = observed()
        actual["navigation"].update(status="no_frontier", search={"status": "boundary_complete"})
        actual["navigation"]["graph"]["current"]["boundary_complete"] = True
        self.assertEqual(self.choose(actual)[1]["target_node_id"], "B")
        self.assertEqual(self.navigator.strategy, "dfs_unfinished_room")

    def test_no_movement_when_night_has_no_torch_or_threat_is_active(self):
        for kind in ("night", "threat"):
            actual = observed()
            if kind == "night":
                actual["world"]["phase"] = "night"
            else:
                actual["nearby"] = [{"prefab": "hound", "guid": 99, "distance": 2,
                                      "activeThreat": True, "tags": ["hostile"]}]
            self.assertIsNone(self.choose(actual))











if __name__ == "__main__":
    unittest.main()
