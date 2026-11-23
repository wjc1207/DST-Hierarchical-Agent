"""Tests for unified Player and Companion task targets, schemas, and basic survival actions."""
from __future__ import annotations

import copy
import unittest

from common.camp_work import target_error as camp_target_error
from common.companion_transfer import query_storage, validate_target as validate_transfer
from common.task_board import board_view, validate_board
from executor.companion_tasks import CompanionTasks, validate_board as validate_companion_board
from executor.priority_board import PriorityBoardExecutor, validate_goals
from mcp_server.companion_bridge import CompanionBridge
from transport.http_bridge import BridgeError

TOKEN = "u" * 40


def companion_obs(**overrides):
    base = {
        "version": 1,
        "bound": True,
        "token": "b1",
        "revision": 1,
        "mode": "stay",
        "capabilities": [
            "follow", "stay", "move", "collect", "recipes", "equip", "unequip",
            "work", "craft", "place", "search", "storage", "give_owner", "drop_item",
            "receive_item", "visit_building", "timed_return", "eat", "refuel", "home", "hunger",
            "cook_meal", "ignite_tree",
        ],
        "buddy": {
            "guid": 100,
            "prefab": "wilson",
            "position": {"x": 0.0, "z": 0.0},
            "inventory": {"log": 3, "berries_cooked": 2},
            "owner_distance": 2.0,
            "health": 150,
            "health_max": 150,
            "hunger": 60,
            "hunger_max": 150,
            "temperature": 25.0,
            "dead": False,
        },
        "world": {"day": 1, "phase": "day", "time": 0.25},
        "nearby": [],
        "work_targets": [],
        "structures": [],
        "equipment": [],
        "recipes": {},
        "containers": [],
        "foods": [
            {"guid": 501, "prefab": "berries_cooked", "count": 2, "hunger_delta": 12.5, "sanity_delta": 0, "health_delta": 1},
            {"guid": 502, "prefab": "monstermeat", "count": 1, "hunger_delta": 18.75, "sanity_delta": -15, "health_delta": -20},
        ],
        "places": {
            "available": True,
            "scope": "companion_observed_buildings",
            "identity": "server_guid",
            "buildings": [
                {"id": "building:40", "guid": 40, "prefab": "firepit", "x": 10.0, "z": 0.0, "status": "observed", "arrived": False},
                {"id": "building:50", "guid": 50, "prefab": "treasurechest", "x": 12.0, "z": 0.0, "status": "unconfirmed", "arrived": False},
            ],
            "anchors": [
                {"id": "building:40", "prefab": "firepit", "x": 10.0, "z": 0.0, "ready": False},
            ],
            "home": None,
        },
        "refueling": {
            "available": True,
            "ratio_available": True,
            "targets": [
                {"building_id": "building:40", "guid": 40, "prefab": "firepit", "lit": True, "fuel_percent": 0.25, "fire_section": 1},
            ],
        },
        "transfer_receipts": [],
    }
    base.update(overrides)
    return base


class UnifiedStorageAndGeometryTests(unittest.TestCase):
    def test_companion_storage_accepts_unified_building_id_and_legacy_guid(self):
        by_building = validate_transfer({"type": "deposit", "building_id": "building:50", "prefab": "log", "count": 2})
        self.assertEqual(by_building["building_id"], "building:50")
        self.assertEqual(by_building["container_guid"], 50)

        by_guid = validate_transfer({"type": "deposit", "container_guid": 50, "prefab": "log", "count": 2})
        self.assertEqual(by_guid["container_guid"], 50)

        with self.assertRaises(ValueError):
            validate_transfer({"type": "deposit", "building_id": "building:50", "container_guid": 51, "prefab": "log", "count": 1})

        obs = companion_obs(containers=[{"guid": 50, "contents_known": True, "items": []}])
        self.assertEqual(len(query_storage(obs, building_id="building:50")["containers"]), 1)

    def test_companion_storage_with_building_id_navigates_when_out_of_range(self):
        tasks = CompanionTasks(clock=lambda: 10.0)
        obs = companion_obs()
        plan = {
            "day": 1,
            "goal": "Deposit logs into remembered chest",
            "tasks": [{
                "id": "dep",
                "title": "Store logs",
                "target": {"type": "deposit", "building_id": "building:50", "prefab": "log", "count": 2},
            }],
        }
        tasks.adopt(plan, obs)
        task, op, args = tasks.next_action(obs)
        self.assertEqual(task["id"], "dep")
        self.assertEqual(op, "visit_building")
        self.assertEqual(args, {"building_id": "building:50"})

    def test_companion_move_to_accepts_custom_arrival_radius_and_structure_placement(self):
        tasks = CompanionTasks(clock=lambda: 10.0)
        obs = companion_obs(
            recipes={"researchlab": {"recipe_id": "researchlab", "product": "researchlab", "placement": True, "supported": True, "ready": True}},
        )
        plan = {
            "day": 1,
            "goal": "Move and place by anchor",
            "tasks": [
                {"id": "go", "title": "Move near point", "target": {"type": "move_to", "x": 3.0, "z": 0.0, "arrival_radius": 4.0}},
                {"id": "lab", "title": "Build science machine by firepit", "target": {
                    "type": "structure", "prefab": "researchlab", "recipe_id": "researchlab",
                    "placement": {"anchor_id": "building:40", "radius": 6},
                }},
            ],
        }
        tasks.adopt(plan, obs)
        view = tasks.view(obs)
        self.assertEqual(view["tasks"][0]["status"], "done")  # 3.0 <= arrival_radius 4.0
        task, op, args = tasks.next_action(obs)
        self.assertEqual(task["id"], "lab")
        self.assertEqual(op, "place")
        self.assertEqual(args, {"recipe_id": "researchlab", "x": 10.0, "z": 0.0, "radius": 6})


class CompanionSurvivalUnificationTests(unittest.TestCase):
    def test_companion_refuel_count_approaches_and_tracks_receipts(self):
        tasks = CompanionTasks(clock=lambda: 10.0)
        obs = companion_obs()
        plan = {
            "day": 1,
            "goal": "Refuel firepit",
            "tasks": [{"id": "fuel", "title": "Add 2 logs", "target": {
                "type": "refuel", "building_id": "building:40", "fuel_prefab": "log", "count": 2,
            }}],
        }
        tasks.adopt(plan, obs)

        # Far from firepit -> approaches via visit_building
        task, op, args = tasks.next_action(obs)
        self.assertEqual((task["id"], op, args), ("fuel", "visit_building", {"building_id": "building:40"}))

        # Arrived -> dispatches refuel
        obs2 = copy.deepcopy(obs)
        obs2["version"] = 2
        obs2["places"]["buildings"][0]["arrived"] = True
        task, op, args = tasks.next_action(obs2)
        self.assertEqual(op, "refuel")
        self.assertEqual(args["building_id"], "building:40")
        self.assertEqual(args["fuel_prefab"], "log")
        self.assertEqual(args["total_count"], 2)

        # Receipt of 2 -> completed
        obs3 = copy.deepcopy(obs2)
        obs3["version"] = 3
        obs3["transfer_receipts"] = [{"task_key": args["task_key"], "count": 2}]
        tasks.on_result("fuel", {"status": "completed", "reason": "fuel_added", "data": {"task_key": args["task_key"], "count": 2}}, obs3)
        _, op3, _ = tasks.next_action(obs3)
        self.assertEqual(op3, "completed")

    def test_companion_refuel_ratio_maintenance_lifecycle(self):
        tasks = CompanionTasks(clock=lambda: 10.0)
        obs = companion_obs()
        obs["places"]["buildings"][0]["arrived"] = True
        plan = {
            "day": 1,
            "goal": "Maintain firepit fuel",
            "tasks": [{"id": "keep_fire", "title": "Keep fire above 60%", "duration": 0.25, "target": {
                "type": "refuel", "building_id": "building:40", "fuel_prefab": "log", "at_least": 0.6,
            }}],
        }
        tasks.adopt(plan, obs)
        task, op, args = tasks.next_action(obs)
        self.assertEqual(op, "refuel")
        self.assertEqual(args["total_count"], 1)

        # Fuel rises to 0.75 -> maintaining
        obs2 = copy.deepcopy(obs)
        obs2["version"] = 2
        obs2["world"]["time"] = 0.35
        obs2["refueling"]["targets"][0]["fuel_percent"] = 0.75
        view2 = tasks.view(obs2)
        self.assertEqual(view2["tasks"][0]["status"], "maintaining")

        # Duration expires at 0.50 with fuel >= 0.6 -> done
        obs3 = copy.deepcopy(obs2)
        obs3["version"] = 3
        obs3["world"]["time"] = 0.50
        view3 = tasks.view(obs3)
        self.assertEqual(view3["tasks"][0]["status"], "done")

    def test_companion_set_home_and_stay_home_residence(self):
        tasks = CompanionTasks(clock=lambda: 10.0)
        obs = companion_obs()
        plan = {
            "day": 1,
            "goal": "Set home at firepit",
            "tasks": [{"id": "home", "title": "Set camp home", "target": {"type": "set_home", "anchor_id": "building:40"}}],
        }
        tasks.adopt(plan, obs)
        _, op, args = tasks.next_action(obs)
        self.assertEqual((op, args), ("visit_building", {"building_id": "building:40"}))

        obs2 = copy.deepcopy(obs)
        obs2["version"] = 2
        obs2["places"]["buildings"][0]["arrived"] = True
        _, op2, args2 = tasks.next_action(obs2)
        self.assertEqual((op2, args2), ("set_home", {"anchor_id": "building:40"}))

        obs3 = copy.deepcopy(obs2)
        obs3["version"] = 3
        obs3["places"]["home"] = {"id": "home:1", "anchor_id": "building:40", "prefab": "firepit", "x": 10.0, "z": 0.0, "arrived": True}
        self.assertEqual(tasks.view(obs3)["status"], "completed")

        # Now test stay_home with duration
        tasks2 = CompanionTasks(clock=lambda: 20.0)
        obs3["buddy"]["position"] = {"x": 10.0, "z": 0.0}
        plan2 = {
            "day": 1,
            "goal": "Guard camp",
            "tasks": [{"id": "stay", "title": "Stay home", "duration": 0.25, "target": {"type": "stay_home", "home_id": "home:1", "radius": 10}}],
        }
        tasks2.adopt(plan2, obs3)
        _, op_hold, _ = tasks2.next_action(obs3)
        self.assertEqual(op_hold, "waiting")

        obs4 = copy.deepcopy(obs3)
        obs4["version"] = 4
        obs4["world"]["time"] = 0.50
        self.assertEqual(tasks2.view(obs4)["status"], "completed")

    def test_companion_hunger_ratio_picks_safe_food_and_completes(self):
        tasks = CompanionTasks(clock=lambda: 10.0)
        obs = companion_obs()
        plan = {
            "day": 1,
            "goal": "Restore hunger",
            "tasks": [{"id": "feed", "title": "Reach 70% hunger", "target": {"type": "hunger_ratio", "at_least": 0.7}}],
        }
        tasks.adopt(plan, obs)
        task, op, args = tasks.next_action(obs)
        self.assertEqual(op, "eat")
        # Prefers safe berries_cooked over monstermeat despite lower hunger_delta
        self.assertEqual(args["prefab"], "berries_cooked")
        self.assertEqual(args["total_count"], 1)

        obs2 = copy.deepcopy(obs)
        obs2["version"] = 2
        obs2["buddy"]["hunger"] = 120
        self.assertEqual(tasks.view(obs2)["status"], "completed")

    def test_companion_bridge_validates_refuel_and_set_home(self):
        now = [100.0]
        bridge = CompanionBridge(TOKEN, clock=lambda: now[0])
        snap = companion_obs()
        bridge.exchange({
            "token": TOKEN,
            "session_id": "s1",
            "sequence": 1,
            "state": {
                "ready": True,
                "snapshot_age_ms": 10,
                "control": {"enabled": True, "mode": "instruction", "revision": 1, "instruction": "refuel", "messages": []},
                "companion": snap,
            },
        })
        obs_id = bridge.observe()["observation_id"]
        rec = bridge.action("refuel", obs_id, {"building_id": "building:40", "fuel_prefab": "log", "total_count": 2, "task_key": "k1"})
        self.assertEqual(rec["op"], "refuel")
        bridge.actions[rec["id"]]["status"] = "completed"
        bridge.active = None

        obs_id = bridge.observe()["observation_id"]
        rec2 = bridge.action("set_home", obs_id, {"anchor_id": "building:40"})
        self.assertEqual(rec2["op"], "set_home")
        bridge.actions[rec2["id"]]["status"] = "completed"
        bridge.active = None

        obs_id = bridge.observe()["observation_id"]
        with self.assertRaises(BridgeError):
            bridge.action("refuel", obs_id, {"building_id": "building:40", "fuel_prefab": "livinglog", "total_count": 1, "task_key": "k2"})
        with self.assertRaises(BridgeError):
            bridge.action("set_home", obs_id, {"anchor_id": "building:999"})


class PlayerItemActionUnificationTests(unittest.TestCase):
    def test_player_board_supports_equip_unequip_eat_and_drop_item(self):
        raw = {
            "goal": "Manage gear and items",
            "tasks": [
                {"id": "eq", "title": "Equip axe", "target": {"type": "equip", "prefab": "axe"}},
                {"id": "uneq", "title": "Unequip torch", "target": {"type": "unequip", "prefab": "torch"}},
                {"id": "eat1", "title": "Eat 2 berries", "target": {"type": "eat", "prefab": "berries", "count": 2}},
                {"id": "drop1", "title": "Drop 1 log", "target": {"type": "drop_item", "prefab": "log", "count": 1}},
            ],
        }
        board = validate_goals(raw, 1)
        executor = PriorityBoardExecutor(clock=lambda: 10.0)
        executor.adopt(board)
        state = {
            "observed_at": 1,
            "world": {"day": 1, "phase": "day", "time": 0.2},
            "player": {
                "position": {"x": 0, "z": 0},
                "vitals": {"hunger": 100, "hunger_max": 150},
                "inventory": {
                    "items": [{"prefab": "axe", "count": 1}, {"prefab": "berries", "count": 3}, {"prefab": "log", "count": 2}],
                    "equipped": [{"prefab": "torch", "slot": "hands", "durability_percent": 0.8}],
                },
            },
        }
        actions = {
            "equip_item:axe": {"kind": "equip_item", "prefab": "axe", "planner_only": True},
            "unequip_item:torch": {"kind": "unequip_item", "prefab": "torch", "planner_only": True},
            "eat_item:berries": {"kind": "eat_item", "prefab": "berries", "planner_only": True},
            "drop_item:log": {"kind": "drop_item", "prefab": "log", "planner_only": True},
        }
        view = executor.view(board, state)
        by_id = {t["id"]: t for t in view["tasks"]}
        self.assertEqual([by_id[k]["status"] for k in ("eq", "uneq", "eat1", "drop1")], ["ready", "ready", "ready", "ready"])
        self.assertEqual(executor.local_choices(by_id["eq"], actions, state)[0][0], "equip_item:axe")
        self.assertEqual(executor.local_choices(by_id["uneq"], actions, state)[0][0], "unequip_item:torch")
        self.assertEqual(executor.local_choices(by_id["eat1"], actions, state)[0][0], "eat_item:berries")
        self.assertEqual(executor.local_choices(by_id["drop1"], actions, state)[0][0], "drop_item:log")

        # Complete eat (2x) and drop (1x) via on_result
        executor.on_result("eat1", "eat_item:berries", actions["eat_item:berries"], "completed", "ok", None, state)
        self.assertEqual({t["id"]: t for t in executor.view(board, state)["tasks"]}["eat1"]["status"], "in_progress")
        executor.on_result("eat1", "eat_item:berries", actions["eat_item:berries"], "completed", "ok", None, state)
        executor.on_result("drop1", "drop_item:log", actions["drop_item:log"], "completed", "ok", None, state)

        # Equip axe & unequip torch reflected in inventory
        state2 = copy.deepcopy(state)
        state2["observed_at"] = 2
        state2["player"]["inventory"]["equipped"] = [{"prefab": "axe", "slot": "hands", "durability_percent": 1.0}]
        view2 = executor.view(board, state2)
        self.assertEqual(view2["status"], "completed")


class Phase2CampWorkUnificationTests(unittest.TestCase):
    def test_player_camp_work_accepts_icebox_and_dragonflychest(self):
        state = {
            "camp_work": {"available": True, "fixtures": []},
            "places": {"buildings": [
                {"id": "building:60", "prefab": "icebox", "status": "observed"},
                {"id": "building:61", "prefab": "dragonflychest", "status": "observed"},
            ]},
        }
        self.assertEqual(camp_target_error({"type": "deposit", "building_id": "building:60", "prefab": "berries", "count": 2}, state), "")
        self.assertEqual(camp_target_error({"type": "withdraw", "building_id": "building:61", "prefab": "log", "count": 1}, state), "")

    def test_companion_cook_meal_full_lifecycle(self):
        tasks = CompanionTasks(clock=lambda: 10.0)
        obs = companion_obs()
        obs["buddy"]["inventory"] = {"meat": 1, "berries": 3}
        obs["places"]["buildings"].append({"id": "building:70", "guid": 70, "prefab": "cookpot", "x": 8.0, "z": 0.0, "status": "observed", "arrived": False})
        obs["camp_work"] = {
            "available": True,
            "fixtures": [{"building_id": "building:70", "guid": 70, "prefab": "cookpot", "phase": "closed", "contents_known": True, "items": []}],
            "trees": [],
            "meals": [{"product": "meatballs", "ingredients": ["berries", "berries", "berries", "meat"], "cook_seconds": 15}],
        }
        plan = {
            "day": 1,
            "goal": "Cook meatballs",
            "tasks": [{"id": "cook1", "title": "Cook meatballs", "target": {
                "type": "cook_meal", "building_id": "building:70", "product": "meatballs",
                "ingredients": ["meat", "berries", "berries", "berries"],
            }}],
        }
        tasks.adopt(plan, obs)

        # 1. Not arrived -> visit_building
        _, op, args = tasks.next_action(obs)
        self.assertEqual((op, args), ("visit_building", {"building_id": "building:70"}))

        # 2. Arrived, empty pot -> cook_load
        obs2 = copy.deepcopy(obs)
        obs2["version"] = 2
        obs2["places"]["buildings"][-1]["arrived"] = True
        _, op2, args2 = tasks.next_action(obs2)
        self.assertEqual(op2, "cook_load")
        self.assertEqual(args2["building_id"], "building:70")
        self.assertIn(args2["prefab"], {"berries", "meat"})

        # 3. All 4 ingredients loaded -> start_meal
        obs3 = copy.deepcopy(obs2)
        obs3["version"] = 3
        obs3["camp_work"]["fixtures"][0]["items"] = [
            {"slot": 1, "prefab": "berries", "count": 1},
            {"slot": 2, "prefab": "berries", "count": 1},
            {"slot": 3, "prefab": "berries", "count": 1},
            {"slot": 4, "prefab": "meat", "count": 1},
        ]
        _, op3, args3 = tasks.next_action(obs3)
        self.assertEqual((op3, args3), ("start_meal", {"building_id": "building:70", "product": "meatballs"}))
        tasks.on_result("cook1", {"op": "start_meal", "status": "completed", "reason": "pot_started", "data": {"cook_seconds": 15}}, obs3)

        # 4. Cooking -> maintaining
        obs4 = copy.deepcopy(obs3)
        obs4["version"] = 4
        obs4["camp_work"]["fixtures"][0]["phase"] = "unavailable"
        self.assertEqual(tasks.view(obs4)["tasks"][0]["status"], "maintaining")

        # 5. Ready -> harvest_meal -> completed
        obs5 = copy.deepcopy(obs4)
        obs5["version"] = 20
        obs5["camp_work"]["fixtures"][0]["phase"] = "ready"
        _, op5, args5 = tasks.next_action(obs5)
        self.assertEqual((op5, args5), ("harvest_meal", {"building_id": "building:70", "product": "meatballs"}))
        tasks.on_result("cook1", {"op": "harvest_meal", "status": "completed", "reason": "meal_harvested:meatballs", "data": {"product": "meatballs"}}, obs5)
        self.assertEqual(tasks.view(obs5)["status"], "completed")

    def test_companion_ignite_tree_equips_torch_and_completes(self):
        tasks = CompanionTasks(clock=lambda: 10.0)
        obs = companion_obs(
            equipment=[{"guid": 901, "prefab": "torch", "equipped": False, "usable": True, "tools": []}],
            camp_work={"available": True, "fixtures": [], "meals": [], "trees": [{"guid": 801, "prefab": "evergreen", "distance": 4.0, "status": "ignitable"}]},
        )
        plan = {
            "day": 1,
            "goal": "Burn isolated tree for charcoal",
            "tasks": [{"id": "burn1", "title": "Ignite tree", "target": {"type": "ignite_tree", "tree_guid": 801}}],
        }
        tasks.adopt(plan, obs)

        # Equips torch first
        _, op, args = tasks.next_action(obs)
        self.assertEqual((op, args), ("equip", {"item_guid": 901}))

        # Torch equipped -> ignite_tree
        obs2 = copy.deepcopy(obs)
        obs2["version"] = 2
        obs2["equipment"][0]["equipped"] = True
        _, op2, args2 = tasks.next_action(obs2)
        self.assertEqual((op2, args2), ("ignite_tree", {"tree_guid": 801}))

        # Completed
        tasks.on_result("burn1", {"op": "ignite_tree", "status": "completed", "reason": "tree_ignited", "data": {"tree_guid": 801}}, obs2)
        self.assertEqual(tasks.view(obs2)["status"], "completed")

    def test_companion_bridge_validates_cooking_ignite_and_meals_query(self):
        now = [100.0]
        bridge = CompanionBridge(TOKEN, clock=lambda: now[0])
        snap = companion_obs()
        snap["places"]["buildings"].append({"id": "building:70", "guid": 70, "prefab": "cookpot", "status": "observed"})
        snap["camp_work"] = {
            "available": True,
            "fixtures": [{"building_id": "building:70", "guid": 70, "prefab": "cookpot", "phase": "closed", "contents_known": True, "items": []}],
            "trees": [{"guid": 801, "prefab": "evergreen", "distance": 4.0, "status": "ignitable"}],
            "meals": [{"product": "meatballs", "ingredients": ["berries", "berries", "berries", "meat"], "cook_seconds": 15}],
        }
        bridge.exchange({
            "token": TOKEN,
            "session_id": "s1",
            "sequence": 1,
            "state": {
                "ready": True,
                "snapshot_age_ms": 10,
                "control": {"enabled": True, "mode": "instruction", "revision": 1, "instruction": "cook", "messages": []},
                "companion": snap,
            },
        })
        self.assertEqual(len(bridge.meals("meatballs")["meals"]), 1)

        for op, args in (
            ("cook_load", {"building_id": "building:70", "prefab": "meat"}),
            ("start_meal", {"building_id": "building:70", "product": "meatballs"}),
            ("harvest_meal", {"building_id": "building:70", "product": "meatballs"}),
            ("ignite_tree", {"tree_guid": 801}),
        ):
            obs_id = bridge.observe()["observation_id"]
            rec = bridge.action(op, obs_id, args)
            self.assertEqual(rec["op"], op)
            bridge.actions[rec["id"]]["status"] = "completed"
            bridge.active = None

        obs_id = bridge.observe()["observation_id"]
        with self.assertRaises(BridgeError):
            bridge.action("start_meal", obs_id, {"building_id": "building:70", "product": "wetgoop"})
        with self.assertRaises(BridgeError):
            bridge.action("ignite_tree", obs_id, {"tree_guid": 999})

    def test_hunger_ratio_avoids_reserved_cooked_meal_when_other_food_exists(self):
        tasks = CompanionTasks(clock=lambda: 10.0)
        obs = companion_obs(
            foods=[
                {"prefab": "berries", "count": 4, "hunger_delta": 9.375, "health_delta": 0, "sanity_delta": 0},
                {"prefab": "meatballs", "count": 1, "hunger_delta": 62.5, "health_delta": 3, "sanity_delta": 5},
            ],
            camp_work={
                "available": True,
                "fixtures": [{"building_id": "building:70", "guid": 70, "prefab": "cookpot", "phase": "closed", "contents_known": True, "items": []}],
                "trees": [],
                "meals": [{"product": "meatballs", "ingredients": ["berries", "berries", "berries", "meat"], "cook_seconds": 15}],
            },
        )
        obs["buddy"]["hunger"] = 50
        obs["buddy"]["hunger_max"] = 150
        obs["places"]["buildings"].append({"id": "building:70", "guid": 70, "prefab": "cookpot", "x": 8.0, "z": 0.0, "status": "observed", "arrived": True})
        plan = {
            "day": 1,
            "goal": "Cook meatballs for owner and stay fed",
            "tasks": [
                {"id": "cook1", "title": "Cook meatballs", "priority": 2, "target": {
                    "type": "cook_meal", "building_id": "building:70", "product": "meatballs",
                    "ingredients": ["meat", "berries", "berries", "berries"],
                }},
                {"id": "eat1", "title": "Keep hunger >= 60%", "priority": 1, "target": {"type": "hunger_ratio", "at_least": 0.6}},
            ],
        }
        tasks.adopt(plan, obs)
        _, op, args = tasks.next_action(obs)
        self.assertEqual(op, "eat")
        self.assertEqual(args["prefab"], "berries")

    def test_charcoal_inventory_waits_for_burning_tree_then_chops_burnt_tree(self):
        tasks = CompanionTasks(clock=lambda: 10.0)
        obs = companion_obs(
            mode="stay",
            equipment=[{"guid": 902, "prefab": "axe", "equipped": True, "usable": True, "tools": ["CHOP"]}],
            camp_work={"available": True, "fixtures": [], "meals": [], "trees": [{"guid": 801, "prefab": "evergreen", "distance": 3.0, "status": "burning"}]},
        )
        plan = {
            "day": 1,
            "goal": "Collect charcoal from burning tree",
            "tasks": [{"id": "coal1", "title": "Get 1 charcoal", "target": {"type": "inventory", "prefab": "charcoal", "count": 1}}],
        }
        tasks.adopt(plan, obs)

        # While tree is burning -> wait in place instead of running search
        _, op, _ = tasks.next_action(obs)
        self.assertEqual(op, "waiting")

        # If too close to burning tree with coordinates -> step back to ~7 units away
        obs_close = copy.deepcopy(obs)
        obs_close["camp_work"]["trees"][0].update(distance=2.0, x=2.0, z=0.0)
        _, op_retreat, args_retreat = tasks.next_action(obs_close)
        self.assertEqual(op_retreat, "move")
        self.assertAlmostEqual(args_retreat["x"], -5.0, places=1)

        # Once burnt -> chop burnt tree for charcoal
        obs2 = copy.deepcopy(obs)
        obs2["version"] = 2
        obs2["camp_work"]["trees"][0]["status"] = "burnt"
        obs2["work_targets"] = [{"guid": 801, "prefab": "evergreen", "work": "CHOP", "products": ["charcoal"], "remaining": 1, "distance": 3.0}]
        _, op2, args2 = tasks.next_action(obs2)
        self.assertEqual((op2, args2), ("work", {"target_guid": 801, "work": "CHOP"}))

    def test_camp_execution_batch_transfer_credits_verified_count(self):
        from executor import camp_execution

        rec = {}
        camp_execution.on_completed(
            {"type": "deposit", "building_id": "building:60", "prefab": "berries", "count": 10},
            rec, {"kind": "store_item"}, "items_transferred:10", {},
        )
        self.assertEqual(rec["camp_count"], 10)

    def test_companion_combat_auto_equips_armor_weapon_and_kills_or_retreats(self):
        tasks = CompanionTasks(clock=lambda: 10.0)
        obs = companion_obs(
            capabilities=sorted(list(companion_obs()["capabilities"]) + ["combat"]),
            equipment=[
                {"guid": 301, "prefab": "axe", "slot": "hands", "equipped": True, "usable": True, "tools": ["CHOP"], "damage": 27},
                {"guid": 302, "prefab": "spear", "slot": "hands", "equipped": False, "usable": True, "tools": [], "damage": 34},
                {"guid": 303, "prefab": "armorwood", "slot": "body", "equipped": False, "usable": True, "tools": [], "armor": 0.8},
                {"guid": 304, "prefab": "footballhat", "slot": "head", "equipped": False, "usable": True, "tools": [], "armor": 0.8},
            ],
            enemies=[
                {"guid": 901, "prefab": "spider", "health": 100, "health_max": 100, "distance": 5.0, "x": 5.0, "z": 0.0,
                 "hostile": True, "targeting_buddy": False, "targeting_owner": True},
            ],
        )
        tasks.adopt({
            "day": 1, "goal": "Slay spider",
            "tasks": [{"id": "fight1", "title": "Slay spider", "priority": 1, "target": {"type": "attack", "target_guid": 901}}],
        }, obs)

        # 1. Auto-equip body armor -> head helmet -> best weapon (spear > axe) -> attack
        _, op1, args1 = tasks.next_action(obs)
        self.assertEqual((op1, args1), ("equip", {"item_guid": 303}))
        obs["equipment"][2]["equipped"] = True

        _, op2, args2 = tasks.next_action(obs)
        self.assertEqual((op2, args2), ("equip", {"item_guid": 304}))
        obs["equipment"][3]["equipped"] = True

        _, op3, args3 = tasks.next_action(obs)
        self.assertEqual((op3, args3), ("equip", {"item_guid": 302}))
        obs["equipment"][0]["equipped"] = False
        obs["equipment"][1]["equipped"] = True

        _, op4, args4 = tasks.next_action(obs)
        self.assertEqual((op4, args4), ("attack", {"target_guid": 901}))

        # Low HP triggers safe retreat instead of suicide
        low_obs = copy.deepcopy(obs)
        low_obs["buddy"]["health"] = 30
        low_obs["buddy"]["health_max"] = 150
        _, op_low, args_low = tasks.next_action(low_obs)
        self.assertEqual((op_low, args_low), ("needs_replan", {"reason": "low_health_retreat"}))

        # Kill receipt completes the attack task
        tasks.on_result("fight1", {"op": "attack", "status": "completed", "reason": "target_killed", "data": {"target_guid": 901}}, obs)
        self.assertEqual(tasks.view(obs)["status"], "completed")

    def test_companion_defend_owner_and_expanded_meals_validation(self):
        tasks = CompanionTasks(clock=lambda: 10.0)
        obs = companion_obs(
            capabilities=sorted(list(companion_obs()["capabilities"]) + ["combat"]),
            enemies=[
                {"guid": 910, "prefab": "hound", "health": 150, "health_max": 150, "distance": 6.0, "x": 3.0, "z": 0.0,
                 "hostile": True, "targeting_buddy": False, "targeting_owner": True},
            ],
        )
        tasks.adopt({
            "day": 1, "goal": "Protect owner",
            "tasks": [{"id": "def1", "title": "Defend owner", "priority": 1, "duration": 0.2,
                       "target": {"type": "defend_owner", "min_health_ratio": 0.4}}],
        }, obs)
        _, op, args = tasks.next_action(obs)
        self.assertEqual((op, args), ("attack", {"target_guid": 910, "min_health_ratio": 0.4}))

        # Bridge validates attack capability + observed enemy GUID and expanded meals
        now = [100.0]
        bridge = CompanionBridge(TOKEN, clock=lambda: now[0])
        snap = companion_obs(
            capabilities=sorted(list(companion_obs()["capabilities"]) + ["combat"]),
            enemies=obs["enemies"],
        )
        snap["places"]["buildings"].append({"id": "building:70", "guid": 70, "prefab": "cookpot", "status": "observed"})
        bridge.exchange({
            "token": TOKEN, "session_id": "s2", "sequence": 1,
            "state": {"ready": True, "snapshot_age_ms": 10,
                      "control": {"enabled": True, "mode": "instruction", "revision": 1, "instruction": "fight", "messages": []},
                      "companion": snap},
        })
        obs_id = bridge.observe()["observation_id"]
        rec = bridge.action("attack", obs_id, {"target_guid": 910, "min_health_ratio": 0.4})
        self.assertEqual(rec["op"], "attack")
        bridge.actions[rec["id"]]["status"] = "completed"
        bridge.active = None

        obs_id = bridge.observe()["observation_id"]
        with self.assertRaises(BridgeError):
            bridge.action("attack", obs_id, {"target_guid": 99999})
        for dish in ("perogies", "baconeggs", "bonestew", "honeyham", "honeynuggets", "taffy", "trailmix", "dragonpie"):
            obs_id = bridge.observe()["observation_id"]
            rec_m = bridge.action("start_meal", obs_id, {"building_id": "building:70", "product": dish})
            self.assertEqual(rec_m["op"], "start_meal")
            bridge.actions[rec_m["id"]]["status"] = "completed"
            bridge.active = None

    def test_companion_health_ratio_heals_then_falls_back_to_food_and_requires_missing(self):
        tasks = CompanionTasks(clock=lambda: 10.0)
        obs = companion_obs(
            capabilities=sorted(list(companion_obs()["capabilities"]) + ["heal"]),
            healers=[{"guid": 501, "prefab": "healingsalve", "count": 1, "health_delta": 20}],
            foods=[
                {"guid": 502, "prefab": "perogies", "count": 1, "hunger_delta": 37.5, "sanity_delta": 5, "health_delta": 40},
                {"guid": 503, "prefab": "monstermeat", "count": 1, "hunger_delta": 18.75, "sanity_delta": -20, "health_delta": -20},
            ],
        )
        obs["buddy"].update(health=50, health_max=150)
        tasks.adopt({
            "day": 1, "goal": "Recover health",
            "tasks": [{"id": "heal1", "title": "Heal to 60%", "priority": 1, "target": {"type": "health_ratio", "at_least": 0.6}}],
        }, obs)

        # 1. Uses carried healer first
        _, op1, args1 = tasks.next_action(obs)
        self.assertEqual(op1, "heal")
        self.assertEqual(args1["prefab"], "healingsalve")
        self.assertEqual(args1["total_count"], 1)
        tasks.on_result("heal1", {"op": "heal", "status": "completed", "reason": "healed",
                                  "data": {"task_key": args1["task_key"], "count": 1}}, obs)

        # 2. Healer spent; falls back to safe healing food
        obs["healers"] = []
        obs["buddy"]["health"] = 70
        _, op2, args2 = tasks.next_action(obs)
        self.assertEqual(op2, "eat")
        self.assertEqual(args2["prefab"], "perogies")
        self.assertEqual(args2["total_count"], 1)
        tasks.on_result("heal1", {"op": "eat", "status": "completed", "reason": "food_eaten",
                                  "data": {"task_key": args2["task_key"], "count": 1}}, obs)

        # 3. Still below target with no healers or healing food -> needs_replan
        obs["foods"] = []
        obs["buddy"]["health"] = 75
        _, op3, args3 = tasks.next_action(obs)
        self.assertEqual((op3, args3), ("needs_replan", {"reason": "healing_item_missing"}))

        # 4. At >=60% health completes target
        obs["buddy"]["health"] = 95
        self.assertEqual(tasks.view(obs)["status"], "completed")

    def test_companion_thermal_comfort_winter_summer_and_rain_and_coldfire_pause(self):
        from common.thermal import refuel_pause
        from executor.companion_tasks import actor_state

        # Winter: equips winterhat -> visits firepit -> refuels with log -> waits until warm
        tasks = CompanionTasks(clock=lambda: 10.0)
        obs = companion_obs(
            capabilities=sorted(list(companion_obs()["capabilities"]) + ["thermal", "refuel", "visit_building"]),
            equipment=[
                {"guid": 601, "prefab": "winterhat", "slot": "head", "equipped": False, "usable": True,
                 "tools": [], "insulation": 120, "insulation_type": "winter"},
            ],
            refueling={"available": True, "ratio_available": True,
                       "targets": [{"building_id": "building:80", "guid": 80, "prefab": "firepit",
                                    "is_cold": False, "lit": False, "fuel_percent": 0.0}]},
        )
        obs["buddy"].update(temperature=4.0, inventory={"log": 2, "winterhat": 1})
        obs["places"]["buildings"].append({"id": "building:80", "guid": 80, "prefab": "firepit",
                                           "status": "observed", "arrived": False, "distance": 6.0})
        tasks.adopt({
            "day": 1, "goal": "Stay warm",
            "tasks": [{"id": "therm", "title": "Thermal comfort", "priority": 1,
                       "target": {"type": "thermal_comfort", "min_temp": 18, "max_temp": 55}}],
        }, obs)
        _, op, args = tasks.next_action(obs)
        self.assertEqual((op, args), ("equip", {"item_guid": 601}))
        obs["equipment"][0]["equipped"] = True

        _, op, args = tasks.next_action(obs)
        self.assertEqual((op, args), ("visit_building", {"building_id": "building:80"}))
        obs["places"]["buildings"][-1]["arrived"] = True

        _, op, args = tasks.next_action(obs)
        self.assertEqual(op, "refuel")
        self.assertEqual((args["building_id"], args["fuel_prefab"]), ("building:80", "log"))
        obs["refueling"]["targets"][0].update(lit=True, fuel_percent=0.5)

        _, op, _ = tasks.next_action(obs)
        self.assertEqual(op, "waiting")

        # Summer: overheating equips strawhat + umbrella -> visits coldfirepit -> refuels nitre (not paused by heat!)
        hot_obs = companion_obs(
            capabilities=sorted(list(companion_obs()["capabilities"]) + ["thermal", "refuel", "visit_building"]),
            equipment=[
                {"guid": 602, "prefab": "strawhat", "slot": "head", "equipped": False, "usable": True,
                 "tools": [], "insulation": 60, "insulation_type": "summer", "waterproof": 0.2},
                {"guid": 603, "prefab": "umbrella", "slot": "hands", "equipped": False, "usable": True,
                 "tools": [], "insulation": 120, "insulation_type": "summer", "waterproof": 0.9},
            ],
            refueling={"available": True, "ratio_available": True,
                       "targets": [{"building_id": "building:81", "guid": 81, "prefab": "coldfirepit",
                                    "is_cold": True, "lit": True, "fuel_percent": 0.1}]},
        )
        hot_obs["buddy"].update(temperature=68.0, overheating=True, inventory={"nitre": 2})
        hot_obs["places"]["buildings"].append({"id": "building:81", "guid": 81, "prefab": "coldfirepit",
                                               "status": "observed", "arrived": True, "distance": 2.0})
        self.assertEqual(refuel_pause({"type": "refuel", "building_id": "building:81", "fuel_prefab": "nitre", "count": 1},
                                      actor_state(hot_obs)), "")
        t_hot = CompanionTasks(clock=lambda: 10.0)
        t_hot.adopt({
            "day": 1, "goal": "Cool down",
            "tasks": [{"id": "cool", "title": "Cool down", "priority": 1,
                       "target": {"type": "thermal_comfort", "min_temp": 15, "max_temp": 50}}],
        }, hot_obs)
        self.assertEqual(t_hot.next_action(hot_obs)[1:], ("equip", {"item_guid": 602}))
        hot_obs["equipment"][0]["equipped"] = True
        self.assertEqual(t_hot.next_action(hot_obs)[1:], ("equip", {"item_guid": 603}))
        hot_obs["equipment"][1]["equipped"] = True
        _, op_cold, args_cold = t_hot.next_action(hot_obs)
        self.assertEqual(op_cold, "refuel")
        self.assertEqual((args_cold["building_id"], args_cold["fuel_prefab"]), ("building:81", "nitre"))
        hot_obs["buddy"].update(temperature=25.0, overheating=False)
        self.assertEqual(t_hot.view(hot_obs)["status"], "completed")

        # Rain maintenance: equips umbrella when wet
        rain_tasks = CompanionTasks(clock=lambda: 10.0)
        rain_obs = companion_obs(
            capabilities=sorted(list(companion_obs()["capabilities"]) + ["thermal"]),
            equipment=[{"guid": 604, "prefab": "umbrella", "slot": "hands", "equipped": False, "usable": True,
                        "tools": [], "waterproof": 0.9}],
        )
        rain_obs["buddy"].update(temperature=25.0, moisture=35.0)
        rain_obs["world"]["israining"] = True
        rain_tasks.adopt({
            "day": 1, "goal": "Keep dry",
            "tasks": [{"id": "dry", "title": "Stay dry", "priority": 1, "duration": 0.2,
                       "target": {"type": "thermal_comfort", "min_temp": 15, "max_temp": 55}}],
        }, rain_obs)
        self.assertEqual(rain_tasks.next_action(rain_obs)[1:], ("equip", {"item_guid": 604}))

    def test_companion_thermal_comfort_winter_crafts_gear_heatrock_places_fire_and_roasts(self):
        tasks = CompanionTasks(clock=lambda: 10.0)
        obs = companion_obs(
            capabilities=sorted(list(companion_obs()["capabilities"]) + ["thermal", "refuel", "visit_building"]),
            recipes={
                "winterhat": {"recipe_id": "winterhat", "product": "winterhat", "placement": False, "supported": True, "ready": True},
                "heatrock": {"recipe_id": "heatrock", "product": "heatrock", "placement": False, "supported": True, "ready": True},
                "firepit": {"recipe_id": "firepit", "product": "firepit", "placement": True, "supported": True, "ready": True},
            },
            places={"available": True, "scope": "companion_observed_buildings", "identity": "server_guid", "buildings": []},
            thermal={"stones": [], "winter_insulation": 0, "summer_insulation": 0, "waterproof": 0},
        )
        obs["world"]["season"] = "winter"
        obs["buddy"].update(temperature=4.0, position={"x": 2.0, "z": 3.0}, inventory={"log": 3})
        tasks.adopt({
            "day": 1, "goal": "Prepare winter gear and roast thermal stone",
            "tasks": [{"id": "w1", "title": "Winter survival", "priority": 1,
                       "target": {"type": "thermal_comfort", "min_temp": 18, "max_temp": 55, "charge_heatrock": "warm"}}],
        }, obs)

        # 1. Craft winterhat -> equip -> craft heatrock -> place firepit -> refuel -> roast stone >= 45C
        self.assertEqual(tasks.next_action(obs)[1:], ("craft", {"recipe_id": "winterhat"}))
        obs["equipment"] = [{"guid": 701, "prefab": "winterhat", "slot": "head", "equipped": False,
                             "usable": True, "tools": [], "insulation": 120, "insulation_type": "winter"}]
        self.assertEqual(tasks.next_action(obs)[1:], ("equip", {"item_guid": 701}))
        obs["equipment"][0]["equipped"] = True

        self.assertEqual(tasks.next_action(obs)[1:], ("craft", {"recipe_id": "heatrock"}))
        obs["thermal"]["stones"] = [{"guid": 702, "prefab": "heatrock", "temperature": 20.0, "range": 3, "durability": 1.0}]

        self.assertEqual(tasks.next_action(obs)[1:], ("place", {"recipe_id": "firepit", "x": 2.0, "z": 3.0, "radius": 6}))
        obs["places"]["buildings"].append({"id": "building:90", "guid": 90, "prefab": "firepit",
                                           "status": "observed", "arrived": True, "distance": 1.5})
        obs["refueling"] = {"available": True, "ratio_available": True,
                            "targets": [{"building_id": "building:90", "guid": 90, "prefab": "firepit",
                                         "is_cold": False, "lit": True, "fuel_percent": 0.30}]}
        obs["buddy"]["temperature"] = 25.0  # Body temp ok, but heatrock still 20C < 45C and fire fuel 0.30 < 0.45!
        _, op_ref, args_ref = tasks.next_action(obs)
        self.assertEqual(op_ref, "refuel")
        self.assertEqual((args_ref["building_id"], args_ref["fuel_prefab"]), ("building:90", "log"))
        obs["refueling"]["targets"][0]["fuel_percent"] = 0.65
        self.assertEqual(tasks.next_action(obs)[1:], ("waiting", {}))

        obs["thermal"]["stones"][0]["temperature"] = 48.0
        self.assertEqual(tasks.view(obs)["status"], "completed")

    def test_companion_thermal_comfort_summer_ignores_firepit_places_coldfire_and_chills_heatrock(self):
        tasks = CompanionTasks(clock=lambda: 10.0)
        obs = companion_obs(
            capabilities=sorted(list(companion_obs()["capabilities"]) + ["thermal", "refuel", "visit_building"]),
            recipes={
                "strawhat": {"recipe_id": "strawhat", "product": "strawhat", "placement": False, "supported": True, "ready": True},
                "coldfire": {"recipe_id": "coldfire", "product": "coldfire", "placement": True, "supported": True, "ready": True},
            },
            thermal={"stones": [{"guid": 710, "prefab": "heatrock", "temperature": 38.0, "range": 4, "durability": 1.0}],
                     "winter_insulation": 0, "summer_insulation": 0, "waterproof": 0},
        )
        # Note companion_obs() already has building:40 firepit in places; Summer must ignore it and place coldfire.
        obs["world"]["season"] = "summer"
        obs["buddy"].update(temperature=66.0, overheating=True, position={"x": -2.0, "z": 1.0}, inventory={"nitre": 2})
        tasks.adopt({
            "day": 1, "goal": "Cool down in summer",
            "tasks": [{"id": "s1", "title": "Summer survival", "priority": 1,
                       "target": {"type": "thermal_comfort", "min_temp": 12, "max_temp": 45, "charge_heatrock": "cool"}}],
        }, obs)

        self.assertEqual(tasks.next_action(obs)[1:], ("craft", {"recipe_id": "strawhat"}))
        obs["equipment"] = [{"guid": 711, "prefab": "strawhat", "slot": "head", "equipped": True,
                             "usable": True, "tools": [], "insulation": 60, "insulation_type": "summer"}]
        self.assertEqual(tasks.next_action(obs)[1:], ("place", {"recipe_id": "coldfire", "x": -2.0, "z": 1.0, "radius": 6}))

        obs["places"]["buildings"].append({"id": "building:91", "guid": 91, "prefab": "coldfire",
                                           "status": "observed", "arrived": True, "distance": 1.5})
        obs["refueling"] = {"available": True, "ratio_available": True,
                            "targets": [{"building_id": "building:91", "guid": 91, "prefab": "coldfire",
                                         "is_cold": True, "lit": True, "fuel_percent": 0.30}]}
        obs["buddy"].update(temperature=35.0, overheating=False)  # Body temp cooled, heatrock still 38C > 20C
        _, op_ref, args_ref = tasks.next_action(obs)
        self.assertEqual(op_ref, "refuel")
        self.assertEqual((args_ref["building_id"], args_ref["fuel_prefab"]), ("building:91", "nitre"))
        obs["thermal"]["stones"][0]["temperature"] = 18.0
        self.assertEqual(tasks.view(obs)["status"], "completed")

    def test_companion_spring_rain_drying_and_seasonal_alert_reflex(self):
        from executor.team_runner import seasonal_alert_reason

        tasks = CompanionTasks(clock=lambda: 10.0)
        obs = companion_obs(
            capabilities=sorted(list(companion_obs()["capabilities"]) + ["thermal", "visit_building", "refuel"]),
            recipes={"grass_umbrella": {"recipe_id": "grass_umbrella", "product": "grass_umbrella",
                                        "placement": False, "supported": True, "ready": True}},
        )
        obs["world"].update(season="spring", israining=True)
        obs["buddy"].update(temperature=25.0, moisture=55.0)
        tasks.adopt({
            "day": 1, "goal": "Dry off in spring rain",
            "tasks": [{"id": "r1", "title": "Stay dry", "priority": 1,
                       "target": {"type": "thermal_comfort", "min_temp": 12, "max_temp": 55, "max_moisture": 40}}],
        }, obs)

        self.assertEqual(tasks.next_action(obs)[1:], ("craft", {"recipe_id": "grass_umbrella"}))
        obs["equipment"] = [{"guid": 720, "prefab": "grass_umbrella", "slot": "hands", "equipped": True,
                             "usable": True, "tools": [], "waterproof": 0.5}]
        # Still wet (55 > 40): travels to firepit (building:40) to dry off
        self.assertEqual(tasks.next_action(obs)[1:], ("visit_building", {"building_id": "building:40"}))
        obs["buddy"]["moisture"] = 35.0
        self.assertEqual(tasks.view(obs)["status"], "completed")

        # Autonomous seasonal alert reflex triggers on freezing/overheating/wet and suppresses if thermal_comfort active
        alert_obs = companion_obs(
            capabilities=sorted(list(companion_obs()["capabilities"]) + ["thermal"]),
            equipment=[{"guid": 721, "prefab": "umbrella", "slot": "hands", "equipped": False, "usable": True,
                        "tools": [], "waterproof": 0.9}],
        )
        alert_obs["buddy"].update(temperature=6.0, freezing=True)
        self.assertEqual(seasonal_alert_reason(alert_obs, {"tasks": []}), "thermal_emergency_freezing")
        self.assertIsNone(seasonal_alert_reason(alert_obs, {"tasks": [
            {"id": "t", "status": "in_progress", "target": {"type": "thermal_comfort"}}
        ]}))
        alert_obs["buddy"].update(temperature=64.0, freezing=False, overheating=True)
        self.assertEqual(seasonal_alert_reason(alert_obs, {"tasks": []}), "thermal_emergency_overheating")
        alert_obs["buddy"].update(temperature=24.0, overheating=False, moisture=45.0)
        alert_obs["world"]["israining"] = True
        self.assertEqual(seasonal_alert_reason(alert_obs, {"tasks": []}), "thermal_emergency_wet")


if __name__ == "__main__":
    unittest.main()
