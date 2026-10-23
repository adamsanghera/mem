#!/usr/bin/env python
"""Sync a skills directory into pointer cards in the memory corpus, then
reindex. The logic lives in mem.skillcards. Run:

    python scripts/sync_skill_cards.py [skills_dir]   # default ~/.cursor/skills
"""

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from mem import corpus, skillcards  # noqa: E402


def main() -> None:
    skills_dir = (
        Path(sys.argv[1]).expanduser() if len(sys.argv) > 1 else skillcards.DEFAULT_SKILLS_DIR
    )
    written, skipped, removed = skillcards.sync(corpus.root(), skills_dir)
    print(f"cards: {written} written, {skipped} unchanged, {removed} removed")
    subprocess.run(["mem", "reindex"], check=True)


if __name__ == "__main__":
    main()
