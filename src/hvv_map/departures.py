"""One-off 24h departureList snapshots per anchor-station set, cached raw in
Redis for later analysis (service ID collection, SEV detection), plus a
deduplicated merge (hvv:departures:merged) and a per-serviceId grouping
(hvv:departures:by_service) built from it. Not part of the continuous
fetcher loop - run manually or on its own schedule.
"""

import json
import time
from datetime import date, datetime, time as dtime, timedelta

from hvv_map.departures_tree import parse_anchor
from hvv_map.gti_client import GtiClient
from hvv_map.lines import LINE_NAME_PATTERN, WANTED_R_BAHN_LINES, load_sublines_cache
from hvv_map.redis_client import get_redis_client
from hvv_map.trips import berlin_now

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
    "Rübenkamp (City Nord)",  # last stop before the S1 Airport/Poppenbüttel split
    "Kornweg (Klein Borstel)",  # Poppenbüttel branch: late destination changes
    "Iserbrook",  # only trains running on to Wedel stop here
} - PSEUDO_STATIONS  # fmt: skip
MAIN_SERVICE_TYPES = ["SBAHN", "UBAHN", "AKN", "FAEHRE"]

# Terminal/hub and subline-disambiguating stations covering the regional
# trains in R_BAHN_LINES (Lübeck-Kücknitz tells the RE8 shuttle variants apart;
# Harburg, Buxtehude and Buchholz cover the lines south of the Elbe; the far
# terminals and hubs give live data for the whole route).
ANCHOR_STATIONS_R = {
    "Elmshorn", "Ahrensburg", "Wrist", "Itzehoe", "Bad Oldesloe",
    "Hamburg-Altona", "Hamburg Hbf", "Lübeck Hbf", "Bargteheide",
    "Hamburg Dammtor", "Hasselbrook", "Tonndorf", "Glückstadt",
    "Lübeck-Kücknitz", "Harburg", "Buxtehude", "Buchholz",
    "Hannover Hbf", "Bremen Hbf", "Husum", "Westerland(Sylt)", "Flensburg",
    "Cuxhaven", "Neumünster", "Uelzen", "Lüneburg", "Kiel Hbf", "Schwerin Hbf",
}  # fmt: skip
R_SERVICE_TYPES = ["RBAHN"]  # GTI service type, covers RB and RE

SEV_SERVICE_TYPES = ["SBAHN", "UBAHN", "AKN", "FAEHRE", "BUS"]
REDIS_ANNOUNCEMENT_STATIONS_KEY = "hvv:announcement_stations"

REDIS_DAY_KEY = "hvv:departures:day:{date}"  # merged departures of one day
REDIS_MERGED_KEY = "hvv:departures:merged"
REDIS_BY_SERVICE_KEY = "hvv:departures:by_service"
# The anchor time all three snapshots were queried from - needed to turn a
# by_service stop's timeOffset (minutes from it) back into an absolute time,
# e.g. for a departureCourse query (see gti_shapes.py).
REDIS_ANCHOR_KEY = "hvv:departures:anchor_start"


# GTI resolves the part of a query after midnight with the previous day's
# timetable, so a plan query must start at 00:00 and end before 24:00.
QUERY_ANCHOR_HOUR = 0
NEXT_DAY_TAIL_MINUTES = 6 * 60  # tomorrow's hours kept in the plan
PREVIOUS_DAY_TAIL_MINUTES = 3 * 60  # yesterday's late hours kept after midnight
PLAN_MINUTES = 24 * 60 - 1


def _query_start_time(now: datetime) -> datetime:
    """Start of the current calendar day: one query holds the day's services."""
    return now.replace(hour=QUERY_ANCHOR_HOUR, minute=0, second=0, microsecond=0)


def _day_key(day: date) -> str:
    return REDIS_DAY_KEY.format(date=day.isoformat())


def snapshots_stale(redis_client, now: datetime | None = None) -> bool:
    """True if the combined plan is not anchored at today's start (e.g. right
    after midnight) or today's snapshot is missing."""
    start = _query_start_time(now or berlin_now())
    raw = redis_client.get(REDIS_ANCHOR_KEY)
    anchor = parse_anchor(json.loads(raw)["data"]) if raw else None
    return anchor != start or not redis_client.get(_day_key(start.date()))


def _build_request(
    stations: set[str],
    service_types: list[str],
    start: datetime,
    minutes: int = PLAN_MINUTES,
) -> dict:
    return {
        "maxList": 25000,
        "maxTimeOffset": minutes,
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


def fetch_main_departures(
    client: GtiClient, start: datetime, minutes: int = PLAN_MINUTES
) -> dict:
    """departureList across every U/S/AKN/ferry anchor station."""
    request = _build_request(ANCHOR_STATIONS_MAIN, MAIN_SERVICE_TYPES, start, minutes)
    return client.send("departureList", request)


def fetch_r_departures(
    client: GtiClient, start: datetime, minutes: int = PLAN_MINUTES
) -> dict:
    """departureList across the regional-train anchor stations, filtered
    down to WANTED_R_BAHN_LINES - RBAHN alone also returns other DB regional trains
    passing through."""
    request = _build_request(ANCHOR_STATIONS_R, R_SERVICE_TYPES, start, minutes)
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
    client: GtiClient, redis_client, start: datetime, minutes: int = PLAN_MINUTES
) -> dict | None:
    """departureList (incl. bus) across stations currently named in
    active announcements, filtered down to is_line_of_interest() - widens
    beyond the fixed anchors to catch replacement services without pulling
    in every other bus line passing through. None if no announcement
    stations are cached."""
    payload = redis_client.get(REDIS_ANNOUNCEMENT_STATIONS_KEY)
    station_names = json.loads(payload)["data"] if payload else []
    if not station_names:
        return None
    request = _build_request(set(station_names), SEV_SERVICE_TYPES, start, minutes)
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
                "vehicles": departure.get("vehicles"),
            }
        )
    for entry in grouped.values():
        entry["stops"].sort(key=lambda s: s.get("timeOffset", 0))
        entry["destination"] = _first_trip(entry["stops"])[-1]["direction"]
    return grouped


def _names_match(departure_name: str | None, station_name: str) -> bool:
    """Equal, or one name is the other plus a trailing qualifier at a word
    boundary: departures say "Landungsbrücken" or "AIRBUS" where the subline
    has "Landungsbrücken Brücke 1" or "AIRBUS (Schiffsanleger)"."""
    if not departure_name:
        return False
    if departure_name == station_name:
        return True
    shorter, longer = sorted((departure_name, station_name), key=len)
    return longer.startswith(shorter + " ")


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
        and _names_match(origin, sub["stations"][0]["name"])
        and _names_match(destination, sub["stations"][-1]["name"])
    ]


def _is_subsequence(observed_ids: list[str], candidate_ids: list[str]) -> bool:
    """True if observed_ids appears, in order, within candidate_ids (not
    necessarily contiguous)."""
    idx = 0
    for station_id in candidate_ids:
        if idx < len(observed_ids) and observed_ids[idx] == station_id:
            idx += 1
    return idx == len(observed_ids)


# Lines whose stations form one continuous physical loop (Hamburg's
# "Ringlinie") rather than two distinct termini - a subline's declared
# station list is only one arbitrary cut through the loop, so a real trip
# can be observed starting anywhere else on it and run past the declared
# list's last station right back around to its own first one. Doubling the
# candidate's ids lets that wrap-around still match in order - see
# _candidate_match_ids. Every other line has two real, distinct termini, so
# a trip can never wrap from its route's end back to its own start; doubling
# those unconditionally would risk matching a reversed trip on a short
# route (e.g. a 2-stop shuttle) to the forward candidate.
RING_LINE_NAMES = {"U3"}


def _candidate_match_ids(candidate: list[dict], line_name: str) -> list[str]:
    """Station ids to subsequence-match observed stops against - doubled for
    a ring line (see RING_LINE_NAMES), unchanged for every other line."""
    ids = [s["id"] for s in candidate]
    if line_name in RING_LINE_NAMES:
        return ids + ids
    return ids


def _anchor_set(line: dict) -> set[str]:
    """Anchor stations queried for this service's own vehicle type - other
    anchors don't apply, since they were never queried for it. SEV/bus uses
    the dynamic announcement-station set, not tracked here, so no anchor
    check applies to it."""
    short_info = (line.get("type") or {}).get("shortInfo")
    if short_info in ("RB", "RE"):
        return ANCHOR_STATIONS_R
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

    line_name = line.get("name", "")
    subseq = [
        c
        for c in candidates
        if _is_subsequence(observed_ids, _candidate_match_ids(c, line_name))
    ]
    anchors = _anchor_set(line)
    narrowed = [
        c
        for c in subseq
        if not any(s["name"] in anchors and s["id"] not in observed_ids for s in c[:-1])
    ]
    result = narrowed or subseq
    if len(result) > 1:
        # Variants that only skip stops of the longest one cannot be told
        # apart by few observed stops (e.g. ferries seen at one anchor);
        # the longest covers every station any of them serves.
        longest = max(result, key=len)
        longest_ids = [s["id"] for s in longest]
        if all(_is_subsequence([s["id"] for s in c], longest_ids) for c in result):
            return [longest]
    return result


def match_sublines(by_service: dict[str, dict], sublines_cache: dict) -> None:
    """Attach a "subline" key to each grouped service in place - the matched
    station sequence (each a {id, name} dict, as carried by the sublines
    cache) when match_subline() resolves to exactly one candidate, else None
    (no match, or still ambiguous)."""
    for entry in by_service.values():
        candidates = match_subline(entry, sublines_cache)
        entry["subline"] = candidates[0] if len(candidates) == 1 else None


def fetch_day(client: GtiClient, redis_client, day: date) -> list[dict]:
    """Merged departures of all three queries for one calendar day."""
    start = datetime.combine(day, dtime.min)
    main = fetch_main_departures(client, start)
    time.sleep(1)  # 1 req/s rate limit across all GTI endpoints
    regional = fetch_r_departures(client, start)
    time.sleep(1)
    sev = fetch_sev_departures(client, redis_client, start) or {}
    return merge_departures(main, regional, sev)


def _ensure_day(client: GtiClient, redis_client, day: date) -> list[dict]:
    """The stored departures of a day, fetched first if missing."""
    key = _day_key(day)
    if not redis_client.get(key):
        departures = fetch_day(client, redis_client, day)
        _store(redis_client, key, {"departures": departures})
        print(f"{len(departures)} departures ({key})")
        time.sleep(1)
    return _load_data(redis_client, key).get("departures", [])


def _shifted(departures: list[dict], minutes: int, limit: int) -> list[dict]:
    """Copies with timeOffset moved by minutes, keeping those up to limit."""
    shifted = []
    for departure in departures:
        offset = departure.get("timeOffset")
        if offset is None or offset > limit:
            continue
        shifted.append({**departure, "timeOffset": offset + minutes})
    return shifted


def combine_days(
    today: list[dict], tomorrow: list[dict], yesterday: list[dict] | None = None
) -> list[dict]:
    """Yesterday's late hours (negative offsets), today's departures and the
    early hours of tomorrow, as offsets from today's start, so trips running
    past midnight stay in one piece, also across the midnight rollover."""
    late = 24 * 60 - PREVIOUS_DAY_TAIL_MINUTES
    previous = [
        {**d, "timeOffset": d["timeOffset"] - 24 * 60}
        for d in yesterday or []
        if d.get("timeOffset") is not None and d["timeOffset"] >= late
    ]
    return previous + today + _shifted(tomorrow, 24 * 60, NEXT_DAY_TAIL_MINUTES)


def run_daily_snapshots(client: GtiClient, redis_client) -> None:
    """Fetch today's and tomorrow's plan once each (a day is stored as soon
    as it is known, so the midnight rollover needs no new query), then
    rebuild the merged and by-service views anchored at today's 00:00."""
    start = _query_start_time(berlin_now())
    today = _ensure_day(client, redis_client, start.date())
    try:
        tomorrow = _ensure_day(client, redis_client, start.date() + timedelta(days=1))
    except Exception as e:  # tomorrow is a bonus, today's plan is enough
        print(f"tomorrow's plan not available: {e}")
        tomorrow = []

    # yesterday only if still stored: no query just for the rollover
    yesterday = _load_data(
        redis_client, _day_key(start.date() - timedelta(days=1))
    ).get("departures", [])
    merged = combine_days(today, tomorrow, yesterday)
    _store(redis_client, REDIS_MERGED_KEY, {"departures": merged})
    print(f"{len(merged)} unique departures ({REDIS_MERGED_KEY})")

    by_service = group_by_service(merged)
    _, sublines_cache = load_sublines_cache(redis_client)
    match_sublines(by_service, sublines_cache)
    resolved = sum(1 for e in by_service.values() if e["subline"] is not None)
    _store(redis_client, REDIS_BY_SERVICE_KEY, by_service)
    _store(
        redis_client,
        REDIS_ANCHOR_KEY,
        {"date": start.strftime("%Y-%m-%d"), "time": start.strftime("%H:%M:%S")},
    )
    print(
        f"{len(by_service)} service_ids, {resolved} matched to a subline "
        f"({REDIS_BY_SERVICE_KEY})"
    )


def main() -> None:
    """CLI: run the daily snapshots once."""
    run_daily_snapshots(GtiClient(), get_redis_client())


if __name__ == "__main__":
    main()
