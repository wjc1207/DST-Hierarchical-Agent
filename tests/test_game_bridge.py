"""Bridge protocol tests use fake Lua snapshots, never the game or a model API."""
import copy
import asyncio
import json
import socket
import sys
import tempfile
import unittest
import urllib.request
import contextlib
import io
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mcp_server.game_bridge import GameBridge, BridgeError


TOKEN = "offline-test-token-" * 3


def state():
    return {"world": {"day": 1, "phase": "day"},
            "player": {"guid": 1, "position": {"x": 0, "z": 0},
                       "vitals": {"health": 150, "hunger": 100, "hunger_max": 150},
                       "inventory": {"items": [], "equipped": []}}, "nearby": [],
            "bridge": {"enabled": True, "executor_busy": False,
                       "available_actions": {"wait": {"kind": "wait"},
                                             "collect_grass": {"kind": "collect", "guid": 20, "prefab": "grass"}}}}


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.now = 0.0
        self.bridge = GameBridge(TOKEN, clock=lambda: self.now)
        self.current = state()
        self.seq = 0
        self.exchange()

    def exchange(self, report=None, session="world-one"):
        self.seq += 1
        return self.bridge.exchange({"token": TOKEN, "session_id": session, "sequence": self.seq,
                                     "state": self.current, "action_report": report})

    def start(self, choice="wait"):
        observation = self.bridge.get_available_actions()["observation_id"]
        return self.bridge.start_action(choice, observation)

    def heartbeat(self, control=None, report=None, **extra):
        self.seq += 1
        return self.bridge.exchange({"token": TOKEN, "session_id": "world-one", "sequence": self.seq,
            "kind": "heartbeat", "control": control or {}, "action_report": report, **extra})

    def test_heartbeats_keep_connection_without_freshening_or_overwriting_facts(self):
        version = self.bridge.get_state()["state_version"]
        for tick in range(1, 13):
            self.now = tick
            self.heartbeat({"sampled_at": tick, "executor_busy": False,
                            "available_actions": {}, "player": {"guid": 99}})
        packet = self.bridge.get_available_actions()
        self.assertEqual(packet["state_version"], version)
        self.assertEqual(packet["transport_sequence"], self.seq)
        self.assertEqual(packet["state_age_ms"], 12000)
        self.assertEqual(packet["connection_age_ms"], 0)
        self.assertEqual(packet["state"]["player"]["guid"], 1)
        self.assertIn("collect_grass", packet["actions"])
        with self.assertRaises(BridgeError):
            self.bridge.start_action("collect_grass", packet["observation_id"])
        self.now += 4
        with self.assertRaises(BridgeError): self.bridge.get_state()

    def test_heartbeat_delivers_command_cancel_and_terminal_ack_without_full_state(self):
        aid = self.start()["action_id"]
        self.assertEqual(self.heartbeat()["command"]["action_id"], aid)
        self.heartbeat({"executor_busy": True}, {"action_id": aid, "status": "running"})
        self.bridge.cancel_action(aid)
        self.assertEqual(self.heartbeat()["command"]["operation"], "cancel")
        self.heartbeat({"executor_busy": False}, {"action_id": aid, "status": "cancelled"})
        self.assertEqual(self.bridge.get_action_status(aid)["status"], "cancelled")
        self.assertFalse(self.bridge.get_available_actions()["busy"])
        self.assertEqual(self.bridge.state_version, 1)

    def test_refresh_requires_a_post_request_snapshot_not_another_heartbeat(self):
        with ThreadPoolExecutor(max_workers=1) as worker:
            future = worker.submit(self.bridge.get_state, True, timeout=1)
            deadline = time.monotonic()+0.5
            while self.bridge.snapshot_request == 0 and time.monotonic() < deadline:
                time.sleep(0.001)
            requested = self.bridge.snapshot_request
            self.assertEqual(requested, 1)
            result = self.heartbeat(snapshot_request_id=requested)
            self.assertEqual(result["snapshot_request_id"], requested)
            self.assertFalse(future.done())
            self.current["player"]["vitals"]["health"] = 50
            self.seq += 1
            self.bridge.exchange({"token": TOKEN, "session_id": "world-one", "sequence": self.seq,
                "kind": "snapshot", "state": self.current, "snapshot_request_id": requested})
            packet = future.result(timeout=1)
        self.assertEqual(packet["state"]["player"]["vitals"]["health"], 50)
        self.assertEqual(packet["state_version"], self.seq)

    def test_refresh_timeout_does_not_pass_cached_state_off_as_new(self):
        with self.assertRaisesRegex(BridgeError, "fresh Lua snapshot"):
            self.bridge.get_available_actions(refresh=True, timeout=0.01)
        self.assertEqual(self.bridge.state_version, 1)
        self.assertEqual(self.bridge.snapshot_request, 1)

    def test_new_server_requests_resync_instead_of_using_heartbeat_as_initial_state(self):
        bridge = GameBridge(TOKEN)
        response = bridge.exchange({"token": TOKEN, "session_id": "world-one", "sequence": 10,
                                    "kind": "heartbeat", "control": {}})
        self.assertTrue(response["need_snapshot"])
        with self.assertRaises(BridgeError): bridge.get_state()
        self.assertEqual(bridge.state_version, 0)

    def test_heartbeat_cannot_replace_state_and_replays_still_fail(self):
        with self.assertRaises(BridgeError): self.heartbeat(state=self.current)
        with self.assertRaises(BridgeError):
            self.bridge.exchange({"token": TOKEN, "session_id": "world-one", "sequence": 1,
                                  "kind": "heartbeat", "control": {}})

    def test_full_lifecycle_dedup_cancel_ack_and_read_is_not_mutable(self):
        value = self.bridge.get_state()
        value["state"]["world"]["phase"] = "night"
        self.assertEqual(self.bridge.get_state()["state"]["world"]["phase"], "day")
        record = self.start()
        aid = record["action_id"]
        self.assertEqual(record["status"], "queued")
        self.assertEqual(self.exchange()["command"]["action_id"], aid)
        self.assertEqual(self.exchange()["command"]["action_id"], aid)
        with self.assertRaises(BridgeError): self.start()
        self.exchange({"action_id": aid, "status": "running"})
        self.assertIsNone(self.exchange()["command"])
        self.assertEqual(self.bridge.cancel_action(aid)["status"], "cancel_requested")
        self.assertEqual(self.exchange()["command"]["operation"], "cancel")
        self.exchange({"action_id": aid, "status": "cancelled"})
        self.assertEqual(self.bridge.get_action_status(aid)["status"], "cancelled")
        # A delayed start acknowledgement cannot revive a terminal action.
        self.exchange({"action_id": aid, "status": "running"})
        self.assertEqual(self.bridge.get_action_status(aid)["status"], "cancelled")

    def test_game_changes_queue_original_intent_for_lua_validation(self):
        for change in ("phase", "health", "target", "unavailable"):
            self.setUp()
            observation = self.bridge.get_available_actions()["observation_id"]
            if change == "phase": self.current["world"]["phase"] = "night"
            elif change == "health": self.current["player"]["vitals"]["health"] = 50
            elif change == "target": self.current["bridge"]["available_actions"]["collect_grass"]["guid"] = 21
            else: self.current["bridge"]["available_actions"].pop("collect_grass")
            self.exchange()
            with self.subTest(change=change):
                result = self.bridge.start_action("collect_grass", observation)
                self.assertEqual(result["status"], "queued")
                self.assertEqual(result["action"]["guid"], 20)
                command = self.exchange()["command"]
                self.assertEqual(command["action"]["guid"], 20)
                self.exchange({"action_id": result["action_id"], "status": "failed", "reason": "target_changed"})
                self.assertEqual(self.bridge.get_action_status(result["action_id"])["reason"], "target_changed")

    def test_expiry_player_ownership_and_unoffered_actions_still_rejected(self):
        for change in ("expiry", "player", "unoffered"):
            self.setUp()
            observation = self.bridge.get_available_actions()["observation_id"]
            if change == "expiry": self.now = 11
            elif change == "player": self.current["player"]["guid"] = 99
            self.exchange()
            with self.subTest(change=change), self.assertRaises(BridgeError):
                self.bridge.start_action("craft_torch" if change == "unoffered" else "collect_grass", observation)
        self.now += 4
        with self.assertRaises(BridgeError): self.bridge.get_state()

    def test_normal_polling_does_not_evict_an_unexpired_source_observation(self):
        observation = self.bridge.get_available_actions()["observation_id"]
        for _ in range(40):
            self.now += 0.2
            self.exchange()
            self.bridge.get_available_actions()
        self.assertEqual(self.bridge.start_action("collect_grass", observation)["action"]["guid"], 20)

    def test_board_sync_ignores_damage_but_keeps_player_ownership(self):
        observation = self.bridge.get_available_actions()["observation_id"]
        self.current["player"]["vitals"]["health"] = 50
        self.current["world"]["phase"] = "night"
        self.exchange()
        board = {"goal": "Get grass", "tasks": [{"id": "grass", "title": "Get grass", "priority": 1,
                 "depends_on": [], "target": {"type": "inventory", "prefab": "cutgrass", "count": 2}}]}
        self.assertTrue(self.bridge.submit_task_board(board, observation, True)["auto_execute"])
        self.current["player"]["guid"] = 99
        self.exchange()
        with self.assertRaises(BridgeError): self.bridge.submit_task_board(board, observation, False)

    def test_bad_token_replay_and_other_live_game_are_rejected(self):
        for patch in ({"token": "wrong"}, {"sequence": 1}, {"session_id": "world-two"}):
            payload = {"token": TOKEN, "sequence": 2, "session_id": "world-one", "state": self.current}
            payload.update(patch)
            with self.subTest(patch=list(patch)), self.assertRaises((PermissionError, BridgeError)):
                self.bridge.exchange(payload)

    def test_world_restart_invalidates_actions_observations_and_old_callbacks(self):
        record = self.start()
        old = self.bridge.get_available_actions()["observation_id"]
        self.now = 4
        self.exchange(session="world-two")
        self.assertEqual(self.bridge.get_action_status(record["action_id"])["status"], "interrupted")
        self.assertIsNone(self.bridge.get_available_actions()["action_status"])
        with self.assertRaises(BridgeError): self.bridge.start_action("wait", old)
        self.now = 8
        with self.assertRaises(BridgeError): self.exchange(session="world-one")

    def test_lua_busy_and_auto_board_share_single_executor(self):
        self.current["bridge"]["executor_busy"] = True
        self.exchange()
        with self.assertRaises(BridgeError): self.start()
        self.current["bridge"]["executor_busy"] = False
        self.exchange()
        observation = self.bridge.get_available_actions()["observation_id"]
        board = {"goal": "Get grass", "tasks": [{"id": "grass", "title": "Get grass", "priority": 1,
                   "depends_on": [], "target": {"type": "inventory", "prefab": "cutgrass", "count": 2}}]}
        result = self.bridge.submit_task_board(board, observation, True)
        self.assertTrue(result["auto_execute"])
        with self.assertRaises(BridgeError): self.start()
        self.assertTrue(self.exchange()["task_board"]["auto_execute"])
        self.bridge.submit_task_board(board, observation, False)
        self.start()

    def test_dispatch_expiry_is_not_reported_as_success(self):
        record = self.start()
        self.now = 6
        self.exchange()
        self.assertEqual(self.bridge.get_action_status(record["action_id"])["status"], "failed")

    def test_dst_empty_table_codec_is_supported_without_inventing_actions(self):
        self.current["bridge"]["available_actions"] = []
        self.exchange()
        self.assertEqual(self.bridge.get_available_actions()["actions"], {})

    def test_atomic_lifecycle_and_labelled_clock_domain_timings(self):
        aid = self.start()["action_id"]
        self.now = 0.2
        self.exchange()
        self.now = 0.4
        self.exchange({"action_id": aid, "status": "running", "started_at": 10,
                       "confirmed_started_at": 10.1})
        self.now = 1.3
        self.current["bridge"]["sampled_at"] = 12
        self.exchange({"action_id": aid, "status": "completed", "started_at": 10,
                       "confirmed_started_at": 10.1, "finished_at": 11.5,
                       "previous_action_id": "previous", "handoff_gap_ms": 400})
        self.now = 1.5
        packet = self.bridge.get_available_actions()
        status = packet["action_status"]
        self.assertEqual(status["action_id"], aid)
        self.assertEqual(status["status"], "completed")
        self.assertFalse(packet["busy"])
        self.assertEqual(packet["state_age_ms"], 200)
        details = status["timing"]
        self.assertEqual(details["queue_to_delivery_ms"], 200)
        self.assertEqual(details["delivery_to_running_report_ms"], 200)
        self.assertEqual(details["terminal_cache_ms"], 200)
        self.assertEqual(details["lua_finish_to_exchange_ms"], 500)
        self.assertEqual(details["lua_handoff_gap_ms"], 400)
        self.assertFalse(any(k.startswith("_") for k in status))
        # Duplicated reports do not move the first terminal receive timestamp.
        self.exchange({"action_id": aid, "status": "completed", "finished_at": 999})
        self.now = 2
        self.assertEqual(self.bridge.get_action_status(aid)["timing"]["terminal_cache_ms"], 700)

    def test_nonfinite_or_missing_native_timestamps_stay_unknown(self):
        aid = self.start()["action_id"]
        self.exchange({"action_id": aid, "status": "completed", "started_at": float("nan"),
                       "finished_at": -1, "confirmed_started_at": True})
        details = self.bridge.get_action_status(aid)["timing"]
        self.assertNotIn("lua_started_at", details)
        self.assertNotIn("lua_confirmed_started_at", details)
        self.assertNotIn("lua_finish_to_exchange_ms", details)


try:
    import mcp
except ImportError:
    mcp = None


@unittest.skipUnless(mcp, "Optional: install requirements-mcp.txt to test the real MCP SDK")
class MCPIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_sdk_refresh_is_nonblocking_and_waits_for_a_requested_full_snapshot(self):
        from mcp import Client
        from mcp_server.mcp_server import create_server
        bridge = GameBridge(TOKEN)
        bridge.exchange({"token": TOKEN, "session_id": "offline", "sequence": 1, "state": state()})
        async with Client(create_server(bridge), read_timeout_seconds=3) as client:
            refresh = asyncio.create_task(client.call_tool("get_state", {"refresh": True}))
            deadline = time.monotonic()+1
            while bridge.snapshot_request == 0 and time.monotonic() < deadline:
                await asyncio.sleep(0.001)
            self.assertEqual(bridge.snapshot_request, 1)
            # Other tools still run while the refresh worker waits for Lua.
            cached = await client.call_tool("get_available_actions", {})
            self.assertFalse(cached.is_error)
            heartbeat = bridge.exchange({"token": TOKEN, "session_id": "offline", "sequence": 2,
                                         "kind": "heartbeat", "control": {}})
            self.assertEqual(heartbeat["snapshot_request_id"], 1)
            self.assertFalse(refresh.done())
            current = state(); current["player"]["vitals"]["health"] = 50
            bridge.exchange({"token": TOKEN, "session_id": "offline", "sequence": 3,
                             "kind": "snapshot", "state": current, "snapshot_request_id": 1})
            result = await asyncio.wait_for(refresh, 2)
            self.assertFalse(result.is_error)
            self.assertEqual(result.structured_content["state_version"], 3)
            self.assertEqual(result.structured_content["state"]["player"]["vitals"]["health"], 50)

    async def test_probe_is_read_only_unless_a_choice_is_explicit(self):
        from mcp_agent.mcp_probe import probe
        calls = []
        class Session:
            async def __aenter__(self): return self
            async def __aexit__(self, *_): pass
            async def call_tool(self, name, arguments):
                calls.append(name)
                snapshot = state()
                snapshot["bridge"]["connected"] = True
                values = {"get_state": {"state": snapshot, "session_id": "fake"},
                          "get_available_actions": {"observation_id": "fake", "actions": {"wait": {}}},
                          "start_action": {"action_id": "fake", "status": "completed"}}
                return SimpleNamespace(is_error=False, structured_content=values[name])
        args = SimpleNamespace(config=Path("unused.json"), port=0, timeout=1, execute_choice=None)
        with patch.object(mcp, "Client", return_value=Session()), contextlib.redirect_stdout(io.StringIO()) as output:
            await probe(args)
            self.assertNotIn("start_action", calls)
            self.assertIn("read_only: no game action sent", output.getvalue())
            args.execute_choice = "wait"
            await probe(args)
            self.assertEqual(calls.count("start_action"), 1)

    async def test_real_protocol_discovery_state_and_action(self):
        from mcp import Client
        from mcp_server.mcp_server import create_server
        bridge = GameBridge(TOKEN)
        bridge.exchange({"token": TOKEN, "session_id": "offline", "sequence": 1, "state": state()})
        async with Client(create_server(bridge), read_timeout_seconds=3) as client:
            result = await client.list_tools()
            self.assertEqual({t.name for t in result.tools}, {"get_state", "get_available_actions", "start_action",
                "get_action_status", "cancel_action", "submit_task_board"})
            response = await client.call_tool("get_available_actions", {})
            self.assertFalse(response.is_error)
            data = response.structured_content
            self.assertEqual(data["state"]["bridge"]["available_actions"], data["actions"])
            self.assertEqual(data["state_version"], 1)
            result = await client.call_tool("start_action", {"choice": "wait", "observation_id": data["observation_id"]})
            self.assertFalse(result.is_error)
            self.assertEqual(result.structured_content["status"], "queued")

    async def test_autonomous_host_uses_real_sdk_with_simulated_native_completion(self):
        from mcp import Client
        from mcp_server.mcp_server import create_server
        from mcp_agent.mcp_agent import MCPAgent
        snapshot = state()
        snapshot["bridge"].update(protocol=4, events=[])
        bridge = GameBridge(TOKEN)
        bridge.exchange({"token": TOKEN, "session_id": "offline", "sequence": 1, "state": snapshot})
        def decide(s, criteria, plan):
            self.assertEqual(s["bridge"]["available_actions"], snapshot["bridge"]["available_actions"])
            self.assertIn("collect_grass", criteria)
            return {"answer": {"choice": "collect_grass", "confidence": 0.1}, "response": {"model": "fake"}}
        async with Client(create_server(bridge), read_timeout_seconds=3) as client:
            agent = MCPAgent(client, decide, execute=True, poll=0.01)
            async def lua_peer():
                seq = 1
                while not bridge.active:
                    await asyncio.sleep(0.01)
                aid = bridge.active
                snapshot["player"]["inventory"]["items"] = [{"prefab": "cutgrass", "count": 1}]
                seq += 1
                bridge.exchange({"token": TOKEN, "session_id": "offline", "sequence": seq,
                    "state": snapshot, "action_report": {"action_id": aid,
                        "status": "completed", "reason": "observed_effect"}})
            peer = asyncio.create_task(lua_peer())
            try:
                await asyncio.wait_for(agent.cycle(), 3)
                await peer
                self.assertIsNone(agent.active_id)
                self.assertEqual(next(iter(bridge.actions.values()))["status"], "completed")
            finally:
                peer.cancel()
                await agent.shutdown()

    async def test_real_stdio_and_loopback_http_with_simulated_lua(self):
        from mcp import Client, StdioServerParameters
        root = Path(__file__).resolve().parent.parent
        with tempfile.TemporaryDirectory(dir=root) as folder:
            config = Path(folder) / "bridge.json"
            config.write_text(json.dumps({"token": TOKEN}), encoding="utf8")
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            parameters = StdioServerParameters(command=sys.executable, args=[str(root / "mcp_server" / "mcp_server.py"),
                "--config", str(config), "--port", str(port)], cwd=str(root))
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            seq = 0
            def exchange(report=None, token=TOKEN):
                nonlocal seq
                seq += 1
                request = urllib.request.Request(f"http://127.0.0.1:{port}/v1/exchange",
                    data=json.dumps({"token": token, "session_id": "fake-game", "sequence": seq,
                        "state": state(), "action_report": report}).encode(),
                    headers={"Content-Type": "application/json"})
                with opener.open(request, timeout=3) as response:
                    return json.load(response)
            async with Client(parameters, read_timeout_seconds=5) as client:
                stale = await client.call_tool("get_state", {})
                self.assertTrue(stale.is_error)
                await asyncio.to_thread(exchange)
                actions = await client.call_tool("get_available_actions", {})
                result = await client.call_tool("start_action", {"choice": "wait",
                    "observation_id": actions.structured_content["observation_id"]})
                aid = result.structured_content["action_id"]
                delivered = await asyncio.to_thread(exchange)
                self.assertEqual(delivered["command"]["action_id"], aid)
                await asyncio.to_thread(exchange, {"action_id": aid, "status": "running"})
                cancelled = await client.call_tool("cancel_action", {"action_id": aid})
                self.assertEqual(cancelled.structured_content["status"], "cancel_requested")
                delivered = await asyncio.to_thread(exchange)
                self.assertEqual(delivered["command"]["operation"], "cancel")
                await asyncio.to_thread(exchange, {"action_id": aid, "status": "cancelled"})
                result = await client.call_tool("get_action_status", {"action_id": aid})
                self.assertEqual(result.structured_content["status"], "cancelled")
                with self.assertRaises(urllib.error.HTTPError) as error:
                    await asyncio.to_thread(exchange, None, "wrong")
                self.assertEqual(error.exception.code, 403)
