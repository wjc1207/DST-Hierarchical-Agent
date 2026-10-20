"""Own exploration intents and topology traversal; JEV still selects local actions."""
from __future__ import annotations

import time

import controller
import timing_log as timing
from board_executor import MINE_PRODUCTS
from task_board import board_view
from exploration_events import resource_types


# Same conservative whole-target workloads as the Mod's work bridge. A recipe
# being craftable is not evidence that a usable tool is already carried.
WORK_SOURCES = {
    **{prefab: ("pickaxe", 33, uses, "MINE_workable") for prefab, uses in {
        "rock1": 6, "rock2": 6, "rock_flintless": 6,
        "rock_flintless_med": 4, "rock_flintless_low": 2,
    }.items()},
    **{prefab: ("axe", 100, 15, "CHOP_workable") for prefab in
       ("evergreen", "evergreen_sparse", "deciduoustree", "twiggytree")},
}


def local_candidates(criteria, dispatch):
    """Keep navigation in the executor, including in speculative JEV requests."""
    actions = {k: a for k, a in dispatch.items() if a["kind"] != "explore"}
    return {k: v for k, v in criteria.items() if k in actions}, actions


def exploration_key(key):
    return key.startswith(("explore_", "travel_region_"))


def sources_for(product):
    sources = {product} | {k for k, v in controller.COLLECT_PRODUCTS.items() if v == product}
    sources.update(k for k, v in MINE_PRODUCTS.items() if product in v)
    if product == "log":
        sources.update({"evergreen", "evergreen_sparse", "deciduoustree", "twiggytree"})
    return sources


def usable_sources_for(product, state):
    """Filter source relations by currently carried, conservatively usable tools."""
    uses = {}
    result = set()
    for source in sources_for(product):
        work = WORK_SOURCES.get(source)
        if work is None:
            result.add(source)
            continue
        tool, maximum, required, _ = work
        if tool not in uses:
            uses[tool] = controller._conservative_tool_uses(state, tool, maximum)
        if uses[tool] >= required:
            result.add(source)
    return result


def visible_sources(state, sources):
    """Fresh positive evidence must be obtainable, not depleted or across water."""
    result = set()
    for entity in state.get("nearby", []):
        prefab = entity.get("prefab")
        if prefab not in sources or not controller.collect_target_reachable(state, entity):
            continue
        work = WORK_SOURCES.get(prefab)
        if work:
            tool, maximum, _, tag = work
            required = entity.get("work_required")
            if (tag not in (entity.get("tags") or []) or required is None
                    or controller._conservative_tool_uses(state, tool, maximum) < required):
                continue
        elif not controller.collect_target_available(entity):
            continue
        result.add(prefab)
    return result


class ExplorationExecutor:
    def __init__(self):
        self.player = None
        self.last_observed = None
        self.path = []
        self.visited = set()
        self.closed = set()
        self.deferred_rooms = {}
        # Negative source evidence has no timer: only new relevant evidence can
        # restore its priority. This does not ban transit or unrelated tasks.
        self.source_failures = {}
        self.visits_done = set()
        self.intent = None
        self.goal = None
        self.paused = {}
        self.handoff = True
        self.last_event = None
        self.strategy = None
        self.resources = None
        self.fresh_handoff = None

    @staticmethod
    def identity(action):
        return (action.get("navigation_mode"), action.get("target_node_id"),
                action.get("search_region_id") or action.get("_executor_region"))

    def _observe(self, state):
        player = state.get("player", {}).get("guid")
        observed = state.get("observed_at")
        if (self.player != player or (observed is not None and self.last_observed is not None
                                      and observed < self.last_observed)):
            self.__init__()
        self.player, self.last_observed = player, observed
        resources = resource_types(state, controller.exploration_resource_available)
        if self.resources is not None and resources != self.resources:
            self.handoff = True
        self.resources = resources
        node = (state.get("navigation", {}).get("graph") or {}).get("current_node")
        if node and (not self.path or self.path[-1] != node):
            if node in self.path:
                self.path = self.path[:self.path.index(node) + 1]
            else:
                self.path.append(node)
            self.visited.add(node)
        self.paused = {k: until for k, until in self.paused.items() if until > time.monotonic()}
        return node

    def _goal(self, state, board, node):
        if not board or board.get("kind") != "task_board":
            return ("exploration", None)
        view = board_view(board, state)
        if view["status"] != "active":
            return ("exploration", None)
        for task in view["tasks"]:
            target = task["target"]
            if target["type"] == "visit_region":
                key = (board.get("board_id"), task["id"])
                if target["node_id"] == node:
                    self.visits_done.add(key)
                # A historical 'entered' flag is not proof this requested trip occurred.
                if key not in self.visits_done and task["status"] != "blocked":
                    return ("visit", target["node_id"])
        for task in view["tasks"]:
            target = task["target"]
            if task["status"] in {"ready", "in_progress"} and target["type"] == "inventory":
                return ("inventory", target["prefab"])
        return ("exploration", None)

    def _source_evidence(self, state, node, product, sources, area):
        key = (node, product)
        recorded = sources.intersection(area.get("resources_seen", []))
        live = visible_sources(state, sources)
        if key in self.source_failures and live:
            self._restore_source(key, "visible_obtainable_source")
        elif key in self.source_failures and recorded - self.source_failures[key]:
            self._restore_source(key, "new_usable_source_type")
        # Absence at an entrance, unsafe paths, darkness and a temporarily
        # blocked frontier are not proof that the room's sources were exhausted.
        if (recorded and not live and area.get("boundary_complete")
                and state.get("navigation", {}).get("status") == "no_frontier"
                and key not in self.source_failures):
            self.source_failures[key] = frozenset(recorded)
            self.closed.add(node)
            print(f"exploration_source_failed node={node} product={product} "
                  "reason=no_source_after_survey")
            timing.annotate(exploration_source_failure={"node": node, "product": product,
                                                       "reason": "no_source_after_survey"})

    def _restore_source(self, key, reason):
        self.source_failures.pop(key, None)
        print(f"exploration_source_reconfirmed node={key[0]} product={key[1]} reason={reason}")
        timing.annotate(exploration_source_reconfirmed={"node": key[0], "product": key[1],
                                                       "reason": reason})

    def _known_source(self, node, product, sources, area):
        recorded = sources.intersection(area.get("resources_seen", []))
        key = (node, product)
        if key in self.source_failures and recorded - self.source_failures[key]:
            self._restore_source(key, "new_usable_source_type")
        return bool(recorded and key not in self.source_failures
                    and self.deferred_rooms.get(node, 0) <= time.monotonic())

    def choose(self, state, dispatch, board=None):
        node = self._observe(state)
        if (state.get("player", {}).get("vitals", {}).get("dead")
                or any(e.get("activeThreat") for e in state.get("nearby", []))):
            self.handoff = True
            return None
        # dispatch has already passed the common light/hunger/danger legality checks.
        actions = {k: {**a, "_executor_region": node if a.get("navigation_mode") != "graph" else None}
                   for k, a in dispatch.items() if a["kind"] == "explore"}
        actions = {k: a for k, a in actions.items()
                   if self.paused.get(self.identity(a), 0) <= time.monotonic()}
        goal = self._goal(state, board, node)
        if goal != self.goal:
            self.intent = None
        self.goal = goal
        graph = state.get("navigation", {}).get("graph") or {}
        current_area = graph.get("current") or {}
        sources = usable_sources_for(goal[1], state) if goal[0] == "inventory" else set()
        if goal[0] == "inventory":
            self._source_evidence(state, node, goal[1], sources, current_area)
            timing.annotate(exploration_unusable_sources=sorted(sources_for(goal[1]) - sources))
        if not actions:
            return None
        routes = graph.get("routes")
        if routes is None:
            routes = [*(graph.get("adjacent") or []),
                      *([graph["return_route"]] if graph.get("return_route") else [])]
        route_map = {r.get("node_id"): r for r in routes}
        if self.intent and self.strategy in {"source_room", "known_source_bfs"}:
            target = self.intent[1] if self.intent[0] == "graph" else node
            area = current_area if target == node else route_map.get(target, {})
            if not self._known_source(target, goal[1], sources, area):
                self.intent = None
        if self.intent:
            match = next(((k, a) for k, a in actions.items()
                          if self.identity(a) == self.intent), None)
            if match and not (self.intent[0] == "graph" and self.intent[1] == node):
                return match
            self.intent = None

        if current_area.get("boundary_complete"):
            self.closed.add(node)
        if not any(a.get("navigation_mode") in {"search", "region"} for a in actions.values()):
            if not current_area.get("boundary_complete"):
                self.deferred_rooms[node] = time.monotonic() + 15
        travel = [(k, a, route_map.get(a.get("target_node_id"), {}))
                  for k, a in actions.items() if a.get("navigation_mode") == "graph"]
        # Mod destinations already carry BFS hop counts across verified graph edges.
        travel.sort(key=lambda item: (int(item[2].get("hops", 1)),
                                     float(item[2].get("distance", 999)),
                                     str(item[1].get("target_node_id"))))

        def select(pair, strategy):
            self.intent, self.strategy = self.identity(pair[1]), strategy
            return pair

        if goal[0] == "visit":
            match = next(((k, a) for k, a, _ in travel if a.get("target_node_id") == goal[1]), None)
            if match:
                return select(match, "board_visit")
        if goal[0] == "inventory":
            # Search inside a known source room before treating an entrance as exhausted.
            if self._known_source(node, goal[1], sources, current_area):
                if "explore_current_region" in actions:
                    return select(("explore_current_region", actions["explore_current_region"]), "source_room")
            for key, action, route in travel:
                if self._known_source(action.get("target_node_id"), goal[1], sources, route):
                    return select((key, action), "known_source_bfs")

        # Depth-first branch order, using the existing safe short-leg navigator.
        unseen = [(k, a, r) for k, a, r in travel
                  if not r.get("entered") and a.get("target_node_id") not in self.visited]
        direct = next(((k, a) for k, a, r in unseen if int(r.get("hops", 1)) == 1), None)
        if direct:
            return select(direct, "dfs_neighbor")
        for key in ("explore_other_region", "explore_current_region"):
            if key in actions:
                return select((key, actions[key]), "dfs_unknown_exit" if key == "explore_other_region" else "local_frontier")
        if len(self.path) >= 2:
            parent = self.path[-2]
            match = next(((k, a) for k, a, _ in travel if a.get("target_node_id") == parent), None)
            failed_parent = goal[0] == "inventory" and (parent, goal[1]) in self.source_failures
            if match and not (failed_parent and parent in self.closed):
                return select(match, "dfs_backtrack")
        if unseen:
            return select(unseen[0][:2], "dfs_pending_branch")
        for key, action, route in travel:
            target = action.get("target_node_id")
            if (target not in self.closed and not route.get("boundary_complete")
                    and self.deferred_rooms.get(target, 0) <= time.monotonic()):
                return select((key, action), "dfs_unfinished_room")
        return None

    def on_event(self, event, _state):
        self.last_event = event
        reason = event.get("reason")
        self.handoff = reason in {"resource_types_changed", "targeted_by_hostile", "potential_threat",
                                  "health_lost", "critical_hunger_with_food", "night_light_lost",
                                  "phase_changed", "player_dead", "player_changed"}
        if reason in {"destination_reached", "region_changed", "path_unavailable", "route_paused",
                      "movement_stalled", "region_confirmation_timeout"}:
            if self.intent and reason not in {"destination_reached", "region_changed"}:
                self.paused[self.intent] = time.monotonic() + 15
            self.intent = None

    def after_local_action(self, kind):
        # A local action may expose another worthwhile local action. 'wait' releases
        # local arbitration; it no longer vetoes the executor's safe exploration.
        self.handoff = kind != "wait"
