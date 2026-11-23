"""Structured task board schema, view projection, and alias validation tests."""
import unittest

from common import game_rules as rules
from common.task_board import (validate_board, board_view, format_board,
                               INVENTORY_TARGET_PREFABS, INVENTORY_PREFAB_ALIASES)
from fixtures import state


def raw_board():
    return {"goal": "Prepare light and materials", "tasks": [
        {"id": "grass", "title": "Reserve four grass", "priority": 1,
         "target": {"type": "inventory", "prefab": "cutgrass", "count": 4},
         "depends_on": [], "action_hints": ["collect_grass"]},
        {"id": "torch", "title": "Carry a torch", "priority": 2,
         "target": {"type": "inventory", "prefab": "torch", "count": 1},
         "depends_on": ["grass"], "action_hints": ["craft_torch"]}]}


class BoardTests(unittest.TestCase):
    def test_cli_table_shows_tasks_progress_and_blockers_without_control_sequences(self):
        board = validate_board(raw_board(), 1)
        board["tasks"][0]["title"] = "收集草以备夜晚使用" * 8 + "\x1b[2J"
        rendered = format_board(board_view(board, state([], cutgrass=2)), width=80)
        self.assertIn("TASK BOARD", rendered)
        self.assertIn("in_progress", rendered)
        self.assertIn("2/4", rendered)
        self.assertIn("blocked", rendered)
        self.assertIn("dependencies: grass", rendered)
        self.assertNotIn("\x1b", rendered)
        self.assertIn("...", rendered)
        row_widths = [len(line) for line in rendered.splitlines() if line.startswith("+")]
        self.assertEqual(len(set(row_widths)), 1)

    def test_status_is_runtime_owned_and_dependencies_are_validated(self):
        raw = raw_board()
        raw["tasks"][0].update(status="done", progress={"current": 999})
        board = validate_board(raw, 1)
        self.assertNotIn("status", board["tasks"][0])
        for variant in ("duplicate", "cycle", "unknown", "bad_target", "too_many"):
            changed = raw_board()
            if variant == "duplicate": changed["tasks"][1]["id"] = "grass"
            elif variant == "cycle": changed["tasks"][0]["depends_on"] = ["torch"]
            elif variant == "unknown": changed["tasks"][0]["depends_on"] = ["missing"]
            elif variant == "bad_target": changed["tasks"][0]["target"]["type"] = "eval"
            else: changed["tasks"] *= 4
            with self.subTest(variant=variant), self.assertRaises(ValueError):
                validate_board(changed, 1)

    def test_inventory_alias_is_normalized_before_progress_and_dependencies(self):
        raw = raw_board()
        raw["tasks"][0].update(id="logs", title="Gather logs")
        raw["tasks"][0]["target"] = {"type": "inventory", "prefab": " LOGS ", "count": 2}
        raw["tasks"][1]["depends_on"] = ["logs"]
        board = validate_board(raw, 1)
        self.assertEqual(board["tasks"][0]["target"]["prefab"], "log")
        self.assertEqual(board["tasks"][0]["id"], "logs")
        self.assertEqual(raw["tasks"][0]["target"]["prefab"], " LOGS ")
        actual = state([])
        actual["player"]["inventory"]["items"] = [{"prefab": "log", "count": 2}]
        view = board_view(board, actual)
        self.assertEqual(view["tasks"][0]["status"], "done")
        self.assertEqual(view["tasks"][1]["status"], "ready")

    def test_inventory_names_are_explicit_not_fuzzy_or_singularized(self):
        for prefab in INVENTORY_TARGET_PREFABS:
            raw = raw_board()
            raw["tasks"][0]["target"]["prefab"] = prefab
            self.assertEqual(validate_board(raw, 1)["tasks"][0]["target"]["prefab"], prefab)
        for invalid in ("loggs", "rock", "twig", "grass", "sapling", "rock1", "unknown_item", "log;wait", None):
            raw = raw_board()
            raw["tasks"][0]["target"]["prefab"] = invalid
            with self.subTest(invalid=invalid), self.assertRaises(ValueError): validate_board(raw, 1)

    def test_inventory_catalog_covers_current_collection_work_and_food_products(self):
        products = set(rules.COLLECT_PRODUCTS.values()) | {"carrot"}
        products |= set().union(*rules.MINE_PRODUCTS.values())
        products |= {"log", "pinecone", "acorn", "charcoal", "livinglog", "marble", "moonrocknugget",
                     "ice", "redgem", "bluegem", "purplegem", "thulecite", "thulecite_pieces"}
        products |= set(rules.SAFE_FOOD_PREFABS)
        products |= set(rules.RAW_COOKABLE_FOOD_PREFABS)
        products |= {"torch", "axe", "pickaxe"}
        self.assertTrue(products <= INVENTORY_TARGET_PREFABS, products - INVENTORY_TARGET_PREFABS)

    def test_observed_inventory_updates_progress_dependencies_and_current_stock(self):
        board = validate_board(raw_board(), 1)
        board["quarter"] = 1
        view = board_view(board, state([], cutgrass=2))
        self.assertEqual([t["status"] for t in view["tasks"]], ["in_progress", "blocked"])
        self.assertEqual(view["ready_task_ids"], ["grass"])
        ready = board_view(board, state([], cutgrass=4))
        self.assertEqual([t["status"] for t in ready["tasks"]], ["done", "ready"])
        self.assertEqual(ready["ready_task_ids"], ["torch"])
        self.assertNotIn("status", board["tasks"][0])
        spent = board_view(board, state([], cutgrass=1))
        self.assertEqual(spent["tasks"][0]["progress"]["current"], 1)
        self.assertEqual(spent["tasks"][1]["status"], "blocked")

    def test_missing_hunger_or_region_telemetry_never_completes_a_task(self):
        raw = {"goal": "Recover and explore", "tasks": [
            {"id": "food", "title": "Eat", "target": {"type": "hunger_ratio", "at_least": 0.8}},
            {"id": "region", "title": "Visit forest", "target": {"type": "visit_region", "node_id": "forest"}}]}
        board = validate_board(raw, 1)
        self.assertTrue(all(t["status"] == "blocked" for t in board_view(board, {"world": {"day": 1}})["tasks"]))
        actual = state([])
        actual["navigation"]["graph"] = {"current_node": "forest"}
        self.assertTrue(all(t["status"] == "done" for t in board_view(board, actual)["tasks"]))

    def test_expired_board_has_no_ready_tasks(self):
        board = validate_board(raw_board(), 1)
        board["quarter"] = 1
        actual = state([])
        actual["world"]["time"] = 0.26
        view = board_view(board, actual)
        self.assertEqual(view["status"], "expired")
        self.assertEqual(view["ready_task_ids"], [])
        self.assertTrue(all(t["status"] == "expired" for t in view["tasks"]))

    def test_verified_science_machine_fact_completes_task_even_after_leaving_it(self):
        raw = {"goal": "Research", "tasks": [{"id": "science", "title": "Build science machine",
               "target": {"type": "structure", "prefab": "sciencemachine"}}]}
        board = validate_board(raw, 1)
        self.assertEqual(board_view(board, state([]))["tasks"][0]["status"], "ready")
        self.assertEqual(board_view(board, state([]), verified_structures=["sciencemachine"])["tasks"][0]["status"], "done")


if __name__ == "__main__":
    unittest.main()
