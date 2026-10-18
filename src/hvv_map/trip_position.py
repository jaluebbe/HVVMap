"""Where a trip is at a given time: at a stop, or between two stops.

Works on stop times that already include the delay. Times are minute-exact
at best, so the dwell at a stop is derived: the planned dwell if it is
positive, else DEFAULT_DWELL_SECONDS; ferries dwell at least
FERRY_MIN_DWELL_SECONDS. The departure time is the anchor, the arrival is
placed the dwell before it.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

DEFAULT_DWELL_SECONDS = 30
FERRY_MIN_DWELL_SECONDS = 60

BEFORE, AT_STOP, BETWEEN, AFTER = "before", "at_stop", "between", "after"


@dataclass(frozen=True)
class Position:
    state: str
    index: int | None = None  # stop index (at_stop) or segment start (between)
    progress: float | None = None  # 0..1 on the segment (between only)

    @property
    def end_index(self) -> int | None:
        return self.index + 1 if self.state == BETWEEN else None


def dwell_seconds(arr: datetime | None, dep: datetime | None, ferry: bool) -> float:
    """Dwell at a stop: planned if positive, else the default."""
    planned = (dep - arr).total_seconds() if arr and dep else 0
    if planned <= 0:
        planned = DEFAULT_DWELL_SECONDS
    return max(planned, FERRY_MIN_DWELL_SECONDS) if ferry else planned


def apply_delay(stops: list[dict]) -> list[dict]:
    """Stops with arr/dep shifted by their delay (seconds); a stop without a
    delay takes the previous stop's."""
    shifted, last = [], 0
    for stop in stops:
        if stop.get("delay") is not None:
            last = stop["delay"]
        shift = timedelta(seconds=last)
        shifted.append(
            {
                **stop,
                "arr": stop["arr"] + shift if stop.get("arr") else None,
                "dep": stop["dep"] + shift if stop.get("dep") else None,
            }
        )
    return shifted


def timeline(stops: list[dict], ferry: bool = False) -> list[tuple[datetime, datetime]]:
    """(arrival, departure) per stop, monotonic. Stops carry arr/dep with the
    delay applied; the first lacks arr, the last lacks dep."""
    result = []
    for index, stop in enumerate(stops):
        arr, dep = stop.get("arr"), stop.get("dep")
        dwell = timedelta(seconds=dwell_seconds(arr, dep, ferry))
        if dep is None and arr is not None:
            dep = arr + dwell  # terminus: stays until the dwell is over
        elif arr is None and dep is not None:
            arr = dep - dwell
        elif dep is None:
            raise ValueError(f"stop {index} has no time")
        if dep - arr < dwell:
            arr = dep - dwell
        if result:
            arr = max(arr, result[-1][1])
            dep = max(dep, arr)
        result.append((arr, dep))
    return result


def locate(stops: list[dict], now: datetime, ferry: bool = False) -> Position | None:
    """Position at `now`; None for a trip without stop times."""
    if not stops or any(not (s.get("arr") or s.get("dep")) for s in stops):
        return None
    times = timeline(apply_delay(stops), ferry)
    if now < times[0][0]:
        return Position(BEFORE)
    for index, (arr, dep) in enumerate(times):
        if arr <= now <= dep:
            return Position(AT_STOP, index)
        if index + 1 < len(times):
            nxt = times[index + 1][0]
            if dep < now < nxt:
                span = (nxt - dep).total_seconds()
                return Position(BETWEEN, index, (now - dep).total_seconds() / span)
    return Position(AFTER)


def _clock(base: datetime, hhmm: str | None) -> datetime | None:
    """HH:MM on the day of `base`, moved by a day across midnight."""
    if not hhmm:
        return None
    value = datetime.combine(base.date(), datetime.strptime(hhmm, "%H:%M").time())
    if value - base > timedelta(hours=12):
        value -= timedelta(days=1)
    elif base - value > timedelta(hours=12):
        value += timedelta(days=1)
    return value


def stops_from_row(row: dict) -> list[dict]:
    """Stops of a trip-table row as arr/dep datetimes plus delay (seconds)."""
    stops = []
    for stop in row["stops"]:
        at = datetime.fromisoformat(stop["at"]) if stop.get("at") else None
        if at is None:
            arr = dep = None
        else:
            arr, dep = _clock(at, stop.get("arr")), _clock(at, stop.get("dep"))
            if arr is None and dep is None:
                dep = at
        stops.append(
            {
                "name": stop["station"],
                "arr": arr,
                "dep": dep,
                "delay": stop.get("delay"),
            }
        )
    return stops
