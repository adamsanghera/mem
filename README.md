# mem

Personal agent memory: a corpus of markdown pages with a sqlite vector index
(the on-disk layout follows Cal Paterson's
[memoryfield](https://calpaterson.com/memoryfields.html) format, so corpora
interoperate with other memoryfield tooling) plus the piece that format
doesn't have — a **hit ledger** (`meta/ledger.jsonl`) recording every search
hit and read, so that frequently-recalled memories can be nominated for
consolidation into longer-lived skills.

The design in one line: heat nominates, character decides, a human ratifies —
frequently-recalled procedural memories earn consolidation into agent skills,
and unmet demand (weak searches, misses) becomes a self-clearing bounty board.

## Setup

```bash
ollama pull nomic-embed-text   # and have ollama serving (brew services start ollama on macOS)
uv tool install --editable .
mem init                       # creates ~/memories (override with MEM_ROOT)
```

## Use

```bash
mem add --title "Pg vacuum quirk" --summary "..." --citations "https://..." --text "..."
mem search "why is vacuum slow" -k 8
mem show pg-vacuum-quirk.md          # logs a read in the ledger
mem reindex                          # incremental; --full to re-embed all
mem hot --window-days 30 --min-hits 5   # consolidation candidates
mem stats
mem eval                             # golden retrieval fixtures (meta/evals.yaml)
mem verify                           # corpus hygiene checks
mem export --out memories.memoryfield.zip
```

## Tests

```bash
uv run pytest
```
