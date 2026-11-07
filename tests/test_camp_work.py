import copy
import unittest

from common.camp_work import query_meals, query_storage
from mcp_agent import daily_planner
from mcp_agent.priority_board import PriorityBoardExecutor, merge_goals, progress_signatures_match, task_signature
from mcp_agent.navigation_intents import ExplorationExecutor
from test_priority_board import board, state, task


CHEST, POT = "building:10", "building:20"
MEAL = {"product": "meatballs", "ingredients": ["meat", "berries", "berries", "berries"], "cook_seconds": 15}


def current():
    s = state(log=5, berries=3, meat=1)
    s["places"] = {"buildings": [
        {"id": CHEST, "guid": 10, "prefab": "treasurechest", "status": "observed"},
        {"id": POT, "guid": 20, "prefab": "cookpot", "status": "observed"},
    ]}
    s["camp_work"] = {"available": True, "fixtures": [
        {"building_id": CHEST, "prefab": "treasurechest", "phase": "closed", "contents_known": False},
        {"building_id": POT, "prefab": "cookpot", "phase": "closed", "contents_known": False},
    ], "meals": [copy.deepcopy(MEAL)], "trees": []}
    return s


def goal(identity, kind, building_id=None, **fields):
    return {"id": identity, "title": identity, "focus": True, "target": {
        "type": kind, **({"building_id": building_id} if building_id else {}), **fields}}


def offer(kind, building, **fields):
    return {"kind": kind, "building_id": building, "planner_only": True, **fields}


def find(view, identity):
    return next(t for t in view["tasks"] if t["id"] == identity)


class CampWorkTests(unittest.TestCase):
    def setUp(self):
        self.state, self.runtime, self.nav = current(), PriorityBoardExecutor(), ExplorationExecutor()

    def act(self, goals, actions):
        return self.runtime.choose(goals, self.state, actions, self.nav)

    def finish(self, chosen, reason, status="completed"):
        owner, key, action = chosen
        self.runtime.on_result(owner["id"], key, action, status, reason, self.nav, self.state)

    def test_inspect_travels_opens_and_only_completes_after_slots_observed(self):
        goals = board(goal("inspect", "inspect_storage", CHEST))
        route = {"go": offer("explore", CHEST, mode="place")}
        self.assertEqual(self.act(goals, route)[1], "go")
        self.assertEqual(self.act(goals, {"open": offer("open_container", CHEST)})[1], "open")
        self.assertEqual(find(self.runtime.view(goals, self.state), "inspect")["status"], "ready")
        self.state["camp_work"]["fixtures"][0].update(phase="open", contents_known=True, items=[])
        self.assertEqual(find(self.runtime.view(goals, self.state), "inspect")["status"], "done")
        read = query_storage(self.state["camp_work"], CHEST)
        self.assertTrue(read["fixtures"][0]["contents_known"])
        self.assertEqual(read["fixtures"][0]["items"], [])

    def test_five_stored_two_taken_are_distinct_counted_batches(self):
        deposit = goal("store", "deposit", CHEST, prefab="log", count=5)
        withdraw = {**goal("take", "withdraw", CHEST, prefab="log", count=2), "depends_on": ["store"]}
        goals = board(deposit, withdraw)
        chest = self.state["camp_work"]["fixtures"][0]
        chest.update(phase="open", contents_known=True, items=[])
        for n in range(1, 6):
            chosen = self.act(goals, {"wrong": offer("store_item", POT, product="log"),
                                      "store": offer("store_item", CHEST, product="log")})
            self.assertEqual(chosen[1], "store")
            chest["items"] = [{"prefab": "log", "count": n}]
            self.finish(chosen, "one_item_transferred")
        self.assertEqual(find(self.runtime.view(goals, self.state), "store")["status"], "done")
        for n in (4, 3):
            chosen = self.act(goals, {"take": offer("take_item", CHEST, product="log")})
            self.assertEqual(chosen[0]["id"], "take")
            chest["items"] = [{"prefab": "log", "count": n}]
            self.finish(chosen, "one_item_transferred")
        self.assertEqual(find(self.runtime.view(goals, self.state), "take")["progress"]["current"], 2)
        self.assertEqual(chest["items"][0]["count"], 3)

    def test_uncertain_transfer_does_not_retry_or_count(self):
        goals = board(goal("store", "deposit", CHEST, prefab="log", count=5))
        self.state["camp_work"]["fixtures"][0].update(phase="open", contents_known=True, items=[])
        actions = {"store": offer("store_item", CHEST, product="log")}
        self.finish(self.act(goals, actions), "camp_effect_unconfirmed:verification_timeout", "failed")
        view = find(self.runtime.view(goals, self.state), "store")
        self.assertEqual((view["status"], view["progress"]["current"]), ("failed", 0))
        self.assertIsNone(self.act(goals, actions))

    def test_quantity_review_preserves_both_completed_and_inflight_transfers(self):
        for kind, operation in (("deposit", "store_item"), ("withdraw", "take_item")):
            with self.subTest(kind=kind):
                self.setUp()
                self.state["camp_work"]["fixtures"][0].update(phase="open", contents_known=True, items=[])
                actions = {"move": offer(operation, CHEST, product="log")}
                goals = board(goal("transfer", kind, CHEST, prefab="log", count=15))
                for _ in range(7):
                    self.finish(self.act(goals, actions), "one_item_transferred")
                pending = self.act(goals, actions)
                original = task_signature(pending[0])
                revised = board(goal("transfer", kind, CHEST, prefab="log", count=12))
                self.runtime.adopt(revised)
                self.assertEqual(find(self.runtime.view(revised, self.state), "transfer")["progress"]["current"], 7)
                self.assertTrue(progress_signatures_match(original, self.runtime.records["transfer"]["signature"]))
                self.finish(pending, "one_item_transferred")
                reduced = board(goal("transfer", kind, CHEST, prefab="log", count=8))
                self.assertEqual(find(self.runtime.view(reduced, self.state), "transfer")["status"], "done")
                enlarged = board(goal("transfer", kind, CHEST, prefab="log", count=10))
                self.assertEqual(find(self.runtime.view(enlarged, self.state), "transfer")["progress"]["current"], 8)
                for _ in range(2):
                    self.finish(self.act(enlarged, actions), "one_item_transferred")
                self.assertEqual(find(self.runtime.view(enlarged, self.state), "transfer")["status"], "done")
                self.runtime.adopt(enlarged, new_batch=True)
                self.assertIsNone(self.act(enlarged, actions))
                other = board(goal("transfer", kind, POT, prefab="log", count=10))
                self.assertEqual(find(self.runtime.view(other, self.state), "transfer")["progress"]["current"], 0)

    def test_new_batch_ids_are_not_merged_as_renamed_old_storage_tasks(self):
        self.state["camp_work"]["fixtures"][0].update(phase="open", contents_known=True, items=[])
        chores = task("grass", "cutgrass")
        original = board(goal("first", "deposit", CHEST, prefab="log", count=1), chores)
        actions = {"move": offer("store_item", CHEST, product="log")}
        self.finish(self.act(original, actions), "one_item_transferred")
        previous = self.runtime.view(original, self.state)
        incoming = board(goal("second", "deposit", CHEST, prefab="log", count=1), chores)
        merged = merge_goals(previous, incoming)
        self.runtime.adopt(merged)
        selected = self.act(merged, actions)
        self.assertEqual(selected[0]["id"], "second")
        self.assertEqual(find(self.runtime.view(merged, self.state), "second")["progress"]["current"], 0)

    def test_cook_only_missing_ingredients_then_other_work_then_owned_harvest(self):
        cook = goal("meal", "cook_meal", POT, product=MEAL["product"], ingredients=MEAL["ingredients"])
        chores = task("grass", "cutgrass")
        goals = board(cook, chores)
        pot = self.state["camp_work"]["fixtures"][1]
        pot.update(phase="open", contents_known=True, items=[{"prefab": "berries", "count": 3}])
        actions = {"wrong": offer("store_item", POT, product="berries"),
                   "meat": offer("store_item", POT, product="meat")}
        self.assertEqual(self.act(goals, actions)[1], "meat")
        pot["items"].append({"prefab": "meat", "count": 1})
        chosen = self.act(goals, {"cook": offer("start_meal", POT, product="meatballs", cook_seconds=15)})
        self.assertEqual(chosen[1], "cook")
        pot.update(phase="unavailable", contents_known=False, items=[])
        self.finish(chosen, "pot_started")
        self.assertEqual(find(self.runtime.view(goals, self.state), "meal")["status"], "maintaining")
        grass = {"grass": {"kind": "collect", "product": "cutgrass", "guid": 31}}
        self.assertEqual(self.act(goals, grass)[1], "grass")
        merged = merge_goals(self.runtime.view(goals, self.state), board(chores))
        self.runtime.adopt(merged)
        pot.update(phase="ready")
        chosen = self.act(merged, {"take": offer("harvest_meal", POT)})
        self.assertEqual(chosen[0]["id"], "meal")
        pot.update(phase="closed")
        self.finish(chosen, "meal_harvested:meatballs")
        self.assertEqual(find(self.runtime.view(merged, self.state), "meal")["status"], "done")

    def test_does_not_overwrite_foreign_ingredients_or_harvest_unknown_meal(self):
        goals = board(goal("meal", "cook_meal", POT, product=MEAL["product"], ingredients=MEAL["ingredients"]))
        pot = self.state["camp_work"]["fixtures"][1]
        pot.update(phase="open", contents_known=True, items=[{"prefab": "twigs", "count": 1}])
        self.assertIsNone(self.act(goals, {"meat": offer("store_item", POT, product="meat")}))
        pot.update(phase="ready", contents_known=False)
        self.assertIsNone(self.act(goals, {"harvest": offer("harvest_meal", POT)}))

    def test_charcoal_waits_for_burnout_and_needs_explicit_permission_to_ignite(self):
        target = {"type": "inventory", "prefab": "charcoal", "count": 1}
        def charcoal(allow):
            return board({**task("charcoal", "charcoal"), "target": {**target, "allow_ignite": allow}})
        ignition = {"ignite": {"kind": "ignite_tree", "guid": 55, "planner_only": True}}
        self.assertIsNone(self.act(charcoal(False), ignition))
        self.assertEqual(self.act(charcoal(True), ignition)[1], "ignite")
        self.state["camp_work"]["trees"] = [{"guid": 55, "status": "burning"}]
        self.assertIsNone(self.act(charcoal(True), ignition))
        self.assertEqual(self.runtime.reason, "charcoal_burning")
        self.state["camp_work"]["trees"][0]["status"] = "burnt"
        chop = {"chop": {"kind": "chop_nearest_tree", "charcoal_work": True, "guid": 55, "planner_only": True}}
        self.assertEqual(self.act(charcoal(True), {**ignition, **chop})[1], "chop")

    def test_query_data_is_separate_from_persistent_planner_history(self):
        ctx = daily_planner.planning_context(self.state, [])
        self.assertEqual(query_meals(ctx["_camp_work"], "meatballs")["meals"], [MEAL])
        self.assertNotIn('"_camp_work"', daily_planner.snapshot_message(ctx))
        self.assertIn('"camp_work_summary"', daily_planner.snapshot_message(ctx))
