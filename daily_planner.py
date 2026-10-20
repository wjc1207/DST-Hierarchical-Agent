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
from pathlib import Path
from typing import Optional
from model_state import model_game_state
from task_board import (validate_board, board_view, format_board,
                        INVENTORY_TARGET_PREFABS, INVENTORY_PREFAB_ALIASES)


DEFAULT_API_URL = "https://api.deepseek.com/chat/completions"
DEFAULT_MODEL = "deepseek-flash"
PLAN_PREVIEW_CHARS = 240
KNOWLEDGE_PATH = Path(__file__).resolve().with_name("DSTKnowledge.txt")
MAX_KNOWLEDGE_CHARS = 12000
PLAN_MAX_TOKENS = 4096
BOARD_LOG_DIR = Path(__file__).resolve().parent / "logs"
COMPLETION_REPLAN_INTERVAL = 15.0
PLAN_HISTORY_TURNS = 3
PLAN_HISTORY_CHARS = 48000


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
    if "tasks" in raw:
        return validate_board(raw, day)
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
                 conversation: Optional[list[tuple[str, str]]] = None) -> dict:
    system_prompt = (
        "You are the daily planner for a Don't Starve Together agent. The separate JEV model "
        "makes fast next-action decisions; do not select or force its next action. Plan only "
        "a short, feasible goal for the next quarter of this game day using observed facts, "
        "the supplied DSTKnowledge reference, and recent outcomes. "
        "Earlier user messages are historical state snapshots; earlier assistant messages "
        "are proposed plans, not evidence of execution or current facts. The FINAL user "
        "message is the latest observation and overrides conflicting history. Use its "
        "observed progress and recent outcomes to revise or abandon earlier plans. "
        "Output a small task board, not a paragraph or an action sequence. The controller "
        "automatically advances locally executable tasks by priority and dependencies using "
        "observed reachable resources and legal actions. The executor also owns exploration "
        "and known-region travel: it follows visit_region targets, uses known source records "
        "and BFS hop counts, and investigates unknown branches with resumable depth-first "
        "traversal. Entered does not mean fully surveyed; source records can be stale. It does "
        "not yet invent missing-tool subtasks. JEV handles non-navigation local actions, "
        "opportunities and emergencies, not exploration selection. Use 1-6 tasks with stable "
        "IDs when carrying forward previous_task_board, integer priority 1-5 (1 highest), "
        "optional depends_on IDs and action_hints. No status or fabricated progress: the runtime "
        "calculates those from facts. Targets must be machine-checkable: "
        "inventory {prefab,count}, hunger_ratio {at_least:0..1}, known_regions {count}, "
        "visit_region {node_id from observed graph routes}, or structure "
        "{prefab:campfire|firepit|sciencemachine}. Inventory means desired CURRENT stock, "
        "not lifetime collection. Use canonical inventory prefab IDs from "
        "supported_capabilities.inventory_target_prefabs, not English display names or source "
        "entities: logs must be log, and grass material must be cutgrass. Read individual "
        "inventory_details durability_percent (0..1; torch fuel remaining) and equipped slots, "
        "lighting.observed_fires (lit, distance), and world phase before judging light reserves. "
        "Missing fuel values or fire lifetime are unknown, not full or zero. Craftability is "
        "not demand: another torch is possible, but increase its target only for an observed "
        "or anticipated lighting shortfall, such as low remaining fuel or necessary night travel. "
        "Account for stationary light only while staying near a lit fire; it does not prove "
        "coverage for the whole night. Explain any increased reserve using those facts, not "
        "merely 'more is safer'. Dependencies must be "
        "acyclic and reference this board only. "
        "If replan_reason is all_tasks_completed, use the completed board as history and "
        "choose new unmet goals from the CURRENT input; do not repeat already satisfied "
        "inventory or exploration targets merely because they appeared on the old board. "
        "Completion or a new planning window is not itself a reason to raise stock targets. "
        "Keep sufficient reserves sufficient unless consumption, conditions, or intended use "
        "changes; redirect effort to other unmet needs rather than ratcheting stock upward. "
        "Use only supported capabilities in the context; unavailable research and structures "
        "are longer-term context, not executable tasks. The longer-term milestone is an alchemy "
        "engine, but this controller currently supports only a science machine as a research "
        "structure; do not ask it to build an alchemy engine yet. If history says a science "
        "machine was already built, do not plan another. Survival, food, "
        "light, and safety outrank optional progress. Do not assume unseen "
        "resources or unsupported abilities. The live state and hard safety rules always override "
        "your plan. Return JSON only, shaped like: "
        '{"goal":"Prepare safely for night","tasks":[{"id":"grass","title":"Reserve torch grass",'
        '"priority":1,"target":{"type":"inventory","prefab":"cutgrass","count":2},'
        '"depends_on":[],"action_hints":["collect_grass"],"reason":"Night preparation"}],'
        '"replan_if":["Threats or resources change"]}'
    )
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            *[message for user, assistant in recent_exchanges(conversation or [])
              for message in ({"role": "user", "content": user},
                              {"role": "assistant", "content": assistant})],
            {"role": "user", "content": json.dumps(context, ensure_ascii=False, separators=(",", ":"))},
        ],
        "response_format": {"type": "json_object"},
        "reasoning_effort": "low",
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

        choices = payload.get("choices") or []
        if not choices or not isinstance(choices[0], dict):
            raise RuntimeError("DeepSeek planning response has no choice")
        choice = choices[0]
        finish_reason = choice.get("finish_reason")
        content = (choice.get("message") or {}).get("content") or ""
        completion_tokens = (payload.get("usage") or {}).get("completion_tokens")
        if finish_reason == "stop" and content:
            try:
                return validate_plan(json.loads(content), day)
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
                       conversation: Optional[list[tuple[str, str]]] = None) -> tuple:
    """Measure the worker's request, excluding the JEV cycle's polling delay."""
    started_at = time.monotonic()
    try:
        plan = request_plan(api_url, api_key, model, day, context, conversation)
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

    def _board(self, state):
        if self.plan is None or self.plan.get("kind") != "task_board":
            return self.plan
        facts = ["sciencemachine"] if self.science_machine_built else []
        return board_view(self.plan, state, verified_structures=facts)

    def _publish_board(self, board):
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

    def update(self, state: dict) -> Optional[dict]:
        """Poll without waiting; replan each quarter and when a board is completed."""
        slot = planning_slot(state)
        if slot is None:
            return None
        day, quarter = slot
        if self.plan is not None and (
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
        if self.requested_slot != slot and self.future is None and completion_ready:
            self.requested_slot = slot
            reason = "all_tasks_completed" if completing else "quarter_window_or_invalidation"
            context = planning_context(state, list(self.history))
            context["replan_reason"] = reason
            context["science_machine_built_this_run"] = self.science_machine_built
            context["previous_task_board"] = self._board(state) or self.previous_board
            context["supported_capabilities"] = {
                "inventory_target_prefabs": sorted(INVENTORY_TARGET_PREFABS),
                "inventory_prefab_aliases": dict(INVENTORY_PREFAB_ALIASES),
                "actions": ["collect_<observed prefab>", "explore_current_region", "explore_other_region",
                            "travel_region_<observed index>", "craft_axe", "craft_pickaxe", "craft_torch",
                            "chop_nearest_tree", "mine_nearest_rock", "cook_food", "eat_safe_food",
                            "build_campfire", "build_sciencemachine", "equip_torch", "unequip_torch",
                            "equip_weapon", "attack_nearest_hostile", "flee_from_nearest_hostile", "wait"],
                "note": "Hints are not commands; actual candidates and safety rules take precedence."}
            context["planning_window"] = {"day": day, "quarter": quarter + 1, "quarters_per_day": 4}
            try:
                context["dst_knowledge"] = load_dst_knowledge(self.knowledge_path)
            except (OSError, UnicodeError) as exc:
                context["dst_knowledge"] = ""
                print(f"daily_plan_knowledge_unavailable={exc}")
            # Snapshot the actual request, not a later observation or mutated board.
            context = json.loads(json.dumps(context, ensure_ascii=False))
            self.pending_user_message = json.dumps(context, ensure_ascii=False, separators=(",", ":"))
            self.future = self.executor.submit(
                timed_request_plan, self.api_url, self.api_key, self.model, day, context,
                list(self.conversation),
            )
            self.pending_slot = slot
            if completing:
                self.last_completion_request = time.monotonic()
            self.completion_replan_slot = None
            print(
                f"daily_plan_requested day={day} quarter={quarter + 1}/4 "
                f"knowledge_chars={len(context['dst_knowledge'])} "
                f"history_turns={len(self.conversation)} reason={reason}"
            )
        view = self._board(state) if self.plan and self.plan["day"] == day else None
        self._publish_board(view)
        return view

    def record_action(self, state: dict, action: str, outcome: str) -> None:
        vitals = state.get("player", {}).get("vitals", {})
        self.history.append({
            "day": state.get("world", {}).get("day"),
            "phase": state.get("world", {}).get("phase"),
            "action": action,
            "outcome": outcome,
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
