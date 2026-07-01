## Detached Mode

Each step is a separate session with no shared context. Rebuild from
your **radio private channel**
(`fractal radio messages --channel=private --all`), **memory**
(`$MEMORY_DIR`), and recent **saved messages**
(`fractal radio messages --saved`). Before finishing, write a concise
handoff for the next step:

```bash
fractal radio send <context> --channel=private --subject=<subject> --priority=<priority>
```
