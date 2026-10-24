"""Extract resource presence priors, not spawn probabilities, from a local Klei scripts.zip.

No Lua is executed. Only literal positive distribution/count entries and explicit
weighted-prefab alternatives are retained; layouts, functions and settings remain unknown.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import zipfile

TOKEN = re.compile(r'--\[\[.*?\]\]|--[^\n]*|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|[A-Za-z_][\w]*|\d+(?:\.\d*)?|\.\d+|[^\s]', re.S)


def tokens(source):
    return [t for t in TOKEN.findall(source) if not t.startswith("--")]


def table(ts, start):
    if ts[start] != "{":
        return [], start
    depth = 0
    for end in range(start, len(ts)):
        depth += (ts[end] == "{") - (ts[end] == "}")
        if depth == 0:
            return ts[start + 1:end], end + 1
    raise ValueError("Unbalanced Lua table")


def fields(ts):
    result, i = {}, 0
    while i < len(ts) - 1:
        if re.fullmatch(r"[A-Za-z_]\w*", ts[i]) and ts[i + 1] == "=":
            name, i = ts[i], i + 2
            if i < len(ts) and ts[i] == "{":
                result[name], i = table(ts, i)
            else:
                end = i
                while end < len(ts) and ts[end] not in {",", ";"}:
                    end += 1
                result[name], i = ts[i:end], end + 1
        else:
            i += 1
    return result


def extract(source):
    ts, result = tokens(source), {}
    for i in range(len(ts) - 4):
        if ts[i:i + 2] != ["AddRoom", "("] or ts[i + 3:i + 5] != [",", "{"]:
            continue
        name = ts[i + 2].strip("\"'")
        body, _ = table(ts, i + 4)
        contents = fields(fields(body).get("contents", []))
        prefabs = set()
        for kind in ("distributeprefabs", "countprefabs"):
            for prefab, value in fields(contents.get(kind, [])).items():
                if len(value) == 1 and re.fullmatch(r"\d+(?:\.\d*)?|\.\d+", value[0]):
                    if float(value[0]) > 0:
                        prefabs.add(prefab)
                elif "weight" in value:
                    weighted = fields(value)
                    weight = weighted.get("weight", ["0"])
                    if len(weight) == 1 and float(weight[0]) > 0:
                        prefabs.update(t.strip("\"'") for t in weighted.get("prefabs", [])
                                       if t.startswith(('"', "'")))
        result[name] = sorted(prefabs)
    return result


def build(archive):
    rooms, sources = {}, {}
    with zipfile.ZipFile(archive) as bundle:
        for name in sorted(bundle.namelist()):
            if not name.startswith("scripts/map/rooms/forest/") or not name.endswith(".lua"):
                continue
            data = bundle.read(name)
            sources[name] = hashlib.sha256(data).hexdigest()
            rooms.update(extract(data.decode("utf-8-sig")))
    return {"schema": 1, "meaning": "possible initial prefabs, NOT guarantees or probabilities; layouts/functions/settings omitted",
            "source_sha256": sources, "rooms": dict(sorted(rooms.items()))}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parents[1] / "data" / "worldgen_resources.json")
    args = parser.parse_args()
    snapshot = build(args.archive)
    args.output.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"worldgen_priors rooms={len(snapshot['rooms'])} sources={len(snapshot['source_sha256'])}")
