"""What the scheduler remembers between runs, all under meta/dreams/.

- registry.jsonl: every session the scheduler created (dreams,
  consolidations, and prunes). These are never dreamed about.
- watermarks/<subject>.json: how far into a transcript dreams have read.
- log.jsonl: one line per dream, consolidation, prune, and budget deferral,
  with the usage each run reported. Budgets are computed from it.
- consolidated.json: the corpus commit the last consolidation covered.
"""

import fcntl
import json
import os
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .. import corpus
from . import config


def _append_jsonl(path: Path, record: dict) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, separators=(",", ":")) + "\n")


def _read_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def register(r: Path, kind: str, session: str, subject: str | None = None) -> None:
    record = {"ts": corpus.now_iso(), "kind": kind, "session": session}
    if subject:
        record["subject_session"] = subject
    _append_jsonl(config.state_dir(r) / "registry.jsonl", record)


def registered_sessions(r: Path) -> set[str]:
    return {e["session"] for e in _read_jsonl(config.state_dir(r) / "registry.jsonl")}


@dataclass
class Watermark:
    offset: int = 0
    prompts: int = 0
    dreamed_at: str | None = None
    commit: str | None = None


def _watermark_path(r: Path, subject: str) -> Path:
    d = config.state_dir(r) / "watermarks"
    d.mkdir(exist_ok=True)
    return d / f"{subject}.json"


def watermark(r: Path, subject: str) -> Watermark:
    path = _watermark_path(r, subject)
    if not path.is_file():
        return Watermark()
    try:
        return Watermark(**json.loads(path.read_text(encoding="utf-8")))
    except (ValueError, TypeError):
        return Watermark()


def save_watermark(r: Path, subject: str, mark: Watermark) -> None:
    _watermark_path(r, subject).write_text(json.dumps(asdict(mark)), encoding="utf-8")


def log(r: Path, record: dict) -> None:
    _append_jsonl(config.state_dir(r) / "log.jsonl", {"ts": corpus.now_iso(), **record})


def load_log(r: Path, hours: float | None = None) -> list[dict]:
    events = _read_jsonl(config.state_dir(r) / "log.jsonl")
    if hours is None:
        return events
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return [e for e in events if e.get("ts", "") >= cutoff]


USAGE_KEYS = ("inputTokens", "outputTokens", "cacheReadTokens", "cacheWriteTokens")


def usage_tokens(usage: dict | None) -> int:
    """Every token a run reported: input, output, and cache reads and writes.
    Counting input and output alone made the meter lie about which run did
    more work: a model that reads its context from cache reports almost no
    input, however many times it re-read it."""
    return sum(int((usage or {}).get(key) or 0) for key in USAGE_KEYS)


def estimate(cfg: dict, given_tokens: int) -> int:
    """Tokens a run will process when handed `given_tokens` of prompt,
    digest, and pages, in the unit usage_tokens counts."""
    return (cfg["overhead_tokens"] + given_tokens) * cfg["reread_factor"]


def tokens_spent(r: Path, hours: float) -> int:
    """Tokens reported by runs in the window. A run killed at its timeout
    reports nothing, so the runner logs its estimate instead."""
    return sum(usage_tokens(e.get("usage")) for e in load_log(r, hours))


def over_budget(cfg: dict, estimate: int, spent_day: int, spent_week: int) -> bool:
    """Whether spending `estimate` more tokens would pass the daily or weekly
    budget. A budget set to null is no budget."""
    day, week = cfg["daily_token_budget"], cfg["weekly_token_budget"]
    return (day is not None and spent_day + estimate > day) or (
        week is not None and spent_week + estimate > week
    )


def consolidated(r: Path) -> dict:
    path = config.state_dir(r) / "consolidated.json"
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return {}


def save_consolidated(r: Path, commit: str) -> None:
    (config.state_dir(r) / "consolidated.json").write_text(
        json.dumps({"commit": commit, "ts": corpus.now_iso()}), encoding="utf-8"
    )


def _lock_path(r: Path, name: str) -> Path:
    d = config.state_dir(r) / "locks"
    d.mkdir(exist_ok=True)
    return d / f"{name}.lock"


@contextmanager
def try_lock(r: Path, name: str):
    """Yield True while holding an exclusive flock on locks/<name>.lock, or
    False if another process holds it. The OS drops the lock when the holder
    dies, so a crashed run never leaves a stale lock behind."""
    fd = os.open(_lock_path(r, name), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def is_locked(r: Path, name: str) -> bool:
    with try_lock(r, name) as held:
        return not held
