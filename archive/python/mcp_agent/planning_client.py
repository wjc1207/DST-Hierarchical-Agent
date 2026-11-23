"""Model-side orchestration using the public MCP task tools."""
import asyncio
import json
import threading
import time

from mcp_agent.agent_memory import AgentMemoryStore, capabilities_fingerprint
from mcp_agent.context_harness import ContextHarness, llm_compact_summary
from mcp_agent.player_planner import SUPPORTED_CAPABILITIES, planning_context, planning_slot, request_plan, snapshot_message
from mcp_server.player_bridge import SUPPORTED as PLAYER_SUPPORTED_ACTIONS


def _extract_world_id(snapshot: dict) -> str | None:
    for row in (snapshot.get("members") or {}).values():
        obs = row.get("observation") or {}
        world_id = (obs.get("world") or {}).get("world_id")
        if isinstance(world_id, str) and world_id.strip():
            return world_id.strip()
    session_id = snapshot.get("session_id")
    return session_id if isinstance(session_id, str) and session_id.strip() else None


class PlanningClient:
    def __init__(self, client, *, player_model=None, companion_model=None, external_backend=None, memory_root=None):
        self.client = client
        self.player_model = player_model
        self.companion_model = companion_model
        self.external_backend = external_backend
        self.memory = AgentMemoryStore(root=memory_root)
        self.context = ContextHarness(role="companion" if self.is_companion else "player", root=memory_root)
        self.handled = {}
        self.pending = None
        self.cancel = None
        self.versions = {}
        self.request_markers = {}
        self.retry_at = 0
        self.blocked_until = {}
        self.finished = {}
        if companion_model:
            if hasattr(companion_model, "set_memory_store"):
                companion_model.set_memory_store(self.memory)
            if hasattr(companion_model, "set_usage_handler"):
                companion_model.set_usage_handler(self.context.note_usage)
            if hasattr(companion_model, "preserve_history"):
                companion_model.preserve_history = True
            loop = asyncio.get_running_loop()

            def query(args, context, cancel):
                actor = context["members"][args["buddy_id"]]
                future = asyncio.run_coroutine_threadsafe(self.rpc(
                    "buddy_recipes", **args, revision=actor["interaction"]["revision"],
                    binding_token=actor["observation"]["token"]), loop)
                try:
                    from concurrent.futures import TimeoutError
                    for _ in range(80):
                        if cancel.is_set():
                            raise RuntimeError("Planning superseded")
                        try:
                            return future.result(timeout=.1)
                        except TimeoutError:
                            continue
                    raise RuntimeError("Recipe query timed out")
                finally:
                    future.cancel()

            companion_model.set_query_handler(query)

    @property
    def is_companion(self) -> bool:
        return bool(self.companion_model) or (self.external_backend is not None and self.external_backend.role == "companion")

    async def rpc(self, name, **args):
        result = await self.client.call_tool(name, args)
        if result.is_error:
            raise RuntimeError("; ".join(getattr(row, "text", "") for row in result.content))
        return result.structured_content

    def _compact_if_needed(self):
        if not self.context.should_compact():
            return
        summarizer = None
        if self.companion_model and getattr(self.companion_model, "api_url", None) and getattr(self.companion_model, "key", None):
            summarizer = lambda prev, older: llm_compact_summary(
                self.companion_model.api_url, self.companion_model.key, self.companion_model.model,
                prev, older, memory_store=self.memory)
        elif self.player_model:
            api_url, api_key, model = self.player_model
            summarizer = lambda prev, older: llm_compact_summary(
                api_url, api_key, model, prev, older, memory_store=self.memory)
        if self.context.compact(summarizer=summarizer):
            print(f"context_compacted role={self.context.role} count={self.context.compaction_count} turns_kept={len(self.context.turns)}")

    def plan(self, snapshot, requested, cancel):
        if self.external_backend is None:
            self._compact_if_needed()
        history = self.context.messages_for_prompt()
        members = snapshot["members"]
        if self.is_companion:
            context = {
                "interaction": snapshot["interaction"],
                "long_term_goal": snapshot["interaction"].get("goal"),
                "members": {identity: {**row, "replan_reason": (
                    "player_dialogue" if identity in requested and self.new_intent(identity, row)
                    else row["review_reason"])} for identity, row in members.items()},
                "request_ids": requested,
            }
            if self.external_backend is not None:
                result = self.external_backend.plan_companion(context, history, cancel, world_id=self.context.world_id)
            else:
                result = self.companion_model(context, history, cancel)
            plans = {row["buddy_id"]: row["plan"] for row in result["assignments"]}
            return json.dumps(context, ensure_ascii=False), result["_assistant_message"], plans
        actor = members["player"]
        state = actor["observation"]
        context = planning_context(state, state.get("bridge", {}).get("events", []))
        context.update(current_task_board=actor["current_task_board"], replan_reason=(
            "player_message" if self.new_intent("player", actor) else actor["review_reason"] or "quarter_review"))
        if self.external_backend is not None:
            result = self.external_backend.plan_player(state["world"]["day"], context, history, cancel, world_id=self.context.world_id)
        else:
            api_url, api_key, model = self.player_model
            result = request_plan(api_url, api_key, model, state["world"]["day"], context, history,
                                  cancel_event=cancel, memory_store=self.memory,
                                  on_usage=self.context.note_usage, preserve_history=True)
        return snapshot_message(context), json.dumps(result, ensure_ascii=False), {"player": result}

    def new_intent(self, identity, row):
        return self.handled.get(identity, (None,))[0] != row["intent_version"]

    def marker(self, row):
        control = row["interaction"]
        board = row["current_task_board"] or {}
        tasks = tuple(sorted((t["id"], t["status"], t.get("blocked_reason", "")) for t in board.get("tasks", [])))
        slot = planning_slot(row["observation"]) if not self.is_companion and control.get("mode") == "autonomous" else None
        reason = row["review_reason"]
        return (row["intent_version"], slot, reason, tasks if reason else ())

    async def cycle(self):
        snapshot = await self.rpc("get_planning_state")
        if snapshot.get("paused"):
            return
        world_id = _extract_world_id(snapshot)
        if world_id:
            self.memory.bind_world(world_id)
            self.context.bind_world(world_id)
        members = snapshot["members"]
        if self.is_companion:
            caps = sorted({
                c for row in members.values()
                for c in (row.get("supported_capabilities") or (row.get("observation") or {}).get("capabilities") or [])
            })
            if caps:
                self.memory.set_capabilities_hash(capabilities_fingerprint(caps))
        else:
            self.memory.set_capabilities_hash(capabilities_fingerprint([
                *SUPPORTED_CAPABILITIES.get("target_types", []),
                *PLAYER_SUPPORTED_ACTIONS,
            ]))
        if self.pending:
            if any(members.get(i, {}).get("version") != v or not members[i]["interaction"].get("enabled")
                   for i, v in self.versions.items()):
                self.cancel.set()
                if self.pending.done():
                    self.pending.exception()
                    self.pending = None
                return
            if not self.pending.done():
                return
            pending, self.pending = self.pending, None
            try:
                user_message, assistant_message, plans = pending.result()
                boards = {i: p for i, p in plans.items() if p.get("kind") == "task_board"}
                replies = {i: p for i, p in plans.items() if i not in boards}
                if boards:
                    board = {"assignments": boards} if self.is_companion else boards["player"]
                    result = await self.rpc("submit_task_board", board=board, versions={i: self.versions[i] for i in boards})
                    for identity, plan in boards.items():
                        if plan.get("reply"):
                            await self.rpc("send_planning_reply", text=plan["reply"], version=result["versions"][identity],
                                           **({"buddy_id": identity} if self.is_companion else {}))
                for identity, reply in replies.items():
                    if reply["outcome"] == "blocked" and members[identity]["interaction"].get("mode") == "autonomous":
                        self.blocked_until[identity] = time.monotonic() + 30
                    await self.rpc("send_planning_reply", text=reply["reply"], version=self.versions[identity],
                                   outcome=reply["outcome"], **({"buddy_id": identity} if self.is_companion else {}))
                    if reply["outcome"] == "completed":
                        self.finished[identity] = members[identity]["intent_version"]
                self.context.append(user_message, assistant_message)
                fresh = (await self.rpc("get_planning_state"))["members"]
                for identity in plans:
                    row = fresh.get(identity)
                    if row and row["intent_version"] == members[identity]["intent_version"]:
                        marker = self.marker(row)
                        requested = self.request_markers[identity]
                        # Review an idle state the model has not seen. Once a
                        # review leaves the same idle reason, wait for a change.
                        if identity in boards and marker[2] in {
                            "all_tasks_completed", "only_scheduled_tasks", "no_actionable_tasks",
                            "search_or_prerequisite_blocked",
                        } and requested[2] != marker[2]:
                            marker = requested
                        self.handled[identity] = marker
                return
            except Exception as exc:
                if getattr(exc, "http_status", None) == 402:
                    for identity in self.versions:
                        row = members[identity]
                        await self.rpc("send_planning_reply", text="DeepSeek API 余额不足（HTTP 402）。请检查余额后再发送消息。",
                                       version=row["version"], **({"buddy_id": identity} if self.is_companion else {}))
                        self.finished[identity] = row["intent_version"]
                self.retry_at = time.monotonic() + 5
                raise
        if time.monotonic() < self.retry_at:
            return
        requested = [
            identity for identity, row in members.items()
            if row["ready"] and row["interaction"].get("enabled")
            and self.finished.get(identity) != row["intent_version"]
            and (row["interaction"].get("mode") == "autonomous" or row["interaction"].get("instruction")
                 or row["interaction"].get("messages"))
            and (self.marker(row) != self.handled.get(identity)
                 or identity in self.blocked_until and time.monotonic() >= self.blocked_until[identity])
        ]
        if requested:
            for identity in requested:
                self.blocked_until.pop(identity, None)
            self.versions = {identity: members[identity]["version"] for identity in requested}
            self.request_markers = {identity: self.marker(members[identity]) for identity in requested}
            self.cancel = threading.Event()
            self.pending = asyncio.create_task(asyncio.to_thread(self.plan, snapshot, requested, self.cancel))

    def close(self):
        if self.cancel:
            self.cancel.set()
        if self.pending:
            self.pending.cancel()


async def run_planning_loop(client, *, poll, max_cycles=0, player_model=None, companion_model=None,
                            external_backend=None, idle_tool=None, on_error=None, on_recovered=None):
    """Shared MCP orchestration loop for both player and companion CLIs."""
    has_planner = bool(player_model or companion_model or external_backend)
    planning = PlanningClient(client, player_model=player_model, companion_model=companion_model,
                              external_backend=external_backend) if has_planner else None
    cycle = 0
    try:
        while not max_cycles or cycle < max_cycles:
            cycle += 1
            try:
                if planning:
                    await planning.cycle()
                elif idle_tool:
                    result = await client.call_tool(idle_tool, {})
                    if result.is_error:
                        raise RuntimeError("; ".join(getattr(row, "text", "") for row in result.content))
                if on_recovered:
                    on_recovered()
            except Exception as exc:
                if on_error:
                    on_error(exc)
                else:
                    print(f"planning_cycle_error: {exc}")
            await asyncio.sleep(poll)
    finally:
        if planning:
            planning.close()
