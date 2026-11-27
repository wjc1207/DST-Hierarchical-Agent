"""Companion item handoff contracts; owner inventory is never a source."""
from copy import deepcopy
import re

COUNTED_TARGETS = {"give_owner", "give_buddy", "drop_item", "deposit", "withdraw", "eat", "heal"}
STORAGE_TARGETS = {"inspect_storage", "deposit", "withdraw"}
TRANSFER_TARGETS = COUNTED_TARGETS | STORAGE_TARGETS | {"receive_item"}
BUDDY_SLOTS = {"self", "buddy1", "buddy2"}


def guid(value):
    if type(value) is not int or not 0 < value <= 2**53-1:
        raise ValueError("An observed container_guid is required")
    return value


def parse_building_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"building:[0-9]{1,30}", value):
        raise ValueError("Storage target requires an observed building:<GUID> ID")
    return value, guid(int(value.split(":", 1)[1]))


def container_guid_for(target, places=None):
    if type(target.get("container_guid")) is int:
        return target["container_guid"]
    building_id = target.get("building_id")
    if isinstance(building_id, str):
        for row in (places or {}).get("buildings", []):
            if row.get("id") == building_id and type(row.get("guid")) is int:
                return row["guid"]
        if re.fullmatch(r"building:[0-9]{1,30}", building_id):
            return int(building_id.split(":", 1)[1])
    return None


def validate_target(target):
    kind = target["type"]
    fields = {"type"}
    clean = {"type": kind}
    if kind in STORAGE_TARGETS:
        fields.update({"container_guid", "building_id"})
        has_guid = "container_guid" in target and target["container_guid"] is not None
        has_building = "building_id" in target and target["building_id"] is not None
        if not has_guid and not has_building:
            raise ValueError("An observed building_id or container_guid is required")
        if has_building:
            building_id, parsed_guid = parse_building_id(target["building_id"])
            if has_guid and guid(target["container_guid"]) != parsed_guid:
                raise ValueError("building_id and container_guid must refer to the same chest")
            clean["building_id"] = building_id
            clean["container_guid"] = parsed_guid
        else:
            clean["container_guid"] = guid(target.get("container_guid"))
    if kind == "give_buddy":
        fields.add("target_buddy_id")
        tid = target.get("target_buddy_id")
        if not isinstance(tid, str) or tid not in BUDDY_SLOTS:
            raise ValueError("give_buddy requires target_buddy_id in self, buddy1, buddy2")
        clean["target_buddy_id"] = tid
    if kind != "inspect_storage":
        fields.update({"prefab", "count"})
        if not isinstance(target.get("prefab"), str) or not re.fullmatch(r"[a-z0-9_]{1,60}", target["prefab"]):
            raise ValueError("Transfer requires a canonical item prefab")
        count = target.get("count", 1 if kind in {"eat", "heal"} else None)
        if type(count) is not int or not 1 <= count <= 100:
            raise ValueError("Item count must be 1..100")
        clean.update(prefab=target["prefab"], count=count)
    if kind == "receive_item":
        fields.add("timeout_seconds")
        timeout = target.get("timeout_seconds", 120)
        if type(timeout) not in (int, float) or not 10 <= timeout <= 600:
            raise ValueError("Waiting timeout must be 10..600 seconds")
        clean["timeout_seconds"] = timeout
    if set(target)-fields:
        raise ValueError("Unsupported companion transfer fields")
    return clean


def query_storage(observed, container_guid=None, building_id=None):
    target_guid = None
    if container_guid is not None:
        target_guid = guid(container_guid)
    if building_id is not None:
        _, parsed_guid = parse_building_id(building_id)
        if target_guid is not None and target_guid != parsed_guid:
            raise ValueError("building_id and container_guid must match")
        target_guid = parsed_guid
    return {"containers": [deepcopy(row) for row in observed.get("containers", [])
                           if target_guid is None or row["guid"] == target_guid],
            "note": "Only contents_known=true is a current local server observation; missing/busy is unknown, not empty."}


STORAGE_TOOL = {"type": "function", "function": {
    "name": "query_storage", "description": "Read currently observed nearby chest contents and availability. Unknown is not empty.",
    "parameters": {"type": "object", "properties": {
        "building_id": {"type": "string"}, "container_guid": {"type": "integer"}}, "additionalProperties": False}}}
