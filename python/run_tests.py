"""Run the offline DST regression suite without launching the game."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path


PYTHON_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PYTHON_ROOT))


def main() -> int:
    suite = unittest.defaultTestLoader.discover(str(PYTHON_ROOT / "tests"), pattern="test_*.py")
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
