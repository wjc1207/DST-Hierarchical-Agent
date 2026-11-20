"""Event-driven external agent backends (Claude Code, Codex CLI, DSH CLI/HTTP)."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import time
import urllib.error
import urllib.request

from mcp_agent.agent_memory import DEFAULT_MEMORY_ROOT, sanitize_slug, sanitize_world_id
from mcp_agent.companion_planner import (
    COMPACT_FORMAT,
    CompanionPlanningError,
    companion_profile,
    validate_team_plan,
)
from mcp_agent.daily_planner import (
    PLAYER_TASK_FORMAT,
    planning_reference,
    snapshot_message,
    validate_player_board,
)
from mcp_agent.planning_core import PLANNING_RULES, TASK_START_RULES

BACKENDS = ("deepseek", "claude", "codex", "dsh", "dsh-http")
DEFAULT_TIMEOUT_SECONDS = 120


def extract_json_object(text: str) -> dict:
    """Extract the first valid JSON object from raw or markdown-fenced model output."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("External agent returned empty output")
    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()
    try:
        parsed = json.loads(cleaned)
        if isinstance(parsed, dict):
            return parsed
    except ValueError:
        pass
    decoder = json.JSONDecoder()
    for idx, ch in enumerate(cleaned):
        if ch == "{":
            try:
                parsed, _ = decoder.raw_decode(cleaned[idx:])
                if isinstance(parsed, dict):
                    return parsed
            except ValueError:
                continue
    raise ValueError(f"Could not parse JSON object from external agent output: {cleaned[:200]}")


def _resolve_command(default_bin: str, override: str | None = None) -> list[str]:
    if override:
        parts = shlex.split(override, posix=(os.name != "nt"))
        if not parts:
            raise ValueError("Empty --backend-cmd")
        resolved = shutil.which(parts[0])
        if resolved:
            parts[0] = resolved
    else:
        resolved = shutil.which(default_bin)
        parts = [resolved or default_bin]
    if os.name == "nt" and parts[0].lower().endswith(".ps1"):
        for ext in (".cmd", ".exe", ".bat"):
            sibling = Path(parts[0]).with_suffix(ext)
            if sibling.is_file():
                parts[0] = str(sibling)
                break
        else:
            parts = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", *parts]
    return parts


def _run_cancellable(
    cmd: list[str],
    *,
    input_text: str | None = None,
    cancel=None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    cwd: str | None = None,
) -> tuple[int, str, str]:
    """Run a CLI command and terminate it promptly if a newer game intent cancels planning."""
    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=cwd,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(
            f"External backend command not found: {cmd[0]!r}. "
            "Check PATH or pass --backend-cmd <executable> (for DSH HTTP use --backend dsh-http --dsh-url <url>)."
        ) from exc
    if input_text is not None and proc.stdin is not None:
        import threading as _threading
        def _feed():
            try:
                proc.stdin.write(input_text)
            except OSError:
                pass
            finally:
                try:
                    proc.stdin.close()
                except OSError:
                    pass
        _threading.Thread(target=_feed, daemon=True).start()
    deadline = time.monotonic() + timeout
    try:
        while True:
            if cancel is not None and cancel.is_set():
                proc.terminate()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()
                raise RuntimeError("Planning superseded by newer player intent")
            code = proc.poll()
            if code is not None:
                stdout, stderr = proc.communicate()
                return code, stdout or "", stderr or ""
            if time.monotonic() >= deadline:
                proc.terminate()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()
                raise RuntimeError(f"External agent CLI timed out after {timeout:.0f}s")
            time.sleep(0.1)
    except Exception:
        if proc.poll() is None:
            proc.kill()
        raise


class SessionStore:
    """Persist external CLI session IDs per world_id and backend so saves resume cleanly."""

    def __init__(self, *, root: Path | None = None):
        self.root = Path(root) if root is not None else DEFAULT_MEMORY_ROOT

    def _path(self, world_id: str | None, backend: str, role: str) -> Path | None:
        wid = sanitize_world_id(world_id)
        if not wid:
            return None
        return self.root / "worlds" / wid / f"external_{sanitize_slug(backend)}_{sanitize_slug(role)}.json"

    def get(self, world_id: str | None, backend: str, role: str) -> str | None:
        path = self._path(world_id, backend, role)
        if not path or not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            sid = data.get("session_id")
            return sid if isinstance(sid, str) and sid.strip() else None
        except (OSError, ValueError):
            return None

    def save(self, world_id: str | None, backend: str, role: str, session_id: str | None) -> None:
        if not session_id:
            return
        path = self._path(world_id, backend, role)
        if not path:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"session_id": session_id, "backend": backend, "role": role}, indent=2), encoding="utf-8")
        tmp.replace(path)


class ExternalAgentBackend:
    """Adapter that routes game planning events to Claude Code, Codex CLI, or DSH."""

    def __init__(
        self,
        backend: str,
        *,
        role: str = "companion",
        command: str | None = None,
        model: str | None = None,
        dsh_url: str | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        memory_root: Path | None = None,
    ):
        if backend not in {"claude", "codex", "dsh", "dsh-http"}:
            raise ValueError(f"Unsupported external backend: {backend}")
        self.backend = backend
        self.role = role
        self.command = command
        self.model = model
        self.dsh_url = dsh_url or os.getenv("DSH_CALLBACK_URL")
        self.timeout = timeout
        self.sessions = SessionStore(root=memory_root)
        self.error_handler = None
        if backend == "dsh-http" and not self.dsh_url:
            raise ValueError("dsh-http backend requires --dsh-url or DSH_CALLBACK_URL")

    def set_error_handler(self, handler):
        self.error_handler = handler

    def set_query_handler(self, _handler):
        pass

    def _build_companion_prompt(self, context: dict, session_id: str | None) -> str:
        header = (
            "You are the Don't Starve Together companion coordinator. Return ONLY a single valid JSON object.\n"
            + COMPACT_FORMAT
            + "\n"
            + ("" if session_id else companion_profile(compact=True) + "\n")
        )
        return header + "\nCurrent game planning event JSON:\n" + json.dumps(context, ensure_ascii=False)

    def _build_player_prompt(self, context: dict, session_id: str | None) -> str:
        header = (
            "You are the Don't Starve Together player planner. Return ONLY a single valid JSON object.\n"
            + PLANNING_RULES
            + PLAYER_TASK_FORMAT
            + TASK_START_RULES
        )
        if not session_id:
            ref = planning_reference(knowledge=context.get("dst_knowledge"))
            header += "\nStatic reference:\n" + json.dumps(ref, ensure_ascii=False, separators=(",", ":"))
        return header + "\n\nCurrent game planning event JSON:\n" + snapshot_message(context)

    def _invoke_claude(self, prompt: str, session_id: str | None, cancel) -> tuple[str, str | None]:
        cmd = _resolve_command("claude", self.command)
        cmd.extend(["-p", "--output-format", "json"])
        if session_id:
            cmd.extend(["--resume", session_id])
        if self.model:
            cmd.extend(["--model", self.model])
        code, stdout, stderr = _run_cancellable(cmd, input_text=prompt, cancel=cancel, timeout=self.timeout)
        if code != 0:
            raise RuntimeError(f"claude CLI failed (exit {code}): {(stderr or stdout)[:300]}")
        try:
            envelope = json.loads(stdout.strip())
            if isinstance(envelope, dict) and "result" in envelope:
                return str(envelope["result"]), envelope.get("session_id") or session_id
        except ValueError:
            pass
        return stdout, session_id

    def _invoke_codex(self, prompt: str, session_id: str | None, cancel) -> tuple[str, str | None]:
        cmd = _resolve_command("codex", self.command)
        if session_id:
            cmd.extend(["exec", "resume", "--json"])
            if self.model:
                cmd.extend(["-m", self.model])
            cmd.extend([session_id, "-"])
        else:
            cmd.extend(["exec", "--json"])
            if self.model:
                cmd.extend(["-m", self.model])
            cmd.append("-")
        code, stdout, stderr = _run_cancellable(cmd, input_text=prompt, cancel=cancel, timeout=self.timeout)
        if code != 0:
            raise RuntimeError(f"codex CLI failed (exit {code}): {(stderr or stdout)[:300]}")
        new_sid = session_id
        last_text = ""
        for line in stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            if isinstance(event.get("thread_id"), str):
                new_sid = event["thread_id"]
            elif isinstance(event.get("session_id"), str):
                new_sid = event["session_id"]
            item = event.get("item") if isinstance(event.get("item"), dict) else {}
            if item.get("type") == "agent_message" and isinstance(item.get("text"), str):
                last_text = item["text"]
            elif isinstance(event.get("message"), str):
                last_text = event["message"]
        return last_text or stdout, new_sid

    def _invoke_dsh_cli(self, prompt: str, session_id: str | None, cancel) -> tuple[str, str | None]:
        default_bin = "dsh-tui" if shutil.which("dsh-tui") else "dsh"
        cmd = _resolve_command(default_bin, self.command)
        cmd.extend(["run", "--output-format", "json"])
        if session_id:
            cmd.extend(["--session-id", session_id])
        if self.model:
            if "/" in self.model:
                provider, model_id = self.model.split("/", 1)
                cmd.extend(["--provider", provider, "--model", model_id])
            else:
                cmd.extend(["--model", self.model])
        code, stdout, stderr = _run_cancellable(cmd, input_text=prompt, cancel=cancel, timeout=self.timeout)
        if code != 0:
            raise RuntimeError(f"dsh CLI failed (exit {code}): {(stderr or stdout)[:300]}")
        try:
            envelope = json.loads(stdout.strip())
            if isinstance(envelope, dict):
                new_sid = (
                    envelope.get("session_id")
                    or envelope.get("sessionId")
                    or (envelope.get("session") or {}).get("id")
                    or session_id
                )
                for key in ("result", "text", "output", "finalText", "final_text", "content", "message"):
                    val = envelope.get(key)
                    if isinstance(val, str) and val.strip():
                        return val, new_sid
                messages = envelope.get("messages")
                if isinstance(messages, list):
                    for msg in reversed(messages):
                        if isinstance(msg, dict) and isinstance(msg.get("text") or msg.get("content"), str):
                            return str(msg.get("text") or msg.get("content")), new_sid
                return stdout, new_sid
        except ValueError:
            pass
        return stdout, session_id

    def _invoke_dsh_http(self, prompt: str, context: dict, world_id: str | None, session_id: str | None, cancel) -> tuple[str, str | None]:
        if cancel is not None and cancel.is_set():
            raise RuntimeError("Planning superseded by newer player intent")
        payload = {
            "world_id": world_id,
            "session_id": session_id,
            "role": self.role,
            "prompt": prompt,
            "context": context,
        }
        req = urllib.request.Request(
            self.dsh_url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw_text = resp.read().decode("utf-8", errors="replace")
        except (urllib.error.URLError, urllib.error.HTTPError) as exc:
            raise RuntimeError(f"DSH HTTP callback failed: {exc}") from exc
        if cancel is not None and cancel.is_set():
            raise RuntimeError("Planning superseded by newer player intent")
        try:
            envelope = json.loads(raw_text)
            if isinstance(envelope, dict):
                new_sid = envelope.get("session_id") or session_id
                if "plan" in envelope and isinstance(envelope["plan"], dict):
                    return json.dumps(envelope["plan"], ensure_ascii=False), new_sid
                if "result" in envelope:
                    res = envelope["result"]
                    return (json.dumps(res, ensure_ascii=False) if isinstance(res, dict) else str(res)), new_sid
                return raw_text, new_sid
        except ValueError:
            pass
        return raw_text, session_id

    def invoke(self, prompt: str, context: dict, *, world_id: str | None = None, cancel=None) -> tuple[dict, str]:
        session_id = self.sessions.get(world_id, self.backend, self.role)
        if self.backend == "claude":
            text, new_sid = self._invoke_claude(prompt, session_id, cancel)
        elif self.backend == "codex":
            text, new_sid = self._invoke_codex(prompt, session_id, cancel)
        elif self.backend == "dsh":
            text, new_sid = self._invoke_dsh_cli(prompt, session_id, cancel)
        else:
            text, new_sid = self._invoke_dsh_http(prompt, context, world_id, session_id, cancel)
        if new_sid:
            self.sessions.save(world_id, self.backend, self.role, new_sid)
        parsed = extract_json_object(text)
        return parsed, json.dumps(parsed, ensure_ascii=False)

    def plan_companion(self, context: dict, _history, cancel, *, world_id: str | None = None) -> dict:
        session_id = self.sessions.get(world_id, self.backend, self.role)
        prompt = self._build_companion_prompt(context, session_id)
        try:
            raw, raw_json = self.invoke(prompt, context, world_id=world_id, cancel=cancel)
            catalogs = {i: dict(a["observation"].get("recipes") or {}) for i, a in context["members"].items()}
            validated = validate_team_plan(raw, context, catalogs)
            return {**validated, "_catalogs": catalogs, "_assistant_message": raw_json}
        except Exception as exc:
            if self.error_handler:
                self.error_handler("external_backend", str(exc)[:500])
            raise CompanionPlanningError(type(exc).__name__, str(exc)[:500]) from None

    def plan_player(self, day: int, context: dict, _history, cancel, *, world_id: str | None = None) -> dict:
        session_id = self.sessions.get(world_id, self.backend, self.role)
        prompt = self._build_player_prompt(context, session_id)
        raw, _ = self.invoke(prompt, context, world_id=world_id, cancel=cancel)
        return validate_player_board(raw, day, context, map_seen=False)
