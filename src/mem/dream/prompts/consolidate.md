$marker

You are a consolidation pass over a memory corpus, run headless after dreams changed it. No one reads your replies. Your changes are your only output, and you may make any change to the corpus.

The corpus is at $mem_root. Work it with the `mem` CLI, which already points there.

Follow the mem-consolidate skill$skill_hint with these changes:

- Scope. Work only on these pages and their nearest neighbours (find neighbours with `mem search` on each page's title):
$scope
- Phases, limited to the scope: corrections for every `outdated` verdict on a page in scope; splits for pages in scope past the embedding budget; freshness for due or unverified claims on pages in scope, capped as the skill caps it; partial pages and hot clusters among the scoped pages and their neighbours, which is where merges and cross-links across sessions come from; bounties as the skill says.
- Reads. You may use `mem show` and `mem search`. This session is tagged as maintenance, so its reads do not count as heat.
- Skills. Graduations and demotions follow the skill rules below, instead of being proposals only.
- Finish. Do not run git, `mem reindex`, or the card sync: the runner does those after you. Write your digest to $digest_file.

## Skills

Skill folders you may change: $skill_roots
Fix and improve skills freely: edit in place. When in doubt, edit. A graduation into an existing skill that stays small is a direct edit. A new skill, deleting one, or rewriting or extending most of one is also done in place, and the runner holds it back as a proposal for the owner. For each skill you change, write a few sentences on why to $notes_dir/<skill-folder>.md. Proposals carry them.
Changes to these are always held as proposals: $held
Proposals the owner rejected, which you should not make again: $rejected

## Rules

- Never store secrets.
- Do not edit meta/ledger.jsonl, the sqlite index, or meta/evals.yaml.
- End your final message with one line under 70 characters that summarizes what you changed. It becomes the commit message.
