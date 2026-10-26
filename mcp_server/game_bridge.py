"""Loopback-only game bridge. No key simulation, log tailing or model calls."""
from __future__ import annotations

import copy
import hmac
import json
import math
import os
import secrets
import socket
import threading
import time
from collections import OrderedDict
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from common.task_board import validate_board, board_view, validate_navigation_targets
from .transport_metrics import TransportRecorder, lua_measurements

TERMINAL = {"completed", "failed", "interrupted", "cancelled"}
SUPPORTED = {"wait", "collect", "craft_torch", "craft_axe", "craft_pickaxe",
             "equip_torch", "unequip_torch", "eat_safe_food", "cook_food",
             "build_campfire", "build_sciencemachine",
             "chop_nearest_tree", "mine_nearest_rock", "explore", "flee_from_nearest_hostile"}


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
            "before starting mcp_agent/mcp_agent.py. No existing process was stopped."
        )


class ExclusiveBridgeHTTPServer(ThreadingHTTPServer):
    # Windows SO_REUSEADDR permits two live listeners, silently splitting their
    # GameBridge state. Unix REUSEADDR permits prompt restart after TIME_WAIT;
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


def signature(state):
    """Observation ownership only. Live game feasibility belongs to Lua."""
    return state.get("player", {}).get("guid")


def finite_time(value):
    return type(value) in {int, float} and math.isfinite(value) and 0 <= value < 1e12


class GameBridge:
    def __init__(self, token: str, *, clock=time.monotonic, max_age=3.0):
        if not isinstance(token, str) or len(token) < 32:
            raise ValueError("Bridge token must contain at least 32 characters")
        self.token, self.clock, self.max_age = token, clock, max_age
        self.server_id = secrets.token_hex(12)
        self.lock = threading.RLock()
        self.session = None
        self.retired = set()
        self.sequence = 0
        self.state = None
        self.received = -float("inf")
        self.snapshot_received = -float("inf")
        self.state_version = 0
        self.snapshot_request = 0
        self.snapshot_fulfilled = 0
        self.changed = threading.Condition(self.lock)
        self.actions = OrderedDict()
        self.observations = OrderedDict()
        self.active = None
        self.board = None
        self.board_revision = 0

    def _fresh(self):
        if self.state is None or self.clock() - self.received > self.max_age:
            raise BridgeError("Game is disconnected or state is stale; no command sent")

    def exchange(self, payload, *, measurements=None):
        lock_started = time.perf_counter() if measurements is not None else None
        with self.lock:
            if measurements is not None:
                measurements["lock_wait_ms"] = (time.perf_counter()-lock_started)*1000
            if not isinstance(payload, dict) or not isinstance(payload.get("token"), str) \
                    or not hmac.compare_digest(payload["token"], self.token):
                raise PermissionError("Invalid bridge token")
            session, seq, state = payload.get("session_id"), payload.get("sequence"), payload.get("state")
            kind = payload.get("kind", "snapshot")
            control = payload.get("control")
            # DST's bundled JSON codec serializes every empty Lua table as [].
            if isinstance(state, dict) and isinstance(state.get("bridge"), dict) \
                    and state["bridge"].get("available_actions") == []:
                state = copy.deepcopy(state)
                state["bridge"]["available_actions"] = {}
            if (not isinstance(session, str) or not 1 <= len(session) <= 160
                    or type(seq) is not int or seq <= 0 or kind not in ("snapshot", "heartbeat")
                    or (kind == "snapshot" and (not isinstance(state, dict)
                        or not isinstance(state.get("player"), dict)
                        or not isinstance(state.get("bridge", {}).get("available_actions"), dict)))
                    or (kind == "heartbeat" and ("state" in payload or not isinstance(control, dict)))):
                raise BridgeError("Invalid exchange envelope")
            if session != self.session:
                if session in self.retired or (self.session and self.clock() - self.received <= self.max_age):
                    raise BridgeError("Another game session owns this bridge")
                if self.session:
                    self.retired.add(self.session)
                if self.active:
                    self.actions[self.active].update(status="interrupted", reason="game_session_changed")
                self.session, self.sequence, self.active = session, 0, None
                self.board, self.observations = None, OrderedDict()
                self.state, self.state_version = None, 0
                self.snapshot_received = -float("inf")
                self.snapshot_request, self.snapshot_fulfilled = 0, 0
                self.changed.notify_all()
            if seq <= self.sequence:
                raise BridgeError("Out-of-order exchange")
            self.sequence, self.received = seq, self.clock()
            if kind == "snapshot":
                copy_started = time.perf_counter() if measurements is not None else None
                self.state = copy.deepcopy(state)
                self.state_version, self.snapshot_received = seq, self.received
                self.state["bridge"]["snapshot_sampled_at"] = state["bridge"].get("sampled_at")
                requested = payload.get("snapshot_request_id")
                if type(requested) is int and 0 <= requested <= self.snapshot_request:
                    self.snapshot_fulfilled = max(self.snapshot_fulfilled, requested)
                if measurements is not None:
                    measurements["state_copy_ms"] = (time.perf_counter()-copy_started)*1000
                self.changed.notify_all()
            elif self.state is not None:
                # Heartbeats update CONTROL only. They do not freshen world,
                # vitals, inventory, candidates or the full-snapshot version.
                for key in ("connected", "executor_busy", "board_revision", "board_auto_execute",
                            "board_ready", "board_paused", "sampled_at", "action_report", "transport"):
                    if key in control:
                        self.state["bridge"][key] = copy.deepcopy(control[key])
            report = payload.get("action_report")
            if isinstance(report, dict) and report.get("action_id") in self.actions:
                record = self.actions[report["action_id"]]
                status = report.get("status")
                if (record["session_id"] == session and record["status"] not in TERMINAL
                        and status in TERMINAL | {"running"}):
                    # A still-running heartbeat precedes delivery/ack of our stop.
                    # Keep cancellation pending so this exchange returns cancel,
                    # rather than erasing it and waiting for natural completion.
                    cancelling = record["status"] == "cancel_requested" and status == "running"
                    if not cancelling:
                        record.update(status=status, reason=str(report.get("reason", ""))[:240])
                    if status == "running":
                        record.setdefault("_running_at", self.received)
                    else:
                        record.setdefault("_terminal_at", self.received)
                    details = record.setdefault("timing", {})
                    for key in ("started_at", "confirmed_started_at", "finished_at", "handoff_gap_ms"):
                        if finite_time(report.get(key)):
                            details["lua_" + key] = report[key]
                    for key in ("previous_action_id", "start_source"):
                        if isinstance(report.get(key), str):
                            details[key] = report[key][:160]
                    sampled = (self.state or {}).get("bridge", {}).get("sampled_at")
                    finished = details.get("lua_finished_at")
                    if finite_time(sampled) and finite_time(finished) and sampled >= finished:
                        details.setdefault("lua_finish_to_exchange_ms", round((sampled - finished) * 1000, 3))
                    if status in TERMINAL and self.active == record["action_id"]:
                        self.active = None
            command = None
            if self.active:
                record = self.actions[self.active]
                if record["status"] == "queued" and self.clock() > record["expires"]:
                    record.update(status="failed", reason="dispatch_expired")
                    self.active = None
                elif record["status"] in {"queued", "cancel_requested"}:
                    if record["status"] == "queued":
                        record.setdefault("_delivered_at", self.clock())
                    command = {"action_id": record["action_id"], "session_id": session,
                               "operation": "cancel" if record["status"] == "cancel_requested" else "start",
                               "choice": record["choice"], "action": record["action"]}
            result = {"server_id": self.server_id, "session_id": session, "sequence": seq, "command": command,
                      "board_revision": self.board_revision, "snapshot_request_id": self.snapshot_request,
                      "need_snapshot": self.state is None}
            if kind == "snapshot" or control.get("board_revision") != self.board_revision:
                result["task_board"] = copy.deepcopy(self.board)
            return result

    def _refresh_snapshot(self, timeout=3.0):
        """Ask Lua for a post-request snapshot; heartbeats cannot satisfy this."""
        self._fresh()
        session = self.session
        if self.snapshot_request <= self.snapshot_fulfilled:
            self.snapshot_request += 1
        requested = self.snapshot_request
        deadline = time.monotonic() + timeout
        while self.snapshot_fulfilled < requested:
            if self.session != session:
                raise BridgeError("Game session changed while requesting a snapshot")
            self._fresh()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise BridgeError("Timed out requesting a fresh Lua snapshot")
            self.changed.wait(min(remaining, 0.25))
        if self.session != session:
            raise BridgeError("Game session changed while requesting a snapshot")

    def _ages(self):
        return {"state_version": self.state_version, "transport_sequence": self.sequence,
                "state_age_ms": round((self.clock()-self.snapshot_received)*1000, 3),
                "connection_age_ms": round((self.clock()-self.received)*1000, 3)}

    def get_state(self, refresh=False, *, timeout=3.0):
        with self.lock:
            self._fresh()
            if type(refresh) is not bool:
                raise BridgeError("refresh must be a boolean")
            if refresh: self._refresh_snapshot(timeout)
            return {"session_id": self.session, **self._ages(),
                    "age_seconds": round(self.clock() - self.snapshot_received, 3), "state": copy.deepcopy(self.state)}

    def get_available_actions(self, refresh=False, *, timeout=3.0):
        with self.lock:
            self._fresh()
            if type(refresh) is not bool:
                raise BridgeError("refresh must be a boolean")
            if refresh: self._refresh_snapshot(timeout)
            observation = secrets.token_hex(12)
            available = {k: v for k, v in self.state["bridge"]["available_actions"].items()
                         if isinstance(v, dict) and v.get("kind") in SUPPORTED}
            self.observations[observation] = (self.session, self.snapshot_received, signature(self.state), copy.deepcopy(available))
            # Retain the source token across normal host polling during a model
            # request. TTL still expires after 10 seconds, independently of polls.
            while len(self.observations) > 128:
                self.observations.popitem(last=False)
            last_action = self.active or next((aid for aid, record in reversed(self.actions.items())
                                               if record["session_id"] == self.session), None)
            return {"session_id": self.session, "observation_id": observation,
                    **self._ages(), "state": copy.deepcopy(self.state),
                    "action_status": self.get_action_status(last_action) if last_action else None,
                    "actions": copy.deepcopy(available), "busy": bool(self.active or
                        self.state["bridge"].get("executor_busy") or (self.board and self.board.get("auto_execute"))),
                    "task_board": board_view(self.board, self.state) if self.board else None}

    def start_action(self, choice: str, observation_id: str, navigation_targets: list | None = None):
        with self.lock:
            self._fresh()
            if self.active or (self.board and self.board.get("auto_execute")) \
                    or self.state.get("bridge", {}).get("executor_busy"):
                raise BridgeError("Executor busy; cancel its action or pause its board first")
            observation = self.observations.get(observation_id)
            if not observation or observation[0] != self.session or self.clock() - observation[1] > 10:
                raise BridgeError("Observation expired; get available actions again")
            if observation[2] != signature(self.state):
                raise BridgeError("Game player changed; get available actions again")
            old = observation[3].get(choice)
            if not old:
                raise BridgeError("Action was not offered by this observation")
            action = copy.deepcopy(old)
            if navigation_targets is not None:
                if old.get("kind") != "explore":
                    raise BridgeError("Navigation targets apply only to offered navigation actions")
                action["navigation_targets"] = validate_navigation_targets(navigation_targets)
            action_id = secrets.token_hex(12)
            record = {"action_id": action_id, "session_id": self.session, "choice": choice,
                      "action": action, "status": "queued", "reason": "",
                      "expires": self.clock() + 5, "_queued_at": self.clock()}
            self.actions[action_id] = record
            self.active = action_id
            while len(self.actions) > 256:
                self.actions.popitem(last=False)
            return self.get_action_status(action_id)

    def get_action_status(self, action_id: str):
        with self.lock:
            if action_id not in self.actions:
                raise BridgeError("Unknown action ID")
            record = copy.deepcopy(self.actions[action_id])
            record.pop("expires", None)
            details = record.setdefault("timing", {})
            for name, first, last in (("queue_to_delivery_ms", "_queued_at", "_delivered_at"),
                                      ("delivery_to_running_report_ms", "_delivered_at", "_running_at")):
                if first in record and last in record:
                    details[name] = round((record[last] - record[first]) * 1000, 3)
            if "_terminal_at" in record:
                details["terminal_cache_ms"] = round((self.clock() - record["_terminal_at"]) * 1000, 3)
            for key in list(record):
                if key.startswith("_"):
                    del record[key]
            record["connected"] = record["session_id"] == self.session and self.clock() - self.received <= self.max_age
            return record

    def cancel_action(self, action_id: str):
        with self.lock:
            if action_id not in self.actions:
                raise BridgeError("Unknown action ID")
            record = self.actions[action_id]
            if record["status"] == "queued":
                # Nothing has been delivered unless a previous exchange sent it.
                # Always wait for Lua's stop acknowledgement; never claim success.
                record["status"] = "cancel_requested"
            elif record["status"] not in TERMINAL:
                record["status"] = "cancel_requested"
            return self.get_action_status(action_id)

    def submit_task_board(self, board: dict, observation_id: str, auto_execute: bool = False):
        with self.lock:
            if type(auto_execute) is not bool:
                raise BridgeError("auto_execute must be a boolean")
            self._fresh()
            observation = self.observations.get(observation_id)
            if (not observation or observation[0] != self.session or self.clock() - observation[1] > 10
                    or observation[2] != signature(self.state)):
                raise BridgeError("Observation expired or game player changed")
            if self.active:
                raise BridgeError("Cancel the active action before replacing the board")
            clean = validate_board(board, int(self.state.get("world", {}).get("day", 1)))
            self.board_revision += 1
            self.board = {**clean, "board_id": f"mcp:{self.board_revision}", "auto_execute": bool(auto_execute)}
            return {"board_revision": self.board_revision, "auto_execute": bool(auto_execute),
                    "task_board": board_view(self.board, self.state)}


def serve_bridge(bridge: GameBridge, port=8765, *, metrics_path: Path | None = None):
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
            stages, payload, authenticated, size = {}, None, False, 0
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
                stages["body_read_ms"] = (time.perf_counter()-at)*1000
                at = time.perf_counter()
                payload = json.loads(raw, parse_constant=invalid_constant)
                stages["json_decode_ms"] = (time.perf_counter()-at)*1000
                at = time.perf_counter()
                result, status = bridge.exchange(payload, measurements=stages), 200
                stages["exchange_ms"] = (time.perf_counter()-at)*1000
                authenticated = True
            except PermissionError:
                result, status = {"error": "Unauthorized"}, 403
            except (ValueError, TypeError, KeyError, OSError):
                result, status = {"error": "Invalid or conflicting exchange"}, 400
            at = time.perf_counter()
            encoded = json.dumps(result, ensure_ascii=True, allow_nan=False).encode()
            stages["json_encode_ms"] = (time.perf_counter()-at)*1000
            at = time.perf_counter()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.send_header("Cache-Control", "no-store")
            # Keep the original one-exchange-per-connection lifetime. Enabling
            # 100 Continue must not silently introduce idle keep-alive threads.
            self.send_header("Connection", "close")
            self.close_connection = True
            self.end_headers()
            stages["headers_write_ms"] = (time.perf_counter()-at)*1000
            at = time.perf_counter()
            written = False
            try:
                self.wfile.write(encoded)
                written = True
            finally:
                stages["body_write_ms"] = (time.perf_counter()-at)*1000
                record = {"kind": "http_exchange", "server_id": bridge.server_id,
                          "timestamp_utc": stamp, "status": status, "response_written": written,
                          "request_bytes": size, "response_bytes": len(encoded),
                          "server_total_ms": (time.perf_counter()-began)*1000,
                          "stages_ms": stages, "http": self.http_metadata()}
                if authenticated:
                    report = payload.get("action_report")
                    command = result.get("command")
                    record.update(session_id=payload["session_id"], sequence=payload["sequence"],
                        message_kind=payload.get("kind", "snapshot"),
                        board_revision=result["board_revision"],
                        reported_action_id=report.get("action_id") if isinstance(report, dict) else None,
                        response_action_id=command.get("action_id") if isinstance(command, dict) else None,
                        lua=lua_measurements(payload.get("measurement"), payload["sequence"]))
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
