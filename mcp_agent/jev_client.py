"""JEV API client and decision context. No keyboard, window or game-log dependencies."""
from __future__ import annotations
import json
import urllib.error
import urllib.request
from typing import Dict, Optional
from common import timing_log as timing
from common.task_board import board_view
from .model_state import model_game_state
from .navigation_intents import exploration_key

DEFAULT_API_URL = "https://api.typesafe.ai/v1/systemone"
DEFAULT_MODEL = "jev-latest"

@timing.timed("decision.state_compact")
def compact_game_state(state: dict, daily_plan: Optional[dict] = None) -> dict:
    compact = {
        **model_game_state(state),
        "goal": "Survive indefinitely and steadily improve food, light, tools, resources, and safety.",
        "hard_rules": [
            "Choose only an action listed in the available criteria; unavailable actions cannot be executed.",
            "At night without an equipped torch, exploration and collection are blocked even beside a stationary fire; fleeing an immediate threat is an exception.",
            "A torch can be equipped only at night and only when no lit campfire or firepit is nearby. A campfire can be built only at night when no lit fire is nearby.",
        ],
        "decision_guidance": [
            "Exploration and known-region travel are owned by the executor, not by you. Choose a useful available local action or wait to return control; wait is not a request to stop safe exploration. Resource opportunities and survival changes can interrupt navigation, whose intent is preserved for resumption.",
            "Both models use the same game-state fields. inventory_details separates backpack items and equipped slots; durability_percent is a 0..1 remaining fraction (torch fuel), with missing values unknown. lighting.observed_fires reports current burning and distance, not remaining fire lifetime. state_basis marks observed versus predicted snapshots.",
            "Exploration watches the SET of currently collectible, reachable resource product types, not individual GUIDs or counts. Ordinary resource changes yield only after at least 4 world units of displacement from the execution start and while that set differs; threats and other safety/navigation events may interrupt earlier.",
            "Assess immediate threats, darkness, and starvation before optional progress; compare the remaining safe actions rather than following a fixed script.",
            "eat_safe_food is a complete eating action: if no preferred cooked food is carried and a lit cooker is nearby, it cooks one item and then eats; cook_food alone only prepares food for later.",
            "craft_torch is a complete native action: if DST auto-equips the new torch before night, Lua unequips it to preserve fuel.",
            "chop_nearest_tree and mine_nearest_rock include bounded pickup of nearby work drops, including secondary materials. Collect leftovers separately only when useful items remain visible.",
            "At night near a lit fire, unequip a torch to save fuel only if staying near that fire; keep it equipped when moving away.",
            "Prefer a useful safe action over repeated waiting, but do not take unnecessary risks for optional resources.",
            "A nearby potentialThreat may attack on approach even when activeThreat is false; avoid routes through remembered hazards.",
            "Regions are game topology rooms, not individual ground tiles; mixed terrain can belong to one region. Names have stable topology IDs, and observed area is not total area or a completion percentage.",
            "The executor selects local frontiers, unknown exits and known-region travel from the task board and observations. These are not your action choices; focus on worthwhile local actions or return control with wait.",
            "A task_board (or legacy daily_plan) is strategic advice from a separate planner, not a command. Prefer ready/in_progress tasks with higher priority (1 highest); ignore done, blocked or expired tasks. Choose the fastest safe available next action that advances a task. You may respond to new opportunities and emergencies instead; never generate a plan or wait merely because a hint is unavailable.",
            "Only observed game facts prove completion. Hypothetical snapshots are not evidence of an executed action; Lua verifies live feasibility and effects.",
        ],
    }
    if daily_plan is not None:
        if daily_plan.get("kind") == "action_sequence":
            compact["action_sequence"] = daily_plan
            compact["decision_guidance"] = [
                "The executor owns this ordered action_sequence. You are called only for an immediate "
                "safety interruption. Resolve threats, unsafe light or urgent hunger with a supplied "
                "local option; do not replace the sequence with optional gathering. The executor resumes it afterward."
            ]
        elif daily_plan.get("kind") == "task_board":
            if daily_plan.get("execution_policy") == "priority_goals":
                compact["task_board"] = daily_plan
                compact["decision_guidance"] = [
                    "The executor continuously advances this priority board and owns all navigation. "
                    "You arbitrate a safety interruption or a new off-board opportunity, not each plant. "
                    "For safety, resolve the immediate threat/light/hunger issue. For an opportunity, "
                    "choose a worthwhile bounded local action or wait to continue goal navigation. "
                    "Do not create a plan or repeat done/blocked tasks. See task_board.handoff_reason."
                ]
            else:
                compact["task_board"] = board_view(daily_plan, state)
        else:
            compact["daily_plan"] = daily_plan
    return compact


@timing.timed("decision.request")
def call_jev(
    api_url: str, api_key: str, model: str, state: dict,
    criteria: Dict[str, str], daily_plan: Optional[dict] = None,
) -> dict:
    # Also enforce the boundary for direct callers, not only the control loop.
    criteria = {k: v for k, v in criteria.items() if not exploration_key(k)}
    with timing.span("api.encode_state"):
        state_json = json.dumps(compact_game_state(state, daily_plan), ensure_ascii=False, separators=(",", ":"))
    body = {
        "state": state_json,
        "model": model,
        "questions": {
            "next_action": {
                "type": "choice",
                "instructions": (
                    "Choose exactly one action key from the supplied criteria. Treat each key as "
                    "one semantic action, even when Lua performs several verified substeps. "
                    "The executor owns exploration; choose only among local actions. "
                    "Decide the next action quickly from current state and criteria; do not plan ahead. "
                    "Obey hard rules and immediate survival needs. Treat task_board or legacy daily_plan, when present, "
                    "as optional strategic guidance, never as permission to ignore current facts. "
                    "When action_sequence is present, handle the safety interruption; do not replace it with optional gathering. "
                    "Do not request unavailable or redundant substeps."
                ),
                "criteria": criteria,
            }
        },
    }
    with timing.span("api.encode_request"):
        encoded_request = json.dumps(body, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        api_url,
        data=encoded_request,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "jev-dst-agent/0.1",
        },
        method="POST",
    )
    try:
        with timing.span("api.response_headers"):
            response = urllib.request.urlopen(request, timeout=20)
        with response:
            with timing.span("api.read_body"):
                response_body = response.read()
            with timing.span("api.parse_response"):
                payload = json.loads(response_body.decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:1000]
        raise RuntimeError(f"JEV API HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"JEV API connection failed: {exc.reason}") from exc

    answer = payload.get("answers", {}).get("next_action")
    if not isinstance(answer, dict) or not answer.get("choice"):
        raise RuntimeError("JEV response did not contain answers.next_action.choice")
    if answer["choice"] not in criteria:
        raise RuntimeError(f"JEV selected unavailable local action: {answer['choice']}")
    return {"response": payload, "answer": answer}
