"""CLI: survey which regional (RBAHN) lines GTI reports around Hamburg and
how many trips each has per day, to decide which lines and anchor stations
to add to R_BAHN_LINES. One departureList request per station, nothing is
stored.

Usage: hvvmap-r-lines [--stations "Name1,Name2"] [--json FILE]
"""

import json
import sys
import time
from collections import defaultdict

from hvv_map.departures import (
    ANCHOR_STATIONS_R,
    R_SERVICE_TYPES,
    _build_request,
    _query_start_time,
    berlin_now,
)
from hvv_map.gti_client import GtiClient
from hvv_map.lines import R_BAHN_LINES
from hvv_map.redis_client import get_redis_client
from hvv_map.stations import StationInfo, load_stations

# Hubs along candidate routes (search terms, resolved against the catalog).
CANDIDATE_TERMS = (
    "Harburg", "Buchholz", "Tostedt", "Rotenburg", "Winsen", "Lüneburg",
    "Uelzen", "Buxtehude", "Stade", "Cuxhaven", "Pinneberg", "Neumünster",
    "Kiel Hbf", "Husum", "Westerland", "Büchen", "Schwarzenbek",
    "Schwerin Hbf", "Bremen Hbf",
)  # fmt: skip
REQUEST_PAUSE = 1.1  # seconds, GTI allows about one request per second


def resolve_station(catalog: list[StationInfo], term: str) -> tuple[str | None, list]:
    """Name to query for a search term: exact name or combined name first,
    else None. The second value lists similar catalog names as hints."""
    wanted = term.casefold()
    for station in catalog:
        if wanted in (station.name.casefold(), station.combined_name.casefold()):
            return station.name, []
    hints = sorted(
        {s.combined_name or s.name for s in catalog if wanted in s.name.casefold()}
    )
    return None, hints[:5]


def summarize(responses: dict[str, dict]) -> dict:
    """Per line: trips (distinct serviceIds), departures per station and the
    directions seen, from {station name: departureList response}."""
    lines: dict[str, dict] = defaultdict(
        lambda: {"trips": set(), "stations": defaultdict(int), "directions": set()}
    )
    for station, response in responses.items():
        for departure in response.get("departures") or []:
            line = departure.get("line") or {}
            entry = lines[line.get("name", "?")]
            entry["trips"].add(departure.get("serviceId"))
            entry["stations"][station] += 1
            if line.get("direction"):
                entry["directions"].add(line["direction"])
    return {
        name: {
            "trips": len(entry["trips"]),
            "stations": dict(entry["stations"]),
            "directions": sorted(entry["directions"]),
            "wanted": name.split("-")[0] in R_BAHN_LINES,
        }
        for name, entry in lines.items()
    }


def format_summary(summary: dict, errors: dict[str, str]) -> str:
    rows = [f"{'Linie':8} {'Fahrten':>7}  {'drin':4} Stationen / Ziele"]
    for name in sorted(summary):
        entry = summary[name]
        stations = ", ".join(
            f"{s} {n}"
            for s, n in sorted(entry["stations"].items(), key=lambda i: -i[1])
        )
        known = "ja" if entry["wanted"] else ""
        rows.append(f"{name:8} {entry['trips']:>7}  {known:4} {stations}")
        rows.append(f"{'':21} -> {' | '.join(entry['directions'])}")
    if errors:
        rows.append("")
        rows.extend(f"Fehler {station}: {error}" for station, error in errors.items())
    return "\n".join(rows)


def survey(client: GtiClient, stations: list[str]) -> tuple[dict, dict]:
    """departureList (RBAHN, 24h) per station, unfiltered by line name."""
    start = _query_start_time(berlin_now())
    responses: dict[str, dict] = {}
    errors: dict[str, str] = {}
    for position, station in enumerate(stations):
        if position:
            time.sleep(REQUEST_PAUSE)
        try:
            response = client.send(
                "departureList", _build_request({station}, R_SERVICE_TYPES, start)
            )
        except Exception as error:  # one bad station must not stop the survey
            errors[station] = repr(error)
            continue
        if response.get("returnCode") != "OK":
            errors[station] = str(
                response.get("errorText") or response.get("returnCode")
            )
            continue
        responses[station] = response
    return responses, errors


def main() -> None:
    args = sys.argv[1:]
    terms = None
    if "--stations" in args:
        terms = args[args.index("--stations") + 1].split(",")
    catalog = load_stations(get_redis_client())
    stations = sorted(ANCHOR_STATIONS_R) if terms is None else []
    for term in terms or CANDIDATE_TERMS:
        name, hints = resolve_station(catalog, term.strip())
        if name:
            stations.append(name)
        else:
            print(f"Station '{term}' nicht im Katalog; ähnlich: {hints or 'nichts'}")
    stations = list(dict.fromkeys(stations))
    print(f"Abfrage von {len(stations)} Stationen: {', '.join(stations)}\n")
    responses, errors = survey(GtiClient(), stations)
    summary = summarize(responses)
    print(format_summary(summary, errors))
    if "--json" in args:
        path = args[args.index("--json") + 1]
        with open(path, "w", encoding="utf-8") as file:
            json.dump(summary, file, ensure_ascii=False, indent=1)
        print(f"\nRohdaten: {path}")


if __name__ == "__main__":
    main()
