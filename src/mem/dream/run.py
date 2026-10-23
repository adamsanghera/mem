"""Running dreams and the consolidation after them.

One scheduler wake holds the corpus lock throughout, so dreams run one at a
time and never overlap a consolidation. Each run is its own headless agent
session, created with `create-chat` first so its id is registered as
do-not-reflect before it writes anything. The runner, not the agent, settles
skill changes, reindexes, and commits, so every run is one revertible commit.

The agent CLI has no token cap, so cost is bounded from outside: the plan's
estimate gates each run against the budgets, a wall-clock timeout kills a
runaway, and the usage each run reports is charged to the log afterwards.
"""

import json
import os
import shlex
import signal
import string
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime
from importlib import resources
from pathlib import Path

from .. import corpus, index, ledger, skillcards
from . import config, plan, skills, state, transcripts

CORPUS_LOCK = "corpus"
SUMMARY_LIMIT = 70


@dataclass
class AgentResult:
    session: str
    status: str  # ok | error | timeout
    summary: str = ""
    usage: dict = field(default_factory=dict)
    request_id: str | None = None
    seconds: float = 0.0
    error: str = ""


def _agent_cmd(cfg: dict) -> list[str]:
    return shlex.split(cfg["agent_cmd"])


def create_chat(cfg: dict, cwd: Path) -> str:
    out = subprocess.run(
        [*_agent_cmd(cfg), "create-chat"], cwd=cwd, capture_output=True, text=True, timeout=60
    )
    chat = out.stdout.strip().splitlines()[-1].strip() if out.stdout.strip() else ""
    if out.returncode != 0 or not chat:
        raise RuntimeError(f"agent create-chat failed: {out.stderr.strip()[:300]}")
    return chat


def _summary(text: str) -> str:
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    last = lines[-1].strip("*_` ") if lines else "no summary"
    return last if len(last) <= SUMMARY_LIMIT else last[: SUMMARY_LIMIT - 1] + "…"


def run_agent(
    cfg: dict,
    *,
    session: str,
    prompt: str,
    workspace: Path,
    add_dirs: list[Path],
    env: dict[str, str],
    timeout_s: float,
) -> AgentResult:
    cmd = [
        *_agent_cmd(cfg),
        "--resume", session,
        "-p", "--output-format", "json",
        "--trust", "--force", "--sandbox", "disabled",
        "--workspace", str(workspace),
    ]
    if cfg.get("model"):
        cmd += ["--model", str(cfg["model"])]
    for d in add_dirs:
        cmd += ["--add-dir", str(d)]
    cmd.append(prompt)
    started = time.monotonic()
    proc = subprocess.Popen(
        cmd,
        cwd=workspace,
        env={**os.environ, **env},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        # the agent spawns shells of its own; take down the whole group
        os.killpg(proc.pid, signal.SIGTERM)
        try:
            proc.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.communicate()
        return AgentResult(session, "timeout", seconds=time.monotonic() - started)
    seconds = time.monotonic() - started
    result = None
    for line in reversed(stdout.splitlines()):
        try:
            parsed = json.loads(line)
        except ValueError:
            continue
        if isinstance(parsed, dict) and parsed.get("type") == "result":
            result = parsed
            break
    if result is None:
        return AgentResult(session, "error", seconds=seconds, error=(stderr or stdout).strip()[-500:])
    return AgentResult(
        session,
        "error" if result.get("is_error") else "ok",
        summary=_summary(result.get("result") or ""),
        usage=result.get("usage") or {},
        request_id=result.get("request_id"),
        seconds=seconds,
    )


def _git(r: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(r), *args], capture_output=True, text=True)


def head(r: Path) -> str | None:
    out = _git(r, "rev-parse", "HEAD")
    return out.stdout.strip() if out.returncode == 0 else None


def commit_corpus(r: Path, message: str) -> str | None:
    """Commit everything in the corpus, including uncommitted edits other
    sessions left behind, as the consolidation commit always has."""
    if not (r / ".git").exists():
        return None
    _git(r, "add", "-A")
    if _git(r, "diff", "--cached", "--quiet").returncode == 0:
        return None
    if _git(r, "commit", "-q", "-m", message).returncode != 0:
        return None
    return head(r)


def _template(name: str) -> string.Template:
    return string.Template(
        resources.files("mem.dream").joinpath("prompts", name).read_text(encoding="utf-8")
    )


def _bullets(items: list[str], empty: str = "none") -> str:
    return "\n".join(f"   - {i}" for i in items) if items else empty


def _skill_context(r: Path, cfg: dict, snaps, workspace: Path) -> dict:
    held = skills.off_limits(snaps, cfg) + [
        f"{s.root / name}/ (proposal-only)" for s in snaps for name in cfg["skill_proposal_only"]
    ]
    rejected = skills.rejected_summaries(r)
    pending = skills.open_summaries(r)
    return {
        "skill_roots": ", ".join(str(s.root) for s in snaps) or "none",
        "notes_dir": str(workspace / skills.NOTES_DIR),
        "held": "\n" + _bullets(held) if held else "none",
        "pending": "\n" + _bullets(pending) if pending else "none",
        "rejected": "\n" + _bullets(rejected) if rejected else "none",
    }


def _finish(
    r: Path,
    cfg: dict,
    snaps,
    result: AgentResult,
    *,
    workspace: Path,
    subject: str | None,
    message: str,
) -> dict:
    """Settle skills, sync cards, reindex, and commit. Returns log fields."""
    outcome = skills.settle(
        r, cfg, snaps, workspace=workspace, session=result.session, subject=subject,
        summary=result.summary or "no summary",
    )
    if outcome.committed:
        # cards cite the configured path (~/.cursor/skills/...), the one
        # agents load skills from, not where a symlink resolves
        for raw in cfg["skill_roots"]:
            skillcards.sync(r, config.expand(raw))
    # the index is a cache: if ollama is down, commit anyway and let the
    # next reindex catch up
    try:
        index.reindex(r)
        reindexed = True
    except (RuntimeError, OSError):
        reindexed = False
    before = head(r)
    commit = commit_corpus(r, message)
    stat = _git(r, "diff", "--numstat", before, commit) if before and commit else None
    pages = sum(1 for line in (stat.stdout.splitlines() if stat else []) if line.endswith(".md"))
    return {
        "commit": commit,
        "pages_changed": pages,
        "skills_committed": outcome.committed,
        "proposals": [p.name for p in outcome.proposals],
        "reindexed": reindexed,
    }


def _record(result: AgentResult, estimate: int) -> dict:
    # a killed run reports no usage, so charge its estimate
    usage = result.usage or ({"inputTokens": estimate, "estimated": True} if result.status == "timeout" else {})
    return {
        "session": result.session,
        "status": result.status,
        "request_id": result.request_id,
        "usage": usage,
        "seconds": round(result.seconds, 1),
        "summary": result.summary,
        **({"error": result.error} if result.error else {}),
    }


def dream_one(r: Path, cfg: dict, cand: plan.Candidate) -> dict:
    subject = cand.session
    workspace = plan.workspace_dir(r)
    digests = workspace / "digests"
    digests.mkdir(exist_ok=True)
    digest_path = digests / f"{subject}.md"
    digest_path.write_text(cand.digest.text if cand.digest else "", encoding="utf-8")

    snaps = skills.snapshot(cfg)
    session = create_chat(cfg, workspace)
    state.register(r, "dream", session, subject)
    prompt = _template("dream.md").substitute(
        marker=transcripts.DREAM_MARKER,
        subject=subject,
        transcript=cand.transcript.path,
        digest_path=digest_path,
        truncated_note=(
            "The digest stops early because the session is long; a later dream picks up the rest.\n"
            if cand.digest and cand.digest.truncated
            else ""
        ),
        mem_root=r,
        touched=", ".join(cand.touched) or "none",
        prune="; ".join(cand.prune) or "none",
        **_skill_context(r, cfg, snaps, workspace),
    )
    result = run_agent(
        cfg,
        session=session,
        prompt=prompt,
        workspace=workspace,
        add_dirs=[r, cand.transcript.path.parent, *[s.root for s in snaps]],
        env={
            "MEM_ROOT": str(r),
            "MEM_SESSION": session,
            "MEM_SESSION_KIND": "dream",
            "MEM_DREAMED_FROM": subject,
        },
        timeout_s=cfg["timeout_minutes"] * 60,
    )
    finished = _finish(
        r, cfg, snaps, result, workspace=workspace, subject=subject,
        message=f"dream {subject}: {result.summary or result.status}",
    )
    # advance even after a failure, so a session that breaks dreams is not
    # retried, and charged, every run. A truncated digest keeps the old prompt
    # count, so the rest of the session still reads as new next time.
    truncated = bool(cand.digest and cand.digest.truncated)
    state.save_watermark(
        r,
        subject,
        state.Watermark(
            offset=cand.digest.end_offset if cand.digest else cand.start_offset,
            prompts=state.watermark(r, subject).prompts if truncated else cand.prompts_total,
            dreamed_at=corpus.now_iso(),
            commit=finished["commit"],
        ),
    )
    record = {
        "event": "dream",
        "subject_session": subject,
        "estimate": cand.estimate,
        "digest_chars": len(cand.digest.text) if cand.digest else 0,
        **_record(result, cand.estimate),
        **finished,
    }
    state.log(r, record)
    return record


def consolidation_scope(r: Path, since_commit: str | None) -> list[str]:
    """Pages changed since the last consolidation, pages with outdated or
    partial verdicts since then, and pages past the embedding budget."""
    last = state.consolidated(r)
    since = last.get("commit") or since_commit
    scope: set[str] = set()
    if since:
        out = _git(r, "diff", "--name-only", since, "HEAD")
        scope |= {
            name for name in out.stdout.split() if "/" not in name and name.endswith(".md")
        }
    since_ts = last.get("ts")
    for e in ledger.load(r):
        if (
            e["event"] == "feedback"
            and e.get("verdict") in plan.PRUNE_VERDICTS
            and e.get("filename")
            and (since_ts is None or e.get("ts", "") > since_ts)
        ):
            scope.add(e["filename"])
    for page in corpus.pages(r):
        if page.stat().st_size > corpus.EMBED_BYTE_BUDGET:
            scope.add(page.name)
    return sorted(name for name in scope if (r / name).is_file())


def _skill_hint(cfg: dict) -> str:
    for raw in cfg["skill_roots"]:
        path = config.expand(raw) / "mem-consolidate" / "SKILL.md"
        if path.is_file():
            return f" ({path})"
    return ""


def consolidate(r: Path, cfg: dict, since_commit: str | None, force: bool = False) -> dict | None:
    scope = consolidation_scope(r, since_commit)
    if not scope:
        record = {"event": "consolidate", "status": "skip", "reason": "nothing changed"}
        state.log(r, record)
        return record
    estimate = (
        cfg["overhead_tokens"]
        + cfg["consolidate_base_tokens"]
        + sum((r / name).stat().st_size for name in scope) // 4
    )
    over_budget = (
        state.tokens_spent(r, 24) + estimate > cfg["daily_token_budget"]
        or state.tokens_spent(r, 24 * 7) + estimate > cfg["weekly_token_budget"]
    )
    if not force and (estimate > cfg["max_input_tokens_per_consolidation"] or over_budget):
        record = {"event": "consolidate", "status": "deferred", "estimate": estimate, "scope": len(scope)}
        state.log(r, record)
        return record

    workspace = plan.workspace_dir(r)
    snaps = skills.snapshot(cfg)
    session = create_chat(cfg, workspace)
    state.register(r, "consolidate", session)
    stamp = datetime.now().strftime("%Y-%m-%d-%H%M")
    prompt = _template("consolidate.md").substitute(
        marker=transcripts.DREAM_MARKER,
        mem_root=r,
        skill_hint=_skill_hint(cfg),
        scope=_bullets(scope),
        digest_file=corpus.meta_dir(r) / f"consolidation-{stamp}.md",
        **_skill_context(r, cfg, snaps, workspace),
    )
    result = run_agent(
        cfg,
        session=session,
        prompt=prompt,
        workspace=workspace,
        add_dirs=[r, *[s.root for s in snaps]],
        env={"MEM_ROOT": str(r), "MEM_SESSION": session, "MEM_SESSION_KIND": "consolidate"},
        timeout_s=cfg["consolidate_timeout_minutes"] * 60,
    )
    finished = _finish(
        r, cfg, snaps, result, workspace=workspace, subject=None,
        message=f"consolidate {stamp}: {result.summary or result.status}",
    )
    current = head(r)
    if current:
        state.save_consolidated(r, current)
    record = {
        "event": "consolidate",
        "scope": len(scope),
        "estimate": estimate,
        **_record(result, estimate),
        **finished,
    }
    state.log(r, record)
    return record


def run(
    r: Path,
    cfg: dict,
    *,
    session: str | None = None,
    dry_run: bool = False,
    consolidate_after: bool = True,
    out=print,
) -> list[dict]:
    """One scheduler wake. Returns the log records it wrote."""
    if not cfg["enabled"] and session is None:
        out("dreams are paused (mem dream resume)")
        return []
    with state.try_lock(r, CORPUS_LOCK) as held:
        if not held:
            out("another dream or consolidation is running")
            return []
        cands = plan.plan(r, cfg, only=session)
        chosen = [c for c in cands if c.verdict == "dream"][: cfg["max_dreams_per_run"]]
        deferred = [c.session for c in cands if c.verdict == "wait:budget"]
        if dry_run:
            for c in chosen:
                out(f"would dream {c.session}: {c.reason} (~{c.estimate} tokens)")
            return []
        records: list[dict] = []
        if deferred:
            records.append({"event": "deferred", "sessions": deferred})
            state.log(r, records[-1])
        baseline = head(r)
        for cand in chosen:
            spent = state.tokens_spent(r, 24)
            if session is None and spent + cand.estimate > cfg["daily_token_budget"]:
                break
            with state.try_lock(r, plan.subject_lock(cand.session)) as mine:
                if not mine:
                    continue
                out(f"dreaming {cand.session} (~{cand.estimate} tokens)")
                try:
                    records.append(dream_one(r, cfg, cand))
                except RuntimeError as e:
                    # create-chat failing (logged out, CLI missing) fails every dream alike
                    records.append({"event": "dream", "subject_session": cand.session, "status": "error", "error": str(e)})
                    state.log(r, records[-1])
                    out(f"  error: {e}")
                    break
                out(f"  {records[-1]['status']}: {records[-1].get('summary') or ''}")
        if consolidate_after and cfg["consolidate_after_dreams"] and any(
            rec.get("event") == "dream" and rec.get("commit") for rec in records
        ):
            out("consolidating")
            try:
                rec = consolidate(r, cfg, baseline)
            except RuntimeError as e:
                rec = {"event": "consolidate", "status": "error", "error": str(e)}
                state.log(r, rec)
            if rec:
                records.append(rec)
                out(f"  {rec.get('status')}: {rec.get('summary') or rec.get('reason') or ''}")
        return records


def consolidate_now(r: Path, cfg: dict, force: bool, out=print) -> dict | None:
    with state.try_lock(r, CORPUS_LOCK) as held:
        if not held:
            out("another dream or consolidation is running")
            return None
        try:
            rec = consolidate(r, cfg, None, force=force)
        except RuntimeError as e:
            rec = {"event": "consolidate", "status": "error", "error": str(e)}
            state.log(r, rec)
        out(json.dumps(rec, indent=2))
        return rec
