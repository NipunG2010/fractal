---
name: memory
description: How to maintain node memory well — the node's persistent knowledge store.
---

# Memory

Memory (`$MEMORY_DIR`) is the node's durable brain. Read it when you
orient and fold findings back before each iteration ends. Sync may also
write to memory when crucial information arrives via radio. See the
`wiki` skill for how memory relates to the shared project wiki; this doc
is the discipline for keeping memory useful.

Run `wiki --help` and `wiki <command> --help` for the CLI.

## Conventions

- **Write knowledge, not history.** Never reference iteration numbers,
  timestamps, or chronological markers — a reader shouldn't be able to
  tell how many iterations have run.
- **Organize by topic, not time.** Update the existing page for a topic;
  don't append a new entry.
- **No append-only logs.** If you're adding dated entries, stop —
  replace outdated content with current understanding.
- **Todo lists are living state.** Keep your private working checklist
  here as current open items, pruned as they complete — never a
  done-log. A todo list other nodes should see and track belongs in the
  project wiki instead.
- **Keep indexes lean.** Keep each `_index.md` under ~100 lines below
  the `***`; factor overflow into child pages.
- **Wikilinks stay within one wiki.** Reference anything outside this
  wiki — the project wiki, source files, configs — in plain text or
  backticks, never as a wikilink. `wiki lint` flags out-of-wiki
  wikilinks as stale.
