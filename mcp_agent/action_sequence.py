"""Ordered semantic operations; native actions remain verified by the Lua executor."""
from __future__ import annotations

import copy
import math
import re
import time

from common.task_board import normalize_inventory_prefab, text
from common.game_rules import WORK_PRODUCTS, SAFETY_INTERRUPTS

ACTION_TYPES = (
    "collect", "craft", "build", "eat_safe_food", "cook_food", "equip_torch",
    "unequip_torch", "travel_region", "explore_current_region", "explore_other_region", "wait",
)
TOOLS = {"axe", "pickaxe", "torch"}
STRUCTURES = {"campfire", "sciencemachine"}
COOKED = {"cookedmeat", "cookedsmallmeat", "berries_cooked", "berries_juicy_cooked",
          "carrot_cooked", "seeds_cooked"}


def is_sequence(plan):
    return isinstance(plan, dict) and plan.get("kind") == "action_sequence"


def _number(value, lower, upper, field, integer=False):
    if (type(value) not in ((int,) if integer else (int, float))
            or not math.isfinite(value) or not lower <= value <= upper):
        raise ValueError(f"Invalid sequence {field}")
    return value


def validate_sequence(raw, day):
    """Do not accept arbitrary action names, code, coordinates or textual steps."""
    if not isinstance(raw, dict) or not text(raw.get("goal"), 160):
        raise ValueError("Action sequence requires a goal")
    steps = raw.get("steps")
    if not isinstance(steps, list) or not 1 <= len(steps) <= 12:
        raise ValueError("Action sequence requires 1-12 steps")
    clean, identities = [], set()
    for index, step in enumerate(steps, 1):
        if not isinstance(step, dict) or step.get("action") not in ACTION_TYPES:
            raise ValueError("Unsupported sequence action")
        action = step["action"]
        identity = text(step.get("id", f"step_{index}"), 40)
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", identity) or identity in identities:
            raise ValueError("Invalid or duplicate sequence step ID")
        identities.add(identity)
        result = {"id": identity, "action": action, "title": text(step.get("title", action), 100),
                  "timeout_seconds": _number(step.get("timeout_seconds", 120), 5, 300, "timeout_seconds")}
        if action in {"collect", "craft", "build"}:
            prefab = normalize_inventory_prefab(step.get("prefab")) if action != "build" else step.get("prefab")
            if action == "craft" and prefab not in TOOLS:
                raise ValueError("Unsupported sequence recipe")
            if action == "collect" and prefab in TOOLS:
                raise ValueError("Use craft, not collect, for tools and torches")
            if action == "build" and prefab not in STRUCTURES:
                raise ValueError("Unsupported sequence structure")
            result["prefab"] = prefab
        if action in {"collect", "craft", "cook_food"}:
            result["count"] = _number(step.get("count", 1), 1, 100, "count", integer=True)
        if action == "eat_safe_food":
            result["hunger_ratio"] = _number(step.get("hunger_ratio", 0.7), 0.01, 1, "hunger_ratio")
        if action == "travel_region":
            result["node_id"] = text(step.get("node_id"), 160)
            if not result["node_id"]:
                raise ValueError("travel_region requires an observed node_id")
        if action == "explore_current_region":
            result["area"] = _number(step.get("area", 256), 16, 4096, "area", integer=True)
        if action == "explore_other_region":
            result["new_regions"] = _number(step.get("new_regions", 1), 1, 10, "new_regions", integer=True)
        if action == "wait":
            result["seconds"] = _number(step.get("seconds", 1), 1, 30, "seconds")
            if result["seconds"] > result["timeout_seconds"]:
                raise ValueError("Wait duration exceeds its step timeout")
        clean.append(result)
    return {"kind": "action_sequence", "schema": 1, "day": day,
            "goal": text(raw["goal"], 160), "steps": clean}


def _counts(state):
    result = {}
    inventory = state.get("player", {}).get("inventory", {})
    for group in ("items", "equipped"):
        for item in inventory.get(group, []):
            prefab = item.get("prefab")
            result[prefab] = result.get(prefab, 0) + int(item.get("count", 1))
    return result


class SequenceExecutor:
    """Latch completed steps so later consumption cannot rewind a sequence."""
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.identity = None
        self.index = 0
        self.completed = []
        self.baseline = None
        self.status = "active"
        self.reason = ""
        self.needs_safety_decision = False

    def bind(self, plan):
        identity = plan.get("sequence_id") or repr(plan.get("steps"))
        if identity != self.identity:
            clock = self.clock
            self.__init__(clock)
            self.identity = identity

    def fail(self, reason):
        self.status, self.reason = "failed", reason

    def view(self, plan, state):
        self.bind(plan)
        result = copy.deepcopy(plan)
        predicted = state.get("_jev_execution_context", {}).get("predicted_success")
        result["state_basis"] = "predicted_success" if predicted else "observed"
        counts = _counts(state)
        graph = state.get("navigation", {}).get("graph") or {}
        current = graph.get("current") or {}
        while not predicted and self.status == "active" and self.index < len(plan["steps"]):
            step = plan["steps"][self.index]
            if self.baseline is None:
                self.baseline = {"started_at": self.clock(), "node_id": graph.get("current_node"),
                                 "area": current.get("explored_area"), "nodes": graph.get("known_nodes"),
                                 "cooked": sum(counts.get(p, 0) for p in COOKED)}
            action, done = step["action"], False
            if action in {"collect", "craft"}:
                done = counts.get(step["prefab"], 0) >= step["count"]
            elif action == "build":
                done = step["prefab"] in plan.get("verified_structures", []) or any(
                    e.get("prefab") == step["prefab"] for e in state.get("nearby", []))
            elif action == "travel_region":
                done = graph.get("current_node") == step["node_id"]
            elif action == "explore_current_region":
                if graph.get("current_node") != self.baseline["node_id"]:
                    self.fail("left_survey_region")
                done = bool(current.get("boundary_complete")) or (
                    current.get("explored_area") is not None and self.baseline["area"] is not None
                    and current["explored_area"] - self.baseline["area"] >= step["area"])
            elif action == "explore_other_region":
                done = (graph.get("known_nodes") is not None and self.baseline["nodes"] is not None
                        and graph["known_nodes"] - self.baseline["nodes"] >= step["new_regions"])
            elif action == "cook_food":
                done = sum(counts.get(p, 0) for p in COOKED) - self.baseline["cooked"] >= step["count"]
            elif action == "wait":
                done = self.clock() - self.baseline["started_at"] >= step["seconds"]
            elif action == "eat_safe_food":
                vitals = state.get("player", {}).get("vitals", {})
                done = (vitals.get("hunger") is not None and vitals.get("hunger_max", 0) > 0
                        and vitals["hunger"] / vitals["hunger_max"] >= step["hunger_ratio"])
            elif action in {"equip_torch", "unequip_torch"}:
                equipped = any(i.get("prefab") == "torch" for i in
                               state.get("player", {}).get("inventory", {}).get("equipped", []))
                done = equipped == (action == "equip_torch")
            if done and self.status == "active":
                self.completed.append(step["id"])
                self.index += 1
                self.baseline = None
                continue
            if self.clock() - self.baseline["started_at"] >= step["timeout_seconds"]:
                self.fail("step_timeout")
            break
        if self.index == len(plan["steps"]) and self.status == "active":
            self.status = "completed"
        result.update(status=self.status, reason=self.reason, step_index=self.index,
                      current_step_id=plan["steps"][self.index]["id"] if self.index < len(plan["steps"]) else None,
                      completed_step_ids=list(self.completed))
        for index, step in enumerate(result["steps"]):
            step["status"] = ("done" if index < self.index else self.status if index == self.index else "pending")
        return result

    def on_result(self, status, reason):
        if status == "failed":
            self.fail("native_action_failed:" + reason)
        elif status == "interrupted" and reason in SAFETY_INTERRUPTS:
            self.needs_safety_decision = True

    def choose(self, plan, state, actions, navigator):
        """Choose only actions implementing the current step, never a later one."""
        view = self.view(plan, state)
        if view["status"] != "active":
            return None
        step = plan["steps"][self.index]
        operation = step["action"]
        candidates = []
        if operation == "collect":
            candidates = [(key, a) for key, a in actions.items()
                          if a.get("kind") == "collect" and a.get("product", a.get("prefab")) == step["prefab"]]
            if not candidates and step["prefab"] in WORK_PRODUCTS:
                kind, sources = WORK_PRODUCTS[step["prefab"]]
                candidates = [(key, a) for key, a in actions.items()
                              if a.get("kind") == kind and a.get("prefab") in sources]
        elif operation in {"craft", "build"}:
            key = operation + "_" + step["prefab"]
            candidates = [(key, actions[key])] if key in actions else []
        elif operation in {"eat_safe_food", "cook_food", "equip_torch", "unequip_torch", "wait"}:
            candidates = [(operation, actions[operation])] if operation in actions else []
        elif operation == "travel_region":
            candidates = [(key, a) for key, a in actions.items()
                          if a.get("kind") == "explore" and a.get("mode") == "graph"
                          and a.get("node_id") == step["node_id"]]
        elif operation == "explore_current_region":
            key = "explore_current_region"
            candidates = [(key, actions[key])] if key in actions else []
        if candidates:
            distances = {e.get("guid"): e.get("distance", math.inf) for e in state.get("nearby", [])}
            return min(candidates, key=lambda pair: (distances.get(pair[1].get("guid"), math.inf), pair[0]))[0]
        if operation in {"collect", "explore_other_region"}:
            # Reuse the existing verified graph/BFS/DFS navigator, but provide only
            # THIS step as its goal. Resource-change yields do not invoke JEV.
            from mcp_agent.navigation_intents import dispatch_for_navigation
            if operation == "collect":
                goal = {"type": "inventory", "prefab": step["prefab"], "count": step["count"]}
            else:
                goal = {"type": "known_regions", "count": (self.baseline["nodes"] or 0) + step["new_regions"]}
            proxy = {"kind": "task_board", "board_id": str(self.identity) + ":" + step["id"],
                     "day": state["world"]["day"], "goal": plan["goal"], "tasks": [
                         {"id": step["id"], "title": step["title"], "priority": 1,
                          "depends_on": [], "target": goal}]}
            route = navigator.choose(state, dispatch_for_navigation(actions, state), proxy)
            if route:
                return route[0]
        self.fail("step_action_unavailable:" + operation)
        return None


def format_sequence(plan):
    lines = [f"ACTION SEQUENCE [{plan.get('status', 'active')}] id={plan.get('sequence_id', '-')}",
             "Goal: " + plan["goal"]]
    for index, step in enumerate(plan["steps"], 1):
        args = ", ".join(f"{k}={step[k]}" for k in
                         ("prefab", "count", "node_id", "area", "new_regions", "hunger_ratio", "seconds") if k in step)
        lines.append(f"  {index}. [{step.get('status', 'pending')}] {step['action']}({args})")
    if plan.get("reason"):
        lines.append("Reason: " + plan["reason"])
    return "\n".join(lines)
