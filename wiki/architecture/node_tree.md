---
name: architecture/node_tree
desc: |
  The node tree model: dotted branch naming, the parent/child relationship
  derived from branch names, the passive root (user) node, and the central
  node registry.
created: 2026-07-21T04:47:26Z
updated: 2026-07-21T04:47:26Z
---

# architecture/node_tree

[[_index|..]]

***

Fractal organizes work as a tree of nodes. A node is an autonomous agent bound
to one git branch, one worktree checked out on that branch, and one data
directory (`.fractal/<branch>/` inside the worktree) holding its seed,
configuration, and private state. The tree has a single passive root — the user
node — and any number of agent nodes beneath it.

## Dotted branch naming

The branch name encodes the tree position. A child of the node on branch
`main.wiki` named `arch` lives on branch `main.wiki.arch`: every spawn appends
one dot-separated segment to the parent's branch. The dot is reserved as the
hierarchy separator, so a node *name* (one segment) may contain only ASCII
letters, digits, and underscores — no dots, dashes, or slashes. A single segment
is capped at 64 characters, and the composed branch (plus git's `.lock` ref
suffix) must fit the filesystem's 255-character ref bound, which is what
ultimately limits tree depth with long names.

Because position is encoded in the name, no parent pointer is stored anywhere:
the parent is always the branch minus its last dotted segment, and a branch with
no dot is the tree root. Slash-style branch names (`feat/x`) are rejected at
user-node init, since every per-branch artifact keys on the branch as a single
path component.

## Parent and child

Children are created with `fractal node init`, which forks the parent's branch
into a new worktree (see [[architecture/worktrees]]) and registers the child in
the central database. The parent relationship is structural — resolved by
dotted-branch derivation whenever it is needed — and each node reaches its
ancestors and descendants by walking branch names against the registry and the
worktree list.

Tree shape is bounded by per-node limits set at init and enforced at spawn time:
`max_depth` (child nesting below the node), `max_children` (direct children),
and `max_descendants` (whole subtree). Spawn gates re-check live state under a
tree-wide lock so parallel spawns cannot overshoot a cap.

## The passive root (user) node

The root of every tree is a *user node*: the node anchored on the dotless branch
the operator ran `fractal init` from (at the repo root, or a sub-project
directory in a monorepo). It is marked by the `user` flag in its config — not by
a lifecycle status — and it never iterates: it has a lightweight data directory
holding only the central database and a radio presence, with no steps, skills,
or scripts seeded.

The user node differs from agent nodes in what is legal on it. It cannot be
started, merged, or deleted, and it accepts only baseline (`--init`) commits —
its worktree is the operator's own working tree, so ordinary iteration commits
are refused. In exchange it anchors tree-wide state: its data directory hosts
the central database (see [[architecture/database]]), and a tree-wide pause
latches a marker file beside that database so even top-level spawns and starts
refuse until the tree-wide resume. Agent nodes, by contrast, carry the full
lifecycle status set (`active`, `paused`, `completed`, `exited`, and the rest)
in their `.status` file and registry row.

## The node registry

The `nodes` table in the central database is the tree's registry: one row per
node carrying its branch, display title, lifecycle status, and its cost and
tree-shape caps. The registry is flat — subtree queries match on the dotted
branch prefix — and it is the source of truth for `fractal node list` and for
spawn-limit accounting.

Registry rows and history rows have different lifetimes. Deleting a node removes
its registry row and subscriptions, while its history rows — runs, iterations,
steps, events, messages — persist in the central database (see
[[architecture/database]]). For a worktree removed out of band, the registry
offers `fractal node reconcile` (record orphaned descendants as events, keep the
rows) and a deregister path that clears an orphan's subtree rows and prunes its
branches. The rationale for the branch-encoded hierarchy and the passive root
lives in the [[design/_index|design]] branch.
