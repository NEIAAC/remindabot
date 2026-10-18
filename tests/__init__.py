"""The test suite. Run it from the repository root:

    python3 -m unittest            # everything that needs no Docker (about 3 minutes)
    python3 -m unittest tests.test_commands

Importing this package puts `src/` on the import path, so the tests run against the checkout
without installing it. `tests/e2e` needs the dev stack (`docker compose --profile test run --rm e2e`).
"""
import sys
from pathlib import Path

SRC = str(Path(__file__).resolve().parent.parent / "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)
