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

from . import corpus, embed, fresh, index, ledger, report

GITIGNORE = "nomic-embed-text-v1.5.sqlite3\n"

SKILL_URL = "https://raw.githubusercontent.com/adamsanghera/mem/main/skills/mem/SKILL.md"

NEXT_STEPS = f"""
next steps:
  1. install the agent skill for your harness (any directory of <name>/SKILL.md works):
       mkdir -p ~/.cursor/skills/mem && curl -fsSL {SKILL_URL} -o ~/.cursor/skills/mem/SKILL.md
       Claude Code: same file into ~/.claude/skills/mem/SKILL.md
  2. add to the file your agent always reads (AGENTS.md, CLAUDE.md, or a rule):
       You have persistent memory through the `mem` CLI. Start every session with
       `mem prime --for "<the task>"`. Search before unfamiliar work (mem search),
       rate the pages you read once the outcome is known (mem feedback), and save
       non-obvious learnings before wrapping up (mem add).
  3. write the first memory:
       mem add --title "..." --summary "..." --citations "..." --text "..."
  4. mem prime
"""

# union-merge the ledger: concurrent appends from different machines are both
# kept, which is the correct resolution for an append-only event log
GITATTRIBUTES = "meta/ledger.jsonl merge=union\n"

INDEX_MD = """---
title: Index
summary: What this memoryfield is.
---

Personal memories of this corpus's owner and their agents: incidents, tool
quirks, decisions and their reasons, hard-won facts. Written liberally, one
topic per page, with citations. See meta/ for the hit ledger and retrieval
evals.
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
    if not (r / ".gitattributes").exists():
        (r / ".gitattributes").write_text(GITATTRIBUTES, encoding="utf-8")
    if not (r / "index.md").exists():
        (r / "index.md").write_text(INDEX_MD, encoding="utf-8")
    evals = corpus.meta_dir(r) / "evals.yaml"
    if not evals.exists():
        evals.write_text("# - query: ...\n#   expect: some-page.md\n#   top: 3\n", encoding="utf-8")
    if not (r / ".git").is_dir():
        subprocess.run(["git", "init", "-q"], cwd=r, check=True)
    print(f"initialized memory corpus at {r}")
    _ready, message = embed.status()
    print(message)
    print(NEXT_STEPS)


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

    path, text = corpus.render_page(
        r,
        title=args.title,
        body=body,
        summary=args.summary,
        tags=args.tags.split(",") if args.tags else None,
        citations=args.citations.split(",") if args.citations else None,
        volatility=args.volatility,
    )
    raw = text.encode("utf-8")
    if len(raw) > corpus.PAGE_LIMIT:
        _die(
            f"page would be {len(raw)}B, over the {corpus.PAGE_LIMIT}B limit. "
            "Nothing written. Split it into pages of one topic each."
        )
    # embed the exact bytes first, without truncation: a page the index
    # cannot fully represent is rejected rather than written with an
    # unsearchable tail
    try:
        [vector] = embed.embed_texts([index.embed_input(raw)], truncate=False)
    except embed.InputTooLong:
        _die(
            f"page would be {len(raw)}B, too long for the embedding model's "
            "2048-token window (about 6KB of prose). Nothing written. Split it "
            "into pages of one topic each."
        )
    path.write_text(text, encoding="utf-8")
    index.insert_vector(r, path, vector)
    fm, _ = corpus.parse_frontmatter(text)
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
    best = min((h.distance for h in hits), default=None)
    weak = best is None or best > ledger.WEAK_BEST_DISTANCE
    prior_demand = None
    if weak:
        # measure demand before appending, so the count excludes this search
        prior_demand = report.demand_for(r, args.query)
        ledger.append(
            r,
            "weak_search",
            None,
            query=args.query,
            score=round(best, 4) if best is not None else None,
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
    events = ledger.load(r)
    for hit in hits:
        title = _fm_str(hit.frontmatter, "title") or hit.filename
        summary = _fm_str(hit.frontmatter, "summary")
        f = fresh.page_freshness(r, hit.filename, events)
        badge = f"  [{f.line()}]" if f else ""
        print(f"{hit.distance:.3f}  {hit.filename}  {title}{badge}")
        if summary:
            print(f"       {summary}")
    print("\nread with: mem show <filename> (parallel calls are fine)")
    if weak:
        hits_before = sum(prior_demand.values()) if prior_demand else 0
        if hits_before:
            breakdown = " ".join(f"{k}={v}" for k, v in sorted(prior_demand.items()))
            print(
                f"weak results — OPEN BOUNTY: this gap has been hit "
                f"{hits_before} time(s) before ({breakdown}) and still has no "
                "page. If this task teaches you the answer, close the bounty: "
                "`mem add` one cited page, and every future session that hits "
                "this recalls your page instead of re-deriving it."
            )
        else:
            print(
                "weak results — bounty opened: you're the first to hit this "
                "gap. If you work out the answer during this task, close it "
                "with `mem add` (one cited page); also log `mem feedback miss "
                "--note '...'` if a page clearly should have existed."
            )


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
        f = fresh.page_freshness(r, path.name)
        if f:
            hint = (
                " Due or unverified claims are hypotheses until you check them; "
                "then `mem feedback verified <page> --claim <id>`."
                if f.is_due
                else ""
            )
            print(f"[mem] freshness: {f.line()}.{hint}")


def cmd_feedback(args) -> None:
    r = corpus.root()
    if args.verdict == "miss":
        if args.refs:
            _die("miss records a recall gap, not a page rating; drop the page refs")
        if not args.note:
            _die("miss needs --note describing what was missing")
        if args.key and ledger.has_key(r, args.key):
            print(f"already registered: {args.key}")
            return
        ledger.append(
            r, "feedback", None, verdict="miss", note=args.note, source=args.source, key=args.key
        )
        print("recorded miss — now write the memory (mem add) and consider an eval fixture")
        return
    if not args.refs:
        _die("pass at least one page (filename or uuid)")
    if args.claim and len(args.refs) != 1:
        _die("--claim names one claim in one page; pass a single page")
    for ref in args.refs:
        path = corpus.resolve(r, ref)
        if path is None:
            _die(f"no page {ref!r}")
        if args.verdict == "verified" and args.claim:
            if not fresh.stamp_verified(path, args.claim):
                _die(
                    f"no NOTE(unverified:{args.claim}) or NOTE(verified:{args.claim}:...) "
                    f"marker in {path.name}; add one beside the claim first"
                )
            index.upsert_page(r, path)
        fm, _ = corpus.parse_frontmatter(path.read_text(encoding="utf-8"))
        ledger.append(
            r,
            "feedback",
            path.name,
            uuid=_fm_str(fm or {}, "uuid") or None,
            verdict=args.verdict,
            note=args.note,
            claim=args.claim,
        )
        suffix = f" (claim {args.claim})" if args.claim else ""
        print(f"{args.verdict}: {path.name}{suffix}")


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
            fb = page.get("feedback") or {}
            fb_str = (
                "  [" + " ".join(f"{k}={fb[k]}" for k in sorted(fb)) + "]" if fb else ""
            )
            print(
                f"  {page['hits']:>3} hits ({page['reads']} reads, "
                f"{page['search_hits']} surfaced)  {page['filename']}{fb_str}"
            )


def cmd_prime(args) -> None:
    r = corpus.root()
    briefing = report.prime(r, args.task)
    ledger.append(r, "prime", None, query=args.task)
    if args.json:
        print(json.dumps(briefing, indent=2))
        return

    events = ledger.load(r)

    def badge(filename: str) -> str:
        f = fresh.page_freshness(r, filename, events)
        return f"  [{f.line()}]" if f and f.is_due else ""

    print("memory briefing (mem prime). Read any page with: mem show <filename>")
    if briefing["standing"]:
        print("\nstanding orders (know these exist; read when relevant):")
        for page in briefing["standing"]:
            print(f"  {page['filename']}{badge(page['filename'])}")
            if page["summary"]:
                print(f"    {page['summary']}")
    if briefing["hot"]:
        print("\nhot this fortnight (what other sessions leaned on):")
        for page in briefing["hot"]:
            print(f"  {page['hits']:>3}  {page['filename']}{badge(page['filename'])}")
    if briefing["skills"]:
        print("\nskills that helped recently (read the card, then the skill it points to):")
        for card in briefing["skills"]:
            print(f"  {card['filename']}  ({card['helpful']} helpful)")
            if card["summary"]:
                print(f"    {card['summary']}")
    if briefing["bounties"]:
        print("\nopen bounties (pages someone wanted; close one if your task touches it):")
        for bounty in briefing["bounties"]:
            print(f"  x{bounty['count']}  {bounty['text']!r}")
    if briefing["task"]:
        print("\nrelevant to your task:")
        for hit in briefing["task"]:
            print(f"  {hit['distance']:.3f}  {hit['filename']}{badge(hit['filename'])}")
    print(
        "\nthe loop: mem search before unfamiliar work · mem feedback <verdict> <pages> "
        "once the outcome is known · mem add what a future session would want"
    )


def cmd_stale(args) -> None:
    rows = fresh.stale(corpus.root(), args.window_days)
    if args.json:
        print(json.dumps(rows, indent=2))
        return
    if not rows:
        print("no pages with due or unverified claims")
        return
    for row in rows:
        print(f"{row['hits']:>3} hits  {row['filename']}  [{row['freshness']}]")
        for claim in row["unverified"]:
            print(f"           unverified: {claim}")
        for claim in row["due"]:
            print(f"           due: {claim}")
    print(
        "\nverify a claim against its source, then: mem feedback verified <page> --claim <id>. "
        "Wrong? Fix the text, then: mem feedback outdated <page> --claim <id> --note '...'"
    )


def cmd_bounties(args) -> None:
    clusters = report.bounties(corpus.root(), args.window_days)
    if args.json:
        print(json.dumps(clusters, indent=2))
        return
    if not clusters:
        print(f"no unmet-demand signals in {args.window_days}d")
        return
    for i, cluster in enumerate(clusters, 1):
        kinds = " ".join(f"{k}={v}" for k, v in sorted(cluster["kinds"].items()))
        print(f"bounty {i}: {cluster['count']} signal(s), last {cluster['last'][:10]}  [{kinds}]")
        for text in cluster["texts"][:3]:
            print(f"    {text!r}")
        for source in cluster.get("sources", [])[:3]:
            print(f"    declared in: {source}")
    print("\nfill a bounty: research it, then `mem add` the page(s) it wanted")


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
        markers = fresh.parse_markers(text)
        for marker in markers:
            if marker.error:
                warnings.append(f"malformed marker {marker.raw} ({marker.error}): {path.name}")
        seen_ids = [m.id for m in markers if m.id and m.kind in ("unverified", "verified")]
        for dup in sorted({i for i in seen_ids if seen_ids.count(i) > 1}):
            warnings.append(f"claim id {dup!r} used more than once: {path.name}")
        if not seen_ids and fresh.volatility_for(fm or {}, path.name, markers) != "stable":
            warnings.append(f"no claim markers, reads as unverified: {path.name}")
        size = path.stat().st_size
        if size > corpus.PAGE_LIMIT:
            problems.append(f"over the {corpus.PAGE_LIMIT}B page limit ({size}B): {path.name}")
        elif size > corpus.EMBED_BYTE_BUDGET:
            warnings.append(
                f"past the ~{corpus.EMBED_BYTE_BUDGET}B embedding budget ({size}B), "
                f"tail unsearchable: {path.name}"
            )
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
    p.add_argument(
        "--volatility",
        choices=list(fresh.HALF_LIFE_DAYS),
        help="how fast the page's claims decay: fast (14d), slow (90d, default), stable (never)",
    )
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

    p = sub.add_parser(
        "feedback",
        help="rate how recalled memories related to the task outcome",
        description="Rate recalled memories so heat reflects usefulness, not just access.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="verdicts:\n"
        + "\n".join(f"  {k:<10} {v}" for k, v in ledger.VERDICTS.items()),
    )
    p.add_argument("verdict", choices=list(ledger.VERDICTS))
    p.add_argument("refs", nargs="*", help="page filename(s) or uuid(s); none for miss")
    p.add_argument("--note", help="why — expected for partial/unrelated/outdated/miss")
    p.add_argument(
        "--claim",
        help="the claim id this verdict is about; with verified, stamps the page's marker",
    )
    p.add_argument("--source", help="miss only: where the gap was declared (a spec, doc, or thread)")
    p.add_argument(
        "--key", help="miss only: stable id for a declared gap; re-registering it is a no-op"
    )
    p.set_defaults(fn=cmd_feedback)

    p = sub.add_parser("reindex", help="refresh the vector index")
    p.add_argument("--full", action="store_true", help="re-embed everything")
    p.set_defaults(fn=cmd_reindex)

    p = sub.add_parser("hot", help="promotion candidates by ledger heat")
    p.add_argument("--window-days", type=int, default=30)
    p.add_argument("--min-hits", type=int, default=5)
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_hot)

    p = sub.add_parser(
        "prime",
        help="session-start briefing: standing pages, recent heat, helpful skills, bounties",
    )
    p.add_argument("--for", dest="task", help="the task at hand; adds the top hits for it")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_prime)

    p = sub.add_parser("stale", help="pages with due or unverified claims, hottest first")
    p.add_argument("--window-days", type=int, default=30, help="heat window for ordering")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_stale)

    p = sub.add_parser("bounties", help="unmet-demand board: weak searches + misses, clustered")
    p.add_argument("--window-days", type=int, default=90)
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_bounties)

    sub.add_parser("stats", help="corpus and usage metrics").set_defaults(fn=cmd_stats)
    sub.add_parser("eval", help="run golden retrieval fixtures").set_defaults(fn=cmd_eval)
    sub.add_parser("verify", help="corpus hygiene checks").set_defaults(fn=cmd_verify)

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
