"""HADAG ferry positions from the aisstream.io WebSocket, for comparison
with ais.py (Open Waters).

Needs AISSTREAM_KEY; without it nothing is fetched. The stream sends a
message per AIS position report; the latest one per vessel is stored as
GeoJSON under hvv:ais:aisstream, in the same shape as ais.py.
"""

import json
import os
import random
import time
import traceback
from datetime import datetime, timezone

from websockets.sync.client import connect

from hvv_map.ais import HADAG, build_geojson
from hvv_map.redis_client import get_redis_client

AISSTREAM_URL = "wss://stream.aisstream.io/v0/stream"
REDIS_AISSTREAM_KEY = "hvv:ais:aisstream"
# Lower Elbe from Wedel to the harbour, as [lat, lon] corners.
BOUNDING_BOX = [[53.40, 9.60], [53.65, 10.15]]
WRITE_INTERVAL = 15.0  # seconds between Redis writes
MAX_AGE_S = 30 * 60  # positions older than this are dropped
AISSTREAM_TTL = 600
RECONNECT_MAX_S = 300.0
# The handshake often takes longer than websockets' default of 10 s.
OPEN_TIMEOUT_S = 60


def subscription(key: str, fleet=HADAG) -> dict:
    return {
        "APIKey": key,
        "BoundingBoxes": [BOUNDING_BOX],
        "FiltersShipMMSI": [str(m) for m in fleet],
        "FilterMessageTypes": ["PositionReport"],
    }


def _time(meta: dict, received: datetime) -> datetime:
    """MetaData.time_utc ("2026-11-20 16:12:03.123 +0000 UTC"), else the
    time the message came in."""
    try:
        return datetime.strptime(meta["time_utc"][:19], "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=timezone.utc
        )
    except (KeyError, TypeError, ValueError):
        return received


def to_feature(message: dict, received: datetime) -> dict | None:
    """The position report as a feature like ais.fetch_vessels returns, or
    None for other messages and invalid positions."""
    report = message.get("Message", {}).get("PositionReport")
    if message.get("MessageType") != "PositionReport" or not report:
        return None
    meta = message.get("MetaData", {})
    lat, lon = report.get("Latitude"), report.get("Longitude")
    if lat is None or lon is None or not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    seen = _time(meta, received)
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [lon, lat]},
        "properties": {
            "mmsi": meta.get("MMSI") or report.get("UserID"),
            "sog": report.get("Sog"),
            "cog": report.get("Cog"),
            "heading": report.get("TrueHeading"),
            "nav_status": report.get("NavigationalStatus"),
            "seen": seen.strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
    }


def current(latest: dict[int, dict], now: datetime) -> dict:
    """GeoJSON of the latest positions not older than MAX_AGE_S."""
    fresh = [
        f
        for f in latest.values()
        if (
            now
            - datetime.strptime(f["properties"]["seen"], "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=timezone.utc
            )
        ).total_seconds()
        <= MAX_AGE_S
    ]
    return build_geojson(fresh)


def _write(redis_client, latest: dict[int, dict]) -> None:
    data = current(latest, datetime.now(timezone.utc))
    redis_client.set(
        REDIS_AISSTREAM_KEY,
        json.dumps({"fetched_at": int(time.time()), "data": data}),
        ex=AISSTREAM_TTL,
    )


def _stream(key: str, redis_client, latest: dict[int, dict]) -> None:
    """One connection: subscribe, then read until it fails."""
    with connect(AISSTREAM_URL, open_timeout=OPEN_TIMEOUT_S) as ws:
        ws.send(json.dumps(subscription(key)))
        last_write = 0.0
        while True:
            try:
                raw = ws.recv(timeout=WRITE_INTERVAL)
            except TimeoutError:
                raw = None
            if raw is not None:
                text = raw.decode() if isinstance(raw, bytes) else raw
                message = json.loads(text)
                if "error" in message:
                    raise RuntimeError(message["error"])
                feature = to_feature(message, datetime.now(timezone.utc))
                if feature and feature["properties"]["mmsi"] in HADAG:
                    latest[feature["properties"]["mmsi"]] = feature
            if time.time() - last_write >= WRITE_INTERVAL:
                _write(redis_client, latest)
                last_write = time.time()


def run_loop() -> None:
    """Endless stream in its own thread; ends at once without AISSTREAM_KEY.
    Reconnects with growing, jittered waits (the service has no replay)."""
    key = os.environ.get("AISSTREAM_KEY", "").strip()
    if not key:
        print("[hvvmap-aisstream] AISSTREAM_KEY is not set: no stream", flush=True)
        return
    redis_client = get_redis_client()
    latest: dict[int, dict] = {}
    wait = 5.0
    while True:
        started = time.time()
        try:
            _stream(key, redis_client, latest)
        except Exception:  # keep the loop alive across network or data errors
            print(f"[hvvmap-aisstream] {traceback.format_exc()}", flush=True)
        # a connection that lasted resets the wait
        wait = (
            5.0
            if time.time() - started > RECONNECT_MAX_S
            else min(wait * 2, RECONNECT_MAX_S)
        )
        time.sleep(wait * random.uniform(0.5, 1.0))


def main() -> None:
    """CLI: stream for a minute and print what arrived."""
    key = os.environ.get("AISSTREAM_KEY", "").strip()
    if not key:
        raise SystemExit("AISSTREAM_KEY is not set")
    with connect(AISSTREAM_URL, open_timeout=OPEN_TIMEOUT_S) as ws:
        ws.send(json.dumps(subscription(key)))
        end = time.time() + 60
        while time.time() < end:
            try:
                raw = ws.recv(timeout=max(0.1, end - time.time()))
            except TimeoutError:
                break
            message = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
            feature = to_feature(message, datetime.now(timezone.utc))
            if feature:
                p = feature["properties"]
                print(
                    p["seen"],
                    p["mmsi"],
                    HADAG.get(p["mmsi"], "?"),
                    p["sog"],
                    flush=True,
                )
