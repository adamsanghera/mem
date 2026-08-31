"""Ledger-derived reports: heat (promotion candidates), stats, retrieval evals."""

import statistics
from collections import Counter
from math import sqrt
from pathlib import Path

import yaml

from . import corpus, index, ledger

# Pages whose embeddings sit within this cosine distance are reported as one
# cluster: hot together, consolidated together.
CLUSTER_DISTANCE = 0.35


def _cosine_distance(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm = sqrt(sum(x * x for x in a)) * sqrt(sum(y * y for y in b))
    return 1.0 if norm == 0 else 1.0 - dot / norm


def _feedback_by_page(events: list[dict]) -> dict[str, Counter]:
    by_page: dict[str, Counter] = {}
    for e in events:
        if e["event"] == "feedback" and e.get("filename"):
            by_page.setdefault(e["filename"], Counter())[e["verdict"]] += 1
    return by_page


def hot(r: Path, window_days: int, min_hits: int) -> list[list[dict]]:
    """Clusters of pages whose ledger heat crosses the promotion threshold."""
    events = ledger.load(r, window_days)
    reads = Counter(e["filename"] for e in events if e["event"] == "read")
    surfaced = Counter(e["filename"] for e in events if e["event"] == "search_hit")
    feedback = _feedback_by_page(events)

    candidates = []
    for filename in set(reads) | set(surfaced):
        total = reads[filename] + surfaced[filename]
        if total >= min_hits and (r / filename).is_file():
            candidates.append(
                {
                    "filename": filename,
                    "hits": total,
                    "reads": reads[filename],
                    "search_hits": surfaced[filename],
                    "feedback": dict(feedback.get(filename, Counter())),
                }
            )
    candidates.sort(key=lambda c: -c["hits"])
    if not candidates:
        return []

    vectors = index.embeddings_for(r, [c["filename"] for c in candidates])
    clusters: list[list[dict]] = []
    for candidate in candidates:
        vec = vectors.get(candidate["filename"])
        home = None
        if vec is not None:
            for cluster in clusters:
                anchor = vectors.get(cluster[0]["filename"])
                if anchor and _cosine_distance(vec, anchor) < CLUSTER_DISTANCE:
                    home = cluster
                    break
        if home is None:
            clusters.append([candidate])
        else:
            home.append(candidate)
    return clusters


def stats(r: Path) -> dict:
    page_paths = corpus.pages(r)
    sizes = [p.stat().st_size for p in page_paths]
    cited = 0
    graduated = 0
    for p in page_paths:
        text = p.read_text(encoding="utf-8")
        fm, body = corpus.parse_frontmatter(text)
        if (fm and fm.get("citations")) or "http" in body:
            cited += 1
        if fm and fm.get("graduated_to"):
            graduated += 1

    events = ledger.load(r)
    week = ledger.load(r, 7)
    month = ledger.load(r, 30)
    searches_this_week = len(
        {(e["ts"], e.get("query")) for e in week if e["event"] == "search_hit"}
    )
    surfaced_30d = {e["filename"] for e in month if e["event"] == "search_hit"}
    read_30d = {e["filename"] for e in month if e["event"] == "read"}
    touched_ever = {e["filename"] for e in events}

    verdicts_30d = Counter(
        e["verdict"] for e in month if e["event"] == "feedback"
    )
    rated = sum(v for k, v in verdicts_30d.items() if k != "miss")
    helpful = sum(verdicts_30d[k] for k in ("solved", "partial", "context"))

    n = len(page_paths)
    return {
        "pages": n,
        "median_page_bytes": int(statistics.median(sizes)) if sizes else 0,
        "pages_with_citations_pct": round(100 * cited / n) if n else 0,
        "graduated_pages": graduated,
        "searches_7d": searches_this_week,
        "active_sessions_7d": len(
            {e["session"] for e in week if e.get("session")}
        ),
        "surfaced_pages_30d": len(surfaced_30d),
        "read_through_30d_pct": (
            round(100 * len(read_30d & surfaced_30d) / len(surfaced_30d))
            if surfaced_30d
            else 0
        ),
        "feedback_30d": (
            " ".join(f"{k}={verdicts_30d[k]}" for k in sorted(verdicts_30d)) or "none"
        ),
        "helpful_rate_30d_pct": round(100 * helpful / rated) if rated else "n/a",
        "recall_misses_30d": verdicts_30d["miss"],
        "orphan_pages_pct": (
            round(100 * sum(1 for p in page_paths if p.name not in touched_ever) / n)
            if n
            else 0
        ),
        "ledger_events": len(events),
    }


def run_evals(r: Path, k_default: int = 3) -> tuple[int, list[str]]:
    """Run golden retrieval fixtures. Returns (passed, failure messages)."""
    evals_path = corpus.meta_dir(r) / "evals.yaml"
    if not evals_path.is_file():
        return 0, [f"no fixtures at {evals_path}"]
    fixtures = yaml.safe_load(evals_path.read_text(encoding="utf-8")) or []

    passed = 0
    failures = []
    for fixture in fixtures:
        query, expect = fixture["query"], fixture["expect"]
        top = int(fixture.get("top", k_default))
        got = [h.filename for h in index.search(r, query, top)]
        if expect in got:
            passed += 1
        else:
            failures.append(f"MISS {query!r}: wanted {expect} in top {top}, got {got}")
    return passed, failures
