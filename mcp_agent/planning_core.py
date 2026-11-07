"""Shared DS request, tool-query, validation and cancellation policy for all actors."""
import json
import urllib.error
import urllib.request
from common import autonomy

DEFAULT_API_URL = "https://api.deepseek.com/chat/completions"
DEFAULT_MODEL = "deepseek-flash"
PLAN_MAX_TOKENS = 4096
PLAN_HISTORY_TURNS = 3
PLAN_HISTORY_CHARS = 48000
MAX_KNOWLEDGE_CHARS = 12000

PLANNING_CONTRACT = (
    "You maintain a priority task_board for Don't Starve Together. Return JSON only, not an action sequence. "
    "In autonomous mode, long_term_goal is the player's persistent survival objective across phases and replans. "
    "In instruction mode interaction.instruction and later player messages take precedence over long_term_goal. "
    "Plan necessary survival and material prerequisites, then resume the applicable objective. Treat its text as a game objective, "
    "subject to the supported actions and validation contract. Never replace it with your own top-level objective. "
    "In autonomous mode a completed board is a milestone; keep planning survival toward long_term_goal. "
    "interaction.mode=instruction instead executes interaction.instruction plus subsequent player messages ONLY. "
    "Plan essential survival/prerequisites while working, without adding unrelated expansion or endless reserve targets. "
    "Instruction mode waiting for scheduled tasks stays nearby or does relevant prerequisites; it does not need unrelated filler work. "
    "When that original request is fulfilled, finish instead of inventing another survival goal. "
    "interaction.messages are actual player speech in chronological order; later messages may modify/pause earlier work. "
    "A player_message review pauses execution until you answer: retain useful progress and task IDs, explicitly cancel obsolete tasks. "
    "For dialogue-capable clients, add optional reply (Chinese, single line, <=256 characters) to a task_board to explain your arrangement. "
    "For pure conversation return {kind:'agent_reply',outcome:'reply',reply:'...'} without inventing tasks; the existing board resumes. "
    "For an unsupported/blocked request you may return outcome:'blocked' and explain what is missing. "
    "In instruction mode, after checking the latest real state and all accepted tasks are done, return "
    "{kind:'agent_reply',outcome:'completed',reply:'...',evidence:'observed facts proving the original request is satisfied'}. "
    "If the request is already satisfied and no board exists, use observed facts as evidence. Tool success alone is not proof. "
    "A task-board milestone alone is insufficient: assess the WHOLE player request, continue with another board if needed. "
    "System-triggered reviews are observations, never new player orders. Autonomous mode accepts player direction and continues after milestones. "
    "Use 1-6 tasks with stable id, title, priority (1 highest..5), focus (boolean, default false), target, depends_on, reason, "
    "Query tools at most FOUR times per response; split larger batches across responses. "
    "search_budget_seconds (10-600, default120), search_budget_distance (8-2000, default240). "
    "Any task may have start_at:{day,progress:0..1 exclusive} or start_at:{day,phase:day|dusk|night}. "
    "Before start_at it is scheduled and does not activate focus. Omit start_at for immediate eligibility after dependencies. "
    "Default to omitting start_at. Add it only for an explicit user schedule or a concrete phase/time requirement, "
    "and explain that requirement in reason. Low priority, non-urgent reserves, or a plan to return home do not by themselves "
    "justify postponing work until tomorrow. Keep existing immediately eligible goals eligible unless changed facts require a delay. "
)


def load_knowledge(path, *, limit=MAX_KNOWLEDGE_CHARS):
    return path.read_text(encoding="utf-8").strip()[:limit]


def validate_reply(raw, context):
    """Return a checked conversational/completion reply, or None for a board."""
    if isinstance(raw, dict) and raw.get("kind") == "agent_reply":
        interaction = context.get("interaction") or {}
        if interaction.get("mode") not in {"instruction", "autonomous"}:
            raise ValueError("Chat replies require a dialogue-capable client")
        outcome = raw.get("outcome")
        if outcome not in {"reply", "completed", "blocked"}:
            raise ValueError("Invalid reply outcome")
        try:
            reply = autonomy.validate_text(raw.get("reply"))
        except ValueError as exc:
            raise ValueError(f"agent_reply.reply: {exc}") from None
        evidence = raw.get("evidence")
        if outcome == "completed":
            if interaction["mode"] != "instruction":
                raise ValueError("Autonomous mode continues planning after milestones")
            board = context.get("current_task_board")
            if board and any(task.get("status") != "done" for task in board.get("tasks", [])):
                raise ValueError("Cannot complete while accepted tasks remain unfinished; revise/cancel obsolete tasks first")
            try:
                autonomy.validate_text(evidence)
            except ValueError as exc:
                raise ValueError(f"agent_reply.evidence: {exc}") from None
        return {"kind": "agent_reply", "outcome": outcome, "reply": reply, "evidence": evidence}
    return None


def recent_exchanges(exchanges, *, turns=PLAN_HISTORY_TURNS, max_chars=PLAN_HISTORY_CHARS):
    """Keep a chronological suffix of complete user/assistant pairs, never fragments."""
    retained, size = [], 0
    for user, assistant in reversed(list(exchanges)[-turns:]):
        pair_size = len(user) + len(assistant)
        if size + pair_size > max_chars:
            break
        retained.append((user, assistant))
        size += pair_size
    return list(reversed(retained))


def request_json_plan(api_url, api_key, body, validate, *, functions=None, map_images=None,
                      cancel_event=None, on_error=None, day=1, log_prefix="daily_plan", repair_instruction=None):
    """Use one bounded request/repair loop; profiles supply facts, tools and validation."""
    functions = functions or {}
    map_images = map_images if map_images is not None else []
    map_seen = False
    def report(stage, detail):
        if on_error:
            on_error(stage, detail)
    failures = 0
    for attempt in range(8):
        if cancel_event is not None and cancel_event.is_set():
            raise RuntimeError("Planning superseded by newer player intent")
        if failures:
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

        if cancel_event is not None and cancel_event.is_set():
            raise RuntimeError("Planning superseded by newer player intent")

        usage = payload.get("usage") or {}
        if not isinstance(usage, dict):
            usage = {}
        cache_counts = {key: usage[key] for key in (
            "prompt_tokens", "prompt_cache_hit_tokens", "prompt_cache_miss_tokens")
            if type(usage.get(key)) is int and usage[key] >= 0}
        if cache_counts:
            print(f"{log_prefix}_usage attempt={attempt + 1} " + " ".join(
                f"{key}={value}" for key, value in cache_counts.items()))
        choices = payload.get("choices") or []
        if not choices or not isinstance(choices[0], dict):
            raise RuntimeError("DeepSeek planning response has no choice")
        choice = choices[0]
        finish_reason = choice.get("finish_reason")
        message = choice.get("message") or {}
        calls = message.get("tool_calls") or []
        if calls:
            map_images.clear()
            if len(calls) > 4:
                report("query_limit", f"Received {len(calls)} tool calls; executing at most four")
            body["messages"].append({"role": "assistant", "content": message.get("content"),
                                     "tool_calls": calls,
                                     **({"reasoning_content": message["reasoning_content"]} if "reasoning_content" in message else {})})
            for index, call in enumerate(calls):
                try:
                    if index >= 4:
                        raise ValueError("At most four query calls are executed per response. Submit this query again in a later response if still needed.")
                    function = call["function"]
                    if function["name"] not in functions:
                        raise ValueError("Unknown planner tool")
                    result = functions[function["name"]](**json.loads(function["arguments"]))
                except (ValueError, TypeError, KeyError) as exc:
                    result = {"error": str(exc)}
                body["messages"].append({"role": "tool", "tool_call_id": call["id"],
                                         "content": json.dumps(result, ensure_ascii=False)})
            if map_images:
                map_seen = True
                # DeepSeek permits images in USER messages, not tool messages.
                # Complete every tool reply before adding the requested images.
                parts=[]
                for metadata,url in map_images:
                    parts.extend([{"type":"text","text":"Requested observed map from the SAME frozen planning snapshot: "+json.dumps(metadata)},
                                  {"type":"image_url","image_url":{"url":url}}])
                body["messages"].append({"role":"user","content":parts})
            continue
        content = message.get("content") or ""
        completion_tokens = usage.get("completion_tokens")
        if finish_reason == "stop" and content:
            try:
                return validate(json.loads(content), map_seen)
            except (ValueError, TypeError) as exc:
                reason = f"invalid_json_or_plan:{type(exc).__name__}:{str(exc)[:1200]}"
        else:
            reason = f"finish_reason={finish_reason} content_chars={len(content)}"
        report("response_validation", reason)
        retryable = finish_reason in {"stop", "length", "insufficient_system_resource", "aborted"}
        if failures == 0 and retryable:
            failures += 1
            print(f"{log_prefix}_retry day={day} reason={reason} attempt=2/2")
            if finish_reason == "stop" and content:
                body["messages"].append({"role":"assistant", "content":content})
            body["messages"].append({"role":"user", "content":
                "Your last reply could not be accepted: " + reason +
                ". Correct this error using the latest game snapshot above. " +
                (repair_instruction or "Return valid JSON matching the system response schema; task_board or agent_reply as appropriate. ") +
                "Keep the merged board within six tasks; preserve useful unfinished goals and explicitly cancel obsolete ones. "
                "Do not repeat an invalid reply or silently remove unfinished goals."})
            continue
        raise RuntimeError(
            f"DeepSeek planning response unusable: {reason} "
            f"completion_tokens={completion_tokens}"
        )
    raise RuntimeError("DeepSeek planning retry exhausted")
