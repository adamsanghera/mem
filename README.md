# mem

Personal agent memory: a [memoryfield-spec](https://github.com/calpaterson/memoryfield-spec)
corpus (markdown pages + sqlite vector index) plus the piece the spec doesn't
have — a **hit ledger** (`meta/ledger.jsonl`) recording every search hit and
read, so that frequently-recalled memories can be nominated for consolidation
into longer-lived skills.

Design doc: `.adam/personal-memory/spec.md` in the everysphere checkout.

## Setup

```bash
brew services start ollama && ollama pull nomic-embed-text
uv tool install --editable ~/code/mem
mem init            # creates ~/memories (override with MEM_ROOT)
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
mem verify                           # spec-conformance checks
mem export --out memories.memoryfield.zip
```

## Tests

```bash
uv run pytest
```
