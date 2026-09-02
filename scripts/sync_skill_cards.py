#!/usr/bin/env python
"""Sync a skills directory (<name>/SKILL.md layout, as used by Cursor and
Claude Code) into pointer cards in the memory corpus.

One small `skill-<name>.md` page per skill: title, the skill's trigger
description, and the path to the real file. Cards give semantic search a
route to skills whose description didn't fire (and make skill usage
measurable through the ledger), while the SKILL.md stays the single source
of truth — cards carry pointers, never procedure bodies.

Idempotent: cards embed a sha256 of their source; unchanged skills are
skipped, changed ones are rewritten preserving uuid/created, and cards
whose skill vanished are deleted. Sync writes are deliberately NOT logged
to the ledger — it measures usage, not maintenance. Run:

    python scripts/sync_skill_cards.py [skills_dir]   # default ~/.cursor/skills
"""

import hashlib
import re
import subprocess
import sys
import uuid as uuidlib
from datetime import datetime, timezone
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from mem import corpus  # noqa: E402

DEFAULT_SKILLS_DIR = Path.home() / ".cursor" / "skills"
CARD_PREFIX = "skill-"
SUMMARY_LIMIT = 200


def parse_skill(path: Path) -> tuple[str, str]:
    """Return (name, description) from a SKILL.md, tolerating bare files."""
    text = path.read_text(encoding="utf-8", errors="ignore")
    fm, body = corpus.parse_frontmatter(text)
    fm = fm or {}
    name = str(fm.get("name") or path.parent.name)
    description = str(fm.get("description") or "").strip()
    if not description:
        # fall back to the first non-heading paragraph of the body
        for para in re.split(r"\n\s*\n", body):
            para = para.strip()
            if para and not para.startswith("#"):
                description = re.sub(r"\s+", " ", para)[:600]
                break
    return name, description


def card_body(name: str, description: str, source: Path) -> str:
    return (
        f"Pointer card for the agent skill `{name}`. This card is "
        f"a catalog entry, not the procedure: read {source} in full before "
        "acting on it.\n\n"
        f"When to reach for it: {description}"
    )


def main() -> None:
    skills_dir = Path(sys.argv[1]).expanduser() if len(sys.argv) > 1 else DEFAULT_SKILLS_DIR
    root = corpus.root()
    now = corpus.now_iso()

    sources: dict[str, tuple[Path, str, str]] = {}  # card filename -> (path, name, desc)
    for skill_md in sorted(skills_dir.glob("*/SKILL.md")):
        slug = corpus.slugify(f"{CARD_PREFIX}{skill_md.parent.name}")
        name, description = parse_skill(skill_md)
        if not description:
            print(f"skip (no description derivable): {skill_md}")
            continue
        sources[f"{slug}.md"] = (skill_md, name, description)

    written = skipped = removed = 0
    for filename, (skill_md, name, description) in sources.items():
        source_hash = hashlib.sha256(skill_md.read_bytes()).hexdigest()
        card_path = root / filename
        prior, _ = (
            corpus.parse_frontmatter(card_path.read_text(encoding="utf-8"))
            if card_path.is_file()
            else (None, "")
        )
        prior = prior or {}
        if prior.get("source_sha256") == source_hash:
            skipped += 1
            continue
        fm = {
            "title": f"Skill: {name}",
            "created": prior.get("created", now),
            "updated": now,
            "uuid": prior.get("uuid", str(uuidlib.uuid4())),
            "summary": description[:SUMMARY_LIMIT],
            "tags": ["skill-card"],
            "citations": [str(skill_md)],
            "source": str(skill_md),
            "source_sha256": source_hash,
        }
        card_path.write_text(
            corpus.build_page(fm, card_body(name, description, skill_md)),
            encoding="utf-8",
        )
        written += 1

    # cards whose skill no longer exists get removed
    for card in root.glob(f"{CARD_PREFIX}*.md"):
        fm, _ = corpus.parse_frontmatter(card.read_text(encoding="utf-8"))
        if fm and fm.get("source") and card.name not in sources:
            card.unlink()
            removed += 1

    print(f"cards: {written} written, {skipped} unchanged, {removed} removed")
    subprocess.run(["mem", "reindex"], check=True)


if __name__ == "__main__":
    main()
