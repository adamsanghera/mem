"""Unit tests for the pieces that don't need ollama: corpus rules, ledger,
report math, and the reindex change-detection logic (with a fake embedder).
"""

import json
import time

import pytest

from mem import corpus, embed, index, ledger, report


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setenv("MEM_ROOT", str(tmp_path))
    (tmp_path / "meta").mkdir()
    return tmp_path


@pytest.fixture
def fake_embed(monkeypatch):
    calls = []

    def fake(texts):
        calls.append(texts)
        # deterministic tiny vectors keyed off text length
        return [[float(len(t) % 7), 1.0, 0.5] for t in texts]

    monkeypatch.setattr(embed, "embed_texts", fake)
    return calls


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
    # spec: datetimes must be quoted strings, not YAML timestamps
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
