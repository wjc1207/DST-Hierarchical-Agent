"""Claude-style file-based agentic memory: MEMORY.md index + active read/write/search/delete tools."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from pathlib import Path
import re

from common.paths import DATA_DIR

DEFAULT_MEMORY_ROOT = DATA_DIR / "agent_memory"
MAX_MEMORY_CHARS = 4000
MAX_ENTRIES_PER_SCOPE = 60
MAX_INDEX_CHARS = 3000
MEMORY_KINDS = {"lesson", "playbook", "preference", "convention", "fact"}
MEMORY_SOURCES = {"user", "inferred"}
RUNTIME_LIMIT_PATTERNS = (
    re.compile(r"\b(supported_capabilities|target_types|focused_task_unavailable|no_feasible_action_or_route|unsupported_action|task_errors)\b", re.I),
    re.compile(r"(不支持|无法执行|没有提供|exclude|unsupported).{0,24}\b(equip|unequip|target|action|schema|接口|运行时)\b", re.I),
    re.compile(r"\b(equip|unequip|target|action|schema)\b.{0,24}(不支持|不可用|unsupported|excluded|not supported)", re.I),
)


def capabilities_fingerprint(items) -> str | None:
    """Short deterministic hash of the live runtime capability set for stale-memory detection."""
    cleaned = sorted({str(x).strip() for x in (items or []) if isinstance(x, str) and x.strip()})
    if not cleaned:
        return None
    return hashlib.sha256("\n".join(cleaned).encode("utf-8")).hexdigest()[:12]


def _looks_like_runtime_limit_note(name: str, description: str, content: str) -> bool:
    blob = f"{name}\n{description}\n{content}"
    return any(pattern.search(blob) for pattern in RUNTIME_LIMIT_PATTERNS)


def sanitize_slug(value: str, *, field: str = "name", max_len: int = 64) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    raw = value.strip()
    slug = re.sub(r"[^A-Za-z0-9_-]+", "_", raw).strip("_").lower()
    if not slug or len(slug) > max_len:
        raise ValueError(f"{field} must produce a valid slug (1..{max_len} ASCII chars)")
    return slug


def sanitize_world_id(world_id: str | None) -> str | None:
    if not isinstance(world_id, str) or not world_id.strip():
        return None
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", world_id.strip()).strip("._")
    if not cleaned or cleaned in {".", ".."}:
        return None
    return cleaned[:96]


def _parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    if not text.startswith("---\n"):
        return {}, text.strip()
    end = text.find("\n---\n", 4)
    if end == -1:
        return {}, text.strip()
    meta: dict[str, str] = {}
    for line in text[4:end].splitlines():
        if ":" in line:
            key, val = line.split(":", 1)
            meta[key.strip()] = val.strip()
    return meta, text[end + 5:].strip()


def _format_entry(
    name: str,
    description: str,
    kind: str,
    content: str,
    updated_at: str,
    *,
    source: str = "inferred",
    capabilities_hash: str | None = None,
) -> str:
    clean_desc = " ".join(description.split())[:200]
    header = [
        "---",
        f"name: {name}",
        f"description: {clean_desc}",
        f"kind: {kind}",
        f"source: {source}",
    ]
    if capabilities_hash:
        header.append(f"capabilities_hash: {capabilities_hash}")
    header.append(f"updated_at: {updated_at}")
    header.append("---")
    return "\n".join(header) + f"\n\n{content.strip()}\n"


class AgentMemoryStore:
    """Sandboxed Markdown memory store scoped to global + one bound world_id."""

    def __init__(self, *, root: Path | None = None, world_id: str | None = None, capabilities_hash: str | None = None):
        self.root = Path(root) if root is not None else DEFAULT_MEMORY_ROOT
        self.world_id = sanitize_world_id(world_id)
        self.capabilities_hash = capabilities_hash

    def bind_world(self, world_id: str | None) -> None:
        self.world_id = sanitize_world_id(world_id)

    def set_capabilities_hash(self, capabilities_hash: str | None) -> None:
        self.capabilities_hash = capabilities_hash

    def _is_stale(self, meta: dict[str, str]) -> bool:
        if not self.capabilities_hash:
            return False
        source = meta.get("source") or ("user" if meta.get("kind") == "preference" else "inferred")
        if source == "user":
            return False
        stored_hash = meta.get("capabilities_hash") or ""
        return stored_hash != self.capabilities_hash

    def _scope_dir(self, scope: str) -> Path:
        if scope == "global":
            return self.root / "global"
        if scope == "world":
            if not self.world_id:
                raise ValueError("World memory is unavailable until a stable world_id is observed")
            return self.root / "worlds" / self.world_id
        raise ValueError("scope must be 'global' or 'world'")

    def _resolve_path(self, rel_path: str) -> tuple[str, Path]:
        if not isinstance(rel_path, str) or not rel_path.strip():
            raise ValueError("Memory path must be a non-empty string")
        normalized = rel_path.strip().replace("\\", "/")
        if ".." in normalized or normalized.startswith("/"):
            raise ValueError("Invalid memory path")
        parts = normalized.split("/")
        if len(parts) != 2 or parts[0] not in {"global", "world"} or not parts[1].endswith(".md"):
            raise ValueError("Memory path must be 'global/<name>.md' or 'world/<name>.md'")
        filename = parts[1]
        if filename.upper() == "MEMORY.MD":
            raise ValueError("MEMORY.md is managed automatically; read or write individual topic files")
        stem = filename[:-3]
        if not re.fullmatch(r"[a-z0-9_-]{1,64}", stem):
            raise ValueError("Invalid memory filename")
        base = self._scope_dir(parts[0]).resolve()
        target = (base / filename).resolve()
        if target.parent != base:
            raise ValueError("Memory path escapes sandbox")
        return parts[0], target

    def _list_scope_files(self, scope: str) -> list[Path]:
        try:
            directory = self._scope_dir(scope)
        except ValueError:
            return []
        if not directory.is_dir():
            return []
        return sorted(
            path for path in directory.glob("*.md")
            if path.is_file() and path.name.upper() != "MEMORY.MD"
        )

    def _rebuild_index(self, scope: str) -> None:
        directory = self._scope_dir(scope)
        directory.mkdir(parents=True, exist_ok=True)
        lines = [f"# {'Global' if scope == 'global' else 'World'} Memory Index", ""]
        for path in self._list_scope_files(scope):
            meta, _ = _parse_frontmatter(path.read_text(encoding="utf-8", errors="replace"))
            name = meta.get("name") or path.stem
            desc = meta.get("description") or ""
            kind = meta.get("kind") or "lesson"
            rel = f"{scope}/{path.name}"
            lines.append(f"- [{name}]({rel}) ({kind}): {desc}")
        index_path = directory / "MEMORY.md"
        tmp = directory / ".MEMORY.md.tmp"
        tmp.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")
        tmp.replace(index_path)

    def memory_write(
        self,
        scope: str,
        name: str,
        description: str,
        content: str,
        kind: str = "lesson",
        source: str | None = None,
    ) -> dict:
        if scope not in {"global", "world"}:
            raise ValueError("scope must be 'global' or 'world'")
        if kind not in MEMORY_KINDS:
            raise ValueError(f"kind must be one of: {', '.join(sorted(MEMORY_KINDS))}")
        resolved_source = source if source is not None else ("user" if kind == "preference" else "inferred")
        if resolved_source not in MEMORY_SOURCES:
            raise ValueError(f"source must be one of: {', '.join(sorted(MEMORY_SOURCES))}")
        if not isinstance(description, str) or not description.strip():
            raise ValueError("description must be a non-empty one-line summary")
        if not isinstance(content, str) or not content.strip():
            raise ValueError("content must be non-empty")
        if len(content) > MAX_MEMORY_CHARS:
            raise ValueError(f"content exceeds maximum length ({MAX_MEMORY_CHARS} chars)")
        if resolved_source != "user" and _looks_like_runtime_limit_note(name, description, content):
            raise ValueError(
                "Do not store runtime capability, action, or schema limitations in memory; "
                "always rely on live supported_capabilities and current observations."
            )
        slug = sanitize_slug(name, field="name")
        directory = self._scope_dir(scope)
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f"{slug}.md"
        existing = self._list_scope_files(scope)
        if not target.exists() and len(existing) >= MAX_ENTRIES_PER_SCOPE:
            raise ValueError(f"{scope} memory limit ({MAX_ENTRIES_PER_SCOPE}) reached; delete or update an existing memory first")
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        body = _format_entry(
            slug, description, kind, content, now,
            source=resolved_source, capabilities_hash=self.capabilities_hash,
        )
        tmp = directory / f".{slug}.md.tmp"
        tmp.write_text(body, encoding="utf-8")
        tmp.replace(target)
        self._rebuild_index(scope)
        return {
            "saved": True, "path": f"{scope}/{slug}.md", "scope": scope,
            "name": slug, "kind": kind, "source": resolved_source,
        }

    def memory_read(self, path: str) -> dict:
        scope, target = self._resolve_path(path)
        if not target.is_file():
            raise ValueError(f"Memory not found: {path}")
        raw = target.read_text(encoding="utf-8", errors="replace")
        meta, body = _parse_frontmatter(raw)
        return {
            "path": f"{scope}/{target.name}",
            "scope": scope,
            "name": meta.get("name") or target.stem,
            "description": meta.get("description", ""),
            "kind": meta.get("kind", "lesson"),
            "source": meta.get("source") or ("user" if meta.get("kind") == "preference" else "inferred"),
            "capabilities_hash": meta.get("capabilities_hash", ""),
            "stale": self._is_stale(meta),
            "updated_at": meta.get("updated_at", ""),
            "content": body,
        }

    def memory_delete(self, path: str) -> dict:
        scope, target = self._resolve_path(path)
        if not target.is_file():
            raise ValueError(f"Memory not found: {path}")
        target.unlink()
        self._rebuild_index(scope)
        return {"deleted": True, "path": f"{scope}/{target.name}"}

    def memory_search(self, query: str = "", scope: str = "all", limit: int = 6) -> dict:
        if scope not in {"all", "global", "world"}:
            raise ValueError("scope must be 'all', 'global', or 'world'")
        if type(limit) is not int or not 1 <= limit <= 20:
            raise ValueError("limit must be an integer in 1..20")
        if not isinstance(query, str) or len(query) > 200:
            raise ValueError("query must be a string up to 200 chars")
        terms = [t for t in re.split(r"\s+", query.casefold().strip()) if t]
        scopes = ["world", "global"] if scope == "all" else [scope]
        matches = []
        for current_scope in scopes:
            for path in self._list_scope_files(current_scope):
                raw = path.read_text(encoding="utf-8", errors="replace")
                meta, body = _parse_frontmatter(raw)
                name = meta.get("name") or path.stem
                desc = meta.get("description", "")
                kind = meta.get("kind", "lesson")
                haystack = f"{name}\n{desc}\n{kind}\n{body}".casefold()
                if terms:
                    score = sum(haystack.count(term) for term in terms)
                    if score == 0:
                        continue
                else:
                    score = 1
                snippet = " ".join(body.split())[:180]
                matches.append((
                    score,
                    meta.get("updated_at", ""),
                    {
                        "path": f"{current_scope}/{path.name}",
                        "scope": current_scope,
                        "name": name,
                        "description": desc,
                        "kind": kind,
                        "snippet": snippet,
                    },
                ))
        matches.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return {"matches": [item[2] for item in matches[:limit]], "total": len(matches)}

    def index_prompt(self, *, max_chars: int = MAX_INDEX_CHARS) -> str:
        """Render concise MEMORY.md indexes with provenance & stale-capability tagging."""
        sections = []
        for scope, title in (("global", "Global Game Memory (cross-save)"), ("world", "Current World Memory")):
            files = self._list_scope_files(scope)
            if not files:
                continue
            lines = [f"### {title}"]
            for path in files:
                meta, _ = _parse_frontmatter(path.read_text(encoding="utf-8", errors="replace"))
                name = meta.get("name") or path.stem
                desc = meta.get("description") or ""
                kind = meta.get("kind") or "lesson"
                source = meta.get("source") or ("user" if kind == "preference" else "inferred")
                stale_tag = " [stale-capabilities: verify live or delete]" if self._is_stale(meta) else ""
                lines.append(f"- [{name}]({scope}/{path.name}) ({kind}, source={source}){stale_tag}: {desc}")
            sections.append("\n".join(lines))
        guidance = (
            "Agent Memory Tools (do NOT pass buddy_id): You have persistent Markdown memory via memory_search, memory_read, memory_write, and memory_delete. "
            "Truth Hierarchy: live supported_capabilities & current game snapshot > player's latest message > memory notes below. "
            "Verify Before Acting: memory notes are point-in-time pointers, NEVER absolute truth. Never refuse a request solely from a memory note without checking live supported_capabilities/observations first; "
            "if a memory conflicts with live facts or is marked [stale-capabilities], call memory_delete immediately instead of patching it. "
            "Use scope='world' for this save's camp conventions or player preferences, and scope='global' for pure reusable DST gameplay playbooks. "
            "IMPORTANT: Actively call memory_write BEFORE your final JSON response when the player asks you to remember a standing rule/preference (use source='user', kind='preference'). "
            "What NOT to save: NEVER save runtime/API/schema limits, 'action/target unsupported' notes, transient error codes, recipe ingredient counts, current stock, or temporary coordinates. Use absolute game days (Day N)."
        )
        if not sections:
            return guidance + "\n<agent_memory_notes priority=\"low\">\n(No saved memory entries yet.)\n</agent_memory_notes>"
        combined = "\n\n".join(sections)
        if len(combined) > max_chars:
            combined = combined[:max_chars].rstrip() + "\n...(index truncated; use memory_search)"
        return (
            guidance
            + "\n<agent_memory_notes priority=\"low\" note=\"Point-in-time notes; live supported_capabilities and current snapshot always override.\">\n"
            + combined
            + "\n</agent_memory_notes>"
        )

    def tool_specs(self) -> list[dict]:
        return [
            {
                "type": "function",
                "function": {
                    "name": "memory_search",
                    "description": "Search saved global and current-world Markdown memory files by keyword.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {"type": "string", "description": "Keywords to match against memory names, descriptions, and content."},
                            "scope": {"type": "string", "enum": ["all", "global", "world"]},
                            "limit": {"type": "integer", "minimum": 1, "maximum": 20},
                        },
                        "additionalProperties": False,
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "memory_read",
                    "description": "Read the full content of a specific memory file listed in MEMORY.md or memory_search.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string", "description": "Relative memory path: 'global/<name>.md' or 'world/<name>.md'."},
                        },
                        "required": ["path"],
                        "additionalProperties": False,
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "memory_write",
                    "description": "Create or update a persistent Markdown memory entry and refresh MEMORY.md. Never store runtime/API/schema limitations.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "scope": {"type": "string", "enum": ["global", "world"]},
                            "name": {"type": "string", "description": "Short topic slug, e.g. 'night_torch_timing'."},
                            "description": {"type": "string", "description": "One-line summary shown in MEMORY.md."},
                            "content": {"type": "string", "description": "Concise actionable memory body (markdown)."},
                            "kind": {"type": "string", "enum": sorted(MEMORY_KINDS)},
                            "source": {"type": "string", "enum": sorted(MEMORY_SOURCES), "description": "'user' for explicit player instructions/preferences; 'inferred' for agent-derived gameplay notes."},
                        },
                        "required": ["scope", "name", "description", "content"],
                        "additionalProperties": False,
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "memory_delete",
                    "description": "Delete an outdated or incorrect memory entry and refresh MEMORY.md.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string", "description": "Relative memory path: 'global/<name>.md' or 'world/<name>.md'."},
                        },
                        "required": ["path"],
                        "additionalProperties": False,
                    },
                },
            },
        ]

    def tool_functions(self) -> dict:
        return {
            "memory_search": self.memory_search,
            "memory_read": self.memory_read,
            "memory_write": self.memory_write,
            "memory_delete": self.memory_delete,
        }
