"""Claim markers and freshness.

Pages carry their uncertainty inline, next to the claim, using the same
MARKER(type:id) grammar scaffold-docs uses for doc trees:

    NOTE(unverified:<id>)            a hypothesis or unchecked claim; due now
    NOTE(verified:<id>:<YYYY-MM-DD>) checked against its source on that date
    NOTE(as-of:<YYYY-MM-DD>)         a dated observation; ages, never wrong
    TODO(investigate:<id>)           a declared gap; the miner turns it into a bounty

A page's volatility sets the half-life its verified claims live before
coming due. Pages with no markers fall back to a coarse rule: the last
content edit or positive verdict, against the same half-life. Nothing here
hides or reranks a page; it only says how much to trust it right now.
"""

import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

from . import corpus, ledger

HALF_LIFE_DAYS: dict[str, int | None] = {"fast": 14, "slow": 90, "stable": None}
DEFAULT_VOLATILITY = "slow"
CONFIRMING_VERDICTS = ("solved", "partial", "verified")

MARKER_RE = re.compile(r"\b(NOTE|TODO)\(([a-z-]+):([^)]*)\)")
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Title heuristics for pages that declare no volatility: dated observations
# and records of events or decisions do not decay.
OBSERVATION_TITLE_RE = re.compile(r"20\d\d-\d\d-\d\d|snapshot|live-state|\bas of\b", re.I)
STABLE_TITLE_RE = re.compile(r"incident|decided|decision|policy|prefer|postmortem", re.I)


@dataclass
class Marker:
    kind: str  # unverified | verified | as-of | investigate
    id: str | None
    date: str | None
    raw: str
    error: str | None = None


def parse_markers(text: str) -> list[Marker]:
    """All freshness markers in a text, malformed ones carrying an error.
    Other scaffold-docs markers (NOTE(boundary:), TODO(author:)) are ignored."""
    markers = []
    for m in MARKER_RE.finditer(text):
        family, kind, rest = m.group(1), m.group(2), m.group(3)
        parts = rest.split(":")
        if family == "NOTE" and kind == "unverified":
            marker = Marker(kind, parts[0], None, m.group(0))
            if len(parts) != 1 or not ID_RE.match(parts[0]):
                marker.error = "expected NOTE(unverified:<id>)"
        elif family == "NOTE" and kind == "verified":
            marker = Marker(kind, parts[0], parts[1] if len(parts) > 1 else None, m.group(0))
            if len(parts) != 2 or not ID_RE.match(parts[0]) or not DATE_RE.match(parts[1]):
                marker.error = "expected NOTE(verified:<id>:<YYYY-MM-DD>)"
        elif family == "NOTE" and kind == "as-of":
            marker = Marker(kind, None, parts[0], m.group(0))
            if len(parts) != 1 or not DATE_RE.match(parts[0]):
                marker.error = "expected NOTE(as-of:<YYYY-MM-DD>)"
        elif family == "TODO" and kind == "investigate":
            marker = Marker(kind, parts[0], None, m.group(0))
            if len(parts) != 1 or not ID_RE.match(parts[0]):
                marker.error = "expected TODO(investigate:<id>)"
        else:
            continue
        markers.append(marker)
    return markers


def volatility_for(fm: dict, filename: str, markers: list[Marker]) -> str:
    declared = str(fm.get("volatility") or "").lower()
    if declared in HALF_LIFE_DAYS:
        return declared
    if any(m.kind == "as-of" for m in markers):
        return "stable"
    title = f"{fm.get('title', '')} {filename}"
    if OBSERVATION_TITLE_RE.search(title) or STABLE_TITLE_RE.search(title):
        return "stable"
    return DEFAULT_VOLATILITY


@dataclass
class Freshness:
    volatility: str
    half_life_days: int | None
    claims: int
    due: list[str] = field(default_factory=list)
    unverified: list[str] = field(default_factory=list)
    confirmed_age_days: int | None = None
    coarse_due: bool = False

    @property
    def is_due(self) -> bool:
        return bool(self.due or self.unverified or self.coarse_due)

    def line(self) -> str:
        parts = [self.volatility]
        if self.claims:
            if self.due:
                parts.append(f"{len(self.due)} claim{'s' if len(self.due) != 1 else ''} due")
            if self.unverified:
                parts.append(f"{len(self.unverified)} unverified")
            if not self.due and not self.unverified:
                parts.append(f"{self.claims} verified")
        elif self.half_life_days is not None and self.confirmed_age_days is not None:
            prefix = "due, " if self.coarse_due else ""
            parts.append(f"{prefix}confirmed {self.confirmed_age_days}d ago")
        return " · ".join(parts)


def _to_date(value) -> date | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
    except ValueError:
        return None


def freshness(
    filename: str, fm: dict, text: str, page_events: list[dict], today: date | None = None
) -> Freshness:
    """How much to trust a page right now, from its markers or, lacking any,
    from its last edit or confirming verdict."""
    today = today or datetime.now(timezone.utc).date()
    _fm, body = corpus.parse_frontmatter(text)
    markers = [m for m in parse_markers(body) if not m.error]
    volatility = volatility_for(fm, filename, markers)
    half_life = HALF_LIFE_DAYS[volatility]

    unverified = [m.id for m in markers if m.kind == "unverified"]
    verified = [m for m in markers if m.kind == "verified"]
    due = [
        m.id
        for m in verified
        if half_life is not None and (today - date.fromisoformat(m.date)).days > half_life
    ]
    result = Freshness(volatility, half_life, len(unverified) + len(verified), due, unverified)
    if result.claims:
        return result

    last = _to_date(fm.get("updated")) or _to_date(fm.get("created"))
    for e in page_events:
        if e.get("event") == "feedback" and e.get("verdict") in CONFIRMING_VERDICTS:
            when = _to_date(e.get("ts"))
            if when and (last is None or when > last):
                last = when
    if last is not None:
        result.confirmed_age_days = (today - last).days
        result.coarse_due = half_life is not None and result.confirmed_age_days > half_life
    return result


def events_by_page(events: list[dict]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for e in events:
        if e.get("filename"):
            grouped.setdefault(e["filename"], []).append(e)
    return grouped


def page_freshness(r: Path, filename: str, events: list[dict] | None = None) -> Freshness | None:
    path = r / filename
    if not path.is_file():
        return None
    text = path.read_text(encoding="utf-8")
    fm, _ = corpus.parse_frontmatter(text)
    events = ledger.load(r) if events is None else events
    return freshness(filename, fm or {}, text, [e for e in events if e.get("filename") == filename])


def stamp_verified(path: Path, claim: str, today: date | None = None) -> bool:
    """Turn NOTE(unverified:<claim>) into NOTE(verified:<claim>:<today>), or
    refresh the date on an existing verified marker. False if no marker."""
    today = today or datetime.now(timezone.utc).date()
    text = path.read_text(encoding="utf-8")
    stamped = f"NOTE(verified:{claim}:{today.isoformat()})"
    new, n_unverified = re.subn(rf"NOTE\(unverified:{re.escape(claim)}\)", stamped, text)
    new, n_verified = re.subn(
        rf"NOTE\(verified:{re.escape(claim)}:\d{{4}}-\d{{2}}-\d{{2}}\)", stamped, new
    )
    if n_unverified + n_verified == 0:
        return False
    path.write_text(new, encoding="utf-8")
    return True


def stale(r: Path, window_days: int = 30) -> list[dict]:
    """Pages with due or unverified claims (or coarse-rule due), hottest
    first, so verification effort follows recall demand."""
    events = ledger.load(r)
    grouped = events_by_page(events)
    cutoff = (datetime.now(timezone.utc)).date().toordinal() - window_days
    heat: Counter = Counter()
    for e in events:
        if e.get("event") in ("read", "search_hit") and e.get("filename"):
            when = _to_date(e.get("ts"))
            if when and when.toordinal() >= cutoff:
                heat[e["filename"]] += 1

    rows = []
    for path in corpus.pages(r):
        text = path.read_text(encoding="utf-8")
        fm, _ = corpus.parse_frontmatter(text)
        f = freshness(path.name, fm or {}, text, grouped.get(path.name, []))
        if f.is_due:
            rows.append(
                {
                    "filename": path.name,
                    "freshness": f.line(),
                    "due": f.due,
                    "unverified": f.unverified,
                    "hits": heat[path.name],
                }
            )
    rows.sort(key=lambda row: (-row["hits"], row["filename"]))
    return rows
