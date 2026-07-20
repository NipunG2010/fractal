## Reserve Mode

You have exceeded a cost cap, or you are very close to one. Wind down this
iteration as cheaply as possible instead of starting new work. These
instructions override the current step:

1. **Finish in-progress work minimally** -- bring whatever is open to a safe,
   committed state; do not begin anything new.
2. **Update memory** with what was accomplished and what remains.
3. **Settle your children** -- for each settled child, either merge its finished
   work now (`fractal node merge <branch>`, one child at a time) or hand it off
   by naming the branch and its merge-readiness in memory and in the parent
   report, so stranded descendants are never silently orphaned when this run
   ends.
4. **Report to parent** via radio:
   ```bash
   fractal radio send "<summary>" --parent --subject="<subject>" --priority=<0-10>
   ```

The loop decides at this iteration's boundary whether the run ends or continues
-- do **not** run `fractal node finish` yourself, and do not defer wind-down
work past this iteration: it may never run. Budget semantics live in the
`fractal` skill's Cost section.
