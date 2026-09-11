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

## Setup

Requires a running [ollama](https://ollama.com) and
[uv](https://docs.astral.sh/uv/).

```bash
ollama pull nomic-embed-text
uv tool install --editable .
mem init            # creates ~/memories; override with MEM_ROOT
```

## Agent skill

[`skills/mem/SKILL.md`](skills/mem/SKILL.md) teaches an agent the loop:
search first, rate what it read, record what it learned. Install it into
your harness's skills directory:

```bash
npx skills add adamsanghera/mem           # skills-compatible harnesses
cp -r skills/mem ~/.cursor/skills/mem     # Cursor
cp -r skills/mem ~/.claude/skills/mem     # Claude Code
```

Skills load when their description matches the task. For guaranteed
awareness, add a few lines to the file your agent always reads (AGENTS.md,
CLAUDE.md, or an always-on rule):

```markdown
You have persistent memory through the `mem` CLI. Search it before
unfamiliar work (`mem search "..."`), rate the pages you read once the
outcome is known (`mem feedback`), and save non-obvious learnings before
wrapping up (`mem add`). Details in the mem skill.
```

## Writing and recalling

```bash
mem search "why is vacuum slow" -k 8
mem show pg-vacuum-quirk.md
mem add --title "Pg vacuum quirk" \
  --summary "One sentence for search results." \
  --citations "https://source,path/to/code" \
  --text "One topic, under 8KB, with citations."
mem feedback solved pg-vacuum-quirk.md --note "exact fix applied"
mem reindex         # after hand-editing pages; --full re-embeds everything
```

Search and show log every hit and read. `mem feedback` rates a recall once
the task outcome is known. The verdicts: solved, partial, context,
unrelated, outdated, and miss. A miss takes no page name: it records that
nothing useful existed. A search with no close match logs the same kind of
gap on its own.

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
