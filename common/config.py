"""Model configuration loading. Credentials stay outside Lua and source control."""
import os
from pathlib import Path
from typing import Optional

from .paths import PROJECT_ROOT

DEFAULT_INSTALLED_ENV = Path(
    r"C:\Program Files (x86)\Steam\steamapps\common\Don't Starve Together\mods\jev_dst_agent\.env"
)


def load_dotenv(path: Path) -> None:
    if not path.exists():
        raise RuntimeError(f".env file was not found: {path}")
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        name, value = name.strip(), value.strip()
        if value and value[0:1] == value[-1:] and value.startswith(("'", '"')):
            value = value[1:-1]
        if name:
            os.environ.setdefault(name, value)


def resolve_env_path(explicit: Optional[Path]) -> Path:
    candidates = ([explicit] if explicit is not None else []) + [
        PROJECT_ROOT / ".env", Path.cwd() / ".env", DEFAULT_INSTALLED_ENV,
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    raise RuntimeError("No .env file found. Put it in the project root or pass --env PATH.")
