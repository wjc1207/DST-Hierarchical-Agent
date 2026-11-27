"""Deterministic multi-actor Task Executor runtime running inside the server process."""
from __future__ import annotations

import asyncio
import copy
import json
import math
import shutil
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from datetime import datetime, timezone
from types import SimpleNamespace

from common.paths import DATA_DIR
from common.task_board import format_board
from common.task_timing import number
from common.thermal import seasonal_gear_equip
from executor.board_contract import recent_exchanges, validate_reply, validate_team_plan
from executor.companion_tasks import RETURN_ALERTS, CompanionTasks, capabilities, collection_prefab
from executor.priority_board import idle_review_marker, idle_review_reason
from executor.world_graph import WorldGraph
from transport.metrics import TransportRecorder

AUTONOMY_REVIEW_SECONDS = 30
THERMAL_ALERTS = {"thermal_emergency_freezing", "thermal_emergency_overheating", "thermal_emergency_wet"}
URGENT_EXTERNAL_ALERTS = set(RETURN_ALERTS.values()) | THERMAL_ALERTS


def seasonal_alert_reason(observed, board=None):
    """Detect acute freezing, overheating, or unequipped wet-rain hazard not already covered by thermal_comfort."""
    if "thermal" not in capabilities(observed or {}):
        return None
    for task in ((board or {}).get("tasks") or []):
        if ((task.get("target") or {}).get("type") == "thermal_comfort"
                and task.get("status") not in {"done", "failed", "expired"}):
            return None
    buddy = (observed or {}).get("buddy") or {}
    world = (observed or {}).get("world") or {}
    temp = buddy.get("temperature")
    moisture = buddy.get("moisture")
    if buddy.get("freezing") or (number(temp) and temp <= 8.0):
        return "thermal_emergency_freezing"
    if buddy.get("overheating") or (number(temp) and temp >= 62.0):
        return "thermal_emergency_overheating"
    if (world.get("israining") and number(moisture) and moisture >= 35.0
            and seasonal_gear_equip(observed, "rain") is not None):
        return "thermal_emergency_wet"
    return None


class DirectBridgeClient:
    """In-process adapter invoking CompanionTeamBridge directly without MCP routing."""

    def __init__(self, bridge):
        self.bridge = bridge

    async def call_tool(self, name, arguments):
        args = arguments or {}
        buddy_id = args.get("buddy_id")
        try:
            if name == "buddy_observe":
                data = self.bridge.observe() if buddy_id is None else self.bridge.member_call("observe", buddy_id)
            elif name == "buddy_action":
                data = self.bridge.member_call("action", buddy_id, args["op"], args["observation_id"], args.get("args"))
            elif name == "buddy_result":
                data = self.bridge.member_call("result", buddy_id, args["action_id"])
            elif name == "buddy_reply":
                data = self.bridge.member_call(
                    "reply", buddy_id, args["text"], args["revision"], args["message_id"],
                    args.get("finish", False), args.get("dialogue_revision"), args.get("keep_mode", False),
                )
            else:
                raise ValueError(f"Unsupported direct bridge tool: {name}")
            return SimpleNamespace(is_error=False, structured_content=data, content=[])
        except ValueError as exc:
            return SimpleNamespace(is_error=True, structured_content=None, content=[SimpleNamespace(text=str(exc))])


class CycleDiagnostics:
    """Bound repeated connection errors during game pause; report recovery and counts."""

    def __init__(self, agent, *, clock=time.monotonic):
        self.agent, self.clock = agent, clock
        self.key = None
        self.started = 0.0
        self.last_report = 0.0
        self.count = 0
        self.suppressed = 0

    def error(self, exc):
        now = self.clock()
        key = (type(exc).__name__, str(exc)[:250])
        if key != self.key:
            self.recovered()
            self.key = key
            self.started = now
            self.last_report = -math.inf
            self.count = 0
        self.count += 1
        if "disconnected/stale" in key[1] and now - self.last_report < 30:
            self.suppressed += 1
            return
        print(f"companion_cycle_error type={key[0]} detail={key[1]} repeats={self.count}")
        if self.agent:
            self.agent.trace("cycle_error", kind=key[0], detail=key[1], repeats=self.count, suppressed=self.suppressed)
        self.last_report = now
        self.suppressed = 0

    def recovered(self):
        if self.key is not None and self.agent:
            self.agent.trace("cycle_error_end", kind=self.key[0], detail=self.key[1],
                             duration_seconds=round(self.clock() - self.started, 2), repeats=self.count, suppressed=self.suppressed)
        self.key = None
        self.suppressed = 0


class ActorRunner:
    def __init__(self, client, planner, *, execute=False, log_path=None, buddy_id=None, external_board=False,
                 world_graph=None, team_completed_provider=None, team_positions_provider=None):
        self.client, self.planner, self.execute = client, planner, execute
        self.buddy_id = buddy_id
        self.external_board = external_board
        self.world_graph = world_graph
        self.team_completed_provider = team_completed_provider
        self.team_positions_provider = team_positions_provider
        self.external_review_reason = None
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="companion-ds")
        self.future = None
        self.cancel = None
        self.basis = None
        self.dialogue_revision = None
        self.plan = self.previous = None
        self.tasks = CompanionTasks(
            clock=lambda: self.task_time(), graph=self.world_graph,
            team_completed_provider=self.team_completed_provider,
            team_positions_provider=self.team_positions_provider,
        )
        self.observed = None
        self.sampled_at = None
        self.idle_marker = None
        self.history = []
        self.active = None
        self.reason = "player_message"
        self.pending_context = None
        self.failures = 0
        self.planning_unavailable = False
        self.retry_at = 0.0
        self.finished_basis = None
        self.reviews = 0
        self.waiting_at = None
        self.waiting_position = None
        self.events = TransportRecorder(log_path)
        self.last_failure = ("action", "unknown")
        self.observation_marker = None
        self.prerequisite_replans = 0
        self.storage_wait_marker = None
        self.thermal_alert_sent = None
        if hasattr(planner, "set_error_handler"):
            planner.set_error_handler(lambda stage, detail: self.trace("planning_detail", stage=stage, detail=detail))
        if hasattr(planner, "set_query_handler"):
            loop = asyncio.get_running_loop()

            def query(args, context, cancel):
                future = asyncio.run_coroutine_threadsafe(self.rpc(
                    "buddy_recipes", **args,
                    revision=context["interaction"]["revision"], binding_token=context["observation"]["token"],
                ), loop)
                try:
                    for _ in range(100):
                        if cancel.is_set():
                            raise RuntimeError("Planning superseded by newer player intent")
                        try:
                            return future.result(timeout=.1)
                        except FutureTimeout:
                            continue
                    raise RuntimeError("Companion recipe query timed out")
                finally:
                    if not future.done():
                        future.cancel()

            planner.set_query_handler(query)

    def trace(self, event, **fields):
        self.events.write({"timestamp_utc": datetime.now(timezone.utc).isoformat(), "event": event,
                           "revision": self.basis[2] if self.basis else None,
                           **({"buddy_id": self.buddy_id} if self.buddy_id else {}), **fields})

    def failed(self, kind, reason):
        self.failures += 1
        self.last_failure = (kind, reason)
        self.trace("failure", kind=kind, reason=reason, count=self.failures)

    async def rpc(self, name, **args):
        result = await self.client.call_tool(name, args)
        if result.is_error:
            raise RuntimeError("; ".join(getattr(block, "text", "") for block in result.content)[:300])
        if not isinstance(result.structured_content, dict):
            raise RuntimeError("Companion tool returned no structured result")
        return result.structured_content

    def task_time(self):
        return self.sampled_at if self.sampled_at is not None else time.monotonic()

    def board(self):
        if not self.plan:
            return self.previous
        return self.tasks.view(self.observed)

    async def reply(self, text, revision, finish=False, *, keep_mode=False):
        scope = {} if self.dialogue_revision is None else {"dialogue_revision": self.dialogue_revision}
        if keep_mode:
            scope["keep_mode"] = True
        result = await self.rpc("buddy_reply", text=text[:256], revision=revision,
                                message_id=uuid.uuid4().hex, finish=finish, **scope)
        if finish:
            self.finished_basis = self.basis
        self.trace("reply_queued", finish=finish, text_chars=len(text))
        return result

    def wait_for_autonomous_review(self, reason):
        self.previous, self.plan = self.board(), None
        self.reason = reason
        self.retry_at = time.monotonic() + AUTONOMY_REVIEW_SECONDS

    def continue_other_tasks(self, task_id, reason, *, keep_blocked=False):
        self.tasks.defer_task(task_id, reason)
        board = self.board()
        if board["execution_task_ids"] or any(t["status"] == "scheduled" for t in board["tasks"]):
            self.waiting_at = self.waiting_position = None
            self.trace("task_deferred", task_id=task_id, reason=reason)
            return True
        if not keep_blocked:
            self.tasks.clear_task_deferrals(task_id)
        return False

    def cancel_planning(self):
        if self.cancel:
            self.cancel.set()
        if self.future:
            self.future.cancel()
        self.future = None

    def request_plan(self, control, observed, reason):
        if self.external_board:
            self.reason = reason
            return
        board = self.board()
        self.pending_context = {"interaction": control, "observation": observed,
                                "current_task_board": board, "replan_reason": reason,
                                "supported_capabilities": capabilities(observed)}
        if self.active:
            self.pending_context["active_task_id"] = self.active[1]
        if control.get("mode") == "autonomous":
            self.pending_context["long_term_goal"] = control.get("goal")
        self.cancel = threading.Event()
        self.future = self.executor.submit(self.planner, copy.deepcopy(self.pending_context), list(self.history), self.cancel)
        self.trace("plan_requested", reason=reason, current_tasks=(board or {}).get("tasks", []))
        print(f"companion_plan_requested revision={control['revision']} reason={reason}")

    async def cycle(self):
        packet = await self.rpc("buddy_observe")
        state = packet["state"]
        if state.get("paused"):
            return
        control, observed = state["control"], state.get("companion") or {}
        sampled_at = observed.get("sampled_at")
        if number(sampled_at):
            self.sampled_at = sampled_at
        autonomous = control.get("mode") == "autonomous"
        goal = control.get("goal") if autonomous else None
        basis = (packet["session_id"], observed.get("token"), control["revision"], autonomous, goal)
        if basis != self.basis:
            self.cancel_planning()
            self.dialogue_revision = None
            same_actor = self.basis is not None and self.basis[:2] == basis[:2]
            same_goal = same_actor and self.basis[3:] == basis[3:]
            self.tasks.pause_search()
            # The new message can arrive with the first observation of an item
            # just collected. Refresh facts before preserving the old board.
            self.previous = self.tasks.view(observed) if same_goal and self.tasks.plan else None
            if same_goal:
                self.tasks.clear_task_deferrals()
            else:
                self.tasks = CompanionTasks(
                    clock=lambda: self.task_time(), graph=self.world_graph,
                    team_completed_provider=self.team_completed_provider,
                    team_positions_provider=self.team_positions_provider,
                )
                self.history = []
            self.plan, self.active = None, None
            self.basis = basis
            self.reason = "autonomous_goal" if autonomous else "player_message"
            self.external_review_reason = None
            self.failures, self.retry_at = 0, 0.0
            self.planning_unavailable = False
            self.finished_basis, self.reviews, self.waiting_at = None, 0, None
            self.waiting_position = None
            self.idle_marker = None
            self.prerequisite_replans = 0
            self.storage_wait_marker = None
            self.thermal_alert_sent = None
        self.observed = observed
        self.tasks.view(observed)
        products = {}
        for row in observed.get("nearby") or []:
            product = row.get("product", "unknown")
            products[product] = products.get(product, 0) + 1
        diagnostic = {"enabled": control["enabled"], "ready": state.get("ready"), "mode": observed.get("mode"),
                      "buddy_guid": (observed.get("buddy") or {}).get("guid"),
                      "escape": observed.get("escape"),
                      "inventory": (observed.get("buddy") or {}).get("inventory") or {}, "available_products": products}
        buildings = (observed.get("places") or {}).get("buildings", [])
        diagnostic["known_building_count"] = len(buildings)
        diagnostic["recent_buildings"] = [{k: row.get(k) for k in ("id", "prefab", "x", "z", "status", "arrived")}
                                          for row in buildings[-8:]]
        marker = json.dumps({"revision": control["revision"], **diagnostic}, sort_keys=True)
        if marker != self.observation_marker:
            self.observation_marker = marker
            self.trace("observation", **diagnostic)
        if not self.execute or not (control["enabled"] or self.external_board and (self.plan or self.active)) or not state.get("ready"):
            return
        dialogue_revision = control.get("dialogue_revision")
        if dialogue_revision is not None and dialogue_revision != self.dialogue_revision:
            self.cancel_planning()
            self.dialogue_revision = dialogue_revision
            self.planning_unavailable = False
            self.finished_basis = None
            self.request_plan(control, observed, "player_dialogue")
        pending_reason = self.pending_context["replan_reason"] if self.future is not None else None
        dialogue_pending = pending_reason == "player_dialogue"
        return_advisory = pending_reason in RETURN_ALERTS.values()
        if self.finished_basis == basis:
            return
        if (observed.get("buddy") or {}).get("dead"):
            self.cancel_planning()
            if self.external_board:
                self.active = None
                self.tasks.pause_search()
                self.external_review_reason = "buddy_dead"
            else:
                await self.reply("伙伴已死亡，已停止指令执行。", control["revision"], True)
            return
        if self.external_board and self.plan:
            alert = self.tasks.next_return_alert(self.board())
            if alert:
                self.external_review_reason = alert[1]
            seasonal_alert = seasonal_alert_reason(observed, self.board())
            if seasonal_alert and seasonal_alert != self.thermal_alert_sent:
                self.thermal_alert_sent = seasonal_alert
                self.external_review_reason = seasonal_alert
                self.trace("seasonal_alert", reason=seasonal_alert)
        if self.active:
            action = await self.rpc("buddy_result", action_id=self.active[0])
            if action["status"] not in {"completed", "failed", "interrupted"}:
                return
            _, task_id, kind = self.active
            self.trace("action_result", action_id=self.active[0], status=action["status"], reason=action.get("reason"), task_id=task_id)
            self.active = None
            if action.get("reason") == "game_resumed_refresh":
                self.tasks.pause_search()
                return
            if kind == "stop":
                return
            locally_handled = self.tasks.on_result(task_id, action, observed)
            if action["status"] != "completed" and not locally_handled:
                reason = action.get("reason", "unknown")
                if self.continue_other_tasks(task_id, "action_failed:" + reason, keep_blocked=self.external_board) or self.external_board:
                    return
                self.failed("action", reason)
                self.previous = self.board()
                self.plan = None
                self.reason = "action_failed:" + reason
            else:
                self.prerequisite_replans = 0
                if action["status"] == "completed" and (action.get("reason") in {
                        "one_item_transferred", "one_item_dropped", "inventory_increased", "equipped", "unequipped",
                        "work_progressed", "work_finished", "new_structure_observed", "arrived", "building_arrived",
                        "target_killed", "combat_progressed", "pot_loaded", "pot_started", "tree_ignited",
                        "food_eaten", "healed", "fuel_added"}
                        or str(action.get("reason") or "").startswith("meal_harvested:")):
                    self.failures = 0  # Only real progress breaks a consecutive failure streak.
            # The result may arrive after the observation read at the start of this cycle.
            # Read a new snapshot next cycle before selecting another target or verifying completion.
            return
        elif (observed.get("action") or {}).get("status") == "running":
            # A restarted local host may observe an action issued by its prior
            # process. Wait for its real result before planning competing work.
            return
        if observed.get("escape"):
            self.waiting_at = self.waiting_position = None
            return
        if self.failures >= 3 and not dialogue_pending and not self.external_board:
            kind, reason = self.last_failure
            if kind == "planning" and self.plan and any(
                    task.get("at")
                    and task["status"] in {"scheduled", "ready", "in_progress"}
                    for task in self.board()["tasks"]):
                self.planning_unavailable = True
                self.failures = 0
                await self.reply("规划多次失败，已安排的返程会继续。", control["revision"])
                return
            label = "规划" if kind == "planning" else "伙伴动作"
            await self.reply(f"{label}多次失败，最后原因为 {reason[:100]}。已结束本轮并返回跟随。", control["revision"], True)
            return
        if self.future and self.future.done():
            future, self.future = self.future, None
            try:
                plan = future.result()
                if return_advisory:
                    response = validate_reply(plan, {"interaction": control})
                    if not response or response["outcome"] != "reply":
                        raise ValueError("Return timing advisory requires a reply")
                    await self.reply(response["reply"], control["revision"])
                    return
                if plan.get("kind") == "agent_reply":
                    current = self.tasks.view(observed)
                    plan = validate_reply(plan, {"interaction": control, "current_task_board": current})
                    has_work = current and any(t["status"] != "done" for t in current["tasks"])
                    if dialogue_pending:
                        # A conversation can answer or reject a new request while prior work continues.
                        finish = not autonomous and (plan["outcome"] == "completed" or self.plan is None)
                        keep_mode = finish and plan["outcome"] != "completed"
                    else:
                        finish = not autonomous and (plan["outcome"] in {"completed", "blocked"} or not has_work)
                        keep_mode = False
                    await self.reply(plan["reply"], control["revision"], finish, keep_mode=keep_mode)
                    self.history = recent_exchanges([*self.history,
                        (json.dumps(self.pending_context, ensure_ascii=False), json.dumps(plan, ensure_ascii=False))])
                    if dialogue_pending:
                        return
                    if autonomous and (plan["outcome"] == "blocked" or not has_work):
                        self.wait_for_autonomous_review("autonomous_waiting")
                    elif not finish and self.tasks.plan:
                        self.plan = self.tasks.plan
                    else:
                        self.finished_basis = basis
                    return
                for task in plan["tasks"]:
                    target = task["target"]
                    if target["type"] == "inventory":
                        canonical = collection_prefab(target["prefab"], observed)
                        if canonical != target["prefab"]:
                            self.trace("prefab_normalized", task_id=task["id"], source=target["prefab"], product=canonical)
                            target["prefab"] = canonical
                # Merge against fresh facts at the action boundary, then commit
                # only if the bridge still accepts this dialogue's version.
                runtime = copy.deepcopy(self.tasks) if dialogue_pending else self.tasks
                adopted = runtime.adopt(plan, observed)
                await self.reply(plan.get("reply") or plan["goal"], control["revision"])
                self.tasks, self.plan = runtime, adopted
                self.waiting_at, self.waiting_position = None, None
                self.trace("plan_adopted", tasks=plan["tasks"])
                if dialogue_pending:
                    self.failures, self.retry_at = 0, 0.0
                    self.prerequisite_replans, self.reviews = 0, 0
                    self.idle_marker = None
                self.history = recent_exchanges([*self.history,
                    (json.dumps(self.pending_context, ensure_ascii=False), json.dumps(plan, ensure_ascii=False))])
                board = self.board()
                name = observed.get("buddy_name") or self.buddy_id or "伙伴"
                width = shutil.get_terminal_size((100, 24)).columns
                print(f"[{name}]\n{format_board(board, width)}")
                if autonomous and board["status"] == "completed":
                    self.retry_at = time.monotonic() + AUTONOMY_REVIEW_SECONDS
            except Exception as exc:
                detail = (getattr(exc, "safe_detail", None) or
                          (str(exc) if isinstance(exc, (ValueError, TypeError, KeyError)) else type(exc).__name__))[:1000]
                if hasattr(exc, "code"):
                    detail += ":" + str(exc.code)
                if getattr(exc, "http_status", None) == 402:
                    self.planning_unavailable = True
                    self.trace("planning_unavailable", reason=detail, http_status=402)
                    finish = dialogue_pending and not autonomous and self.plan is None
                    await self.reply("DeepSeek API 余额不足（HTTP 402）。请检查余额后再发送消息。",
                                     control["revision"], finish, keep_mode=finish)
                    return
                if return_advisory:
                    self.trace("return_advisory_failed", reason=detail)
                    return
                if dialogue_pending:
                    self.trace("dialogue_plan_failed", reason=detail)
                    finish = not autonomous and self.plan is None
                    await self.reply("这次没能处理你的消息，请再说一次。", control["revision"], finish, keep_mode=finish)
                    return
                self.failed("planning", detail)
                self.retry_at = time.monotonic() + 5
                self.reason = "planning_retry"
                print(f"companion_plan_error type={type(exc).__name__} reason={' '.join(detail.split())[:220]}")
        background_planning = self.future is not None and (dialogue_pending or return_advisory)
        if ((self.future and not background_planning) or time.monotonic() < self.retry_at) and not (
                self.plan and self.tasks.due_returns(self.board())):
            return
        if self.thermal_alert_sent:
            buddy_snap = observed.get("buddy") or {}
            snap_temp = buddy_snap.get("temperature")
            if (seasonal_alert_reason(observed, None) is None
                    and not buddy_snap.get("freezing") and not buddy_snap.get("overheating")
                    and (not number(snap_temp) or 12 <= snap_temp <= 58)):
                self.thermal_alert_sent = None
                if self.external_review_reason in THERMAL_ALERTS:
                    self.external_review_reason = None
        if self.plan:
            if self.future is None and not self.planning_unavailable and not self.external_board:
                seasonal_alert = seasonal_alert_reason(observed, self.board())
                if seasonal_alert and seasonal_alert != self.thermal_alert_sent:
                    self.thermal_alert_sent = seasonal_alert
                    self.previous, self.plan = self.board(), None
                    self.reason = seasonal_alert
                    self.trace("seasonal_alert", reason=seasonal_alert)
                    self.request_plan(control, observed, seasonal_alert)
                    return
                alert = self.tasks.next_return_alert(self.board())
                if alert:
                    task_id, reason, timing = alert
                    self.trace("return_timing_alert", task_id=task_id, reason=reason, **timing)
                    self.request_plan(control, observed, reason)
                    background_planning = True
            task, op, args = self.tasks.next_action(observed)
            if (background_planning or self.planning_unavailable) and op in {"completed", "idle", "blocked", "unsupported", "needs_replan"}:
                return
            if self.external_board and op in {"completed", "idle", "blocked", "unsupported", "needs_replan"}:
                self.external_review_reason = args.get("reason", op)
                if task:
                    self.tasks.defer_task(task["id"], self.reason)
                return
            if op == "waiting_storage":
                marker = (task["id"], args["container_guid"])
                if self.storage_wait_marker != marker:
                    self.trace("storage_wait", task_id=task["id"], **args)
                    self.storage_wait_marker = marker
                return
            if op in {"awaiting_items", "waiting_resource"}:
                return  # Player gives voluntarily; no polling-driven model calls.
            if op == "waiting":
                if (task or {}).get("target", {}).get("type") != "return_owner":
                    self.waiting_at = self.waiting_position = None
                    return
                point = (observed.get("buddy") or {}).get("position") or {}
                position = tuple(point[k] for k in ("x", "z")) if all(number(point.get(k)) for k in ("x", "z")) else None
                progressed = position is not None and (self.waiting_position is None or
                    math.hypot(position[0] - self.waiting_position[0], position[1] - self.waiting_position[1]) >= .5)
                now = self.task_time()
                if self.waiting_at is None or progressed:
                    self.waiting_at, self.waiting_position = now, position
                if now - self.waiting_at < 20 or dialogue_pending:
                    return
                if self.continue_other_tasks(task["id"], "return_owner_stalled", keep_blocked=self.external_board) or self.external_board:
                    return
                self.failed("action", "return_owner_stalled")
                self.previous, self.plan = self.board(), None
                self.reason, self.waiting_at = "return_owner_stalled", None
                self.waiting_position = None
            elif op == "completed":
                if autonomous:
                    self.reason = "autonomous_milestone_complete"
                else:
                    self.reason = "milestone_complete_verify_original_instruction"
                    self.reviews += 1
                    if self.reviews > 3:
                        await self.reply("当前看板步骤已完成，但尚无法确认完整要求，已结束本轮并返回跟随。", control["revision"], True)
                        return
            elif op == "idle":
                view = self.board()
                if view["execution_task_ids"]:
                    return  # Search budget exhaustion can release a focused branch this cycle.
                marker = idle_review_marker(view)
                if marker == self.idle_marker:
                    return
                self.idle_marker = marker
                self.reason = idle_review_reason(view) or "no_actionable_tasks"
            elif op == "unsupported":
                if self.continue_other_tasks(task["id"], args["reason"], keep_blocked=True):
                    return
                await self.reply("当前伙伴能力不可用：" + args["reason"], control["revision"], True)
                return
            elif op == "needs_replan":
                if self.continue_other_tasks(task["id"], "prerequisite_required:" + args["reason"]):
                    return
                self.prerequisite_replans += 1
                if self.prerequisite_replans > 3:
                    await self.reply("当前前置条件仍未满足：" + args["reason"] + "。已停止本轮并返回跟随。", control["revision"], True)
                    return
                self.previous, self.plan = self.board(), None
                self.reason = "prerequisite_required:" + args["reason"]
                self.trace("prerequisite_blocked", task_id=task["id"], **args)
            elif op == "blocked":
                # No native action was attempted. Replanning the same empty
                # resource set three times only burns tokens and mislabels it.
                details = {}
                if args["reason"] == "no_safe_unsearched_frontier":
                    perception = observed.get("perception") or {}
                    details = {"search_diagnostics": {
                        "position": (observed.get("buddy") or {}).get("position"),
                        "walkable_points": perception.get("walkable_points"),
                        "offered_points": len(perception.get("points", [])),
                        "hazards": len(perception.get("hazards", [])),
                        "paused_points": len(self.tasks.search.paused),
                        "observed_cells": len(self.tasks.search.cells),
                    }}
                self.trace("resource_blocked", task_id=task["id"], **args, available_products=products, **details)
                if self.continue_other_tasks(task["id"], args["reason"], keep_blocked=True):
                    return
                labels = {"cutgrass": "草", "twigs": "树枝", "berries": "浆果", "carrot": "胡萝卜", "cutreeds": "芦苇"}
                product = args["prefab"]
                name = labels.get(product, product)
                if autonomous:
                    message = f"附近暂时找不到{name}（{product}），我会稍后重新查看。"
                    await self.reply(message, control["revision"])
                    self.wait_for_autonomous_review("autonomous_resource_unavailable")
                    return
                if args["reason"] == "no_safe_unsearched_frontier":
                    message = f"当前感知范围内没有找到{name}（{product}），也没有可安全继续搜索的新落点。已停止本轮并返回跟随。"
                else:
                    message = f"我目前观察到的15单位范围资源中，没有可采集的{name}（{product}）。当前任务或模组未开启搜索，请靠近对应资源后重试，已返回跟随。"
                await self.reply(message, control["revision"], True)
                return
            else:
                self.waiting_at = None
                self.waiting_position = None
                self.idle_marker = None
                if self.external_review_reason not in URGENT_EXTERNAL_ALERTS:
                    self.external_review_reason = None
                action = await self.rpc("buddy_action", op=op, observation_id=packet["observation_id"], args=args)
                if op in {"deposit", "withdraw"} and self.storage_wait_marker == (task["id"], args["container_guid"]):
                    self.trace("storage_resume", task_id=task["id"], container_guid=args["container_guid"])
                    self.storage_wait_marker = None
                self.tasks.note_action(task, op, args)
                self.active = (action["id"], task["id"], task["target"]["type"])
                self.trace("action_queued", action_id=action["id"], task_id=task["id"], op=op, args=args)
                return
        if self.future is None and not self.planning_unavailable:
            self.request_plan(control, observed, self.reason)

    async def close(self):
        if self.cancel:
            self.cancel.set()
        self.executor.shutdown(wait=False, cancel_futures=True)
        if self.execute:
            try:
                packet = await self.rpc("buddy_observe")
                if packet["state"].get("ready") and (not self.external_board or self.active):
                    await self.rpc("buddy_action", op="stop", observation_id=packet["observation_id"])
            except Exception:
                pass


CompanionAgent = ActorRunner


class Ticket:
    def __init__(self, identity, context, cancel):
        self.identity, self.context, self.cancel = identity, context, cancel
        self.at = time.monotonic()
        self.event = threading.Event()
        self.result = None
        self.error = None

    def finish(self, result=None, error=None):
        self.result, self.error = result, error
        self.event.set()


class PlanningProxy:
    """Local actor requests are tickets, not independent model calls."""

    def __init__(self, host, identity):
        self.host, self.identity = host, identity

    def __call__(self, context, history, cancel):
        ticket = Ticket(self.identity, context, cancel)
        with self.host.lock:
            self.host.pending[self.identity] = ticket
        while not ticket.event.wait(.1):
            if cancel.is_set() or self.host.closed:
                raise RuntimeError("Planning superseded by newer player intent")
        if ticket.error:
            raise ticket.error
        return ticket.result


class ActorClient:
    def __init__(self, host, identity):
        self.host, self.identity = host, identity

    async def call_tool(self, name, args):
        args = {**args, "buddy_id": self.identity}
        claim = None
        if name == "buddy_action" and args.get("op") in {"collect", "work"}:
            claim = (args.get("args") or {}).get("target_guid")
            if not self.host.claim(self.identity, claim):
                raise RuntimeError("Resource is being worked by a teammate")
        try:
            result = await self.host.client.call_tool(name, args)
        except Exception:
            if claim is not None:
                self.host.release(self.identity)
            raise
        if claim is not None:
            if result.is_error:
                self.host.release(self.identity)
            else:
                self.host.claims[claim]["action_id"] = result.structured_content["id"]
        if name == "buddy_result" and not result.is_error and result.structured_content.get("status") in {"completed", "failed", "interrupted"}:
            self.host.release(self.identity, action_id=args["action_id"])
        if name == "buddy_observe" and not result.is_error:
            packet = copy.deepcopy(result.structured_content)
            observed = packet["state"].get("companion") or {}
            busy = {guid for guid, row in self.host.claims.items() if row["owner"] != self.identity and row["until"] > time.monotonic()}
            if busy:
                products = {r.get("product") for r in observed.get("nearby", []) if r.get("guid") in busy}
                for row in observed.get("work_targets", []):
                    if row.get("guid") in busy:
                        products.update(row.get("products", []))
                observed["_team_claimed_products"] = list(products)
                observed["nearby"] = [row for row in observed.get("nearby", []) if row.get("guid") not in busy]
                observed["work_targets"] = [row for row in observed.get("work_targets", []) if row.get("guid") not in busy]
                observed["resources_complete"] = False
            return SimpleNamespace(is_error=False, structured_content=packet)
        return result


class TeamRunner:
    """Unified server-side Task Executor host managing self/buddy1/buddy2 slots."""

    def __init__(self, client, planner=None, *, execute=False, log_path=None):
        self.client, self.planner, self.execute = client, planner, execute
        self.log_path = log_path
        self.events = TransportRecorder(log_path)
        self.agents = {}
        self.pending = {}
        self.lock = threading.RLock()
        self.closed = False
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="team-deepseek")
        self.future = None
        self.cancel = None
        self.inflight = {}
        self.context = None
        self.basis = None
        self.state = None
        self.history = []
        self.claims = {}
        self.member_bases = {}
        self.completed_team_tasks = set()
        self.dispatched_revision = None
        self.world_graph = WorldGraph(persist_root=DATA_DIR / "agent_memory")
        if hasattr(planner, "set_error_handler"):
            planner.set_error_handler(lambda stage, detail: self.trace("planning_detail", stage=stage, detail=detail))
        if hasattr(planner, "set_query_handler"):
            loop = asyncio.get_running_loop()

            def query(args, context, cancel):
                args = dict(args)
                identity = args.pop("buddy_id")
                actor = context["members"][identity]
                future = asyncio.run_coroutine_threadsafe(self.rpc(
                    "buddy_recipes", buddy_id=identity, **args,
                    revision=actor["interaction"]["revision"], binding_token=actor["observation"]["token"],
                ), loop)
                try:
                    for _ in range(100):
                        if cancel.is_set():
                            raise RuntimeError("Planning superseded by newer player intent")
                        try:
                            return future.result(timeout=.1)
                        except FutureTimeout:
                            continue
                    raise RuntimeError("Companion recipe query timed out")
                finally:
                    if not future.done():
                        future.cancel()

            planner.set_query_handler(query)

    def trace(self, event, **fields):
        self.events.write({"timestamp_utc": datetime.now(timezone.utc).isoformat(), "event": event,
                           "team_revision": self.basis[1] if self.basis else None, **fields})

    async def rpc(self, name, **args):
        result = await self.client.call_tool(name, args)
        if result.is_error:
            raise RuntimeError("; ".join(getattr(c, "text", "") for c in result.content)[:300])
        return result.structured_content

    def release(self, identity, action_id=None):
        self.claims = {g: r for g, r in self.claims.items() if not (r["owner"] == identity and (action_id is None or r.get("action_id") == action_id))}

    def claim(self, identity, guid):
        now = time.monotonic()
        row = self.claims.get(guid)
        if row and row["owner"] != identity and row["until"] > now:
            return False
        self.claims[guid] = {"owner": identity, "until": now + 12}
        return True

    def abort_plan(self):
        if self.cancel:
            self.cancel.set()
        if self.future:
            self.future.cancel()
        with self.lock:
            for identity, ticket in self.inflight.items():
                if not ticket.cancel.is_set():
                    self.pending.setdefault(identity, ticket)
        self.future = None
        self.inflight = {}

    def update(self, packet):
        state = packet["state"]
        roster = tuple(sorted((identity, (entry.get("companion") or {}).get("token")) for identity, entry in state["members"].items()))
        basis = (packet["session_id"], state["control"]["revision"], roster)
        if self.basis != basis:
            if self.basis and self.basis[0] != basis[0]:
                self.history = []
                self.dispatched_revision = None
            self.abort_plan()
            self.basis = basis
        self.state = state
        for identity, entry in state["members"].items():
            actor_basis = ((entry.get("companion") or {}).get("token"), entry["control"]["revision"])
            if self.member_bases.get(identity) != actor_basis:
                self.release(identity)
            self.member_bases[identity] = actor_basis
        for row in self.claims.values():
            actor = self.agents.get(row["owner"])
            if actor and actor.active and actor.active[0] == row.get("action_id") and state["members"].get(row["owner"], {}).get("ready"):
                row["until"] = time.monotonic() + 12
        self.claims = {g: r for g, r in self.claims.items() if r["until"] > time.monotonic() and r["owner"] in state["members"]}

    def build_context(self, requests):
        members = {}
        names = {r["id"]: r["name"] for r in self.state["roster"]}
        for identity, entry in self.state["members"].items():
            observed = copy.deepcopy(entry.get("companion") or {})
            runtime = self.agents.get(identity)
            members[identity] = {"name": names[identity], "interaction": copy.deepcopy(entry["control"]), "observation": observed,
                "current_task_board": runtime.board() if runtime else None, "supported_capabilities": capabilities(observed)}
            if identity in requests:
                members[identity]["replan_reason"] = requests[identity].context.get("replan_reason")
            if runtime and runtime.active:
                members[identity]["active_task_id"] = runtime.active[1]
        control = copy.deepcopy(self.state["control"])
        return {"interaction": control, "long_term_goal": control.get("goal") if control["mode"] == "autonomous" else None,
                "members": members, "request_ids": sorted(requests),
                "replan_reasons": {i: t.context.get("replan_reason") for i, t in requests.items()},
                "resource_claims": [{"guid": g, "buddy_id": r["owner"]} for g, r in self.claims.items()]}

    async def flush(self):
        if self.future and self.future.done():
            future, self.future = self.future, None
            requests, self.inflight = self.inflight, {}
            try:
                result = future.result()
                catalogs = result.get("_catalogs")
                clean = validate_team_plan(result, self.context, catalogs)
                # All assignments are checked before any actor is released to execute.
                for item in clean["assignments"]:
                    identity = item["buddy_id"]
                    ticket = requests[identity]
                    current = self.state["members"].get(identity) or {}
                    control = current.get("control", {})
                    requested = ticket.context["interaction"]
                    if (ticket.cancel.is_set() or control.get("revision") != requested["revision"]
                            or control.get("dialogue_revision") != requested.get("dialogue_revision")
                            or (current.get("companion") or {}).get("token") != ticket.context["observation"].get("token")):
                        raise RuntimeError("Planning superseded by newer player intent")
                for item in clean["assignments"]:
                    requests[item["buddy_id"]].finish(item["plan"])
                self.history = recent_exchanges([*self.history, (json.dumps(self.context, ensure_ascii=False), result.get("_assistant_message") or json.dumps(clean, ensure_ascii=False))])
                self.trace("team_plan_adopted", buddy_ids=list(requests), outcome=clean["outcome"])
            except Exception as exc:
                for ticket in requests.values():
                    ticket.finish(error=exc)
            return
        if self.future or not self.execute or self.planner is None:
            return
        with self.lock:
            requests = {}
            for identity, ticket in list(self.pending.items()):
                entry = self.state["members"].get(identity) or {}
                if ticket.cancel.is_set() or not entry.get("control", {}).get("enabled"):
                    self.pending.pop(identity, None)
                    ticket.finish(error=RuntimeError("Planning cancelled"))
                    continue
                current = entry["control"]
                requested = ticket.context["interaction"]
                if ((entry.get("companion") or {}).get("token") != ticket.context["observation"].get("token")
                        or current["revision"] != requested["revision"]
                        or current.get("dialogue_revision") != requested.get("dialogue_revision")):
                    self.pending.pop(identity, None)
                    ticket.finish(error=RuntimeError("Planning superseded"))
                    continue
                if entry.get("ready"):
                    requests[identity] = ticket
            if not requests or time.monotonic() - min(t.at for t in requests.values()) < .1:
                return
            c = self.state["control"]
            if self.dispatched_revision != c["revision"] and c.get("request_kind") in {"message", "autonomy"}:
                expected = {i for i in c["target_ids"] if self.state["members"].get(i, {}).get("control", {}).get("enabled")}
                if not expected <= set(requests):
                    return
            for identity in requests:
                self.pending.pop(identity, None)
            self.inflight = requests
        self.context = self.build_context(requests)
        self.cancel = threading.Event()
        self.future = self.executor.submit(self.planner, copy.deepcopy(self.context), list(self.history), self.cancel)
        self.dispatched_revision = self.state["control"]["revision"]
        self.trace("team_plan_requested", buddy_ids=list(requests), reasons=self.context["replan_reasons"])

    def member_positions(self):
        positions = {}
        for mid, entry in ((self.state or {}).get("members") or {}).items():
            pos = ((entry.get("companion") or {}).get("buddy") or {}).get("position") or {}
            if number(pos.get("x")) and number(pos.get("z")):
                positions[mid] = (float(pos["x"]), float(pos["z"]))
        return positions

    def _refresh_completed_team_tasks(self):
        done = set()
        for mid, agent in self.agents.items():
            board = agent.board()
            if not board:
                continue
            for task in board.get("tasks") or []:
                if task.get("status") == "done" and isinstance(task.get("id"), str):
                    done.add(f"{mid}:{task['id']}")
        self.completed_team_tasks = done

    async def cycle(self):
        packet = await self.rpc("buddy_observe")
        if packet["state"].get("team") is not True:
            raise ValueError("Companion team snapshot required; update DST Buddy Agent")
        self.update(packet)
        if self.state.get("paused"):
            return
        for identity in list(self.agents):
            if identity not in self.state["members"]:
                agent = self.agents.pop(identity)
                await agent.close()
                self.release(identity)
        for identity in self.state["members"]:
            if identity not in self.agents:
                self.agents[identity] = ActorRunner(ActorClient(self, identity), PlanningProxy(self, identity) if self.planner else None, execute=self.execute,
                                                    log_path=self.log_path, buddy_id=identity, external_board=self.planner is None,
                                                    world_graph=self.world_graph,
                                                    team_completed_provider=lambda: self.completed_team_tasks,
                                                    team_positions_provider=self.member_positions)
        self._refresh_completed_team_tasks()
        results = await asyncio.gather(*(agent.cycle() for agent in self.agents.values()), return_exceptions=True)
        self._refresh_completed_team_tasks()
        for identity, result in zip(self.agents, results):
            if isinstance(result, Exception):
                self.trace("member_cycle_error", buddy_id=identity, kind=type(result).__name__, detail=str(result)[:250])
        self.update(await self.rpc("buddy_observe"))
        await self.flush()

    async def close(self):
        self.closed = True
        self.abort_plan()
        with self.lock:
            for ticket in self.pending.values():
                ticket.finish(error=RuntimeError("Team host stopped"))
            self.pending.clear()
        await asyncio.gather(*(a.close() for a in self.agents.values()), return_exceptions=True)
        self.world_graph.save()
        self.executor.shutdown(wait=False, cancel_futures=True)


CompanionTeamAgent = TeamRunner
