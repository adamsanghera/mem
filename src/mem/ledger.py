"""The hit ledger: append-only JSONL under meta/ (a subdirectory so the spec
never treats it as corpus content).

Unlike the vector index this is NOT a cache — it is the consolidation signal
that decides which memories earn promotion into skills. JSONL rather than
sqlite so concurrent agent sessions can append safely and git can merge it.
"""

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import corpus

# Verdict taxonomy for feedback events: how a recalled memory related to the
# task outcome. Each verdict drives a different downstream action —
# solved/partial/context are graduation heat, partial and outdated feed the
# consolidator's refinement queue, unrelated flags retrieval noise (bad
# title/summary or a page that should split), and miss records a recall gap
# (nothing useful existed; the event carries no filename).
VERDICTS = {
    "solved": "directly solved the problem or answered the question",
    "partial": "led toward the solution but was incomplete",
    "context": "helpful background; didn't itself solve anything",
    "unrelated": "surfaced but irrelevant to the task",
    "outdated": "relevant but stale or wrong; needs correction",
    "miss": "nothing useful was found for this need (no page ref)",
}


def ledger_path(r: Path) -> Path:
    return corpus.meta_dir(r) / "ledger.jsonl"


def _session() -> str | None:
    """Cursor agent shells export CURSOR_CONVERSATION_ID, which distinguishes
    concurrent agent sessions; MEM_SESSION overrides for deliberate naming."""
    for var in ("MEM_SESSION", "CURSOR_CONVERSATION_ID", "TERM_SESSION_ID"):
        value = os.environ.get(var)
        if value:
            return value
    return None


def append(r: Path, event: str, filename: str | None, uuid: str | None = None, **extra) -> None:
    record = {
        "ts": corpus.now_iso(),
        "event": event,
        "filename": filename,
        "uuid": uuid,
        "session": _session(),
        **{k: v for k, v in extra.items() if v is not None},
    }
    path = ledger_path(r)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, separators=(",", ":")) + "\n")


def load(r: Path, window_days: int | None = None) -> list[dict]:
    path = ledger_path(r)
    if not path.is_file():
        return []
    cutoff = None
    if window_days is not None:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=window_days)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if cutoff is not None and record.get("ts", "") < cutoff:
            continue
        events.append(record)
    return events
