import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from hvv_map.response_cache import CachedJsonResponse


@pytest.fixture
def served():
    calls = []
    payload = {"n": 1}

    def build():
        calls.append(1)
        return dict(payload)

    cache = CachedJsonResponse(build, ttl=3600)
    app = FastAPI()

    @app.get("/x")
    def endpoint(request: Request):
        return cache.response(request)

    return TestClient(app), calls, payload, cache


def test_body_is_built_once_within_ttl(served):
    client, calls, _, _ = served
    client.get("/x")
    client.get("/x")
    assert len(calls) == 1


def test_gzip_and_plain_bodies_match(served):
    client, _, _, _ = served
    plain = client.get("/x", headers={"Accept-Encoding": "identity"})
    packed = client.get("/x", headers={"Accept-Encoding": "gzip"})
    assert "content-encoding" not in plain.headers
    assert packed.headers["content-encoding"] == "gzip"
    assert packed.json() == plain.json() == {"n": 1}  # client decodes gzip


def test_matching_etag_returns_304(served):
    client, _, _, _ = served
    etag = client.get("/x").headers["etag"]
    response = client.get("/x", headers={"If-None-Match": etag})
    assert response.status_code == 304
    assert response.content == b""


def test_expired_entry_is_rebuilt_and_etag_follows_content(served):
    client, calls, payload, cache = served
    first = client.get("/x").headers["etag"]
    payload["n"] = 2
    cache._built_at = float("-inf")
    second = client.get("/x")
    assert len(calls) == 2
    assert second.json() == {"n": 2}
    assert second.headers["etag"] != first


def test_failed_rebuild_is_retried(served):
    client, calls, _, cache = served
    attempts = []

    def flaky():
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("boom")
        return {"ok": True}

    cache._build = flaky
    with pytest.raises(RuntimeError):
        client.get("/x")
    assert client.get("/x").json() == {"ok": True}
