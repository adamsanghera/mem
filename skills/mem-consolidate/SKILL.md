---
name: mem-consolidate
description: >-
  Run a consolidation pass over a `mem` memory corpus: apply outdated
  corrections, split pages past the embedding budget, verify due claims,
  extend partial pages, cross-link hot clusters, declare and fill bounties,
  propose graduations and demotions, then commit. Use when asked to
  consolidate, clean up, or audit memory, when `mem stats` shows outdated
  verdicts or due claims piling up, or on a weekly cadence.
---

# mem-consolidate

The consolidator edits the corpus deliberately. Agents write pages liberally
during their work, and dreams reflect on each session afterwards. This pass
makes the corpus as a whole trustworthy: corrections, splits, cross-links,
and merges across sessions. It and the dream runner own the corpus's git
commits, so ordinary agents never need to.

Two ways to run it:

- **Headless, from `mem dream`.** The runner starts this pass after dreams
  change the corpus, or on `mem dream consolidate`. Its prompt names a scope
  (pages changed since the last pass, pages with new `outdated` or `partial`
  verdicts, pages past the embedding budget). Limit every phase to those
  pages and their nearest neighbours. The runner holds the corpus lock,
  reindexes, settles skill changes, and commits after you finish.
- **By hand.** The whole corpus is in scope. Run `mem dream pause` first if
  dreams are installed, and `mem dream resume` after, so no dream commits
  under you.

## Hard rules

- Reads. When `MEM_SESSION_KIND=consolidate` is set (the runner sets it),
  use `mem show` and `mem search` freely: tagged reads don't count as heat.
  Otherwise read pages with the file-read tool and find related pages with
  `rg`, because untagged audit reads would inflate the heat this pass ranks
  by. `mem verify`, `mem stale`, `mem hot`, `mem bounties`, `mem stats`, and
  `mem eval` are read-only and always fine.
- Create pages only with `mem add`. It enforces the embedding window and
  logs the write.
- Edit existing pages in place, then `mem reindex` at the end. Keep
  frontmatter valid. Do not change `created` or `uuid`.
- Corrections replace. State the right fact and cite the superseding page.
  Never leave "previously we thought" inline.
- Mark what you touch. A claim you checked against its source gets
  `NOTE(verified:<id>:<today>)`. A claim you rewrote without checking gets
  `NOTE(unverified:<id>)`. Confidence is not evidence.
- Skills. Fix and improve an existing skill directly: a wrong step, a
  missing gotcha, a stale command, a sharper description. When in doubt,
  edit. A new skill, deleting one, or rewriting or extending most of one
  needs the owner's approval. Headless, make those changes in place too: the
  runner restores them and files them as proposals. By hand, describe them
  in the digest instead. Never edit the `mem` or `mem-consolidate` skills
  themselves; they are installed from the mem repo.
- Never hand-edit `meta/ledger.jsonl`, the sqlite index, or `meta/evals.yaml`
  (fix a page, not a fixture).
- Headless: do not run git, `mem reindex`, or the card sync. By hand: commit
  once, at the end, with a summary message. Nothing before.
- Prose: lead with the finding, one claim per sentence, no dashes.

## 0. Preflight

```bash
mem stats
mem verify
mem stale
mem hot --window-days 30 --min-hits 5
mem bounties
grep '"verdict":"outdated"' "${MEM_ROOT:-$HOME/memories}/meta/ledger.jsonl"
grep '"verdict":"partial"'  "${MEM_ROOT:-$HOME/memories}/meta/ledger.jsonl"
ls "${MEM_ROOT:-$HOME/memories}/meta/"   # read the previous digest
```

Decide the scope from the numbers. A pass with 40 or more pages to touch
should fan out to sub-agents with disjoint file sets (see the end).

## 1. Corrections

For every `outdated` verdict and every item on the previous digest's
suspected-wrong list: read the page, read the superseding page the note
names (or `rg` for it), rewrite the claim in place, cite the source, mark
the claim. If a page is wrong from title to body, rewrite it to lead with
the current state and keep the filename. Record anything you could not
resolve.

## 2. Size

For every `mem verify` warning past the embedding budget: keep the original
filename for the lead part (it holds the page's usage history and any eval
fixture), trim it to its core topic, move the rest into new single-topic
pages via `mem add`, carry citations over, and add `See also:` both ways.
Re-run `mem verify` until those warnings are gone.

## 3. Freshness

Take `mem stale` from the top: the hottest pages with due or unverified
claims. For each claim you can check (a config file, a console, a metric,
a command): check it. Holds: `mem feedback verified <page> --claim <id>`.
Wrong: fix the sentence, then `mem feedback outdated <page> --claim <id>
--note "..."`. A dated observation that was never a current-state claim gets
`NOTE(as-of:<date>)` instead. Pages with no markers at all get marked. Set a
budget (for example the top 15 pages) and stop there. The rest waits for
readers to verify at point of use.

## 4. Partial pages

For every `partial` verdict with a note naming the missing piece: add it,
citing the page or source that holds it. Report gaps that no page covers.

## 5. Hot clusters

For each cluster in `mem hot`: where two pages state the same fact, make one
the owner and have the other link to it in one line. Merge only when one
page fully subsumes the other (keep the more-recalled filename, fold in what
is unique, delete the other, record it). Cross-link liberally, merge rarely.

## 6. Bounties

Run the marker miner so declared gaps become bounties:

```bash
python "$(dirname "$(readlink -f "$(command -v mem)")")/../../scripts/mine_markers.py" 2>/dev/null \
  || python ~/code/mem/scripts/mine_markers.py        # the corpus; add doc-tree paths as arguments
mem bounties
```

Fill any bounty you can research in a few minutes with a cited `mem add`.
The board clears itself once a satisfying page exists.

## 7. Skills and proposals

- Graduation: pages that are procedural (how to do X) and earned `solved`
  or `partial`. If an existing skill (see the `skill-*.md` cards) owns the
  territory and the addition is small, add it to that skill directly.
  Otherwise it is a new skill or a large extension: follow the skills rule
  above.
- Demotion: skill cards with no ledger events in 30 days. Caveat: the
  ledger sees only mem-mediated events, so a cold card usually means its
  summary never matches how agents search, not that the skill is unused.
- Retrieval tuning: pages that keep earning `unrelated`, and cards that
  crowd results.

## 8. Finish

Headless: write the digest to the path the prompt names
(`meta/consolidation-<YYYY-MM-DD>-<HHMM>.md`), end with a one-line summary,
and stop. The runner does the rest. By hand:

```bash
python ~/code/mem/scripts/sync_skill_cards.py   # if skill cards are in use
mem reindex
mem eval      # 0 failed, or fix the page (not the fixture)
mem verify    # 0 problems; report remaining warnings
mem stats
```

Write `meta/consolidation-<YYYY-MM-DD>-<HHMM>.md`: corrections applied, pages split
(old to new filenames), claims verified or flagged, pages extended,
cross-links and merges, bounties filled, proposals, unresolved items, and
`mem stats` before and after. Then commit the corpus:

```bash
git -C "${MEM_ROOT:-$HOME/memories}" add -A
git -C "${MEM_ROOT:-$HOME/memories}" commit -m "consolidation pass <date>: <one line>"
```

## Fanning out

When a phase touches 40 or more pages, split the work by file, never by
phase. Build disjoint lists (`\ls -1 *.md | grep -v '^skill-'`, then split
into N files under /tmp), give each sub-agent one list, this skill's hard
rules, the phase instructions, and a digest path `meta/<pass>-part<N>.md`.
Sub-agents never touch files outside their list and never commit. The lead
merges the digests, runs step 8, reviews the diff, and commits.
