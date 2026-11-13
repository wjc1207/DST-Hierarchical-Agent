"""Stable paths independent of package locations and launch directory."""
from pathlib import Path

PYTHON_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PYTHON_ROOT.parent
DATA_DIR = PROJECT_ROOT / "data"
LOG_DIR = PROJECT_ROOT / "logs"
