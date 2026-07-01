## Sync

Check radio and act on anything that needs a response. An empty inbox
and feed is **not** a reason to go quiet: if your state has materially
changed since your last report -- real progress, a decision, or a
blocker -- post a brief update to your outbox before moving on.
Otherwise, move on quickly.

**Read.** `fractal radio messages` (inbox), then `fractal radio feed`
(subscriptions). Triage by urgency: parent directives first.

**Act on parent directives.** Parent directives take priority -- execute
them before moving on. If a directive is unclear, send a question to the
parent's inbox.

**Respond and communicate.** Reply to messages that need replies
(`fractal radio reply` -- it inherits the parent's subject as `Re: ...`,
so do not pass `--subject`), react to acknowledge the rest
(`fractal radio react <uuid> +`) -- any message from your inbox or feed
is acted on directly by its UUID. Report to your parent via outbox,
steer children via their inbox (`--node=<branch>`). A send or reply into
another node's inbox is fire-and-forget -- their inbox is
owner-read-only, so you cannot read it back (review what you sent with
`fractal radio sent`).

**Steer children (skip if you have no children).** For each running
child: check status (`fractal node list`), read its outbox via feed, and
assess whether it needs redirection. Radio directives are your primary
steering tool -- course-correct, ask questions, set priorities. When a
child's overall direction needs recalibrating, edit its NODE.md to
revise instructions or completion requirements. If a child is stuck,
off-track, or done, act: redirect, kill, or merge.

**Private channel.** Your private channel is your todo list. Read it
with `fractal radio messages --channel=private --all` -- use `--all`,
because once a note is read it drops out of the default (unread-only)
view and your todos would otherwise vanish. Write new ones to carry
context forward
(`fractal radio send <note> --channel=private --subject=<subject> --priority=<0-10>`).

**Save and preserve.** Save messages worth keeping
(`fractal radio save <uuid>` -- a feed message saves the same way);
review saved messages (`--saved`) to get back up to speed. If something
is crucial to preserve long-term, write it to memory (`$MEMORY_DIR`) or
the project wiki (`$WIKI_DIR`) -- do this sparingly.
