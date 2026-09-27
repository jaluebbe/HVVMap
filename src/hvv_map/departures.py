"""One-off 24h departureList snapshots per anchor-station set, cached raw in
Redis for later analysis (service ID collection, SEV detection), plus a
deduplicated merge (hvv:departures:merged) and a per-serviceId grouping
(hvv:departures:by_service) built from it. Not part of the continuous
fetcher loop - run manually or on its own schedule.
"""

import json
import time
from datetime import datetime, timedelta

from hvv_map.gti_client import GtiClient
from hvv_map.lines import LINE_NAME_PATTERN, WANTED_R_BAHN_LINES, load_sublines_cache
from hvv_map.redis_client import get_redis_client

REDIS_TTL = 2 * 24 * 60 * 60  # 2 days - headroom to analyze before the next run

# Combined-destination names (e.g. "Schlump - Barmbek") aren't real stations -
# listLines emits them as synthetic subline endpoints only, so departureList
# rejects them.
PSEUDO_STATIONS = {
    "Hauptbahnhof Süd - Barmbek",
    "Hauptbahnhof Süd - Schlump",
    "Schlump - Barmbek",
    "Großhansdorf/Ohlstedt",
    "Schlump - Hauptbahnhof Süd",
}

# Stations where U/S/AKN/ferry lines start or terminate - covers every
# service without querying every single stop.
ANCHOR_STATIONS_MAIN = {
    "Norderstedt Mitte", "Neumünster", "Elmshorn", "Ulzburg Süd", "Barmstedt",
    "Ochsenzoll", "Fuhlsbüttel Nord", "Quickborner Straße", "Kaltenkirchen",
    "Eidelstedt",
    "Berliner Tor", "HafenCity Universität", "Teufelsbrück",
    "Landungsbrücken Brücke 1", "Großhansdorf", "Diebsteich", "Finkenwerder",
    "Altona", "Elbbrücken", "Othmarschen", "Billstedt", "Bad Oldesloe",
    "Poppenbüttel", "Stade", "Buxtehude", "Lattenkamp",
    "Landungsbrücken Brücke 3", "Ernst-August-Schleuse",
    "Landungsbrücken Brücke 2", "Pinneberg", "Niendorf Nord", "Burgstraße",
    "Niendorf Markt", "Wandsbek-Gartenstadt", "Harburg Rathaus",
    "Hauptbahnhof Süd", "Farmsen", "AIRBUS", "Volksdorf", "Steinwerder",
    "Hagenbecks Tierpark", "Barmbek", "Schlump", "Hamburg Hbf",
    "Jungfernstieg", "Neugraben", "Neuhof", "Kellinghusenstraße",
    "Mümmelmannsberg", "Landungsbrücken", "Elbgaustraße",
    "Lattenkamp (Sporthalle)", "Ohlstedt", "Bergedorf",
    "Hamburg Airport (Flughafen)", "Elbphilharmonie", "Wedel", "Blankenese",
    "Aumühle", "Stephansplatz (Oper/CCH)", "Ohlsdorf", "Wandsbek Markt",
    "Saarlandstraße", "dodenhof",
} - PSEUDO_STATIONS  # fmt: skip
MAIN_SERVICE_TYPES = ["SBAHN", "UBAHN", "AKN", "FAEHRE"]

# Terminal/hub and subline-disambiguating stations covering RB60/61/71/81.
ANCHOR_STATIONS_RB = {
    "Elmshorn", "Ahrensburg", "Wrist", "Itzehoe", "Bad Oldesloe",
    "Hamburg-Altona", "Hamburg Hbf", "Lübeck Hbf", "Bargteheide",
    "Hamburg Dammtor", "Hasselbrook", "Tonndorf", "Glückstadt",
}  # fmt: skip
RB_SERVICE_TYPES = ["RBAHN"]

SEV_SERVICE_TYPES = ["SBAHN", "UBAHN", "AKN", "FAEHRE", "BUS"]
REDIS_ANNOUNCEMENT_STATIONS_KEY = "hvv:announcement_stations"

REDIS_MAIN_KEY = "hvv:departures:main"
REDIS_RB_KEY = "hvv:departures:rb"
REDIS_SEV_KEY = "hvv:departures:sev"
REDIS_MERGED_KEY = "hvv:departures:merged"
REDIS_BY_SERVICE_KEY = "hvv:departures:by_service"


QUERY_ANCHOR_HOUR = 4  # most services first run around here


def _query_start_time(now: datetime) -> datetime:
    """Anchor at the most recent ~4:00 - most services first run around
    then, so this captures the full day's service_id set from one query."""
    anchor = now.replace(hour=QUERY_ANCHOR_HOUR, minute=0, second=0, microsecond=0)
    if anchor > now:
        anchor -= timedelta(days=1)
    return anchor


def _build_request(
    stations: set[str], service_types: list[str], start: datetime
) -> dict:
    return {
        "maxList": 25000,
        "maxTimeOffset": 1440,  # 24h from start, so it still covers the full day
        "stations": [{"type": "STATION", "name": name} for name in stations],
        "time": {
            "date": start.strftime("%Y-%m-%d"),
            "time": start.strftime("%H:%M:%S"),
        },
        "serviceTypes": service_types,
    }


def _store(redis_client, key: str, data: dict) -> None:
    payload = json.dumps({"fetched_at": int(time.time()), "data": data})
    redis_client.set(key, payload, ex=REDIS_TTL)


def _is_current(redis_client, key: str, start: datetime) -> bool:
    """True if the cached response under key already echoes our anchor time
    - the response's own "time" field uses a different format than the
    request ("19.09.2026"/"04:00" vs "2026-09-19"/"04:00:00")."""
    payload = redis_client.get(key)
    if not payload:
        return False
    cached_time = json.loads(payload).get("data", {}).get("time") or {}
    return cached_time.get("date") == start.strftime("%d.%m.%Y") and cached_time.get(
        "time"
    ) == start.strftime("%H:%M")


def fetch_main_departures(client: GtiClient, start: datetime) -> dict:
    """24h departureList across every U/S/AKN/ferry anchor station."""
    request = _build_request(ANCHOR_STATIONS_MAIN, MAIN_SERVICE_TYPES, start)
    return client.send("departureList", request)


def fetch_rb_departures(client: GtiClient, start: datetime) -> dict:
    """24h departureList across the RB anchor stations, filtered down to
    WANTED_R_BAHN_LINES - RBAHN alone also returns other DB regional trains
    passing through, same as getVehicleMap."""
    request = _build_request(ANCHOR_STATIONS_RB, RB_SERVICE_TYPES, start)
    response = client.send("departureList", request)
    response["departures"] = [
        d
        for d in response.get("departures", [])
        if d.get("line", {}).get("name") in WANTED_R_BAHN_LINES
    ]
    return response


def _is_wanted_departure(departure: dict) -> bool:
    """Same lines-of-interest rule as hvv_map.lines.is_line_of_interest, but
    departureList's line object has no carrierNameShort to identify
    ferries by - type.simpleType == "SHIP" is used instead."""
    line = departure.get("line") or {}
    if LINE_NAME_PATTERN.match(line.get("name", "")):
        return True
    line_type = line.get("type") or {}
    return line_type.get("simpleType") == "SHIP"


def fetch_sev_departures(
    client: GtiClient, redis_client, start: datetime
) -> dict | None:
    """24h departureList (incl. bus) across stations currently named in
    active announcements, filtered down to is_line_of_interest() - widens
    beyond the fixed anchors to catch replacement services without pulling
    in every other bus line passing through. None if no announcement
    stations are cached."""
    payload = redis_client.get(REDIS_ANNOUNCEMENT_STATIONS_KEY)
    station_names = json.loads(payload)["data"] if payload else []
    if not station_names:
        return None
    request = _build_request(set(station_names), SEV_SERVICE_TYPES, start)
    response = client.send("departureList", request)
    response["departures"] = [
        d for d in response.get("departures", []) if _is_wanted_departure(d)
    ]
    return response


def _load_data(redis_client, key: str) -> dict:
    payload = redis_client.get(key)
    return json.loads(payload)["data"] if payload else {}


def _departure_key(departure: dict) -> tuple:
    """(station id, serviceId) identifies one physical stop event - stable
    across our three queries since they all share the same anchor time."""
    station_id = (departure.get("station") or {}).get("id", "")
    return station_id, departure.get("serviceId")


def merge_departures(*responses: dict) -> list[dict]:
    """Combine departures from several departureList responses into one
    deduplicated list. The same stop event can show up in more than one
    snapshot when its station falls into more than one anchor set."""
    seen: dict[tuple, dict] = {}
    for response in responses:
        for departure in response.get("departures", []):
            seen.setdefault(_departure_key(departure), departure)
    return list(seen.values())


GAP_THRESHOLD_MINUTES = 120  # a same-serviceId gap this large means the next
# operating day's occurrence, not a pause within one trip


def _first_trip(stops: list[dict]) -> list[dict]:
    """The leading run of stops before any gap over GAP_THRESHOLD_MINUTES -
    a serviceId's next-day occurrence shows up as one such gap."""
    first = [stops[0]]
    for prev, cur in zip(stops, stops[1:]):
        if cur["timeOffset"] - prev["timeOffset"] > GAP_THRESHOLD_MINUTES:
            break
        first.append(cur)
    return first


def group_by_service(departures: list[dict]) -> dict[str, dict]:
    """Group departures by serviceId into {line, directionId, destination,
    stops}. line (minus direction) and directionId are constant per
    serviceId; each stop keeps its own direction, since it can change
    mid-trip (e.g. the U3 ring line). destination is the last stop's
    direction within the first trip (see _first_trip), so a short turn or
    a later day's occurrence of the same serviceId doesn't leak in. stops
    stays the full, unsplit per-service timeline, sorted by timeOffset."""
    grouped: dict[str, dict] = {}
    for departure in departures:
        service_id = departure.get("serviceId")
        if not service_id:
            continue
        line = departure.get("line") or {}
        entry = grouped.setdefault(
            str(service_id),
            {
                "line": {k: v for k, v in line.items() if k != "direction"},
                "directionId": departure.get("directionId"),
                "stops": [],
            },
        )
        entry["stops"].append(
            {
                "timeOffset": departure.get("timeOffset"),
                "direction": line.get("direction"),
                "station": departure.get("station"),
                "stopPoint": departure.get("stopPoint"),
                "platform": departure.get("platform"),
                "realtimePlatform": departure.get("realtimePlatform"),
            }
        )
    for entry in grouped.values():
        entry["stops"].sort(key=lambda s: s.get("timeOffset", 0))
        entry["destination"] = _first_trip(entry["stops"])[-1]["direction"]
    return grouped


def _subline_candidates(
    sublines_cache: dict, line_id: str, origin: str, destination: str
) -> list[list[dict]]:
    """Sublines for line_id whose first/last station name matches origin and
    the derived destination."""
    entry = sublines_cache.get(line_id)
    if not entry:
        return []
    return [
        sub["stations"]
        for sub in entry["sublines"]
        if sub["stations"]
        and sub["stations"][0]["name"] == origin
        and sub["stations"][-1]["name"] == destination
    ]


def _is_subsequence(observed_ids: list[str], candidate_ids: list[str]) -> bool:
    """True if observed_ids appears, in order, within candidate_ids (not
    necessarily contiguous)."""
    idx = 0
    for station_id in candidate_ids:
        if idx < len(observed_ids) and observed_ids[idx] == station_id:
            idx += 1
    return idx == len(observed_ids)


def _anchor_set(line: dict) -> set[str]:
    """Anchor stations queried for this service's own vehicle type - other
    anchors don't apply, since they were never queried for it. SEV/bus uses
    the dynamic announcement-station set, not tracked here, so no anchor
    check applies to it."""
    short_info = (line.get("type") or {}).get("shortInfo")
    if short_info == "RB":
        return ANCHOR_STATIONS_RB
    if short_info == "Bus":
        return set()
    return ANCHOR_STATIONS_MAIN


def match_subline(entry: dict, sublines_cache: dict) -> list[list[dict]]:
    """Narrow down which hvv:sublines route variant a grouped service ran:
    keep candidates whose stations contain the observed station IDs as an
    order-preserving subsequence, then drop ones missing an intermediate
    anchor station - but never down to zero, since that could just mean the
    snapshot didn't reach that far. Returns the surviving candidates: empty
    (no match), one (resolved) or several (still ambiguous)."""
    line = entry["line"]
    candidates = _subline_candidates(
        sublines_cache, line.get("id"), line.get("origin"), entry.get("destination")
    )
    observed_ids: list[str] = []
    seen: set[str] = set()
    for stop in entry["stops"]:
        station_id = (stop.get("station") or {}).get("id")
        if station_id and station_id not in seen:
            seen.add(station_id)
            observed_ids.append(station_id)

    subseq = [
        c for c in candidates if _is_subsequence(observed_ids, [s["id"] for s in c])
    ]
    anchors = _anchor_set(line)
    narrowed = [
        c
        for c in subseq
        if not any(s["name"] in anchors and s["id"] not in observed_ids for s in c[:-1])
    ]
    return narrowed or subseq


def match_sublines(by_service: dict[str, dict], sublines_cache: dict) -> None:
    """Attach a "subline" key to each grouped service in place - the matched
    station sequence (each a {id, name} dict, as carried by the sublines
    cache) when match_subline() resolves to exactly one candidate, else None
    (no match, or still ambiguous)."""
    for entry in by_service.values():
        candidates = match_subline(entry, sublines_cache)
        entry["subline"] = candidates[0] if len(candidates) == 1 else None


def _run_snapshot(redis_client, key: str, start: datetime, fetch) -> bool:
    """Fetch and store one snapshot via fetch(), unless the cached response
    is already anchored at start. Returns whether an API call was made, so
    the caller can pace the next one to the 1 req/s rate limit."""
    if _is_current(redis_client, key, start):
        print(f"already current, skipped ({key})")
        return False
    response = fetch()
    if response is None:
        print(f"nothing to query, skipped ({key})")
        return False
    _store(redis_client, key, response)
    print(f"{len(response.get('departures', []))} departures ({key})")
    return True


def main() -> None:
    """CLI: run the three departureList snapshots once, store raw in Redis -
    each skipped once its cache already reflects today's anchor time."""
    client = GtiClient()
    redis_client = get_redis_client()
    start = _query_start_time(datetime.now())

    if _run_snapshot(
        redis_client,
        REDIS_MAIN_KEY,
        start,
        lambda: fetch_main_departures(client, start),
    ):
        time.sleep(1)  # 1 req/s rate limit across all GTI endpoints
    if _run_snapshot(
        redis_client, REDIS_RB_KEY, start, lambda: fetch_rb_departures(client, start)
    ):
        time.sleep(1)
    _run_snapshot(
        redis_client,
        REDIS_SEV_KEY,
        start,
        lambda: fetch_sev_departures(client, redis_client, start),
    )

    merged = merge_departures(
        _load_data(redis_client, REDIS_MAIN_KEY),
        _load_data(redis_client, REDIS_RB_KEY),
        _load_data(redis_client, REDIS_SEV_KEY),
    )
    _store(redis_client, REDIS_MERGED_KEY, {"departures": merged})
    print(f"{len(merged)} unique departures ({REDIS_MERGED_KEY})")

    by_service = group_by_service(merged)
    _, sublines_cache = load_sublines_cache(redis_client)
    match_sublines(by_service, sublines_cache)
    resolved = sum(1 for e in by_service.values() if e["subline"] is not None)
    _store(redis_client, REDIS_BY_SERVICE_KEY, by_service)
    print(
        f"{len(by_service)} service_ids, {resolved} matched to a subline "
        f"({REDIS_BY_SERVICE_KEY})"
    )


if __name__ == "__main__":
    main()
