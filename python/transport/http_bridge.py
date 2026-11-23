"""Loopback-only HTTP bridge server, exclusive port reservation, and shared transport errors."""
from __future__ import annotations

import json
import math
import os
import socket
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from transport.metrics import TransportRecorder, lua_measurements


class BridgeError(ValueError):
    pass


class BridgePortUnavailable(OSError):
    """Startup failure, not a disconnected game that should be polled forever."""
    def __init__(self, port):
        self.port = port
        super().__init__(
            f"bridge_port_unavailable address=127.0.0.1:{port}; "
            "the port is already in use or cannot be reserved exclusively. "
            "Stop the other DST MCP client/service (including Codex, probes or --bridge-only) "
            "before starting the DST MCP server/agent. No existing process was stopped."
        )


class ExclusiveBridgeHTTPServer(ThreadingHTTPServer):
    # Windows SO_REUSEADDR permits two live listeners, silently splitting their
    # bridge state. Unix REUSEADDR permits prompt restart after TIME_WAIT;
    # disabling REUSEPORT there still prevents concurrent listening.
    allow_reuse_address = os.name != "nt"
    allow_reuse_port = False

    def server_bind(self):
        if os.name == "nt":
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def bind_bridge_listener(port, handler):
    try:
        return ExclusiveBridgeHTTPServer(("127.0.0.1", port), handler)
    except OSError as exc:
        raise BridgePortUnavailable(port) from exc


def check_bridge_port(port):
    """Fail before launching a child. The child's final bind also enforces exclusivity."""
    listener = bind_bridge_listener(port, BaseHTTPRequestHandler)
    listener.server_close()


def finite_time(value):
    return type(value) in {int, float} and math.isfinite(value) and 0 <= value < 1e12


def serve_bridge(bridge, port=8766, *, metrics_path: Path | None = None):
    recorder = TransportRecorder(metrics_path)

    class Handler(BaseHTTPRequestHandler):
        # HTTP/1.0 (the stdlib default) skips Expect handling even when the
        # client sends HTTP/1.1. A client waiting for 100 then stalls its upload.
        protocol_version = "HTTP/1.1"

        def log_message(self, *_):
            pass  # Never put token-bearing bodies or protocol noise on MCP stdout.

        def body_length(self):
            lengths = self.headers.get_all("Content-Length", [])
            if self.headers.get_all("Transfer-Encoding") or len(lengths) != 1:
                raise BridgeError("Unsupported or ambiguous request framing")
            text = lengths[0].strip()
            if not text.isascii() or not text.isdecimal() or not 0 < int(text) <= 2_000_000:
                raise BridgeError("Invalid payload length")
            return int(text)

        def handle_expect_100(self):
            # Validate routing/framing before inviting an upload. Token
            # authentication still happens on the body; 100 is NOT authorization.
            if self.command != "POST":
                self.send_error(405)
                return False
            if self.path != "/v1/exchange":
                self.send_error(404)
                return False
            try:
                self.body_length()
            except ValueError:
                self.send_error(400)
                return False
            accepted = super().handle_expect_100()
            self._continue_sent = bool(accepted)
            return accepted

        def http_metadata(self):
            # Never copy arbitrary header values: they may contain credentials.
            expect = self.headers.get("Expect", "").lower()
            return {"request_version": self.request_version if self.request_version in
                        {"HTTP/1.0", "HTTP/1.1"} else "other",
                    "response_version": self.protocol_version,
                    "expect": "100-continue" if expect == "100-continue" else
                        "other" if self.headers.get_all("Expect") else "none",
                    "continue_sent": bool(getattr(self, "_continue_sent", False))}

        def do_POST(self):
            began = time.perf_counter()
            stamp = datetime.now(timezone.utc).isoformat()
            stages, payload, result, authenticated, size = {}, None, {}, False, 0
            if self.path != "/v1/exchange":
                self.send_error(404)
                return
            try:
                size = self.body_length()
                self.connection.settimeout(3)

                def invalid_constant(_):
                    raise ValueError("Non-finite JSON number")

                at = time.perf_counter()
                raw = self.rfile.read(size)
                stages["body_read_ms"] = (time.perf_counter() - at) * 1000
                at = time.perf_counter()
                payload = json.loads(raw, parse_constant=invalid_constant)
                stages["json_decode_ms"] = (time.perf_counter() - at) * 1000
                at = time.perf_counter()
                result, status = bridge.exchange(payload, measurements=stages), 200
                stages["exchange_ms"] = (time.perf_counter() - at) * 1000
                authenticated = True
            except PermissionError:
                result, status = {"error": "Unauthorized"}, 403
            except (ValueError, TypeError, KeyError, OSError):
                result, status = {"error": "Invalid or conflicting exchange"}, 400
            at = time.perf_counter()
            # Klei's JSON4Lua reads strings as Lua literals and cannot decode
            # JSON Unicode escapes (\uXXXX becomes literal uXXXX in Lua 5.1).
            encoded = json.dumps(result, ensure_ascii=False, allow_nan=False).encode("utf-8")
            stages["json_encode_ms"] = (time.perf_counter() - at) * 1000
            at = time.perf_counter()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.send_header("Cache-Control", "no-store")
            # Keep the original one-exchange-per-connection lifetime. Enabling
            # 100 Continue must not silently introduce idle keep-alive threads.
            self.send_header("Connection", "close")
            self.close_connection = True
            self.end_headers()
            stages["headers_write_ms"] = (time.perf_counter() - at) * 1000
            at = time.perf_counter()
            written = False
            try:
                self.wfile.write(encoded)
                written = True
            finally:
                stages["body_write_ms"] = (time.perf_counter() - at) * 1000
                record = {"kind": "http_exchange", "server_id": bridge.server_id,
                          "timestamp_utc": stamp, "status": status, "response_written": written,
                          "request_bytes": size, "response_bytes": len(encoded),
                          "server_total_ms": (time.perf_counter() - began) * 1000,
                          "stages_ms": stages, "http": self.http_metadata()}
                if authenticated:
                    report = payload.get("action_report")
                    command = result.get("command") if isinstance(result, dict) else None
                    record.update(
                        session_id=payload["session_id"],
                        sequence=payload["sequence"],
                        message_kind=payload.get("kind", "snapshot"),
                        board_revision=result.get("board_revision") if isinstance(result, dict) else None,
                        reported_action_id=report.get("action_id") if isinstance(report, dict) else None,
                        response_action_id=command.get("action_id") if isinstance(command, dict) else None,
                        lua=lua_measurements(payload.get("measurement"), payload["sequence"]),
                    )
                    if payload.get("snapshot_reason") in ("initial", "event", "requested", "resync"):
                        record["snapshot_reason"] = payload["snapshot_reason"]
                # Socket-write time is known only AFTER the response. Do not
                # serialize twice or claim a write timestamp inside that response.
                recorder.write(record)

    server = bind_bridge_listener(port, Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, name="dst-bridge", daemon=True)
    thread.start()
    return server
