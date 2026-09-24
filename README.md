# mem

Agent memory as plain files. An agent writes markdown pages as it works,
finds them again by semantic search, and records whether each recall helped.
Usage accumulates in a ledger, and the ledger answers two questions: which
memories deserve promotion into curated docs or skills, and which memories
people wanted but nobody has written.

The corpus is a flat directory of markdown pages plus a sqlite vector index.
The layout follows Cal Paterson's
[memoryfield format](https://calpaterson.com/memoryfields.html), so other
memoryfield tooling can read it. The index is a cache: delete it and
`mem reindex` rebuilds it from the pages. This tool's additions live in
`meta/`, the usage ledger (`ledger.jsonl`) and retrieval fixtures
(`evals.yaml`), so the pages themselves stay plain markdown.

## What belongs here

Knowledge with no single home in code. If a fact belongs to one module,
write it there, in a comment or a doc beside the code. `mem` is for what
crosses those boundaries. That means incidents and their root causes,
decisions and the reasons behind them, and quirks of tools and vendors. It
also means the shape of a system that spans repos, and the preferences of
the people you work with. The test for a page: a future reader finds it
faster here than by re-deriving it from source.

## Quick start

macOS or Linux. Every step is safe to re-run. An agent can execute this
block as-is.

```bash
# 1. uv (runs the CLI) and ollama (local embeddings), if missing
command -v uv >/dev/null || curl -LsSf https://astral.sh/uv/install.sh | sh
if ! command -v ollama >/dev/null; then
  if command -v brew >/dev/null; then brew install ollama; else curl -fsSL https://ollama.com/install.sh | sh; fi
fi
command -v brew >/dev/null && brew services start ollama   # Linux installer starts its own service

# 2. the embedding model (270MB, one time)
ollama pull nomic-embed-text

# 3. mem itself, and an empty corpus at ~/memories (override with MEM_ROOT)
uv tool install git+https://github.com/adamsanghera/mem
mem init
```

`mem init` checks that ollama is serving with the model present and prints
the remaining steps: installing the agent skill and adding the always-on
snippet. Developing mem itself: clone, then `uv tool install --editable .`.

## Agent skill

[`skills/mem/SKILL.md`](skills/mem/SKILL.md) teaches an agent the loop:
search first, rate what it read, record what it learned. Install it into
your harness's skills directory:

```bash
# Cursor (no clone needed); Claude Code uses ~/.claude/skills/mem/SKILL.md
mkdir -p ~/.cursor/skills/mem && curl -fsSL \
  https://raw.githubusercontent.com/adamsanghera/mem/main/skills/mem/SKILL.md \
  -o ~/.cursor/skills/mem/SKILL.md
npx skills add adamsanghera/mem           # skills-compatible harnesses
```

Skills load when their description matches the task. For guaranteed
awareness, add a few lines to the file your agent always reads (AGENTS.md,
CLAUDE.md, or an always-on rule):

```markdown
You have persistent memory through the `mem` CLI. Start every session with
`mem prime --for "<the task>"`. Search before unfamiliar work
(`mem search "..."`), rate the pages you read once the outcome is known
(`mem feedback`), and save non-obvious learnings before wrapping up
(`mem add`). Details in the mem skill.
```

## Writing and recalling

```bash
mem prime --for "the task in one line"
mem search "why is vacuum slow" -k 8
mem show pg-vacuum-quirk.md
mem add --title "Pg vacuum quirk" \
  --summary "One sentence for search results." \
  --citations "https://source,path/to/code" \
  --text "One topic, under 6KB, with citations."
mem feedback solved pg-vacuum-quirk.md --note "exact fix applied"
mem reindex         # after hand-editing pages; --full re-embeds everything
```

Keep a page under about 6KB. Ollama embeds only the first 2048 tokens (about
6KB of technical prose) and drops the rest silently, so a longer page is
still readable but unfindable by its tail. `mem add` refuses a page the
model cannot fully embed and writes nothing; `mem verify` lists existing
pages past the budget.

`mem prime` is the session-start briefing, in about 700 tokens: pages
tagged `prime` (standing orders every session should know exist), the
pages other sessions leaned on this fortnight, skills whose cards earned
helpful verdicts this month, open bounties, and the top hits for the task.
Search and show log every hit and read. `mem feedback` rates a recall once
the task outcome is known. The verdicts: solved, partial, context,
unrelated, outdated, and miss. A miss takes no page name: it records that
nothing useful existed. A search with no close match logs the same kind of
gap on its own.

## Freshness

Pages carry their uncertainty inline, next to the claim, with the marker
grammar scaffold-style doc trees also use:

```
NOTE(unverified:pool-count)             a hypothesis; due for checking now
NOTE(verified:pool-count:2026-09-24)    checked against its source that day
NOTE(as-of:2026-09-22)                  an observation; ages, never wrong
TODO(investigate:deploy-cut-times)      a declared gap; becomes a bounty
```

A page's `volatility` (`fast` 14d, `slow` 90d by default, `stable` never)
sets how long a verified claim holds before it comes due. The default is
pessimistic: a non-stable page with no markers never said which of its
claims were observed, so it reads as unverified until someone marks it.
Confidence of wording is not evidence; the burden of proof is on the page.
Every search hit and shown page prints a freshness line, for example
`fast · 2 claims due · 1 unverified`. Nothing is hidden or reranked:
staleness is shown, and the reader checks the claim at the moment they
are about to act on it.

```bash
mem feedback verified <page> --claim pool-count   # stamps the marker with today
mem stale                                         # due or unverified claims, hottest first
python scripts/mine_markers.py docs/              # TODO(investigate:) markers -> bounties
```

## Reading the ledger

```bash
mem hot         # pages recalled often, with their feedback breakdown
mem bounties    # gaps: repeated searches that never found a page
mem stats       # corpus size, search volume, helpful rate
mem eval        # retrieval regression fixtures (meta/evals.yaml)
```

A hot page that keeps earning "solved" is a candidate for promotion into
something curated, like an agent skill. A bounty is the reverse signal: a
page somebody wanted and nobody wrote. Bounties come from two places.
Searches that find nothing register one automatically. Known gaps, such as
open questions in a spec or a design doc, can be declared directly:

```bash
mem feedback miss --note "which stores hold pool-derived diff data" \
  --source docs/90-cache-topology.md --key investigate:diff-stores
```

The key makes re-declaring the same gap a no-op, so a script that scans
docs for open questions can run repeatedly. The board clears itself once a
satisfying page exists.

## Housekeeping

```bash
mem verify      # filename, frontmatter, and size checks
mem export      # zip the corpus for sharing or backup
uv run pytest   # unit tests; no ollama needed
```
