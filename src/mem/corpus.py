"""Page files: naming rules, frontmatter, listing. Pure file operations.

The corpus layout follows the memoryfield format
(calpaterson.com/memoryfields.html): a flat directory of markdown pages with
YAML frontmatter. The rules here — filename alphabet, debris exclusion,
quoted datetimes, the 8192-byte soft page limit — keep corpora readable by
other memoryfield tooling.
"""

import os
import re
import uuid as uuidlib
from datetime import datetime, timezone
from pathlib import Path

import yaml

PAGE_LIMIT = 8192

# Only the first 2048 tokens of a page reach the embedding: ollama loads
# nomic-embed-text with a 2048-token context by default and ignores a
# per-request num_ctx on /api/embed, silently truncating with truncate=true.
# Dense technical prose measures ~3 bytes/token, so 2048 tokens is ~6.2KB.
# Pages past this budget are still readable but unfindable by their tail.
EMBED_BYTE_BUDGET = 6000
FILENAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?\.md$")
DEBRIS = {".DS_Store", "desktop.ini", "Thumbs.db"}


def root() -> Path:
    return Path(os.environ.get("MEM_ROOT", os.path.expanduser("~/memories")))


def meta_dir(r: Path) -> Path:
    return r / "meta"


def is_page(path: Path) -> bool:
    name = path.name
    if not path.is_file() or not name.endswith(".md"):
        return False
    if name in DEBRIS or ".sync-conflict-" in name or name.endswith("~"):
        return False
    return True


def pages(r: Path) -> list[Path]:
    return sorted(p for p in r.iterdir() if is_page(p))


def slugify(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    if not slug:
        raise ValueError(f"cannot derive a page filename from title {title!r}")
    return slug


def unique_filename(r: Path, slug: str) -> str:
    candidate = f"{slug}.md"
    n = 2
    while (r / candidate).exists():
        candidate = f"{slug}-{n}.md"
        n += 1
    return candidate


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_frontmatter(text: str) -> tuple[dict | None, str]:
    """Return (frontmatter dict or None, body). Malformed YAML → (None, text)."""
    if not text.startswith("---\n"):
        return None, text
    end = text.find("\n---\n", 4)
    if end == -1:
        return None, text
    try:
        fm = yaml.safe_load(text[4:end])
    except yaml.YAMLError:
        return None, text
    if not isinstance(fm, dict):
        return None, text
    return fm, text[end + 5 :]


def build_page(fm: dict, body: str) -> str:
    # safe_dump quotes timestamp-like strings, keeping datetimes as strings
    # so YAML 1.1 parsers don't coerce them to datetime objects.
    dumped = yaml.safe_dump(fm, sort_keys=False, allow_unicode=True)
    return f"---\n{dumped}---\n\n{body.strip()}\n"


def new_page(
    r: Path,
    title: str,
    body: str,
    summary: str | None = None,
    tags: list[str] | None = None,
    citations: list[str] | None = None,
) -> Path:
    ts = now_iso()
    fm: dict = {
        "title": title,
        "created": ts,
        "updated": ts,
        "uuid": str(uuidlib.uuid4()),
    }
    if summary:
        fm["summary"] = summary
    if tags:
        fm["tags"] = tags
    if citations:
        fm["citations"] = citations
    filename = unique_filename(r, slugify(title))
    path = r / filename
    path.write_text(build_page(fm, body), encoding="utf-8")
    return path


def resolve(r: Path, ref: str) -> Path | None:
    """Resolve a page by filename (with or without .md) or by uuid."""
    name = ref if ref.endswith(".md") else f"{ref}.md"
    if (r / name).is_file():
        return r / name
    for p in pages(r):
        fm, _ = parse_frontmatter(p.read_text(encoding="utf-8"))
        if fm and str(fm.get("uuid")) == ref:
            return p
    return None
