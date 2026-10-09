# Models frozen: 9. Oct 2026

This benchmark is a **dated measurement**, so its models are pinned on purpose. Do not bump them to
newer releases. A newer model would make the numbers incomparable with the results already published.

## What is pinned

| Tier | Anthropic API | Amazon Bedrock (inference profile) |
|---|---|---|
| Haiku | `claude-haiku-4-5` | `us.anthropic.claude-haiku-4-5-20251001-v1:0` |
| Sonnet | `claude-sonnet-4-6` | `us.anthropic.claude-sonnet-4-6` |
| Opus | `claude-opus-4-8` | `us.anthropic.claude-opus-4-8` |

Source of truth: `MODELS` in `bench.py`. Prices: `pricing.json`, each half with its own date.

## The point in time

- **Measured:** the run in `results.jsonl`, 300 rows, recorded 25. Jul 2026 (Haiku 90, Sonnet 120, Opus 90).
- **Frozen:** 9. Oct 2026. By then newer models existed (Sonnet 5.5, Opus 5.5, Haiku 5.5). They are deliberately not used.
- **Why these three:** they all run thinking-off when the field is omitted, and none takes sampling params,
  so every cell is identical apart from the axis being measured. See the comment above `MODELS` in `bench.py`.

## Rules

- **A model sweep skips this repo.** Any tool or person updating model IDs across `~/dev` should leave these alone.
- **`drift_watch.py` will report the roster as superseded.** That is expected while frozen. It is a reason to
  decide on a new run, never a reason to edit the IDs in place.
- **To measure newer models, start a new dated run.** Keep this one: new IDs, new results file, a new freeze date
  in this file, and the old run kept for comparison. Never overwrite `results.jsonl` with mixed models.
