"""Unit tests for the pieces that don't need ollama: corpus rules, ledger,
report math, and the reindex change-detection logic (with a fake embedder).
"""

import json
import time

import pytest

from pathlib import Path

from mem import corpus, embed, fresh, index, ledger, report


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setenv("MEM_ROOT", str(tmp_path))
    (tmp_path / "meta").mkdir()
    return tmp_path


@pytest.fixture
def fake_embed(monkeypatch):
    calls = []

    def fake(texts, truncate=True):
        calls.append(texts)
        # deterministic tiny vectors keyed off text length
        return [[float(len(t) % 7), 1.0, 0.5] for t in texts]

    monkeypatch.setattr(embed, "embed_texts", fake)
    return calls


def test_embedding_status(monkeypatch):
    import io
    import urllib.error
    import urllib.request

    def down(*_a, **_k):
        raise urllib.error.URLError("refused")

    monkeypatch.setattr(urllib.request, "urlopen", down)
    ready, message = embed.status()
    assert not ready and "not reachable" in message

    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

    monkeypatch.setattr(
        urllib.request, "urlopen",
        lambda *_a, **_k: Resp(b'{"models": [{"name": "nomic-embed-text:latest"}]}'),
    )
    ready, message = embed.status()
    assert ready and "present" in message


def test_embed_texts_raises_input_too_long_without_truncation(monkeypatch):
    import io
    import urllib.error
    import urllib.request

    def too_long(*_a, **_k):
        raise urllib.error.HTTPError(
            "http://x", 400, "Bad Request", None,
            io.BytesIO(b'{"error":"the input length exceeds the context length"}'),
        )

    monkeypatch.setattr(urllib.request, "urlopen", too_long)
    with pytest.raises(embed.InputTooLong):
        embed.embed_texts(["x" * 20000], truncate=False)


def test_add_rejects_pages_the_model_cannot_fully_embed(root, monkeypatch):
    import argparse

    from mem import cli

    def refuse(texts, truncate=True):
        raise embed.InputTooLong("the input length exceeds the context length")

    monkeypatch.setattr(embed, "embed_texts", refuse)
    args = argparse.Namespace(
        title="Too long", text="y" * 7000, file=None, summary=None, tags=None,
        citations=None, volatility=None,
    )
    with pytest.raises(SystemExit) as exc:
        cli.cmd_add(args)
    assert exc.value.code == 1
    assert corpus.pages(root) == []  # nothing written


def test_add_writes_and_indexes_when_the_page_fits(root, fake_embed):
    import argparse

    from mem import cli

    args = argparse.Namespace(
        title="Fits", text="short body", file=None, summary="s", tags=None,
        citations=None, volatility="fast",
    )
    cli.cmd_add(args)
    assert [p.name for p in corpus.pages(root)] == ["fits.md"]
    assert "volatility: fast" in (root / "fits.md").read_text()
    assert index.search(root, "short", 1)[0].filename == "fits.md"


def test_parse_markers_all_kinds_and_malformed():
    text = (
        "x NOTE(unverified:pool-count) y NOTE(verified:cap-at-boot:2026-09-24) "
        "z NOTE(as-of:2026-09-22) w TODO(investigate:deploy-cut-times) "
        "bad NOTE(verified:no-date) other NOTE(boundary:ignored)"
    )
    markers = fresh.parse_markers(text)
    ok = {(m.kind, m.id, m.date) for m in markers if not m.error}
    assert ("unverified", "pool-count", None) in ok
    assert ("verified", "cap-at-boot", "2026-09-24") in ok
    assert ("as-of", None, "2026-09-22") in ok
    assert ("investigate", "deploy-cut-times", None) in ok
    assert [m.raw for m in markers if m.error] == ["NOTE(verified:no-date)"]
    assert not any(m.kind == "boundary" for m in markers)


def test_freshness_due_rules():
    from datetime import date

    today = date(2026, 9, 24)
    page = "---\ntitle: T\n---\n\nA NOTE(verified:a:2026-09-01). B NOTE(unverified:b)."
    fast = fresh.freshness("t.md", {"volatility": "fast"}, page, [], today)
    assert fast.due == ["a"] and fast.unverified == ["b"] and fast.is_due
    slow = fresh.freshness("t.md", {"volatility": "slow"}, page, [], today)
    assert slow.due == [] and slow.unverified == ["b"]
    stable = fresh.freshness("t.md", {"volatility": "stable"}, page, [], today)
    assert stable.due == []

    observation = "---\ntitle: T\n---\n\nseen NOTE(as-of:2026-09-01)"
    obs = fresh.freshness("t.md", {}, observation, [], today)
    assert obs.volatility == "stable" and not obs.is_due

    # pessimistic default: a non-stable page with no markers reads as unverified,
    # however recently it was edited or confirmed
    plain = "---\ntitle: T\n---\n\nno markers here"
    unmarked = fresh.freshness("t.md", {"updated": "2026-09-23T00:00:00Z"}, plain, [], today)
    assert unmarked.unmarked and unmarked.is_due and unmarked.confirmed_age_days == 1
    assert "unmarked" in unmarked.line()
    confirming = [{"event": "feedback", "verdict": "solved", "ts": "2026-09-20T00:00:00Z"}]
    still = fresh.freshness("t.md", {"updated": "2026-06-01T00:00:00Z"}, plain, confirming, today)
    assert still.unmarked and still.is_due

    incident = fresh.freshness("cev-1541-incident.md", {"title": "An incident"}, plain, [], today)
    assert incident.volatility == "stable" and not incident.is_due
    card = fresh.freshness("skill-x.md", {"tags": ["skill-card"]}, plain, [], today)
    assert card.volatility == "stable" and not card.is_due

    # grammar documentation inside code spans is not a claim
    documented = "---\ntitle: T\n---\n\nuse `NOTE(unverified:<id>)` like this NOTE(verified:real:2026-09-20)."
    assert [m.id for m in fresh.parse_markers(documented)] == ["real"]


def test_stamp_verified_is_idempotent(tmp_path):
    from datetime import date

    page = tmp_path / "p.md"
    page.write_text("---\ntitle: T\n---\n\nX NOTE(unverified:cap) Y")
    assert fresh.stamp_verified(page, "cap", date(2026, 9, 24))
    assert "NOTE(verified:cap:2026-09-24)" in page.read_text()
    assert fresh.stamp_verified(page, "cap", date(2026, 10, 1))
    text = page.read_text()
    assert text.count("NOTE(") == 1 and "2026-10-01" in text
    assert not fresh.stamp_verified(page, "missing", date(2026, 10, 1))


def test_feedback_verified_with_claim_stamps_and_logs(root, fake_embed):
    import argparse

    from mem import cli

    page = corpus.new_page(root, "Cap", "The cap is 250 NOTE(unverified:cap).", summary="s")
    index.reindex(root)
    cli.cmd_feedback(
        argparse.Namespace(
            verdict="verified", refs=[page.name], note=None, claim="cap", source=None, key=None
        )
    )
    assert "NOTE(verified:cap:" in page.read_text()
    event = ledger.load(root)[-1]
    assert event["verdict"] == "verified" and event["claim"] == "cap"


def test_stale_orders_by_heat(root, fake_embed):
    a = corpus.new_page(root, "A", "x NOTE(unverified:one)")
    b = corpus.new_page(root, "B", "y NOTE(unverified:two)")
    corpus.new_page(root, "C", "a decision record, no live claims", volatility="stable")
    for _ in range(3):
        ledger.append(root, "read", b.name)
    rows = fresh.stale(root)
    assert [row["filename"] for row in rows] == [b.name, a.name]
    assert rows[0]["unverified"] == ["two"]


def test_marker_miner_is_idempotent(root, tmp_path):
    import importlib.util

    script = Path(__file__).resolve().parent.parent / "scripts" / "mine_markers.py"
    spec = importlib.util.spec_from_file_location("mine_markers", script)
    miner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(miner)

    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "plan.md").write_text("Open: TODO(investigate:deploy-cut-times) when does prod cut?\n")
    assert miner.mine(root, [docs]) == (1, 0)
    assert miner.mine(root, [docs]) == (0, 1)
    event = ledger.load(root)[-1]
    assert event["verdict"] == "miss"
    assert event["key"] == "investigate:deploy-cut-times"
    assert "prod cut" in event["note"]


def test_verify_flags_pages_past_the_embedding_budget(root, capsys):
    import argparse

    from mem import cli

    corpus.new_page(root, "Short", "fine", volatility="stable")
    corpus.new_page(root, "Long", "x" * (corpus.EMBED_BYTE_BUDGET + 100), volatility="stable")
    corpus.new_page(root, "Bare", "a live claim with no markers")
    cli.cmd_verify(argparse.Namespace())
    out = capsys.readouterr().out
    assert "long.md" in out and "embedding budget" in out
    assert "bare.md" in out and "reads as unverified" in out
    assert "0 problems, 2 warnings" in out


def test_slugify_and_filename_rules():
    assert corpus.slugify("Carbon Fibre Woks!") == "carbon-fibre-woks"
    assert corpus.FILENAME_RE.match("carbon-fibre-woks.md")
    assert not corpus.FILENAME_RE.match("-leading-hyphen.md")
    assert not corpus.FILENAME_RE.match("CAPS.md")
    with pytest.raises(ValueError):
        corpus.slugify("!!!")


def test_frontmatter_roundtrip(root):
    path = corpus.new_page(
        root, "Test Page", "Body text.", summary="A test.", citations=["https://x"]
    )
    fm, body = corpus.parse_frontmatter(path.read_text())
    assert fm["title"] == "Test Page"
    assert fm["citations"] == ["https://x"]
    assert body.strip() == "Body text."
    # datetimes stay quoted strings, not YAML timestamp objects
    assert isinstance(fm["created"], str)


def test_debris_and_subdirs_excluded(root):
    corpus.new_page(root, "Real", "x")
    (root / ".DS_Store").write_text("junk")
    (root / "notes.sync-conflict-123.md").write_text("junk")
    (root / "meta" / "not-a-page.md").write_text("junk")
    names = [p.name for p in corpus.pages(root)]
    assert names == ["real.md"]


def test_ledger_append_load_window(root):
    ledger.append(root, "read", "a.md", uuid="u1")
    ledger.append(root, "search_hit", "a.md", query="q", score=0.1)
    events = ledger.load(root)
    assert len(events) == 2
    assert ledger.load(root, window_days=1)[0]["event"] == "read"
    # corrupt line is skipped, not fatal
    with ledger.ledger_path(root).open("a") as f:
        f.write("not json\n")
    assert len(ledger.load(root)) == 2


def test_session_identifier(root, monkeypatch):
    monkeypatch.setenv("CURSOR_CONVERSATION_ID", "conv-123")
    ledger.append(root, "read", "a.md")
    assert ledger.load(root)[-1]["session"] == "conv-123"
    # explicit override wins over the harness-provided id
    monkeypatch.setenv("MEM_SESSION", "named-session")
    ledger.append(root, "read", "a.md")
    assert ledger.load(root)[-1]["session"] == "named-session"


def test_reindex_incremental(root, fake_embed):
    page = corpus.new_page(root, "One", "first body")
    embedded, removed = index.reindex(root)
    assert (embedded, removed) == (1, 0)
    # unchanged: no new embedding
    assert index.reindex(root) == (0, 0)
    # touch mtime only: cheap update, still no embedding call
    n_calls = len(fake_embed)
    time.sleep(0.01)
    page.touch()
    assert index.reindex(root) == (0, 0)
    assert len(fake_embed) == n_calls
    # content change: re-embedded
    page.write_text(page.read_text().replace("first body", "second body"))
    assert index.reindex(root) == (1, 0)
    # deletion: removed from index
    page.unlink()
    assert index.reindex(root) == (0, 1)


def test_hot_clusters_and_stats(root, fake_embed):
    a = corpus.new_page(root, "Alpha", "alpha topic")
    b = corpus.new_page(root, "Beta", "beta topic https://cite")
    index.reindex(root)
    for _ in range(5):
        ledger.append(root, "read", a.name)
    ledger.append(root, "search_hit", b.name, query="q", score=0.2)

    clusters = report.hot(root, window_days=30, min_hits=5)
    flat = [p["filename"] for c in clusters for p in c]
    assert a.name in flat and b.name not in flat

    stats = report.stats(root)
    assert stats["pages"] == 2
    assert stats["ledger_events"] == 6
    assert stats["pages_with_citations_pct"] == 50
    assert stats["orphan_pages_pct"] == 0  # both pages have events? a has, b has


def test_bounties_cluster_weak_searches_and_misses(root, monkeypatch):
    def fake(texts):
        # first-token topic decides the direction: same topic ⇒ same cluster
        return [
            [1.0, 0.0] if t.startswith("graphite") else [0.0, 1.0] for t in texts
        ]

    monkeypatch.setattr(embed, "embed_texts", fake)
    ledger.append(root, "weak_search", None, query="graphite retry backoff values")
    ledger.append(root, "weak_search", None, query="graphite retry backoff config")
    ledger.append(root, "feedback", None, verdict="miss", note="graphite retry backoff table")
    ledger.append(root, "weak_search", None, query="unrelated other topic")

    clusters = report.bounties(root, 30)
    assert len(clusters) == 2
    assert clusters[0]["count"] == 3
    assert clusters[0]["kinds"] == {"weak_search": 2, "miss": 1}
    assert report.stats(root)["weak_searches_30d"] == 3


def test_declared_miss_carries_source_and_dedups_by_key(root, monkeypatch):
    monkeypatch.setattr(embed, "embed_texts", lambda texts: [[1.0, 0.0] for _ in texts])
    ledger.append(
        root, "feedback", None, verdict="miss", note="graphite retry backoff",
        source="docs/90-scaling.md", key="investigate:retry-backoff",
    )
    assert ledger.has_key(root, "investigate:retry-backoff")
    assert not ledger.has_key(root, "investigate:other")
    clusters = report.bounties(root, 30)
    assert clusters[0]["sources"] == ["docs/90-scaling.md"]


def test_prime_briefing(root, fake_embed, monkeypatch):
    monkeypatch.setenv("CURSOR_CONVERSATION_ID", "conv-prime")
    standing = corpus.new_page(root, "House rules", "always cite", summary="Standing.", tags=["prime"])
    card = corpus.new_page(root, "Skill: prose", "pointer", summary="Write plainly.", tags=["skill-card"])
    organic = corpus.new_page(root, "Vacuum quirk", "details")
    index.reindex(root)
    for _ in range(3):
        ledger.append(root, "read", organic.name)
    ledger.append(root, "search_hit", card.name, query="q", score=0.2)
    ledger.append(root, "feedback", card.name, verdict="solved")

    briefing = report.prime(root, task="vacuum")
    assert [p["filename"] for p in briefing["standing"]] == [standing.name]
    assert briefing["hot"] == [{"filename": organic.name, "hits": 3}]  # cards excluded
    assert briefing["skills"][0]["filename"] == card.name
    assert briefing["skills"][0]["helpful"] == 1
    assert len(briefing["task"]) == 3

    ledger.append(root, "prime", None, query="vacuum")
    assert report.stats(root)["primed_sessions_7d"] == 1


def test_demand_for_counts_prior_signals(root, monkeypatch):
    def fake(texts):
        return [[1.0, 0.0] if t.startswith("graphite") else [0.0, 1.0] for t in texts]

    monkeypatch.setattr(embed, "embed_texts", fake)
    ledger.append(root, "weak_search", None, query="graphite retry backoff values")
    ledger.append(root, "feedback", None, verdict="miss", note="graphite retry backoff table")
    ledger.append(root, "weak_search", None, query="unrelated other topic")

    demand = report.demand_for(root, "graphite retry backoff policy")
    assert demand == {"weak_search": 1, "miss": 1}
    assert report.demand_for(root, "zzz nothing similar") == {"weak_search": 1}


def test_feedback_in_stats_and_hot(root, fake_embed):
    a = corpus.new_page(root, "Alpha", "alpha topic")
    index.reindex(root)
    for _ in range(5):
        ledger.append(root, "read", a.name)
    ledger.append(root, "feedback", a.name, verdict="solved", note="fixed it")
    ledger.append(root, "feedback", a.name, verdict="unrelated")
    ledger.append(root, "feedback", None, verdict="miss", note="needed X")

    stats = report.stats(root)
    assert stats["recall_misses_30d"] == 1
    # rated = solved + unrelated = 2; helpful = solved = 1
    assert stats["helpful_rate_30d_pct"] == 50
    assert stats["feedback_30d"] == "miss=1 solved=1 unrelated=1"

    clusters = report.hot(root, window_days=30, min_hits=5)
    assert clusters[0][0]["feedback"] == {"solved": 1, "unrelated": 1}
