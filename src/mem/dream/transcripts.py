"""Reading agent transcripts without spending tokens: Cursor's and Claude Code's.

Cursor: <project>/agent-transcripts/<id>/<id>.jsonl, with subagents under
<id>/subagents/<sub-id>.jsonl. Each line is {role, message: {content}}.
User text blocks wrap what the user typed in <user_query> and stamp it with a
<timestamp>; turns the CLI injects (a background shell finishing) carry a
timestamp but no <user_query>. Assistant lines hold text blocks (visible reply
and reasoning joined) and tool_use blocks; tool results are not recorded.

The chat's own folder, <chats_root>/<hash>/<id>/, holds prompt_history.json:
the prompts the user actually sent, newest first. Its mtime is when the user
last sent one, which is the best idle signal. Not every transcript has one.

Claude Code: ~/.claude/projects/<project>/<id>.jsonl, with subagents under
<project>/<id>/subagents/agent-<sub-id>.jsonl. Each line has a `type`; only
"user" and "assistant" lines hold conversation, and the rest are session
bookkeeping. A user line's message content is a string or a list of blocks.
What the user typed has origin.kind "human"; task notifications and
auto-continuations carry another origin, and isMeta lines (skill bodies,
command caveats) are the harness talking. Tool results come back as user
lines of tool_result blocks. An assistant line holds one block: text,
thinking, or tool_use. Every line carries an ISO `timestamp`. There is no
prompt history, so idleness comes from the last typed prompt's timestamp.
"""

import glob
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import config

DREAM_MARKER = "<mem-dream do-not-reflect/>"

CURSOR = "cursor"
CLAUDE = "claude"

_TIMESTAMP_RE = re.compile(r"<timestamp>(.*?)</timestamp>", re.S)
_QUERY_RE = re.compile(r"<user_query>\s*(.*?)\s*</user_query>", re.S)
_STAMP_RE = re.compile(
    r"(\w{3})\w* (\d{1,2}), (\d{4}), (\d{1,2}):(\d{2}) (AM|PM) \(UTC([+-]\d{1,2})(?::(\d{2}))?\)"
)
_MONTHS = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1
)}
# the tool input fields that best say what a call was for, in order of preference
_TOOL_LABEL_KEYS = (
    "description", "path", "target_file", "file_path", "pattern", "glob_pattern", "query",
    "command", "url", "skill",
)


@dataclass
class Transcript:
    session: str
    path: Path
    is_subagent: bool
    kind: str = CURSOR

    @property
    def project_dir(self) -> Path:
        if self.kind == CLAUDE:
            # <project>/<id>.jsonl or <project>/<id>/subagents/<file>
            return self.path.parents[2] if self.is_subagent else self.path.parent
        # <project>/agent-transcripts/<id>/[subagents/]<file>
        up = 4 if self.is_subagent else 3
        return self.path.parents[up - 1]

    @property
    def readable(self) -> Path:
        """What a dream about this session may read. A Cursor session has a
        folder of its own; a Claude Code transcript shares its folder with
        every other session in the project, so only the file."""
        return self.path if self.kind == CLAUDE else self.path.parent


def discover(cfg: dict) -> list[Transcript]:
    """One transcript per session. A chat that moves to another workspace
    leaves its old transcript behind under the old project and continues in
    a copy under the new one, so the most recently written copy wins:
    watermarks are byte offsets into one file per session."""
    found: dict[str, Transcript] = {}

    def keep(t: Transcript) -> None:
        prior = found.get(t.session)
        if prior is None or t.path.stat().st_mtime > prior.path.stat().st_mtime:
            found[t.session] = t

    for pattern in cfg["transcript_roots"]:
        for root in map(Path, glob.glob(str(config.expand(pattern)))):
            for path in root.glob("*.jsonl"):
                keep(Transcript(path.stem, path, False, CLAUDE))
            for path in root.glob("*/*.jsonl"):
                if path.stem == path.parent.name:
                    keep(Transcript(path.stem, path, False, CURSOR))
            for path in root.glob("*/subagents/*.jsonl"):
                # Claude Code keeps the parent transcript beside the folder
                parent = root / f"{path.parents[1].name}.jsonl"
                keep(Transcript(path.stem, path, True, CLAUDE if parent.is_file() else CURSOR))
    return list(found.values())


def project_slug(path: Path) -> str:
    """The folder name Cursor gives a workspace under ~/.cursor/projects. It
    is built from the path as given, symlinks unresolved (/tmp, not
    /private/tmp), so pass the same path the agent was given. Claude Code's
    folder names keep every dash (`-Users-me--claude`); passed through here,
    they come out the same as Cursor's."""
    return re.sub(r"[^A-Za-z0-9]+", "-", str(path)).strip("-")


def chat_dirs(cfg: dict) -> dict[str, Path]:
    """Chat folders by session id, listed once per plan."""
    return {d.name: d for d in config.expand(cfg["chats_root"]).glob("*/*") if d.is_dir()}


def prompt_history(chat_dir: Path | None) -> tuple[int, float] | None:
    """(prompt count, mtime of the last prompt) from a chat folder, if any."""
    path = chat_dir / "prompt_history.json" if chat_dir else None
    if path is None or not path.is_file():
        return None
    try:
        prompts = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return None
    return (len(prompts) if isinstance(prompts, list) else 0), path.stat().st_mtime


def parse_timestamp(text: str) -> datetime | None:
    """`Friday, Oct 23, 2026, 12:46 PM (UTC-4)` to an aware datetime."""
    m = _STAMP_RE.search(text)
    if not m:
        return None
    mon, day, year, hour, minute, ampm, off_h, off_m = m.groups()
    if mon not in _MONTHS:
        return None
    hour = int(hour) % 12 + (12 if ampm == "PM" else 0)
    sign = -1 if off_h.startswith("-") else 1
    offset = timedelta(hours=abs(int(off_h)), minutes=int(off_m or 0)) * sign
    return datetime(int(year), _MONTHS[mon], int(day), hour, int(minute), tzinfo=timezone(offset))


def starts_with_marker(path: Path, max_lines: int = 50) -> bool:
    """Whether the session's first user message carries the dream marker.
    Cursor's is the first line; Claude Code writes a few lines of session
    bookkeeping before it."""
    with path.open("r", encoding="utf-8", errors="ignore") as f:
        for _, line in zip(range(max_lines), f):
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if isinstance(record, dict) and "user" in (record.get("role"), record.get("type")):
                return DREAM_MARKER in line
    return False


def _lines(path: Path, start: int):
    """Yield (end_offset, record) for each JSON line at or after byte start."""
    with path.open("rb") as f:
        f.seek(start)
        offset = start
        for raw in f:
            offset += len(raw)
            try:
                yield offset, json.loads(raw)
            except ValueError:
                continue


def _blocks(record: dict) -> list[dict]:
    message = record.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    return [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []


@dataclass
class Said:
    text: str
    typed: bool = False  # the user wrote it, rather than the harness
    when: str = ""  # the timestamp as the digest shows it
    at: datetime | None = None  # set when it shows the user was there


@dataclass
class Turn:
    """One transcript line, reduced to what a digest shows. role is None for
    lines with nothing to show."""

    end_offset: int
    role: str | None = None
    said: list[Said] = field(default_factory=list)
    tools: list[dict] = field(default_factory=list)


def _user_text(block_text: str) -> tuple[str, bool]:
    """(what the user wrote, typed?) for one Cursor user text block. Injected
    turns have no <user_query> and come back as typed=False."""
    m = _QUERY_RE.search(block_text)
    if m:
        return m.group(1), True
    stripped = _TIMESTAMP_RE.sub("", block_text).strip()
    return stripped, False


def _cursor_turn(turn: Turn, record: dict) -> None:
    role = record.get("role")
    if role == "user":
        turn.role = role
        for block in _blocks(record):
            if block.get("type") != "text":
                continue
            stamp = _TIMESTAMP_RE.search(block.get("text") or "")
            when = stamp.group(1) if stamp else ""
            text, typed = _user_text(block.get("text") or "")
            turn.said.append(Said(text, typed, when, parse_timestamp(when) if when else None))
    elif role == "assistant":
        turn.role = role
        for block in _blocks(record):
            if block.get("type") == "text":
                turn.said.append(Said(block.get("text") or ""))
            elif block.get("type") == "tool_use":
                turn.tools.append(block)


def _iso(stamp: object) -> tuple[str, datetime | None]:
    """A Claude Code `timestamp` as (digest label, aware datetime)."""
    try:
        when = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return "", None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.astimezone().strftime("%Y-%m-%d %H:%M %Z"), when


def _claude_typed(record: dict, content: object) -> bool:
    """Whether a Claude Code user line is something the user typed. Lines
    written before Claude Code recorded an origin count when they are plain
    text that the harness did not wrap in a tag or mark as its own."""
    if "origin" in record:
        origin = record.get("origin")
        return isinstance(origin, dict) and origin.get("kind") == "human"
    return (
        isinstance(content, str)
        and not content.lstrip().startswith("<")
        and not record.get("isCompactSummary")
    )


def _claude_turn(turn: Turn, record: dict) -> None:
    kind = record.get("type")
    message = record.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if kind == "user":
        if record.get("isMeta"):
            return
        if isinstance(content, str):
            texts = [content]
        else:
            texts = [b.get("text") or "" for b in _blocks(record) if b.get("type") == "text"]
        if not texts:
            return  # tool results
        typed = _claude_typed(record, content)
        label, when = _iso(record.get("timestamp"))
        turn.role = "user"
        turn.said += [Said(t.strip(), typed, label, when if typed else None) for t in texts]
    elif kind == "assistant":
        turn.role = "assistant"
        for block in _blocks(record):
            if block.get("type") == "text":
                turn.said.append(Said(block.get("text") or ""))
            elif block.get("type") == "thinking":
                turn.said.append(Said(block.get("thinking") or ""))
            elif block.get("type") == "tool_use":
                turn.tools.append(block)


def turns(path: Path, start: int, kind: str = CURSOR):
    """Yield a Turn for each line at or after byte `start`."""
    read = _claude_turn if kind == CLAUDE else _cursor_turn
    for end, record in _lines(path, start):
        turn = Turn(end)
        if isinstance(record, dict):
            read(turn, record)
        yield turn


def last_user_timestamp(path: Path, kind: str = CURSOR) -> datetime | None:
    last = None
    for turn in turns(path, 0, kind):
        for said in turn.said:
            last = said.at or last
    return last


def count_typed_prompts(path: Path, start: int, kind: str = CURSOR) -> int:
    return sum(
        1 for turn in turns(path, start, kind)
        if turn.role == "user" and any(s.typed for s in turn.said)
    )


def _clip(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _tool_line(block: dict) -> str:
    name = block.get("name") or "tool"
    args = block.get("input") if isinstance(block.get("input"), dict) else {}
    for key in _TOOL_LABEL_KEYS:
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            return f"  - {name}: {_clip(' '.join(value.split()), 160)}"
    return f"  - {name}"


@dataclass
class Digest:
    text: str
    end_offset: int
    truncated: bool


def digest(
    path: Path, start: int, max_chars: int, max_chars_per_message: int, kind: str = CURSOR
) -> Digest:
    """The slice of a transcript after byte `start`, as compact text: what the
    user typed, what the agent said (clipped), and one line per tool call.
    Stops before max_chars, but always takes at least one line so a run makes
    progress; end_offset is where the next digest should start. Assistant
    lines in a row share one [assistant] header: Claude Code writes each
    block of a reply on a line of its own."""
    parts: list[str] = []
    size = 0
    end = start
    truncated = False
    last_role = None
    for turn in turns(path, start, kind):
        chunk: list[str] = []
        if turn.role == "user":
            for said in turn.said:
                if said.text:
                    label = "user" if said.typed else "injected"
                    when = f" {said.when}" if said.when else ""
                    chunk.append(f"[{label}{when}]\n{_clip(said.text, max_chars_per_message)}")
        elif turn.role == "assistant":
            texts = [_clip(s.text, max_chars_per_message) for s in turn.said if s.text.strip()]
            lines = texts + [_tool_line(block) for block in turn.tools]
            if lines:
                header = "" if last_role == "assistant" else "[assistant]\n"
                chunk.append(header + "\n".join(lines))
        if not chunk:
            end = turn.end_offset
            continue
        rendered = "\n".join(chunk) + "\n\n"
        if parts and size + len(rendered) > max_chars:
            truncated = True
            break
        parts.append(rendered[:max_chars])
        size += len(rendered)
        end = turn.end_offset
        last_role = turn.role
    return Digest("".join(parts), end, truncated)
