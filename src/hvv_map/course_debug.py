"""CLI: departureCourse for one live serviceId, e.g. a combined-train entry
that has no plan counterpart. Station, time and line come from the cached
live snapshot (hvv:departures:live)."""

import json
import sys
import time

from hvv_map.course_log import KIND_MANUAL, call_entry, log_calls
from hvv_map.departures_live import REDIS_LIVE_KEY
from hvv_map.gti_client import GtiClient
from hvv_map.redis_client import get_redis_client
from hvv_map.trip_courses import course_request, parse_course


def format_course(stops: list[dict]) -> str:
    lines = [f"{'Station':32} {'an':>5} {'ab':>5}  Verspätung  Gleis"]
    for stop in stops:
        delay = (
            stop["dep_delay"] if stop["dep_delay"] is not None else stop["arr_delay"]
        )
        lines.append(
            f"{stop['name'] or '?':32} {(stop['arr'] or '')[11:16]:>5} "
            f"{(stop['dep'] or '')[11:16]:>5}  {'' if delay is None else delay:>10}  "
            f"{stop['platform'] or ''}"
        )
    return "\n".join(lines)


def format_tracks(response: dict) -> str:
    """Per course element the number of path points GTI returned; elements
    without a track are drawn as straight lines."""
    lines = []
    for element in response.get("courseElements") or []:
        points = len((element.get("path") or {}).get("track") or [])
        origin = (element.get("fromStation") or {}).get("name")
        target = (element.get("toStation") or {}).get("name")
        lines.append(
            f"{origin} -> {target}: "
            + (f"{points} Punkte" if points else "keine Geometrie (Luftlinie)")
        )
    return "\n".join(lines)


def main() -> None:
    """Usage: hvvmap-course <serviceId> [stationId] [--json] [--path]"""
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        raise SystemExit(main.__doc__)
    redis_client = get_redis_client()
    raw = redis_client.get(REDIS_LIVE_KEY)
    request = course_request(
        json.loads(raw)["data"], int(args[0]), args[1] if len(args) > 1 else None
    )
    if request is None:
        raise SystemExit(f"serviceId {args[0]} not in the live snapshot")
    request["showPath"] = "--path" in sys.argv
    started = time.time()
    response = GtiClient().send("departureCourse", request)
    stops = parse_course(response)
    log_calls(
        redis_client,
        [
            call_entry(
                KIND_MANUAL,
                request,
                "ok" if stops else "no course",
                started,
                stops=len(stops or []),
            )
        ],
    )
    if "--path" in sys.argv:
        print(format_tracks(response))
        return
    if "--json" in sys.argv:
        print(json.dumps(response, ensure_ascii=False, indent=1))
        return
    print(f"{request['station']['name']} {request['time']} {request['lineId']}")
    print(format_course(stops) if stops else f"no course: {response}")
