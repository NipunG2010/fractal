---
name: wiki
description: The wiki CLI and the node's two knowledge bases (project wiki + memory).
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

Run `wiki --help` and `wiki <command> --help` for the CLI (init, update,
lint, map, search, read). Always pass `--path` (`$WIKI_DIR` or
`$MEMORY_DIR`) — the wiki CLI does not walk up to find a wiki from the
node directory. Run `wiki update --path=<dir>` after adding, moving, or
deleting pages; `wiki lint --path=<dir>` validates structure.

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
