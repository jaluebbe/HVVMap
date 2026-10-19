"""CLI: compare the planned after-midnight trips of several nights.

GTI answers departureList for past dates too, so the nights can be queried
side by side and against the plan cached in Redis. Shows which trips a night
has that another night lacks - e.g. whether the cached plan carries the
Fri/Sat night pattern into the night to Monday.
"""

import json
import sys
import time
from datetime import date, datetime, time as dtime, timedelta

from hvv_map.departures import (
    REDIS_ANCHOR_KEY,
    REDIS_BY_SERVICE_KEY,
    _first_trip,
    fetch_main_departures,
    fetch_r_departures,
    fetch_sev_departures,
    group_by_service,
    merge_departures,
)
from hvv_map.gti_client import GtiClient
from hvv_map.redis_client import get_redis_client
from hvv_map.trips import berlin_now

QUERY_START_HOUR = 23  # evening before the night; trips are seen from their start
QUERY_MINUTES = 5 * 60  # up to 04:00 the next morning
NIGHT_END_HOUR = 4


def night_window(evening: date) -> tuple[datetime, datetime]:
    """[00:00, 04:00) of the day after the given evening."""
    midnight = datetime.combine(evening + timedelta(days=1), dtime.min)
    return midnight, midnight + timedelta(hours=NIGHT_END_HOUR)


def night_trips(
    by_service: dict, anchor: datetime, window: tuple[datetime, datetime]
) -> dict[tuple, dict]:
    """Trips whose first stop lies in the window, keyed by
    (line, first station, HH:MM)."""
    trips = {}
    for entry in by_service.values():
        first = _first_trip(entry["stops"])[0]
        start = anchor + timedelta(minutes=first["timeOffset"])
        if not window[0] <= start < window[1]:
            continue
        key = (
            entry["line"].get("name"),
            (first.get("station") or {}).get("name"),
            start.strftime("%H:%M"),
        )
        trips[key] = {"destination": entry.get("destination")}
    return trips


def fetch_night(client: GtiClient, redis_client, evening: date) -> dict[tuple, dict]:
    """Fresh plan of the night after the evening date."""
    anchor = datetime.combine(evening, dtime(QUERY_START_HOUR))
    responses = []
    for fetch in (
        lambda: fetch_main_departures(client, anchor, QUERY_MINUTES),
        lambda: fetch_r_departures(client, anchor, QUERY_MINUTES),
        lambda: fetch_sev_departures(client, redis_client, anchor, QUERY_MINUTES),
    ):
        responses.append(fetch() or {})
        time.sleep(1)  # 1 req/s rate limit across all GTI endpoints
    by_service = group_by_service(merge_departures(*responses))
    return night_trips(by_service, anchor, night_window(evening))


def cached_night(redis_client) -> tuple[date, dict[tuple, dict]] | None:
    """The night after the cached plan's anchor day, as the app sees it."""
    anchor_raw = redis_client.get(REDIS_ANCHOR_KEY)
    by_service_raw = redis_client.get(REDIS_BY_SERVICE_KEY)
    if not anchor_raw or not by_service_raw:
        return None
    anchor_data = json.loads(anchor_raw)["data"]
    anchor = datetime.fromisoformat(f"{anchor_data['date']}T{anchor_data['time']}")
    # The anchor is 04:00, so the night belongs to the evening of the same day.
    evening = anchor.date()
    trips = night_trips(
        json.loads(by_service_raw)["data"], anchor, night_window(evening)
    )
    return evening, trips


def compare(nights: dict[str, dict[tuple, dict]]) -> str:
    """Per-night trip counts per line and the trips unique to a night."""
    lines = sorted({key[0] for trips in nights.values() for key in trips if key[0]})
    labels = list(nights)
    out = [f"{'Linie':10}" + "".join(f"{label:>16}" for label in labels)]
    for line in lines:
        counts = [sum(1 for k in nights[label] if k[0] == line) for label in labels]
        out.append(f"{line:10}" + "".join(f"{count:>16}" for count in counts))
    for label in labels:
        others = set().union(*(set(t) for lb, t in nights.items() if lb != label))
        only = sorted(set(nights[label]) - others, key=lambda k: (k[0] or "", k[2]))
        out.append(f"\nnur in {label}: {len(only)}")
        out.extend(
            f"  {k[0]:8} {k[2]} {k[1]} -> {nights[label][k]['destination']}"
            for k in only
        )
    return "\n".join(out)


def main() -> None:
    """Usage: hvvmap-plan-compare [YYYY-MM-DD ...] [--no-cache]

    Each date is the evening before a night (default: the last three evenings).
    The cached plan of the app is added unless --no-cache is given."""
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    today = berlin_now().date()
    evenings = (
        [date.fromisoformat(a) for a in args]
        if args
        else [today - timedelta(days=n) for n in (3, 2, 1)]
    )
    redis_client = get_redis_client()
    client = GtiClient()
    nights = {}
    for evening in evenings:
        nights[f"{evening:%a %d.%m.}"] = fetch_night(client, redis_client, evening)
    if "--no-cache" not in sys.argv:
        cached = cached_night(redis_client)
        if cached:
            nights[f"Cache {cached[0]:%d.%m.}"] = cached[1]
    print(compare(nights))


if __name__ == "__main__":
    main()
