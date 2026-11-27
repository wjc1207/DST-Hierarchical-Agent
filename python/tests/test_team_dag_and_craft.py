"""Unit tests for Batch 1: Team DAG, give_buddy handoff, and automatic intermediate Crafting DAG."""
import unittest

from test_companion import TOKEN, state
from test_companion_search import board, observation
from executor.board_contract import validate_team_plan
from executor.companion_tasks import CompanionTasks, validate_board
from executor.world_graph import WorldGraph
from mcp_server.companion_bridge import CompanionBridge
from transport.http_bridge import BridgeError


def _team_context(obs1, obs2):
    return {
        "interaction": {"mode": "instruction", "revision": 1},
        "request_ids": ["buddy1", "buddy2"],
        "members": {
            "buddy1": {
                "name": "小威",
                "interaction": {"mode": "instruction", "enabled": True, "revision": 1},
                "observation": obs1,
                "current_task_board": None,
            },
            "buddy2": {
                "name": "二威",
                "interaction": {"mode": "instruction", "enabled": True, "revision": 1},
                "observation": obs2,
                "current_task_board": None,
            },
        },
    }


class TeamDagAndCraftTests(unittest.TestCase):
    def test_team_dag_validation_rejects_cycles_and_unknown_and_unlocks_at_runtime(self):
        obs1, obs2 = observation(), observation()
        obs1["capabilities"].append("give_buddy")
        obs2["capabilities"].extend(["craft", "give_buddy"])
        obs2["recipes"] = {
            "rope": {"recipe_id": "rope", "product": "rope", "placement": False, "supported": True, "ready": True, "reason": "ready", "ingredients": []},
        }
        ctx = _team_context(obs1, obs2)

        # 1) Valid cross-actor DAG: buddy1 gives cutgrass -> buddy2 crafts rope after buddy1:hand_grass
        valid_raw = {
            "assignments": {
                "buddy1": {
                    "reply": "我把草递给二威。",
                    "tasks": [{"id": "hand_grass", "type": "give_buddy", "target_buddy_id": "buddy2", "prefab": "cutgrass", "count": 3}],
                },
                "buddy2": {
                    "reply": "拿到草后我来搓绳子。",
                    "tasks": [{"id": "make_rope", "type": "inventory", "prefab": "rope", "count": 1, "recipe_id": "rope", "depends_on": ["buddy1:hand_grass"]}],
                },
            }
        }
        plan = validate_team_plan(valid_raw, ctx)
        self.assertEqual(len(plan["assignments"]), 2)

        # 2) Unknown cross-actor dependency rejected
        bad_unknown = {
            "assignments": {
                "buddy1": {"reply": "我去采草。", "tasks": [{"id": "t1", "type": "collect", "prefab": "cutgrass", "count": 3}]},
                "buddy2": {"reply": "我等你。", "tasks": [{"id": "t2", "type": "collect", "prefab": "twigs", "count": 2, "depends_on": ["buddy1:missing"]}]},
            }
        }
        with self.assertRaises(ValueError):
            validate_team_plan(bad_unknown, ctx)

        # 3) Cross-actor cycle rejected
        bad_cycle = {
            "assignments": {
                "buddy1": {"reply": "A等B。", "tasks": [{"id": "a", "type": "collect", "prefab": "cutgrass", "count": 3, "depends_on": ["buddy2:b"]}]},
                "buddy2": {"reply": "B等A。", "tasks": [{"id": "b", "type": "collect", "prefab": "twigs", "count": 2, "depends_on": ["buddy1:a"]}]},
            }
        }
        with self.assertRaises(ValueError):
            validate_team_plan(bad_cycle, ctx)

        # 4) Runtime unlocking via team_completed_provider
        team_done = set()
        rt2 = CompanionTasks(team_completed_provider=lambda: team_done)
        b2 = validate_board({
            "goal": "Craft rope after handoff",
            "reply": "等草来再搓绳。",
            "tasks": [{"id": "make_rope", "title": "Make rope", "depends_on": ["buddy1:hand_grass"],
                       "target": {"type": "inventory", "prefab": "rope", "count": 1, "recipe_id": "rope"}}],
        }, 1, observed=obs2)
        rt2.adopt(b2, obs2)
        self.assertEqual(rt2.view(obs2)["tasks"][0]["status"], "blocked")
        self.assertIn("buddy1:hand_grass", rt2.view(obs2)["tasks"][0]["blocked_reason"])
        team_done.add("buddy1:hand_grass")
        self.assertEqual(rt2.view(obs2)["tasks"][0]["status"], "ready")
        self.assertEqual(rt2.next_action(obs2)[1:], ("craft", {"recipe_id": "rope"}))

    def test_give_buddy_bridge_validation_and_long_distance_approach(self):
        obs = observation()
        obs["capabilities"].append("give_buddy")
        obs["buddy"]["inventory"] = {"goldnugget": 2}
        # Bridge validation
        bridge = CompanionBridge(TOKEN, clock=lambda: 0)
        data = state()
        data["companion"] = obs
        bridge.exchange({"token": TOKEN, "session_id": "local", "sequence": 1, "state": data})
        good = {"prefab": "goldnugget", "total_count": 2, "task_key": "gb1", "target_buddy_id": "buddy2"}
        self.assertEqual(bridge.action("give_buddy", bridge.observe()["observation_id"], good)["status"], "queued")
        with self.assertRaises(BridgeError):
            bridge.action("give_buddy", bridge.observe()["observation_id"], {**good, "target_buddy_id": "buddy9"})

        # Long-distance approach via WorldGraph when buddy2 is 32u away
        graph = WorldGraph()
        obs["perception"]["cells"] = [{"gx": gx, "gz": 0, "land": True, "node_id": "W:0:BGGrass"} for gx in range(0, 12)]
        obs["perception"]["points"] = [{"id": "4:0", "x": 16, "z": 0}, {"id": "-2:0", "x": -8, "z": 0}]
        positions = {"buddy2": (32.0, 0.0)}
        rt = CompanionTasks(graph=graph, team_positions_provider=lambda: positions)
        rt.adopt(validate_board({
            "goal": "Give gold to buddy2",
            "reply": "我把金子送给二威。",
            "tasks": [{"id": "g1", "title": "Give gold", "target": {"type": "give_buddy", "target_buddy_id": "buddy2", "prefab": "goldnugget", "count": 2}}],
        }, 1, observed=obs), obs)
        _, op, args = rt.next_action(obs)
        self.assertEqual((op, args["point_id"]), ("search", "4:0"))
        # Once buddy2 is within 18u, dispatch give_buddy directly
        positions["buddy2"] = (6.0, 0.0)
        _, op2, args2 = rt.next_action(obs)
        self.assertEqual((op2, args2["target_buddy_id"], args2["prefab"], args2["total_count"]),
                         ("give_buddy", "buddy2", "goldnugget", 2))

    def test_auto_intermediate_crafting_dag_resolves_one_and_two_levels(self):
        obs = observation()
        obs["capabilities"].extend(["craft", "place"])
        obs["recipes"] = {
            "boards": {
                "recipe_id": "boards", "product": "boards", "placement": False,
                "supported": True, "ready": True, "reason": "ready",
                "ingredients": [{"prefab": "log", "count": 4, "carried": 4, "missing": 0}],
            },
            "rope": {
                "recipe_id": "rope", "product": "rope", "placement": False,
                "supported": True, "ready": True, "reason": "ready",
                "ingredients": [{"prefab": "cutgrass", "count": 3, "carried": 3, "missing": 0}],
            },
            "spear": {
                "recipe_id": "spear", "product": "spear", "placement": False,
                "supported": True, "ready": False, "reason": "ingredients_missing",
                "ingredients": [
                    {"prefab": "twigs", "count": 2, "carried": 2, "missing": 0},
                    {"prefab": "rope", "count": 1, "carried": 0, "missing": 1},
                    {"prefab": "flint", "count": 1, "carried": 1, "missing": 0},
                ],
            },
            "researchlab2": {
                "recipe_id": "researchlab2", "product": "researchlab2", "placement": True,
                "supported": True, "ready": False, "reason": "ingredients_missing",
                "ingredients": [
                    {"prefab": "boards", "count": 4, "carried": 0, "missing": 4},
                    {"prefab": "cutstone", "count": 2, "carried": 2, "missing": 0},
                    {"prefab": "transistor", "count": 2, "carried": 2, "missing": 0},
                ],
            },
        }
        # 1) Inventory recipe auto-crafts missing rope before spear
        self.assertEqual(
            CompanionTasks.recipe_action({"type": "inventory", "prefab": "spear", "count": 1, "recipe_id": "spear"}, obs),
            ("craft", {"recipe_id": "rope"}),
        )
        # 2) Structure recipe auto-crafts missing boards before researchlab2
        self.assertEqual(
            CompanionTasks.recipe_action({"type": "structure", "prefab": "researchlab2", "recipe_id": "researchlab2", "x": 4, "z": 0, "radius": 6}, obs),
            ("craft", {"recipe_id": "boards"}),
        )
        # 3) Missing technology does NOT trigger intermediate crafting
        obs["recipes"]["spear"]["reason"] = "technology_or_unlock_required"
        self.assertEqual(
            CompanionTasks.recipe_action({"type": "inventory", "prefab": "spear", "count": 1, "recipe_id": "spear"}, obs)[0],
            "needs_replan",
        )
