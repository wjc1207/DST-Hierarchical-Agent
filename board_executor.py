"""Deterministic local board execution; no model calls or exploratory routing."""
from __future__ import annotations

import math
import time

import controller
from task_board import board_view

MINE_PRODUCTS = {"rock1": {"rocks", "flint", "nitre"},
                 "rock2": {"rocks", "flint", "goldnugget"},
                 "rock_flintless": {"rocks"}, "rock_flintless_med": {"rocks"},
                 "rock_flintless_low": {"rocks"}}


def light_signature(state):
    phase = state.get("world", {}).get("phase")
    return (phase, controller.item_count(state, "torch", equipped_only=True) > 0,
            controller.has_nearby_lit_fire(state)) if phase == "night" else (phase,)


class LocalBoardExecutor:
    def __init__(self):
        self.paused = {}

    @staticmethod
    def initial_handoff(state):
        if state.get("player", {}).get("vitals", {}).get("dead"):
            return "player_dead"
        if any(e.get("activeThreat") for e in state.get("nearby", [])):
            return "targeted_by_hostile"
        if controller.night_travel_without_torch(state) and not controller.has_nearby_lit_fire(state):
            return "night_light_requires_decision"
        return None

    @staticmethod
    def guard(baseline, action=None):
        def check(state):
            reason = LocalBoardExecutor.initial_handoff(state)
            old, new = baseline.get("player", {}), state.get("player", {})
            if old.get("guid") != new.get("guid"):
                reason = "player_changed"
            health, previous = new.get("vitals", {}).get("health"), old.get("vitals", {}).get("health")
            if health is not None and previous is not None and float(health) < float(previous):
                reason = "health_lost"
            if light_signature(state) != light_signature(baseline):
                reason = reason or "light_changed"
            if action and action.get("_board_target_guid") is not None:
                work = state.get("work") or {}
                if (int(work.get("sequence") or 0) > int((baseline.get("work") or {}).get("sequence") or 0)
                        and work.get("target_guid") != action["_board_target_guid"]):
                    reason = "work_target_changed"
            if reason:
                raise controller.AutoExecutionInterrupted(reason)
        return check

    @staticmethod
    def key(board, task, action):
        return (board.get("board_id"), task["id"], action["kind"], action.get("prefab"),
                action.get("guid"), action.get("_board_target_guid"))

    def pause(self, board, task, action):
        self.paused[self.key(board, task, action)] = time.monotonic() + 15

    def choose(self, board, state, dispatch):
        if (not board or board.get("kind") != "task_board" or self.initial_handoff(state)
                or board.get("status") in {"completed", "invalidated", "expired"}):
            return None
        view = board_view(board, state)
        if view["status"] != "active" or view["state_basis"] != "observed":
            return None
        now = time.monotonic()
        self.paused = {key: until for key, until in self.paused.items() if until > now}
        sources = dict(controller.COLLECT_PRODUCTS)
        sources["carrot_planted"] = "carrot"
        for task in view["tasks"]:
            if task["status"] not in {"ready", "in_progress"}:
                continue
            target = task["target"]
            choices = []
            if target["type"] == "inventory":
                product = target["prefab"]
                for key, action in dispatch.items():
                    if action["kind"] == "collect" and sources.get(action["prefab"], action["prefab"]) == product:
                        entity = controller.find_target(state, action["prefab"], action.get("guid"))
                        if entity and controller.collect_target_available(entity) and controller.collect_target_reachable(state, entity):
                            distance = float(entity.get("collect_path", {}).get("distance", entity.get("distance", math.inf)))
                            choices.append((distance, key, {**action, "_strict_preferred": True}))
                craft = {"axe": "craft_axe", "pickaxe": "craft_pickaxe", "torch": "craft_torch"}.get(product)
                if craft in dispatch:
                    choices.append((0, craft, dispatch[craft]))
                if not choices:
                    work = self._work_choice(product, state, dispatch)
                    if work:
                        choices.append(work)
            elif target["type"] == "hunger_ratio" and "eat_safe_food" in dispatch:
                choices.append((0, "eat_safe_food", dispatch["eat_safe_food"]))
            elif target["type"] == "structure":
                key = {"campfire": "build_campfire", "sciencemachine": "build_sciencemachine"}.get(target["prefab"])
                if key in dispatch:
                    choices.append((0, key, dispatch[key]))
            # Visit/search tasks require navigation choices, not local execution.
            for _, key, action in sorted(choices, key=lambda v: (v[0], v[1])):
                if self.key(view, task, action) not in self.paused:
                    return task, key, action
        return None

    @staticmethod
    def _work_choice(product, state, dispatch):
        if not state.get("telemetry", {}).get("cancel_work"):
            return None
        kind, tag, tool, maximum = (("chop_nearest_tree", "CHOP_workable", "axe", 100)
                                   if product == "log" else ("mine_nearest_rock", "MINE_workable", "pickaxe", 33))
        uses = controller._conservative_tool_uses(state, tool, maximum)
        candidates = [e for e in state.get("nearby", []) if tag in e.get("tags", [])
                      and float(e.get("distance", math.inf)) <= 6 and e.get("work_required") is not None
                      and uses >= int(e["work_required"])]
        if kind not in dispatch or not candidates:
            return None
        # Match the same nearest eligible target the native work bridge selects.
        entity = min(candidates, key=lambda e: float(e.get("distance", math.inf)))
        if kind.startswith("mine") and product not in MINE_PRODUCTS.get(entity.get("prefab"), set()):
            return None
        if kind.startswith("chop") and entity.get("prefab") not in {"evergreen", "evergreen_sparse", "deciduoustree", "twiggytree"}:
            return None
        if not controller.collect_target_reachable(state, entity):
            return None
        return (float(entity["distance"]), kind, {**dispatch[kind], "_board_target_guid": entity.get("guid")})


def task_progressed(board, task, before, after):
    """Only an observed increase toward this task counts, not time or hunger loss."""
    def progress(state):
        return next(t["progress"]["current"] for t in board_view(board, state)["tasks"]
                    if t["id"] == task["id"])
    return progress(after) > progress(before)
