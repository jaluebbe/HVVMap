"""Plan/live pairing of the departures at one station; same rules as
static/departure_pairing.js.

A live departure joins a plan departure only with the same planned minute
and a compatible planned platform (unknown does not conflict); among equals
the same direction id wins. S1 only (wing trains): a remaining pair within
one minute matches if the known platform and the destination are identical,
and a matched plan departure takes a second live departure with the same
known platform and the same minute as its live partner whose destination
extends the plan destination.

Departures are dicts with "at" (datetime), "platform", "direction" and
"direction_id".
"""

from datetime import timedelta

WING_LINES = {"S1"}
NEAR = timedelta(minutes=1)
QUALITY_EXACT = "exakt"
QUALITY_NEAR = "±1 min"


def extends_destination(planned: str | None, live: str | None) -> bool:
    """The live destination covers the plan destination: it contains it, or
    the train runs on from Blankenese to Wedel."""
    live = live or ""
    return bool(planned) and (
        planned in live or (planned == "Blankenese" and live == "Wedel")
    )


def _compatible(a: str | None, b: str | None) -> bool:
    return not a or not b or a == b


def pair(plan: list[dict], live: list[dict], line: str) -> tuple[list[tuple], list]:
    """([(plan index, live index | None, wing live index | None, quality)],
    indices of unpaired live departures)."""
    free = list(range(len(live)))
    wing = line in WING_LINES

    def exact(p, l):
        return l["at"] == p["at"] and _compatible(p["platform"], l["platform"])

    def near(p, l):
        return (
            wing
            and abs(l["at"] - p["at"]) <= NEAR
            and bool(p["platform"])
            and p["platform"] == l["platform"]
            and p["direction"] == l["direction"]
        )

    def take(p, match):
        best = None
        for i in free:
            if not match(p, live[i]):
                continue
            if best is None or (
                live[i]["direction_id"] == p["direction_id"]
                and live[best]["direction_id"] != p["direction_id"]
            ):
                best = i
        if best is not None:
            free.remove(best)
        return best

    rows = [[i, take(p, exact), None, QUALITY_EXACT] for i, p in enumerate(plan)]
    for row in rows:
        if row[1] is None:
            row[1] = take(plan[row[0]], near)
            row[3] = QUALITY_NEAR if row[1] is not None else None
    for row in rows:
        p = plan[row[0]]
        if not wing or row[1] is None or not p["platform"]:
            continue
        partner = live[row[1]]
        row[2] = next(
            (
                i
                for i in free
                if live[i]["platform"] == p["platform"]
                and live[i]["at"] == partner["at"]
                and extends_destination(p["direction"], live[i]["direction"])
            ),
            None,
        )
        if row[2] is not None:
            free.remove(row[2])
    return [tuple(row) for row in rows], free
