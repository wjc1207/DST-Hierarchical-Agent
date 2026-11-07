"""Companion facts/actions adapted to the same priority board used by the player."""
import copy
import math
import re
import time
import uuid

from common.coordinates import validate_point
from common.task_timing import number
from common.places import target_progress as building_progress
from mcp_agent.priority_board import PriorityBoardExecutor, validate_goals, merge_goals
from mcp_agent.companion_search import ResourceSearch
from common.companion_transfer import (COUNTED_TARGETS, STORAGE_TARGETS, TRANSFER_TARGETS,
                                       validate_target as validate_transfer)

CAPABILITIES = {"collect", "move", "follow", "stay", "recipes", "equip", "unequip", "work", "craft", "place", "search",
                "storage", "give_owner", "drop_item", "receive_item", "visit_building"}
RESOURCE_NAMES = {"grass": "cutgrass", "sapling": "twigs", "berrybush": "berries", "berrybush2": "berries",
                  "carrot_planted": "carrot", "reeds": "cutreeds", "logs": "log"}
STORAGE_WAIT_SECONDS = 60
HAND_GATHERED_TOOL_MATERIALS = {"twigs", "flint"}


def collection_prefab(prefab, state):
    rows = state.get("nearby") or []
    products = {row.get("product") for row in rows}
    inventory = (state.get("buddy") or {}).get("inventory") or {}
    if prefab in products or prefab in inventory:
        return prefab
    matching = {row["product"] for row in rows if row.get("prefab") == prefab and row.get("product")}
    return matching.pop() if len(matching) == 1 else RESOURCE_NAMES.get(prefab, prefab)


def capabilities(observed):
    return sorted(CAPABILITIES.intersection(observed.get("capabilities") or []))


def validate_target(target, *, supported=CAPABILITIES):
    kind = target.get("type")
    if kind in TRANSFER_TARGETS:
        required = "storage" if kind in STORAGE_TARGETS else kind
        if required not in supported:
            raise ValueError("Unsupported companion capability: " + required)
        return validate_transfer(target)
    required = {"inventory": "craft" if "recipe_id" in target else "collect", "structure": "place", "equip": "equip", "unequip": "unequip", "visit_building": "visit_building",
                "move_to": "move", "return_owner": "follow", "follow": "follow", "stay": "stay"}
    if kind not in required or required[kind] not in supported:
        raise ValueError("Unsupported companion task target/capability: " + str(kind))
    fields = {"inventory": {"type", "prefab", "count", "recipe_id", "allow_search"}, "move_to": {"type", "x", "z", "arrival_radius"},
              "structure": {"type", "prefab", "recipe_id", "x", "z", "radius"},
              "equip": {"type", "prefab"}, "unequip": {"type", "prefab"}, "visit_building": {"type", "building_id"}}
    if set(target) - fields.get(kind, {"type"}):
        raise ValueError("Unsupported companion target fields")
    if kind == "visit_building":
        identity = target.get("building_id")
        if not isinstance(identity, str) or not re.fullmatch(r"building:[0-9]{1,16}", identity):
            raise ValueError("Visit requires a remembered building_id")
        return {"type": kind, "building_id": identity}
    if kind == "inventory":
        prefab, count = target.get("prefab"), target.get("count")
        if (not isinstance(prefab, str) or not re.fullmatch(r"[a-z0-9_]{1,60}", prefab)
                or type(count) is not int or not 1 <= count <= 100):
            raise ValueError("Inventory target requires prefab and total count 1..100")
        clean = {"type": kind, "prefab": prefab, "count": count}
        if "allow_search" in target:
            if type(target["allow_search"]) is not bool:
                raise ValueError("allow_search must be a boolean")
            clean["allow_search"] = target["allow_search"]
        if "recipe_id" in target:
            if not isinstance(target["recipe_id"], str) or not re.fullmatch(r"[a-z0-9_]{1,100}", target["recipe_id"]):
                raise ValueError("Invalid companion recipe_id")
            clean["recipe_id"] = target["recipe_id"]
        return clean
    if kind in {"equip", "unequip"}:
        prefab = target.get("prefab")
        if not isinstance(prefab, str) or not re.fullmatch(r"[a-z0-9_]{1,60}", prefab):
            raise ValueError("Equipment target requires an item prefab")
        return {"type": kind, "prefab": prefab}
    if kind == "structure":
        clean = validate_target({"type": "inventory", "prefab": target.get("prefab"), "count": 1,
                                 "recipe_id": target.get("recipe_id")}, supported=CAPABILITIES)
        point = validate_point(target)
        radius = target.get("radius", 6)
        if not number(radius) or not 2 <= radius <= 8:
            raise ValueError("Structure placement radius must be 2..8")
        return {"type": kind, "prefab": clean["prefab"], "recipe_id": clean["recipe_id"],
                "x": point["x"], "z": point["z"], "radius": radius}
    if kind == "move_to":
        point = validate_point({"arrival_radius": 2.5, **target})
        if point["arrival_radius"] != 2.5:
            raise ValueError("Companion move arrival_radius must match native radius 2.5")
        return {"type": kind, **point}
    return {"type": kind}


def validate_board(raw, day, *, observed=None):
    supported = capabilities(observed) if observed is not None else CAPABILITIES
    return validate_goals(raw, day, target_validator=lambda target: validate_target(target, supported=supported))


def actor_state(observed):
    """Only normalize observed facts; never invent a map, hunger maximum or recipes."""
    buddy = observed.get("buddy") or {}
    return {"player": {"guid": buddy.get("guid"), "position": copy.deepcopy(buddy.get("position")),
                       "vitals": {"dead": buddy.get("dead")}, "inventory": {"items": [
                           {"prefab": name, "count": count} for name, count in (buddy.get("inventory") or {}).items()]}},
            "world": copy.deepcopy(observed.get("world") or {}), "_companion": observed,
            "observed_at": observed.get("version")}


def evaluate_target(target, state):
    observed = state["_companion"]
    buddy = observed.get("buddy") or {}
    kind = target["type"]
    if kind in COUNTED_TARGETS:
        return 0, target["count"], True, ""  # Only per-task confirmed receipts count.
    if kind in {"inventory", "receive_item"}:
        return (buddy.get("inventory") or {}).get(target["prefab"], 0), target["count"], True, ""
    if kind == "visit_building":
        done, reason = building_progress(target, observed.get("places"))
        return int(done), 1, not bool(reason), reason
    if kind == "inspect_storage":
        known = "containers" in observed
        done = any(r["guid"] == target["container_guid"] and r.get("contents_known") for r in observed.get("containers", []))
    elif kind == "equip":
        known = "equipment" in observed
        done = any(row["prefab"] == target["prefab"] and row.get("equipped") and row.get("usable") for row in observed.get("equipment", []))
    elif kind == "unequip":
        known = "equipment" in observed
        done = known and not any(row["prefab"] == target["prefab"] and row.get("equipped") for row in observed.get("equipment", []))
    elif kind == "structure":
        known = "structures" in observed
        done = any(row["prefab"] == target["prefab"] and math.hypot(row["x"]-target["x"], row["z"]-target["z"]) <= target["radius"]
                   for row in observed.get("structures", []))
    elif kind == "move_to":
        point = buddy.get("position") or {}
        known = all(number(point.get(k)) for k in ("x", "z"))
        done = known and math.hypot(point["x"]-target["x"], point["z"]-target["z"]) <= target["arrival_radius"]
    elif kind == "return_owner":
        known = number(buddy.get("owner_distance"))
        done = known and buddy["owner_distance"] <= 4
    else:
        known = observed.get("mode") is not None
        done = observed.get("mode") == kind
    return int(bool(done)), 1, known, "" if known else "companion telemetry missing"


class CompanionTasks:
    """Only native dispatch differs. Timing, dependencies, focus and progress are shared."""
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.goals = PriorityBoardExecutor(clock=clock, target_evaluator=evaluate_target, progress_evaluator=self.progress)
        self.search = ResourceSearch(clock)
        self.plan = None
        self.observed = None

    @staticmethod
    def progress(task, record, state):
        if task["target"]["type"] == "visit_building" and record.get("arrival_receipt"):
            task.update(progress={"current": 1, "required": 1}, status="done", blocked_reason="")
            return
        if task["target"]["type"] not in COUNTED_TARGETS:
            return
        if "transfer_key" not in record:
            record["transfer_key"] = uuid.uuid4().hex
        key = record["transfer_key"]
        for receipt in state["_companion"].get("transfer_receipts", []):
            if receipt.get("task_key") == key and type(receipt.get("count")) is int:
                record["camp_count"] = max(record.get("camp_count", 0), receipt["count"])
        current, required = record.get("camp_count", 0), task["target"]["count"]
        task.update(progress={"current": current, "required": required},
                    status="done" if current >= required else "in_progress" if current else "ready", blocked_reason="")

    def view(self, observed=None):
        if observed is not None:
            self.observed = observed
            self.search.observe(observed)
        if not self.plan or not self.observed:
            return None
        result = self.goals.view(self.plan, actor_state(self.observed))
        result["search_memory"] = self.search.summary()
        return result

    def pause_search(self):
        self.goals.pause_accounting()
        self.search.inflight = None
        for record in self.goals.records.values():
            self.clear_storage_wait(record)

    @staticmethod
    def clear_storage_wait(record):
        for key in ("storage_wait_started", "storage_retry_at", "storage_busy_version"):
            record.pop(key, None)

    def storage_wait(self, task, chest=None):
        record = self.goals.records[task["id"]]
        started = record.setdefault("storage_wait_started", self.clock())
        waited = max(0, self.clock()-started)
        return task, "needs_replan" if waited >= STORAGE_WAIT_SECONDS else "waiting_storage", {
            "reason": "chest_busy_timeout" if waited >= STORAGE_WAIT_SECONDS else "chest_busy",
            "container_guid": task["target"]["container_guid"], "waited_seconds": round(waited, 1),
            "busy_openers": (chest or {}).get("busy_openers", [])}

    def note_action(self, task, op, args):
        self.goals.pause_accounting()
        if op == "search":
            self.search.inflight = {"task_id": task["id"], **args}
            self.goals.selected, self.goals.navigation, self.goals.navigation_mode = task["id"], True, "search"

    def on_result(self, task_id, action, observed):
        record = self.goals.records.get(task_id)
        data = action.get("data") or {}
        if record and data.get("task_key") == record.get("transfer_key") and type(data.get("count")) is int:
            record["camp_count"] = max(record.get("camp_count", 0), data["count"])
        task = next((t for t in (self.plan or {}).get("tasks", []) if t["id"] == task_id), None)
        if (record and task and task["target"]["type"] == "visit_building" and action["status"] == "completed"
                and action.get("reason") == "building_arrived" and data.get("arrived") is True
                and data.get("building_id") == task["target"]["building_id"]):
            record.update(done=True, arrival_receipt=dict(data))
            return True
        if record and task and task["target"]["type"] in STORAGE_TARGETS:
            if action["status"] == "failed" and action.get("reason") == "chest_busy":
                record.setdefault("storage_wait_started", self.clock())
                record.update(storage_retry_at=self.clock()+.5, storage_busy_version=observed.get("version"))
                return True  # An occupancy race is a transient wait, not an action failure.
            if action["status"] == "completed" and action.get("reason") in {"one_item_transferred", "transfer_total_already_met"}:
                self.clear_storage_wait(record)
        if not self.search.inflight or self.search.inflight["task_id"] != task_id:
            return False
        self.view(observed)  # Account real travel before releasing the search leg.
        self.search.result(action["status"], action.get("reason", ""))
        self.goals.pause_accounting()
        return True

    def adopt(self, plan, observed):
        previous = self.view(observed)
        clean = validate_board(plan, plan["day"], observed=observed)
        result = merge_goals(previous, clean, target_validator=lambda t: validate_target(t, supported=capabilities(observed)))
        if "reply" in plan:
            result["reply"] = plan["reply"]
        self.plan = result
        # A completed milestone can be repeated in a verification patch as history.
        # Same ID/target keeps its receipt; new IDs or changed targets create fresh work.
        self.goals.adopt(result)
        return result

    @staticmethod
    def recipe_action(target, observed):
        row = (observed.get("recipes") or {}).get(target["recipe_id"], {})
        placement = target["type"] == "structure"
        if (not row.get("supported") or row.get("product") != target["prefab"] or bool(row.get("placement")) != placement):
            return "needs_replan", {"reason": "query_supported_recipe:" + target["recipe_id"]}
        if not row.get("ready"):
            return "needs_replan", {"reason": row.get("reason", "recipe_not_ready"), "recipe_id": target["recipe_id"]}
        return ("place", {k: target[k] for k in ("recipe_id", "x", "z", "radius")}) if placement else ("craft", {"recipe_id": target["recipe_id"]})

    @staticmethod
    def unequip_action(item, observed):
        if "unequip" not in capabilities(observed):
            return "needs_replan", {"reason": "unequip_capability_unavailable:update_server_mod"}
        if item.get("can_unequip") is False:
            return "needs_replan", {"reason": item.get("unequip_reason") or "cannot_stow_equipment"}
        return "unequip", {"item_guid": item["guid"]}

    def next_action(self, observed):
        view = self.view(observed)
        resumed = False
        for task in view["tasks"]:
            target = task["target"]
            if target["type"] == "inventory" and any(r["product"] == target["prefab"] for r in observed.get("nearby", [])):
                resumed = self.goals.resume_local_work(task["id"]) or resumed
        if resumed:
            view = self.view(observed)
        if view["status"] == "completed":
            return None, "completed", {}
        eligible = self.goals.eligible_tasks(view)
        navigation = []
        for task in eligible:
            target = task["target"]
            kind = target["type"]
            required = {"inventory": "craft" if target.get("recipe_id") else "collect", "structure": "place",
                        "move_to": "move", "return_owner": "follow", **{t: "storage" for t in STORAGE_TARGETS}}.get(kind, kind)
            if required not in capabilities(observed):
                return task, "unsupported", {"reason": "capability_unavailable:" + required}
            if kind == "visit_building":
                building = next((r for r in (observed.get("places") or {}).get("buildings", []) if r["id"] == target["building_id"]), None)
                if not building or building.get("status") in {"missing", "unavailable"}:
                    return task, "needs_replan", {"reason": "building_missing_or_unavailable"}
                navigation.append((task, "visit_building", {"building_id": target["building_id"]}))
                continue
            if kind in STORAGE_TARGETS:
                chest = next((r for r in observed.get("containers", []) if r["guid"] == target["container_guid"]), None)
                record = self.goals.records[task["id"]]
                retry_stale = (self.clock() < record.get("storage_retry_at", 0)
                               and observed.get("version") == record.get("storage_busy_version"))
                if (chest or {}).get("reason") == "chest_busy" or retry_stale:
                    waiting = self.storage_wait(task, chest)
                    if waiting[1] == "needs_replan":
                        return waiting
                    navigation.append(waiting)
                    continue
                if not chest or not chest.get("contents_known"):
                    return task, "needs_replan", {"reason": "chest_unavailable"}
            if kind in COUNTED_TARGETS:
                if kind in {"give_owner", "deposit", "drop_item"} and not (observed["buddy"].get("inventory") or {}).get(target["prefab"], 0):
                    return task, "needs_replan", {"reason": "transfer_source_missing:" + target["prefab"]}
                if kind in {"give_owner", "deposit", "drop_item"}:
                    gear = [r for r in observed.get("equipment", []) if r["prefab"] == target["prefab"]]
                    worn = next((r for r in gear if r.get("equipped")), None)
                    if worn and not any(not r.get("equipped") for r in gear):
                        return task, *self.unequip_action(worn, observed)
                if kind in STORAGE_TARGETS:
                    if kind == "withdraw" and not any(i["prefab"] == target["prefab"] and i["count"] > 0 for i in chest.get("items", [])):
                        return task, "needs_replan", {"reason": "chest_source_missing:" + target["prefab"]}
                if kind == "give_owner" and observed["buddy"].get("owner_distance", math.inf) > 15:
                    navigation.append((task, "waiting" if observed.get("mode") == "follow" else "follow", {}))
                    continue
                record = self.goals.records[task["id"]]
                return task, kind, {"prefab": target["prefab"], "total_count": target["count"],
                                   "task_key": record["transfer_key"],
                                   **({"container_guid": target["container_guid"]} if kind in STORAGE_TARGETS else {})}
            if kind == "receive_item":
                record = self.goals.records[task["id"]]
                started = record.setdefault("wait_started", self.clock())
                if self.clock()-started >= target["timeout_seconds"]:
                    return task, "needs_replan", {"reason": "waiting_for_player_items_timed_out:" + target["prefab"]}
                if observed["buddy"].get("owner_distance", math.inf) > 4:
                    navigation.append((task, "waiting" if observed.get("mode") == "follow" else "follow", {}))
                else:
                    navigation.append((task, "awaiting_items" if observed.get("mode") == "stay" else "stay", {}))
                continue
            if kind == "inspect_storage":
                return task, "needs_replan", {"reason": "chest_unavailable"}
            if kind == "inventory":
                candidates = [row for row in observed.get("nearby", []) if row["product"] == target["prefab"]]
                if candidates:
                    return task, "collect", {"target_guid": min(candidates, key=lambda r: r["distance"])["guid"]}
                if target.get("recipe_id"):
                    return task, *self.recipe_action(target, observed)
                works = [r for r in observed.get("work_targets", []) if target["prefab"] in r.get("products", [])]
                if works and {"work", "equip"}.issubset(capabilities(observed)):
                    work = min(works, key=lambda r: r["distance"])
                    tools = [r for r in observed.get("equipment", []) if r.get("usable") and work["work"] in r.get("tools", [])]
                    if tools:
                        equipped = next((r for r in tools if r.get("equipped")), None)
                        if not equipped:
                            return task, "equip", {"item_guid": tools[0]["guid"]}
                        return task, "work", {"target_guid": work["guid"], "work": work["work"]}
                    # Trees and rocks can yield tool ingredients, but saplings
                    # and loose flint can supply them before the first tool.
                    if target["prefab"] not in HAND_GATHERED_TOOL_MATERIALS:
                        return task, "needs_replan", {"reason": "tool_required:" + work["work"]}
                if not candidates:
                    if target["prefab"] in observed.get("_team_claimed_products",[]):
                        navigation.append((task,"waiting_resource",{}))
                        continue
                    if "search" in capabilities(observed) and target.get("allow_search", True):
                        record = self.goals.records[task["id"]]
                        point = self.search.select(target["prefab"], observed,
                            max_distance=task["search_budget_distance"]-record["distance"])
                        if point:
                            products = list(dict.fromkeys(t["target"]["prefab"] for t in eligible
                                if t["target"]["type"] == "inventory" and not t["target"].get("recipe_id")))[:6]
                            remaining = task["search_budget_seconds"]-record["seconds"]
                            navigation.append((task, "search", {"point_id": point["id"], "x": point["x"], "z": point["z"],
                                "products": products, "timeout_seconds": max(.2, min(8, remaining))}))
                        else:
                            if self.search.select(target["prefab"], observed):
                                self.goals.suspend_search(task["id"])
                            else:
                                navigation.append((task, "blocked", {"reason": "no_safe_unsearched_frontier", "prefab": target["prefab"]}))
                    else:
                        navigation.append((task, "blocked", {"reason": "no_nearby_source_for_" + target["prefab"], "prefab": target["prefab"]}))
            if kind == "structure":
                return task, *self.recipe_action(target, observed)
            if kind == "equip":
                items = [r for r in observed.get("equipment", []) if r["prefab"] == target["prefab"] and r.get("usable")]
                if not items:
                    return task, "needs_replan", {"reason": "carried_equipment_required:" + target["prefab"]}
                return task, "equip", {"item_guid": items[0]["guid"]}
            if kind == "unequip":
                worn = next((r for r in observed.get("equipment", []) if r["prefab"] == target["prefab"] and r.get("equipped")), None)
                if worn:
                    return task, *self.unequip_action(worn, observed)
                return task, "needs_replan", {"reason": "equipment_telemetry_missing"}
            if kind == "move_to":
                navigation.append((task, "move", {k: target[k] for k in ("x", "z")}))
                continue
            if kind == "return_owner":
                navigation.append((task, "waiting" if observed.get("mode") == "follow" else "follow", {}))
                continue
            if kind in {"follow", "stay"}:
                navigation.append((task, kind, {}))
        return navigation[0] if navigation else (None, "idle", {})
