---
name: radio
description: How to use radio well — the inter-node live messaging system.
---

# Radio

Radio is the live coordination path between nodes. Channels,
subscriptions, and message routing are described in the `fractal` skill.
This doc is the discipline for messaging well.

Two ways to read: `fractal radio messages` shows your `inbox` by default
(pass `--channel=private`/`outbox`/`public` for your other channels);
`fractal radio feed` fans out across your subscriptions to read other
nodes' channels. Review your outbound mail with `fractal radio sent`
(each row names its recipient). A bare `fractal radio send` (no
`--node`/`--parent`/`--channel`) writes to your own `private` channel --
to report upward, post to your outbox (`--channel=outbox`); use
`--parent` for a direct ask.

Run `fractal radio --help` and `fractal radio <command> --help` for the
CLI.

## Sync Mode

If sync is enabled (the default), it runs before every step and handles
routine radio checks — reading inbox and feed, responding, following
parent directives, and reporting outward. The conventions below guide
how you compose and prioritize messages within that pass (and any ad-hoc
radio use during other steps).

## Conventions

- **Report upward via outbox.** Write to your own outbox to report
  status, findings, or blockers — your parent is auto-subscribed and
  sees it in their feed. To reach a specific node directly, send to
  their inbox (`--node=<branch>`).
- **Radio is for coordination, memory is for knowledge.** Use radio for
  live coordination (requests, status, questions). Write lasting
  knowledge to memory, not messages -- radio is a stream to act on, not
  a knowledge base to mine.
- **Feed marks messages read.** Calling `feed` writes read receipts; the
  same messages won't appear on the next call. Use `--all` to re-read.
- **Replies are threaded, not in feed.** Only root-level messages appear
  in `feed`. To see replies, use `fractal radio thread <uuid>` (it shows
  the whole tree -- root and every reply -- not just unread).
- **Replies inherit the parent's subject.** `fractal radio reply`
  carries the parent's subject forward as `Re: ...` (and its priority)
  automatically -- do not pass `--subject` (it rejects one); `send` is
  the command that requires a subject.
- **Sending into another node's read-only channel is fire-and-forget.**
  A send or reply into a node's `inbox` (or any read-only channel you
  don't own) lands in *their* mailbox, and `read`/`thread` on a
  read-only channel are owner-only -- so you cannot read it back
  afterward. `fractal radio sent` lists what you sent; for a durable
  record keep your own copy (your `outbox`, `private`, or memory).
- **Reach the user (root node).** The user is a passive mailbox with no
  loop, so a sleeping operator sees messages only on wake. If the user
  is your parent, post to your outbox (they are subscribed); otherwise
  send to their inbox (`--node=<root-branch>`). Post and continue —
  never block on a reply; if you truly need an answer to proceed, make a
  reversible call and note it.
- **Radio reaches one hop.** Your feed spans only your parent and your
  direct children — never grandchildren or deeper, and there is no
  tree-wide view. Information crosses more than one level by relaying
  hop-by-hop: each tier reports to its own parent's outbox, so a finding
  walks up one level per iteration. An operator wanting a whole-subtree
  picture watches its direct children's outboxes and lets the tree
  funnel the rest upward.
- **Reaching siblings.** Siblings are not subscribed to you. To reach a
  peer, route through the shared parent: post peer-relevant findings to
  your outbox (the parent relays), and watch the parent's outbox in your
  feed for cohort directives.
