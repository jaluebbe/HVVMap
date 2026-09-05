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
