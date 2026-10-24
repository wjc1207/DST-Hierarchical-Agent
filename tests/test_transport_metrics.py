"""Content-free timing tests use loopback fake game messages only."""
import json
import math
import contextlib
import socket
import time
import unittest
import urllib.error
import urllib.request
from types import SimpleNamespace
from unittest.mock import Mock, patch

from mcp_server.game_bridge import GameBridge, serve_bridge
from mcp_server.transport_metrics import TransportRecorder, lua_measurements
from test_game_bridge import TOKEN, state


class TransportTests(unittest.TestCase):
    @contextlib.contextmanager
    def fake_http_peer(self):
        records = []
        bridge = GameBridge(TOKEN)
        with patch("mcp_server.game_bridge.TransportRecorder", return_value=SimpleNamespace(write=records.append)):
            server = serve_bridge(bridge, 0)
        try:
            yield bridge, server, records
        finally:
            server.shutdown()
            server.server_close()

    def response_head(self, stream):
        status = stream.readline().decode("ascii").strip()
        headers = {}
        for _ in range(100):
            line = stream.readline()
            if line == b"\r\n":
                return status, headers
            self.assertTrue(line, "response ended before its headers")
            key, value = line.decode("ascii").split(":", 1)
            headers[key.lower()] = value.strip()
        self.fail("too many response headers")

    def await_records(self, records, count):
        deadline = time.monotonic()+2
        while len(records) < count and time.monotonic() < deadline:
            time.sleep(0.001)
        self.assertEqual(len(records), count)

    def test_expect_continue_arrives_before_client_sends_body(self):
        # Emulate QueryServer/curl waiting for 100 before uploading. A client
        # that eagerly sends its body (urllib) cannot catch this regression.
        with self.fake_http_peer() as (bridge, server, records):
            body = json.dumps({"token": TOKEN, "session_id": "offline", "sequence": 1,
                               "state": state()}).encode()
            with socket.create_connection(server.server_address, timeout=0.75) as peer:
                with peer.makefile("rb") as stream:
                    peer.sendall(("POST /v1/exchange HTTP/1.1\r\nHost: localhost\r\n"
                                  "Expect: 100-continue\r\nContent-Length: " + str(len(body)) +
                                  "\r\n\r\n").encode())
                    status, _ = self.response_head(stream)
                    self.assertEqual(status, "HTTP/1.1 100 Continue")
                    self.assertIsNone(bridge.session)
                    peer.sendall(body)
                    status, headers = self.response_head(stream)
                    self.assertEqual(status, "HTTP/1.1 200 OK")
                    self.assertEqual(json.loads(stream.read(int(headers["content-length"])))
                                     ["sequence"], 1)
                    self.assertEqual(headers["connection"], "close")
                    self.assertEqual(stream.read(1), b"")
            self.await_records(records, 1)
            self.assertEqual(records[0]["http"], {
                "request_version": "HTTP/1.1", "response_version": "HTTP/1.1",
                "expect": "100-continue", "continue_sent": True})

    def test_expect_rejects_bad_framing_and_endpoint_before_upload(self):
        cases = [
            ("POST /v1/exchange", "Content-Length: 0", 400),
            ("POST /v1/exchange", "Content-Length: 2000001", 400),
            ("POST /v1/exchange", "Content-Length: nope", 400),
            ("POST /v1/exchange", "Content-Length: 3\r\nContent-Length: 4", 400),
            ("POST /v1/exchange", "Content-Length: 3\r\nTransfer-Encoding: chunked", 400),
            ("POST /v1/exchange", "", 400),
            ("POST /wrong", "Content-Length: 3", 404),
            ("GET /v1/exchange", "Content-Length: 3", 405),
        ]
        with self.fake_http_peer() as (bridge, server, _):
            for request, framing, expected in cases:
                with self.subTest(request=request, framing=framing):
                    with socket.create_connection(server.server_address, timeout=0.75) as peer:
                        with peer.makefile("rb") as stream:
                            peer.sendall((request + " HTTP/1.1\r\nHost: localhost\r\n"
                                          "Expect: 100-continue\r\n" + framing + "\r\n\r\n").encode())
                            status, headers = self.response_head(stream)
                            self.assertEqual(int(status.split()[1]), expected)
                            self.assertEqual(headers["connection"], "close")
                            stream.read(int(headers["content-length"]))
            self.assertIsNone(bridge.session)

    def test_http10_http11_and_untrusted_headers_remain_compatible_and_private(self):
        with self.fake_http_peer() as (_, server, records):
            for sequence, version in enumerate(("HTTP/1.0", "HTTP/1.1"), 1):
                with self.subTest(version=version):
                    body = json.dumps({"token": TOKEN, "session_id": "offline",
                                       "sequence": sequence, "state": state()}).encode()
                    with socket.create_connection(server.server_address, timeout=2) as peer:
                        with peer.makefile("rb") as stream:
                            peer.sendall((f"POST /v1/exchange {version}\r\nHost: localhost\r\n"
                                "Connection: keep-alive\r\nAuthorization: private-header-value\r\n"
                                "Cookie: private-header-value\r\nX-Anything: private-header-value\r\n"
                                "Expect: private-header-value\r\nContent-Length: " + str(len(body)) +
                                "\r\n\r\n").encode() + body)
                            status, headers = self.response_head(stream)
                            self.assertEqual(status, "HTTP/1.1 200 OK")
                            stream.read(int(headers["content-length"]))
                            self.assertEqual(headers["connection"], "close")
                            self.assertEqual(stream.read(1), b"")
                    self.await_records(records, sequence)
                    self.assertEqual(records[-1]["http"]["request_version"], version)
                    self.assertEqual(records[-1]["http"]["expect"], "other")
                    self.assertFalse(records[-1]["http"]["continue_sent"])
                    self.assertNotIn("private-header-value", json.dumps(records[-1]))
                    self.assertNotIn(TOKEN, json.dumps(records[-1]))

    def test_continue_does_not_bypass_body_authentication(self):
        with self.fake_http_peer() as (bridge, server, records):
            body = json.dumps({"token": "wrong", "session_id": "offline", "sequence": 1,
                               "state": state()}).encode()
            with socket.create_connection(server.server_address, timeout=2) as peer:
                with peer.makefile("rb") as stream:
                    peer.sendall(("POST /v1/exchange HTTP/1.1\r\nHost: localhost\r\n"
                                  "Expect: 100-continue\r\nContent-Length: " + str(len(body)) +
                                  "\r\n\r\n").encode())
                    status, _ = self.response_head(stream)
                    self.assertEqual(status, "HTTP/1.1 100 Continue")
                    peer.sendall(body)
                    status, headers = self.response_head(stream)
                    self.assertEqual(status, "HTTP/1.1 403 Forbidden")
                    stream.read(int(headers["content-length"]))
            self.assertIsNone(bridge.session)
            self.await_records(records, 1)
            self.assertTrue(records[0]["http"]["continue_sent"])
            self.assertNotIn("session_id", records[0])
            self.assertNotIn("lua", records[0])

    def test_exclusive_listener_can_restart_after_a_completed_http_exchange(self):
        bridge = GameBridge(TOKEN)
        server = serve_bridge(bridge, 0)
        port = server.server_port
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            request = urllib.request.Request(f"http://127.0.0.1:{port}/v1/exchange",
                data=json.dumps({"token": TOKEN, "session_id": "offline", "sequence": 1,
                                 "state": state()}).encode(),
                headers={"Content-Type": "application/json"})
            with opener.open(request, timeout=3) as response:
                self.assertEqual(json.load(response)["sequence"], 1)
        finally:
            server.shutdown()
            server.server_close()
        replacement = serve_bridge(GameBridge(TOKEN), port)
        replacement.shutdown()
        replacement.server_close()

    def test_ambiguous_framing_is_also_rejected_without_expect(self):
        with self.fake_http_peer() as (bridge, server, records):
            with socket.create_connection(server.server_address, timeout=2) as peer:
                with peer.makefile("rb") as stream:
                    peer.sendall(b"POST /v1/exchange HTTP/1.1\r\nHost: localhost\r\n"
                                 b"Content-Length: 3\r\nContent-Length: 4\r\n\r\n")
                    status, headers = self.response_head(stream)
                    self.assertEqual(status, "HTTP/1.1 400 Bad Request")
                    self.assertEqual(headers["connection"], "close")
                    stream.read(int(headers["content-length"]))
            self.assertIsNone(bridge.session)
            self.await_records(records, 1)
            self.assertEqual(records[0]["http"]["expect"], "none")
            self.assertFalse(records[0]["http"]["continue_sent"])

    def test_lua_fields_are_bounded_numeric_and_content_free(self):
        value = {"clock": "lua_real_seconds", "token": "do-not-log", "prompt": "do-not-log",
            "sampling": {"ticks": 3, "sum_ms": {"navigation_ms": 2, "token": 123,
                                                 "state_total_ms": math.nan}, "max_ms": []},
            "previous_exchanges": [{"sequence": 1, "encode_ms": 2, "decode_ms": -3,
                                    "token": "do-not-log", "action_id": "a", "accepted": True},
                                   {"sequence": 99, "encode_ms": 2}]}
        result = lua_measurements(value, 2)
        self.assertEqual(result["sampling"]["sum_ms"], {"navigation_ms": 2})
        self.assertEqual(len(result["previous_exchanges"]), 1)
        self.assertNotIn("decode_ms", result["previous_exchanges"][0])
        self.assertNotIn("token", json.dumps(result))
        self.assertNotIn("do-not-log", json.dumps(result))
        self.assertEqual(lua_measurements([], 1), {})
        self.assertEqual(lua_measurements({"sampling": [], "previous_exchanges": []}, 1)["clock"], "unavailable")

    def test_measurement_log_failure_never_raises_or_repeats_warning(self):
        path = Mock()
        path.parent.mkdir.side_effect = OSError("blocked")
        recorder = TransportRecorder(path)
        with patch("builtins.print") as printed:
            recorder.write({"server_total_ms": 1})
            recorder.write({"server_total_ms": 2})
        self.assertEqual(printed.call_count, 1)

    def test_real_http_phases_correlate_sequence_and_omit_bodies_and_bad_token_data(self):
        records = []
        bridge = GameBridge(TOKEN)
        with patch("mcp_server.game_bridge.TransportRecorder", return_value=SimpleNamespace(write=records.append)):
            server = serve_bridge(bridge, 0)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        def post(payload):
            req = urllib.request.Request(f"http://127.0.0.1:{server.server_port}/v1/exchange",
                data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
            with opener.open(req, timeout=3) as response:
                return json.load(response)
        def await_records(count):
            deadline = time.monotonic()+2
            while len(records) < count and time.monotonic() < deadline:
                time.sleep(0.001)
            self.assertEqual(len(records), count)
        try:
            payload = {"token": TOKEN, "session_id": "offline", "sequence": 2, "state": state(),
                "measurement": {"clock": "lua_real_seconds", "token": TOKEN,
                    "sampling": {"ticks": 1, "sum_ms": {"navigation_ms": 1}},
                    "previous_exchanges": [{"sequence": 1, "encode_ms": 0.2}]}}
            result = post(payload)
            self.assertEqual(result["sequence"], 2)
            await_records(1)
            record = records[0]
            self.assertEqual(record["session_id"], "offline")
            self.assertEqual(record["sequence"], 2)
            self.assertTrue(record["response_written"])
            self.assertEqual(record["lua"]["previous_exchanges"][0]["sequence"], 1)
            self.assertNotIn(TOKEN, json.dumps(record))
            self.assertNotIn("player", json.dumps(record))
            for key in ("body_read_ms", "json_decode_ms", "lock_wait_ms", "state_copy_ms", "exchange_ms",
                        "json_encode_ms", "headers_write_ms", "body_write_ms"):
                self.assertGreaterEqual(record["stages_ms"][key], 0)
                self.assertLessEqual(record["stages_ms"][key], record["server_total_ms"])
            payload["token"] = "wrong"
            with self.assertRaises(urllib.error.HTTPError) as error:
                post(payload)
            self.assertEqual(error.exception.code, 403)
            await_records(2)
            self.assertNotIn("lua", records[1])
            self.assertNotIn("session_id", records[1])
            post({"token": TOKEN, "session_id": "offline", "sequence": 3, "kind": "heartbeat",
                  "control": {"board_revision": 0}, "snapshot_reason": "private-header-value"})
            await_records(3)
            self.assertEqual(records[2]["message_kind"], "heartbeat")
            self.assertNotIn("state_copy_ms", records[2]["stages_ms"])
            self.assertNotIn("snapshot_reason", records[2])
            self.assertEqual(bridge.get_state()["state_version"], 2)
            self.assertLess(records[2]["request_bytes"], records[0]["request_bytes"])
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
