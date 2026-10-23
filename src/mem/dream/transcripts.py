"""Reading Cursor agent transcripts without spending tokens.

Layout: <project>/agent-transcripts/<id>/<id>.jsonl, with subagents under
<id>/subagents/<sub-id>.jsonl. Each line is {role, message: {content}}.
User text blocks wrap what the user typed in <user_query> and stamp it with a
<timestamp>; turns the CLI injects (a background shell finishing) carry a
timestamp but no <user_query>. Assistant lines hold text blocks (visible reply
and reasoning joined) and tool_use blocks; tool results are not recorded.

The chat's own folder, <chats_root>/<hash>/<id>/, holds prompt_history.json:
the prompts the user actually sent, newest first. Its mtime is when the user
last sent one, which is the best idle signal. Not every transcript has one.
"""

import glob
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import config

DREAM_MARKER = "<mem-dream do-not-reflect/>"

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
    "description", "path", "target_file", "pattern", "glob_pattern", "query", "command", "url",
)


@dataclass
class Transcript:
    session: str
    path: Path
    is_subagent: bool

    @property
    def project_dir(self) -> Path:
        # <project>/agent-transcripts/<id>/[subagents/]<file>
        up = 4 if self.is_subagent else 3
        return self.path.parents[up - 1]


def discover(cfg: dict) -> list[Transcript]:
    found = []
    for pattern in cfg["transcript_roots"]:
        for root in map(Path, glob.glob(str(config.expand(pattern)))):
            for path in root.glob("*/*.jsonl"):
                if path.stem == path.parent.name:
                    found.append(Transcript(path.stem, path, False))
            for path in root.glob("*/subagents/*.jsonl"):
                found.append(Transcript(path.stem, path, True))
    return found


def project_slug(path: Path) -> str:
    """The folder name Cursor gives a workspace under ~/.cursor/projects. It
    is built from the path as given, symlinks unresolved (/tmp, not
    /private/tmp), so pass the same path the agent was given."""
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


def first_line_has_marker(path: Path) -> bool:
    with path.open("r", encoding="utf-8", errors="ignore") as f:
        return DREAM_MARKER in f.readline()


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


def _user_text(block_text: str) -> tuple[str, bool]:
    """(what the user wrote, typed?) for one user text block. Injected turns
    have no <user_query> and come back as typed=False."""
    m = _QUERY_RE.search(block_text)
    if m:
        return m.group(1), True
    stripped = _TIMESTAMP_RE.sub("", block_text).strip()
    return stripped, False


def last_user_timestamp(path: Path) -> datetime | None:
    last = None
    for _, record in _lines(path, 0):
        if record.get("role") != "user":
            continue
        for block in _blocks(record):
            stamp = _TIMESTAMP_RE.search(block.get("text") or "")
            if stamp:
                last = parse_timestamp(stamp.group(1)) or last
    return last


def count_typed_prompts(path: Path, start: int) -> int:
    n = 0
    for _, record in _lines(path, start):
        if record.get("role") == "user" and any(
            _QUERY_RE.search(b.get("text") or "") for b in _blocks(record)
        ):
            n += 1
    return n


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


def digest(path: Path, start: int, max_chars: int, max_chars_per_message: int) -> Digest:
    """The slice of a transcript after byte `start`, as compact text: what the
    user typed, what the agent said (clipped), and one line per tool call.
    Stops before max_chars, but always takes at least one line so a run makes
    progress; end_offset is where the next digest should start."""
    parts: list[str] = []
    size = 0
    end = start
    truncated = False
    for offset, record in _lines(path, start):
        role = record.get("role")
        chunk: list[str] = []
        if role == "user":
            for block in _blocks(record):
                if block.get("type") != "text":
                    continue
                stamp = _TIMESTAMP_RE.search(block.get("text") or "")
                when = f" {stamp.group(1)}" if stamp else ""
                text, typed = _user_text(block.get("text") or "")
                label = "user" if typed else "injected"
                if text:
                    chunk.append(f"[{label}{when}]\n{_clip(text, max_chars_per_message)}")
        elif role == "assistant":
            texts, tools = [], []
            for block in _blocks(record):
                if block.get("type") == "text" and (block.get("text") or "").strip():
                    texts.append(_clip(block["text"], max_chars_per_message))
                elif block.get("type") == "tool_use":
                    tools.append(_tool_line(block))
            if texts or tools:
                chunk.append("[assistant]\n" + "\n".join(texts + tools))
        if not chunk:
            end = offset
            continue
        rendered = "\n".join(chunk) + "\n\n"
        if parts and size + len(rendered) > max_chars:
            truncated = True
            break
        parts.append(rendered[:max_chars])
        size += len(rendered)
        end = offset
    return Digest("".join(parts), end, truncated)
