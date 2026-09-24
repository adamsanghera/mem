#!/usr/bin/env python
"""Declare TODO(investigate:<id>) markers as bounties.

Scans markdown under the given directories (default: the memory corpus) for
TODO(investigate:<id>) markers, the scaffold-docs grammar for a known gap,
and registers each as a declared miss with the file as source and
`investigate:<id>` as the stable key. Keys make re-runs no-ops, so this can
run on every consolidation pass and over doc trees outside the corpus:

    python scripts/mine_markers.py                # the corpus
    python scripts/mine_markers.py docs/ .adam/   # doc trees too
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from mem import corpus, fresh, ledger  # noqa: E402

SKIP_DIRS = {".git", "node_modules", "meta"}


def markdown_files(roots: list[Path]):
    for root in roots:
        if root.is_file():
            yield root
            continue
        for path in sorted(root.rglob("*.md")):
            if not SKIP_DIRS & set(path.relative_to(root).parts[:-1]):
                yield path


def mine(r: Path, roots: list[Path]) -> tuple[int, int]:
    """Register each TODO(investigate:<id>) once. Returns (new, already)."""
    new = already = 0
    for path in markdown_files(roots):
        text = path.read_text(encoding="utf-8", errors="ignore")
        for marker in fresh.parse_markers(text):
            if marker.kind != "investigate" or marker.error:
                continue
            key = f"investigate:{marker.id}"
            if ledger.has_key(r, key):
                already += 1
                continue
            line = next((ln for ln in text.splitlines() if marker.raw in ln), marker.raw)
            ledger.append(
                r, "feedback", None, verdict="miss",
                note=" ".join(line.split()), source=str(path), key=key,
            )
            new += 1
    return new, already


def main() -> None:
    r = corpus.root()
    roots = [Path(a).expanduser() for a in sys.argv[1:]] or [r]
    new, already = mine(r, roots)
    print(f"bounties: {new} declared, {already} already registered")


if __name__ == "__main__":
    main()
