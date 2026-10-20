"""One advisory JEV request in flight, accepted only against current candidates."""

from __future__ import annotations

import copy
import math
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError

import timing_log as timing
from controller import exploration_resource_available
from exploration_events import resource_types


def action_identity(action: dict) -> tuple:
    kind = action["kind"]
    if kind == "explore":
        mode = action.get("navigation_mode", "region")
        if mode == "graph":
            return kind, mode, action.get("target_node_id")
        if mode == "search" and action.get("search_region_id") is not None:
            return kind, mode, action["search_region_id"]
        return kind, mode, action.get("frontier_x"), action.get("frontier_z")
    if kind == "collect":
        return kind, action.get("prefab"), action.get("guid")
    if kind == "flee_from_nearest_hostile":
        return kind, action.get("guid")
    return kind,


def current_action(action: dict, dispatch: dict) -> tuple | None:
    """Match semantic targets, not an adjacent area's unstable list index."""
    identity = action_identity(action)
    return next(((key, value) for key, value in dispatch.items()
                 if action_identity(value) == identity), None)


def _inventory(state: dict) -> tuple:
    inventory = state.get("player", {}).get("inventory", {})
    counts = {}
    for group in ("items", "equipped"):
        for item in inventory.get(group, []):
            key = (group, item.get("prefab", ""))
            counts[key] = counts.get(key, 0) + int(item.get("count", 1))
    return tuple(sorted(counts.items()))


def _resources(state: dict) -> set:
    return resource_types(state, exploration_resource_available)


def compatible(source: dict, current: dict, *, check_inventory: bool = True) -> bool:
    for key in ("day", "phase"):
        if source.get("world", {}).get(key) != current.get("world", {}).get(key):
            return False
    old_nav, new_nav = source.get("navigation", {}), current.get("navigation", {})
    if (old_nav.get("current_biome") != new_nav.get("current_biome")
            or (old_nav.get("graph") or {}).get("current_node") != (new_nav.get("graph") or {}).get("current_node")
            or int((new_nav.get("graph") or {}).get("known_nodes") or 0)
                > int((old_nav.get("graph") or {}).get("known_nodes") or 0)):
        return False
    old, new = source.get("player", {}), current.get("player", {})
    if old.get("guid") != new.get("guid") or new.get("vitals", {}).get("dead"):
        return False
    if float(new.get("vitals", {}).get("health") or 0) < float(old.get("vitals", {}).get("health") or 0):
        return False
    hunger_ratio = lambda p: float(p.get("vitals", {}).get("hunger") or 0) / max(1, float(p.get("vitals", {}).get("hunger_max") or 1))
    if hunger_ratio(old) >= 0.30 > hunger_ratio(new):
        return False
    if _resources(current) != _resources(source):
        return False
    a, b = old.get("position", {}), new.get("position", {})
    if a and b and math.hypot(float(a.get("x", 0)) - float(b.get("x", 0)),
                             float(a.get("z", 0)) - float(b.get("z", 0))) > 7:
        return False
    threats = lambda s: {(e.get("guid"), e.get("prefab")) for e in s.get("nearby", [])
                         if e.get("activeThreat") or e.get("potentialThreat")}
    return (not check_inventory or _inventory(source) == _inventory(current)) and threats(source) == threats(current)


class DecisionPrefetch:
    def __init__(self, request, max_age: float = 3.0):
        self.request = request
        self.max_age = max_age
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="jev-prefetch")
        self.future = None
        self.abandoned = False
        self.confirmed = True
        self.board_id = None

    def submit(self, state: dict, criteria: dict, dispatch: dict, plan, *, predicted=False) -> bool:
        if self.future is not None:
            if self.future.done() and (self.abandoned or time.monotonic() - self.submitted >= self.max_age
                                       or not compatible(self.source, state)):
                self.future = None
            else:
                return False
        self.source = copy.deepcopy(state)
        self.dispatch = copy.deepcopy(dispatch)
        self.submitted = time.monotonic()
        self.abandoned = False
        self.confirmed = not predicted
        self.board_id = plan.get("board_id") if plan and plan.get("kind") == "task_board" else None
        self.future = self.executor.submit(self._request, self.source, copy.deepcopy(criteria), copy.deepcopy(plan))
        print("jev_prefetch_submitted")
        timing.annotate(buffer_stage="predicted" if predicted else "verified")
        return True

    def confirm(self, state: dict) -> bool:
        if self.future is None or self.abandoned or self.confirmed:
            return False
        if not compatible(self.source, state):
            timing.annotate(buffer_validation="prediction_mismatch_or_new_event")
            self.discard("prediction_mismatch_or_new_event")
            return False
        self.confirmed = True
        timing.annotate(buffer_validation="confirmed")
        print("jev_buffer_confirmed")
        return True

    def _request(self, state, criteria, plan):
        started = time.monotonic()
        with timing.CycleTrace(0, emit_console=False) as trace:
            timing.annotate(kind="prefetch_request")
            result = self.request(state, criteria, plan)
        return result, time.monotonic() - started, trace.record

    def discard(self, reason: str) -> None:
        if self.future is None:
            return
        print(f"jev_prefetch_discarded reason={reason}")
        self.abandoned = True
        if self.future.cancel() or self.future.done():
            self.future = None

    def take(self, state: dict, dispatch: dict, *, plan=None):
        if self.future is None or self.abandoned:
            return None
        board_id = plan.get("board_id") if plan and plan.get("kind") == "task_board" else None
        if self.board_id != board_id:
            self.discard("task_board_changed")
            return None
        if not self.confirmed:
            self.discard("action_not_confirmed")
            return None
        age = time.monotonic() - self.submitted
        if age >= self.max_age or not compatible(self.source, state):
            self.discard("state_changed_or_expired")
            return None
        waited = time.monotonic()
        try:
            with timing.span("decision.prefetch_wait"):
                packet = self.future.result(timeout=self.max_age - age)
                result, elapsed = packet[:2]
        except TimeoutError:
            self.discard("request_expired")
            return None
        except (RuntimeError, OSError, ValueError):
            self.discard("request_failed")
            return None
        self.future = None
        if time.monotonic() - self.submitted > self.max_age:
            print("jev_prefetch_discarded reason=response_expired")
            return None
        choice = result["answer"]["choice"]
        selected = self.dispatch.get(choice)
        match = current_action(selected, dispatch) if selected else None
        if match is None or (choice == "wait" and set(self.dispatch) != set(dispatch)):
            print("jev_prefetch_discarded reason=action_changed")
            return None
        remaining = time.monotonic() - waited
        timing.annotate(decision_source="prefetch", prefetch_request_ms=round(elapsed * 1000, 3),
                        prefetch_wait_ms=round(remaining * 1000, 3),
                        prefetch_hidden_ms=round(max(0, elapsed - remaining) * 1000, 3))
        if len(packet) > 2:
            timing.annotate(prefetch_timing=packet[2])
        print(f"jev_prefetch_ready request_time={elapsed:.3f}s remaining_wait={remaining:.3f}s")
        return result, selected, elapsed

    def close(self) -> None:
        self.discard("shutdown")
        self.executor.shutdown(wait=False, cancel_futures=True)
