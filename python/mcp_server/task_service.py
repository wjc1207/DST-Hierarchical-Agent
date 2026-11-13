"""Own task boards and run the existing execution loops inside the MCP process."""
import asyncio
from contextlib import asynccontextmanager, suppress
from contextvars import ContextVar
from copy import deepcopy
import hashlib
import json
import sys
from types import SimpleNamespace
from typing import Any
import uuid

from common import autonomy, timing_log
from common.paths import PROJECT_ROOT
from mcp_agent.compact_tasks import expand_tasks
from mcp_agent.companion_planner import validate_plan as validate_companion
from mcp_agent.companion_team_planner import expand_assignment
from mcp_agent.companion_tasks import capabilities
from mcp_agent.priority_board import merge_goals, idle_review_reason, task_signature, PriorityBoardExecutor
from mcp_server.game_bridge import BridgeError


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
            return SimpleNamespace(is_error=True, content=[SimpleNamespace(text=str(exc))])
        finally:
            internal_call.reset(token)


class TaskService:
    def __init__(self, bridge, *, companion=False, poll=.2, timing_path=None):
        self.bridge = bridge
        self.companion = companion
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
        if self.companion:
            from mcp_agent.companion_team_agent import CompanionTeamAgent
            return CompanionTeamAgent(client, None, execute=True, log_path=PROJECT_ROOT / "logs/companion_agent.jsonl")
        from mcp_agent.mcp_agent import MCPAgent
        host = MCPAgent(client, None, execute=True, poll=self.poll)
        host.external_board = True
        return host

    async def run(self):
        from mcp_agent.mcp_agent import SessionChanged
        from mcp_agent.companion_agent import CycleDiagnostics

        self.host = self.make_host()
        diagnostics = CycleDiagnostics(self.host) if self.companion else None
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
                    elif self.companion or self.host.plan or self.host.active_id:
                        await self.host.cycle()
                    if diagnostics:
                        diagnostics.recovered()
                    last_error = None
                except SessionChanged:
                    # A new player/session cannot inherit prior task or action ownership.
                    self.host.execute = False
                    await self.close_host()
                    self.host = self.make_host()
                except Exception as exc:
                    if diagnostics:
                        diagnostics.error(exc)
                    elif str(exc) != last_error:
                        last_error = str(exc)
                        print(f"task_service_cycle_error: {last_error}", file=sys.stderr)
                await asyncio.sleep(self.poll)
        finally:
            with suppress(Exception):
                await asyncio.wait_for(self.close_host(), timeout=2)
            self.host = None

    async def close_host(self):
        if self.companion:
            await self.host.close()
        else:
            await self.host.shutdown()

    def actor(self, buddy_id):
        if self.host is None:
            raise BridgeError("Task service is starting")
        if not self.companion:
            if buddy_id is not None:
                raise BridgeError("Player board does not take a buddy_id")
            return self.host, self.bridge.get_available_actions()
        team = self.bridge.observe()["state"]["members"]
        if buddy_id is None and len(team) == 1:
            buddy_id = next(iter(team))
        actor = self.host.agents.get(buddy_id)
        if buddy_id not in team or actor is None:
            raise BridgeError("Specify a currently bound buddy_id")
        return actor, self.bridge.member_call("observe", buddy_id)

    def current(self, actor, packet):
        state = packet["state"]
        if self.companion:
            snapshot = state.get("companion") or {}
            control = state["control"]
            goal = control.get("goal") if control.get("mode") == "autonomous" else None
            basis = (packet["session_id"], snapshot.get("token"), control["revision"],
                     control.get("mode") == "autonomous", goal)
            return actor.tasks.view(snapshot) if actor.basis == basis and actor.plan else None
        if (actor.session != packet["session_id"] or actor.player_guid != state["player"].get("guid")
                or actor.autonomy_revision != autonomy.revision(state) or not actor.plan):
            return None
        return actor.goals.view(actor.plan, state)

    def prepare(self, actor, packet, raw, previous, *, replace=False):
        from mcp_agent.companion_tasks import CompanionTasks
        from mcp_agent.daily_planner import validate_player_board, planning_context
        state = packet["state"]
        rows = raw.get("tasks")
        if not isinstance(rows, list):
            raise BridgeError("tasks must be a list")
        if self.companion:
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
        if state.get("bridge", {}).get("protocol", 0) < 4:
            raise BridgeError("Task scheduling requires player Mod protocol 4")
        if not autonomy.enabled(state):
            raise BridgeError("Enable character control in the game UI")
        if state["player"].get("vitals", {}).get("dead"):
            raise BridgeError("Player is dead")
        context = {**planning_context(state, []), "current_task_board": previous}
        clean = validate_player_board(raw, state["world"]["day"], context, map_seen=True)
        return (clean if replace else merge_goals(previous, clean)), None

    def commit(self, actor, packet, board, runtime):
        state = packet["state"]
        if self.companion:
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
        else:
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
        if (board and board.get("status") != "completed") or (actor.active if self.companion else actor.active_id):
            raise BridgeError("Scheduler owns this actor. Wait for its action and complete or delete its tasks before manual control.")

    def get(self, buddy_id):
        actor, packet = self.actor(buddy_id)
        board = self.current(actor, packet)
        active = actor.active if self.companion else actor.active_id
        reason = idle_review_reason(board)
        if board and not reason and not self.companion:
            reason = actor.goals.reason
        return {"task_board": board, "active_action_id": active[0] if self.companion and active else active,
                "review_reason": reason or None}

    def planning_state(self):
        """Capture game intent, boards and their submission versions together."""
        with self.bridge.lock:
            ids = list(self.bridge.observe()["state"]["members"]) if self.companion else ["player"]
            members = {}
            for identity in ids:
                actor, packet = self.actor(identity if self.companion else None)
                state = packet["state"]
                control = state["control"] if self.companion else state.get("autonomy") or {}
                observed = state["companion"] if self.companion else state
                board = self.current(actor, packet)
                dead = bool(self.companion and (observed.get("buddy") or {}).get("dead"))
                basis = [packet["session_id"], observed.get("token") if self.companion else state["player"].get("guid"),
                         control.get("revision"), control.get("dialogue_revision"), dead]
                intent_version = hashlib.sha256(json.dumps(basis).encode()).hexdigest()
                basis.append((board or {}).get("board_id"))
                version = hashlib.sha256(json.dumps(basis).encode()).hexdigest()
                active = actor.active if self.companion else actor.goal_actions.get(actor.active_id)
                reason = idle_review_reason(board)
                if board and self.companion:
                    advisory = actor.external_review_reason
                    if advisory in {"return_at_risk", "return_deadline_missed"} and reason != "all_tasks_completed":
                        reason = advisory
                    elif not reason and not active:
                        reason = advisory
                elif board and not reason and not active:
                    if actor.goals.reason not in {"", "waiting_for_start", "residence_holding", "maintenance_holding",
                                                    "refuel_settling", "charcoal_burning", "search_budget_cooldown"}:
                        reason = actor.goals.reason
                members[identity] = {
                    "interaction": control, "observation": observed, "current_task_board": board,
                    "ready": state.get("ready", True) and not dead, "version": version, "intent_version": intent_version,
                    "active_task_id": (active[1] if self.companion else active[0]) if active else None,
                    "review_reason": "buddy_dead" if dead else reason,
                    **({"supported_capabilities": capabilities(observed)} if self.companion else {}),
                }
            team = self.bridge.observe()["state"]["control"] if self.companion else {}
            return {"members": members, "interaction": team}

    def check_versions(self, versions, identities):
        if versions is None:
            return
        current = self.planning_state()["members"]
        if set(versions) != set(identities) or any(
                identity not in current or current[identity]["version"] != version
                for identity, version in versions.items()):
            raise BridgeError("Planning state changed; read get_planning_state again")

    def reply(self, text, version, outcome, buddy_id, message_id):
        with self.bridge.lock:
            actor, packet = self.actor(buddy_id)
            identity = actor.buddy_id if self.companion else "player"
            self.check_versions({identity: version}, [identity])
            state = packet["state"]
            control = state["control"] if self.companion else state.get("autonomy") or {}
            board = self.current(actor, packet)
            if outcome == "completed" and control.get("mode") == "autonomous":
                raise BridgeError("Autonomous mode continues after milestones; send an ordinary reply")
            if outcome == "completed" and any(t["status"] != "done" for t in (board or {}).get("tasks", [])):
                raise BridgeError("Cannot complete while accepted tasks remain unfinished")
            finish = control.get("mode") == "instruction" and (
                outcome == "completed" or outcome == "blocked" and (not board or not board.get("execution_task_ids")))
            if self.companion:
                return self.bridge.member_call("reply", identity, text, control["revision"], message_id, finish,
                                               control.get("dialogue_revision"))
            return self.bridge.send_agent_feedback(text, control["revision"], message_id, finish)

    def stop_changed_action(self, actor, packet, board):
        active = actor.active if self.companion else actor.goal_actions.get(actor.active_id)
        if not active:
            return
        task_id = active[1] if self.companion else active[0]
        if task_id is None:
            return
        old = next((task for task in actor.plan["tasks"] if task["id"] == task_id), None) if actor.plan else None
        new = next((task for task in board["tasks"] if task["id"] == task_id), None) if board else None
        if old and new and all(old.get(key) == new.get(key) for key in ("priority", "focus", "depends_on")) and task_signature(old) == task_signature(new):
            return
        if self.companion:
            record = self.bridge.member_call("action", actor.buddy_id, "stop", packet["observation_id"])
            actor.active = (record["id"], None, "stop")
            return task_id
        self.bridge.cancel_action(actor.active_id)

    def submit(self, raw, buddy_id=None, observation_id=None, versions=None):
        if not self.companion or "assignments" not in raw:
            assignments = [(buddy_id, raw)]
        else:
            assignments = raw["assignments"]
            if buddy_id is not None or not isinstance(assignments, dict) or not assignments:
                raise BridgeError("Use assignments keyed by buddy_id for a team batch")
            assignments = list(assignments.items())
        with self.bridge.lock:
            if observation_id is not None:
                if self.companion:
                    raise BridgeError("Companion boards use buddy_id or assignments")
                self.bridge.validate_board_observation(observation_id)
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
            self.check_versions(versions, [actor.buddy_id if self.companion else "player" for actor, _, _, _ in staged])
            for actor, packet, board, runtime in staged:
                if self.stop_changed_action(actor, packet, board) and runtime:
                    runtime.pause_search()
            for actor, packet, board, runtime in staged:
                board["board_id"] = uuid.uuid4().hex
                self.commit(actor, packet, board, runtime)
            boards = {(actor.buddy_id if self.companion else "player"): board
                      for actor, _, board, _ in staged}
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
            if self.companion:
                task = expand_assignment({"tasks": [task]}, context)["tasks"][0]
            else:
                task = expand_tasks([task], rows)[0]
            required = set(task["depends_on"]) if isinstance(task.get("depends_on"), list) and all(
                isinstance(item, str) for item in task["depends_on"]) else set()
            prerequisites = [row for row in rows if row["id"] in required]
            # Include the transitive prerequisite closure for the existing validator.
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
                from mcp_agent.compact_tasks import TASK_OPTIONS
                if {"id", "target"}.intersection(changes):
                    raise BridgeError("Keep the task ID and pass target fields alongside other fields")
                row = {key: value for key, value in old.items() if key in TASK_OPTIONS}
                target = old["target"] if changes.get("type", old["target"]["type"]) == old["target"]["type"] else {}
                row = {**row, **target, **changes, "id": task_id}
                expanded = (expand_assignment({"tasks": [row]}, {"observation": packet["state"]["companion"],
                            "current_task_board": previous})["tasks"] if self.companion
                            else expand_tasks([row], previous["tasks"]))
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
                if self.companion:
                    from mcp_agent.companion_tasks import CompanionTasks
                    runtime = CompanionTasks()
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
            if internal_call.get() and not self.companion:
                # Existing internal Lua synchronization only releases a previous lease.
                if auto_execute:
                    raise ToolError("Automatic task boards run in Python")
                return invoke(self.bridge.submit_task_board, board, observation_id, False)
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
