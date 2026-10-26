"""DeepSeek/JEV autonomous host; ALL game reads and writes go through MCP."""
from __future__ import annotations

import argparse
import asyncio
import copy
import os
import queue
import sys
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if __package__ in {None, ""}:
    sys.path.insert(0, str(PROJECT_ROOT))

from common import timing_log as timing
from mcp_agent.daily_planner import DailyPlanner, DEFAULT_API_URL as DS_URL, DEFAULT_MODEL as DS_MODEL
from mcp_agent.navigation_intents import ExplorationExecutor, dispatch_for_navigation
from common.config import load_dotenv, resolve_env_path
from mcp_agent.jev_client import call_jev, DEFAULT_API_URL, DEFAULT_MODEL
from common.game_rules import survival_dangers, SAFE_FOOD_PREFABS
from mcp_server.game_bridge import TERMINAL, finite_time, BridgePortUnavailable, check_bridge_port
from mcp_agent.action_sequence import SequenceExecutor, is_sequence
from mcp_agent.priority_board import PriorityBoardExecutor, is_priority_board, task_signature


class MCPRejected(RuntimeError):
    """A tool rejected stale/unsafe work. Reobserve, never bypass validation."""


class SessionChanged(RuntimeError):
    pass


class IncompatibleBridge(RuntimeError):
    pass


class PlayerDead(RuntimeError):
    pass


class MCPAgent:
    def __init__(self, client, decide, *, planner=None, execute=False, poll=0.25, navigator=None):
        self.client, self.decide, self.planner = client, decide, planner
        self.execute, self.poll = execute, poll
        self.navigator = navigator or ExplorationExecutor()
        self.worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mcp-jev")
        self.pending = None
        self.session = None
        self.player_guid = None
        self.packet = None
        self.plan = None
        self.uploaded = None
        self.uploaded_source = None
        self.board_revision = None
        self.auto_enabled = False
        self.board_acked = False
        self.active_id = None
        self.active_started = None
        self.seen_actions = set()
        self.status_marks = set()
        self.last_terminal = None
        self.request_index = 0
        self.request_events = queue.SimpleQueue()
        self.request_timings = OrderedDict()
        self.pending_request_id = None
        self.sequence = getattr(planner, "sequence", None) or SequenceExecutor()
        self.sequence_actions = {}
        self.goals = getattr(planner, "goals", None) or PriorityBoardExecutor()
        self.goal_actions = {}
        self.deferred_safety = None
        self.goal_gap_marker = None
        self.goal_light = None

    def measured_decision(self, request_id, submitted_at, metadata, state, criteria, plan):
        # ContextVars are intentionally NOT copied from the async host into this
        # thread. Parallel spans need their own stack and elapsed-time totals.
        queue_ms = max(0, (time.perf_counter()-submitted_at)*1000)
        try:
            with timing.CycleTrace(0, emit_console=False) as trace:
                timing.annotate(kind="jev_worker", request_id=request_id, **metadata)
                with timing.span("decision.worker_call"):
                    return self.decide(state, criteria, plan)
        finally:
            self.request_events.put({"request_id": request_id, "finished_at": time.perf_counter(),
                                     "queue_ms": queue_ms, "trace": trace.record})

    def drain_request_timings(self):
        while True:
            try:
                record = self.request_events.get_nowait()
            except queue.Empty:
                return
            identity = record["request_id"]
            self.request_timings[identity] = record
            while len(self.request_timings) > 64:
                self.request_timings.popitem(last=False)
            timing.event("decision.worker_completed", request_id=identity,
                         worker_queue_ms=round(record["queue_ms"], 3),
                         worker_timing=record["trace"])

    async def rpc(self, name, **arguments):
        with timing.span("mcp.request", tool=name):
            result = await self.client.call_tool(name, arguments)
        if result.is_error:
            detail = "; ".join(getattr(block, "text", "") for block in result.content)
            raise MCPRejected(detail[:350] or f"MCP tool rejected: {name}")
        if not isinstance(result.structured_content, dict):
            raise RuntimeError(f"MCP tool {name} did not return structured output")
        return result.structured_content

    def record(self, record, state):
        identity, choice = record.get("action_id"), record.get("choice")
        if not identity or not choice or identity in self.seen_actions or record.get("status") not in TERMINAL:
            return
        self.seen_actions.add(identity)
        if len(self.seen_actions) > 4096:
            # Keep bounded memory; old events have already left Lua's 64-entry ring.
            self.seen_actions = {e.get("action_id") for e in state.get("bridge", {}).get("events", [])}
            self.seen_actions.add(identity)
        status, reason = record["status"], record.get("reason", "")
        details = dict(record.get("timing") or {})
        for key in ("started_at", "confirmed_started_at", "finished_at", "handoff_gap_ms"):
            if record.get(key) is not None:
                details.setdefault("lua_" + key, record[key])
        for key in ("previous_action_id", "start_source"):
            if record.get(key) is not None:
                details.setdefault(key, record[key])
        started, finished = details.get("lua_started_at"), details.get("lua_finished_at")
        if finite_time(started) and finite_time(finished) and finished >= started:
            details["lua_execution_ms"] = round((finished - started) * 1000, 3)
        sampled = state.get("bridge", {}).get("sampled_at")
        if finite_time(sampled) and finite_time(finished) and sampled >= finished:
            details.setdefault("lua_finish_to_exchange_ms", round((sampled-finished)*1000, 3))
        transport = state.get("bridge", {}).get("transport") or {}
        rtt = transport.get("last_round_trip_ms") if isinstance(transport, dict) else None
        if finite_time(rtt):
            details["lua_previous_http_round_trip_ms"] = rtt
        timing.event("action.terminal_observed", action_id=identity, choice=choice,
                     status=status, reason=reason, **details)
        self.last_terminal = (identity, time.monotonic())
        owner = self.sequence_actions.pop(identity, None)
        if owner and owner[0] == self.sequence.identity and (status == "interrupted" or owner[1] not in self.sequence.completed):
            self.sequence.on_result(status, reason)
        goal_owner = self.goal_actions.pop(identity, None)
        if goal_owner:
            task_id, signature, action = goal_owner
            if self.goals.records.get(task_id, {}).get("signature") == signature:
                if is_priority_board(self.plan):
                    self.goals.view(self.plan, state)
                self.goals.on_result(task_id, choice, action, status, reason, self.navigator, state)
        if details.get("lua_handoff_gap_ms") is not None:
            print(f"mcp_handoff choice={choice} accepted_gap={details['lua_handoff_gap_ms']/1000:.3f}s "
                  f"execution={details.get('lua_execution_ms', 0)/1000:.3f}s")
        if self.planner:
            if isinstance(self.planner, DailyPlanner):
                self.planner.record_action(state, choice, "executed" if status == "completed" else status, reason)
            else:
                self.planner.record_action(state, choice, "executed" if status == "completed" else status)
        if choice.startswith(("explore_", "travel_region_", "navigate_resource_")):
            reason = {"navigation_yield": "destination_reached", "navigation_path_became_unsafe": "path_unavailable",
                      "verification_timeout": "movement_stalled", "light_changed": "phase_changed",
                      "critical_hunger": "critical_hunger_with_food"}.get(reason, reason)
            self.navigator.on_event({"reason": reason}, state)
        else:
            self.navigator.after_local_action(choice)
        print(f"lua_action choice={choice} status={status} reason={reason}")

    def record_status(self, record):
        if not record or not record.get("action_id") or record.get("status") != "running":
            return
        details = dict(record.get("timing") or {})
        for key in ("started_at", "confirmed_started_at"):
            if record.get(key) is not None:
                details.setdefault("lua_" + key, record[key])
        if record.get("start_source") is not None:
            details.setdefault("start_source", record["start_source"])
        confirmed = details.get("lua_confirmed_started_at") is not None
        key = (record["action_id"], confirmed)
        if key in self.status_marks:
            return
        if len(self.status_marks) >= 512:
            self.status_marks.clear()
        self.status_marks.add(key)
        timing.event("action.native_start_observed" if confirmed else "action.lua_accept_observed",
                     action_id=record["action_id"], choice=record.get("choice"), **details)

    async def observe(self, refresh=False):
        self.drain_request_timings()
        with timing.span("mcp.observe"):
            packet = await self.rpc("get_available_actions", **({"refresh": True} if refresh else {}))
        state = packet.get("state")
        if not isinstance(state, dict) or state.get("bridge", {}).get("protocol", 0) < 4:
            raise IncompatibleBridge("Automatic MCP mode requires Mod/server 1.8.6+ (protocol 4); update Python files and scripts/, then re-enter the world")
        session = packet["session_id"]
        if self.session is not None and self.session != session:
            raise SessionChanged("Game world/player session changed; restart the autonomous host")
        player_guid = state.get("player", {}).get("guid")
        if self.player_guid is not None and self.player_guid != player_guid:
            raise SessionChanged("Game player changed; restart the autonomous host")
        self.player_guid = player_guid
        self.session, self.packet = session, packet
        timing.annotate(game_session_id=session, state_version=packet.get("state_version"))
        self.drain_request_timings()
        timing.annotate(mcp_state_age_ms=packet.get("state_age_ms"))
        timing.annotate(mcp_connection_age_ms=packet.get("connection_age_ms"))
        status = packet.get("action_status") or {}
        self.record_status(status)
        self.record_status(state.get("bridge", {}).get("action_report"))
        for event in state.get("bridge", {}).get("events", []):
            self.record({**event, "timing": status.get("timing", {})}
                        if event.get("action_id") == status.get("action_id") else event, state)
        if status.get("status") in TERMINAL:
            self.record(status, state)
        with timing.span("planner.update"):
            self.plan = self.planner.update(state) if self.planner else None
        if state["player"].get("vitals", {}).get("dead"):
            raise PlayerDead("player is dead")
        return packet

    @staticmethod
    def board_id(plan):
        return plan.get("board_id") if plan and plan.get("kind") == "task_board" else None

    async def sync_board(self, plan, enabled):
        board = plan or self.uploaded
        if not board or board.get("kind") != "task_board":
            return
        source = self.board_id(board)
        unchanged = source == self.uploaded_source and enabled == self.auto_enabled
        if unchanged and self.board_acked:
            return
        began = time.monotonic()
        timing.event("board.sync_requested", source_board=source, auto_execute=enabled)
        # Get a fresh short-lived observation, not an ID saved before model inference.
        packet = await self.observe()
        if packet.get("state_age_ms", 0) > 1000:
            packet = await self.observe(refresh=True)
        if not unchanged:
            result = await self.rpc("submit_task_board", board=board,
                                    observation_id=packet["observation_id"], auto_execute=enabled)
            self.uploaded, self.uploaded_source = copy.deepcopy(board), source
            self.auto_enabled, self.board_revision = enabled, result["board_revision"]
            self.board_acked = False
        deadline = time.monotonic() + 4
        while True:
            packet = await self.observe()
            bridge = packet["state"]["bridge"]
            if (bridge.get("board_revision") == self.board_revision
                    and bridge.get("board_auto_execute") == enabled
                    and (enabled or not bridge.get("executor_busy"))):
                self.board_acked = True
                timing.event("board.sync_acknowledged", source_board=source, auto_execute=enabled,
                             elapsed_ms=round((time.monotonic()-began)*1000, 3))
                return
            if time.monotonic() >= deadline:
                raise MCPRejected("Lua has not acknowledged the task board; no competing action sent")
            await asyncio.sleep(self.poll)

    async def wait_action(self):
        with timing.span("action.execute"):
            while True:
                packet = await self.observe()  # Poll planning while Lua, not Python, works.
                # One atomic read supplies both facts and lifecycle. Avoid the old
                # get-status -> observe race, which added a poll after completion.
                result = packet.get("action_status") or {}
                if result.get("action_id") != self.active_id:
                    result = await self.rpc("get_action_status", action_id=self.active_id)
                if result["status"] in TERMINAL:
                    self.record(result, packet["state"])
                    self.active_id = None
                    timing.annotate(outcome="executed" if result["status"] == "completed" else result["status"],
                                    action_reason=result.get("reason"))
                    return
                owner = self.goal_actions.get(self.active_id)
                if owner and owner[2].get("kind") == "explore" and "navigation_targets" in owner[2] \
                        and result["status"] != "cancel_requested":
                    reason = self.goals.navigation_review_reason(self.plan, packet["state"], *owner[:2],
                                                                 owner[2]["navigation_targets"])
                    if reason:
                        timing.event("navigation.review_requested", reason=reason, action_id=self.active_id)
                        print(f"navigation_review reason={reason}")
                        await self.rpc("cancel_action", action_id=self.active_id)
                if time.monotonic() - self.active_started > 140:
                    await self.rpc("cancel_action", action_id=self.active_id)
                    raise RuntimeError("Native action exceeded its bound; cancellation requested, awaiting confirmation")
                await asyncio.sleep(self.poll)

    async def perform(self, choice, packet, sequence_step=None, goal_task=None):
        timing.annotate(action=choice, current_action=choice)
        if not self.execute:
            print(f"dry_run choice={choice}: no game command sent")
            timing.annotate(outcome="dry_run")
            return
        timing.event("action.command_submitted", choice=choice,
                     previous_action_id=self.last_terminal[0] if self.last_terminal else None,
                     host_observed_to_submit_ms=round((time.monotonic()-self.last_terminal[1])*1000, 3)
                     if self.last_terminal else None)
        arguments = {"choice": choice, "observation_id": packet["observation_id"]}
        if goal_task is not None and packet["actions"][choice]["kind"] == "explore" and is_priority_board(self.plan):
            arguments["navigation_targets"] = self.goals.navigation_targets(self.plan, packet["state"])
            timing.annotate(navigation_target_count=len(arguments["navigation_targets"]))
        result = await self.rpc("start_action", **arguments)
        self.active_id, self.active_started = result["action_id"], time.monotonic()
        if sequence_step is not None:
            self.sequence_actions[self.active_id] = (self.sequence.identity, sequence_step)
        if goal_task is not None:
            owned_action = copy.deepcopy(packet["actions"][choice])
            if "navigation_targets" in arguments:
                owned_action["navigation_targets"] = arguments["navigation_targets"]
            self.goal_actions[self.active_id] = (goal_task["id"], task_signature(goal_task), owned_action)
        timing.event("action.command_queued", choice=choice, action_id=self.active_id)
        print(f"mcp_action_started choice={choice} id={self.active_id}")
        await self.wait_action()

    async def choose_local(self, packet, handoff="local"):
        state = packet["state"]
        local = {k: a for k, a in packet["actions"].items() if a["kind"] != "explore"}
        criteria = {k: a.get("description", k) for k, a in local.items()}
        if set(local) == {"wait"} or not local:
            return "wait" if local else None, packet
        # Preserve the observation that offered this choice/target. Fresh polls
        # are for lifecycle checks, not for silently retargeting a model answer.
        source_packet = packet
        # One bounded request at a time. An abandoned response never executes later.
        if self.pending is not None and not self.pending.done():
            await asyncio.sleep(self.poll)
            return None, await self.observe()
        with timing.span("decision.prepare_request"):
            request_state, request_plan = copy.deepcopy(state), copy.deepcopy(self.plan)
            if is_priority_board(request_plan):
                request_plan["handoff_reason"] = handoff
        self.request_index += 1
        self.pending_request_id = f"{timing.RUN_ID}:{self.request_index}"
        metadata = {"game_session_id": self.session, "state_version": packet.get("state_version"),
                    "after_action_id": self.last_terminal[0] if self.last_terminal else None}
        timing.annotate(jev_request_id=self.pending_request_id)
        submitted_at = time.perf_counter()
        self.pending = self.worker.submit(self.measured_decision, self.pending_request_id, submitted_at,
                                          metadata, request_state, criteria, request_plan)
        timing.event("decision.submitted", request_id=self.pending_request_id, **metadata)
        started = time.monotonic()
        with timing.span("decision.request"):
            while not self.pending.done():
                await asyncio.sleep(self.poll)
                packet = await self.observe()
            self.drain_request_timings()
            measured = self.request_timings.get(self.pending_request_id)
            if measured:
                timing.event("decision.result_observed", request_id=self.pending_request_id,
                             result_ready_to_notice_ms=round(max(0, (time.perf_counter()-measured["finished_at"])*1000), 3))
            result = self.pending.result()
        await self.observe()
        choice = result["answer"]["choice"]
        if choice not in criteria:
            raise RuntimeError("JEV returned a choice outside the requested options")
        seconds = time.monotonic() - started
        timing.annotate(confidence=float(result["answer"].get("confidence", 0)), decision_time=seconds)
        print(f"model={result['response'].get('model', 'jev')} choice={choice} "
              f"confidence={float(result['answer'].get('confidence', 0)):.3f} decision_time={seconds:.2f}s")
        # Lua validates current feasibility. Python only checks that the answer
        # belonged to this request; the original observation retains its TTL.
        return choice, source_packet

    async def cycle(self):
        packet = await self.observe()
        state = packet["state"]
        timing.annotate(transport="mcp", execution_owner="lua", day=state["world"].get("day"),
                        phase=state["world"].get("phase"), task_board_id=self.board_id(self.plan))
        if self.active_id:
            await self.wait_action()
            return False
        if is_priority_board(self.plan):
            # New host-side scheduling must not race a previously uploaded Lua board.
            if self.execute and self.uploaded and not self.board_acked:
                await self.sync_board(None, self.auto_enabled)
            if self.execute and self.auto_enabled:
                await self.sync_board(None, False)
                packet = await self.observe()
            if is_priority_board(self.plan):
                await self.run_priority_board(packet)
                return False
        if is_sequence(self.plan):
            # Old local boards must release ownership before a new sequence.
            if self.execute and self.uploaded and not self.board_acked:
                await self.sync_board(None, self.auto_enabled)
            if self.execute and self.auto_enabled:
                await self.sync_board(None, False)
                packet = await self.observe()
            if is_sequence(self.plan):
                await self.run_sequence_step(packet)
                return False
        # A prior pause acknowledgement may have timed out. Finish that handshake
        # before considering either manual work or re-enabling a board.
        if self.execute and self.uploaded and not self.board_acked:
            await self.sync_board(None, self.auto_enabled)
            packet = await self.observe()
        if self.execute and self.board_id(self.plan):
            if self.uploaded_source != self.board_id(self.plan):
                await self.sync_board(self.plan, True)
            elif not self.auto_enabled and packet["state"]["bridge"].get("board_ready"):
                await self.sync_board(self.plan, True)
            if self.auto_enabled:
                timing.annotate(decision_source="board_auto")
                with timing.span("action.board_run"):
                    while True:
                        packet = await self.observe()
                        b = packet["state"]["bridge"]
                        if self.board_id(self.plan) != self.uploaded_source or b.get("board_paused"):
                            break
                        if not b.get("executor_busy") and not b.get("board_ready"):
                            break
                        await asyncio.sleep(self.poll)
                await self.sync_board(None, False)
                packet = await self.observe()
                if self.board_id(self.plan) and self.board_id(self.plan) != self.uploaded_source:
                    return False
        elif self.execute and self.auto_enabled:
            await self.sync_board(None, False)
            packet = await self.observe()
        return await self.run_local_control(packet, planning_gap=self.planner is not None and self.plan is None)

    def board_can_resume(self, packet):
        return (is_priority_board(self.plan) and
                self.goals.choose(self.plan, packet["state"], packet["actions"], self.navigator) is not None)

    def gap_packet(self, packet):
        # Temporary control must respect exact-choice failure pauses too.
        paused = {(choice, guid) for (_, choice, guid), until in self.goals.paused.items()
                  if until > time.monotonic()}
        return {**packet, "actions": {k: a for k, a in packet["actions"].items()
                                     if (k, a.get("guid")) not in paused}}

    async def run_local_control(self, packet, *, planning_gap=False):
        """Existing no-plan control also fills gaps without dropping future board goals."""
        if packet["state"]["bridge"].get("executor_busy"):
            timing.annotate(outcome="skipped", decision_source="native_busy")
            await asyncio.sleep(self.poll)
            return False
        # Poll cached control while work/models are running. Demand a new full
        # snapshot only before another actionable decision if the cache is old.
        # A stable wait-only night does not upload full facts on each idle cycle.
        if packet.get("state_age_ms", 0) > 1000 and set(packet["actions"]) - {"wait"}:
            packet = await self.observe(refresh=True)
        if planning_gap and self.board_can_resume(packet):
            timing.annotate(decision_source="goal_resume", outcome="skipped")
            return False
        if planning_gap:
            packet = self.gap_packet(packet)
        actions = packet["actions"]
        navigation = self.navigator.choose(packet["state"], dispatch_for_navigation(actions, packet["state"]),
                                           None if planning_gap else self.plan)
        local = {k: a for k, a in actions.items() if a["kind"] not in {"explore", "wait"}}
        if navigation and (not local or (not planning_gap and not self.navigator.handoff)):
            choice = navigation[0]
            timing.annotate(decision_source="gap_navigation" if planning_gap else "exploration_auto",
                            exploration_strategy=self.navigator.strategy)
        else:
            timing.annotate(decision_source="jev_planning_gap" if planning_gap else "jev")
            choice, packet = await self.choose_local(packet, handoff="planning_gap" if planning_gap else "local")
            if planning_gap and self.board_can_resume(self.packet):
                timing.event("decision.discarded", choice=choice, reason="task_board_actionable")
                timing.annotate(decision_source="goal_resume", outcome="skipped")
                return False
            if choice is None:
                await asyncio.sleep(self.poll)
                return False
            if choice == "wait":
                # Navigation is a separate executor choice, not the model's
                # original target, and must use its own fresh observation.
                packet = await self.observe()
                if planning_gap and self.board_can_resume(packet):
                    timing.annotate(decision_source="goal_resume", outcome="skipped")
                    return False
                if planning_gap:
                    packet = self.gap_packet(packet)
                self.navigator.after_local_action("wait")
                navigation = self.navigator.choose(packet["state"], dispatch_for_navigation(packet["actions"], packet["state"]),
                                                   None if planning_gap else self.plan)
                if navigation:
                    choice = navigation[0]
                    timing.annotate(decision_source="gap_navigation" if planning_gap else "exploration_auto", jev_local_choice="wait",
                                    exploration_strategy=self.navigator.strategy)
                else:
                    timing.annotate(decision_source="executor_idle")
                    await asyncio.sleep(self.poll)
                    return False
        timing.event("decision.ready", choice=choice)
        await self.perform(choice, packet)
        return False

    async def run_sequence_step(self, packet):
        if packet["state"]["bridge"].get("executor_busy"):
            timing.annotate(outcome="skipped", decision_source="native_busy")
            await asyncio.sleep(self.poll)
            return
        if packet.get("state_age_ms", 0) > 1000:
            packet = await self.observe(refresh=True)
        if not is_sequence(self.plan):
            return
        state, actions = packet["state"], packet["actions"]
        view = self.sequence.view(self.plan, state)
        if view["status"] != "active":
            timing.annotate(outcome=view["status"], decision_source="sequence", sequence_reason=view.get("reason"))
            await asyncio.sleep(self.poll)
            return
        step_id = view["current_step_id"]
        timing.annotate(sequence_id=self.plan.get("sequence_id"), sequence_step_id=step_id,
                        sequence_step_index=view["step_index"], decision_source="sequence")
        if await self.handle_safety(packet, self.sequence, "sequence_safety"):
            return
        choice = self.sequence.choose(self.plan, state, actions, self.navigator)
        if choice is None:
            print(f"action_sequence_blocked step={step_id} reason={self.sequence.reason}")
            timing.annotate(outcome="failed", sequence_reason=self.sequence.reason)
            # Observe once to publish verified failure and launch replanning;
            # never substitute a later step or ask JEV to override the sequence.
            await self.observe()
            await asyncio.sleep(self.poll)
            return
        print(f"action_sequence_step index={view['step_index'] + 1}/{len(view['steps'])} id={step_id} choice={choice}")
        await self.perform(choice, packet, sequence_step=step_id)

    async def handle_safety(self, packet, owner, source, *, light_changed=False):
        state, actions = packet["state"], packet["actions"]
        reasons = survival_dangers(state)
        forced = light_changed or owner.needs_safety_decision
        if not reasons and not forced:
            self.deferred_safety = None
            return False
        remedies = tuple(sorted(k for k, a in actions.items()
            if a["kind"] in {"eat_safe_food", "cook_food", "craft_torch", "equip_torch",
                             "unequip_torch", "build_campfire", "flee_from_nearest_hostile"}
            or a["kind"] == "collect" and a.get("product") in {*SAFE_FOOD_PREFABS, "petals"}))
        vitals = state["player"].get("vitals", {})
        bands = tuple((name, int(max(0, vitals[name] / vitals[name + "_max"]) * 10))
                      for name in ("health", "hunger", "sanity") if "critical_" + name in reasons)
        hostiles = tuple(sorted(str(e.get("guid", e.get("prefab", "unknown")))
                                for e in state.get("nearby", []) if e.get("activeThreat")))
        signature = (tuple(sorted(reasons)), remedies, bands, hostiles)
        # A model-selected wait acknowledges this situation. Do not ask again
        # merely because low sanity persists without any new available remedy.
        # Native damage/light/threshold events always override this acknowledgement.
        if not forced and signature == self.deferred_safety:
            return False
        if isinstance(owner, PriorityBoardExecutor):
            owner.pause_accounting()
        timing.annotate(decision_source=source, safety_reasons=sorted(reasons))
        print(f"jev_safety reasons={','.join(sorted(reasons)) or 'native_or_light_change'}")
        choice, offered = await self.choose_local(packet, handoff="safety")
        self.deferred_safety = signature if choice in {None, "wait"} else None
        if choice is not None:
            owner.needs_safety_decision = False
            await self.perform(choice, offered)
        return True

    async def run_priority_board(self, packet):
        if packet["state"]["bridge"].get("executor_busy"):
            timing.annotate(outcome="skipped", decision_source="native_busy")
            await asyncio.sleep(self.poll)
            return
        if packet.get("state_age_ms", 0) > 1000:
            packet = await self.observe(refresh=True)
        if not is_priority_board(self.plan):
            return
        state, actions = packet["state"], packet["actions"]
        fire = any(e.get("prefab") in {"campfire", "firepit"} and "fire" in e.get("tags", []) and e.get("distance", 999) <= 8 for e in state.get("nearby", []))
        light = (state["world"].get("phase"), fire)
        light_changed = self.goal_light is not None and (light[0] != self.goal_light[0]
                        or light[0] == "night" and light[1] != self.goal_light[1])
        self.goal_light = light
        if await self.handle_safety(packet, self.goals, "goal_safety", light_changed=light_changed):
            return
        selected = self.goals.choose(self.plan, state, actions, self.navigator)
        if selected:
            task, choice, action = selected
            self.goal_gap_marker = None
            # New resources may reschedule board work, but never request an
            # off-board model decision. JEV owns only safety and planning gaps.
            timing.annotate(decision_source="goal_navigation" if action.get("kind") == "explore" else "goal_local",
                            goal_task_id=task["id"], task_board_id=self.plan.get("board_id"),
                            exploration_strategy=self.navigator.strategy if action.get("kind") == "explore" else None)
            print(f"task_board_action task={task['id']} priority={task['priority']} choice={choice} "
                  f"reason={self.navigator.strategy if action.get('kind') == 'explore' else 'visible_goal'}")
            await self.perform(choice, packet, goal_task=task)
            return
        self.goals.pause_accounting()
        reason = "no_tasks" if not self.plan.get("tasks") else self.goals.reason or "no_feasible_action_or_route"
        marker = (self.board_id(self.plan), reason)
        first_gap = marker != self.goal_gap_marker
        self.goal_gap_marker = marker
        if isinstance(self.planner, DailyPlanner):
            with timing.span("planner.update"):
                self.plan = self.planner.request_review(state, reason, immediate=first_gap)
        if first_gap:
            print(f"task_board_handoff reason={reason} controller=jev_planning_gap")
        timing.annotate(goal_reason=reason)
        await self.run_local_control(packet, planning_gap=True)

    async def shutdown(self):
        self.drain_request_timings()
        if self.pending:
            self.pending.cancel()
        self.worker.shutdown(wait=False, cancel_futures=True)
        if not self.execute:
            return
        if self.active_id:
            await self.rpc("cancel_action", action_id=self.active_id)
            while True:
                result = await self.rpc("get_action_status", action_id=self.active_id)
                if result["status"] in TERMINAL:
                    self.active_id = None
                    break
                await asyncio.sleep(self.poll)
        if self.auto_enabled and not self.active_id:
            await self.sync_board(None, False)


async def run(args, decide, planner):
    # Preflight is for a clear host error. The child still performs an atomic
    # exclusive bind, so a competing launch between these calls cannot share it.
    check_bridge_port(args.port)
    from mcp import Client, StdioServerParameters
    root = PROJECT_ROOT
    parameters = StdioServerParameters(command=sys.executable, args=[str(root / "mcp_server" / "mcp_server.py"),
        "--config", str(args.config.resolve()), "--port", str(args.port)], cwd=str(root))
    async with Client(parameters, read_timeout_seconds=5) as client:
        agent = MCPAgent(client, decide, planner=planner, execute=args.execute, poll=args.poll)
        cycle = 0
        failures = 0
        try:
            while True:
                cycle += 1
                with timing.CycleTrace(cycle, args.timing_log, emit_console=False) as trace:
                    try:
                        terminal = await agent.cycle()
                        failures = 0
                    except PlayerDead:
                        print("goal_failed: player is dead; control loop stopped")
                        timing.annotate(outcome="terminal")
                        terminal = True
                    except MCPRejected as exc:
                        print(f"mcp_reobserve reason={exc}")
                        timing.annotate(outcome="skipped")
                        terminal = False
                        await asyncio.sleep(args.poll)
                    except (SessionChanged, IncompatibleBridge):
                        raise
                    except Exception as exc:
                        if not args.loop:
                            raise
                        failures += 1
                        delay = min(8, 2 ** min(failures - 1, 3))
                        print(f"mcp_cycle_error type={type(exc).__name__} retrying_in={delay:.1f}s")
                        timing.annotate(outcome="error", error_type=type(exc).__name__)
                        terminal = False
                        with timing.span("loop.retry_backoff"):
                            await asyncio.sleep(delay)
                print(f"mcp_cycle cycle={cycle} outcome={trace.record['outcome']} "
                      f"source={trace.record.get('decision_source', 'observe')} "
                      f"jev={trace.record['totals'].get('decision.request', {}).get('total_ms', 0)/1000:.3f}s "
                      f"total={trace.record['total_ms']/1000:.3f}s")
                if terminal or not args.loop or (args.max_cycles and cycle >= args.max_cycles):
                    return
        finally:
            try:
                await asyncio.wait_for(agent.shutdown(), timeout=3)
            except (Exception, asyncio.CancelledError):
                print("mcp_stop_pending: server closes; Lua stops residual work when its lease expires")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", type=Path)
    parser.add_argument("--config", type=Path, default=PROJECT_ROOT / "logs" / "mcp_bridge.json")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--no-planner", action="store_true")
    parser.add_argument("--poll", type=float, default=0.25)
    parser.add_argument("--max-cycles", type=int, default=0)
    parser.add_argument("--timing-log", type=Path, default=PROJECT_ROOT / "logs" / "mcp_timing.jsonl")
    args = parser.parse_args()
    if not 0.1 <= args.poll <= 2 or args.max_cycles < 0 or not 1 <= args.port <= 65535:
        parser.error("--poll must be 0.1..2 seconds; --max-cycles must be nonnegative; --port must be 1..65535")
    load_dotenv(resolve_env_path(args.env))
    key = os.environ.get("JEV_API_KEY") or os.environ.get("TYPESAFE_API_KEY")
    if not key:
        parser.error("JEV_API_KEY is missing")
    api_url, model = os.environ.get("JEV_API_URL", DEFAULT_API_URL), os.environ.get("JEV_MODEL", DEFAULT_MODEL)
    planner = DailyPlanner(os.environ["DEEPSEEK_API_KEY"], os.environ.get("DEEPSEEK_API_URL", DS_URL),
        os.environ.get("DEEPSEEK_MODEL", DS_MODEL)) if (args.execute and args.loop and not args.no_planner
                                                     and os.environ.get("DEEPSEEK_API_KEY")) else None
    print(f"mcp_control_loop mode={'execute' if args.execute else 'dry_run'} "
          f"planner={'enabled' if planner else 'disabled'} execution=lua navigation_intent=python "
          f"timing_log={args.timing_log.resolve()}")
    try:
        asyncio.run(run(args, lambda s, c, p: call_jev(api_url, key, model, s, c, p), planner))
    except KeyboardInterrupt:
        print("mcp_control_loop_stopped: keyboard interrupt")
    except (SessionChanged, IncompatibleBridge) as exc:
        print(f"mcp_control_loop_stopped: {exc}")
    except BridgePortUnavailable as exc:
        print(f"mcp_startup_error {exc}", file=sys.stderr)
        return 2
    finally:
        if planner:
            planner.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
