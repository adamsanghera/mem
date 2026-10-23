"""The skip check: which quiet sessions are worth a dream. Reads files only.

Each transcript gets one verdict, from the first check that decides it:
skip:* (nothing to do), wait:* (try again later), or dream. Checks run
cheapest first, so most of a large transcript tree costs one stat and one
line read.
"""

import time
from dataclasses import dataclass, field
from pathlib import Path

from .. import fresh, ledger
from . import config, state, transcripts
from .transcripts import Digest, Transcript

PRUNE_VERDICTS = ("outdated", "partial")


@dataclass
class Candidate:
    transcript: Transcript
    verdict: str
    reason: str = ""
    new_prompts: int = 0
    prompts_total: int = 0
    prune: list[str] = field(default_factory=list)
    touched: list[str] = field(default_factory=list)
    estimate: int = 0
    digest: Digest | None = None
    start_offset: int = 0

    @property
    def session(self) -> str:
        return self.transcript.session


def workspace_dir(r: Path) -> Path:
    d = config.state_dir(r) / "workspace"
    d.mkdir(parents=True, exist_ok=True)
    return d


def subject_lock(session: str) -> str:
    return f"subject-{session}"


def _session_events(r: Path) -> dict[str, list[dict]]:
    by_session: dict[str, list[dict]] = {}
    for e in ledger.load(r):
        if e.get("session"):
            by_session.setdefault(e["session"], []).append(e)
    return by_session


def _prune_signal(r: Path, events: list[dict], since: str | None) -> tuple[list[str], list[str]]:
    """(reasons to prune, pages the session touched) from the session's own
    ledger events after `since`. A page read with expired verified claims
    counts; merely unverified ones do not, or nearly every read would."""
    reasons: list[str] = []
    touched: list[str] = []
    for e in events:
        if since and e.get("ts", "") <= since:
            continue
        name = e.get("filename")
        if not name or not (r / name).is_file():
            continue
        if name not in touched:
            touched.append(name)
        if e["event"] == "feedback" and e.get("verdict") in PRUNE_VERDICTS:
            reasons.append(f"{e['verdict']}: {name}")
        elif e["event"] == "read":
            f = fresh.page_freshness(r, name, events)
            if f and f.due and f"due claims: {name}" not in reasons:
                reasons.append(f"due claims: {name}")
    return reasons, touched


def evaluate(
    r: Path,
    cfg: dict,
    t: Transcript,
    *,
    now: float,
    registered: set[str],
    workspace_slug: str,
    chat_dirs: dict[str, Path],
    session_events: dict[str, list[dict]],
    spent_day: int,
    spent_week: int,
    force: bool = False,
) -> Candidate:
    """One transcript's verdict. force (a dream asked for by session id)
    skips the age, idle, thin, and budget checks, never the others."""
    if (
        t.session in registered
        or t.project_dir.name == workspace_slug
        or transcripts.first_line_has_marker(t.path)
    ):
        return Candidate(t, "skip:dream", "a dream or consolidation session")
    if t.is_subagent and not cfg["include_subagents"]:
        return Candidate(t, "skip:subagent", "its results reach the parent transcript")
    if state.is_locked(r, subject_lock(t.session)):
        return Candidate(t, "skip:locked", "another dream holds it")

    stat = t.path.stat()
    age = now - stat.st_mtime
    if not force and age > cfg["max_age_days"] * 86400:
        return Candidate(t, "skip:old", f"last written {age / 86400:.0f}d ago")

    history = transcripts.prompt_history(chat_dirs.get(t.session))
    if not force:
        if history and history[1] > stat.st_mtime + 1:
            return Candidate(t, "wait:active", "a prompt is waiting for the agent")
        if age < cfg["settle_minutes"] * 60:
            return Candidate(t, "wait:active", f"written {age / 60:.0f}m ago")
        if history:
            last_user = history[1]
        else:
            stamp = transcripts.last_user_timestamp(t.path)
            last_user = stamp.timestamp() if stamp else stat.st_mtime
        idle = now - last_user
        if idle < cfg["idle_hours"] * 3600:
            return Candidate(t, "wait:idle", f"user active {idle / 3600:.1f}h ago")

    mark = state.watermark(r, t.session)
    if stat.st_size < mark.offset:
        mark = state.Watermark()
    if stat.st_size == mark.offset:
        return Candidate(t, "skip:dreamed", "nothing new since the last dream")

    if history:
        total = history[0]
        new = total - mark.prompts if total >= mark.prompts else total
    else:
        new = transcripts.count_typed_prompts(t.path, mark.offset)
        total = mark.prompts + new

    prune, touched = _prune_signal(r, session_events.get(t.session, []), mark.dreamed_at)
    pages_tokens = sum((r / name).stat().st_size for name in touched) // 4
    overhead = cfg["overhead_tokens"]
    room_chars = max(4000, (cfg["max_input_tokens_per_dream"] - overhead - pages_tokens) * 4)
    dg = transcripts.digest(t.path, mark.offset, room_chars, cfg["max_chars_per_message"])
    cand = Candidate(
        t,
        "dream",
        new_prompts=new,
        prompts_total=total,
        prune=prune,
        touched=touched,
        estimate=overhead + len(dg.text) // 4 + pages_tokens,
        digest=dg,
        start_offset=mark.offset,
    )
    adds = new >= cfg["min_new_prompts"] and len(dg.text) >= cfg["min_new_chars"]
    if not force and not adds and not prune:
        cand.verdict = "skip:thin"
        cand.reason = f"{new} new prompts, {len(dg.text)} chars, nothing to prune"
        return cand
    if not force and (
        spent_day + cand.estimate > cfg["daily_token_budget"]
        or spent_week + cand.estimate > cfg["weekly_token_budget"]
    ):
        cand.verdict = "wait:budget"
        cand.reason = f"~{cand.estimate} tokens; spent {spent_day} today, {spent_week} this week"
        return cand
    signals = [f"{new} new prompts"] + prune[:3] + (["truncated"] if dg.truncated else [])
    cand.reason = "; ".join(signals)
    return cand


def plan(r: Path, cfg: dict, only: str | None = None, now: float | None = None) -> list[Candidate]:
    """Every transcript's verdict, dreams first (prune signals, then most new
    prompts). `only` restricts to one session and forces it."""
    now = time.time() if now is None else now
    found = transcripts.discover(cfg)
    if only:
        found = [t for t in found if t.session == only]
    context = dict(
        now=now,
        registered=state.registered_sessions(r),
        workspace_slug=transcripts.project_slug(workspace_dir(r)),
        chat_dirs=transcripts.chat_dirs(cfg),
        session_events=_session_events(r),
        spent_day=state.tokens_spent(r, 24),
        spent_week=state.tokens_spent(r, 24 * 7),
    )
    cands = [evaluate(r, cfg, t, force=bool(only), **context) for t in found]
    order = {"dream": 0, "wait:budget": 1}
    cands.sort(
        key=lambda c: (order.get(c.verdict, 2), -len(c.prune), -c.new_prompts, c.session)
    )
    return cands
