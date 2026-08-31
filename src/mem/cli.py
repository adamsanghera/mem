"""The mem CLI. Reads and writes go through here so the ledger sees them;
agents remain free to grep the corpus directly, those reads just don't count
toward heat.
"""

import argparse
import json
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import NoReturn

from . import corpus, index, ledger, report

GITIGNORE = "nomic-embed-text-v1.5.sqlite3\n"

INDEX_MD = """---
title: Index
summary: What this memoryfield is.
---

Personal memories of Adam Sanghera (and his agents): incidents, tool quirks,
decisions and their reasons, hard-won facts. Written liberally, one topic per
page, with citations. See meta/ for the hit ledger and retrieval evals.
"""


def _die(message: str) -> NoReturn:
    print(f"mem: {message}", file=sys.stderr)
    sys.exit(1)


def _fm_str(fm: dict, key: str) -> str:
    value = fm.get(key)
    return str(value) if value is not None else ""


def cmd_init(args) -> None:
    r = corpus.root()
    corpus.meta_dir(r).mkdir(parents=True, exist_ok=True)
    if not (r / ".gitignore").exists():
        (r / ".gitignore").write_text(GITIGNORE, encoding="utf-8")
    if not (r / "index.md").exists():
        (r / "index.md").write_text(INDEX_MD, encoding="utf-8")
    evals = corpus.meta_dir(r) / "evals.yaml"
    if not evals.exists():
        evals.write_text("# - query: ...\n#   expect: some-page.md\n#   top: 3\n", encoding="utf-8")
    if not (r / ".git").is_dir():
        subprocess.run(["git", "init", "-q"], cwd=r, check=True)
    print(f"initialized memoryfield at {r}")


def cmd_add(args) -> None:
    r = corpus.root()
    if args.text is not None:
        body = args.text
    elif args.file == "-" or args.file is None:
        body = sys.stdin.read()
    else:
        body = Path(args.file).read_text(encoding="utf-8")
    if not body.strip():
        _die("empty memory body")

    path = corpus.new_page(
        r,
        title=args.title,
        body=body,
        summary=args.summary,
        tags=args.tags.split(",") if args.tags else None,
        citations=args.citations.split(",") if args.citations else None,
    )
    size = path.stat().st_size
    if size > corpus.PAGE_LIMIT:
        print(
            f"warning: {path.name} is {size}B (> {corpus.PAGE_LIMIT}B); "
            "only the first 8192 bytes are embedded — consider splitting",
            file=sys.stderr,
        )
    index.upsert_page(r, path)
    fm, _ = corpus.parse_frontmatter(path.read_text(encoding="utf-8"))
    ledger.append(r, "write", path.name, uuid=_fm_str(fm or {}, "uuid") or None)
    print(f"wrote {path}")


def cmd_search(args) -> None:
    r = corpus.root()
    hits = index.search(r, args.query, args.k)
    for hit in hits:
        ledger.append(
            r,
            "search_hit",
            hit.filename,
            uuid=_fm_str(hit.frontmatter, "uuid") or None,
            query=args.query,
            score=round(hit.distance, 4),
        )
    if args.json:
        print(
            json.dumps(
                [
                    {"filename": h.filename, "distance": h.distance, "frontmatter": h.frontmatter}
                    for h in hits
                ],
                indent=2,
            )
        )
        return
    if not hits:
        print("no results")
        return
    for hit in hits:
        title = _fm_str(hit.frontmatter, "title") or hit.filename
        summary = _fm_str(hit.frontmatter, "summary")
        print(f"{hit.distance:.3f}  {hit.filename}  {title}")
        if summary:
            print(f"       {summary}")
    print("\nread with: mem show <filename> (parallel calls are fine)")


def cmd_show(args) -> None:
    r = corpus.root()
    for ref in args.refs:
        path = corpus.resolve(r, ref)
        if path is None:
            _die(f"no page {ref!r}")
        text = path.read_text(encoding="utf-8")
        fm, _ = corpus.parse_frontmatter(text)
        ledger.append(r, "read", path.name, uuid=_fm_str(fm or {}, "uuid") or None)
        if len(args.refs) > 1:
            print(f"===== {path.name} =====")
        print(text)


def cmd_reindex(args) -> None:
    embedded, removed = index.reindex(corpus.root(), full=args.full)
    print(f"embedded {embedded}, removed {removed}")


def cmd_hot(args) -> None:
    clusters = report.hot(corpus.root(), args.window_days, args.min_hits)
    if args.json:
        print(json.dumps(clusters, indent=2))
        return
    if not clusters:
        print(f"nothing over {args.min_hits} hits in {args.window_days}d")
        return
    for i, cluster in enumerate(clusters, 1):
        print(f"cluster {i}:")
        for page in cluster:
            print(
                f"  {page['hits']:>3} hits ({page['reads']} reads, "
                f"{page['search_hits']} surfaced)  {page['filename']}"
            )


def cmd_stats(args) -> None:
    for key, value in report.stats(corpus.root()).items():
        print(f"{key:>28}: {value}")


def cmd_eval(args) -> None:
    passed, failures = report.run_evals(corpus.root())
    for failure in failures:
        print(failure)
    print(f"{passed} passed, {len(failures)} failed")
    if failures:
        sys.exit(1)


def cmd_verify(args) -> None:
    r = corpus.root()
    problems = []
    warnings = []
    uuids: dict[str, str] = {}
    for path in corpus.pages(r):
        if not corpus.FILENAME_RE.match(path.name):
            problems.append(f"bad filename: {path.name}")
        text = path.read_text(encoding="utf-8")
        fm, _ = corpus.parse_frontmatter(text)
        if text.startswith("---\n") and fm is None:
            warnings.append(f"unparseable frontmatter: {path.name}")
        if fm and fm.get("uuid"):
            u = str(fm["uuid"])
            if u in uuids:
                problems.append(f"duplicate uuid {u}: {path.name} and {uuids[u]}")
            uuids[u] = path.name
        if path.stat().st_size > corpus.PAGE_LIMIT:
            warnings.append(f"over {corpus.PAGE_LIMIT}B (embed truncated): {path.name}")
    for line in problems + warnings:
        print(line)
    print(f"{len(problems)} problems, {len(warnings)} warnings")
    if problems:
        sys.exit(1)


def cmd_export(args) -> None:
    r = corpus.root()
    out = Path(args.out)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in corpus.pages(r):
            zf.write(path, path.name)
        if index.index_path(r).is_file():
            zf.write(index.index_path(r), index.index_path(r).name)
    print(f"exported {out}")


def main() -> None:
    parser = argparse.ArgumentParser(prog="mem", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="create the memoryfield skeleton").set_defaults(fn=cmd_init)

    p = sub.add_parser("add", help="write a new memory page")
    p.add_argument("--title", required=True)
    p.add_argument("--summary")
    p.add_argument("--tags", help="comma-separated")
    p.add_argument("--citations", help="comma-separated URLs/paths")
    p.add_argument("--text", help="body as an argument (else file/stdin)")
    p.add_argument("file", nargs="?", help="body file, or - for stdin")
    p.set_defaults(fn=cmd_add)

    p = sub.add_parser("search", help="semantic search over pages")
    p.add_argument("query")
    p.add_argument("-k", type=int, default=8)
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_search)

    p = sub.add_parser("show", help="print page(s) by filename or uuid")
    p.add_argument("refs", nargs="+")
    p.set_defaults(fn=cmd_show)

    p = sub.add_parser("reindex", help="refresh the vector index")
    p.add_argument("--full", action="store_true", help="re-embed everything")
    p.set_defaults(fn=cmd_reindex)

    p = sub.add_parser("hot", help="promotion candidates by ledger heat")
    p.add_argument("--window-days", type=int, default=30)
    p.add_argument("--min-hits", type=int, default=5)
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_hot)

    sub.add_parser("stats", help="corpus and usage metrics").set_defaults(fn=cmd_stats)
    sub.add_parser("eval", help="run golden retrieval fixtures").set_defaults(fn=cmd_eval)
    sub.add_parser("verify", help="spec-conformance checks").set_defaults(fn=cmd_verify)

    p = sub.add_parser("export", help="archive pages + index as a memoryfield zip")
    p.add_argument("--out", default="memories.memoryfield.zip")
    p.set_defaults(fn=cmd_export)

    args = parser.parse_args()
    try:
        args.fn(args)
    except RuntimeError as e:
        _die(str(e))


if __name__ == "__main__":
    main()
