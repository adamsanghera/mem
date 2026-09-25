"""A tiny local web app for browsing, visualizing, and editing the corpus.

Browsing is admin activity, so the read-only endpoints never log usage
events: clicks would inflate the heat that ranks everything else. Saving a
page runs the same size and embedding checks as `mem add`, re-embeds the
page, and logs a write. The server binds to 127.0.0.1 only.

The cluster view needs no dimensionality reduction: each page's k nearest
neighbours (by cosine distance, from the sqlite index) become weighted
edges, and a force layout in the browser lets clusters emerge.
"""

import contextlib
import json
import webbrowser
from collections import Counter
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from . import corpus, embed, fresh, index, ledger

ASSET = Path(__file__).parent / "browser" / "index.html"
NEIGHBOURS = 4
LINK_MAX_DISTANCE = 0.55  # beyond this, pages are not meaningfully related
RECENT_EVENTS = 20


class SaveError(ValueError):
    """The page as submitted cannot be saved; message is user-facing."""


def _cutoff(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _tags(fm: dict) -> list[str]:
    tags = fm.get("tags") or []
    return [str(t) for t in tags] if isinstance(tags, list) else [str(tags)]


def list_pages(r: Path) -> list[dict]:
    events = ledger.load(r)
    grouped = fresh.events_by_page(events)
    month = _cutoff(30)
    rows = []
    for path in corpus.pages(r):
        text = path.read_text(encoding="utf-8")
        fm, _ = corpus.parse_frontmatter(text)
        fm = fm or {}
        page_events = grouped.get(path.name, [])
        f = fresh.freshness(path.name, fm, text, page_events)
        heat = sum(
            1 for e in page_events if e["event"] in ("read", "search_hit") and e["ts"] >= month
        )
        verdicts = Counter(e["verdict"] for e in page_events if e["event"] == "feedback")
        rows.append(
            {
                "filename": path.name,
                "title": str(fm.get("title") or path.name),
                "summary": str(fm.get("summary") or ""),
                "tags": _tags(fm),
                "volatility": f.volatility,
                "freshness": f.line(),
                "due": f.is_due,
                "heat": heat,
                "verdicts": dict(verdicts),
                "bytes": len(text.encode("utf-8")),
                "updated": str(fm.get("updated") or ""),
            }
        )
    return rows


def get_page(r: Path, filename: str) -> dict | None:
    path = corpus.resolve(r, filename)
    if path is None:
        return None
    text = path.read_text(encoding="utf-8")
    fm, _ = corpus.parse_frontmatter(text)
    events = [e for e in ledger.load(r) if e.get("filename") == path.name]
    f = fresh.freshness(path.name, fm or {}, text, events)
    recent = [
        {k: e.get(k) for k in ("ts", "event", "verdict", "claim", "note", "query", "session")}
        for e in events[-RECENT_EVENTS:]
    ]
    return {"filename": path.name, "text": text, "freshness": f.line(), "due": f.is_due, "events": recent}


def save_page(r: Path, filename: str, text: str) -> dict:
    """Validate, write, re-embed, and log an edited page. Raises SaveError."""
    if not corpus.FILENAME_RE.match(filename):
        raise SaveError("bad filename")
    path = r / filename
    if text.startswith("---\n"):
        fm, _ = corpus.parse_frontmatter(text)
        if fm is None:
            raise SaveError("frontmatter is not valid YAML")
    raw = text.encode("utf-8")
    if len(raw) > corpus.PAGE_LIMIT:
        raise SaveError(f"{len(raw)}B is over the {corpus.PAGE_LIMIT}B page limit; split the page")
    try:
        [vector] = embed.embed_texts([index.embed_input(raw)], truncate=False)
    except embed.InputTooLong:
        raise SaveError(
            f"{len(raw)}B is too long for the embedding model's 2048-token window; split the page"
        ) from None
    path.write_text(text, encoding="utf-8")
    index.insert_vector(r, path, vector)
    fm, _ = corpus.parse_frontmatter(text)
    ledger.append(r, "write", path.name, uuid=str((fm or {}).get("uuid") or "") or None)
    return get_page(r, path.name)


def graph(r: Path) -> dict:
    """Nodes for every indexed page and weighted edges to their nearest
    neighbours, for a force layout."""
    meta = {p["filename"]: p for p in list_pages(r)}
    import sqlite_vec

    nodes, links, seen = [], [], set()
    with contextlib.closing(index.open_index(r)) as db:
        rows = db.execute("SELECT filename, embedding FROM pages").fetchall()
        for filename, blob in rows:
            m = meta.get(filename)
            if m is None:
                continue
            nodes.append(
                {
                    "id": filename,
                    "title": m["title"],
                    "volatility": m["volatility"],
                    "tag": m["tags"][0] if m["tags"] else "",
                    "heat": m["heat"],
                    "due": m["due"],
                    "card": "skill-card" in m["tags"],
                }
            )
            neighbours = db.execute(
                "SELECT filename, vec_distance_cosine(embedding, ?) AS d FROM pages "
                "WHERE filename != ? ORDER BY d LIMIT ?",
                (blob, filename, NEIGHBOURS),
            ).fetchall()
            for other, d in neighbours:
                if d > LINK_MAX_DISTANCE or other not in meta:
                    continue
                key = tuple(sorted((filename, other)))
                if key in seen:
                    continue
                seen.add(key)
                links.append({"source": filename, "target": other, "distance": round(float(d), 3)})
    return {"nodes": nodes, "links": links}


def search(r: Path, query: str, k: int = 10) -> list[dict]:
    if not query.strip():
        return []
    return [{"filename": h.filename, "distance": round(h.distance, 3)} for h in index.search(r, query, k)]


class Handler(BaseHTTPRequestHandler):
    root: Path = corpus.root()

    def log_message(self, *_args) -> None:  # quiet
        pass

    def _json(self, payload, status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        url = urlparse(self.path)
        if url.path == "/":
            body = ASSET.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif url.path == "/api/pages":
            self._json(list_pages(self.root))
        elif url.path == "/api/graph":
            self._json(graph(self.root))
        elif url.path == "/api/search":
            q = parse_qs(url.query).get("q", [""])[0]
            try:
                self._json(search(self.root, q))
            except RuntimeError as e:
                self._json({"error": str(e)}, 503)
        elif url.path.startswith("/api/page/"):
            page = get_page(self.root, unquote(url.path[len("/api/page/") :]))
            self._json(page if page else {"error": "no such page"}, 200 if page else 404)
        else:
            self._json({"error": "not found"}, 404)

    def do_PUT(self) -> None:  # noqa: N802
        url = urlparse(self.path)
        if not url.path.startswith("/api/page/"):
            self._json({"error": "not found"}, 404)
            return
        length = int(self.headers.get("Content-Length") or 0)
        text = self.rfile.read(length).decode("utf-8")
        try:
            self._json(save_page(self.root, unquote(url.path[len("/api/page/") :]), text))
        except SaveError as e:
            self._json({"error": str(e)}, 400)
        except RuntimeError as e:
            self._json({"error": str(e)}, 503)


def serve(r: Path, port: int, open_browser: bool = True) -> None:
    Handler.root = r
    server = None
    for candidate in range(port, port + 20):  # walk past ports another app holds
        try:
            server = ThreadingHTTPServer(("127.0.0.1", candidate), Handler)
            break
        except OSError:
            continue
    if server is None:
        raise RuntimeError(f"no free port in {port}..{port + 19}")
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    print(
        f"mem browser on {url}  (Ctrl-C to stop; browsing logs no usage events, saves do)",
        flush=True,
    )
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
