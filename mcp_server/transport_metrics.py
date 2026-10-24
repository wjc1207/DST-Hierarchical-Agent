"""Server-side HTTP measurements. Never log bodies, credentials or model text."""
from __future__ import annotations

import json
import math
import sys
import threading
from pathlib import Path

LUA_STAGES = {"camera_ms", "entities_ms", "navigation_ms", "collection_paths_ms", "vitals_ms",
              "inventory_ms", "execution_read_ms", "crafting_ms", "control_status_ms",
              "board_ready_ms", "available_actions_ms", "state_total_ms", "native_tick_ms",
              "board_step_ms", "total_ms"}
RECEIPT_FIELDS = {"encode_ms", "request_bytes", "response_bytes", "http_status", "query_call_ms",
                  "query_to_callback_ms", "decode_ms", "callback_dispatch_ms"}


def numbers(value, allowed):
    return {k: v for k, v in value.items() if k in allowed and type(v) in {int, float}
            and math.isfinite(v) and 0 <= v < 1e12} if isinstance(value, dict) else {}


def lua_measurements(value, current_sequence):
    """Whitelist a small numeric schema; do not trust diagnostic fields as text."""
    if not isinstance(value, dict):
        return {}
    result = {"clock": "lua_real_seconds" if value.get("clock") == "lua_real_seconds" else "unavailable"}
    sample = value.get("sampling")
    if isinstance(sample, dict):
        result["sampling"] = {**numbers(sample, {"ticks"}),
                              "sum_ms": numbers(sample.get("sum_ms"), LUA_STAGES),
                              "max_ms": numbers(sample.get("max_ms"), LUA_STAGES)}
    result["refresh"] = numbers(value.get("refresh"), LUA_STAGES)
    receipts = value.get("previous_exchanges")
    result["previous_exchanges"] = []
    if isinstance(receipts, list):
        for receipt in receipts[-8:]:
            if not isinstance(receipt, dict) or type(receipt.get("sequence")) is not int \
                    or not 0 < receipt["sequence"] < current_sequence:
                continue
            entry = {"sequence": receipt["sequence"], **numbers(receipt, RECEIPT_FIELDS)}
            for key in ("server_id", "action_id"):
                identity = receipt.get(key)
                if isinstance(identity, str) and len(identity) <= 160 \
                        and all(c.isalnum() or c in ":_-" for c in identity):
                    entry[key] = identity
            entry["accepted"] = receipt.get("accepted") is True
            result["previous_exchanges"].append(entry)
    return result


class TransportRecorder:
    def __init__(self, path: Path | None):
        self.path, self.lock, self.warned = path, threading.Lock(), False

    def write(self, record):
        if self.path is None:
            return
        try:
            encoded = json.dumps(record, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
            with self.lock:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as stream:
                    stream.write(encoded + "\n")
        except (OSError, ValueError, TypeError):
            if not self.warned:
                self.warned = True
                print("mcp_transport_timing_unavailable: measurement skipped; control is unchanged", file=sys.stderr)
