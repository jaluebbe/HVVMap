"""Isolates tests from production data on a shared Redis instance.

Must run before any test module imports hvv_map code that calls
get_redis_client() at import time (e.g. fetcher.py's module-level
redis_client). setdefault() still lets REDIS_DB be overridden explicitly
(e.g. in CI), it just keeps the accidental case (developer running pytest
against the same Redis as their running fetcher) from overwriting real
data with test fixtures.
"""

import os

os.environ.setdefault("REDIS_DB", "15")


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _unit_positions_db(tmp_path, monkeypatch):
    """Keeps the unit positions database of the positions loop out of the
    working directory."""
    from hvv_map import unit_positions

    monkeypatch.setattr(
        unit_positions, "DEFAULT_DB_PATH", str(tmp_path / "unit_positions.db")
    )
