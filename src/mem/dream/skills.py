"""What dreams and consolidations may do to skills, enforced after each run.

Agents edit skills in place. Afterwards the runner compares every skill root
against a snapshot taken before the run and sorts each changed skill folder:

- a fix or small improvement to an existing skill is committed to the skill
  repo, with a pathspec so the owner's other uncommitted work stays put;
- a new skill, a deleted one, a large rewrite, a change to a file that already
  had uncommitted edits, a change to a proposal-only skill, or any change in a
  root outside git is restored byte for byte and written up as a proposal
  under meta/proposals/, with a diff `mem dream proposals accept` can apply.

Doing this after the fact, from bytes on disk, means the rules hold whatever
the agent believed it was doing.
"""

import difflib
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .. import corpus
from . import config

SKIP_DIRS = {".git", "__pycache__", "node_modules"}
NOTES_DIR = "skill-notes"


def _git(repo: Path, *args: str, input: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, input=input
    )


def repo_of(path: Path) -> Path | None:
    out = _git(path, "rev-parse", "--show-toplevel")
    return Path(out.stdout.strip()).resolve() if out.returncode == 0 else None


def _read_tree(root: Path) -> dict[str, bytes]:
    files = {}
    if not root.is_dir():
        return files
    for path in root.rglob("*"):
        if any(part in SKIP_DIRS for part in path.relative_to(root).parts):
            continue
        if path.is_file() and not path.is_symlink():
            files[path.relative_to(root).as_posix()] = path.read_bytes()
    return files


def _dirty(root: Path, repo: Path) -> set[str]:
    """Files under root with uncommitted changes, relative to root."""
    out = _git(repo, "status", "--porcelain", "-z", "--untracked-files=all", "--", str(root))
    dirty = set()
    entries = out.stdout.split("\0")
    i = 0
    while i < len(entries):
        entry = entries[i]
        i += 1
        if len(entry) < 4:
            continue
        status, rel = entry[:2], entry[3:]
        if "R" in status or "C" in status:
            i += 1  # the rename source follows
        absolute = (repo / rel).resolve()
        try:
            dirty.add(absolute.relative_to(root).as_posix())
        except ValueError:
            continue
    return dirty


@dataclass
class RootSnapshot:
    root: Path
    repo: Path | None
    files: dict[str, bytes]
    dirty: set[str]


def snapshot(cfg: dict) -> list[RootSnapshot]:
    snaps = []
    for raw in cfg["skill_roots"]:
        root = config.expand(raw)
        if not root.exists():
            continue
        root = root.resolve()
        repo = repo_of(root)
        snaps.append(
            RootSnapshot(root, repo, _read_tree(root), _dirty(root, repo) if repo else set())
        )
    return snaps


def off_limits(snaps: list[RootSnapshot], cfg: dict) -> list[str]:
    """What the instructions should tell the agent not to touch."""
    lines = [f"{s.root / rel}" for s in snaps for rel in sorted(s.dirty)]
    lines += [f"{s.root}/ (not in git: propose, don't edit)" for s in snaps if s.repo is None]
    return lines


def _skill_of(rel: str) -> str:
    return rel.split("/", 1)[0] if "/" in rel else ""


def _lines(data: bytes | None) -> list[str]:
    return data.decode("utf-8", errors="replace").splitlines(keepends=True) if data else []


def _churn(before: dict[str, bytes], after: dict[str, bytes], changed: set[str]) -> float:
    """Lines added plus lines removed, over the skill folder's lines before."""
    moved = 0
    for rel in changed:
        a, b = _lines(before.get(rel)), _lines(after.get(rel))
        for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b).get_opcodes():
            if tag != "equal":
                moved += (i2 - i1) + (j2 - j1)
    total = sum(len(_lines(data)) for data in before.values())
    return moved / max(total, 1)


def _diff(snap: RootSnapshot, before: dict, after: dict, changed: set[str]) -> str:
    base = snap.repo or snap.root
    chunks = []
    for rel in sorted(changed):
        name = (snap.root / rel).relative_to(base).as_posix()
        old, new = before.get(rel), after.get(rel)
        chunks.extend(
            difflib.unified_diff(
                _lines(old),
                _lines(new),
                fromfile=f"a/{name}" if old is not None else "/dev/null",
                tofile=f"b/{name}" if new is not None else "/dev/null",
            )
        )
    return "".join(line if line.endswith("\n") else line + "\n" for line in chunks)


def _restore(snap: RootSnapshot, before: dict, after: dict, changed: set[str]) -> None:
    for rel in changed:
        path = snap.root / rel
        if rel in before:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(before[rel])
        elif path.exists():
            path.unlink()
    for rel in sorted(changed, key=lambda p: -p.count("/")):
        parent = (snap.root / rel).parent
        while parent != snap.root and parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()
            parent = parent.parent


def proposals_dir(r: Path) -> Path:
    d = corpus.meta_dir(r) / "proposals"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write_proposal(r: Path, fm: dict, why: str, diff: str) -> Path:
    slug = corpus.slugify(f"{corpus.now_iso()[:10]}-{fm['skill'] or 'skills'}-{fm['kind']}")
    path = proposals_dir(r) / corpus.unique_filename(proposals_dir(r), slug)
    body = f"## Why\n\n{why.strip() or 'No note from the agent.'}\n\n## Diff\n\n```diff\n{diff}```\n"
    path.write_text(corpus.build_page(fm, body), encoding="utf-8")
    return path


@dataclass
class Outcome:
    committed: list[str] = field(default_factory=list)
    proposals: list[Path] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.committed or self.proposals)


def settle(
    r: Path,
    cfg: dict,
    snaps: list[RootSnapshot],
    *,
    workspace: Path,
    session: str,
    subject: str | None,
    summary: str,
) -> Outcome:
    """Sort every skill change made since `snaps` into commits and proposals."""
    outcome = Outcome()
    notes_dir = workspace / NOTES_DIR
    for snap in snaps:
        after = _read_tree(snap.root)
        changed_all = {
            rel for rel in set(snap.files) | set(after) if snap.files.get(rel) != after.get(rel)
        }
        accepted: list[str] = []
        accepted_skills: list[str] = []
        for skill in sorted({_skill_of(rel) for rel in changed_all}):
            changed = {rel for rel in changed_all if _skill_of(rel) == skill}
            before = {k: v for k, v in snap.files.items() if _skill_of(k) == skill}
            now = {k: v for k, v in after.items() if _skill_of(k) == skill}
            kind, reason = "edit", None
            if snap.repo is None:
                reason = f"{snap.root} is not in a git repo, so an edit could not be reverted"
            elif not skill:
                reason = "the change is outside any skill folder"
            elif skill in cfg["skill_proposal_only"]:
                reason = f"{skill} is proposal-only"
            elif changed & snap.dirty:
                reason = f"{', '.join(sorted(changed & snap.dirty))} had uncommitted edits"
            elif not before:
                kind, reason = "new", "a new skill"
            elif not now:
                kind, reason = "delete", "deletes the skill"
            else:
                churn = _churn(before, now, changed)
                if churn > cfg["skill_edit_max_churn"]:
                    kind = "rewrite"
                    reason = f"changes {churn:.0%} of the skill (limit {cfg['skill_edit_max_churn']:.0%})"
            if reason is None:
                accepted.extend(str(snap.root / rel) for rel in sorted(changed))
                accepted_skills.append(skill)
                continue
            note = notes_dir / f"{skill}.md"
            why = note.read_text(encoding="utf-8") if note.is_file() else ""
            base = snap.repo or snap.root
            fm = {
                "status": "open",
                "kind": kind,
                "skill": skill,
                "reason": reason,
                "repo": str(base),
                "paths": [(snap.root / rel).relative_to(base).as_posix() for rel in sorted(changed)],
                "session": session,
                "subject_session": subject,
                "created": corpus.now_iso(),
            }
            outcome.proposals.append(_write_proposal(r, fm, why, _diff(snap, before, now, changed)))
            _restore(snap, before, now, changed)
        if accepted and snap.repo:
            names = ", ".join(accepted_skills)
            _git(snap.repo, "add", "-A", "--", *accepted)
            done = _git(snap.repo, "commit", "-q", "-m", f"mem dream: {names}: {summary}", "--", *accepted)
            if done.returncode == 0:
                outcome.committed.extend(accepted_skills)
    if notes_dir.is_dir():
        for note in notes_dir.iterdir():
            note.unlink()
    return outcome


_DIFF_RE = re.compile(r"```diff\n(.*)```\n", re.S)


def load_proposals(r: Path) -> list[tuple[Path, dict, str]]:
    out = []
    for path in sorted(proposals_dir(r).glob("*.md")):
        fm, body = corpus.parse_frontmatter(path.read_text(encoding="utf-8"))
        if fm:
            out.append((path, fm, body))
    return out


def find_proposal(r: Path, ref: str) -> tuple[Path, dict, str]:
    name = ref if ref.endswith(".md") else f"{ref}.md"
    for path, fm, body in load_proposals(r):
        if path.name == name:
            return path, fm, body
    raise KeyError(f"no proposal {ref!r}")


def _save(path: Path, fm: dict, body: str) -> None:
    path.write_text(corpus.build_page(fm, body), encoding="utf-8")


def accept(r: Path, ref: str) -> str:
    """Apply a proposal's diff in its repo and commit only its paths."""
    path, fm, body = find_proposal(r, ref)
    if fm.get("status") != "open":
        raise ValueError(f"{path.name} is {fm.get('status')}")
    m = _DIFF_RE.search(body)
    repo = Path(fm["repo"])
    if not m or repo_of(repo) is None:
        raise ValueError(f"{path.name} has no diff, or {repo} is not a git repo")
    applied = _git(repo, "apply", "--whitespace=nowarn", "-", input=m.group(1))
    if applied.returncode != 0:
        fm["error"] = applied.stderr.strip()[:500]
        _save(path, fm, body)
        raise ValueError(f"git apply failed: {fm['error']}")
    paths = [str(repo / p) for p in fm.get("paths") or []]
    _git(repo, "add", "-A", "--", *paths)
    committed = _git(
        repo, "commit", "-q", "-m", f"mem: {fm['kind']} {fm['skill']} (proposal {path.stem})", "--", *paths
    )
    if committed.returncode != 0:
        raise ValueError(f"git commit failed: {committed.stderr.strip()[:500]}")
    fm["status"] = "accepted"
    fm.pop("error", None)
    _save(path, fm, body)
    return path.name


def reject(r: Path, ref: str, reason: str | None) -> str:
    path, fm, body = find_proposal(r, ref)
    fm["status"] = "rejected"
    if reason:
        fm["rejected_because"] = reason
    _save(path, fm, body)
    return path.name


def rejected_summaries(r: Path) -> list[str]:
    """One line per rejected proposal, so later runs don't make it again."""
    return [
        f"{fm.get('kind')} {fm.get('skill')}: {fm.get('reason')}"
        + (f" (rejected: {fm['rejected_because']})" if fm.get("rejected_because") else "")
        for _, fm, _ in load_proposals(r)
        if fm.get("status") == "rejected"
    ]

