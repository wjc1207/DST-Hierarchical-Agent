"""Run the archived client-only player regression suite cleanly without active python/."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path


ARCHIVE_PYTHON_ROOT = Path(__file__).resolve().parent
TESTS_ROOT = ARCHIVE_PYTHON_ROOT / "tests"
sys.path.insert(0, str(ARCHIVE_PYTHON_ROOT))
sys.path.insert(0, str(TESTS_ROOT))


def main() -> int:
    suite = unittest.defaultTestLoader.discover(str(TESTS_ROOT), pattern="test_*.py")
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
