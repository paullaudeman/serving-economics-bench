# RUN.md - Saturday-morning checklist

Total time: one afternoon. Total spend: a few dollars (see the cost estimate
in README.md). Every step says what to run and what to eyeball before moving
on. `bench.py` is resumable - rerunning skips completed requests, so an
interruption never wastes spend.

Prerequisite: `uv` installed (it is). No credentials needed until step 3.

---

## Step 0 - Sanity: estimate the spend

```bash
cd serving-economics-bench
uv run bench.py --estimate
```

**Eyeball:** Anthropic subtotal about $2.6, Bedrock subtotal about 10%
above it (`pricing.json` ships filled, with source and date per block). If a
subtotal prints UNKNOWN, that block still holds placeholder values - see
step 3b. If the Anthropic subtotal looks wildly different, stop -
pricing.json or the matrix changed.

## Step 1 - Dry run: validate the whole pipeline, zero spend

```bash
uv run bench.py --dry-run
uv run analyze.py
open results.md chart.png
```

**Eyeball:**
- The run prints `20 cells x 15 requests = 300 requests [DRY RUN - zero spend]`
  and finishes in well under a minute.
- `results.jsonl` exists and has 300 lines (`wc -l results.jsonl`).
- `results.md` has 20 cell rows, a DRY RUN banner at the top, some retries > 0
  (the mock injects invalid JSON on every 5th request to prove the retry path
  works), and pass 15/15 everywhere.
- `chart.png` renders: blue = anthropic, orange = bedrock, every point
  direct-labeled, title says `[DRY RUN - MOCK DATA]`.

Then clear the mock data (bench.py refuses to mix mock and real rows):

```bash
rm results.jsonl
```

## Step 2 - Credentials (nothing written to disk)

```bash
export ANTHROPIC_API_KEY="$(op read 'op://Personal/Anthropic API Key/credential')"
# Bedrock: standard AWS auth - your profile / SSO session
export AWS_DEFAULT_REGION=us-east-1
aws sts get-caller-identity     # eyeball: the account you expect
```

Adjust the `op read` path to wherever the Anthropic key actually lives in
your 1Password (`op item list | grep -i anthropic` to find it). The EC2 path
reads the same item name from the Makefile; personal overrides belong in
`local.mk` (gitignored), not in tracked files.

## Step 3 - Anthropic smoke test: one cheap cell (about $0.03)

```bash
uv run bench.py --provider anthropic --cells "anthropic:haiku:short"
```

**Eyeball:** 15 requests complete, no exceptions. Then peek at one row:

```bash
tail -1 results.jsonl | python3 -m json.tool
```

- `ttft_s` is a plausible sub-second number, `input_tokens` roughly 500-700,
  `output_tokens` roughly 100-300, `passed: true` on most rows, `mock` absent/false.
- If `passed: false` dominates, read `fail_reason` before spending more.

## Step 3b - Verify pricing before quoting any cost number

`pricing.json` ships filled, with the source and pull date recorded per
block. Prices move: open https://aws.amazon.com/bedrock/pricing/ and the
Anthropic pricing page, confirm the per-MTok values (input, output, cache
write, cache read) still match, and bump the dates. If a block ever holds
placeholder (`-1.0`) values, `analyze.py` reports that provider's costs as
`n/a` and `--estimate` refuses to guess - fill the real prices to proceed.

## Step 4 - Bedrock smoke test: one cheap cell

```bash
uv run bench.py --provider bedrock --cells "bedrock:haiku:short"
```

**Eyeball:** same checks as step 3. First-ever call to an Anthropic model in
the account may take up to about 15 minutes to activate the marketplace
subscription - see troubleshooting.

## Step 5 - Full run

```bash
uv run bench.py --provider both
```

About 300 requests, mostly sequential; expect roughly 30-60 minutes wall time.
Interruptions are fine - rerun the same command, completed requests are
skipped. **Eyeball while it runs:** cache cells print request 0 first
(the cache write) before the rest (the reads).

## Step 6 - Analyze

```bash
uv run analyze.py
open results.md chart.png
```

**Eyeball:**
- No DRY RUN banner.
- Cache-on cells: `cache_read_tokens` large, TTFT p50 lower than the matching
  no-cache cell. If cache reads are near 0, see troubleshooting.
- The concurrency-4 cells sit differently from their c1 twins - that delta is
  the concurrency story.
- Bedrock `$/completed task` shows real numbers only if step 3b happened.

Then read `CLAIMS.md` before saying anything about the results out loud.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Bedrock `AccessDeniedException` on first call | Model access not enabled for Anthropic models in this account/region (one-time use form) | Bedrock console > **Model access** > request/enable access for the Anthropic models, submit the use-case form. Also verify your IAM identity has `bedrock:InvokeModelWithResponseStream` (ConverseStream needs it, not just `bedrock:InvokeModel`). |
| `AccessDeniedException` persisting a couple of minutes after enabling | First invocation kicks off a background marketplace subscription (up to about 15 min) | Wait, retry. bench.py resumes automatically. |
| `ValidationException`: model/inference profile not found | The `us.anthropic.*` inference-profile ID does not match your account's catalog, or the region has no such profile | Bedrock console > Model catalog > open the model card > copy its exact inference profile ID into `MODELS` at the top of `bench.py`. |
| `ThrottlingException` storms, especially in the c4 cell | TPM quota (input + reserved max_tokens counts at request start; output burns at 5x) | bench.py already backs off and retries. If persistent: Service Quotas console > Bedrock > request a TPM increase, or run the c4 cell last. |
| Cross-region profile works in console, fails from CLI | IAM/SCP only allows `bedrock:InvokeModel*` in the source region; geo profiles route to every destination region in the geography | Extend the IAM policy to all destination regions listed on the model card. |
| Anthropic 429 `rate_limit_error` | Org TPM/RPM tier limit | bench.py backs off automatically; if constant, run providers on separate days or lower concurrency. |
| Cache-on cell shows `cache_read_tokens: 0` | Requests spaced beyond the 5-minute TTL (e.g. heavy throttling stretched the cell), or the prefix changed between requests | Rerun just that cell: `uv run bench.py --provider X --cells "X:model:long:cache"` after deleting that cell's rows from results.jsonl. The system prefix is byte-identical by construction, so TTL expiry is the usual cause. |
| `results.jsonl contains DRY-RUN (mock) rows` | Step 1 leftovers | `rm results.jsonl` (or archive it first). |
| Bedrock costs show `n/a` in results.md | pricing.json still has PLACEHOLDER values | Step 3b. |
