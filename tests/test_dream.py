"""Dreams: planning, digests, skills rules, and the runner against a stub agent."""

import json
import os
import plistlib
import subprocess
import sys
import textwrap
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from mem import corpus, embed, ledger, skillcards
from mem.dream import cli as dream_cli
from mem.dream import config, launchd, plan, prune, run, skills, state, transcripts

SESSION = "11111111-1111-4111-8111-111111111111"
HOUR = 3600


@pytest.fixture
def root(tmp_path, monkeypatch):
    r = tmp_path / "memories"
    (r / "meta").mkdir(parents=True)
    monkeypatch.setenv("MEM_ROOT", str(r))
    for var in ("MEM_SESSION", "CURSOR_CONVERSATION_ID", "TERM_SESSION_ID", "MEM_SESSION_KIND", "MEM_DREAMED_FROM"):
        monkeypatch.delenv(var, raising=False)
    # temp repos must not pick up the developer's global hooks or identity
    gitconfig = tmp_path / "gitconfig"
    gitconfig.write_text("[user]\n\tname = test\n\temail = test@example.com\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gitconfig))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setattr(embed, "embed_texts", lambda texts, truncate=True: [[1.0, 0.0] for _ in texts])
    return r


@pytest.fixture
def cfg(root, tmp_path):
    projects = tmp_path / "projects"
    chats = tmp_path / "chats"
    projects.mkdir()
    chats.mkdir()
    c = config.load(root)
    c.update(
        transcript_roots=[str(projects / "*" / "agent-transcripts")],
        chats_root=str(chats),
        skill_roots=[],
        min_new_chars=10,
        sandbox_config_dir=str(tmp_path / "agent-config"),
    )
    return c


def user_line(text: str, stamp: str = "Friday, Oct 23, 2026, 9:00 AM (UTC-4)", typed: bool = True) -> dict:
    body = f"<timestamp>{stamp}</timestamp>\n" + (f"<user_query>\n{text}\n</user_query>" if typed else text)
    return {"role": "user", "message": {"content": [{"type": "text", "text": body}]}}


def assistant_line(text: str, tool: dict | None = None) -> dict:
    content = [{"type": "text", "text": text}]
    if tool:
        content.append({"type": "tool_use", "name": tool["name"], "input": tool["input"]})
    return {"role": "assistant", "message": {"content": content}}


def write_transcript(cfg, session=SESSION, lines=None, project="proj", age_s=3 * HOUR, prompts=None):
    root = Path(cfg["transcript_roots"][0].replace("*", project))
    path = root / session / f"{session}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = lines or [
        user_line("why does the cache miss on cold start"),
        assistant_line("Because the warmup job skips tenants without traffic.", {"name": "Shell", "input": {"description": "Inspect warmup job"}}),
    ]
    path.write_text("".join(json.dumps(line) + "\n" for line in lines))
    then = time.time() - age_s
    os.utime(path, (then, then))
    if prompts is not None:
        chat = Path(cfg["chats_root"]) / "hash" / session
        chat.mkdir(parents=True, exist_ok=True)
        history = chat / "prompt_history.json"
        history.write_text(json.dumps(prompts))
        os.utime(history, (then, then))
    return path


def verdicts(root, cfg, **kw):
    return {c.session: c for c in plan.plan(root, cfg, **kw)}


# ---- transcripts ----------------------------------------------------------


def test_project_slug_keeps_the_path_as_given():
    assert transcripts.project_slug(Path("/tmp/mem/meta/dreams/workspace")) == "tmp-mem-meta-dreams-workspace"


def test_a_session_that_moved_workspaces_is_one_transcript(cfg):
    write_transcript(cfg, project="old", age_s=30 * HOUR)
    newer = write_transcript(cfg, project="new", age_s=3 * HOUR)
    write_transcript(cfg, session="other", project="old")
    found = transcripts.discover(cfg)
    assert sorted(t.session for t in found) == sorted([SESSION, "other"])
    assert next(t.path for t in found if t.session == SESSION) == newer


def test_parse_timestamp():
    when = transcripts.parse_timestamp("Friday, Oct 23, 2026, 12:46 PM (UTC-4)")
    assert (when.year, when.month, when.day, when.hour, when.minute) == (2026, 10, 23, 12, 46)
    assert when.utcoffset().total_seconds() == -4 * HOUR
    assert transcripts.parse_timestamp("Sunday, Jan 4, 2026, 12:05 AM (UTC+5:30)").hour == 0
    assert transcripts.parse_timestamp("not a time") is None


def test_digest_labels_users_injections_and_tools(cfg):
    path = write_transcript(cfg, lines=[
        user_line("fix the flaky test"),
        user_line("background shell finished", typed=False),
        assistant_line("x" * 50, {"name": "Read", "input": {"path": "/repo/a.py"}}),
    ])
    dg = transcripts.digest(path, 0, 10_000, max_chars_per_message=20)
    assert "[user Friday, Oct 23, 2026, 9:00 AM (UTC-4)]\nfix the flaky test" in dg.text
    assert "[injected" in dg.text
    assert "x" * 19 + "…" in dg.text
    assert "  - Read: /repo/a.py" in dg.text
    assert dg.end_offset == path.stat().st_size and not dg.truncated


def test_digest_truncates_but_always_makes_progress(cfg):
    path = write_transcript(cfg, lines=[user_line("a" * 500), user_line("b" * 500)])
    dg = transcripts.digest(path, 0, 100, max_chars_per_message=1000)
    assert dg.truncated and "a" * 50 in dg.text and "b" not in dg.text
    rest = transcripts.digest(path, dg.end_offset, 100, max_chars_per_message=1000)
    assert "b" * 50 in rest.text and rest.end_offset == path.stat().st_size


# ---- plan -----------------------------------------------------------------


def test_plan_dreams_on_a_quiet_session_with_new_material(root, cfg):
    write_transcript(cfg, prompts=["why does the cache miss on cold start"])
    c = verdicts(root, cfg)[SESSION]
    assert c.verdict == "dream", c.reason
    assert c.new_prompts == 1 and c.estimate > cfg["overhead_tokens"]


def test_plan_never_dreams_about_dreams(root, cfg):
    write_transcript(cfg, session="a" * 8)
    state.register(root, "dream", "a" * 8, SESSION)
    write_transcript(cfg, session="b" * 8, lines=[user_line(f"{transcripts.DREAM_MARKER}\nreflect")])
    write_transcript(cfg, session="c" * 8, project=transcripts.project_slug(plan.workspace_dir(root)))
    got = verdicts(root, cfg)
    assert {got[s].verdict for s in ("a" * 8, "b" * 8, "c" * 8)} == {"skip:dream"}


def test_plan_skips_subagents_old_and_locked(root, cfg):
    sub = Path(cfg["transcript_roots"][0].replace("*", "proj")) / SESSION / "subagents" / "sub.jsonl"
    write_transcript(cfg)
    sub.parent.mkdir()
    sub.write_text(json.dumps(user_line("child")) + "\n")
    write_transcript(cfg, session="old", age_s=10 * 24 * HOUR)
    got = verdicts(root, cfg)
    assert got["sub"].verdict == "skip:subagent"
    assert got["old"].verdict == "skip:old"
    with state.try_lock(root, plan.subject_lock(SESSION)) as held:
        assert held
        assert verdicts(root, cfg)[SESSION].verdict == "skip:locked"


def test_plan_waits_while_active_or_not_idle(root, cfg):
    write_transcript(cfg, age_s=60)
    assert verdicts(root, cfg)[SESSION].verdict == "wait:active"
    path = write_transcript(cfg, prompts=["q"])
    history = Path(cfg["chats_root"]) / "hash" / SESSION / "prompt_history.json"
    later = path.stat().st_mtime + 60
    os.utime(history, (later, later))
    assert verdicts(root, cfg)[SESSION].verdict == "wait:active"
    write_transcript(cfg, prompts=["q"])
    cfg["idle_hours"] = 5
    assert verdicts(root, cfg)[SESSION].verdict == "wait:idle"
    assert verdicts(root, cfg, only=SESSION)[SESSION].verdict == "dream"  # forced


def test_plan_watermark_skips_dreamed_and_resets_on_shrink(root, cfg):
    path = write_transcript(cfg, prompts=["q"])
    state.save_watermark(root, SESSION, state.Watermark(offset=path.stat().st_size, prompts=1))
    assert verdicts(root, cfg)[SESSION].verdict == "skip:dreamed"
    state.save_watermark(root, SESSION, state.Watermark(offset=10**9, prompts=1))
    assert verdicts(root, cfg)[SESSION].verdict == "dream"


def test_plan_thin_unless_something_to_prune(root, cfg, monkeypatch):
    cfg["min_new_chars"] = 10_000
    write_transcript(cfg, prompts=["q"])
    assert verdicts(root, cfg)[SESSION].verdict == "skip:thin"
    page = corpus.new_page(root, "Cache warmup", "warmup skips idle tenants")
    monkeypatch.setenv("MEM_SESSION", SESSION)
    ledger.append(root, "feedback", page.name, verdict="outdated", note="now warms all")
    c = verdicts(root, cfg)[SESSION]
    assert c.verdict == "dream" and c.prune == [f"outdated: {page.name}"]
    assert c.touched == [page.name]


def test_plan_defers_over_budget(root, cfg):
    write_transcript(cfg, prompts=["q"])
    state.log(root, {"event": "dream", "usage": {"inputTokens": cfg["daily_token_budget"]}})
    assert verdicts(root, cfg)[SESSION].verdict == "wait:budget"
    cfg.update(daily_token_budget=None, weekly_token_budget=None)  # no budget
    assert verdicts(root, cfg)[SESSION].verdict == "dream"


def test_budgets_count_every_token_a_run_reports(root, cfg):
    usage = {"inputTokens": 62, "outputTokens": 28078, "cacheReadTokens": 2580716, "cacheWriteTokens": 119105}
    assert state.usage_tokens(usage) == 2727961
    assert state.usage_tokens({"inputTokens": 5}) == 5 and state.usage_tokens(None) == 0
    state.log(root, {"event": "prune", "usage": usage})
    state.log(root, {"event": "dream", "usage": {"inputTokens": 1200, "outputTokens": 80}})
    assert state.tokens_spent(root, 24) == 2727961 + 1280
    # an estimate is in the same unit, so one run's estimate can be weighed against the budget
    assert state.estimate(cfg, 1000) == (cfg["overhead_tokens"] + 1000) * cfg["reread_factor"]
    assert state.over_budget(cfg, state.estimate(cfg, 1000), state.tokens_spent(root, 24), 0) is False
    cfg["daily_token_budget"] = 2727961 + 1280 + state.estimate(cfg, 1000) - 1
    assert state.over_budget(cfg, state.estimate(cfg, 1000), state.tokens_spent(root, 24), 0) is True


def test_try_lock_is_exclusive(root):
    with state.try_lock(root, "corpus") as first:
        with state.try_lock(root, "corpus") as second:
            assert first and not second
    assert not state.is_locked(root, "corpus")


# ---- skills ---------------------------------------------------------------


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout


@pytest.fixture
def skill_repo(root, cfg, tmp_path):
    repo = tmp_path / "ai"
    skills_root = repo / "skills"
    for name, lines in (("alpha", 20), ("beta", 20), ("mem", 5)):
        (skills_root / name).mkdir(parents=True)
        (skills_root / name / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: Use for {name}.\n---\n" + "".join(f"step {i}\n" for i in range(lines))
        )
    (repo / "README.md").write_text("readme\n")
    git(repo, "init", "-q")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "init")
    cfg["skill_roots"] = [str(skills_root)]
    return repo, skills_root


def settle(root, cfg, snaps, tmp_path):
    return skills.settle(root, cfg, snaps, workspace=tmp_path, session="dream-1", subject=SESSION, summary="fix")


def test_sandboxed_runs_hold_even_a_small_skill_edit(root, cfg, skill_repo, tmp_path):
    repo, skills_root = skill_repo
    snaps = skills.snapshot(cfg)
    assert skills.off_limits(snaps, cfg) == ["every skill folder: this run is sandboxed, so all skill changes are held"]
    md = skills_root / "alpha" / "SKILL.md"
    before = md.read_text()
    md.write_text(before.replace("step 3\n", "step 3, then check the cache\n"))
    outcome = settle(root, cfg, snaps, tmp_path)
    assert outcome.committed == [] and len(outcome.proposals) == 1
    [(_, fm, _)] = skills.load_proposals(root)
    assert fm["kind"] == "edit" and "sandboxed" in fm["reason"]
    assert md.read_text() == before
    assert git(repo, "log", "--oneline").count("\n") == 1


def test_small_skill_edit_is_committed_without_touching_other_work(root, cfg, skill_repo, tmp_path):
    repo, skills_root = skill_repo
    cfg["sandbox"] = False  # only full-access runs commit skill edits
    (repo / "README.md").write_text("owner's work in progress\n")
    snaps = skills.snapshot(cfg)
    md = skills_root / "alpha" / "SKILL.md"
    md.write_text(md.read_text().replace("step 3\n", "step 3, then check the cache\n"))
    outcome = settle(root, cfg, snaps, tmp_path)
    assert outcome.committed == ["alpha"] and not outcome.proposals
    assert "mem dream: alpha: fix" in git(repo, "log", "-1", "--format=%s")
    assert git(repo, "status", "--porcelain").strip() == "M README.md"


def test_held_changes_are_restored_and_proposed(root, cfg, skill_repo, tmp_path):
    repo, skills_root = skill_repo
    dirty = skills_root / "beta" / "SKILL.md"
    dirty.write_text(dirty.read_text() + "owner's edit\n")
    snaps = skills.snapshot(cfg)
    original = {p: p.read_bytes() for p in skills_root.rglob("*.md")}
    dirty.write_text(dirty.read_text() + "dream's edit\n")  # file had uncommitted edits
    (skills_root / "alpha" / "SKILL.md").write_text("all new\n")  # rewrite past the churn cap
    (skills_root / "gamma").mkdir()
    (skills_root / "gamma" / "SKILL.md").write_text("---\nname: gamma\n---\nnew\n")  # new skill
    (skills_root / "mem" / "SKILL.md").write_text("tweak\n")  # proposal-only
    (tmp_path / skills.NOTES_DIR).mkdir()
    (tmp_path / skills.NOTES_DIR / "gamma.md").write_text("Sessions keep re-deriving gamma.")

    outcome = settle(root, cfg, snaps, tmp_path)
    assert outcome.committed == []
    kinds = sorted((fm["skill"], fm["kind"]) for _, fm, _ in skills.load_proposals(root))
    assert kinds == [("alpha", "rewrite"), ("beta", "edit"), ("gamma", "new"), ("mem", "edit")]
    assert {p: p.read_bytes() for p in skills_root.rglob("*.md")} == original
    assert not (skills_root / "gamma").exists()
    gamma = [b for _, fm, b in skills.load_proposals(root) if fm["skill"] == "gamma"][0]
    assert "Sessions keep re-deriving gamma." in gamma
    assert git(repo, "log", "--oneline").count("\n") == 1


def test_accept_and_reject_proposals(root, cfg, skill_repo, tmp_path):
    repo, skills_root = skill_repo
    snaps = skills.snapshot(cfg)
    (skills_root / "gamma").mkdir()
    (skills_root / "gamma" / "SKILL.md").write_text("---\nname: gamma\n---\nnew\n")
    (skills_root / "beta").joinpath("SKILL.md").unlink()
    settle(root, cfg, snaps, tmp_path)
    new = [p for p, fm, _ in skills.load_proposals(root) if fm["kind"] == "new"][0]
    gone = [p for p, fm, _ in skills.load_proposals(root) if fm["kind"] == "delete"][0]

    skills.accept(root, new.stem)
    assert (skills_root / "gamma" / "SKILL.md").read_text().endswith("new\n")
    assert "gamma" in git(repo, "log", "-1", "--format=%s")
    skills.reject(root, gone.stem, "beta is still used")
    assert (skills_root / "beta" / "SKILL.md").exists()
    assert skills.rejected_summaries(root) == ["delete beta: deletes the skill (rejected: beta is still used)"]


def test_accept_leaves_the_commit_to_the_owner_when_the_file_has_their_edits(root, cfg, skill_repo, tmp_path):
    repo, skills_root = skill_repo
    md = skills_root / "beta" / "SKILL.md"
    md.write_text(md.read_text() + "owner's edit\n")
    snaps = skills.snapshot(cfg)
    md.write_text(md.read_text().replace("step 2\n", "step 2, with the dream's fix\n"))
    (tmp_path / skills.NOTES_DIR).mkdir()
    (tmp_path / skills.NOTES_DIR / "beta.md").write_text("Step 2 was missing the fix.")
    settle(root, cfg, snaps, tmp_path)
    [(proposal, fm, _)] = skills.load_proposals(root)
    assert "dream's fix" not in md.read_text()
    assert skills.open_summaries(root) == [f"edit beta ({proposal.stem}): Step 2 was missing the fix."]

    message = skills.accept(root, proposal.stem)
    assert "not committed" in message
    text = md.read_text()
    assert "owner's edit" in text and "dream's fix" in text
    assert git(repo, "log", "--oneline").count("\n") == 1
    assert skills.load_proposals(root)[0][1]["status"] == "accepted"
    assert skills.open_summaries(root) == []


def test_root_outside_git_is_proposal_only(root, cfg, tmp_path):
    loose = tmp_path / "loose"
    (loose / "alpha").mkdir(parents=True)
    (loose / "alpha" / "SKILL.md").write_text("one\ntwo\nthree\n")
    cfg["skill_roots"] = [str(loose)]
    snaps = skills.snapshot(cfg)
    (loose / "alpha" / "SKILL.md").write_text("one\ntwo\nthree\nfour\n")
    outcome = settle(root, cfg, snaps, tmp_path)
    assert outcome.committed == [] and len(outcome.proposals) == 1
    assert (loose / "alpha" / "SKILL.md").read_text() == "one\ntwo\nthree\n"


def test_skillcards_sync(root, skill_repo):
    _, skills_root = skill_repo
    assert skillcards.sync(root, skills_root) == (3, 0, 0)
    assert skillcards.sync(root, skills_root) == (0, 3, 0)


# ---- prune nominations ----------------------------------------------------


def days_ago(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def old_page(root, name, days=60, body="the pool has four nodes", **frontmatter) -> str:
    """Write a page created `days` ago and return its filename."""
    fm = {
        "title": name,
        "created": days_ago(days),
        "uuid": str(uuid.uuid4()),
        "volatility": "slow",
        **frontmatter,
    }
    (root / f"{name}.md").write_text(corpus.build_page(fm, body))
    return f"{name}.md"


def past_event(root, event, filename, days, **extra):
    """Append a ledger event dated `days` ago, which ledger.append cannot do."""
    record = {"ts": days_ago(days), "event": event, "filename": filename, **extra}
    with ledger.ledger_path(root).open("a") as f:
        f.write(json.dumps(record) + "\n")


def nominated(root, cfg):
    return [n.filename for n in prune.nominations(root, cfg)]


def test_prune_nominates_an_old_page_nobody_read(root, cfg):
    cold = old_page(root, "cold")
    past_event(root, "search_hit", cold, 3)
    past_event(root, "feedback", cold, 3, verdict="unrelated")
    [n] = prune.nominations(root, cfg)
    assert (n.filename, n.volatility, n.age_days, n.surfaced) == (cold, "slow", 60, 1)
    assert n.verdicts == {"unrelated": 1}
    assert n.evidence().startswith("slow, 60 days old") and "rated: unrelated=1" in n.evidence()


def test_prune_leaves_pages_that_are_new_in_use_or_protected(root, cfg):
    cold = old_page(root, "cold")
    old_page(root, "new", days=10)
    past_event(root, "read", old_page(root, "read"), 5)
    for verdict in ("solved", "partial", "context"):
        past_event(root, "feedback", old_page(root, f"rated-{verdict}"), 50, verdict=verdict)
    past_event(root, "prune", old_page(root, "judged"), 10, kind="prune", outcome="kept")
    old_page(root, "decision", volatility="stable")
    old_page(root, "standing", tags=["prime"])
    old_page(root, "card", tags=["skill-card"])
    fixture = old_page(root, "fixture")
    (root / "meta" / "evals.yaml").write_text(f"- query: pool size\n  expect: {fixture}\n")
    (root / "undated.md").write_text("---\ntitle: Undated\n---\n\nno created date\n")
    assert nominated(root, cfg) == [cold]


def test_prune_windows_end_one_day_past_their_length(root, cfg):
    assert cfg["prune_cold_days"] == 30 and cfg["prune_review_days"] == 90
    old_page(root, "aged-30", days=30)
    aged = old_page(root, "aged-31", days=31)
    past_event(root, "read", old_page(root, "read-30"), 30)
    read = old_page(root, "read-31")
    past_event(root, "read", read, 31)
    past_event(root, "prune", old_page(root, "judged-90", days=200), 90, kind="prune", outcome="kept")
    judged = old_page(root, "judged-91", days=200)
    past_event(root, "prune", judged, 91, kind="prune", outcome="kept")
    assert sorted(nominated(root, cfg)) == sorted([aged, read, judged])


def test_a_maintenance_read_does_not_keep_a_page_from_pruning(root, cfg):
    cold = old_page(root, "cold")
    for kind in ("dream", "consolidate", "prune"):
        past_event(root, "read", cold, 1, kind=kind)
        past_event(root, "feedback", cold, 1, kind=kind, verdict="solved")
    assert all(ledger.is_maintenance(e) for e in ledger.load(root))
    assert nominated(root, cfg) == [cold]


def test_prune_takes_fast_pages_first_then_the_oldest(root, cfg):
    slow_old = old_page(root, "slow-old", days=90)
    fast_young = old_page(root, "fast-young", days=40, volatility="fast")
    fast_old = old_page(root, "fast-old", days=80, volatility="fast")
    slow_young = old_page(root, "slow-young", days=40)
    assert nominated(root, cfg) == [fast_old, fast_young, slow_old, slow_young]


# ---- runner ---------------------------------------------------------------

STUB = textwrap.dedent(
    """\
    import json, os, sys, time, uuid
    from pathlib import Path
    if sys.argv[1] == "status":
        print("Not logged in" if os.environ.get("STUB_STATUS_LOGGED_OUT") else "✓ Logged in as test@example.com")
        sys.exit(1 if os.environ.get("STUB_STATUS_LOGGED_OUT") else 0)
    if sys.argv[1] == "create-chat":
        print(uuid.uuid4())
        sys.exit(0)
    if os.environ.get("STUB_AUTH_FAIL"):
        print("Error: Authentication required. Please run 'agent login' first.", file=sys.stderr)
        sys.exit(1)
    root = Path(os.environ["MEM_ROOT"])
    prompt = sys.argv[-1]
    calls = root.parent / "calls.jsonl"
    with calls.open("a") as f:
        f.write(json.dumps({"kind": os.environ.get("MEM_SESSION_KIND"),
                            "from": os.environ.get("MEM_DREAMED_FROM"),
                            "session": os.environ.get("MEM_SESSION"),
                            "config_dir": os.environ.get("CURSOR_CONFIG_DIR"),
                            "argv": sys.argv[1:-1], "prompt": prompt}) + "\\n")
    if os.environ.get("STUB_REQUEST") and os.environ.get("MEM_SESSION_KIND") == "dream":
        requests = Path.cwd() / "context-requests"
        requests.mkdir(exist_ok=True)
        (requests / "warmup-runbook.md").write_text("Read the warmup runbook\\n\\nIt lives in the ops wiki.\\n")
    if os.environ.get("MEM_SESSION_KIND") == "prune":
        acts = json.loads(os.environ.get("STUB_PRUNE", "{}"))
        for name in acts.get("delete", []):
            (root / name).unlink()
        for name in acts.get("edit", []):
            with (root / name).open("a") as f:
                f.write("tightened\\n")
        for name in acts.get("add", []):
            (root / name).write_text("---\\ntitle: Added\\n---\\n\\nwritten by a working session\\n")
        for name, text in acts.get("write", {}).items():
            (root / name).write_text(text)
    if os.environ.get("STUB_SLEEP"):
        time.sleep(float(os.environ["STUB_SLEEP"]))
    if os.environ.get("MEM_SESSION_KIND") == "dream":
        (root / "cache-warmup.md").write_text("---\\ntitle: Cache warmup\\n---\\n\\nwarmup skips idle tenants\\n")
    print(json.dumps({"type": "result", "is_error": False, "result": "work\\n\\nAdded cache warmup page",
                      "request_id": "req-1", "usage": {"inputTokens": 1200, "outputTokens": 80}}))
    """
)


@pytest.fixture
def stub(root, cfg, tmp_path):
    script = tmp_path / "stub_agent.py"
    script.write_text(STUB)
    cfg["agent_cmd"] = f"{sys.executable} {script}"
    git(root, "init", "-q")
    (root / "index.md").write_text("---\ntitle: Index\n---\n\nhome\n")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "init")
    return tmp_path / "calls.jsonl"


def test_run_dreams_commits_and_consolidates(root, cfg, stub):
    write_transcript(cfg, prompts=["why does the cache miss on cold start"])
    waiting = skills._write_proposal(
        root, {"status": "open", "kind": "new", "skill": "gamma", "reason": "a new skill"}, "Gamma keeps coming up.", ""
    )
    records = run.run(root, cfg, out=lambda *_: None)
    assert [r["event"] for r in records] == ["dream", "consolidate"]

    dream, consolidation = records
    assert dream["status"] == "ok" and dream["summary"] == "Added cache warmup page"
    assert dream["usage"] == {"inputTokens": 1200, "outputTokens": 80}
    assert dream["commit"] and consolidation["commit"] is None  # consolidation changed nothing
    assert "dream " + SESSION in git(root, "log", "--format=%s")

    calls = [json.loads(line) for line in stub.read_text().splitlines()]
    assert [(c["kind"], c["from"]) for c in calls] == [("dream", SESSION), ("consolidate", None)]
    assert transcripts.DREAM_MARKER in calls[0]["prompt"]
    assert SALIENCE in calls[0]["prompt"]
    assert f"new gamma ({waiting.stem}): Gamma keeps coming up." in calls[0]["prompt"]
    assert "cache-warmup.md" in calls[1]["prompt"]
    assert "--workspace" in calls[0]["argv"]
    assert {c["session"] for c in calls} <= state.registered_sessions(root)

    assert state.watermark(root, SESSION).commit == dream["commit"]
    assert state.consolidated(root)["commit"]
    assert state.tokens_spent(root, 24) == 2 * 1280
    assert verdicts(root, cfg)[SESSION].verdict == "skip:dreamed"


def test_summary_is_the_last_line_written_for_a_person():
    assert run._summary("work\n\nAdded cache warmup page") == "Added cache warmup page"
    assert run._summary("Pruned 3 pages\n\n{waiting-on:nothing}\n") == "Pruned 3 pages"
    assert run._summary("**Tightened two pages**\n```\n") == "Tightened two pages"
    assert run._summary("Fixed {braces} inside a sentence") == "Fixed {braces} inside a sentence"
    assert run._summary("{waiting-on:nothing}") == "no summary"
    assert run._summary("") == "no summary" and run._summary(None) == "no summary"
    clipped = run._summary("x" * 200)
    assert len(clipped) == run.SUMMARY_LIMIT and clipped.endswith("…")


def test_run_stops_before_dreaming_when_logged_out(root, cfg, stub, monkeypatch):
    write_transcript(cfg, prompts=["q"])
    monkeypatch.setenv("STUB_STATUS_LOGGED_OUT", "1")
    [record] = run.run(root, cfg, out=lambda *_: None)
    assert record["event"] == "auth" and "Not logged in" in record["error"]
    assert state.watermark(root, SESSION).offset == 0
    assert not stub.exists()


def test_a_dream_that_never_started_marks_nothing_and_stops_the_run(root, cfg, stub, monkeypatch):
    write_transcript(cfg, prompts=["q"])
    write_transcript(cfg, session="second", prompts=["q2"])
    head_before = git(root, "rev-parse", "HEAD")
    (root / "unrelated-edit.md").write_text("---\ntitle: x\n---\n\nleft by another session\n")
    monkeypatch.setenv("STUB_AUTH_FAIL", "1")
    records = run.run(root, cfg, out=lambda *_: None)
    assert [(r["event"], r["status"]) for r in records] == [("dream", "error")]
    assert "Authentication required" in records[0]["error"]
    assert state.watermark(root, records[0]["subject_session"]).offset == 0
    assert git(root, "rev-parse", "HEAD") == head_before  # nothing committed, no consolidation


def test_run_respects_pause_and_dry_run(root, cfg, stub):
    write_transcript(cfg, prompts=["q"])
    cfg["enabled"] = False
    assert run.run(root, cfg, out=lambda *_: None) == []
    cfg["enabled"] = True
    lines = []
    assert run.run(root, cfg, dry_run=True, out=lines.append) == []
    assert lines and lines[0].startswith("would dream")
    assert not stub.exists()


def test_run_kills_a_dream_at_its_timeout(root, cfg, stub, monkeypatch):
    write_transcript(cfg, prompts=["q"])
    monkeypatch.setenv("STUB_SLEEP", "30")
    cfg["timeout_minutes"] = 0.02
    started = time.monotonic()
    [dream] = run.run(root, cfg, consolidate_after=False, out=lambda *_: None)
    assert time.monotonic() - started < 20
    assert dream["status"] == "timeout"
    assert dream["usage"]["estimated"] and dream["usage"]["inputTokens"] == dream["estimate"]


# ---- prune runs -----------------------------------------------------------

SALIENCE = "Memory exists to save a later session labor"


def prune_events(root):
    return {(e["filename"], e["outcome"]) for e in ledger.load(root) if e["event"] == "prune"}


def test_prune_commits_once_and_records_each_outcome(root, cfg, stub, monkeypatch):
    gone, tight, fine = old_page(root, "gone"), old_page(root, "tight"), old_page(root, "fine")
    (root / "referrer.md").write_text(f"---\ntitle: Referrer\n---\n\nSee also: {gone}\n")
    monkeypatch.setenv("STUB_PRUNE", json.dumps({"delete": [gone], "edit": [tight]}))
    record = run.prune(root, cfg)

    assert record["status"] == "ok" and record["batch"] == 3
    assert (record["kept"], record["edited"], record["deleted"]) == (1, 1, [gone])
    assert record["restored"] == [] and record["dangling_links"] == 1
    assert dream_cli._prune_counts(record) == "kept 1, edited 1, deleted 1, dangling links 1: "
    assert git(root, "log", "--format=%s").splitlines()[0].startswith("prune ")
    assert git(root, "rev-list", "--count", "HEAD").strip() == "2"  # init, then one prune commit
    assert git(root, "status", "--porcelain") == ""
    assert not (root / gone).exists() and (root / tight).read_text().endswith("tightened\n")

    assert prune_events(root) == {(gone, "deleted"), (tight, "edited"), (fine, "kept")}
    events = [e for e in ledger.load(root) if e["event"] == "prune"]
    assert {e["session"] for e in events} == {record["session"]}
    assert all(ledger.is_maintenance(e) for e in events)
    assert nominated(root, cfg) == []  # judged pages are not asked about again

    [call] = [json.loads(line) for line in stub.read_text().splitlines()]
    assert call["kind"] == "prune" and call["session"] in state.registered_sessions(root)
    assert argv_values(call["argv"], "--add-dir") == [str(root)]  # no skill folders
    assert transcripts.DREAM_MARKER in call["prompt"] and SALIENCE in call["prompt"]
    assert all(f"- {name} (slow, 60 days old" in call["prompt"] for name in (gone, tight, fine))
    assert "skill folders" not in call["prompt"]


def test_prune_puts_back_a_page_deleted_outside_the_batch(root, cfg, stub, monkeypatch):
    cold = old_page(root, "cold")
    decision = old_page(root, "decision", volatility="stable")
    kept_bytes = (root / decision).read_bytes()
    monkeypatch.setenv("STUB_PRUNE", json.dumps({"delete": [cold, decision], "add": ["fresh.md"]}))
    record = run.prune(root, cfg)

    assert record["deleted"] == [cold] and record["restored"] == [decision]
    assert (root / decision).read_bytes() == kept_bytes
    assert (root / "fresh.md").exists()  # a working session may have written it mid-run
    assert prune_events(root) == {(cold, "deleted")}
    assert git(root, "status", "--porcelain") == ""


def test_prune_puts_back_the_identity_of_a_page_it_rewrote(root, cfg, stub, monkeypatch):
    miscopied, dropped, broken = old_page(root, "miscopied"), old_page(root, "dropped"), old_page(root, "broken")
    original = {name: (root / name).read_text() for name in (miscopied, dropped, broken)}
    identity = {
        name: {key: corpus.parse_frontmatter(text)[0][key] for key in ("uuid", "created")}
        for name, text in original.items()
    }
    uuid_line = f"uuid: {identity[dropped]['uuid']}\n"
    rewrites = {
        miscopied: original[miscopied]
        .replace(identity[miscopied]["uuid"], str(uuid.uuid4()))
        .replace(identity[miscopied]["created"], days_ago(1))
        .replace("the pool has four nodes", "four nodes"),
        dropped: original[dropped].replace(uuid_line, "").replace("the pool has four nodes", "four nodes"),
        broken: "four nodes, and no frontmatter\n",
    }
    assert uuid_line in original[dropped] and all(rewrites[name] != original[name] for name in rewrites)
    monkeypatch.setenv("STUB_PRUNE", json.dumps({"write": rewrites}))
    record = run.prune(root, cfg)

    assert sorted(record["identity_repaired"]) == sorted([miscopied, dropped, broken])
    for name in (miscopied, dropped):
        fm, body = corpus.parse_frontmatter((root / name).read_text())
        assert {key: fm[key] for key in ("uuid", "created")} == identity[name]
        assert body.strip() == "four nodes"  # the rewrite itself stands
    assert (root / broken).read_text() == original[broken]
    assert prune_events(root) == {(miscopied, "edited"), (dropped, "edited"), (broken, "kept")}
    assert "uuid or created repaired 3" in dream_cli._prune_counts(record)


def test_prune_reports_the_verified_markers_it_created(root, cfg, stub, monkeypatch):
    source = old_page(
        root, "source",
        body="Four nodes NOTE(verified:pool-count:2026-09-24). Scale-out stalls NOTE(unverified:scaleout).",
    )
    owner = old_page(root, "owner", volatility="stable", body="The fleet.")
    moved = (root / owner).read_text().replace(
        "The fleet.",
        "The fleet. Four nodes NOTE(verified:pool-count:2026-09-24). "
        "Scale-out stalls NOTE(verified:scaleout:2026-09-24).",
    )
    monkeypatch.setenv("STUB_PRUNE", json.dumps({"delete": [source], "write": {owner: moved}}))
    record = run.prune(root, cfg)

    # the claim moved with its marker is fine; the one promoted on the way is reported
    assert record["new_verified"] == [f"{owner}: NOTE(verified:scaleout:2026-09-24)"]
    assert record["deleted"] == [source] and record["identity_repaired"] == []
    assert "new verified markers 1" in dream_cli._prune_counts(record)


def test_a_prune_that_stops_early_leaves_untouched_pages_unjudged(root, cfg, stub, monkeypatch):
    tight, unseen = old_page(root, "tight"), old_page(root, "unseen")
    monkeypatch.setenv("STUB_PRUNE", json.dumps({"edit": [tight]}))
    monkeypatch.setenv("STUB_SLEEP", "30")
    cfg["consolidate_timeout_minutes"] = 0.02
    record = run.prune(root, cfg)

    assert record["status"] == "timeout" and record["commit"]
    assert prune_events(root) == {(tight, "edited")}
    assert nominated(root, cfg) == [unseen]


def test_prune_uses_its_own_model_when_one_is_set(root, cfg, stub):
    cfg.update(model="dream-model", prune_batch_pages=1)
    old_page(root, "first", days=90)
    old_page(root, "second")
    assert run.prune(root, cfg)["model"] == "dream-model"
    cfg["prune_model"] = "prune-model"
    assert run.prune(root, cfg)["model"] == "prune-model"
    calls = [json.loads(line) for line in stub.read_text().splitlines()]
    assert [argv_values(c["argv"], "--model") for c in calls] == [["dream-model"], ["prune-model"]]


def test_prune_skips_or_defers_before_spending_anything(root, cfg, stub):
    assert run.prune(root, cfg)["reason"] == "nothing nominated"
    old_page(root, "cold")
    state.log(root, {"event": "dream", "usage": {"inputTokens": cfg["daily_token_budget"]}})
    assert run.prune(root, cfg)["status"] == "deferred"
    assert not stub.exists()
    assert run.prune(root, cfg, force=True)["status"] == "ok"


def test_prune_refuses_a_corpus_it_could_not_undo_in(root, cfg):
    old_page(root, "cold")
    record = run.prune(root, cfg)
    assert record["status"] == "skip" and "not a git repository" in record["reason"]


def test_a_scheduled_run_prunes_only_when_switched_on(root, cfg, stub):
    cold = old_page(root, "cold")
    assert cfg["prune"] is False
    assert run.run(root, cfg, out=lambda *_: None) == []
    assert not stub.exists()

    cfg.update(prune=True, prune_batch_pages=1)
    old_page(root, "colder", days=90)
    [record] = run.run(root, cfg, out=lambda *_: None)
    assert record["event"] == "prune" and record["batch"] == 1 and record["kept"] == 1
    assert nominated(root, cfg) == [cold]  # the older page went first


# ---- sandbox --------------------------------------------------------------


def argv_values(argv, flag):
    return [argv[i + 1] for i, a in enumerate(argv) if a == flag]


def test_dreams_run_sandboxed_by_default(root, cfg, stub):
    transcript = write_transcript(cfg, prompts=["q"])
    run.run(root, cfg, consolidate_after=False, out=lambda *_: None)
    [call] = [json.loads(line) for line in stub.read_text().splitlines()]

    assert "--force" not in call["argv"]
    assert argv_values(call["argv"], "--sandbox") == ["enabled"]
    assert argv_values(call["argv"], "--add-dir") == [str(root)]  # the transcript is read-only
    assert call["config_dir"] == cfg["sandbox_config_dir"]
    assert "context-requests" in call["prompt"]

    workspace = plan.workspace_dir(root)
    policy = json.loads((workspace / ".cursor" / "sandbox.json").read_text())
    assert policy["readBoundary"] == "workspace"
    assert str(transcript.parent) in policy["additionalReadPaths"]
    assert policy["additionalReadwritePaths"] == [str(root)]
    assert policy["networkPolicy"]["default"] == "deny" and "127.0.0.1" in policy["networkPolicy"]["allow"]

    cli_config = json.loads((Path(cfg["sandbox_config_dir"]) / "cli-config.json").read_text())
    assert cli_config["approvalMode"] == "allowlist" and cli_config["sandbox"]["mode"] == "enabled"
    assert cli_config["permissions"]["allow"] == [f"Write({root}/**)"]
    assert cli_config["permissions"]["deny"] == [f"Write({workspace / '.cursor'}/**)", "WebFetch(*)", "Mcp(*:*)"]


def test_sandbox_off_runs_with_full_access(root, cfg, stub):
    transcript = write_transcript(cfg, prompts=["q"])
    cfg["sandbox"] = False
    [dream] = run.run(root, cfg, consolidate_after=False, out=lambda *_: None)
    [call] = [json.loads(line) for line in stub.read_text().splitlines()]
    assert "--force" in call["argv"] and argv_values(call["argv"], "--sandbox") == ["disabled"]
    assert str(transcript.parent) in argv_values(call["argv"], "--add-dir")
    assert call["config_dir"] is None
    assert "Outside context" not in call["prompt"]
    assert dream["sandbox"] is False


def test_sandbox_refuses_a_config_dir_the_dream_could_write(root, cfg):
    from mem.dream import sandbox

    cfg["sandbox_config_dir"] = str(root / "meta" / "agent-config")
    with pytest.raises(RuntimeError, match="inside a root the dream can write"):
        sandbox.prepare(cfg, plan.workspace_dir(root), writable=[root], readable=[])


def test_context_requests_become_proposals(root, cfg, stub, monkeypatch):
    write_transcript(cfg, prompts=["q"])
    monkeypatch.setenv("STUB_REQUEST", "1")
    [dream] = run.run(root, cfg, consolidate_after=False, out=lambda *_: None)

    [(path, fm, body)] = skills.load_proposals(root)
    assert fm["kind"] == "context" and fm["status"] == "open"
    assert fm["reason"] == "Read the warmup runbook" and "ops wiki" in body
    assert dream["context_requests"] == [path.name]
    assert not any((plan.workspace_dir(root) / skills.REQUESTS_DIR).iterdir())
    assert skills.open_summaries(root) == [f"context ({path.stem}): Read the warmup runbook"]

    assert skills.accept(root, path.stem) == f"{path.stem}: marked picked up"
    assert skills.load_proposals(root)[0][1]["status"] == "accepted"


# ---- launchd and config ---------------------------------------------------


def test_plist_render(root, cfg):
    data = plistlib.loads(launchd.render(root, cfg, "/bin/mem", "/usr/bin:/bin"))
    assert data["Label"] == cfg["launchd_label"]
    assert data["ProgramArguments"] == ["/bin/mem", "dream", "run"]
    assert "StartInterval" not in data  # its firings are dropped while the Mac sleeps
    assert data["StartCalendarInterval"] == [{"Hour": 4, "Minute": 0}]
    assert data["EnvironmentVariables"]["MEM_ROOT"] == str(root)
    assert data["EnvironmentVariables"]["PYTHONUNBUFFERED"] == "1"


def test_schedule_aligns_to_run_at_and_rejects_bad_settings(cfg):
    cfg.update(interval_minutes=360, run_at="03:30")
    assert launchd.schedule(cfg) == [
        {"Hour": 3, "Minute": 30}, {"Hour": 9, "Minute": 30}, {"Hour": 15, "Minute": 30}, {"Hour": 21, "Minute": 30}
    ]
    cfg.update(interval_minutes=30, run_at="00:10")
    assert len(launchd.schedule(cfg)) == 48 and launchd.schedule(cfg)[0] == {"Hour": 0, "Minute": 10}
    for interval, at in ((7, "04:00"), (2880, "04:00"), (0, "04:00"), (1440, "4am"), (1440, "25:00")):
        cfg.update(interval_minutes=interval, run_at=at)
        with pytest.raises(RuntimeError):
            launchd.schedule(cfg)


def test_config_set_keeps_types_and_rejects_unknown_keys(root):
    assert config.set_value(root, "idle_hours", "3.5") == 3.5
    assert config.set_value(root, "skill_roots", "[~/a, ~/b]") == ["~/a", "~/b"]
    assert config.set_value(root, "agent_status_args", "null") is None
    assert config.load(root)["idle_hours"] == 3.5
    assert config.load(root)["agent_status_args"] is None
    loaded = config.load(root)
    loaded["agent_run_args"].append("--extra")
    assert "--extra" not in config.DEFAULTS["agent_run_args"]
    with pytest.raises(KeyError):
        config.set_value(root, "nope", "1")
    assert "!config.yaml" in (config.state_dir(root) / ".gitignore").read_text()


def test_default_agent_argv_is_the_cursor_cli(cfg):
    argv = run.agent_argv(
        cfg,
        session="s",
        workspace=Path("/w"),
        prompt="do the thing",
        add_dirs=[Path("/a")],
        launch_flags=["--sandbox", "enabled"],
    )
    assert argv[:2] == ["agent", "--resume"]
    assert argv[argv.index("--workspace") + 1] == "/w"
    assert argv[argv.index("--sandbox") + 1] == "enabled"
    assert "--force" not in argv
    assert argv[argv.index("--add-dir") + 1] == "/a"
    assert argv[-1] == "do the thing"
    with_model = run.agent_argv(
        cfg,
        session="s",
        workspace=Path("/w"),
        prompt="p",
        add_dirs=[],
        launch_flags=["--force", "--sandbox", "disabled"],
        model="gpt",
    )
    assert with_model[with_model.index("--model") + 1] == "gpt"
    assert with_model[with_model.index("--sandbox") + 1] == "disabled"


def test_agent_argv_follows_the_configured_executable(cfg):
    cfg["agent_cmd"] = "pi"
    cfg["agent_run_args"] = ["-p", "--mode", "json", "--session-id", "{session}", "{prompt}"]
    cfg["agent_add_dir_flag"] = None
    argv = run.agent_argv(
        cfg,
        session="abc",
        workspace=Path("/w"),
        prompt="hello",
        add_dirs=[Path("/a")],
        launch_flags=["--sandbox", "enabled"],
    )
    assert argv == ["pi", "-p", "--mode", "json", "--session-id", "abc", "hello"]


def test_interpret_output_accepts_cursor_pi_and_plain_text():
    status, summary, usage, request_id, error = run.interpret_output(
        json.dumps({
            "type": "result", "is_error": False, "result": "Added a page",
            "request_id": "req-1", "usage": {"inputTokens": 3, "outputTokens": 1},
        }),
        "",
        0,
    )
    assert (status, summary, usage, request_id, error) == (
        "ok", "Added a page", {"inputTokens": 3, "outputTokens": 1}, "req-1", ""
    )

    pi = "\n".join([
        json.dumps({"type": "message_end", "message": {
            "role": "assistant", "stopReason": "toolUse",
            "content": [{"type": "text", "text": "looking"}],
            "usage": {"input": 4, "output": 1},
        }}),
        json.dumps({"type": "message_end", "message": {
            "role": "assistant", "stopReason": "stop",
            "content": [{"type": "text", "text": "Pi wrote it"}],
            "usage": {"input": 6, "output": 2},
        }}),
    ])
    status, summary, usage, _, _ = run.interpret_output(pi, "", 0)
    assert status == "ok" and summary == "Pi wrote it"
    assert usage == {"inputTokens": 10, "outputTokens": 3}

    status, summary, usage, _, error = run.interpret_output("noise\nplain summary\n", "", 0)
    assert (status, summary, usage, error) == ("ok", "plain summary", {}, "")
    status, _, _, _, error = run.interpret_output("", "boom", 1)
    assert status == "error" and error == "boom"


def test_run_uses_a_configured_executable_without_cursor_flags(root, cfg, stub):
    cfg["sandbox"] = False
    cfg["agent_status_args"] = None
    cfg["agent_create_args"] = None
    cfg["agent_run_args"] = ["--print", "{prompt}"]
    write_transcript(cfg, prompts=["why does the cache miss on cold start"])
    records = run.run(root, cfg, out=lambda *_: None)
    assert records[0]["status"] == "ok"
    calls = [json.loads(line) for line in stub.read_text().splitlines()]
    assert calls[0]["argv"] == ["--print"]
    assert calls[0]["session"] in state.registered_sessions(root)


def test_setup_prompt_shows_how_to_set_the_agent_executable():
    from mem.cli import NEXT_STEPS

    assert "mem dream config set agent_cmd" in NEXT_STEPS
    assert "agent_run_args" in NEXT_STEPS
    assert "agent_status_args null" in NEXT_STEPS
