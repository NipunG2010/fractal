---
name: features/agents/models_and_effort
desc: |
  Where a spawn's model and reasoning-effort overrides come from, how each
  provider backend spells them on its command line, and how a backend
  resolves the model its own configuration defaults to.
created: 2026-07-21T05:04:16Z
updated: 2026-07-21T05:04:16Z
---

# features/agents/models_and_effort

[[features/agents/_index|..]]

***

## Override sources

A node's agent identity and its overrides are configuration: the config keys
`agent` (the command, extra words spliced into every invocation), `provider`
(the route, see [[features/agents/providers]]), `model`, and `effort`. Every
invocation builder also accepts per-call `model` and `effort` overrides, so a
single spawn can deviate from the node's configured defaults.

Effort is passed through to the provider's own effort flag *unvalidated* —
fractal does not maintain a level vocabulary; an unknown level surfaces as the
agent's own error. A hard USD budget is a separate per-invocation knob accepted
only by backends that declare budget enforcement; asking a non-enforcing backend
for one is an error.

## Per-backend spelling

Each backend translates the same two overrides into its own CLI dialect:

| backend    | model flag | effort flag                         |
| ---------- | ---------- | ----------------------------------- |
| `claude`   | `--model`  | `--effort`                          |
| `codex`    | `-m`       | `-c model_reasoning_effort="<lvl>"` |
| `grok`     | model flag | `--reasoning-effort`                |
| `opencode` | model flag | `--variant` (a model variant)       |
| `omp`      | `--model`  | `--thinking` (a thinking level)     |

For claude the effort flag outranks the settings-file effort level; node
settings (permissions, model, environment) ride a CLI flag rather than a file
merge. On the openrouter route a model-less claude invocation pins an explicit
model slug, because the process environment beats the settings file and the
route must not trust latest-model aliases.

## The configured-model fallback

When a spawn names no model, the model that actually served is still
recoverable: each stream parser starts from the backend's *configured model* and
prefers the model the stream itself reports, so the session record stamps the
real served model even for defaulted spawns. Each backend resolves its
configured model from its own vendor config, best-effort — an unreadable or
malformed file simply names no model:

- claude walks its settings chain (the node's agent dir, then the user's
  `~/.claude/settings.json`); the first file naming a model wins.
- codex reads the top-level model from its `config.toml`.
- omp reads its `config.yml` default once a YAML parser is available.

## Preflight

Model acceptance is probed before a run commits to it. codex in particular runs
one bounded preflight probe when an explicit model is set, because some accounts
reject models outside their entitlement (pricing knowledge proves a model
priceable, not that the account accepts it) — a defaulted model skips the probe.
On the openrouter route the preflight instead fails fast when the API key is
missing, and probe failures name the route-specific causes. The base preflight
also validates that the bound route is one the backend supports.
