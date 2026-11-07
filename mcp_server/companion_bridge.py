"""Per-computer companion MCP bridge. The game server never receives model keys."""
import copy
import hmac
import math
import re
import secrets
import threading
import time
from collections import OrderedDict

from common import autonomy
from common.companion_transfer import COUNTED_TARGETS, guid, query_storage
from common.places import query_places
from mcp_server.game_bridge import BridgeError

TERMINAL = {"completed", "failed", "interrupted"}
OPS = {"summon", "bind", "follow", "stay", "stop", "move", "collect", "recipes", "equip", "unequip", "work", "craft", "place", "search", "visit_building"} | COUNTED_TARGETS


class CompanionBridge:
    def __init__(self, token, *, clock=time.monotonic):
        if not isinstance(token, str) or len(token) < 32:
            raise ValueError("Local token must have at least 32 characters")
        self.token, self.clock = token, clock
        self.server_id = secrets.token_hex(12)
        self.lock = threading.RLock()
        self.session = None
        self.sequence = 0
        self.received = -math.inf
        self.state = None
        self.actions, self.observations, self.feedback = OrderedDict(), OrderedDict(), OrderedDict()
        self.active = None

    def _fresh(self):
        if self.state is None or self.clock() - self.received > 2:
            raise BridgeError("Companion client disconnected/stale")

    def _basis(self):
        control = self.state.get("control") or {}
        companion = self.state.get("companion") or {}
        return self.session, companion.get("token"), control.get("revision")

    def exchange(self, payload, measurements=None):
        with self.lock:
            if not isinstance(payload.get("token"), str) or not hmac.compare_digest(payload["token"], self.token):
                raise PermissionError("Unauthorized")
            session, sequence, state = payload.get("session_id"), payload.get("sequence"), payload.get("state")
            if (not isinstance(session, str) or not 1 <= len(session) <= 100
                    or type(sequence) is not int or sequence < 1 or not isinstance(state, dict)):
                raise BridgeError("Invalid companion envelope")
            control = autonomy.settings({"autonomy": state.get("control")})
            if (control is None or control.get("mode") not in {"instruction", "autonomous"}
                    or (control["mode"] == "autonomous" and not control["goal"])
                    or type(state.get("ready")) is not bool):
                raise BridgeError("Invalid companion intent")
            remote = state.get("companion")
            age = state.get("snapshot_age_ms")
            if remote is not None and not isinstance(remote, dict):
                raise BridgeError("Invalid companion snapshot")
            if age is not None and (type(age) not in (int, float) or not math.isfinite(age) or age < 0):
                raise BridgeError("Invalid companion snapshot age")
            if state["ready"] and (not remote or not remote.get("bound") or not isinstance(remote.get("token"), str)
                                    or remote.get("revision") != control["revision"] or age is None):
                raise BridgeError("Companion snapshot does not match player intent")
            results = payload.get("results") or []
            if not isinstance(results, list) or len(results) > 32:
                raise BridgeError("Invalid result list")
            if session != self.session:
                if self.session is not None and self.clock() - self.received < 3:
                    raise BridgeError("Another local client owns this companion bridge")
                for record in self.actions.values():
                    if record["status"] not in TERMINAL:
                        record.update(status="interrupted", reason="client_session_changed")
                self.session, self.sequence, self.active = session, 0, None
                self.observations.clear()
                self.feedback.clear()
            if sequence <= self.sequence:
                raise BridgeError("Out-of-order companion exchange")
            old_basis = self._basis() if self.state else None
            self.state = copy.deepcopy(state)
            companion = self.state.get("companion") or {}
            if isinstance(companion.get("buddy"), dict) and companion["buddy"].get("inventory") == []:
                companion["buddy"]["inventory"] = {}
            if companion.get("recipes") == []:
                companion["recipes"] = {}
            self.sequence, self.received = sequence, self.clock()
            basis = self._basis()
            if old_basis != basis:
                successful_binds = {r.get("id") for r in results if isinstance(r, dict) and r.get("status") == "completed"}
                for record in self.actions.values():
                    expected_bind = record["op"] in {"bind", "summon"} and record["id"] in successful_binds and record["session_id"] == session
                    if record["status"] not in TERMINAL and not expected_bind:
                        record.update(status="interrupted", reason="player_intent_changed")
                if self.active and self.actions[self.active]["status"] in TERMINAL:
                    self.active = None
                self.observations.clear()
            for result in results:
                if not isinstance(result, dict):
                    continue
                record = self.actions.get(result.get("id"))
                if (record and record["status"] not in TERMINAL and result.get("revision") == record["revision"]
                        and result.get("status") in TERMINAL | {"running"}):
                    record.update(status=result["status"], reason=str(result.get("reason", ""))[:200])
                    if isinstance(result.get("data"), dict):
                        record["data"] = copy.deepcopy(result["data"])
                    if record["status"] in TERMINAL and self.active == record["id"]:
                        self.active = None
            command = None
            if self.active:
                record = self.actions[self.active]
                if record["status"] == "queued" and self.clock() - record["at"] > 5:
                    record.update(status="failed", reason="dispatch_expired")
                    self.active = None
                elif record["status"] == "queued":
                    command = {k: record[k] for k in ("id", "op", "args", "token", "revision")}
            self.feedback = OrderedDict((key, value) for key, value in self.feedback.items() if value["revision"] == basis[2])
            return {"server_id": self.server_id, "session_id": session, "sequence": sequence,
                    "board_revision": 0, "command": command, "feedback": list(self.feedback.values())}

    def observe(self):
        with self.lock:
            self._fresh()
            identity = secrets.token_hex(12)
            self.observations[identity] = (self.clock(), self._basis())
            while len(self.observations) > 64:
                self.observations.popitem(last=False)
            return {"observation_id": identity, "session_id": self.session, "state": copy.deepcopy(self.state),
                    "connection_age_ms": (self.clock() - self.received) * 1000}

    def action(self, op, observation_id, args=None):
        with self.lock:
            self._fresh()
            observation = self.observations.get(observation_id)
            if not observation or self.clock() - observation[0] > 3 or observation[1] != self._basis():
                raise BridgeError("Expired observation or changed companion intent")
            if op not in OPS:
                raise BridgeError("Unsupported companion operation")
            if self.active:
                if op == "stop":
                    self.actions[self.active].update(status="interrupted", reason="cancel_requested")
                    self.active = None
                else:
                    raise BridgeError("Companion executor busy; stop or change intent first")
            if op not in {"bind", "summon"} and not self.state.get("ready"):
                raise BridgeError("Companion snapshot not ready; bind and await intent synchronization")
            if op not in {"bind", "summon"} and (self.state.get("snapshot_age_ms", math.inf) + (self.clock()-self.received)*1000 > 2000):
                raise BridgeError("Companion world snapshot stale")
            args = copy.deepcopy(args or {})
            if not isinstance(args, dict):
                raise BridgeError("Invalid action arguments")
            snapshot = self.state.get("companion") or {}
            if op in {"recipes", "equip", "unequip", "work", "craft", "place", "search", "visit_building"} and op not in snapshot.get("capabilities", []):
                raise BridgeError("Companion capability unavailable; update the server mod")
            if op == "visit_building":
                if (set(args) != {"building_id"} or not isinstance(args.get("building_id"), str)
                        or not any(r.get("id") == args["building_id"] and r.get("status") not in {"missing", "unavailable"}
                                   for r in (snapshot.get("places") or {}).get("buildings", []))):
                    raise BridgeError("Visit requires a remembered available building ID")
            if op in COUNTED_TARGETS:
                capability = "storage" if op in {"deposit", "withdraw"} else op
                expected = {"prefab", "total_count", "task_key"} | ({"container_guid"} if capability == "storage" else set())
                if capability not in snapshot.get("capabilities", []):
                    raise BridgeError("Companion capability unavailable; update the server mod")
                if (set(args) != expected or not isinstance(args.get("task_key"), str)
                        or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", args["task_key"])
                        or not isinstance(args.get("prefab"), str) or not re.fullmatch(r"[a-z0-9_]{1,60}", args["prefab"])
                        or type(args.get("total_count")) is not int or not 1 <= args["total_count"] <= 100):
                    raise BridgeError("Invalid transfer arguments")
                if capability == "storage":
                    guid(args["container_guid"])
                    if not any(r["guid"] == args["container_guid"] and r.get("contents_known") for r in snapshot.get("containers", [])):
                        raise BridgeError("Chest was not offered or is busy")
            if op == "recipes":
                from common.recipes import query_recipes
                query_recipes({}, **args)  # Reuse the shared read-only query contract.
            if op == "move":
                if any(type(args.get(key)) not in (int, float) or not math.isfinite(args[key]) for key in ("x", "z")):
                    raise BridgeError("move requires finite x/z")
            if op == "search":
                points = (snapshot.get("perception") or {}).get("points", [])
                if not any(r.get("id") == args.get("point_id") and r.get("x") == args.get("x") and r.get("z") == args.get("z") for r in points):
                    raise BridgeError("Search requires a currently offered point")
                products = args.get("products")
                if (not isinstance(products, list) or not 1 <= len(products) <= 6
                        or any(not isinstance(p, str) or not re.fullmatch(r"[a-z0-9_]{1,60}", p) for p in products)
                        or type(args.get("timeout_seconds")) not in (int, float) or not .2 <= args["timeout_seconds"] <= 8):
                    raise BridgeError("Invalid search resource targets or deadline")
            if op == "collect":
                if type(args.get("target_guid")) is not int or args["target_guid"] < 1:
                    raise BridgeError("collect requires observed target_guid")
                nearby = ((self.state.get("companion") or {}).get("nearby") or [])
                if not any(row.get("guid") == args["target_guid"] for row in nearby):
                    raise BridgeError("Target was not offered in the current companion snapshot")
            if op == "work":
                if type(args.get("target_guid")) is not int or args.get("work") not in {"CHOP", "MINE"}:
                    raise BridgeError("work requires target_guid and CHOP/MINE")
                if not any(r.get("guid") == args["target_guid"] and r.get("work") == args["work"] for r in snapshot.get("work_targets", [])):
                    raise BridgeError("Work target was not offered")
            if op == "equip":
                if type(args.get("item_guid")) is not int or not any(
                        r.get("guid") == args["item_guid"] and r.get("usable") for r in snapshot.get("equipment", [])):
                    raise BridgeError("Equip requires an offered usable carried item")
            if op == "unequip":
                if (set(args) != {"item_guid"} or type(args.get("item_guid")) is not int
                        or not any(r.get("guid") == args["item_guid"] and r.get("equipped")
                                   for r in snapshot.get("equipment", []))):
                    raise BridgeError("Unequip requires a currently offered equipped item")
            if op in {"craft", "place"}:
                recipe_id = args.get("recipe_id")
                if not isinstance(recipe_id, str) or not re.fullmatch(r"[a-z0-9_]{1,100}", recipe_id):
                    raise BridgeError("Recipe ID is required")
                row = (snapshot.get("recipes") or {}).get(recipe_id, {})
                if not row.get("supported") or not row.get("ready") or bool(row.get("placement")) != (op == "place"):
                    raise BridgeError("Recipe is not currently ready for this operation")
                if op == "place" and (any(type(args.get(k)) not in (int, float) or not math.isfinite(args[k]) for k in ("x", "z"))
                                      or type(args.get("radius", 6)) not in (int, float) or not 2 <= args.get("radius", 6) <= 8):
                    raise BridgeError("place requires finite x/z and radius 2..8")
            identity = secrets.token_hex(12)
            _, token, revision = self._basis()
            record = {"id": identity, "op": op, "args": args, "token": token, "revision": revision,
                      "status": "queued", "reason": "", "at": self.clock(), "session_id": self.session}
            self.actions[identity] = record
            self.active = identity
            while len(self.actions) > 128:
                self.actions.popitem(last=False)
            return copy.deepcopy(record)

    def result(self, action_id):
        with self.lock:
            if action_id not in self.actions:
                raise BridgeError("Unknown companion action")
            return copy.deepcopy(self.actions[action_id])

    def storage(self, container_guid=None):
        with self.lock:
            self._fresh()
            snapshot = self.state.get("companion") or {}
            age = self.state.get("snapshot_age_ms", math.inf)+(self.clock()-self.received)*1000
            if not self.state.get("ready") or age > 2000 or "storage" not in snapshot.get("capabilities", []):
                raise BridgeError("Current companion storage observation unavailable")
            return query_storage(snapshot, container_guid)

    def places(self, query="", building_id=None, offset=0, limit=12):
        with self.lock:
            self._fresh()
            snapshot = self.state.get("companion") or {}
            age = self.state.get("snapshot_age_ms", math.inf)+(self.clock()-self.received)*1000
            if not self.state.get("ready") or age > 2000 or "visit_building" not in snapshot.get("capabilities", []):
                raise BridgeError("Current companion building memory unavailable")
            return query_places(snapshot.get("places"), query, building_id, offset, limit)

    def expire_query(self, action_id):
        with self.lock:
            record = self.actions.get(action_id)
            if record and record["op"] == "recipes" and record["status"] not in TERMINAL:
                record.update(status="failed", reason="recipe_query_timeout")
                if self.active == action_id:
                    self.active = None

    def reply(self, text, revision, message_id, finish=False):
        with self.lock:
            self._fresh()
            autonomy.validate_text(text)
            if (type(revision) is not int or revision != self._basis()[2] or type(finish) is not bool
                    or not isinstance(message_id, str) or not 1 <= len(message_id) <= 100):
                raise BridgeError("Invalid or obsolete companion reply")
            message = {"id": message_id, "text": text, "revision": revision, "finish": finish}
            if message_id in self.feedback and self.feedback[message_id] != message:
                raise BridgeError("Reply ID reused")
            self.feedback[message_id] = message
            while len(self.feedback) > 32:
                self.feedback.popitem(last=False)
            if finish and self.active:
                self.actions[self.active].update(status="interrupted", reason="instruction_finished")
                self.active = None
            return {"queued": True}
