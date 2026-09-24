---
name: mem
description: >-
  Persistent memory for coding agents through the `mem` CLI: semantic search
  over a directory of markdown pages, plus a ledger recording what was recalled
  and whether it helped. Use at the start of any task in an unfamiliar area,
  when an error or behavior seems familiar, when the user says "remember this"
  or "have we seen this before", after a fix that took real digging, when the
  user corrects an assumption, and before wrapping up any task where something
  non-obvious was learned.
---

# mem

`mem` is a CLI over a directory of markdown memories (default `~/memories`,
override with `MEM_ROOT`). Every search, read, and rating goes through it and
lands in a ledger. The ledger decides which memories deserve curation and
which gaps deserve a page, so use the CLI rather than grepping the directory.

The loop: search, read, work, report back, record what is new.

## 1. Recall

```bash
mem prime --for "the task in one line"
mem search "the question, phrased naturally" -k 8
mem show <filename> <filename>
```

`mem prime` is the first command of every session. It lists the standing
pages every session should know about, what other sessions leaned on
recently, which skills recently helped (load the ones that fit the task),
open bounties, and the top hits for your task.

Search before starting work in an area you have not touched this session,
and whenever an error looks like something seen before. Lower distance is a
closer match, and under ~0.45 is usually relevant. If the CLI reports weak
results, that search is now an open bounty: nothing close exists. Solve the
problem, then write the page that should have existed.

## 2. Report back (required whenever you read memories)

Once the outcome is known, rate every page you read:

```bash
mem feedback solved <filename> --note "exact fix applied"
mem feedback unrelated <filename> <filename>
mem feedback miss --note "what was needed and did not exist"
```

| verdict     | meaning                                              |
|-------------|------------------------------------------------------|
| `solved`    | directly solved the problem or answered the question |
| `partial`   | led toward the solution but was incomplete           |
| `context`   | helpful background, not itself the answer            |
| `unrelated` | surfaced but irrelevant to the task                  |
| `outdated`  | relevant but stale or wrong, needs correction        |
| `miss`      | nothing useful existed (takes no page name)          |

Add `--note` for anything other than solved or context. The note is what a
later curation pass learns from. After a miss, write the missing page. If a
page was outdated, fix it and run `mem reindex`.

## 3. Record

Write a memory when any of these happen:

- A fix or answer took real digging: several attempts, reading source,
  chasing logs or docs. Save the conclusion, not the journey.
- The user corrected an assumption, fact, or approach. Save the correction
  and the reason.
- You hit a tool, API, or configuration quirk that will bite again.
- A decision was made with a rationale: chose X over Y because Z.
- You learned a stable, non-obvious fact about a person, team, service, or
  codebase.
- You rated a memory partial or outdated. Extend or fix the page.

Put a fact where it lives. If it belongs to one module, write it into that
module's code or docs. Memory is for knowledge with no single home in code:
cross-module shape, incidents, decisions, tool quirks, people. When in
doubt, write it. Irrelevant pages are never surfaced, so the only bad memory
is the unwritten one.

```bash
mem add --title "Short specific title" \
  --summary "One sentence for search results." \
  --citations "https://source,path/or/id" \
  --text "One topic. Prose. Under 8KB. Cite sources."
```

Rules: one topic per page, and split rather than exceed 8KB. Always cite
sources. Never store secrets or credentials. Never edit the sqlite index or
`meta/ledger.jsonl` by hand. Run `mem reindex` after hand-editing a page.

## Wrap-up, before the final summary

1. If you read memories this session, rate each one.
2. If a write trigger fired, save the page now.
3. Mention each save in the summary as "saved memory: <title>" so the user
   can veto or refine it.

## Maintenance, only when asked

`mem hot` lists pages recalled often, with their verdicts. `mem bounties`
lists gaps: searches that found nothing and declared misses, clustered, and
cleared once a page satisfies them. `mem stats` reports usage and the
helpful rate. `mem eval` runs retrieval fixtures from `meta/evals.yaml`.
Add a fixture whenever a search misses a page it should have found.
Promoting hot pages into skills or docs is a human-reviewed step: propose,
never auto-write.

If embedding fails, ollama must be serving (`ollama serve`, or set
`OLLAMA_URL`) with the model pulled (`ollama pull nomic-embed-text`). Setup
lives in the repository README.
