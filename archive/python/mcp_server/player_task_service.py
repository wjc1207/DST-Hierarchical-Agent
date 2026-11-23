"""Own archived player task boards and run the execution loop inside the player MCP process."""
import asyncio
from contextlib import asynccontextmanager, suppress
from contextvars import ContextVar
import hashlib
import json
import sys
from typing import Any
import uuid

from common import autonomy, timing_log
from mcp_agent.compact_tasks import expand_tasks
from mcp_agent.priority_board import merge_goals, idle_review_reason, task_signature, PriorityBoardExecutor
from mcp_server.player_bridge import BridgeError


internal_call = ContextVar("internal_game_tool", default=False)


class LocalTools:
    """Invoke the same registered tool handlers without a second MCP connection."""

    def __init__(self, server):
        self.server = server

    async def call_tool(self, name, arguments):
        from mcp.server.mcpserver.exceptions import ToolError
        token = internal_call.set(True)
        try:
            return await self.server.call_tool(name, arguments)
        except ToolError as exc:
            from types import SimpleNamespace
            return SimpleNamespace(is_error=True, content=[SimpleNamespace(text=str(exc))])
        finally:
            internal_call.reset(token)


class TaskService:
    def __init__(self, bridge, *, companion=False, poll=.2, timing_path=None):
        if companion:
            raise ValueError("Archived player task service supports companion=False only")
        self.bridge = bridge
        self.companion = False
        self.poll = poll
        self.timing_path = timing_path
        self.host = None
        self.worker = None
        self.server = None

    @asynccontextmanager
    async def lifespan(self, server):
        self.server = server
        self.worker = asyncio.create_task(self.run())
        try:
            yield
        finally:
            from anyio import CancelScope
            with CancelScope(shield=True):
                self.worker.cancel()
                with suppress(asyncio.CancelledError):
                    await self.worker
            self.worker = None

    def make_host(self):
        client = LocalTools(self.server)
        from mcp_agent.player_agent import MCPAgent
        host = MCPAgent(client, execute=True, poll=self.poll)
        host.external_board = True
        return host

    async def run(self):
        from mcp_agent.player_agent import SessionChanged
        self.host = self.make_host()
        last_error = None
        cycle = 0
        try:
            while True:
                try:
                    cycle += 1
                    path = self.timing_path
                    if path and (self.host.plan or self.host.active_id):
                        with timing_log.CycleTrace(cycle, path, emit_console=False):
                            await self.host.cycle()
                    elif self.host.plan or self.host.active_id:
                        await self.host.cycle()
                    last_error = None
                except SessionChanged:
                    self.host.execute = False
                    await self.close_host()
                    self.host = self.make_host()
                except Exception as exc:
                    if str(exc) != last_error:
                        last_error = str(exc)
                        print(f"task_service_cycle_error: {last_error}", file=sys.stderr)
                await asyncio.sleep(self.poll)
        finally:
            with suppress(Exception):
                await asyncio.wait_for(self.close_host(), timeout=2)
            self.host = None

    async def close_host(self):
        await self.host.shutdown()

    def actor(self, buddy_id=None):
        if self.host is None:
            raise BridgeError("Task service is starting")
        if buddy_id is not None:
            raise BridgeError("Player board does not take a buddy_id")
        return self.host, self.bridge.get_available_actions()

    def current(self, actor, packet):
        state = packet["state"]
        if (actor.session != packet["session_id"] or actor.player_guid != state["player"].get("guid")
                or actor.autonomy_revision != autonomy.revision(state) or not actor.plan):
            return None
        return actor.goals.view(actor.plan, state)

    def prepare(self, actor, packet, raw, previous, *, replace=False):
        from mcp_agent.player_planner import validate_player_board, planning_context
        state = packet["state"]
        rows = raw.get("tasks")
        if not isinstance(rows, list):
            raise BridgeError("tasks must be a list")
        if state.get("bridge", {}).get("protocol", 0) < 4:
            raise BridgeError("Task scheduling requires player Mod protocol 4")
        if not autonomy.enabled(state):
            raise BridgeError("Enable character control in the game UI")
        if state["player"].get("vitals", {}).get("dead"):
            raise BridgeError("Player is dead")
        context = {**planning_context(state, []), "current_task_board": previous}
        clean = validate_player_board(raw, state["world"]["day"], context, map_seen=True)
        return (clean if replace else merge_goals(previous, clean)), None

    def commit(self, actor, packet, board, _runtime):
        state = packet["state"]
        actor.session, actor.player_guid = packet["session_id"], state["player"].get("guid")
        revision = autonomy.revision(state)
        if revision != actor.autonomy_revision:
            from mcp_agent.navigation_intents import ExplorationExecutor
            actor.goals = PriorityBoardExecutor()
            actor.navigator = ExplorationExecutor()
        actor.autonomy_revision = revision
        if board:
            actor.goals.adopt(board)
        else:
            actor.goals.pause_accounting()
        actor.plan = board

    def check_manual_action(self, buddy_id=None):
        if internal_call.get() or self.host is None:
            return
        actor, packet = self.actor(buddy_id)
        board = self.current(actor, packet)
        if (board and board.get("status") != "completed") or actor.active_id:
            raise BridgeError("Scheduler owns this actor. Wait for its action and complete or delete its tasks before manual control.")

    def get(self, buddy_id=None):
        actor, packet = self.actor(buddy_id)
        board = self.current(actor, packet)
        active = actor.active_id
        reason = idle_review_reason(board)
        if board and not reason:
            reason = actor.goals.reason
        return {"task_board": board, "active_action_id": active, "review_reason": reason or None}

    def planning_state(self):
        with self.bridge.lock:
            actor, packet = self.actor(None)
            state = packet["state"]
            control = state.get("autonomy") or {}
            board = self.current(actor, packet)
            basis = [packet["session_id"], state["player"].get("guid"),
                     control.get("revision"), control.get("dialogue_revision"), False]
            intent_version = hashlib.sha256(json.dumps(basis).encode()).hexdigest()
            basis.append((board or {}).get("board_id"))
            version = hashlib.sha256(json.dumps(basis).encode()).hexdigest()
            active = actor.goal_actions.get(actor.active_id)
            reason = idle_review_reason(board)
            if board and not reason and not active:
                if actor.goals.reason not in {"", "waiting_for_start", "residence_holding", "maintenance_holding",
                                                "refuel_settling", "charcoal_burning", "search_budget_cooldown"}:
                    reason = actor.goals.reason
            members = {
                "player": {
                    "interaction": control, "observation": state, "current_task_board": board,
                    "ready": state.get("ready", True), "version": version, "intent_version": intent_version,
                    "active_task_id": active[0] if active else None,
                    "review_reason": reason,
                }
            }
            return {"members": members, "interaction": {}}

    def check_versions(self, versions, identities):
        if versions is None:
            return
        snapshot = self.planning_state()
        current = snapshot["members"]
        if set(versions) != set(identities) or any(
                identity not in current or current[identity]["version"] != version
                for identity, version in versions.items()):
            raise BridgeError("Planning state changed; read get_planning_state again")

    def reply(self, text, version, outcome, buddy_id, message_id):
        with self.bridge.lock:
            actor, packet = self.actor(buddy_id)
            self.check_versions({"player": version}, ["player"])
            state = packet["state"]
            control = state.get("autonomy") or {}
            board = self.current(actor, packet)
            if outcome == "completed" and control.get("mode") == "autonomous":
                raise BridgeError("Autonomous mode continues after milestones; send an ordinary reply")
            if outcome == "completed" and any(t["status"] != "done" for t in (board or {}).get("tasks", [])):
                raise BridgeError("Cannot complete while accepted tasks remain unfinished")
            finish = control.get("mode") == "instruction" and (
                outcome == "completed" or outcome == "blocked" and (not board or not board.get("execution_task_ids")))
            return self.bridge.send_agent_feedback(text, control["revision"], message_id, finish,
                                                   control.get("dialogue_revision"))

    def stop_changed_action(self, actor, packet, board):
        active = actor.goal_actions.get(actor.active_id)
        if not active:
            return
        task_id = active[0]
        if task_id is None:
            return
        old = next((task for task in actor.plan["tasks"] if task["id"] == task_id), None) if actor.plan else None
        new = next((task for task in board["tasks"] if task["id"] == task_id), None) if board else None
        if old and new and all(old.get(key) == new.get(key) for key in ("priority", "focus", "depends_on")) and task_signature(old) == task_signature(new):
            return
        self.bridge.cancel_action(actor.active_id)

    def submit(self, raw, buddy_id=None, observation_id=None, versions=None):
        with self.bridge.lock:
            if observation_id is not None:
                self.bridge.validate_board_observation(observation_id)
            actor, packet = self.actor(buddy_id)
            if not isinstance(raw, dict) or "tasks" not in raw or raw.get("kind", "task_board") != "task_board":
                raise BridgeError("Board must contain tasks")
            previous = self.current(actor, packet)
            if not raw.get("goal"):
                raw = {**raw, "goal": (previous or {}).get("goal") or "MCP tasks"}
            board, runtime = self.prepare(actor, packet, raw, previous)
            self.check_versions(versions, ["player"])
            self.stop_changed_action(actor, packet, board)
            board["board_id"] = uuid.uuid4().hex
            self.commit(actor, packet, board, runtime)
            state = self.planning_state()["members"]
            return {"accepted": True, "boards": {"player": board}, "versions": {"player": state["player"]["version"]}}

    def add(self, task, buddy_id=None):
        with self.bridge.lock:
            actor, packet = self.actor(buddy_id)
            previous = self.current(actor, packet)
            rows = (previous or {}).get("tasks", [])
            if task.get("id") in {row["id"] for row in rows}:
                raise BridgeError("Task ID already exists; use update_task")
            task = expand_tasks([task], rows)[0]
            required = set(task["depends_on"]) if isinstance(task.get("depends_on"), list) and all(
                isinstance(item, str) for item in task["depends_on"]) else set()
            prerequisites = [row for row in rows if row["id"] in required]
            while True:
                required |= {item for row in prerequisites for item in row["depends_on"]}
                closure = [row for row in rows if row["id"] in required]
                if len(closure) == len(prerequisites):
                    break
                prerequisites = closure
            return self.submit({"tasks": [*closure, task]}, buddy_id)

    def edit(self, task_id, buddy_id=None, changes=None):
        with self.bridge.lock:
            actor, packet = self.actor(buddy_id)
            previous = self.current(actor, packet)
            if not previous:
                raise BridgeError("No task board")
            old = next((task for task in previous["tasks"] if task["id"] == task_id), None)
            if old is None:
                raise BridgeError("Unknown task ID")
            if changes is None:
                dependents = [task["id"] for task in previous["tasks"] if task_id in task["depends_on"]]
                if dependents:
                    raise BridgeError("Task is required by " + ", ".join(dependents))
                rows = [task for task in previous["tasks"] if task["id"] != task_id]
            else:
                from mcp_agent.compact_tasks import TASK_OPTIONS
                if {"id", "target"}.intersection(changes):
                    raise BridgeError("Keep the task ID and pass target fields alongside other fields")
                row = {key: value for key, value in old.items() if key in TASK_OPTIONS}
                target = old["target"] if changes.get("type", old["target"]["type"]) == old["target"]["type"] else {}
                row = {**row, **target, **changes, "id": task_id}
                expanded = expand_tasks([row], previous["tasks"])
                rows = [expanded[0] if task["id"] == task_id else task for task in previous["tasks"]]
            if rows:
                raw = {"goal": previous["goal"], "tasks": rows}
                if changes is None:
                    raw["cancel_task_ids"] = [task_id]
                if previous.get("camp_site"):
                    raw["camp_site"] = previous["camp_site"]
                board, runtime = self.prepare(actor, packet, raw, previous, replace=True)
            else:
                board, runtime = None, None
            self.stop_changed_action(actor, packet, board)
            if board:
                board["board_id"] = uuid.uuid4().hex
            self.commit(actor, packet, board, runtime)
            return {"task_id": task_id, **self.get(buddy_id)}

    def register(self, server):
        from mcp.server.mcpserver.exceptions import ToolError

        def invoke(operation, *args):
            try:
                return operation(*args)
            except ValueError as exc:
                raise ToolError(str(exc)) from None

        @server.tool()
        async def submit_task_board(board: dict, observation_id: str | None = None,
                                    auto_execute: bool = True, buddy_id: str | None = None,
                                    versions: dict[str, str] | None = None) -> dict[str, Any]:
            if internal_call.get():
                if auto_execute:
                    raise ToolError("Automatic task boards run in Python")
                return invoke(self.bridge.submit_task_board, board, observation_id, False)
            if not auto_execute:
                raise ToolError("This tool starts Python task execution. Use delete_task to remove a task.")
            return invoke(self.submit, board, buddy_id, observation_id, versions)

        @server.tool()
        async def get_planning_state() -> dict[str, Any]:
            return invoke(self.planning_state)

        @server.tool()
        async def send_planning_reply(text: str, version: str, outcome: str = "reply",
                                      buddy_id: str | None = None, message_id: str | None = None) -> dict[str, Any]:
            if outcome not in {"reply", "blocked", "completed"}:
                raise ToolError("Invalid reply outcome")
            return invoke(self.reply, text, version, outcome, buddy_id, message_id or uuid.uuid4().hex)

        @server.tool()
        async def get_task_board(buddy_id: str | None = None) -> dict[str, Any]:
            return invoke(self.get, buddy_id)

        @server.tool()
        async def submit_task(task: dict, buddy_id: str | None = None) -> dict[str, Any]:
            return invoke(self.add, task, buddy_id)

        @server.tool()
        async def update_task(task_id: str, changes: dict, buddy_id: str | None = None) -> dict[str, Any]:
            return invoke(self.edit, task_id, buddy_id, changes)

        @server.tool()
        async def delete_task(task_id: str, buddy_id: str | None = None) -> dict[str, Any]:
            return invoke(self.edit, task_id, buddy_id)
