"""Our own SPERRUNG/BARRIEREFREIHEIT/SONSTIGE classification of HVV
announcements - not an HVV API field. Kept dependency-free (stdlib only)
so both the fetcher (via disruptions.py) and the API server can import it
without pulling in the GTI client and its `requests` dependency.
"""

import re
from datetime import datetime, timezone

CATEGORY_SPERRUNG = "SPERRUNG"
CATEGORY_BARRIEREFREIHEIT = "BARRIEREFREIHEIT"
CATEGORY_SONSTIGE = "SONSTIGE"
CATEGORY_COLORS = {
    CATEGORY_SPERRUNG: "#ff6600",
    CATEGORY_BARRIEREFREIHEIT: "#42A5F5",
    CATEGORY_SONSTIGE: "#888888",
}

# Reference to the "Rollstuhl/Kinderwagen" search option in the hvv journey
# planner, or an elevator explicitly reported out of service - both signal
# an accessibility notice rather than a service disruption. Spelling varies
# (with/without spaces, with/without quotes), hence the regex.
ACCESSIBILITY_PATTERNS = [
    re.compile(r"Rollstuhl\s*/\s*Kinderwagen", re.IGNORECASE),
    re.compile(r"Aufzu(g|üge).*?außer Betrieb", re.IGNORECASE),
]

# Real closure announcements don't always say "Sperrung"
# outright (e.g. a bomb-disposal notice) - checked against summary AND
# description, since the actual "no trains" wording often only appears there.
CLOSURE_PATTERNS = [
    re.compile(r"Sperrung", re.IGNORECASE),
    re.compile(r"fahren (keine|nicht)", re.IGNORECASE),
    re.compile(r"keine Züge", re.IGNORECASE),
    re.compile(r"unterbrochen", re.IGNORECASE),
    re.compile(r"kein Zugverkehr", re.IGNORECASE),
    re.compile(r"gesperrt", re.IGNORECASE),
]


def is_accessibility_related(announcement: dict) -> bool:
    description = announcement.get("description") or ""
    return any(p.search(description) for p in ACCESSIBILITY_PATTERNS)


def classify_category(announcement: dict) -> str:
    """SPERRUNG: clear closure/replacement-service, matched against summary
    AND description. BARRIEREFREIHEIT: pure accessibility notice.
    SONSTIGE: everything else."""
    if is_accessibility_related(announcement):
        return CATEGORY_BARRIEREFREIHEIT
    text = " ".join(
        [announcement.get("summary") or "", announcement.get("description") or ""]
    )
    if any(p.search(text) for p in CLOSURE_PATTERNS):
        return CATEGORY_SPERRUNG
    return CATEGORY_SONSTIGE


def is_currently_valid(announcement: dict, now: datetime | None = None) -> bool:
    """True if `now` falls within any of the announcement's validity windows.
    No validities at all -> treated as valid (conservative)."""
    if now is None:
        now = datetime.now(timezone.utc)
    validities = announcement.get("validities") or []
    if not validities:
        return True
    for time_range in validities:
        begin, end = time_range.get("begin"), time_range.get("end")
        if not begin or not end:
            continue
        if datetime.fromisoformat(begin) <= now <= datetime.fromisoformat(end):
            return True
    return False
