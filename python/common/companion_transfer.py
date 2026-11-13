"""Companion item handoff contracts; owner inventory is never a source."""
from copy import deepcopy
import re

COUNTED_TARGETS = {"give_owner", "drop_item", "deposit", "withdraw"}
STORAGE_TARGETS = {"inspect_storage", "deposit", "withdraw"}
TRANSFER_TARGETS = COUNTED_TARGETS | STORAGE_TARGETS | {"receive_item"}


def guid(value):
    if type(value) is not int or not 0 < value <= 2**53-1:
        raise ValueError("An observed container_guid is required")
    return value


def validate_target(target):
    kind = target["type"]
    fields = {"type"}
    clean = {"type": kind}
    if kind in STORAGE_TARGETS:
        fields.add("container_guid")
        clean["container_guid"] = guid(target.get("container_guid"))
    if kind != "inspect_storage":
        fields.update({"prefab", "count"})
        if not isinstance(target.get("prefab"), str) or not re.fullmatch(r"[a-z0-9_]{1,60}", target["prefab"]):
            raise ValueError("Transfer requires a canonical item prefab")
        if type(target.get("count")) is not int or not 1 <= target["count"] <= 100:
            raise ValueError("Transfer count must be 1..100")
        clean.update(prefab=target["prefab"], count=target["count"])
    if kind == "receive_item":
        fields.add("timeout_seconds")
        timeout = target.get("timeout_seconds", 120)
        if type(timeout) not in (int, float) or not 10 <= timeout <= 600:
            raise ValueError("Waiting timeout must be 10..600 seconds")
        clean["timeout_seconds"] = timeout
    if set(target)-fields:
        raise ValueError("Unsupported companion transfer fields")
    return clean


def query_storage(observed, container_guid=None):
    if container_guid is not None:
        guid(container_guid)
    return {"containers": [deepcopy(row) for row in observed.get("containers", [])
                           if container_guid is None or row["guid"] == container_guid],
            "note": "Only contents_known=true is a current local server observation; missing/busy is unknown, not empty."}


STORAGE_TOOL = {"type": "function", "function": {
    "name": "query_storage", "description": "Read currently observed nearby chest contents and availability. Unknown is not empty.",
    "parameters": {"type": "object", "properties": {"container_guid": {"type": "integer"}}, "additionalProperties": False}}}
