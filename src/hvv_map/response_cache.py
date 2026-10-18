"""Short-lived cache of serialized JSON responses for expensive endpoints.

The body is built once per TTL, stored raw and gzipped, and served with an
ETag so polling clients get a 304 instead of the full payload.
"""

import gzip
import hashlib
import json
import threading
import time
from collections.abc import Callable

from fastapi import Request, Response

DEFAULT_TTL = 60.0  # seconds


class CachedJsonResponse:
    def __init__(self, build: Callable[[], object], ttl: float = DEFAULT_TTL):
        self._build = build
        self._ttl = ttl
        self._lock = threading.Lock()
        self._built_at = float("-inf")
        self._body = b""
        self._gzipped = b""
        self._etag = ""

    def _refresh(self) -> None:
        """Rebuild if expired; a failed build leaves the previous entry."""
        with self._lock:
            if time.monotonic() - self._built_at < self._ttl:
                return
            body = json.dumps(self._build(), separators=(",", ":")).encode()
            etag = f'"{hashlib.sha1(body).hexdigest()}"'
            if etag != self._etag:
                self._gzipped = gzip.compress(body, compresslevel=6)
                self._body, self._etag = body, etag
            self._built_at = time.monotonic()

    def response(self, request: Request) -> Response:
        self._refresh()
        headers = {"ETag": self._etag, "Cache-Control": "no-cache"}
        if request.headers.get("if-none-match") == self._etag:
            return Response(status_code=304, headers=headers)
        if "gzip" in request.headers.get("accept-encoding", ""):
            headers["Content-Encoding"] = "gzip"
            body = self._gzipped
        else:
            body = self._body
        headers["Vary"] = "Accept-Encoding"
        return Response(body, media_type="application/json", headers=headers)
