"""Persistent priority goals. Chooses ONLY native actions offered by the Lua snapshot."""
from __future__ import annotations

import copy
import json
import math
import time

from common.game_rules import WORK_PRODUCTS, SAFETY_INTERRUPTS
from common.task_board import validate_board, board_view
from mcp_agent.navigation_intents import dispatch_for_navigation


def is_priority_board(board):
    return isinstance(board, dict) and board.get("kind") == "task_board" and board.get("execution_policy") == "priority_goals"


def validate_goals(raw, day):
    if not isinstance(raw, dict):
        raise ValueError("Priority board must be an object")
    clean = validate_board(raw, day)
    for task, source in zip(clean["tasks"], raw["tasks"]):
        target = task["target"]
        if target["type"] == "inventory" and target["prefab"] in {"torch", "axe", "pickaxe"} and target["count"] != 1:
            raise ValueError("Native crafting offers one carried tool/torch, not reserve stock")
        if target["type"] == "structure" and target["prefab"] == "firepit":
            raise ValueError("Building firepit is not supported")
        for key, default, low, high in (("search_budget_seconds", 120, 10, 600),
                                         ("search_budget_distance", 240, 8, 2000)):
            value = source.get(key, default)
            if type(value) not in {int, float} or not math.isfinite(value) or not low <= value <= high:
                raise ValueError("Invalid " + key)
            task[key] = value
    cancelled = raw.get("cancel_task_ids", [])
    if (not isinstance(cancelled, list) or len(cancelled) > 6
            or any(not isinstance(t, str) or len(t) > 40 for t in cancelled)
            or set(cancelled).intersection(t["id"] for t in clean["tasks"])):
        raise ValueError("Invalid cancellations")
    clean.update(schema=2, execution_policy="priority_goals", cancel_task_ids=cancelled)
    return clean


def task_signature(task):
    return json.dumps(task["target"], sort_keys=True)


def merge_goals(previous, incoming):
    """Reviews are patches: omission never silently cancels existing unfinished goals."""
    result = copy.deepcopy(incoming)
    if not is_priority_board(previous) or previous.get("status") == "completed":
        return result
    tasks = {t["id"]: copy.deepcopy(t) for t in previous["tasks"]
             if t["id"] not in incoming.get("cancel_task_ids", [])}
    for task in result["tasks"]:
        # Preserve stable identity if the planner renamed an otherwise identical goal.
        same = next((old for old in tasks.values() if task_signature(old) == task_signature(task)), None)
        if same and task["id"] not in tasks:
            old_id = task["id"]
            task["id"] = same["id"]
            for other in result["tasks"]:
                other["depends_on"] = [task["id"] if d == old_id else d for d in other["depends_on"]]
        tasks[task["id"]] = task
    result["tasks"] = list(tasks.values())
    # Never adopt a merge that introduces unknown dependencies, cycles or >6 tasks.
    return {**validate_goals(result, incoming["day"]),
            **{k: incoming[k] for k in ("board_id", "quarter") if k in incoming}}


class PriorityBoardExecutor:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.records = {}
        self.selected = None
        self.navigation = False
        self.navigation_mode = None
        self.last_tick = None
        self.last_position = None
        self.paused = {}
        self.room_search = {}
        self.needs_safety_decision = False
        self.reason = ""

    def adopt(self, board, *, new_batch=False):
        """Explicitly revised blocked goals get a fresh budget, not every review."""
        if new_batch:
            self.__init__(self.clock)
        for task in board["tasks"]:
            record = self._record(task)
            revision = (task.get("reason", ""), task.get("search_budget_seconds"), task.get("search_budget_distance"))
            if record.get("blocked") and record.get("revision") != revision:
                record.update(seconds=0., distance=0., blocked="")
            record["revision"] = revision
        active = {t["id"] for t in board["tasks"]}
        self.records = {k: v for k, v in self.records.items() if k in active}

    def _record(self, task):
        signature = task_signature(task)
        record = self.records.get(task["id"])
        if record is None or record["signature"] != signature:
            record = {"signature": signature, "done": False, "seconds": 0., "distance": 0., "blocked": ""}
            self.records[task["id"]] = record
        return record

    def pause_accounting(self):
        self.navigation = False

    def view(self, board, state):
        result = board_view(board, state)
        now, position = self.clock(), state.get("player", {}).get("position")
        emergency = any(e.get("activeThreat") for e in state.get("nearby", []))
        if self.navigation and self.selected in self.records and self.last_tick is not None and not emergency:
            record = self.records[self.selected]
            delta = max(0, now - self.last_tick)
            record["seconds"] += delta
            node = (state.get("navigation", {}).get("graph") or {}).get("current_node")
            room = self.room_search.get((self.selected, node))
            if room and self.navigation_mode in {"region", "search", "resource"}:
                room["seconds"] += delta
            if position and self.last_position:
                record["distance"] += math.hypot(position["x"] - self.last_position["x"], position["z"] - self.last_position["z"])
        self.last_tick, self.last_position = now, copy.deepcopy(position)
        predicted = result["state_basis"] != "observed"
        for task in result["tasks"]:
            record = self._record(task)
            if task["target"]["type"] == "survey_region":
                graph = state.get("navigation", {}).get("graph") or {}
                node = task["target"]["node_id"]
                area = (graph.get("current") or {}) if graph.get("current_node") == node else next(
                    (r for r in graph.get("routes", []) if r.get("node_id") == node), {})
                if area.get("explored_area") is not None and "start_area" not in record and not predicted:
                    record["start_area"] = area["explored_area"]
                additional = max(0, area.get("explored_area", 0) - record.get("start_area", area.get("explored_area", 0)))
                task["progress"] = {"current": additional, "required": task["target"]["area"]}
                task["status"] = "done" if area.get("boundary_complete") or additional >= task["target"]["area"] else "ready"
            if task["status"] == "done" and not predicted:
                record["done"] = True
            elif task["status"] == "done" and predicted and not record["done"]:
                task["status"] = "ready"
            if record["done"]:
                task.update(status="done", blocked_reason="")
            elif (record["seconds"] >= task.get("search_budget_seconds", 120)
                  or record["distance"] >= task.get("search_budget_distance", 240)):
                record["blocked"] = "search_budget_exhausted"
            if record["blocked"] and not record["done"]:
                task.update(status="blocked", blocked_reason=record["blocked"])
            task["search_spent"] = {"seconds": round(record["seconds"], 2), "distance": round(record["distance"], 2)}
        by_id = {t["id"]: t for t in result["tasks"]}
        # Dependencies use latched observed completion, never a historical 'entered' flag.
        for _ in result["tasks"]:
            for task in result["tasks"]:
                missing = [d for d in task["depends_on"] if by_id[d]["status"] != "done"]
                if missing and task["status"] != "done":
                    task.update(status="blocked", blocked_reason="dependencies: " + ",".join(missing))
                elif (not missing and task["status"] == "blocked" and task.get("blocked_reason", "").startswith("dependencies:")
                      and not self.records[task["id"]]["blocked"]):
                    task.update(status="ready", blocked_reason="")
        result["status"] = ("completed" if all(t["status"] == "done" for t in result["tasks"])
                            else "blocked" if all(t["status"] in {"done", "blocked"} for t in result["tasks"]) else "active")
        result["ready_task_ids"] = [t["id"] for t in result["tasks"] if t["status"] in {"ready", "in_progress"}]
        return result

    @staticmethod
    def local_choices(task, actions):
        target = task["target"]
        if target["type"] == "inventory":
            product = target["prefab"]
            candidates = [(k, a) for k, a in actions.items() if a.get("kind") == "collect" and a.get("product", a.get("prefab")) == product]
            if product in WORK_PRODUCTS:
                kind, sources = WORK_PRODUCTS[product]
                candidates += [(k, a) for k, a in actions.items() if a.get("kind") == kind and a.get("prefab") in sources]
            craft = "craft_" + product
            if craft in actions:
                candidates.append((craft, actions[craft]))
            return candidates
        key = ("eat_safe_food" if target["type"] == "hunger_ratio" else
               "build_" + target["prefab"] if target["type"] == "structure" else None)
        return [(key, actions[key])] if key in actions else []

    def choose(self, board, state, actions, navigator):
        view, now = self.view(board, state), self.clock()
        self.navigation, self.selected, self.reason = False, None, ""
        self.paused = {k: v for k, v in self.paused.items() if v > now}
        ready = [t for t in view["tasks"] if t["status"] in {"ready", "in_progress"}]
        distances = {e.get("guid"): (e.get("collect_path") or {}).get("distance", e.get("distance", math.inf)) for e in state.get("nearby", [])}
        # Scan ALL goals for local work before navigating for even priority 1.
        for task in ready:
            choices = [(k, a) for k, a in self.local_choices(task, actions)
                       if (task["id"], k, a.get("guid")) not in self.paused]
            if choices:
                key, action = min(choices, key=lambda pair: (distances.get(pair[1].get("guid"), 0), pair[0]))
                self.selected = task["id"]
                return task, key, action
        nav = dispatch_for_navigation(actions, state)
        for task in ready:
            target = task["target"]
            if target["type"] not in {"inventory", "visit_region", "survey_region", "known_regions"} or target.get("prefab") in {"torch", "axe", "pickaxe"}:
                continue
            if target["type"] == "inventory":
                graph = state.get("navigation", {}).get("graph") or {}
                node, area = graph.get("current_node"), graph.get("current") or {}
                room_key = (task["id"], node)
                survey = self.room_search.setdefault(room_key, {"seconds": 0., "area": area.get("explored_area", 0)})
                if (survey["seconds"] >= 30 or area.get("explored_area", 0) - survey["area"] >= 256):
                    navigator.exhaust_source(node, target["prefab"], area, "room_search_budget")
                    self.room_search[room_key] = {"seconds": 0., "area": area.get("explored_area", 0)}
            # Dependencies were checked against the FULL board above. The
            # navigator receives just this goal, not dangling prerequisite IDs.
            proxy = {**board, "tasks": [{**task, "depends_on": []}]}
            eligible = {k: a for k, a in nav.items() if (task["id"], k, a.get("guid")) not in self.paused}
            route = navigator.choose(state, eligible, proxy)
            if route:
                self.selected, self.navigation = task["id"], True
                self.navigation_mode = route[1].get("navigation_mode")
                self.last_tick = now
                return task, route[0], route[1]
            # Route failures do not mean the resource does not exist. Other goals can run.
        self.reason = "no_feasible_action_or_route"
        return None

    def on_result(self, task_id, choice, action, status, reason, navigator, state):
        if status == "interrupted" and reason in SAFETY_INTERRUPTS:
            self.needs_safety_decision = True
        if status == "failed" or (status == "interrupted" and reason in {"navigation_path_became_unsafe", "movement_stalled", "region_route_unavailable"}):
            self.paused[(task_id, choice, action.get("guid"))] = self.clock() + 15
            if action.get("kind") == "explore":
                navigator.on_event({"reason": "path_unavailable"}, state)
        self.navigation = False
