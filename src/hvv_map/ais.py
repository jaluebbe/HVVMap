"""HADAG ferry positions from the Open Waters AIS API, queried by MMSI.

A personal token (AIS_TOKEN, openwaters.io/ais/token) allows 50 MMSIs per
request. Without a token nothing is fetched. The GeoJSON is stored under
hvv:ais:hadag; it is independent of the GTI data.
"""

import json
import os
import time
import traceback

import requests

from hvv_map.redis_client import get_redis_client

AIS_URL = "https://ais.openwaters.io/v1/vessels"
REDIS_AIS_KEY = "hvv:ais:hadag"
AIS_INTERVAL = 30.0  # seconds between requests
AIS_MAX_AGE = "30m"  # positions older than this are not returned
AIS_TTL = 600  # seconds the stored positions stay valid
ERROR_LOG_INTERVAL = 600.0

# The 28 vessels of hadag.de/de/unsere-flotte.
HADAG = {
    211437250: "KIRCHDORF",
    211437160: "ALTONA",
    211437190: "NEUENFELDE",
    211437240: "BLANKENESE",
    211437280: "FALKENSTEIN",
    211437330: "WOLFGANG BORCHERT",
    211706910: "ST NIKOLAUS",
    211437180: "NALA",
    211437310: "RAFIKI",
    211643790: "REIHERSTIEG",
    211263850: "ALTENWERDER",
    211257820: "ST.PAULI",
    211437270: "HAFENCITY",
    211316150: "REEPERBAHN",
    211437470: "HARMONIE",
    211437320: "WALTERSHOF",
    211437290: "ELBMEILE",
    211151080: "OEVELGOENNE",
    211437340: "TOLLERORT",
    211462080: "OORTKATEN",
    211437770: "WILHELMSBURG",
    211472760: "HARBURG",
    211611680: "HAMBURGENSIE",
    211261270: "KEHRWIEDER",
    211762670: "ELBPHILHARMONIE",
    218038620: "NEULAND",
    218049110: "FINKENWERDER",
    218055750: "GRASBROOK",
}

NAV_STATUS = {
    0: "unterwegs (Maschine)",
    1: "vor Anker",
    2: "manövrierunfähig",
    3: "manövrierbehindert",
    4: "durch Tiefgang behindert",
    5: "festgemacht",
    6: "aufgelaufen",
    7: "Fischerei",
    8: "unterwegs (Segel)",
}
MOORED_STATUSES = {1, 5}
MOVING_KNOTS = 0.5  # below this a vessel counts as lying still


def fetch_vessels(token: str, mmsis, max_age: str = AIS_MAX_AGE) -> list[dict]:
    """GeoJSON features of /v1/vessels for up to 50 MMSIs."""
    response = requests.get(
        AIS_URL,
        params={"mmsi": ",".join(map(str, mmsis)), "max_age": max_age},
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    response.raise_for_status()
    return response.json()["features"]


def _state(properties: dict) -> str:
    """"moored" or "underway": the crew's nav status, else the speed."""
    if properties.get("nav_status") in MOORED_STATUSES:
        return "moored"
    if (properties.get("sog") or 0) < MOVING_KNOTS:
        return "moored"
    return "underway"


def _angle(value) -> float | None:
    # AIS marks "not available" with 360 (cog) or 511 (heading).
    return value if value is not None and 0 <= value < 360 else None


def _rotation(state: str, heading: float | None, course: float | None) -> float:
    """Degrees the icon points to: the course of a moving vessel (the hull
    may crab), else the heading."""
    order = (course, heading) if state == "underway" else (heading, course)
    return next((angle for angle in order if angle is not None), 0)


def build_geojson(features: list[dict], fleet: dict[int, str] = HADAG) -> dict:
    """Display features of the fleet's vessels; others are dropped."""
    result = []
    for feature in features:
        props = feature.get("properties", {})
        mmsi = props.get("mmsi")
        if mmsi not in fleet:
            continue
        heading = _angle(props.get("heading"))
        course = _angle(props.get("cog"))
        state = _state(props)
        destination = (props.get("destination") or "").replace("@", "").strip()
        result.append(
            {
                "type": "Feature",
                "geometry": feature["geometry"],
                "properties": {
                    "mmsi": mmsi,
                    "name": fleet[mmsi],
                    "state": state,
                    "rotation": _rotation(state, heading, course),
                    "sog": props.get("sog"),
                    "cog": course,
                    "heading": heading,
                    "nav_status": NAV_STATUS.get(props.get("nav_status"), ""),
                    "destination": destination,
                    "length": props.get("length"),
                    "seen": props.get("seen"),
                },
            }
        )
    return {"type": "FeatureCollection", "features": result}


def run_once(redis_client, token: str) -> int:
    """One request for the whole fleet; stored for AIS_TTL. Returns the count."""
    data = build_geojson(fetch_vessels(token, HADAG))
    redis_client.set(
        REDIS_AIS_KEY,
        json.dumps({"fetched_at": int(time.time()), "data": data}),
        ex=AIS_TTL,
    )
    return len(data["features"])


def run_loop(interval: float = AIS_INTERVAL) -> None:
    """Endless polling in its own thread; ends at once without AIS_TOKEN."""
    token = os.environ.get("AIS_TOKEN", "").strip()
    if not token:
        print("[hvvmap-ais] AIS_TOKEN is not set: no AIS positions", flush=True)
        return
    redis_client = get_redis_client()
    last_error = ("", 0.0)
    while True:
        started = time.time()
        try:
            run_once(redis_client, token)
        except Exception:  # keep the loop alive across network or data errors
            message = traceback.format_exc()
            if (
                message != last_error[0]
                or started - last_error[1] >= ERROR_LOG_INTERVAL
            ):
                print(f"[hvvmap-ais] {message}", flush=True)
                last_error = (message, started)
        time.sleep(max(0.0, interval - (time.time() - started)))


def main() -> None:
    """CLI: one request, prints the number of vessels."""
    token = os.environ.get("AIS_TOKEN", "").strip()
    if not token:
        raise SystemExit("AIS_TOKEN is not set")
    print(f"{run_once(get_redis_client(), token)} of {len(HADAG)} ferries")
