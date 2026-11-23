"""Loopback bridge token management and Workshop in-game pairing command helpers."""
from __future__ import annotations

import json
import secrets
from pathlib import Path


def ensure_bridge_token(config_path: Path) -> tuple[str, bool]:
    """Load an existing local bridge token or generate and persist a new one.

    Returns (token, created).
    """
    if config_path.exists():
        data = json.loads(config_path.read_text(encoding="utf-8-sig"))
        token = data["token"]
        if not isinstance(token, str) or len(token) < 32:
            raise ValueError(f"Invalid bridge token in {config_path}")
        return token, False
    token = secrets.token_hex(32)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps({"token": token}), encoding="utf-8")
    return token, True


def format_pair_command(token: str, port: int, *, prefix: str, default_port: int) -> str:
    """Format the local-only in-game DST chat pairing command."""
    if port == default_port:
        return f"{prefix} pair {token}"
    return f"{prefix} pair {token} {port}"
