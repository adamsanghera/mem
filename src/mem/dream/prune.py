"""Which pages a prune should judge. Reads files only.

Dreams and consolidations add and repair. Neither looks at a page nobody
reads, so the corpus only grew, and every page's claims still come due for
checking. A prune takes the pages listed here and keeps, tightens, or
deletes each one.

Disuse nominates a page; it does not condemn it. Deleting on usage alone
was rejected because a page read once a year can still save a day. The run
that judges a nomination applies the test a session applies before writing
a page: would this save a later session the investigation.
"""

from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

import yaml

from .. import corpus, fresh, ledger, report

VOLATILITY_ORDER = {"fast": 0, "slow": 1}


@dataclass
class Nomination:
    filename: str
    volatility: str
    age_days: int
    size: int
    surfaced: int
    verdicts: dict[str, int]

    def evidence(self) -> str:
        """The page's record in a phrase, for the plan and the prompt."""
        parts = [
            self.volatility,
            f"{self.age_days} days old",
            f"{self.size / 1000:.1f} KB",
            f"search hits: {self.surfaced}",
        ]
        if self.verdicts:
            parts.append("rated: " + " ".join(f"{v}={n}" for v, n in sorted(self.verdicts.items())))
        return ", ".join(parts)


def _fixture_pages(r: Path) -> set[str]:
    """Pages a retrieval fixture expects to find. Deleting one would fail
    `mem eval` for a reason that has nothing to do with retrieval."""
    path = corpus.meta_dir(r) / "evals.yaml"
    if not path.is_file():
        return set()
    fixtures = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    return {f["expect"] for f in fixtures if isinstance(f, dict) and f.get("expect")}


def _days_since(value, today: date) -> int | None:
    when = fresh.to_date(value)
    return (today - when).days if when else None


def nominations(r: Path, cfg: dict, today: date | None = None) -> list[Nomination]:
    """Pages worth a prune's judgment, the most perishable first.

    A page is nominated when it is older than prune_cold_days, no working
    session has read it for that long, and no prune has judged it within
    prune_review_days. A page a working session ever rated helpful is not
    nominated: it has a record, and a quiet month usually means the work
    moved elsewhere. Stable pages (decisions, incidents, preferences) are
    read rarely and on purpose, so they are never nominated, and neither are
    standing pages, skill cards, or the pages retrieval fixtures expect. A
    page with no readable created date is left alone: its age is unknown.
    """
    today = today or datetime.now(timezone.utc).date()
    cold_days, review_days = cfg["prune_cold_days"], cfg["prune_review_days"]

    last_read: dict[str, int] = {}
    last_review: dict[str, int] = {}
    surfaced: Counter = Counter()
    verdicts: dict[str, Counter] = {}
    for e in ledger.load(r):
        name = e.get("filename")
        if not name:
            continue
        days = _days_since(e.get("ts"), today)
        if e["event"] == "prune":
            if days is not None:
                last_review[name] = min(days, last_review.get(name, days))
        elif ledger.is_maintenance(e):
            continue
        elif e["event"] == "read":
            if days is not None:
                last_read[name] = min(days, last_read.get(name, days))
        elif e["event"] == "search_hit":
            surfaced[name] += 1
        elif e["event"] == "feedback" and e.get("verdict"):
            verdicts.setdefault(name, Counter())[e["verdict"]] += 1

    fixtures = _fixture_pages(r)
    found = []
    for path in corpus.pages(r):
        name = path.name
        text = path.read_text(encoding="utf-8")
        fm, body = corpus.parse_frontmatter(text)
        fm = fm or {}
        tags = fm.get("tags") or []
        tags = tags if isinstance(tags, list) else [tags]
        if report.PRIME_TAG in tags or "skill-card" in tags or name in fixtures:
            continue
        markers = [m for m in fresh.parse_markers(body) if not m.error]
        volatility = fresh.volatility_for(fm, name, markers)
        if volatility not in VOLATILITY_ORDER:
            continue
        age = _days_since(fm.get("created"), today)
        if age is None or age <= cold_days:
            continue
        if name in last_read and last_read[name] <= cold_days:
            continue
        rated = verdicts.get(name, Counter())
        if any(rated[v] for v in report.HELPFUL_VERDICTS):
            continue
        if name in last_review and last_review[name] <= review_days:
            continue
        found.append(
            Nomination(name, volatility, age, path.stat().st_size, surfaced[name], dict(rated))
        )
    found.sort(key=lambda n: (VOLATILITY_ORDER[n.volatility], -n.age_days, n.filename))
    return found
