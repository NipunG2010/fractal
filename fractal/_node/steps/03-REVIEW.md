---
requires_approval: false
---

## Review

Review the diff (`git status`, `git diff`) for mistakes, missed edge
cases, and style violations; fix and re-validate.

**Update memory** per the memory skill: fold this iteration's findings
into child pages, refresh the indexes, and split any bloated index into
child pages. Then `wiki update --path=$MEMORY_DIR` and
`wiki lint --path=$MEMORY_DIR`; iterate until clean.

**Project-wide learnings** (architecture, conventions, patterns useful
to other nodes) go in `$WIKI_DIR`, not node memory. After editing, run
`wiki update --path=$WIKI_DIR` and `wiki lint --path=$WIKI_DIR`.

Append a `## Post-Mortem` section (accomplishments, deviations,
next-iteration notes) to each plan you wrote this iteration -- list them
with `fractal plan list`. If you adopted a plan from an interrupted
earlier iteration, append to that plan instead and note it; if there is
no plan this iteration, skip.
