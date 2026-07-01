---
requires_approval: false
---

## Execute

Execute the plan. If running low on time, finish the current sub-task
cleanly rather than starting a new one.

If your plan lists children to spawn and you have not yet spawned them,
do so now before any leaf work. Managing running children (checking
status, editing their NODE.md, sending directives, merging completed
work) is also execution.

Verify with `bash $NODE_DIR/scripts/test.sh` if configured (exit 0 or
no-op = proceed). Run `bash $NODE_DIR/scripts/lint.sh` as you go to
catch issues early and fix what you introduce; it is enforced at COMMIT.

The full memory update happens in REVIEW. But if you discover something
that would be lost if the session ended (a finding, blocker, or
convention), write it to memory now and run
`wiki update --path=$MEMORY_DIR` after so the index stays valid.

If you hit a blocker someone else owns, raise it on the radio now rather
than waiting for the next sync -- send it to your parent's inbox
(`fractal radio send "<note>" --parent --subject="<subject>" --priority=<0-10>`)
or a sibling's (swap `--parent` for `--node=<branch>`). `--subject` and
`--priority` are required. It is fire-and-forget, so do not pause
execution waiting on a reply.
