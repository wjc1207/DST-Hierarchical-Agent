"""Model-side orchestration using the public MCP task tools."""
import asyncio
import json
import threading
import time

from mcp_agent.daily_planner import planning_context, planning_slot, request_plan, snapshot_message
from mcp_agent.planning_core import recent_exchanges


class PlanningClient:
    def __init__(self, client, *, player_model=None, companion_model=None):
        self.client = client
        self.player_model = player_model
        self.companion_model = companion_model
        self.history = []
        self.handled = {}
        self.pending = None
        self.cancel = None
        self.versions = {}
        self.retry_at = 0
        self.blocked_until = {}
        self.finished = {}
        if companion_model:
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

    async def rpc(self, name, **args):
        result = await self.client.call_tool(name, args)
        if result.is_error:
            raise RuntimeError("; ".join(getattr(row, "text", "") for row in result.content))
        return result.structured_content

    def plan(self, snapshot, requested, cancel):
        members = snapshot["members"]
        if self.companion_model:
            context = {
                "interaction": snapshot["interaction"],
                "long_term_goal": snapshot["interaction"].get("goal"),
                "members": {identity: {**row, "replan_reason": (
                    "player_dialogue" if identity in requested and self.new_intent(identity, row)
                    else row["review_reason"])} for identity, row in members.items()},
                "request_ids": requested,
            }
            result = self.companion_model(context, self.history, cancel)
            plans = {row["buddy_id"]: row["plan"] for row in result["assignments"]}
            return json.dumps(context, ensure_ascii=False), result["_assistant_message"], plans
        actor = members["player"]
        state = actor["observation"]
        context = planning_context(state, state.get("bridge", {}).get("events", []))
        context.update(current_task_board=actor["current_task_board"], replan_reason=(
            "player_message" if self.new_intent("player", actor) else actor["review_reason"] or "quarter_review"))
        result = request_plan(*self.player_model, state["world"]["day"], context, self.history, cancel_event=cancel)
        return snapshot_message(context), json.dumps(result, ensure_ascii=False), {"player": result}

    def new_intent(self, identity, row):
        return self.handled.get(identity, (None,))[0] != row["intent_version"]

    def marker(self, row):
        control = row["interaction"]
        board = row["current_task_board"] or {}
        tasks = tuple(sorted((t["id"], t["status"], t.get("blocked_reason", "")) for t in board.get("tasks", [])))
        slot = planning_slot(row["observation"]) if self.player_model and control.get("mode") == "autonomous" else None
        reason = row["review_reason"]
        return (row["intent_version"], slot, reason, tasks if reason else ())

    async def cycle(self):
        snapshot = await self.rpc("get_planning_state")
        members = snapshot["members"]
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
                markers = {identity: self.marker(members[identity]) for identity in plans}
                boards = {i: p for i, p in plans.items() if p.get("kind") == "task_board"}
                replies = {i: p for i, p in plans.items() if i not in boards}
                if boards:
                    board = {"assignments": boards} if self.companion_model else boards["player"]
                    result = await self.rpc("submit_task_board", board=board, versions={i: self.versions[i] for i in boards})
                    for identity, plan in boards.items():
                        if plan.get("reply"):
                            await self.rpc("send_planning_reply", text=plan["reply"], version=result["versions"][identity],
                                           **({"buddy_id": identity} if self.companion_model else {}))
                for identity, reply in replies.items():
                    if reply["outcome"] == "blocked" and members[identity]["interaction"].get("mode") == "autonomous":
                        self.blocked_until[identity] = time.monotonic() + 30
                    await self.rpc("send_planning_reply", text=reply["reply"], version=self.versions[identity],
                                   outcome=reply["outcome"], **({"buddy_id": identity} if self.companion_model else {}))
                    if reply["outcome"] == "completed":
                        self.finished[identity] = members[identity]["intent_version"]
                self.history = recent_exchanges([*self.history, (user_message, assistant_message)])
                fresh = (await self.rpc("get_planning_state"))["members"]
                for identity in plans:
                    row = fresh.get(identity)
                    if row and row["intent_version"] == members[identity]["intent_version"]:
                        # Preserve an immediate review when an accepted board is already complete.
                        marker = self.marker(row)
                        if identity in boards and marker[2] == "all_tasks_completed" and markers[identity][2] != marker[2]:
                            marker = markers[identity]
                        self.handled[identity] = marker
                return
            except Exception as exc:
                if getattr(exc, "http_status", None) == 402:
                    for identity in self.versions:
                        row = members[identity]
                        await self.rpc("send_planning_reply", text="DeepSeek API 余额不足（HTTP 402）。请检查余额后再发送消息。",
                                       version=row["version"], **({"buddy_id": identity} if self.companion_model else {}))
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
            self.cancel = threading.Event()
            self.pending = asyncio.create_task(asyncio.to_thread(self.plan, snapshot, requested, self.cancel))

    def close(self):
        if self.cancel:
            self.cancel.set()
        if self.pending:
            self.pending.cancel()
