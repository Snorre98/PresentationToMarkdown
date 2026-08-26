# 0002. Expose AI capabilities as flags mapped onto env vars

- Status: Accepted
- Date: 2026-08-26

## Context

The AI passes — vision transcription, the classifier gate, markdown
restructure, and the RAG summary — are configured exclusively through
environment variables (`VISION_ENABLED`, `VISION_CLASSIFY_ENABLED`,
`FORMAT_ENABLED`, `SUMMARY_ENABLED`, plus `*_BASE_URL`/`*_MODEL`/`EMBED_*`
overrides). Enabling them from the shell required long, error-prone
`VAR=1 VAR=1 ...` chains.

These variables are read **at import time** as module-level constants
(`converter/vision.py`, `converter/format.py`, `converter/summary.py`,
`converter/classify.py`). Any wrapper must therefore set the environment
*before* the first import of `converter` (or `gui`, which imports `converter`).

## Decision

Provide simple boolean flags that map onto the existing env vars, with a single
source of truth in `cli_common.AI_FLAGS`:

| Flag | Env var set |
| --- | --- |
| `--vision` | `VISION_ENABLED=1` |
| `--classify` | `VISION_CLASSIFY_ENABLED=1` (implies `--vision`) |
| `--format` | `FORMAT_ENABLED=1` |
| `--summary` | `SUMMARY_ENABLED=1` |
| `--all` | all of the above |
| `--env KEY=VALUE` | arbitrary passthrough |

- `--classify` implies `--vision` because the classifier gate only has an effect
  when vision transcription is enabled.
- `--env KEY=VALUE` (repeatable) is a passthrough for anything the flags do not
  cover (model ids, base URLs, the log DB, tuning thresholds), so advanced users
  retain full control without one dedicated flag per variable.

Because the converter reads config at import time, both entry points apply the
mapping via `apply_ai_env()` (which calls `os.environ.update`) and only then
import `converter`/`gui` **lazily inside `main()`**. `cli_common` itself never
imports `converter`, so importing it cannot accidentally lock in defaults.

## Consequences

- The flags are a thin ergonomic layer over config the converter already owns;
  no new configuration mechanism is introduced.
- The "set env before import" ordering is now a hard requirement, encoded in
  `cli_common`'s docstring and tested.
- `--env` is the escape hatch; new env vars are automatically reachable without
  code changes.

## Alternatives considered

- **Dedicated flags for every env var** (`--vision-model`, `--embed-base-url`,
  …) — explodes the flag surface and needs updating with every new var;
  rejected in favour of `--env` passthrough.
- **Changing the converter to read config lazily** (functions instead of
  module constants) — a broad refactor touching every module; rejected as out of
  scope. The lazy-import pattern in the entry points achieves the same effect
  with far less churn.
