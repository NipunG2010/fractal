---
name: wiki
description: The wiki CLI and the node's two knowledge bases -- project wiki and memory.
---

# Wiki

A wiki is an indexed folder tree of `_index.md` files. A node works with
two:

- **Project wiki** (`$WIKI_DIR`) — shared across nodes; per-branch,
  reaching others only through merges (use `fractal radio` for live
  coordination). Holds architecture, conventions, and patterns;
  contribute durable project-wide knowledge here.
- **Memory** (`$MEMORY_DIR`) — this node's private knowledge base. See
  the `memory` skill for how to maintain it.

Todo lists follow the same split: a private working checklist lives in
memory; a task list other nodes should see and track lives in the
project wiki. Either way it is living state — current open items, pruned
as they complete — never an append-only log.

Run `wiki --help` and `wiki <command> --help` for the CLI (init, update,
lint, map, search, read). Always pass `--path` (`$WIKI_DIR` or
`$MEMORY_DIR`) — the wiki CLI does not walk up to find a wiki from the
node directory. Run `wiki update --path=<dir>` after adding, moving, or
deleting pages; `wiki lint --path=<dir>` validates structure.

## Editing discipline

`wiki update` regenerates derived state: each page's entry line in
`_index.md` (name and description) is pulled from the page's own
frontmatter, so fix a `desc:` on the page and rerun update — hand edits
to an index line are overwritten by the next update. `wiki lint` is
regenerate-and-compare: it prints the diff `wiki update` would apply
plus any real defects, and separates issues (must fix) from advisory
notes. Work the loop — edit pages, `wiki update`, `wiki lint` — until
clean (clean = lint exits 0; scripts branch on the exit code, not the
prose summary); lint validates structure, not content truth, so verify
facts against your sources yourself.

## Cross-linking

Cross-reference aggressively — the links between pages are the wiki's
primary value — but **link only to pages that already exist.** Sibling
nodes build their pages in parallel, so a page you would link to may not
exist yet: defer that forward link rather than emit a wikilink to a
not-yet-created page. Stale sibling links are an expected transient, not
a failure — the **parent** reconciles and prunes them when children
merge up (it reruns `wiki update` and `wiki lint` during integration).
`wiki lint`'s stale-link warnings on leaf bodies are non-blocking, so
never stall an iteration chasing them.

Wikilinks also stay inside the wiki you are writing in. A `[[...]]` link
targets another page in the same wiki; anything outside it — source
files, configs, or the other knowledge base (project wiki vs. memory) —
is referenced in plain text or backticks, never linked. `wiki lint`
flags out-of-wiki wikilinks as stale.
