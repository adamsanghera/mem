"""Dreams: planning, digests, skills rules, and the runner against a stub agent."""

import json
import os
import plistlib
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from mem import corpus, embed, ledger, skillcards
from mem.dream import config, launchd, plan, run, skills, state, transcripts

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


def test_small_skill_edit_is_committed_without_touching_other_work(root, cfg, skill_repo, tmp_path):
    repo, skills_root = skill_repo
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
                            "argv": sys.argv[1:-1], "prompt": prompt}) + "\\n")
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
    assert f"new gamma ({waiting.stem}): Gamma keeps coming up." in calls[0]["prompt"]
    assert "cache-warmup.md" in calls[1]["prompt"]
    assert "--workspace" in calls[0]["argv"]
    assert {c["session"] for c in calls} <= state.registered_sessions(root)

    assert state.watermark(root, SESSION).commit == dream["commit"]
    assert state.consolidated(root)["commit"]
    assert state.tokens_spent(root, 24) == 2 * 1280
    assert verdicts(root, cfg)[SESSION].verdict == "skip:dreamed"


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


# ---- launchd and config ---------------------------------------------------


def test_plist_render(root, cfg):
    data = plistlib.loads(launchd.render(root, cfg, "/bin/mem", "/usr/bin:/bin"))
    assert data["Label"] == cfg["launchd_label"]
    assert data["ProgramArguments"] == ["/bin/mem", "dream", "run"]
    assert data["StartInterval"] == cfg["interval_minutes"] * 60
    assert data["EnvironmentVariables"]["MEM_ROOT"] == str(root)
    assert data["EnvironmentVariables"]["PYTHONUNBUFFERED"] == "1"


def test_config_set_keeps_types_and_rejects_unknown_keys(root):
    assert config.set_value(root, "idle_hours", "3.5") == 3.5
    assert config.set_value(root, "skill_roots", "[~/a, ~/b]") == ["~/a", "~/b"]
    assert config.load(root)["idle_hours"] == 3.5
    with pytest.raises(KeyError):
        config.set_value(root, "nope", "1")
    assert "!config.yaml" in (config.state_dir(root) / ".gitignore").read_text()
