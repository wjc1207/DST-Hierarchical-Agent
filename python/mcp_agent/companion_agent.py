"""Run YOUR local DeepSeek planner for YOUR server-side companion."""
from __future__ import annotations
import argparse
import asyncio
import copy
import json
import math
import os
import shutil
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.paths import PROJECT_ROOT, PYTHON_ROOT
from common.config import load_dotenv, resolve_env_path
from common.task_board import format_board
from common.task_timing import number
from mcp_agent.companion_planner import collection_prefab
from mcp_agent.companion_tasks import RETURN_ALERTS, CompanionTasks, capabilities
from mcp_agent.planning_core import recent_exchanges, validate_reply, DEFAULT_API_URL, DEFAULT_MODEL
from mcp_agent.priority_board import idle_review_reason, idle_review_marker
from mcp_server.game_bridge import check_bridge_port
from mcp_server.transport_metrics import TransportRecorder

AUTONOMY_REVIEW_SECONDS = 30


class CycleDiagnostics:
    """Bound repeated connection errors during game pause; report recovery and counts."""
    def __init__(self,agent,*,clock=time.monotonic):
        self.agent,self.clock=agent,clock;self.key=None;self.started=0.;self.last_report=0.;self.count=0;self.suppressed=0

    def error(self,exc):
        now=self.clock();key=(type(exc).__name__,str(exc)[:250])
        if key!=self.key:
            self.recovered();self.key=key;self.started=now;self.last_report=-math.inf;self.count=0
        self.count+=1
        if "disconnected/stale" in key[1] and now-self.last_report<30:
            self.suppressed+=1;return
        print(f"companion_cycle_error type={key[0]} detail={key[1]} repeats={self.count}")
        if self.agent:
            self.agent.trace("cycle_error",kind=key[0],detail=key[1],repeats=self.count,suppressed=self.suppressed)
        self.last_report=now;self.suppressed=0

    def recovered(self):
        if self.key is not None and self.agent:
            self.agent.trace("cycle_error_end",kind=self.key[0],detail=self.key[1],
                             duration_seconds=round(self.clock()-self.started,2),repeats=self.count,suppressed=self.suppressed)
        self.key=None;self.suppressed=0


class CompanionAgent:
    def __init__(self, client, planner, *, execute=False, log_path=None, buddy_id=None, external_board=False):
        self.client, self.planner, self.execute = client, planner, execute
        self.buddy_id = buddy_id
        self.external_board = external_board
        self.external_review_reason = None
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="companion-ds")
        self.future = None
        self.cancel = None
        self.basis = None
        self.dialogue_revision = None
        self.plan = self.previous = None
        self.tasks = CompanionTasks()
        self.observed = None
        self.idle_marker = None
        self.history = []
        self.active = None
        self.reason = "player_message"
        self.pending_context = None
        self.failures = 0
        self.planning_unavailable = False
        self.retry_at = 0.
        self.finished_basis = None
        self.reviews = 0
        self.waiting_at = None
        self.waiting_position = None
        self.events = TransportRecorder(log_path)
        self.last_failure = ("action", "unknown")
        self.observation_marker = None
        self.prerequisite_replans = 0
        self.storage_wait_marker = None
        if hasattr(planner, "set_error_handler"):
            planner.set_error_handler(lambda stage, detail: self.trace("planning_detail", stage=stage, detail=detail))
        if hasattr(planner, "set_query_handler"):
            loop = asyncio.get_running_loop()
            def query(args, context, cancel):
                future = asyncio.run_coroutine_threadsafe(self.rpc("buddy_recipes", **args,
                    revision=context["interaction"]["revision"], binding_token=context["observation"]["token"]), loop)
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
                           **({"buddy_id":self.buddy_id} if self.buddy_id else {}), **fields})

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
        control, observed = state["control"], state.get("companion") or {}
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
                self.tasks = CompanionTasks()
                self.history = []
            self.plan, self.active = None, None
            self.basis = basis
            self.reason = "autonomous_goal" if autonomous else "player_message"
            self.external_review_reason = None
            self.failures, self.retry_at = 0, 0.
            self.planning_unavailable = False
            self.finished_basis, self.reviews, self.waiting_at = None, 0, None
            self.waiting_position = None
            self.idle_marker = None
            self.prerequisite_replans = 0
            self.storage_wait_marker = None
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
        if self.active:
            action = await self.rpc("buddy_result", action_id=self.active[0])
            if action["status"] not in {"completed", "failed", "interrupted"}:
                return
            _, task_id, kind = self.active
            self.trace("action_result", action_id=self.active[0], status=action["status"], reason=action.get("reason"), task_id=task_id)
            self.active = None
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
                if action["status"] == "completed" and action.get("reason") in {
                        "one_item_transferred", "one_item_dropped", "inventory_increased", "equipped", "unequipped",
                        "work_progressed", "work_finished", "new_structure_observed", "arrived", "building_arrived"}:
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
                self.waiting_at,self.waiting_position = None,None
                self.trace("plan_adopted", tasks=plan["tasks"])
                if dialogue_pending:
                    self.failures, self.retry_at = 0, 0.
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
        if self.plan:
            if self.future is None and not self.planning_unavailable and not self.external_board:
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
            if op in {"awaiting_items","waiting_resource"}:
                return  # Player gives voluntarily; no polling-driven model calls.
            if op == "waiting":
                point = (observed.get("buddy") or {}).get("position") or {}
                position = tuple(point[k] for k in ("x", "z")) if all(number(point.get(k)) for k in ("x", "z")) else None
                progressed = position is not None and (self.waiting_position is None or
                    math.hypot(position[0]-self.waiting_position[0],position[1]-self.waiting_position[1]) >= .5)
                now = time.monotonic()
                if self.waiting_at is None or progressed:
                    self.waiting_at,self.waiting_position = now,position
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
                if self.external_review_reason not in RETURN_ALERTS.values():
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


async def run(args, planner):
    from mcp import Client, StdioServerParameters
    check_bridge_port(args.port)
    server_args = [str(PYTHON_ROOT / "mcp_server" / "companion_server.py"),
                   "--config", str(args.config.resolve()), "--port", str(args.port)]
    server_args += ["--poll", str(args.poll)]
    parameters = StdioServerParameters(command=sys.executable, args=server_args, cwd=str(PROJECT_ROOT))
    async with Client(parameters, read_timeout_seconds=10) as client:
        from mcp_agent.planning_client import PlanningClient
        planning = PlanningClient(client, companion_model=planner) if planner else None
        cycle = 0
        diagnostics = CycleDiagnostics(None)
        try:
            while not args.max_cycles or cycle < args.max_cycles:
                cycle += 1
                try:
                    if planning:
                        await planning.cycle()
                    else:
                        result = await client.call_tool("buddy_observe", {})
                        if result.is_error:
                            raise RuntimeError("; ".join(getattr(row, "text", "") for row in result.content))
                    diagnostics.recovered()
                except Exception as exc:
                    diagnostics.error(exc)
                await asyncio.sleep(args.poll)
        finally:
            if planning:
                planning.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", type=Path)
    parser.add_argument("--config", type=Path, default=PROJECT_ROOT / "logs/companion_bridge.json")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--poll", type=float, default=.2)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--max-cycles", type=int, default=0)
    args = parser.parse_args()
    if not .1 <= args.poll <= 1 or not 1 <= args.port <= 65535 or args.max_cycles < 0:
        parser.error("Invalid poll, port or max-cycles")
    planner = None
    if args.execute:
        if args.env or not os.environ.get("DEEPSEEK_API_KEY"):
            load_dotenv(resolve_env_path(args.env))
        key = os.environ.get("DEEPSEEK_API_KEY")
        if not key:
            parser.error("DEEPSEEK_API_KEY is required on THIS player's computer")
        from mcp_agent.companion_team_planner import CompanionTeamPlanner
        planner = CompanionTeamPlanner(os.getenv("DEEPSEEK_API_URL", DEFAULT_API_URL), key,
                                       os.getenv("DEEPSEEK_MODEL", DEFAULT_MODEL))
    print("Companion mode: server executes; this computer alone pays for its model calls.")
    try:
        asyncio.run(run(args, planner))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
