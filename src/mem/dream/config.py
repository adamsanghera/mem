"""Dream settings: meta/dreams/config.yaml over built-in defaults."""

from pathlib import Path

import yaml

from .. import corpus

DEFAULTS: dict = {
    "enabled": True,
    "interval_minutes": 15,
    "idle_hours": 2.0,
    # a transcript written to this recently is still being worked on, even
    # when the user has not sent a prompt for a while
    "settle_minutes": 30,
    # sessions quiet for longer than this are history, not backlog: without
    # it, the first install would dream about every old transcript
    "max_age_days": 3,
    "min_new_prompts": 1,
    "min_new_chars": 1500,
    "include_subagents": False,
    "transcript_roots": ["~/.cursor/projects/*/agent-transcripts"],
    "chats_root": "~/.cursor/chats",
    "model": None,
    # a run re-sends its context on every tool call, so the input it reports
    # runs well past the size of what it was given
    "overhead_tokens": 40000,
    "max_input_tokens_per_dream": 150000,
    "daily_token_budget": 1000000,
    "weekly_token_budget": 4000000,
    "max_dreams_per_run": 2,
    "timeout_minutes": 20,
    "max_chars_per_message": 2000,
    "skill_roots": ["~/.cursor/skills"],
    "skill_edit_max_churn": 0.5,
    "skill_proposal_only": ["memory", "mem", "mem-consolidate"],
    "consolidate_after_dreams": True,
    "consolidate_base_tokens": 60000,
    "max_input_tokens_per_consolidation": 250000,
    "consolidate_timeout_minutes": 30,
    "agent_cmd": "agent",
    "launchd_label": "dev.mem.dream",
}

# Scheduler state (log, watermarks, registry, locks, workspace) describes this
# machine's transcripts, so only the settings travel with the corpus.
STATE_GITIGNORE = "*\n!.gitignore\n!config.yaml\n"


def state_dir(r: Path) -> Path:
    """meta/dreams/, created on first use with a .gitignore that keeps
    everything but config.yaml out of corpus commits."""
    d = corpus.meta_dir(r) / "dreams"
    d.mkdir(parents=True, exist_ok=True)
    ignore = d / ".gitignore"
    if not ignore.exists():
        ignore.write_text(STATE_GITIGNORE, encoding="utf-8")
    return d


def config_path(r: Path) -> Path:
    return state_dir(r) / "config.yaml"


def load(r: Path) -> dict:
    path = config_path(r)
    stored = yaml.safe_load(path.read_text(encoding="utf-8")) if path.is_file() else None
    return {**DEFAULTS, **(stored or {})}


def set_value(r: Path, key: str, raw: str) -> object:
    """Store one setting. The value is parsed as YAML, so `2`, `true`, and
    `[a, b]` keep their types. Only keys with a default are accepted."""
    if key not in DEFAULTS:
        raise KeyError(f"unknown setting {key!r}; known: {', '.join(sorted(DEFAULTS))}")
    value = yaml.safe_load(raw)
    path = config_path(r)
    stored = yaml.safe_load(path.read_text(encoding="utf-8")) if path.is_file() else None
    stored = stored or {}
    stored[key] = value
    path.write_text(yaml.safe_dump(stored, sort_keys=True), encoding="utf-8")
    return value


def expand(path: str) -> Path:
    return Path(path).expanduser()
