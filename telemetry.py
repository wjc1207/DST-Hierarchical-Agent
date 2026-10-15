"""Reassemble complete DST telemetry records from bounded client-log lines."""

from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path
from threading import Lock
from typing import Optional, Tuple


STATE_PREFIX = "[JEV_DST_STATE]"
CHUNK_PREFIX = "[JEV_DST_CHUNK]"
MAX_TAIL_BYTES = 2_000_000


class StateAssembler:
    def __init__(self) -> None:
        self.pending: dict[str, dict] = {}

    def feed(self, line: str) -> Optional[dict]:
        marker = line.find(STATE_PREFIX)
        if marker >= 0:
            try:
                value = json.loads(line[marker + len(STATE_PREFIX):].strip())
            except json.JSONDecodeError:
                return None
            return value if isinstance(value, dict) else None

        marker = line.find(CHUNK_PREFIX)
        if marker < 0:
            return None
        # DST appends a tab to each printed log line, including every chunk.
        fields = line[marker + len(CHUNK_PREFIX):].rstrip("\t\r\n").split(":", 3)
        if len(fields) != 4:
            return None
        frame_id, index_text, total_text, fragment = fields
        try:
            index = int(index_text)
            total = int(total_text)
        except ValueError:
            return None
        if not frame_id or not 1 <= index <= total <= 1000:
            return None
        if index == 1:
            if len(self.pending) >= 32:
                self.pending.pop(next(iter(self.pending)))
            self.pending[frame_id] = {"total": total, "parts": {}}
        frame = self.pending.get(frame_id)
        if frame is None or frame["total"] != total:
            return None
        frame["parts"][index] = fragment
        if len(frame["parts"]) != total:
            return None
        del self.pending[frame_id]
        try:
            value = json.loads("".join(frame["parts"][part] for part in range(1, total + 1)))
        except (KeyError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None


class StateReader:
    """Tail incrementally, retaining partial lines and chunked frames between polls."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = Lock()
        self.identity = None
        self.offset = 0
        self.anchor = b""
        self.buffer = b""
        self.assembler = StateAssembler()
        self.latest: Optional[dict] = None
        self.latest_offset = 0

    def read(self) -> Tuple[dict, int]:
        with self.lock:
            try:
                with self.path.open("rb") as stream:
                    metadata = os.fstat(stream.fileno())
                    identity = (metadata.st_dev, metadata.st_ino)
                    reset = (identity != self.identity or metadata.st_size < self.offset
                             or metadata.st_size - self.offset > MAX_TAIL_BYTES)
                    if not reset and self.anchor:
                        # Detect a log truncated and regrown between polls, even
                        # if the new file is already larger than the old cursor.
                        stream.seek(self.offset - len(self.anchor))
                        reset = stream.read(len(self.anchor)) != self.anchor
                    if reset:
                        self.identity = identity
                        self.offset = max(0, metadata.st_size - MAX_TAIL_BYTES)
                        self.buffer = b""
                        self.assembler = StateAssembler()
                        self.latest = None
                        self.latest_offset = 0
                        stream.seek(self.offset)
                        if self.offset:
                            stream.readline()  # discard the first partial line
                            self.offset = stream.tell()
                    stream.seek(self.offset)
                    self.buffer += stream.read()
                    self.offset = stream.tell()
                    stream.seek(max(0, self.offset - 64))
                    self.anchor = stream.read(self.offset - stream.tell())
            except FileNotFoundError as exc:
                raise RuntimeError(f"DST log does not exist: {self.path}") from exc

            line_offset = self.offset - len(self.buffer)
            lines = self.buffer.split(b"\n")
            self.buffer = lines.pop()
            for raw_line in lines:
                line_offset += len(raw_line) + 1
                state = self.assembler.feed(raw_line.decode("utf-8", errors="replace"))
                if state is not None:
                    self.latest = state
                    self.latest_offset = line_offset
            if self.latest is None:
                raise RuntimeError("No complete JEV telemetry record found; install the updated mod")
            return self.latest, self.latest_offset


@lru_cache(maxsize=8)
def _reader(path: str) -> StateReader:
    return StateReader(Path(path))


def latest_state(log_path: Path) -> Tuple[dict, int]:
    """Return the latest complete JSON record and its final byte offset."""
    return _reader(os.path.abspath(log_path)).read()
