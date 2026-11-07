import unittest
from types import SimpleNamespace
from unittest.mock import patch

from test_companion import state, TOKEN, ImmediateWorker
from test_companion_storage import observation, board, TransferPeer
from test_companion_actions import recipe
from mcp_agent.companion_tasks import CompanionTasks, validate_board
from mcp_agent.companion_agent import CompanionAgent
from mcp_server.companion_bridge import CompanionBridge


def equipped_snapshot():
    observed = observation();observed["capabilities"].append("unequip")
    observed["buddy"]["inventory"]["axe"] = 1
    observed["equipment"] = [{"guid": 200001, "prefab": "axe", "slot": "hands", "equipped": True,
                              "usable": True, "can_unequip": True, "tools": ["CHOP"]}]
    return observed


def unequip_board():
    return validate_board({"goal": "Take off the axe", "tasks": [{"id": "off", "title": "Unequip axe", "target": {"type": "unequip", "prefab": "axe"}}]}, 1)


class UnequipTasksTests(unittest.TestCase):
    def test_explicit_unequip_requires_observed_equipment_change_not_tool_durability(self):
        observed = equipped_snapshot();observed["equipment"][0]["usable"] = False
        runtime = CompanionTasks();runtime.adopt(unequip_board(), observed)
        self.assertEqual(runtime.next_action(observed)[1:], ("unequip", {"item_guid": 200001}))
        observed["equipment"][0]["equipped"] = False
        self.assertEqual(runtime.next_action(observed)[1], "completed")

    def test_outgoing_tools_are_unequipped_before_giving_dropping_or_storing(self):
        for kind in ("give_owner", "drop_item", "deposit"):
            with self.subTest(kind=kind):
                observed = equipped_snapshot();runtime = CompanionTasks();runtime.adopt(board(kind, count=1, prefab="axe"), observed)
                self.assertEqual(runtime.next_action(observed)[1:], ("unequip", {"item_guid": 200001}))
                runtime.on_result("exchange", {"status": "completed", "reason": "unequipped"}, observed)
                self.assertEqual(runtime.view(observed)["tasks"][0]["progress"]["current"], 0)
                observed["equipment"][0]["equipped"] = False
                self.assertEqual(runtime.next_action(observed)[1], kind)

    def test_an_available_spare_tool_can_be_transferred_without_stripping_the_equipped_one(self):
        observed = equipped_snapshot();observed["equipment"].append({**observed["equipment"][0], "guid": 200002, "equipped": False})
        observed["buddy"]["inventory"]["axe"] = 2
        runtime = CompanionTasks();runtime.adopt(board(count=1, prefab="axe"), observed)
        self.assertEqual(runtime.next_action(observed)[1], "give_owner")

    def test_full_inventory_preserves_the_tool_and_requests_a_prerequisite(self):
        observed = equipped_snapshot();observed["equipment"][0].update(can_unequip=False, unequip_reason="inventory_full_or_rejected")
        runtime = CompanionTasks();runtime.adopt(board(count=1, prefab="axe"), observed)
        self.assertEqual(runtime.next_action(observed)[1:], ("needs_replan", {"reason": "inventory_full_or_rejected"}))
        self.assertTrue(observed["equipment"][0]["equipped"])

    def test_an_old_server_cannot_silently_receive_an_unequip_operation(self):
        observed = equipped_snapshot();observed["capabilities"].remove("unequip")
        runtime = CompanionTasks();runtime.adopt(board(count=1, prefab="axe"), observed)
        self.assertIn("update_server_mod", runtime.next_action(observed)[2]["reason"])
        with self.assertRaises(ValueError): runtime.adopt(unequip_board(), observed)


class UnequipBridgeTests(unittest.TestCase):
    def test_only_currently_equipped_owned_guids_may_be_unequipped(self):
        data = state();data["companion"] = equipped_snapshot()
        data["companion"]["equipment"].append({"guid": 200002, "prefab": "axe", "equipped": False})
        bridge = CompanionBridge(TOKEN, clock=lambda: 0)
        bridge.exchange({"token": TOKEN, "session_id": "local", "sequence": 1, "state": data})
        for args in ({"item_guid": True}, {"item_guid": 200002}, {"item_guid": 999}, {"item_guid": 200001, "owner_guid": 10}):
            with self.assertRaises(ValueError): bridge.action("unequip", bridge.observe()["observation_id"], args)
        self.assertEqual(bridge.action("unequip", bridge.observe()["observation_id"], {"item_guid": 200001})["op"], "unequip")


class AutoEquipPeer(TransferPeer):
    def __init__(self):
        super().__init__();observed = equipped_snapshot()
        observed["buddy"]["inventory"] = {};observed["equipment"] = []
        observed["capabilities"] += ["craft", "recipes", "equip"]
        observed["recipes"] = {"axe": recipe()};self.state["companion"] = observed;self.exchange()
    async def call_tool(self, name, args):
        if name != "buddy_action" or args["op"] not in {"craft", "unequip"}:
            return await super().call_tool(name, args)
        self.calls.append((name, args));record = self.bridge.action(**args);observed = self.state["companion"]
        if args["op"] == "craft":
            observed["buddy"]["inventory"]["axe"] = 1
            observed["equipment"] = equipped_snapshot()["equipment"]
            reason = "inventory_increased"
        else:
            observed["equipment"][0]["equipped"] = False;reason = "unequipped"
        observed["version"] += 1
        self.exchange([{"id": record["id"], "status": "completed", "revision": 1, "reason": reason}])
        return SimpleNamespace(is_error=False, structured_content=record)


class AutoEquipAgentTests(unittest.IsolatedAsyncioTestCase):
    async def test_craft_auto_equips_then_unequips_and_delivers_without_extra_planning(self):
        peer = AutoEquipPeer();contexts = []
        def planner(context, *_):
            contexts.append(context)
            if len(contexts)>1:
                return {"kind": "agent_reply", "outcome": "completed", "reply": "斧头做好并交给你了。", "evidence": "原生交付回执一把斧头。"}
            return validate_board({"goal": "Make and give an axe", "tasks": [
                {"id": "make", "title": "Make axe", "target": {"type": "inventory", "prefab": "axe", "count": 1, "recipe_id": "axe"}},
                {"id": "give", "title": "Give axe", "target": {"type": "give_owner", "prefab": "axe", "count": 1}, "depends_on": ["make"]}]}, 1)
        with patch("mcp_agent.companion_agent.ThreadPoolExecutor", ImmediateWorker):
            agent = CompanionAgent(peer, planner, execute=True)
        try:
            for _ in range(14): await agent.cycle()
            self.assertEqual([args["op"] for name,args in peer.calls if name == "buddy_action"], ["craft", "unequip", "give_owner"])
            self.assertEqual(len(contexts), 2)
            self.assertEqual(peer.state["companion"]["transfer_receipts"][0]["count"], 1)
        finally: await agent.close()


if __name__ == "__main__": unittest.main()
