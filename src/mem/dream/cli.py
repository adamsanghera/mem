"""`mem dream ...` subcommands."""

import argparse
import json
from collections import Counter
from pathlib import Path

from .. import corpus
from . import config, launchd, plan, run, skills, state


def _root() -> Path:
    return corpus.root()


def cmd_install(args) -> None:
    r = _root()
    cfg = config.load(r)
    path = launchd.install(r, cfg)
    minutes = int(cfg["interval_minutes"])
    every = f"{minutes // 60} hours" if minutes % 60 == 0 else f"{minutes} minutes"
    print(f"installed {path}; `mem dream run` every {every}")


def cmd_uninstall(args) -> None:
    removed = launchd.uninstall(config.load(_root()))
    print("uninstalled" if removed else "not installed")


def cmd_pause(args) -> None:
    config.set_value(_root(), "enabled", "false")
    print("paused: no new dreams start until `mem dream resume`")


def cmd_resume(args) -> None:
    config.set_value(_root(), "enabled", "true")
    print("resumed")


def cmd_config(args) -> None:
    r = _root()
    if args.action == "set":
        try:
            value = config.set_value(r, args.key, args.value)
        except KeyError as e:
            raise RuntimeError(str(e.args[0])) from e
        print(f"{args.key} = {value!r}")
        return
    cfg = config.load(r)
    if args.action == "get":
        print(json.dumps(cfg.get(args.key)))
        return
    for key in sorted(cfg):
        print(f"{key:>36}: {cfg[key]!r}")


def cmd_status(args) -> None:
    r = _root()
    cfg = config.load(r)
    runs = [e for e in state.load_log(r) if e.get("event") in ("dream", "consolidate")]
    verdicts = Counter(c.verdict for c in plan.plan(r, cfg))
    open_proposals = sum(1 for _, fm, _ in skills.load_proposals(r) if fm.get("status") == "open")
    rows = {
        "installed": launchd.plist_path(cfg["launchd_label"]).exists(),
        "loaded": launchd.loaded(cfg),
        "enabled": cfg["enabled"],
        "last run": runs[-1]["ts"] if runs else "never",
        "tokens 24h": f"{state.tokens_spent(r, 24)} / {cfg['daily_token_budget']}",
        "tokens 7d": f"{state.tokens_spent(r, 24 * 7)} / {cfg['weekly_token_budget']}",
        "ready to dream": verdicts.get("dream", 0),
        "waiting": sum(n for v, n in verdicts.items() if v.startswith("wait:")),
        "open proposals": open_proposals,
    }
    for key, value in rows.items():
        print(f"{key:>16}: {value}")


def cmd_plan(args) -> None:
    r = _root()
    cands = plan.plan(r, config.load(r), only=args.session)
    quiet = ("skip:old", "skip:subagent", "skip:dream")
    shown = cands if args.all or args.session else [c for c in cands if c.verdict not in quiet]
    if args.json:
        print(json.dumps([
            {"session": c.session, "verdict": c.verdict, "reason": c.reason, "estimate": c.estimate}
            for c in shown
        ], indent=2))
        return
    for c in shown:
        estimate = f"  ~{c.estimate} tokens" if c.estimate else ""
        print(f"{c.verdict:<13} {c.session}  {c.reason}{estimate}")
    hidden = Counter(c.verdict for c in cands if c not in shown)
    if hidden:
        print("hidden (--all): " + ", ".join(f"{v}={n}" for v, n in sorted(hidden.items())))


def cmd_digest(args) -> None:
    r = _root()
    cands = plan.plan(r, config.load(r), only=args.session)
    if not cands:
        raise RuntimeError(f"no transcript for session {args.session}")
    c = cands[0]
    if c.digest is None:
        print(f"{c.verdict}: {c.reason}")
        return
    print(c.digest.text)


def cmd_run(args) -> None:
    r = _root()
    run.run(
        r,
        config.load(r),
        session=args.session,
        dry_run=args.dry_run,
        consolidate_after=not args.no_consolidate,
    )


def cmd_consolidate(args) -> None:
    r = _root()
    run.consolidate_now(r, config.load(r), force=args.force)


def cmd_log(args) -> None:
    for e in state.load_log(_root())[-args.n :]:
        usage = e.get("usage") or {}
        tokens = int(usage.get("inputTokens") or 0) + int(usage.get("outputTokens") or 0)
        who = e.get("subject_session") or e.get("session") or ""
        commit = (e.get("commit") or "")[:10]
        detail = e.get("summary") or e.get("reason") or e.get("error") or ""
        print(f"{e['ts']}  {e['event']:<11} {e.get('status', ''):<8} {tokens:>7}t  {commit:<10} {who}  {detail}")


def cmd_proposals(args) -> None:
    r = _root()
    if args.action == "accept":
        try:
            print(f"accepted {skills.accept(r, args.id)}")
        except (KeyError, ValueError) as e:
            raise RuntimeError(str(e)) from e
        return
    if args.action == "reject":
        try:
            print(f"rejected {skills.reject(r, args.id, args.reason)}")
        except KeyError as e:
            raise RuntimeError(str(e.args[0])) from e
        return
    rows = [(p, fm) for p, fm, _ in skills.load_proposals(r) if args.all or fm.get("status") == "open"]
    if not rows:
        print("no open proposals")
    for path, fm in rows:
        print(f"{path.stem}  [{fm.get('status')}] {fm.get('kind')} {fm.get('skill')}: {fm.get('reason')}")


def register(sub: argparse._SubParsersAction) -> None:
    dream = sub.add_parser("dream", help="reflect on quiet sessions: plan, run, schedule")
    dsub = dream.add_subparsers(dest="dream_command", required=True)

    dsub.add_parser("install", help="install the launchd job").set_defaults(fn=cmd_install)
    dsub.add_parser("uninstall", help="remove the launchd job").set_defaults(fn=cmd_uninstall)
    dsub.add_parser("pause", help="start no new dreams").set_defaults(fn=cmd_pause)
    dsub.add_parser("resume", help="undo pause").set_defaults(fn=cmd_resume)
    dsub.add_parser("status", help="schedule, budget, and queue").set_defaults(fn=cmd_status)

    p = dsub.add_parser("config", help="show or change settings")
    p.add_argument("action", nargs="?", choices=["get", "set"])
    p.add_argument("key", nargs="?")
    p.add_argument("value", nargs="?", help="parsed as YAML: 2, true, [a, b]")
    p.set_defaults(fn=cmd_config)

    p = dsub.add_parser("plan", help="every session's verdict and reason; spends no tokens")
    p.add_argument("--session")
    p.add_argument("--all", action="store_true", help="include old, subagent, and dream sessions")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_plan)

    p = dsub.add_parser("digest", help="print the digest a dream would get")
    p.add_argument("session")
    p.set_defaults(fn=cmd_digest)

    p = dsub.add_parser("run", help="dream now (what launchd runs)")
    p.add_argument("--session", help="dream about this session regardless of idle and size")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--no-consolidate", action="store_true")
    p.set_defaults(fn=cmd_run)

    p = dsub.add_parser("consolidate", help="the consolidation step alone")
    p.add_argument("--force", action="store_true", help="ignore the size and budget caps")
    p.set_defaults(fn=cmd_consolidate)

    p = dsub.add_parser("log", help="recent dreams and consolidations")
    p.add_argument("-n", type=int, default=20)
    p.set_defaults(fn=cmd_log)

    p = dsub.add_parser("proposals", help="skill changes held for approval")
    p.add_argument("action", nargs="?", choices=["accept", "reject"])
    p.add_argument("id", nargs="?")
    p.add_argument("--reason")
    p.add_argument("--all", action="store_true", help="include accepted and rejected")
    p.set_defaults(fn=cmd_proposals)
