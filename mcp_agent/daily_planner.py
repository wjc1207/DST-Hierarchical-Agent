"""Asynchronous task planning for local execution and fast JEV handoffs."""

from __future__ import annotations

import json
import math
import shutil
import time
import uuid
import urllib.error
import urllib.request
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
from typing import Optional
from common.paths import DATA_DIR, LOG_DIR
from mcp_agent.model_state import model_game_state
from mcp_agent.region_resources import room_resources
from common.task_board import (validate_board, board_view, format_board,
                        INVENTORY_TARGET_PREFABS, INVENTORY_PREFAB_ALIASES)
from mcp_agent.action_sequence import SequenceExecutor, is_sequence, validate_sequence, format_sequence
from mcp_agent.priority_board import PriorityBoardExecutor, is_priority_board, validate_goals, merge_goals


DEFAULT_API_URL = "https://api.deepseek.com/chat/completions"
DEFAULT_MODEL = "deepseek-flash"
PLAN_PREVIEW_CHARS = 240
KNOWLEDGE_PATH = DATA_DIR / "DSTKnowledge.txt"
MAX_KNOWLEDGE_CHARS = 12000
PLAN_MAX_TOKENS = 4096
BOARD_LOG_DIR = LOG_DIR
COMPLETION_REPLAN_INTERVAL = 15.0
PLAN_HISTORY_TURNS = 3
PLAN_HISTORY_CHARS = 48000
STATIC_REFERENCE_MARKER = "\n\nStatic reference JSON (background, not a live observation):\n"
PLANNING_POLICY = {"quarters_per_day": 4, "mode": "review_not_reset"}
CONTROL_CONTRACT = {
    "transport": "mcp", "execution_owner": "lua", "navigation_intent_owner": "python",
    "unsupported": ["attack", "equip_weapon", "automatic_tool_prerequisite_solver", "alchemyengine"],
}
SUPPORTED_CAPABILITIES = {
    "execution_policy": "priority_goals",
    "inventory_target_prefabs": sorted(INVENTORY_TARGET_PREFABS),
    "inventory_prefab_aliases": dict(INVENTORY_PREFAB_ALIASES),
    "target_types": ["inventory", "structure", "hunger_ratio", "visit_region", "survey_region", "known_regions"],
    "constraints": {"max_carried_tools_or_torches": 1, "campfire_build_phase": "night"},
}


def planning_slot(state: dict) -> Optional[tuple[int, int]]:
    """Return (game day, zero-based quarter) from normalized DST world time."""
    world = state.get("world", {})
    day = world.get("day")
    if not isinstance(day, int) or day < 1:
        return None
    try:
        progress = float(world.get("time", 0))
    except (TypeError, ValueError):
        progress = 0.0
    if not math.isfinite(progress):
        progress = 0.0
    return day, min(3, max(0, int(progress * 4)))


def load_dst_knowledge(path: Path = KNOWLEDGE_PATH) -> str:
    return path.read_text(encoding="utf-8").strip()[:MAX_KNOWLEDGE_CHARS]


def planning_reference(knowledge_path: Path = KNOWLEDGE_PATH, *, knowledge: Optional[str] = None) -> dict:
    """Only installed knowledge/contracts enter the stable system prefix, never live state."""
    if knowledge is None:
        try:
            knowledge = load_dst_knowledge(knowledge_path)
        except (OSError, UnicodeError) as exc:
            knowledge = ""
            print(f"daily_plan_knowledge_unavailable={type(exc).__name__}")
    return deepcopy({
        "dst_knowledge": knowledge[:MAX_KNOWLEDGE_CHARS],
        "region_resource_priors": {room: sorted(prefabs) for room, prefabs in room_resources().items()},
        "supported_capabilities": SUPPORTED_CAPABILITIES,
        "control_contract": CONTROL_CONTRACT,
        "planning_policy": PLANNING_POLICY,
    })


def planning_snapshot(context: dict) -> dict:
    """Remove references repeated by older callers/history without changing live facts."""
    result = deepcopy(context)
    for key in ("dst_knowledge", "region_resource_priors", "supported_capabilities"):
        result.pop(key, None)
    if isinstance(result.get("control"), dict):
        for key in CONTROL_CONTRACT:
            result["control"].pop(key, None)
    if isinstance(result.get("planning_window"), dict):
        for key in PLANNING_POLICY:
            result["planning_window"].pop(key, None)
    return result


def snapshot_message(context: dict) -> str:
    return json.dumps(planning_snapshot(context), sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def planning_context(state: dict, history: list[dict]) -> dict:
    """Summarize facts and recent action outcomes, not the repeating telemetry log."""
    return {
        **model_game_state(state),
        "science_machine_built_this_run": any(
            event.get("action") == "build_sciencemachine" and event.get("outcome") == "executed"
            for event in history
        ),
        "recent_actions": history[-40:],
    }


def _short_text(value: object, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:limit]


def validate_plan(raw: object, day: int) -> dict:
    """Keep only a small, well-formed advisory plan in JEV's state."""
    if not isinstance(raw, dict):
        raise ValueError("DeepSeek plan must be a JSON object")
    if raw.get("kind") == "action_sequence" or (isinstance(raw.get("steps"), list)
            and raw["steps"] and isinstance(raw["steps"][0], dict)):
        return validate_sequence(raw, day)
    if "tasks" in raw:
        return validate_goals(raw, day) if is_priority_board(raw) else validate_board(raw, day)
    goal = _short_text(raw.get("goal"), 160)
    if not goal:
        raise ValueError("DeepSeek plan has no goal")
    steps = raw.get("steps")
    if not isinstance(steps, list):
        raise ValueError("DeepSeek plan has no steps array")
    cleaned_steps = [_short_text(step, 120) for step in steps[:4]]
    cleaned_steps = [step for step in cleaned_steps if step]
    if not cleaned_steps:
        raise ValueError("DeepSeek plan has no usable steps")
    return {
        "day": day,
        "goal": goal,
        "steps": cleaned_steps,
        "priority": _short_text(raw.get("priority"), 160),
        "replan_if": [
            text for condition in (raw.get("replan_if") or [])[:3]
            if (text := _short_text(condition, 100))
        ] if isinstance(raw.get("replan_if"), list) else [],
    }


def recent_exchanges(exchanges) -> list[tuple[str, str]]:
    """Keep a chronological suffix of complete user/assistant pairs, never fragments."""
    retained, size = [], 0
    for user, assistant in reversed(list(exchanges)[-PLAN_HISTORY_TURNS:]):
        pair_size = len(user) + len(assistant)
        if size + pair_size > PLAN_HISTORY_CHARS:
            break
        retained.append((user, assistant))
        size += pair_size
    return list(reversed(retained))


def request_plan(api_url: str, api_key: str, model: str, day: int, context: dict,
                 conversation: Optional[list[tuple[str, str]]] = None,
                 reference: Optional[dict] = None) -> dict:
    # Goal scheduling is deterministic. The model supplies needs, not movement legs.
    system_prompt = (
        "You maintain a priority task_board for Don't Starve Together. Return JSON only, not an action sequence. "
        "Use 1-6 tasks with stable id, title, priority (1 highest..5), target, depends_on, reason, "
        "search_budget_seconds (10-600, default120), search_budget_distance (8-2000, default240). "
        "Targets: inventory {prefab,count} for CURRENT stock; structure {prefab:campfire|sciencemachine}; "
        "hunger_ratio {at_least:0..1}; visit_region {node_id} for a known node; known_regions {count}. "
        "survey_region {node_id,area:16..4096} reveals ADDITIONAL area in an observed room (default256). "
        "The executor FIRST performs feasible visible goals by priority, skipping unavailable higher ones, "
        "then navigates for the highest ready goal. Goals can be completed in interleaved order. "
        "A hunger_ratio goal eats carried safe food, otherwise gathers nearby safe food or searches for food sources until the hunger target is observed. "
        "Use depends_on only for real prerequisites such as a pickaxe before mining. "
        "It uses observed source positions, region resource memory, Klei room generation priors, then unknown branches. "
        "Priors are possible initial resources, not guarantees, remaining stock, or spawn probabilities. "
        "A route failure is not proof that a resource is absent. Safety always overrides execution; "
        "JEV handles emergencies and temporary planning gaps only; off-board opportunities do not trigger it. "
        "Include needed food, sanity recovery and resource reserves explicitly in the board. "
        "If no tasks remain or no offered action/route can advance the board, reassess and update it. "
        "Keep useful future goals, but include feasible work or resource search for the current phase; "
        "a night-only campfire must not be the sole remaining daytime work. "
        "JEV temporarily handles local decisions while this evaluation is pending. "
        "Quarterly review is a PATCH, not a reset: preserve current goals, priorities, IDs and progress unless "
        "changed facts justify an update. Omitted tasks remain; explicitly cancel using cancel_task_ids with "
        "an explanation in goal/reasons. Do not raise targets just because time passed. "
        "When budgets are exhausted, use progress and precise failure reasons to revise or cancel the task. "
        "Current Lua limits: only one carried torch/axe/pickaxe can be requested, never count2; "
        "campfires can only be built AT NIGHT, when no nearby lit fire exists; "
        "tools must be carried with adequate durability before work. Craftability is not current action availability. "
        "No wait-second or equip-torch goals: illumination is handled by safety decisions when phase changes. "
        "Read the FINAL user snapshot as current truth; older user snapshots and assistant plans are "
        "history, not evidence of execution. Use observed facts, recent outcomes and DSTKnowledge. "
        "Static reference JSON below supplies DSTKnowledge, supported_capabilities, control_contract, "
        "planning_policy and the complete Klei room-type resource priors. A listed room type is not "
        "evidence that this world contains that room or that it has been discovered; use these priors "
        "only for room types in the observed navigation graph. Priors never override live resource facts. "
        "Use canonical inventory IDs from supported_capabilities.inventory_target_prefabs: log not logs, "
        "cutgrass not grass. Never invent GUIDs, coordinates, unknown node IDs or unsupported recipes. "
        "Alchemy engine is a future milestone, not currently buildable. Do not build another science "
        "machine if science_machine_built_this_run is true. Assess torch fuel and equipped slots, "
        "world phase and actual lit fires. Missing fuel values or fire lifetime are unknown, not full or zero. "
        "Craftability is not demand. Completion or a new planning window is not itself a reason "
        "to raise stock targets. Eat_safe_food already cooks "
        "raw food first at a nearby fire. Make night light and food prerequisites before optional travel. "
        'Example: {"goal":"Prepare light","tasks":['
        '{"id":"grass","title":"Torch grass","priority":1,"target":{"type":"inventory","prefab":"cutgrass","count":2}},'
        '{"id":"twigs","title":"Torch twigs","priority":1,"target":{"type":"inventory","prefab":"twigs","count":2}},'
        '{"id":"torch","title":"One torch","priority":1,"depends_on":["grass","twigs"],'
        '"target":{"type":"inventory","prefab":"torch","count":1}}]}'
    )
    if reference is None:
        # Compatibility for direct callers; runtime passes a separate local reference.
        reference = planning_reference(knowledge=context.get("dst_knowledge"))
    system_prompt += STATIC_REFERENCE_MARKER + json.dumps(
        reference, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    history = []
    for user, assistant in conversation or []:
        try:
            old_context = json.loads(user)
        except (ValueError, TypeError):
            old_context = None
        if isinstance(old_context, dict):
            user = snapshot_message(old_context)
        history.append((user, assistant))
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            *[message for user, assistant in recent_exchanges(history)
              for message in ({"role": "user", "content": user},
                              {"role": "assistant", "content": assistant})],
            {"role": "user", "content": snapshot_message(context)},
        ],
        "response_format": {"type": "json_object"},
        "reasoning_effort": "medium",
        "max_tokens": PLAN_MAX_TOKENS,
        "stream": False,
    }
    for attempt in range(2):
        if attempt:
            # A short non-thinking retry leaves more of the budget for JSON output.
            body["reasoning_effort"] = "none"
        request = urllib.request.Request(
            api_url,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"DeepSeek planning HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"DeepSeek planning connection failed: {exc.reason}") from exc

        usage = payload.get("usage") or {}
        if not isinstance(usage, dict):
            usage = {}
        cache_counts = {key: usage[key] for key in (
            "prompt_tokens", "prompt_cache_hit_tokens", "prompt_cache_miss_tokens")
            if type(usage.get(key)) is int and usage[key] >= 0}
        if cache_counts:
            print(f"daily_plan_usage attempt={attempt + 1} " + " ".join(
                f"{key}={value}" for key, value in cache_counts.items()))
        choices = payload.get("choices") or []
        if not choices or not isinstance(choices[0], dict):
            raise RuntimeError("DeepSeek planning response has no choice")
        choice = choices[0]
        finish_reason = choice.get("finish_reason")
        content = (choice.get("message") or {}).get("content") or ""
        completion_tokens = usage.get("completion_tokens")
        if finish_reason == "stop" and content:
            try:
                return validate_goals(json.loads(content), day)
            except (ValueError, TypeError) as exc:
                reason = f"invalid_json_or_plan:{type(exc).__name__}"
        else:
            reason = f"finish_reason={finish_reason} content_chars={len(content)}"
        retryable = finish_reason in {"stop", "length", "insufficient_system_resource", "aborted"}
        if attempt == 0 and retryable:
            print(f"daily_plan_retry day={day} reason={reason} attempt=2/2")
            continue
        raise RuntimeError(
            f"DeepSeek planning response unusable: {reason} "
            f"completion_tokens={completion_tokens}"
        )
    raise RuntimeError("DeepSeek planning retry exhausted")


def timed_request_plan(api_url: str, api_key: str, model: str, day: int, context: dict,
                       conversation: Optional[list[tuple[str, str]]] = None,
                       reference: Optional[dict] = None) -> tuple:
    """Measure the worker's request, excluding the JEV cycle's polling delay."""
    started_at = time.monotonic()
    try:
        plan = request_plan(api_url, api_key, model, day, context, conversation, reference)
    except Exception as exc:
        return None, time.monotonic() - started_at, exc
    return plan, time.monotonic() - started_at, None


class DailyPlanner:
    def __init__(
        self, api_key: str, api_url: str = DEFAULT_API_URL, model: str = DEFAULT_MODEL,
        knowledge_path: Path = KNOWLEDGE_PATH,
        board_log_dir: Optional[Path] = BOARD_LOG_DIR,
    ):
        self.api_key = api_key
        self.api_url = api_url
        self.model = model
        self.knowledge_path = knowledge_path
        self.history: deque[dict] = deque(maxlen=80)
        # Immutable JSON snapshots, accepted replies only; scoped to this run.
        self.conversation: deque[tuple[str, str]] = deque(maxlen=PLAN_HISTORY_TURNS)
        self.pending_user_message: Optional[str] = None
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="dst-planner")
        self.future: Optional[Future] = None
        self.pending_slot: Optional[tuple[int, int]] = None
        self.requested_slot: Optional[tuple[int, int]] = None
        self.plan: Optional[dict] = None
        self.science_machine_built = False
        self.failed_action: Optional[str] = None
        self.failure_count = 0
        self.last_replan = 0.0
        self.previous_board = None
        self.board_revision = 0
        self.board_run_id = uuid.uuid4().hex[:12]
        self.board_log_dir = board_log_dir
        self.board_signature = None
        self.board_console_signature = None
        self.board_console_status = None
        self.board_console_at = 0.0
        self.completion_replan_slot = None
        self.last_completion_request = None
        self.sequence = SequenceExecutor()
        self.sequence_replan_reason = None
        self.goals = PriorityBoardExecutor()
        self.pending_basis = None
        self.goal_replan_reason = None
        self.goal_retry_at = 0.
        self.goal_terminal = None
        self.legacy_mode = False

    def _board(self, state):
        if is_priority_board(self.plan):
            plan = {**self.plan, "verified_structures": ["sciencemachine"] if self.science_machine_built else []}
            return self.goals.view(plan, state)
        if is_sequence(self.plan):
            plan = {**self.plan, "verified_structures": ["sciencemachine"] if self.science_machine_built else []}
            return self.sequence.view(plan, state)
        if self.plan is None or self.plan.get("kind") != "task_board":
            return self.plan
        facts = ["sciencemachine"] if self.science_machine_built else []
        return board_view(self.plan, state, verified_structures=facts)

    def _publish_board(self, board):
        if is_sequence(board):
            self._publish_sequence(board)
            return
        if not board or board.get("kind") != "task_board":
            return
        signature = json.dumps({k: v for k, v in board.items() if k != "observed_at"}, sort_keys=True, ensure_ascii=False)
        status = (board.get("board_id"), board.get("status"),
                  tuple((t["id"], t["status"], t.get("blocked_reason")) for t in board["tasks"]))
        now = time.monotonic()
        if (signature != self.board_console_signature
                and (status != self.board_console_status or now - self.board_console_at >= 3)):
            print(format_board(board, shutil.get_terminal_size((100, 24)).columns))
            self.board_console_signature, self.board_console_status = signature, status
            self.board_console_at = now
        if signature == self.board_signature:
            return
        self.board_signature = signature
        if self.board_log_dir is None:
            return
        try:
            self.board_log_dir.mkdir(parents=True, exist_ok=True)
            packet = {"run_id": self.board_run_id, "event": "board_updated", "task_board": board}
            temporary = self.board_log_dir / (".task_board_" + self.board_run_id + ".tmp")
            temporary.write_text(json.dumps(packet, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(self.board_log_dir / "task_board.json")
            with (self.board_log_dir / "task_board.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(packet, ensure_ascii=False, separators=(",", ":")) + "\n")
        except OSError as exc:
            print(f"task_board_log_unavailable error={type(exc).__name__}")

    def _publish_sequence(self, plan):
        signature = json.dumps(plan, sort_keys=True, ensure_ascii=False)
        if signature == self.board_signature:
            return
        self.board_signature = signature
        print(format_sequence(plan))
        if self.board_log_dir is None:
            return
        try:
            self.board_log_dir.mkdir(parents=True, exist_ok=True)
            packet = {"run_id": self.board_run_id, "event": "sequence_updated", "action_sequence": plan}
            temporary = self.board_log_dir / (".action_sequence_" + self.board_run_id + ".tmp")
            temporary.write_text(json.dumps(packet, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(self.board_log_dir / "action_sequence.json")
            with (self.board_log_dir / "action_sequence.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(packet, ensure_ascii=False, separators=(",", ":")) + "\n")
        except OSError as exc:
            print(f"action_sequence_log_unavailable error={type(exc).__name__}")

    def request_review(self, state: dict, reason: str, *, immediate: bool = False) -> Optional[dict]:
        """One asynchronous evaluation; repeated polls keep the existing request/backoff."""
        if self.future is None:
            self.goal_replan_reason = self.goal_replan_reason or reason
            if immediate and self.goal_replan_reason != "review_retry":
                self.goal_retry_at = 0.
        return self.update(state)

    def update(self, state: dict) -> Optional[dict]:
        # The Mod normalizes Klei's researchlab to the public structure name.
        # An observed existing machine is also a verified milestone.
        if any(e.get("prefab") == "sciencemachine" for e in state.get("nearby", [])):
            self.science_machine_built = True
        # Legacy clients/history retain their old semantics, never the new DS path.
        if self.legacy_mode or (self.plan is not None and not is_priority_board(self.plan)):
            self.legacy_mode = True
            return self._legacy_update(state)
        if self.plan is None and self.future is not None and self.future.done():
            try:
                pending = self.future.result()[0]
                if pending is not None and not is_priority_board(pending):
                    self.legacy_mode = True
                    return self._legacy_update(state)
            except Exception:
                pass
        slot = planning_slot(state)
        if slot is None:
            return None
        day, quarter = slot
        now = time.monotonic()
        if self.future is not None and self.future.done():
            future, user, basis = self.future, self.pending_user_message, self.pending_basis
            self.future = self.pending_user_message = self.pending_basis = self.pending_slot = None
            seconds = 0.
            try:
                result, seconds, error = future.result()
                if error:
                    raise error
                compatible = (not state.get("player", {}).get("vitals", {}).get("dead") and (not basis or (
                    basis[0] == state.get("player", {}).get("guid")
                    and basis[1] == state.get("bridge", {}).get("session_id")
                    and (basis[2] is None or state.get("observed_at", basis[2]) >= basis[2]))))
                if not compatible:
                    raise ValueError("Planner snapshot belongs to a different/reset player session")
                result = merge_goals(self._board(state) or self.previous_board, result)
                graph = state.get("navigation", {}).get("graph") or {}
                known = {graph.get("current_node"), *(r.get("node_id") for r in graph.get("routes", []))}
                if any(t["target"]["type"] in {"visit_region", "survey_region"} and t["target"]["node_id"] not in known for t in result["tasks"]):
                    raise ValueError("Visit target is not in the observed region graph")
                self.board_revision += 1
                result.update(board_id=f"{self.board_run_id}:{self.board_revision}", quarter=quarter + 1)
                self.plan = result
                new_batch = bool(user and json.loads(user).get("replan_reason") == "all_tasks_completed")
                self.goals.adopt(result, new_batch=new_batch)
                self.goal_terminal = None
                if user:
                    self.conversation = deque(recent_exchanges([*self.conversation,
                        (user, json.dumps(result, ensure_ascii=False, separators=(",", ":")))]), maxlen=PLAN_HISTORY_TURNS)
                preview = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
                print(f"daily_plan_ready day={day} quarter={quarter + 1}/4 response_time={seconds:.2f}s preview={preview[:PLAN_PREVIEW_CHARS]}{'…' if len(preview)>PLAN_PREVIEW_CHARS else ''}")
            except Exception as exc:
                print(f"daily_plan_error response_time={seconds:.2f}s error={type(exc).__name__}:{exc}")
                self.goal_retry_at = now + COMPLETION_REPLAN_INTERVAL
                self.goal_replan_reason = "review_retry"
        view = self._board(state) if self.plan else None
        terminal = view and view.get("status") in {"completed", "blocked"}
        if terminal:
            marker = (view["board_id"], view["status"])
            if marker != self.goal_terminal:
                self.goal_terminal = marker
                self.previous_board = view
                if self.goal_replan_reason != "review_retry":
                    self.goal_replan_reason = "all_tasks_completed" if view["status"] == "completed" else "search_or_prerequisite_blocked"
                    # A newly terminal board needs evaluation now, even if the
                    # last normal review was recent. Keep failed-request backoff.
                    if self.future is None:
                        self.goal_retry_at = now
                print(f"task_board_review_requested reason={self.goal_replan_reason}")
        reason = self.goal_replan_reason or ("quarter_review" if self.plan else "initial_plan")
        if self.future is None and now >= self.goal_retry_at and (self.requested_slot != slot or self.goal_replan_reason):
            context = planning_context(state, list(self.history))
            context.update(replan_reason=reason, current_task_board=view, previous_task_board=self.previous_board,
                           science_machine_built_this_run=self.science_machine_built,
                           planning_window={"day": day, "quarter": quarter + 1})
            reference = planning_reference(self.knowledge_path)
            context = planning_snapshot(context)
            self.pending_user_message = snapshot_message(context)
            self.pending_basis = (state.get("player", {}).get("guid"), state.get("bridge", {}).get("session_id"), state.get("observed_at"))
            self.future = self.executor.submit(timed_request_plan, self.api_url, self.api_key, self.model, day,
                                               context, list(self.conversation), reference)
            self.pending_slot = self.requested_slot = slot
            self.goal_replan_reason = None
            self.goal_retry_at = now + COMPLETION_REPLAN_INTERVAL
            print(f"daily_plan_requested day={day} quarter={quarter + 1}/4 reason={reason} knowledge_chars={len(reference['dst_knowledge'])} history_turns={len(self.conversation)}")
        self._publish_board(view)
        return view

    def _legacy_update(self, state: dict) -> Optional[dict]:
        """Poll without waiting; keep active sequences, replan on terminal progress."""
        slot = planning_slot(state)
        if slot is None:
            return None
        day, quarter = slot
        if self.plan is not None and not is_sequence(self.plan) and (
            self.plan["day"] != day or self.plan.get("quarter") != quarter + 1
        ):
            if self.plan.get("kind") == "task_board":
                self.previous_board = self._board(state)
                self._publish_board(self.previous_board)
            self.plan = None
        if self.future is not None and self.future.done():
            future = self.future
            completed_slot = self.pending_slot
            completed_user_message = self.pending_user_message
            self.future = None
            self.pending_slot = None
            self.pending_user_message = None
            try:
                result, response_seconds, error = future.result()
            except Exception as exc:
                # Planning is optional; even an unexpected planner failure must not stop JEV.
                print(f"daily_plan_error={exc}")
            else:
                if error is not None:
                    print(f"daily_plan_error response_time={response_seconds:.2f}s error={error}")
                elif completed_slot == slot and self.requested_slot == slot and result["day"] == day:
                    if completed_user_message is not None:
                        # Save before runtime quarter/board IDs and progress are added.
                        reply = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
                        self.conversation = deque(recent_exchanges([
                            *self.conversation, (completed_user_message, reply),
                        ]), maxlen=PLAN_HISTORY_TURNS)
                    result["quarter"] = quarter + 1
                    if result.get("kind") == "task_board":
                        self.board_revision += 1
                        result["board_id"] = f"{self.board_run_id}:{self.board_revision}"
                    elif is_sequence(result):
                        self.board_revision += 1
                        result["sequence_id"] = f"{self.board_run_id}:{self.board_revision}"
                    self.plan = result
                    serialized = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
                    preview = serialized[:PLAN_PREVIEW_CHARS]
                    if len(serialized) > PLAN_PREVIEW_CHARS:
                        preview += "…"
                    print(
                        f"daily_plan_ready day={day} quarter={quarter + 1}/4 "
                        f"response_time={response_seconds:.2f}s preview={preview}"
                    )

        view = self._board(state) if self.plan else None
        if is_sequence(view) and view["status"] in {"completed", "failed"}:
            self.previous_board = view
            self._publish_board(view)
            self.plan = None
            self.requested_slot = None
            self.sequence_replan_reason = "sequence_completed" if view["status"] == "completed" else "sequence_failed"
            self.completion_replan_slot = slot
            print(f"action_sequence_finished id={view.get('sequence_id')} status={view['status']} reason={view.get('reason', '')}")
        if (view and view.get("kind") == "task_board" and view.get("status") == "active"
                and view.get("state_basis") == "observed" and view.get("tasks")
                and all(t["status"] == "done" for t in view["tasks"])
                and not state.get("player", {}).get("vitals", {}).get("dead")):
            # Retire this revision once. Never keep it as active guidance while
            # waiting for a new board, or treat predicted completion as real.
            view["status"] = "completed"
            self.previous_board = view
            self._publish_board(view)
            self.plan = None
            self.requested_slot = None
            self.completion_replan_slot = slot
            print(f"task_board_completed id={view['board_id']} reason=all_tasks_completed")

        completing = self.completion_replan_slot == slot
        completion_ready = (not completing or self.last_completion_request is None
                            or time.monotonic() - self.last_completion_request >= COMPLETION_REPLAN_INTERVAL)
        if self.requested_slot != slot and self.future is None and completion_ready and not is_sequence(self.plan):
            self.requested_slot = slot
            reason = self.sequence_replan_reason or ("all_tasks_completed" if completing else "quarter_window_or_invalidation")
            self.sequence_replan_reason = None
            context = planning_context(state, list(self.history))
            context["replan_reason"] = reason
            context["science_machine_built_this_run"] = self.science_machine_built
            context["previous_task_board"] = self._board(state) or self.previous_board
            context["previous_action_sequence"] = self.previous_board if is_sequence(self.previous_board) else None
            context["planning_window"] = {"day": day, "quarter": quarter + 1}
            reference = planning_reference(self.knowledge_path)
            # Snapshot the actual request, not a later observation or mutated board.
            context = planning_snapshot(context)
            self.pending_user_message = snapshot_message(context)
            self.future = self.executor.submit(
                timed_request_plan, self.api_url, self.api_key, self.model, day, context,
                list(self.conversation), reference,
            )
            self.pending_slot = slot
            if completing:
                self.last_completion_request = time.monotonic()
            self.completion_replan_slot = None
            print(
                f"daily_plan_requested day={day} quarter={quarter + 1}/4 "
                f"knowledge_chars={len(reference['dst_knowledge'])} "
                f"history_turns={len(self.conversation)} reason={reason}"
            )
        view = self._board(state) if self.plan and self.plan["day"] == day else None
        self._publish_board(view)
        return view

    def record_action(self, state: dict, action: str, outcome: str, reason: str = "") -> None:
        vitals = state.get("player", {}).get("vitals", {})
        self.history.append({
            "day": state.get("world", {}).get("day"),
            "phase": state.get("world", {}).get("phase"),
            "action": action,
            "outcome": outcome,
            "reason": reason,
            "hunger": vitals.get("hunger"),
            "health": vitals.get("health"),
        })
        if outcome == "failed":
            self.failure_count = self.failure_count + 1 if self.failed_action == action else 1
            self.failed_action = action
        else:
            self.failure_count = 0
            self.failed_action = None
        major_change = outcome == "executed" and action == "build_sciencemachine"
        if major_change:
            self.science_machine_built = True
        if is_priority_board(self.plan):
            if major_change:
                self.goal_replan_reason = "science_machine_completed"
            # Route failure is handled locally; do not invalidate the board.
            return
        if is_sequence(self.plan):
            # SequenceExecutor owns exact step failures/completion. A milestone
            # must not discard the remaining ordered steps during native work.
            return
        repeated_failure = self.failure_count >= 2
        if (major_change or repeated_failure) and time.monotonic() - self.last_replan >= 60:
            self.last_replan = time.monotonic()
            self.requested_slot = None
            if self.plan and self.plan.get("kind") == "task_board":
                self.previous_board = self._board(state)
                self.previous_board["status"] = "invalidated"
                for task in self.previous_board["tasks"]:
                    if task["status"] != "done":
                        task["status"] = "invalidated"
                self._publish_board(self.previous_board)
            self.plan = None
            self.failure_count = 0

    def close(self) -> None:
        self.executor.shutdown(wait=False, cancel_futures=True)
