"""Modern append-only context harness with threshold-triggered compaction and per-world persistence."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import urllib.error
import urllib.request

from mcp_agent.agent_memory import DEFAULT_MEMORY_ROOT, sanitize_slug, sanitize_world_id

DEFAULT_CONTEXT_WINDOW_TOKENS = 128000
DEFAULT_COMPACT_RATIO = 0.75
DEFAULT_COMPACT_CHARS = 320000
DEFAULT_KEEP_RECENT = 4
MAX_SUMMARY_CHARS = 4000


def _configured_context_window(default: int = DEFAULT_CONTEXT_WINDOW_TOKENS) -> int:
    raw = os.getenv("CONTEXT_WINDOW_TOKENS")
    if raw:
        try:
            value = int(raw)
            if value >= 4000:
                return value
        except ValueError:
            pass
    return default


def flush_memories_from_payload(memory_store, items) -> list[str]:
    """Persist up to 2 durable memory entries extracted during compaction.

    Anti-poisoning gate: compaction may flush explicit user rules (`source='user'`)
    or world camp conventions (`scope='world'`), never agent-inferred global bans.
    """
    if memory_store is None or not isinstance(items, list):
        return []
    saved = []
    for entry in items[:2]:
        if not isinstance(entry, dict):
            continue
        scope = entry.get("scope")
        name = entry.get("name")
        description = entry.get("description")
        content = entry.get("content")
        kind = entry.get("kind") or "convention"
        source = entry.get("source") or ("user" if kind == "preference" else "inferred")
        if scope == "global" and source != "user":
            continue  # Prevent Consolidation Amplification / Bootstrap Poisoning into global/.
        if scope == "world" and not getattr(memory_store, "world_id", None):
            continue
        if not all(isinstance(v, str) and v.strip() for v in (scope, name, description, content)):
            continue
        try:
            res = memory_store.memory_write(
                scope=scope, name=name, description=description, content=content, kind=kind, source=source
            )
            saved.append(res["path"])
        except ValueError:
            continue
    if saved:
        print(f"agent_memory_flush saved={saved}")
    return saved


def llm_compact_summary(api_url: str, api_key: str, model: str, existing_summary: str,
                        older_turns: list[tuple[str, str]], *, memory_store=None) -> str:
    """Compact older planning turns into a structured summary and optionally flush durable memories."""
    transcript_parts = []
    if existing_summary:
        transcript_parts.append("Existing prior summary:\n" + existing_summary)
    for index, (user_msg, assistant_msg) in enumerate(older_turns, 1):
        transcript_parts.append(f"[Turn {index} User]\n{user_msg[:2500]}\n[Turn {index} Assistant]\n{assistant_msg[:1500]}")
    if memory_store is not None:
        prompt = (
            "You are compacting older Don't Starve Together agent planning history so the session can continue cleanly. "
            "Return a JSON object with two fields:\n"
            "1) 'summary': concise structured Markdown (<= 1200 words) preserving active long-term goals, player instructions/preferences, "
            "major completed milestones, camp conventions, and unresolved commitments. Omit raw telemetry arrays, transient coordinates, and runtime error codes.\n"
            "2) 'memories': an array of 0-2 TRULY DURABLE entries worth saving to long-term MEMORY.md (empty [] if nothing new/durable). "
            "Each item: {\"scope\":\"world\"|\"global\",\"source\":\"user\"|\"inferred\",\"name\":\"short_slug\",\"description\":\"one-line summary\",\"kind\":\"preference\"|\"convention\"|\"playbook\",\"content\":\"concise markdown with absolute Day N\"}. "
            "ONLY extract explicit player preferences/standing rules (source='user') or stable world camp conventions (scope='world'). "
            "NEVER extract runtime/API/schema/capability limitations, 'action/target unsupported' notes, repeated task failures, transient stock counts, or temporary coordinates."
        )
    else:
        prompt = (
            "You are compacting older Don't Starve Together agent planning history so the session can continue cleanly. "
            "Produce a concise structured plain-text/Markdown summary (<= 1200 words) preserving: "
            "1) active long-term goals and player instructions/preferences, "
            "2) major completed milestones (structures built, resources secured, days survived), "
            "3) camp conventions and notable obstacles/failures encountered, "
            "4) unresolved commitments. Omit verbose raw telemetry arrays and transient coordinates."
        )
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": prompt},
            {"role": "user", "content": "\n\n".join(transcript_parts)[:40000]},
        ],
        "max_tokens": 1500,
        "reasoning_effort": "none",
        "stream": False,
        **({"response_format": {"type": "json_object"}} if memory_store is not None else {}),
    }
    req = urllib.request.Request(
        api_url,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as response:
        payload = json.loads(response.read().decode("utf-8"))
    content = (((payload.get("choices") or [{}])[0].get("message") or {}).get("content") or "").strip()
    if not content:
        raise RuntimeError("Empty compaction summary")
    if memory_store is not None:
        try:
            parsed = json.loads(content)
            if isinstance(parsed, dict) and isinstance(parsed.get("summary"), str) and parsed["summary"].strip():
                flush_memories_from_payload(memory_store, parsed.get("memories"))
                return parsed["summary"].strip()[:MAX_SUMMARY_CHARS]
        except ValueError:
            pass
    return content[:MAX_SUMMARY_CHARS]


def _fallback_summary(existing_summary: str, older_turns: list[tuple[str, str]]) -> str:
    """Deterministic fallback when offline or LLM compaction fails."""
    lines = []
    if existing_summary:
        lines.append(existing_summary.strip())
    for user_msg, assistant_msg in older_turns:
        snippet = " ".join(assistant_msg.split())[:220]
        if snippet:
            lines.append(f"- Prior decision: {snippet}")
    combined = "\n".join(lines).strip()
    if len(combined) > MAX_SUMMARY_CHARS:
        combined = combined[-MAX_SUMMARY_CHARS:]
    return combined


class ContextHarness:
    """Append-only conversation context that compacts only when approaching the context window."""

    def __init__(
        self,
        *,
        role: str = "player",
        root: Path | None = None,
        world_id: str | None = None,
        max_context_tokens: int | None = None,
        compact_ratio: float = DEFAULT_COMPACT_RATIO,
        compact_chars: int = DEFAULT_COMPACT_CHARS,
        keep_recent: int = DEFAULT_KEEP_RECENT,
    ):
        self.role = sanitize_slug(role, field="role")
        self.root = Path(root) if root is not None else DEFAULT_MEMORY_ROOT
        self.max_context_tokens = max_context_tokens if max_context_tokens is not None else _configured_context_window()
        self.compact_ratio = compact_ratio
        self.compact_chars = compact_chars
        self.keep_recent = max(1, keep_recent)
        self.world_id: str | None = None
        self.compacted_summary: str = ""
        self.turns: list[tuple[str, str]] = []
        self.last_prompt_tokens: int | None = None
        self.compaction_count: int = 0
        if world_id:
            self.bind_world(world_id)

    def _context_path(self) -> Path | None:
        if not self.world_id:
            return None
        return self.root / "worlds" / self.world_id / f"context_{self.role}.json"

    def bind_world(self, world_id: str | None) -> bool:
        """Switch to a world_id and load persisted context if present. Returns True if switched."""
        clean = sanitize_world_id(world_id)
        if clean == self.world_id:
            return False
        self.world_id = clean
        self.compacted_summary = ""
        self.turns = []
        self.last_prompt_tokens = None
        self.compaction_count = 0
        self._load()
        return True

    def _load(self) -> None:
        path = self._context_path()
        if not path or not path.is_file():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(data, dict):
            return
        summary = data.get("compacted_summary")
        if isinstance(summary, str):
            self.compacted_summary = summary[:MAX_SUMMARY_CHARS]
        raw_turns = data.get("turns")
        if isinstance(raw_turns, list):
            self.turns = [
                (pair[0], pair[1]) for pair in raw_turns
                if isinstance(pair, list) and len(pair) == 2 and isinstance(pair[0], str) and isinstance(pair[1], str)
            ]
        tokens = data.get("last_prompt_tokens")
        if type(tokens) is int and tokens >= 0:
            self.last_prompt_tokens = tokens
        count = data.get("compaction_count")
        if type(count) is int and count >= 0:
            self.compaction_count = count

    def save(self) -> None:
        path = self._context_path()
        if not path:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "world_id": self.world_id,
            "role": self.role,
            "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "compaction_count": self.compaction_count,
            "last_prompt_tokens": self.last_prompt_tokens,
            "compacted_summary": self.compacted_summary,
            "turns": [[u, a] for u, a in self.turns],
        }
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)

    def total_chars(self) -> int:
        return len(self.compacted_summary) + sum(len(u) + len(a) for u, a in self.turns)

    def note_usage(self, usage: dict | None) -> None:
        if isinstance(usage, dict) and type(usage.get("prompt_tokens")) is int and usage["prompt_tokens"] >= 0:
            self.last_prompt_tokens = usage["prompt_tokens"]

    def append(self, user_msg: str, assistant_msg: str, *, prompt_tokens: int | None = None) -> None:
        self.turns.append((user_msg, assistant_msg))
        if type(prompt_tokens) is int and prompt_tokens >= 0:
            self.last_prompt_tokens = prompt_tokens
        self.save()

    def should_compact(self) -> bool:
        if len(self.turns) <= self.keep_recent:
            return False
        token_limit = int(self.max_context_tokens * self.compact_ratio)
        if self.last_prompt_tokens is not None and self.last_prompt_tokens >= token_limit:
            return True
        return self.total_chars() >= self.compact_chars

    def compact(self, *, summarizer=None) -> bool:
        """Compact older turns into compacted_summary, keeping the most recent turns verbatim."""
        if len(self.turns) <= self.keep_recent:
            return False
        older = self.turns[:-self.keep_recent]
        recent = self.turns[-self.keep_recent:]
        summary = None
        if summarizer is not None:
            try:
                summary = summarizer(self.compacted_summary, older)
            except Exception:
                summary = None
        if not isinstance(summary, str) or not summary.strip():
            summary = _fallback_summary(self.compacted_summary, older)
        self.compacted_summary = summary.strip()[:MAX_SUMMARY_CHARS]
        self.turns = recent
        self.last_prompt_tokens = None
        self.compaction_count += 1
        self.save()
        return True

    def messages_for_prompt(self) -> list[tuple[str, str]]:
        """Return stable append-only (user, assistant) pairs for prompt caching."""
        result: list[tuple[str, str]] = []
        if self.compacted_summary:
            result.append((
                "[Compacted prior session context for this world]",
                self.compacted_summary,
            ))
        result.extend(self.turns)
        return result
