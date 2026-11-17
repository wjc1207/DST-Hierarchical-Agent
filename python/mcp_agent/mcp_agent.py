"""DeepSeek planning host and native player task executor over MCP."""
from __future__ import annotations

import argparse
import asyncio
import copy
import os
import sys
import time
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.paths import PROJECT_ROOT, PYTHON_ROOT

from common import timing_log as timing
from common import autonomy
from mcp_agent.daily_planner import DailyPlanner, DEFAULT_API_URL as DS_URL, DEFAULT_MODEL as DS_MODEL
from mcp_agent.navigation_intents import ExplorationExecutor
from common.config import load_dotenv, resolve_env_path
from common.game_rules import survival_dangers
from mcp_server.game_bridge import TERMINAL, finite_time, BridgePortUnavailable, check_bridge_port
from mcp_agent.action_sequence import SequenceExecutor, is_sequence
from mcp_agent.priority_board import PriorityBoardExecutor, is_priority_board, task_signature, idle_review_reason, progress_signatures_match
from common.residence import allowed as residence_allowed
from common.thermal import heat_risk


class MCPRejected(RuntimeError):
    """A tool rejected stale/unsafe work. Reobserve, never bypass validation."""


class SessionChanged(RuntimeError):
    pass


class IncompatibleBridge(RuntimeError):
    pass


class PlayerDead(RuntimeError):
    pass


class MCPAgent:
    def __init__(self, client, *, planner=None, execute=False, poll=0.25, navigator=None):
        self.client, self.planner = client, planner
        self.execute, self.poll = execute, poll
        self.external_board = False
        self.navigator = navigator or ExplorationExecutor()
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
        self.sequence = getattr(planner, "sequence", None) or SequenceExecutor()
        self.sequence_actions = {}
        self.goals = getattr(planner, "goals", None) or PriorityBoardExecutor()
        self.goal_actions = {}
        self.goal_gap_marker = None
        self.goal_light = None
        self.active_policy = None
        self.autonomy_revision = None
        self.active_autonomy_revision = None

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
            if progress_signatures_match(self.goals.records.get(task_id, {}).get("signature"), signature):
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
        vitals=state.get("player",{}).get("vitals",{})
        timing.annotate(temperature=vitals.get("temperature"),overheating=vitals.get("overheating"),
                        freezing=vitals.get("freezing"),heat_warning_threshold=vitals.get("heat_warning_threshold"),
                        thermal_status=(state.get("thermal") or {}).get("status"),
                        thermal_reason=(state.get("thermal") or {}).get("reason"),
                        fire_state=[{k:row.get(k) for k in ("building_id","guid","lit","fire_section","fuel_percent","fuel_min","fuel_max","fuel_estimated","fire_data_source","reason","ratio_reason")}
                                    for row in (state.get("refueling") or {}).get("targets",[])[:4]])
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
            if self.planner:
                self.plan = self.planner.update(state)
        control_revision = autonomy.revision(state)
        if control_revision != self.autonomy_revision:
            if self.external_board:
                self.plan = None
                self.goals = PriorityBoardExecutor()
                self.navigator = ExplorationExecutor()
            self.autonomy_revision = control_revision
            self.active_policy = None
            self.goal_actions.clear()
            self.sequence_actions.clear()
            self.goal_gap_marker = self.goal_light = None
            if self.planner:
                self.goals = getattr(self.planner, "goals", self.goals)
                self.sequence = getattr(self.planner, "sequence", self.sequence)
        if isinstance(self.planner, DailyPlanner):
            while self.planner.feedback:
                message = self.planner.feedback[0]
                if message["revision"] != control_revision:
                    self.planner.feedback.popleft()
                    continue
                try:
                    await self.rpc("send_agent_feedback", **message)
                except MCPRejected as exc:
                    print(f"agent_feedback_pending error={exc}")
                    break
                self.planner.feedback.popleft()
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
                    self.active_policy = None
                    timing.annotate(outcome="executed" if result["status"] == "completed" else result["status"],
                                    action_reason=result.get("reason"))
                    return
                if (not autonomy.enabled(packet["state"]) or
                        autonomy.revision(packet["state"]) != self.active_autonomy_revision):
                    if result["status"] != "cancel_requested":
                        await self.rpc("cancel_action", action_id=self.active_id)
                    await asyncio.sleep(self.poll)
                    continue
                if self.active_policy and result["status"] != "cancel_requested":
                    current_ids = self.goals.timed_task_ids(self.plan,packet["state"])
                    area = self.goals.residence
                    old_area = self.active_policy["residence"]
                    action = self.active_policy["action"]
                    changed = current_ids != self.active_policy["timed_ids"]
                    owner = self.goal_actions.get(self.active_id)
                    if changed and action.get("kind") == "add_fuel" and owner:
                        task = next((t for t in self.goals.view(self.plan,packet["state"])["tasks"] if t["id"]==owner[0]),None)
                        # A newly adequate fire must not cancel the physical addition before its native confirmation.
                        if (task and (task["status"]=="maintaining" or (task["status"]=="done" and "duration" not in task))
                                and set(current_ids).symmetric_difference(self.active_policy["timed_ids"])=={owner[0]}):
                            changed = False
                    changed = changed or bool(old_area and old_area["holding"] and not area)
                    changed = changed or bool(area and area["holding"] and not residence_allowed(action,packet["state"],area))
                    if changed:
                        timing.event("task_time.review_requested",reason="task_time_or_residence_changed",action_id=self.active_id)
                        await self.rpc("cancel_action",action_id=self.active_id)
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

    async def perform(self, choice, packet, sequence_step=None, goal_task=None, *, safety=False):
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
        if packet["actions"][choice].get("coordinate_target"):
            arguments["destination"] = packet["actions"][choice]["coordinate_target"]
        policy = None
        policy_state = (self.packet or packet)["state"]
        if is_priority_board(self.plan) and not safety:
            ids = self.goals.timed_task_ids(self.plan,policy_state)
            area = copy.deepcopy(self.goals.residence)
            if area and area["holding"]:
                arguments["residence"] = {"home_id":area["home_id"],"radius":area["radius"]}
            policy = {"timed_ids":ids,"residence":area,"action":copy.deepcopy(packet["actions"][choice])}
        if goal_task is not None and packet["actions"][choice]["kind"] == "explore" and is_priority_board(self.plan):
            arguments["navigation_targets"] = self.goals.navigation_targets(self.plan, policy_state)
            timing.annotate(navigation_target_count=len(arguments["navigation_targets"]))
        if (goal_task is not None and goal_task["target"].get("placement")
                and packet["actions"][choice]["kind"] in {"place","build_campfire","build_sciencemachine"}):
            arguments["placement"] = copy.deepcopy(goal_task["target"]["placement"])
        result = await self.rpc("start_action", **arguments)
        self.active_id, self.active_started = result["action_id"], time.monotonic()
        self.active_autonomy_revision = autonomy.revision(packet["state"])
        self.active_policy = policy
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

    async def cycle(self):
        packet = await self.observe()
        state = packet["state"]
        timing.annotate(transport="mcp", execution_owner="lua", day=state["world"].get("day"),
                        phase=state["world"].get("phase"), task_board_id=self.board_id(self.plan))
        if self.active_id:
            await self.wait_action()
            return False
        if not autonomy.enabled(state):
            self.goals.pause_accounting()
            if self.execute and self.auto_enabled:
                await self.sync_board(None, False)
            timing.annotate(outcome="skipped", decision_source="autonomy_disabled")
            await asyncio.sleep(self.poll)
            return False
        if await self.handle_thermal(packet):
            return False
        if isinstance(self.planner, DailyPlanner) and self.planner.hold_execution:
            self.goals.pause_accounting()
            if self.execute and self.auto_enabled:
                await self.sync_board(None, False)
            if await self.handle_safety(packet, self.goals, "replanning_safety"):
                return False
            timing.annotate(outcome="skipped", decision_source="dialogue_waiting")
            await asyncio.sleep(self.poll)
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
        if not self.external_board:
            await self.handle_safety(packet, self.goals, "idle_safety")
        await asyncio.sleep(self.poll)
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
            # Leave the next step untouched until a new plan is accepted.
            await self.observe()
            await asyncio.sleep(self.poll)
            return
        print(f"action_sequence_step index={view['step_index'] + 1}/{len(view['steps'])} id={step_id} choice={choice}")
        await self.perform(choice, packet, sequence_step=step_id)

    async def handle_thermal(self, packet):
        state,actions=packet["state"],packet["actions"]
        if not heat_risk(state) or any(e.get("activeThreat") for e in state.get("nearby",[])):
            return False
        self.goals.pause_accounting()
        self.goals.needs_safety_decision=False
        # Release a legacy automatic board before issuing a host safety action.
        if self.execute and self.auto_enabled:
            await self.sync_board(None,False)
            packet=await self.observe()
            state,actions=packet["state"],packet["actions"]
            if not heat_risk(state) or any(e.get("activeThreat") for e in state.get("nearby",[])):
                return False
        if state.get("bridge",{}).get("executor_busy"):
            await asyncio.sleep(self.poll)
            return True
        choice="cool_down" if "cool_down" in actions else "equip_torch" if "equip_torch" in actions else None
        timing.annotate(decision_source="thermal_safety" if choice else "thermal_holding",safety_reasons=["thermal_warning"])
        if choice:
            print(f"thermal_safety temperature={state['player']['vitals'].get('temperature')} choice={choice}")
            await self.perform(choice,packet,safety=True)
        else:
            await asyncio.sleep(self.poll)
        return True

    async def handle_safety(self, packet, owner, source, *, light_changed=False):
        state, actions = packet["state"], packet["actions"]
        reasons = survival_dangers(state)
        forced = light_changed or owner.needs_safety_decision
        owner.needs_safety_decision = False
        if not reasons and not forced:
            return False

        remedies = ["flee_from_nearest_hostile"]
        if "night_light_requires_decision" in reasons:
            remedies.extend(("equip_torch", "build_campfire", "craft_torch"))
        if "critical_hunger" in reasons:
            remedies.append("eat_safe_food")
        choice = next((key for key in remedies if key in actions), None)
        if choice is None:
            return False
        if state["bridge"].get("executor_busy"):
            return True
        if isinstance(owner, PriorityBoardExecutor):
            owner.pause_accounting()
        timing.annotate(decision_source=source, safety_reasons=sorted(reasons))
        await self.perform(choice, packet, safety=True)
        return True

    async def run_priority_board(self, packet):
        if packet["state"]["bridge"].get("executor_busy"):
            timing.annotate(outcome="skipped", decision_source="native_busy")
            await asyncio.sleep(self.poll)
            return
        if packet.get("state_age_ms", 0) > 1000:
            packet = await self.observe(refresh=True)
        if await self.handle_thermal(packet):
            return
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
            if action.get("coordinate_target"):
                packet = {**packet,"actions":{**packet["actions"],choice:action}}
            self.goal_gap_marker = None
            timing.annotate(decision_source="goal_navigation" if action.get("kind") == "explore" else "goal_local",
                            goal_task_id=task["id"], task_board_id=self.plan.get("board_id"),
                            exploration_strategy=self.navigator.strategy if action.get("kind") == "explore" else None)
            print(f"task_board_action task={task['id']} priority={task['priority']} choice={choice} "
                  f"reason={self.navigator.strategy if action.get('kind') == 'explore' else 'visible_goal'}")
            await self.perform(choice, packet, goal_task=task)
            return
        self.goals.pause_accounting()
        reason = "no_tasks" if not self.plan.get("tasks") else self.goals.reason or "no_feasible_action_or_route"
        if reason in {"residence_holding", "refuel_settling", "maintenance_holding", "charcoal_burning"}:
            timing.annotate(decision_source="residence_idle" if reason=="residence_holding" else reason,goal_reason=reason)
            await asyncio.sleep(self.poll)
            return
        if reason == "waiting_for_start":
            timing.annotate(goal_reason=reason)
            await asyncio.sleep(self.poll)
            return
        marker = (self.board_id(self.plan), reason)
        first_gap = marker != self.goal_gap_marker
        self.goal_gap_marker = marker
        if isinstance(self.planner, DailyPlanner):
            with timing.span("planner.update"):
                if idle_review_reason(self.goals.view(self.plan, state)):
                    # DailyPlanner owns idle-board review deduplication, including
                    # across accepted revisions that still have nothing runnable.
                    self.plan = self.planner.update(state)
                else:
                    self.plan = self.planner.request_review(state, reason, immediate=first_gap)
        timing.annotate(goal_reason=reason, decision_source="waiting_for_board")
        await asyncio.sleep(self.poll)

    async def shutdown(self):
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


async def run(args, planner):
    check_bridge_port(args.port)
    from mcp import Client, StdioServerParameters

    server_args = [str(PYTHON_ROOT / "mcp_server" / "mcp_server.py"),
                   "--config", str(args.config.resolve()), "--port", str(args.port)]
    if planner:
        server_args += ["--poll", str(args.poll), "--timing-log", str(args.timing_log.resolve())]
    parameters = StdioServerParameters(command=sys.executable, args=server_args, cwd=str(PROJECT_ROOT))
    async with Client(parameters, read_timeout_seconds=5) as client:
        if not planner:
            result = await client.call_tool("get_state", {})
            if result.is_error:
                raise RuntimeError("; ".join(getattr(row, "text", "") for row in result.content))
            state = result.structured_content["state"]
            print(f"read_only: day={state['world'].get('day')} phase={state['world'].get('phase')}")
            return

        from mcp_agent.planning_client import PlanningClient

        planning = PlanningClient(client, player_model=planner)
        cycle = 0
        try:
            while not args.max_cycles or cycle < args.max_cycles:
                cycle += 1
                try:
                    await planning.cycle()
                except Exception as exc:
                    print(f"planning_cycle_error: {exc}")
                await asyncio.sleep(args.poll)
        finally:
            planning.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", type=Path)
    parser.add_argument("--config", type=Path, default=PROJECT_ROOT / "logs" / "mcp_bridge.json")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--poll", type=float, default=0.25)
    parser.add_argument("--max-cycles", type=int, default=0)
    parser.add_argument("--timing-log", type=Path, default=PROJECT_ROOT / "logs" / "mcp_timing.jsonl")
    args = parser.parse_args()
    if not 0.1 <= args.poll <= 2 or args.max_cycles < 0 or not 1 <= args.port <= 65535:
        parser.error("--poll must be 0.1..2 seconds; --max-cycles must be nonnegative; --port must be 1..65535")
    load_dotenv(resolve_env_path(args.env))
    key = os.getenv("DEEPSEEK_API_KEY")
    if args.execute and not args.loop:
        parser.error("--execute requires --loop")
    if args.execute and not key:
        parser.error("DEEPSEEK_API_KEY is required with --execute")
    planner = (key, os.getenv("DEEPSEEK_API_URL", DS_URL),
               os.getenv("DEEPSEEK_MODEL", DS_MODEL)) if args.execute else None
    try:
        asyncio.run(run(args, planner))
    except KeyboardInterrupt:
        print("mcp_control_loop_stopped: keyboard interrupt")
    except (BridgePortUnavailable, RuntimeError) as exc:
        print(f"mcp_startup_error {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
