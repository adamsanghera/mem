"""The vector index: a deletable cache derived from the pages.

Schema, embedding input (whole file incl. frontmatter, truncated to 8192
bytes, nomic task prefixes) and cosine full-scan search all match the
memoryfield format's reference layout, so the corpus stays readable by
other memoryfield tooling.
"""

import contextlib
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import sqlite_vec

from . import corpus, embed
from .db import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS pages (
    filename      TEXT PRIMARY KEY,
    frontmatter   JSON NOT NULL,
    last_modified DATETIME NOT NULL,
    sha256_hash   BLOB NOT NULL,
    embedding     BLOB NOT NULL
);
"""


def index_path(r: Path) -> Path:
    return r / f"{embed.MODEL_CODE}.sqlite3"


def open_index(r: Path) -> "sqlite3.Connection":
    db = sqlite3.connect(str(index_path(r)), timeout=5.0)
    db.enable_load_extension(True)
    sqlite_vec.load(db)
    db.enable_load_extension(False)
    db.executescript(SCHEMA)
    return db


def _embed_input(raw: bytes) -> str:
    return "search_document: " + raw[: corpus.PAGE_LIMIT].decode("utf-8", errors="ignore")


def _row_for(path: Path) -> tuple[str, str, bytes, bytes]:
    raw = path.read_bytes()
    fm, _ = corpus.parse_frontmatter(raw.decode("utf-8", errors="ignore"))
    fm_json = json.dumps(fm or {}, default=str)
    mtime_iso = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat()
    return fm_json, mtime_iso, hashlib.sha256(raw).digest(), raw


def upsert_page(r: Path, path: Path) -> None:
    fm_json, mtime_iso, sha, raw = _row_for(path)
    [vector] = embed.embed_texts([_embed_input(raw)])
    blob = sqlite_vec.serialize_float32(vector)
    with contextlib.closing(open_index(r)) as db:
        db.execute(
            "INSERT INTO pages (filename, frontmatter, last_modified, sha256_hash, embedding) "
            "VALUES (?,?,?,?,?) ON CONFLICT(filename) DO UPDATE SET "
            "frontmatter=excluded.frontmatter, last_modified=excluded.last_modified, "
            "sha256_hash=excluded.sha256_hash, embedding=excluded.embedding",
            (path.name, fm_json, mtime_iso, sha, blob),
        )
        db.commit()


def reindex(r: Path, full: bool = False) -> tuple[int, int]:
    """Incremental (mtime, then sha256) reindex. Returns (embedded, removed)."""
    current = {p.name: p for p in corpus.pages(r)}

    removed = 0
    with contextlib.closing(open_index(r)) as db:
        for (filename,) in db.execute("SELECT filename FROM pages").fetchall():
            if filename not in current:
                db.execute("DELETE FROM pages WHERE filename = ?", (filename,))
                removed += 1
        db.commit()
        state = {
            row[0]: (row[1], row[2])
            for row in db.execute(
                "SELECT filename, last_modified, sha256_hash FROM pages"
            ).fetchall()
        }

    embedded = 0
    for name, path in current.items():
        fm_json, mtime_iso, sha, raw = _row_for(path)
        prior = None if full else state.get(name)
        if prior is not None and prior[0] == mtime_iso:
            continue
        if prior is not None and prior[1] == sha:
            # mtime moved but content didn't: cheap update, no embedding call
            with contextlib.closing(open_index(r)) as db:
                db.execute(
                    "UPDATE pages SET last_modified = ? WHERE filename = ?",
                    (mtime_iso, name),
                )
                db.commit()
            continue
        upsert_page(r, path)
        embedded += 1
    return embedded, removed


@dataclass(frozen=True)
class Hit:
    filename: str
    distance: float
    frontmatter: dict


def search(r: Path, query: str, k: int) -> list[Hit]:
    if not index_path(r).is_file():
        raise RuntimeError("no vector index; run `mem reindex` first")
    [qvec] = embed.embed_texts([f"search_query: {query}"])
    qblob = sqlite_vec.serialize_float32(qvec)
    with contextlib.closing(open_index(r)) as db:
        rows = db.execute(
            "SELECT filename, frontmatter, vec_distance_cosine(embedding, ?) AS distance "
            "FROM pages ORDER BY distance LIMIT ?",
            (qblob, k),
        ).fetchall()
    hits = []
    for filename, fm_json, distance in rows:
        try:
            fm = json.loads(fm_json)
        except ValueError:
            fm = {}
        hits.append(Hit(filename, float(distance), fm if isinstance(fm, dict) else {}))
    return hits


def embeddings_for(r: Path, filenames: list[str]) -> dict[str, list[float]]:
    """Deserialized vectors for the given pages (used by hot-cluster grouping)."""
    import struct

    out: dict[str, list[float]] = {}
    with contextlib.closing(open_index(r)) as db:
        qmarks = ",".join("?" * len(filenames))
        for filename, blob in db.execute(
            f"SELECT filename, embedding FROM pages WHERE filename IN ({qmarks})",
            filenames,
        ).fetchall():
            out[filename] = list(struct.unpack(f"{len(blob) // 4}f", blob))
    return out
