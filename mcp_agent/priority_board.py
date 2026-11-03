"""Persistent priority goals. Chooses ONLY native actions offered by the Lua snapshot."""
from __future__ import annotations

import copy
import json
import math
import time

from common import timing_log as timing
from common.placement import resolve as placement_area, inside as inside_placement
from common.refueling import has_fuel
from common.thermal import refuel_pause, heat_risk
from common.places import PLACE_TARGETS
from common.coordinates import move_action, validate_point
from common.game_rules import WORK_PRODUCTS, SAFETY_INTERRUPTS, SAFE_FOOD_PREFABS
from common.task_board import validate_board, board_view, focus_scope, MAX_TASKS
from common.task_timing import TIME_FIELDS, TERMINAL_TASKS, number
from common.exploration_metrics import observed_cells, survey_cells, ROOM_SEARCH_CELLS
from common.residence import allowed as residence_allowed
from mcp_agent.residence import update_residences
from mcp_agent.maintenance import is_maintenance, update_maintenance
from mcp_agent.navigation_intents import dispatch_for_navigation, FOOD_SEARCH_PRODUCT

SEARCH_RETRY_SECONDS = 15


def is_priority_board(board):
    return isinstance(board, dict) and board.get("kind") == "task_board" and board.get("execution_policy") == "priority_goals"


def idle_review_reason(view):
    """A mixture of finished, scheduled and blocked tasks has no work to run."""
    if not is_priority_board(view):
        return None
    tasks = view.get("tasks", [])
    if any(t.get("status") not in TERMINAL_TASKS | {"scheduled", "blocked"} for t in tasks):
        return None
    if all(t.get("status") == "done" for t in tasks):
        return "all_tasks_completed"
    remaining = [t for t in tasks if t.get("status") not in TERMINAL_TASKS]
    if remaining and all(t.get("status") == "scheduled" for t in remaining):
        return "only_scheduled_tasks"
    if any(t.get("status") == "scheduled" for t in remaining):
        return "no_actionable_tasks"
    return "search_or_prerequisite_blocked"


def idle_review_marker(view):
    """Ignore board revisions and changing telemetry when deduplicating reviews."""
    return tuple(sorted((t["id"], task_signature(t), t.get("status"),
                         t.get("blocked_reason", ""), tuple(t.get("depends_on", [])),
                         t.get("focus", False), t.get("priority", 1),
                         t.get("search_budget_seconds"), t.get("search_budget_distance"))
                        for t in view.get("tasks", [])))


def validate_goals(raw, day):
    if not isinstance(raw, dict):
        raise ValueError("Priority board must be an object")
    clean = validate_board(raw, day)
    for task, source in zip(clean["tasks"], raw["tasks"]):
        target = task["target"]
        if target["type"] == "inventory" and target["prefab"] in {"torch", "axe", "pickaxe"} and target["count"] != 1:
            raise ValueError("Native crafting offers one carried tool/torch, not reserve stock")
        if target["type"] == "structure" and target["prefab"] == "firepit" and not target.get("recipe_id"):
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
    if "camp_site" in raw:
        site = raw["camp_site"]
        if not isinstance(site, dict):
            raise ValueError("camp_site requires coordinates, reason and optional backup")
        clean["camp_site"] = {**validate_point(site), "reason":str(site.get("reason", ""))[:400]}
        if "backup" in site:
            clean["camp_site"]["backup"] = {**validate_point(site["backup"]),
                "reason":str(site["backup"].get("reason", ""))[:400]}
    clean.update(schema=2, execution_policy="priority_goals", cancel_task_ids=cancelled)
    return clean


def task_signature(task):
    timing = {k:task[k] for k in TIME_FIELDS if k in task}
    return json.dumps({"target":task["target"], **timing} if timing else task["target"], sort_keys=True)


def progress_signatures_match(left, right):
    """Quantity/schedule reviews cannot erase a verified fuel addition in flight."""
    if left == right:
        return True
    try:
        targets = [json.loads(signature) for signature in (left, right)]
        targets = [value.get("target", value) for value in targets]
    except (ValueError, TypeError, AttributeError):
        return False
    return (all(target.get("type") == "refuel" for target in targets)
            and all(targets[0].get(k) == targets[1].get(k) for k in ("building_id", "fuel_prefab")))


def merge_goals(previous, incoming):
    """Reviews are patches: omission never silently cancels existing unfinished goals."""
    result = copy.deepcopy(incoming)
    if not is_priority_board(previous) or previous.get("status") == "completed":
        return result
    tasks = {t["id"]: copy.deepcopy(t) for t in previous["tasks"]
             if t["id"] not in incoming.get("cancel_task_ids", [])}
    for task in result["tasks"]:
        # Preserve stable identity if the planner renamed an otherwise identical goal.
        same = next((old for old in tasks.values() if task["target"]["type"] != "refuel"
                     and task_signature(old) == task_signature(task)
                     and not (task["target"]["type"] in PLACE_TARGETS | {"move_to"} and old.get("status") == "done")), None)
        if same and task["id"] not in tasks:
            old_id = task["id"]
            task["id"] = same["id"]
            for other in result["tasks"]:
                other["depends_on"] = [task["id"] if d == old_id else d for d in other["depends_on"]]
        tasks[task["id"]] = task
    explicit = {t["id"] for t in result["tasks"]}
    # Completed milestones need no active slot unless explicitly retained or
    # still referenced as a prerequisite. Never discard unfinished goals.
    while True:
        required = {d for t in tasks.values() for d in t.get("depends_on", [])}
        removable = [identity for identity, t in tasks.items()
                     if t.get("status") == "done" and identity not in explicit and identity not in required]
        if not removable:
            break
        for identity in removable:
            del tasks[identity]
    result["tasks"] = list(tasks.values())
    if len(tasks) > MAX_TASKS:
        raise ValueError(f"Merged task board has {len(tasks)} tasks (maximum {MAX_TASKS}); "
                         f"retained IDs: {', '.join(tasks)}. Explicitly cancel obsolete unfinished tasks with cancel_task_ids.")
    # Never adopt a merge that introduces unknown dependencies, cycles or >6 tasks.
    return {**validate_goals(result, incoming["day"]),
            **{k: incoming[k] for k in ("board_id", "quarter") if k in incoming}}


class PriorityBoardExecutor:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.records = {}
        self.refuel_totals = {}
        self.selected = None
        self.navigation = False
        self.navigation_mode = None
        self.last_tick = None
        self.last_position = None
        self.paused = {}
        self.room_search = {}
        self.needs_safety_decision = False
        self.reason = ""
        self.residence = None
        self.latest_observed_state = None
        self.stale_observations_ignored = 0

    def adopt(self, board, *, new_batch=False):
        """Explicitly revised blocked goals get a fresh budget, not every review."""
        if new_batch:
            fuel_totals = self.refuel_totals
            latest = self.latest_observed_state
            self.__init__(self.clock)
            self.refuel_totals = fuel_totals
            self.latest_observed_state = latest
        for task in board["tasks"]:
            record = self._record(task)
            revision = (task.get("reason", ""), task.get("search_budget_seconds"), task.get("search_budget_distance"))
            if record.get("blocked") and record.get("revision") != revision:
                self._restart_search(record)
            record["revision"] = revision
        active = {t["id"] for t in board["tasks"]}
        self.records = {k: v for k, v in self.records.items() if k in active}

    def _refuel_total(self, task_id, target):
        identity = (target["building_id"], target["fuel_prefab"])
        total = self.refuel_totals.get(task_id)
        if total is None or total["identity"] != identity:
            total = {"identity":identity,"added":0}
            self.refuel_totals[task_id] = total
        return total

    def _record(self, task):
        signature = task_signature(task)
        record = self.records.get(task["id"])
        if record is None or not progress_signatures_match(record["signature"], signature):
            record = {"signature": signature, "done": False, "seconds": 0., "distance": 0., "blocked": "",
                      "total_seconds": 0., "total_distance": 0., "attempt": 1}
            self.records[task["id"]] = record
        if task["target"]["type"] == "refuel" and record["signature"] != signature:
            old = json.loads(record["signature"])
            old_target=old.get("target",old)
            if (old_target.get("at_least")!=task["target"].get("at_least")
                    or {k:old[k] for k in TIME_FIELDS if k in old} != {k:task[k] for k in TIME_FIELDS if k in task}):
                record.pop("maintenance",None)
                record["done"] = False
        record["signature"] = signature
        if task["target"]["type"] == "refuel":
            total = self._refuel_total(task["id"],task["target"])
            record["fuel_added"] = total["added"]
            # Unlike a one-shot milestone, a larger cumulative total reopens the task.
            if is_maintenance(task):
                record["done"] = (record.get("maintenance") or {}).get("status") == "done"
            elif "count" in task["target"]:
                record["done"] = total["added"] >= task["target"]["count"]
        return record

    def pause_accounting(self):
        self.navigation = False

    def _restart_search(self, record):
        record.update(seconds=0., distance=0., blocked="", retry_at=None, attempt=record["attempt"] + 1)

    @staticmethod
    def eligible_tasks(view):
        scoped = copy.deepcopy(view)
        for task in scoped["tasks"]:
            if task["target"]["type"] == "stay_home":
                task["focus"] = False  # Residence is an area constraint, not a ban on compatible chores.
        scope = focus_scope(scoped)
        return [t for t in view["tasks"] if t["status"] in {"ready", "in_progress"}
                and (scope is None or t["id"] in scope)]

    def view(self, board, state):
        # Action credentials may refer to an older immutable snapshot. Such reads
        # must not rewind timers, progress, positions or budget accounting.
        if not (state.get("_jev_execution_context") or {}).get("predicted_success"):
            latest = self.latest_observed_state
            stamp = state.get("observed_at")
            if number(stamp):
                if latest and number(latest.get("observed_at")) and stamp < latest["observed_at"]:
                    timing.event("observation.stale_ignored",observed_at=stamp,current_observed_at=latest["observed_at"])
                    state = latest
                    self.stale_observations_ignored += 1
                else:
                    self.latest_observed_state = state
        result = board_view(board, state)
        now, position = self.clock(), state.get("player", {}).get("position")
        emergency = heat_risk(state) or any(e.get("activeThreat") for e in state.get("nearby", []))
        if self.navigation and self.selected in self.records and self.last_tick is not None and not emergency:
            record = self.records[self.selected]
            delta = max(0, now - self.last_tick)
            record["seconds"] += delta
            record["total_seconds"] += delta
            node = (state.get("navigation", {}).get("graph") or {}).get("current_node")
            room = self.room_search.get((self.selected, node))
            if room and self.navigation_mode in {"region", "search", "resource"}:
                room["seconds"] += delta
            if position and self.last_position:
                travelled = math.hypot(position["x"] - self.last_position["x"], position["z"] - self.last_position["z"])
                record["distance"] += travelled
                record["total_distance"] += travelled
        self.last_tick, self.last_position = now, copy.deepcopy(position)
        predicted = result["state_basis"] != "observed"
        for task in result["tasks"]:
            record = self._record(task)
            if task["status"] in {"failed", "scheduled"} or task.get("blocked_reason") == "game time unavailable":
                continue
            if (record.get("blocked") == "search_budget_exhausted" and record.get("retry_at") is not None
                    and now >= record["retry_at"] and not predicted):
                self._restart_search(record)
            if task["target"]["type"] == "refuel":
                added = record.get("fuel_added", 0)
                task["fuel_added"] = added
                if "count" in task["target"]:
                    task["progress"] = {"current":added,"required":task["target"]["count"]}
                    if added >= task["target"]["count"]:
                        task.update(status="done",blocked_reason="")
                    elif task["status"] != "blocked" and added:
                        task["status"] = "in_progress"
                    pause=refuel_pause(task["target"],state)
                    if added < task["target"]["count"] and pause not in {"", "fuel_settling"}:
                        task.update(status="blocked",blocked_reason=pause)
            if task["target"]["type"] == "survey_region":
                graph = state.get("navigation", {}).get("graph") or {}
                node = task["target"]["node_id"]
                area = (graph.get("current") or {}) if graph.get("current_node") == node else next(
                    (r for r in graph.get("routes", []) if r.get("node_id") == node), {})
                cells = observed_cells(area,state.get("navigation",{}).get("cell_size",4))
                required = survey_cells(task["target"])
                if cells is not None:
                    if "start_cells" not in record and not predicted:
                        record["start_cells"] = cells
                    additional = max(0,cells-record.get("start_cells",cells))
                    task["progress"] = {"current":additional,"required":required}
                    task["status"] = "done" if area.get("boundary_complete") or additional>=required else "ready"
                else:
                    task.update(status="blocked",blocked_reason="observed map cell count unavailable",
                                progress={"current":0,"required":required})
            if task["status"] == "done" and not predicted:
                record["done"] = True
            elif task["status"] == "done" and predicted and not record["done"]:
                task["status"] = "ready"
            if is_maintenance(task) and task["status"] == "maintaining" and not predicted:
                # A replenished maintenance episode no longer owns a resource-search budget.
                record.update(seconds=0., distance=0., blocked="", retry_at=None)
            if record["done"]:
                task.update(status="done", blocked_reason="")
            elif (record["seconds"] >= task.get("search_budget_seconds", 120)
                  or record["distance"] >= task.get("search_budget_distance", 240)):
                record["blocked"] = "search_budget_exhausted"
                if record.get("retry_at") is None:
                    record.update(retry_at=now + SEARCH_RETRY_SECONDS, recovery_pending=True)
            if record["blocked"] and not record["done"]:
                task.update(status="blocked", blocked_reason=record["blocked"])
            task["search_spent"] = {"seconds": round(record["seconds"], 2), "distance": round(record["distance"], 2)}
            task["search_retry"] = {"attempt": record["attempt"],
                "remaining_seconds": round(max(0, (record.get("retry_at") or now) - now), 1),
                "total_seconds": round(record["total_seconds"], 2), "total_distance": round(record["total_distance"], 2)}
        by_id = {t["id"]: t for t in result["tasks"]}
        # Dependencies use latched observed completion, never a historical 'entered' flag.
        for _ in result["tasks"]:
            for task in result["tasks"]:
                missing = [d for d in task["depends_on"] if by_id[d]["status"] != "done"]
                if missing and task["status"] not in TERMINAL_TASKS | {"scheduled"}:
                    task.update(status="blocked", blocked_reason="dependencies: " + ",".join(missing))
                elif (not missing and task["status"] == "blocked" and task.get("blocked_reason", "").startswith("dependencies:")
                      and not self.records[task["id"]]["blocked"]):
                    task.update(status="ready", blocked_reason="")
        update_maintenance(result, state, self.records)
        self.residence = update_residences(result, state, self.records)
        # A completed residence may release dependent work in this same observation.
        for task in result["tasks"]:
            if task.get("blocked_reason", "").startswith("dependencies:") and all(
                    by_id[d]["status"] == "done" for d in task["depends_on"]) and not self.records[task["id"]]["blocked"]:
                task.update(status="ready", blocked_reason="")
        result["status"] = ("completed" if all(t["status"] == "done" for t in result["tasks"])
                            else "failed" if all(t["status"] in TERMINAL_TASKS for t in result["tasks"])
                            else "blocked" if all(t["status"] in TERMINAL_TASKS | {"blocked"} for t in result["tasks"]) else "active")
        result["ready_task_ids"] = [t["id"] for t in result["tasks"] if t["status"] in {"ready", "in_progress"}]
        scope = focus_scope(result)
        result["focus_task_ids"] = [t["id"] for t in result["tasks"] if t.get("focus") and t["status"] not in TERMINAL_TASKS | {"scheduled", "maintaining"}]
        result["execution_task_ids"] = [t["id"] for t in self.eligible_tasks(result)]
        if self.residence:
            result["residence_constraint"] = copy.deepcopy(self.residence)
        return result

    def timed_task_ids(self, board, state):
        if not is_priority_board(board):
            return []
        return sorted(t["id"] for t in self.view(board,state)["tasks"] if
                      (t.get("start_at") or t["target"]["type"] == "stay_home" or is_maintenance(t))
                      and t["status"] in {"ready","in_progress"})

    @staticmethod
    def local_choices(task, actions, state=None):
        target = task["target"]
        if target.get("placement"):
            try:
                area = placement_area(target["placement"],state or {})
            except ValueError:
                return []
            if not inside_placement((state or {}).get("player",{}).get("position"),area):
                return []
        if target["type"] in PLACE_TARGETS:
            if target["type"] == "set_home":
                return [(k, a) for k, a in actions.items() if a.get("planner_only") and (
                    a.get("kind") == "set_home" and a.get("anchor_id") == target["anchor_id"]
                    or a.get("kind") == "explore" and a.get("building_id") == target["anchor_id"])]
            field = {"visit_building": "building_id", "set_home": "anchor_id", "return_home": "home_id", "stay_home":"home_id"}[target["type"]]
            return [(k, a) for k, a in actions.items() if a.get("planner_only")
                    and a.get(field) == target[field]
                    and a.get("kind") == ("set_home" if target["type"] == "set_home" else "explore")]
        if target["type"] == "refuel":
            choices = [(k,a) for k,a in actions.items() if a.get("kind")=="add_fuel"
                       and a.get("building_id")==target["building_id"] and a.get("fuel_prefab")==target["fuel_prefab"]
                       and bool(a.get("ratio_refuel")) == ("at_least" in target)]
            if choices or not state or not has_fuel(target,state):
                return choices
            return [(k,a) for k,a in actions.items() if a.get("kind")=="explore" and a.get("planner_only")
                    and a.get("building_id")==target["building_id"]]
        if target["type"] == "hunger_ratio":
            if "eat_safe_food" in actions:
                return [("eat_safe_food", actions["eat_safe_food"])]
            # The native snapshot already validates pickability and the path.
            # Gather one available safe food, then re-evaluate eating/goal progress.
            return [(k, a) for k, a in actions.items() if a.get("kind") == "collect"
                    and a.get("product", a.get("prefab")) in SAFE_FOOD_PREFABS]
        if target["type"] == "inventory":
            product = target["prefab"]
            candidates = [(k, a) for k, a in actions.items() if a.get("kind") == "collect" and a.get("product", a.get("prefab")) == product]
            if product in WORK_PRODUCTS:
                kind, sources = WORK_PRODUCTS[product]
                candidates += [(k, a) for k, a in actions.items() if a.get("kind") == kind and a.get("prefab") in sources]
            candidates += [(k, a) for k, a in actions.items()
                           if a.get("kind") == "craft" and a.get("product") == product
                           and (not target.get("recipe_id") or a.get("recipe_id") == target["recipe_id"])]
            craft = "craft_" + product
            if craft in actions:
                candidates.append((craft, actions[craft]))
            return candidates
        key = "build_" + target["prefab"] if target["type"] == "structure" else None
        choices = [(key, actions[key])] if key in actions else []
        if target["type"] == "structure":
            choices += [(k, a) for k, a in actions.items() if a.get("kind") == "place"
                        and a.get("product") == target["prefab"]
                        and (not target.get("recipe_id") or a.get("recipe_id") == target["recipe_id"])]
        return choices

    def navigation_targets(self, board, state):
        # Watch the execution scope, including unfinished focus prerequisites.
        return [copy.deepcopy(task["target"]) for task in self.eligible_tasks(self.view(board, state))
                if task["target"]["type"] in {"inventory", "structure", "hunger_ratio"}]

    def navigation_review_reason(self, board, state, task_id, signature, targets):
        """Longer native walks still obey planner updates and existing budgets."""
        if not is_priority_board(board):
            return "board_changed"
        view = self.view(board, state)
        eligible = self.eligible_tasks(view)
        task = next((t for t in view["tasks"] if t["id"] == task_id), None)
        if not task or task_signature(task) != signature or task["status"] not in {"ready", "in_progress"}:
            return "goal_changed_or_blocked"
        if task["target"]["type"] == "stay_home":
            area = self.residence
            if area and task_id in area["task_ids"] and not area["holding"]:
                return None  # The same residence-return rule used by choose().
            return "residence_return_inactive"
        if not any(t["id"] == task_id for t in eligible):
            return "goal_changed_or_blocked"
        current = [t["target"] for t in eligible if t["target"]["type"] in {"inventory", "structure", "hunger_ratio"}]
        if current != targets:
            return "navigation_targets_changed"
        graph = state.get("navigation", {}).get("graph") or {}
        room = self.room_search.get((task_id, graph.get("current_node")))
        if self.navigation_mode in {"region", "search", "resource"} and room and (
                room["seconds"] >= 30 or (observed_cells(graph.get("current") or {},state.get("navigation",{}).get("cell_size",4)) or 0) - room["cells"] >= ROOM_SEARCH_CELLS):
            return "room_search_budget"
        return None

    def choose(self, board, state, actions, navigator):
        view, now = self.view(board, state), self.clock()
        self.navigation, self.selected, self.reason = False, None, ""
        self.paused = {k: v for k, v in self.paused.items() if v > now}
        # Budget exhaustion suspends navigation, not freshly available local work.
        # Recover with remembered negative evidence intact and a different search intent.
        resumed = False
        for task in view["tasks"]:
            record = self._record(task)
            if record.pop("recovery_pending", False):
                target = task["target"]
                if target["type"] in {"inventory", "hunger_ratio"}:
                    navigator.recover_search(FOOD_SEARCH_PRODUCT if target["type"] == "hunger_ratio" else target["prefab"], state)
            if (record.get("blocked") == "search_budget_exhausted"
                    and not record["done"] and any(a.get("kind") != "explore"
                        and (task["id"], key, a.get("guid")) not in self.paused
                        for key, a in self.local_choices(task, actions, state))):
                self._restart_search(record)
                resumed = True
        if resumed:
            view = self.view(board, state)
        ready = self.eligible_tasks(view)
        area = self.residence
        if area:
            if not area["holding"]:
                task = next(t for t in view["tasks"] if t["id"] in area["task_ids"])
                route = next(((k,a) for k,a in self.local_choices(task,actions)
                              if (task["id"],k,a.get("guid")) not in self.paused),None)
                if route and task["status"] in {"ready","in_progress"}:
                    self.selected,self.navigation = task["id"],True
                    self.navigation_mode,self.last_tick = route[1].get("mode"),now
                    return task,route[0],route[1]
                self.reason = "residence_return_unavailable"
                return None
            actions = {k:a for k,a in actions.items() if residence_allowed(a,state,area)}
            ready = [t for t in ready if t["target"]["type"] != "stay_home"]
        distances = {e.get("guid"): (e.get("collect_path") or {}).get("distance", e.get("distance", math.inf)) for e in state.get("nearby", [])}
        # Ordinary scope allows local detours; focused scope excludes them.
        for task in ready:
            choices = [(k, a) for k, a in self.local_choices(task, actions, state)
                       if a.get("kind") != "explore" and (task["id"], k, a.get("guid")) not in self.paused]
            if choices:
                key, action = min(choices, key=lambda pair: (distances.get(pair[1].get("guid"), 0), pair[0]))
                self.selected = task["id"]
                return task, key, action
        # A refuel task owns its trip to the exact fire, not just a trip home.
        # During residence, actions were already filtered by the whole-path bound.
        for task in ready:
            if task["target"]["type"] != "refuel":
                continue
            routes = [(k,a) for k,a in self.local_choices(task,actions,state)
                      if a.get("kind")=="explore" and (task["id"],k,a.get("guid")) not in self.paused]
            if routes:
                key,action=routes[0]
                self.selected,self.navigation=task["id"],True
                self.navigation_mode,self.last_tick=action.get("mode"),now
                return task,key,action
        if area:
            self.reason = "residence_holding"
            return None
        nav = dispatch_for_navigation(actions, state)
        for task in ready:
            target = task["target"]
            if target["type"] == "move_to":
                key = "move_to"
                if (task["id"],key,None) not in self.paused:
                    try:
                        action = move_action(target,state)
                    except ValueError:
                        continue
                    self.selected,self.navigation = task["id"],True
                    self.navigation_mode,self.last_tick = "coordinate",now
                    return task,key,action
                continue
            if target.get("placement"):
                anchor=target["placement"]["anchor_id"]
                routes=[(k,a) for k,a in actions.items() if a.get("kind")=="explore"
                        and a.get("building_id")==anchor and (task["id"],k,a.get("guid")) not in self.paused]
                if routes:
                    key,action=routes[0]
                    self.selected,self.navigation=task["id"],True
                    self.navigation_mode,self.last_tick=action.get("mode"),now
                    return task,key,action
                continue
            if target["type"] in PLACE_TARGETS:
                routes = [(k, a) for k, a in self.local_choices(task, actions, state)
                          if a.get("kind") == "explore" and (task["id"], k, a.get("guid")) not in self.paused]
                if routes:
                    key, action = routes[0]
                    self.selected, self.navigation = task["id"], True
                    self.navigation_mode, self.last_tick = action.get("mode"), now
                    return task, key, action
                continue
            if target.get("recipe_id") or target["type"] not in {"inventory", "hunger_ratio", "visit_region", "survey_region", "known_regions"} or target.get("prefab") in {"torch", "axe", "pickaxe"}:
                continue
            if target["type"] in {"inventory", "hunger_ratio"}:
                product = FOOD_SEARCH_PRODUCT if target["type"] == "hunger_ratio" else target["prefab"]
                graph = state.get("navigation", {}).get("graph") or {}
                node, area = graph.get("current_node"), graph.get("current") or {}
                room_key = (task["id"], node)
                count = observed_cells(area,state.get("navigation",{}).get("cell_size",4)) or 0
                survey = self.room_search.setdefault(room_key, {"seconds": 0., "cells": count})
                if (survey["seconds"] >= 30 or count - survey["cells"] >= ROOM_SEARCH_CELLS):
                    navigator.exhaust_source(node, product, area, "room_search_budget")
                    self.room_search[room_key] = {"seconds": 0., "cells": count}
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
        if any(t["target"]["type"]=="refuel" and refuel_pause(t["target"],state)=="fuel_settling" for t in ready):
            self.reason="refuel_settling"
            return None
        if not ready and any(t["status"]=="maintaining" for t in view["tasks"]):
            self.reason="maintenance_holding"
            return None
        scope = focus_scope(view)
        if any(t.get("blocked_reason") == "search_budget_exhausted" for t in view["tasks"]
               if scope is None or t["id"] in scope):
            self.reason = "search_budget_cooldown"
        else:
            self.reason = ("focused_task_unavailable" if scope is not None else "waiting_for_start"
                if not ready and any(t["status"] == "scheduled" for t in view["tasks"]) else "no_feasible_action_or_route")
        return None

    def on_result(self, task_id, choice, action, status, reason, navigator, state):
        if status == "completed" and action.get("kind") == "add_fuel":
            record = self.records.get(task_id)
            if record:
                target = json.loads(record["signature"])
                target = target.get("target", target)
                if (target.get("type")=="refuel" and target.get("building_id")==action.get("building_id")
                        and target.get("fuel_prefab")==action.get("fuel_prefab")):
                    total = self._refuel_total(task_id,target)
                    total["added"] += 1
                    record["fuel_added"] = total["added"]
        if status == "interrupted" and reason in SAFETY_INTERRUPTS:
            self.needs_safety_decision = True
        if status == "failed" or (status == "interrupted" and reason in {"navigation_path_became_unsafe", "movement_stalled", "region_route_unavailable"}):
            self.paused[(task_id, choice, action.get("guid"))] = self.clock() + 15
            if action.get("kind") == "explore" and not (action.get("building_id") or action.get("home_id")):
                navigator.on_event({"reason": "path_unavailable"}, state)
        self.navigation = False
