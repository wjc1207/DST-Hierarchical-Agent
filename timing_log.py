"""Per-cycle timing spans; no game state, prompts, responses or credentials."""

from __future__ import annotations

import json
import sys
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path


DEFAULT_LOG = Path(__file__).resolve().parent / "logs" / "timing.jsonl"
RUN_ID = uuid.uuid4().hex[:12]
MAX_SPANS = 512
_active = ContextVar("cycle_timing", default=None)
_failed_paths: set[Path] = set()


def annotate(**fields) -> None:
    trace = _active.get()
    if trace is not None:
        trace.metadata.update(fields)


@contextmanager
def span(name: str, *, record: bool = True, **fields):
    trace = _active.get()
    if trace is None:
        yield
        return
    started = time.perf_counter()
    trace.sequence += 1
    frame = {"id": trace.sequence, "children": 0.0}
    parent = trace.stack[-1] if trace.stack else None
    trace.stack.append(frame)
    outcome = "ok"
    error_type = None
    try:
        yield
    except BaseException as exc:
        outcome = "interrupted" if isinstance(exc, KeyboardInterrupt) else "error"
        error_type = type(exc).__name__
        raise
    finally:
        elapsed = time.perf_counter() - started
        trace.stack.pop()
        if parent is not None:
            parent["children"] += elapsed
        own = max(0.0, elapsed - frame["children"])
        aggregate = trace.totals.setdefault(name, {
            "calls": 0, "total_ms": 0.0, "self_ms": 0.0, "max_ms": 0.0,
        })
        aggregate["calls"] += 1
        aggregate["total_ms"] += elapsed * 1000
        aggregate["self_ms"] += own * 1000
        aggregate["max_ms"] = max(aggregate["max_ms"], elapsed * 1000)
        if record:
            if len(trace.spans) < MAX_SPANS:
                trace.spans.append({
                    **fields,
                    "id": frame["id"], "parent_id": parent["id"] if parent else None,
                    "stage": name, "start_ms": round((started - trace.started) * 1000, 3),
                    "duration_ms": round(elapsed * 1000, 3), "self_ms": round(own * 1000, 3),
                    "outcome": outcome, "error_type": error_type,
                })
            else:
                trace.dropped_spans += 1


def timed(name: str, *, record: bool = True):
    def decorate(function):
        @wraps(function)
        def wrapped(*args, **kwargs):
            if _active.get() is None:
                return function(*args, **kwargs)
            with span(name, record=record):
                return function(*args, **kwargs)
        return wrapped
    return decorate


class CycleTrace:
    def __init__(self, cycle: int, path: Path | None = None, details: bool = False, *, emit_console: bool = True):
        self.cycle = cycle
        self.path = path
        self.details = details
        self.emit_console = emit_console
        self.metadata = {}
        self.totals = {}
        self.spans = []
        self.stack = []
        self.sequence = 0
        self.dropped_spans = 0

    def __enter__(self):
        self.timestamp = datetime.now(timezone.utc).isoformat()
        self.started = time.perf_counter()
        self.token = _active.set(self)
        return self

    def __exit__(self, error_type, error, traceback):
        elapsed_ms = (time.perf_counter() - self.started) * 1000
        _active.reset(self.token)
        if error_type is not None:
            self.metadata.update(
                outcome="interrupted" if isinstance(error, KeyboardInterrupt) else "error",
                error_type=error_type.__name__,
            )
        totals = {
            name: {key: value if key == "calls" else round(value, 3)
                   for key, value in values.items()}
            for name, values in self.totals.items()
        }
        self.record = {
            **self.metadata, "outcome": self.metadata.get("outcome", "ok"),
            "run_id": RUN_ID, "cycle": self.cycle, "timestamp_utc": self.timestamp,
            "total_ms": round(elapsed_ms, 3), "totals": totals,
            "spans": sorted(self.spans, key=lambda entry: entry["id"]),
            "dropped_spans": self.dropped_spans,
        }
        labels = {
            "observe": "cycle.observe", "planner": "planner.update",
            "candidates": "decision.candidates", "jev": "decision.request",
            "prefetch_wait": "decision.prefetch_wait", "revalidate": "decision.revalidate",
            "action": "action.execute", "focus": "window.focus",
            "keys": "input.hold", "fresh_wait": "state.wait_fresh",
            "verify_wait": "state.wait_condition", "log_read": "state.read",
            "cooldown": "loop.cooldown", "backoff": "loop.retry_backoff",
        }
        breakdown = " ".join(
            f"{label}={totals.get(stage, {}).get('total_ms', 0) / 1000:.3f}s"
            for label, stage in labels.items()
        )
        if self.emit_console:
            print(f"cycle_timing cycle={self.cycle} outcome={self.record['outcome']} "
                  f"{breakdown} total={elapsed_ms / 1000:.3f}s")
        if self.details and self.emit_console:
            for entry in self.record["spans"]:
                print(f"timing_step cycle={self.cycle} id={entry['id']} "
                      f"parent={entry['parent_id']} stage={entry['stage']} "
                      f"elapsed={entry['duration_ms']:.3f}ms self={entry['self_ms']:.3f}ms "
                      f"outcome={entry['outcome']}")
        if self.path is not None:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(self.record, ensure_ascii=False, separators=(",", ":")) + "\n")
                _failed_paths.discard(self.path)
            except OSError as exc:
                if self.path not in _failed_paths:
                    print(f"timing_log_unavailable path={self.path} error={type(exc).__name__}", file=sys.stderr)
                    _failed_paths.add(self.path)
        return False
