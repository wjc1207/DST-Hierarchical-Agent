"""Own task boards and run the deterministic Task Executor inside the MCP process."""
import asyncio
from contextlib import asynccontextmanager, suppress
from copy import deepcopy
import hashlib
import json
from typing import Any
import uuid

from common import timing_log
from common.paths import PROJECT_ROOT
from executor.board_contract import expand_assignment, validate_plan as validate_companion
from executor.compact_tasks import TASK_OPTIONS
from executor.companion_tasks import capabilities, CompanionTasks
from executor.priority_board import idle_review_reason, task_signature
from executor.team_runner import CycleDiagnostics, DirectBridgeClient, TeamRunner
from transport.http_bridge import BridgeError

URGENT_ADVISORY_REASONS = {
    "return_at_risk",
    "return_deadline_missed",
    "thermal_emergency_freezing",
    "thermal_emergency_overheating",
    "thermal_emergency_wet",
}


class TaskService:
    def __init__(self, bridge, *, companion=True, poll=.2, timing_path=None):
        if not companion:
            raise ValueError("Unified TaskService supports companion=True only")
        self.bridge = bridge
        self.companion = True
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
        return TeamRunner(
            DirectBridgeClient(self.bridge),
            None,
            execute=True,
            log_path=PROJECT_ROOT / "logs/companion_agent.jsonl",
        )

    async def run(self):
        self.host = self.make_host()
        diagnostics = CycleDiagnostics(self.host)
        cycle = 0
        try:
            while True:
                try:
                    cycle += 1
                    path = self.timing_path
                    if path:
                        with timing_log.CycleTrace(cycle, path, emit_console=False):
                            await self.host.cycle()
                    else:
                        await self.host.cycle()
                    diagnostics.recovered()
                except Exception as exc:
                    diagnostics.error(exc)
                await asyncio.sleep(self.poll)
        finally:
            with suppress(Exception):
                await asyncio.wait_for(self.close_host(), timeout=2)
            self.host = None

    async def close_host(self):
        await self.host.close()

    def actor(self, buddy_id):
        if self.host is None:
            raise BridgeError("Task service is starting")
        team = self.bridge.observe()["state"]["members"]
        if buddy_id is None and len(team) == 1:
            buddy_id = next(iter(team))
        actor = self.host.agents.get(buddy_id)
        if buddy_id not in team or actor is None:
            raise BridgeError("Specify a currently bound buddy_id")
        return actor, self.bridge.member_call("observe", buddy_id)

    def current(self, actor, packet):
        state = packet["state"]
        snapshot = state.get("companion") or {}
        control = state["control"]
        goal = control.get("goal") if control.get("mode") == "autonomous" else None
        basis = (packet["session_id"], snapshot.get("token"), control["revision"],
                 control.get("mode") == "autonomous", goal)
        return actor.tasks.view(snapshot) if actor.basis == basis and actor.plan else None

    def prepare(self, actor, packet, raw, previous, *, replace=False):
        state = packet["state"]
        rows = raw.get("tasks")
        if not isinstance(rows, list):
            raise BridgeError("tasks must be a list")
        observed = state["companion"]
        if not state.get("ready") or (observed.get("buddy") or {}).get("dead") or not observed.get("bound"):
            raise BridgeError("Companion is not ready to execute tasks")
        context = {"observation": observed, "interaction": state["control"], "current_task_board": previous}
        if any(isinstance(row, dict) and "type" in row for row in rows):
            raw = expand_assignment(raw, context)
        clean = validate_companion(raw, context)
        runtime = deepcopy(actor.tasks) if previous else CompanionTasks()
        if replace:
            runtime.plan = clean
            runtime.goals.adopt(clean)
            board = clean
        else:
            board = runtime.adopt(clean, observed)
        return board, runtime

    def commit(self, actor, packet, board, runtime):
        state = packet["state"]
        snapshot, control = state["companion"], state["control"]
        goal = control.get("goal") if control.get("mode") == "autonomous" else None
        actor.basis = (packet["session_id"], snapshot.get("token"), control["revision"],
                       control.get("mode") == "autonomous", goal)
        actor.dialogue_revision = control.get("dialogue_revision")
        actor.tasks, actor.observed = runtime, snapshot
        actor.idle_marker = None
        actor.finished_basis = None
        actor.external_review_reason = None
        actor.waiting_at = actor.waiting_position = None
        actor.plan = board

    def check_manual_action(self, buddy_id=None):
        if self.host is None:
            return
        actor, packet = self.actor(buddy_id)
        board = self.current(actor, packet)
        if (board and board.get("status") != "completed") or actor.active:
            raise BridgeError("Scheduler owns this actor. Wait for its action and complete or delete its tasks before manual control.")

    def get(self, buddy_id):
        actor, packet = self.actor(buddy_id)
        board = self.current(actor, packet)
        active = actor.active
        reason = idle_review_reason(board)
        return {"task_board": board, "active_action_id": active[0] if active else None,
                "review_reason": reason or None}

    def planning_state(self):
        """Capture game intent, boards and their submission versions together."""
        with self.bridge.lock:
            team_state = self.bridge.observe()["state"]
            if team_state.get("paused"):
                return {"paused": True, "members": {}, "interaction": team_state["control"]}
            members = {}
            for identity in team_state["members"]:
                actor, packet = self.actor(identity)
                state = packet["state"]
                control = state["control"]
                observed = state["companion"]
                board = self.current(actor, packet)
                dead = bool((observed.get("buddy") or {}).get("dead"))
                basis = [packet["session_id"], observed.get("token"),
                         control.get("revision"), control.get("dialogue_revision"), dead]
                intent_version = hashlib.sha256(json.dumps(basis).encode()).hexdigest()
                basis.append((board or {}).get("board_id"))
                version = hashlib.sha256(json.dumps(basis).encode()).hexdigest()
                active = actor.active
                reason = idle_review_reason(board)
                if board:
                    advisory = actor.external_review_reason
                    if advisory in URGENT_ADVISORY_REASONS and reason != "all_tasks_completed":
                        reason = advisory
                    elif not reason and not active:
                        reason = advisory
                members[identity] = {
                    "interaction": control, "observation": observed, "current_task_board": board,
                    "ready": state.get("ready", True) and not dead, "version": version, "intent_version": intent_version,
                    "active_task_id": active[1] if active else None,
                    "review_reason": "buddy_dead" if dead else reason,
                    "supported_capabilities": capabilities(observed),
                }
            team = self.bridge.observe()["state"]["control"]
            return {"members": members, "interaction": team}

    def check_versions(self, versions, identities):
        if versions is None:
            return
        snapshot = self.planning_state()
        if snapshot.get("paused"):
            raise BridgeError("Game is paused")
        current = snapshot["members"]
        if set(versions) != set(identities) or any(
                identity not in current or current[identity]["version"] != version
                for identity, version in versions.items()):
            raise BridgeError("Planning state changed; read get_planning_state again")

    def reply(self, text, version, outcome, buddy_id, message_id):
        with self.bridge.lock:
            actor, packet = self.actor(buddy_id)
            identity = actor.buddy_id
            self.check_versions({identity: version}, [identity])
            state = packet["state"]
            control = state["control"]
            board = self.current(actor, packet)
            if outcome == "completed" and control.get("mode") == "autonomous":
                raise BridgeError("Autonomous mode continues after milestones; send an ordinary reply")
            if outcome == "completed" and any(t["status"] != "done" for t in (board or {}).get("tasks", [])):
                raise BridgeError("Cannot complete while accepted tasks remain unfinished")
            finish = control.get("mode") == "instruction" and (
                outcome == "completed" or outcome == "blocked" and (not board or not board.get("execution_task_ids")))
            return self.bridge.member_call("reply", identity, text, control["revision"], message_id, finish,
                                           control.get("dialogue_revision"))

    def stop_changed_action(self, actor, packet, board):
        active = actor.active
        if not active:
            return
        task_id = active[1]
        if task_id is None:
            return
        old = next((task for task in actor.plan["tasks"] if task["id"] == task_id), None) if actor.plan else None
        new = next((task for task in board["tasks"] if task["id"] == task_id), None) if board else None
        if old and new and all(old.get(key) == new.get(key) for key in ("priority", "focus", "depends_on")) and task_signature(old) == task_signature(new):
            return
        record = self.bridge.member_call("action", actor.buddy_id, "stop", packet["observation_id"])
        actor.active = (record["id"], None, "stop")
        return task_id

    def submit(self, raw, buddy_id=None, observation_id=None, versions=None):
        if "assignments" not in raw:
            assignments = [(buddy_id, raw)]
        else:
            assignments = raw["assignments"]
            if buddy_id is not None or not isinstance(assignments, dict) or not assignments:
                raise BridgeError("Use assignments keyed by buddy_id for a team batch")
            assignments = list(assignments.items())
        with self.bridge.lock:
            if observation_id is not None:
                raise BridgeError("Companion boards use buddy_id or assignments")
            staged = []
            for identity, plan in assignments:
                actor, packet = self.actor(identity)
                if not isinstance(plan, dict) or "tasks" not in plan or plan.get("kind", "task_board") != "task_board":
                    raise BridgeError("Board must contain tasks")
                previous = self.current(actor, packet)
                if not plan.get("goal"):
                    plan = {**plan, "goal": (previous or {}).get("goal") or "MCP tasks"}
                board, runtime = self.prepare(actor, packet, plan, previous)
                staged.append((actor, packet, board, runtime))
            self.check_versions(versions, [actor.buddy_id for actor, _, _, _ in staged])
            for actor, packet, board, runtime in staged:
                if self.stop_changed_action(actor, packet, board) and runtime:
                    runtime.pause_search()
            for actor, packet, board, runtime in staged:
                board["board_id"] = uuid.uuid4().hex
                self.commit(actor, packet, board, runtime)
            boards = {actor.buddy_id: board for actor, _, board, _ in staged}
            state = self.planning_state()["members"]
            return {"accepted": True, "boards": boards,
                    "versions": {identity: state[identity]["version"] for identity in boards}}

    def add(self, task, buddy_id):
        with self.bridge.lock:
            actor, packet = self.actor(buddy_id)
            previous = self.current(actor, packet)
            rows = (previous or {}).get("tasks", [])
            if task.get("id") in {row["id"] for row in rows}:
                raise BridgeError("Task ID already exists; use update_task")
            context = {"observation": packet["state"].get("companion"), "current_task_board": previous}
            task = expand_assignment({"tasks": [task]}, context)["tasks"][0]
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

    def edit(self, task_id, buddy_id, changes=None):
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
                if {"id", "target"}.intersection(changes):
                    raise BridgeError("Keep the task ID and pass target fields alongside other fields")
                row = {key: value for key, value in old.items() if key in TASK_OPTIONS}
                target = old["target"] if changes.get("type", old["target"]["type"]) == old["target"]["type"] else {}
                row = {**row, **target, **changes, "id": task_id}
                expanded = expand_assignment({"tasks": [row]}, {"observation": packet["state"]["companion"],
                            "current_task_board": previous})["tasks"]
                rows = [expanded[0] if task["id"] == task_id else task for task in previous["tasks"]]
            if rows:
                raw = {"goal": previous["goal"], "tasks": rows}
                if changes is None:
                    raw["cancel_task_ids"] = [task_id]
                if previous.get("camp_site"):
                    raw["camp_site"] = previous["camp_site"]
                board, runtime = self.prepare(actor, packet, raw, previous, replace=True)
            else:
                board, runtime = None, CompanionTasks()
            if self.stop_changed_action(actor, packet, board) and runtime:
                runtime.pause_search()
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
            """Merge a batch into the live Python board. Omitted tasks remain. Team batches use assignments keyed by buddy_id."""
            if not auto_execute:
                raise ToolError("This tool starts Python task execution. Use delete_task to remove a task.")
            return invoke(self.submit, board, buddy_id, observation_id, versions)

        @server.tool()
        async def get_planning_state() -> dict[str, Any]:
            """Read current game intent, observations, boards, review reasons and per-actor versions."""
            return invoke(self.planning_state)

        @server.tool()
        async def send_planning_reply(text: str, version: str, outcome: str = "reply",
                                      buddy_id: str | None = None, message_id: str | None = None) -> dict[str, Any]:
            """Send a version-checked game chat reply. Completed verifies all accepted tasks."""
            if outcome not in {"reply", "blocked", "completed"}:
                raise ToolError("Invalid reply outcome")
            return invoke(self.reply, text, version, outcome, buddy_id, message_id or uuid.uuid4().hex)

        @server.tool()
        async def get_task_board(buddy_id: str | None = None) -> dict[str, Any]:
            """Read the board, progress, task IDs, blocked reasons and active action."""
            return invoke(self.get, buddy_id)

        @server.tool()
        async def submit_task(task: dict, buddy_id: str | None = None) -> dict[str, Any]:
            """Add one flat task, e.g. {type:collect,prefab:cutgrass,count:4}. Count is target stock."""
            return invoke(self.add, task, buddy_id)

        @server.tool()
        async def update_task(task_id: str, changes: dict, buddy_id: str | None = None) -> dict[str, Any]:
            """Patch a task by ID, e.g. changes={count:6}. Existing dependencies and other tasks are retained."""
            return invoke(self.edit, task_id, buddy_id, changes)

        @server.tool()
        async def delete_task(task_id: str, buddy_id: str | None = None) -> dict[str, Any]:
            """Delete a task and stop its action if running. Reports any remaining dependents."""
            return invoke(self.edit, task_id, buddy_id)
