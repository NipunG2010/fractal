## Reserve Mode

You have either exceeded your budget, or you are very close to exceeding
it. You will **not** be stopped mid-step -- every further step
decrements your parent's budget -- so wind down this iteration as
cheaply as possible instead of starting new work. These instructions
override the current step:

1. **Finish in-progress work minimally** -- bring whatever is open to a
   safe, committed state; do not begin anything new.
2. **Update memory** with what was accomplished and what remains.
3. **Report to parent** via radio:
   ```bash
   fractal radio send <summary> --parent --subject=<subject> --priority=<priority>
   ```

The loop decides what happens next: if your **total** budget is
exhausted (you've entered the reserve), the run ends at this iteration's
boundary -- wind down within the steps remaining in this iteration, as
there is no extra wind-down iteration; if only **this iteration's** cap
was hit it continues with a fresh budget next iteration. Do **not** run
`fractal node finish` yourself.
