# Code walkthrough

A guided reading of the codebase - design decisions and trade-offs, not
line-by-line syntax. Read this next to the diagrams in `ARCHITECTURE.md`.

## The 30-second overview

Three moving parts, one contract between them:

- **`bench.py`** runs a matrix of streamed requests against two providers and
  appends one JSON line per request to `results.jsonl`. It measures; it never
  prices.
- **`analyze.py`** reads `results.jsonl` plus `pricing.json` and writes
  `results.md` and `chart.png`. It prices; it never measures.
- **`terraform/` + `Makefile`** put the client on an in-region EC2 instance
  so the home ISP is not inside every latency number, and move code, secret,
  and results between laptop and runner without any of them meeting on disk.

The contract is the results row. Everything downstream (tables, charts,
claims) derives from provider-reported fields written at measurement time,
which is why the harness stays honest even when it is rerun months later.

---

## bench.py - the harness

### The matrix builder

`build_cells()` is deliberately explicit rather than a cartesian product.
Each provider gets: two tiers x (short/no-cache, long/no-cache,
long/cache), plus one concurrency-4 probe on Sonnet. Two decisions are
encoded right there in the loop:

- **The cache axis only exists for the long context.** The short prompt
  (about 500 tokens) is below the minimum cacheable prefix on both providers
  (1,024-4,096 tokens depending on model). A short+cache cell would run
  fine, cache nothing, and produce a plausible-looking "caching has no
  effect" row. The most dangerous benchmark result is the one that silently
  measures nothing; the matrix shape rules it out by construction.
- **One concurrency probe, not a concurrency axis.** Concurrency 4 on the
  mid tier shows the effect without tripling the bill or drifting into load
  testing the harness cannot claim.

`Cell` is a frozen dataclass whose `cell_id`
(`provider:model:context:cache:cN`) doubles as the resume key and the
`--cells` filter target - one string, three jobs.

### Two provider clients, one shape

`AnthropicRunner` and `BedrockRunner` both return the same `StreamResult`,
so the orchestration and scoring code cannot tell providers apart. The
differences that matter live inside them:

- **Bedrock uses Converse/ConverseStream only, never legacy InvokeModel.**
  Converse is the unified surface where caching, usage reporting, and
  streaming all behave consistently; InvokeModel is the per-model legacy
  path. On the Anthropic side, the Messages API streaming client is the
  equivalent single surface.
- **Caching is expressed differently but means the same thing.** Anthropic:
  `cache_control: {"type": "ephemeral"}` on the last static system block.
  Bedrock: a `{"cachePoint": {"type": "default"}}` block appended after the
  static system text. Both mark "everything up to here is a stable prefix."
- **Region is passed explicitly to boto3.** `AWS_REGION` is the
  lowest-priority fallback in boto3's resolution chain; relying on it is a
  classic source of "worked on my machine, ran in the wrong region" bugs.
- **No temperature, no thinking config, anywhere.** Some current Claude models reject
  sampling parameters with a 400, and the chosen models run
  thinking-off when the field is omitted. Omitting everything keeps every
  request byte-comparable across tiers and providers - a serving benchmark
  must not smuggle in a model-behavior confound.

### TTFT capture from stream events

Both runners start a monotonic clock before the request and record the first
*text delta* event as TTFT - not the HTTP response, not message-start
metadata, the first token a user would actually see. Every subsequent delta
timestamp goes into a list, and inter-token latencies are the pairwise
differences. This is the customer's-eye definition: what a real workload
experiences through the API, which is the only vantage point the client
controls.

Throttling is handled inside the runner with exponential backoff plus
jitter, and the throttle-retry count is recorded on the row. A throttled
request that eventually succeeds is data about the door's operational
behavior, not noise to hide.

### The retry and scoring loop

`run_one_request()` gives each request up to 3 attempts. Every attempt's
tokens - including failed ones - accumulate into the row's totals. This is
the load-bearing design decision of the whole project: **a failed extraction
is not discarded, it is priced.** `score_output()` is deterministic (valid
JSON, schema keys, exact jurisdiction and risk level, set-equal prohibited
phrases, claims recall at or above 0.5 against the answer key), so pass/fail
never involves a judge model or an opinion.

### Resumability

Every completed request is one appended JSON line, and on startup
`load_completed()` reads back `(cell_id, request_index)` pairs and skips
them. An interruption - a throttle storm, a laptop sleep, a Ctrl-C - never
wastes spend. One guard: if the file contains mock rows, a real run refuses
to start until it is deleted, so dry-run data can never contaminate real
data.

### The dry-run mock

`MockRunner` exercises the entire pipeline - matrix, streaming timings,
token accounting, cache read/write asymmetry, retry path - with zero spend.
It deterministically injects invalid JSON on every fifth request precisely
so the retry path is proven before money touches it, and every row it writes
is flagged `mock: true` so it can never be quoted as a measurement.

## analyze.py - the aggregation

Aggregation is per cell: pass rate, retry counts, TTFT p50, ITL p50/p99,
tokens/sec (output tokens over generation time, i.e. wall minus TTFT), and
the headline metric:

**Cost per completed task = cell cost / passed count.** Cell cost sums every
attempt's provider-reported tokens (input, output, cache read, cache write)
against `pricing.json`. Because retry tokens are in the numerator and only
*passing* tasks in the denominator, an unreliable model gets more expensive
exactly the way it does in production. Per-token pricing tells you what a
model costs; per-completed-task pricing tells you what your problem costs.

Two refusals are built in: if `pricing.json` still holds placeholder values
for a provider, its costs report as `n/a` rather than a fabricated number,
and if any row is mock, every output carries a DRY RUN banner.

The chart is a single-axis scatter (TTFT vs tokens/sec) with cost annotated
per point, fixed series colors per provider, and direct labels with a small
de-collision pass - readable in grayscale, no dual axes.

## terraform/main.tf - the rig, resource by resource

| Resource | The why |
|---|---|
| `aws_security_group.runner` | **Zero ingress rules on purpose.** Nothing dials in, ever - no SSH, no key pairs. All access is SSM over the instance's own outbound 443. The egress-only shape removes the entire inbound attack surface instead of narrowing it. |
| `aws_s3_bucket.results` + public access block | One bucket for code zip and results, all four public-access blocks on. `force_destroy = true` because this is a weekend rig: teardown must never strand on leftover objects. Corollary: `make pull` before `make destroy`, results die with the bucket. |
| `aws_iam_role.runner` + inline policy | Least privilege, three statements: Bedrock invoke (wildcarded across regions because the `us.*` geo inference profiles route to every destination region in the geography - a single-region grant breaks them), `ssm:GetParameter` on exactly the `/serving-econ-bench/*` path, and S3 on exactly the one bucket. |
| `AmazonSSMManagedInstanceCore` attachment | The managed channel that replaces SSH: Session Manager and send-command. |
| `aws_instance.runner` | t3.micro - the client is I/O-bound streaming, not compute-bound. `http_tokens = "required"` enforces IMDSv2. `associate_public_ip_address` is the egress path only (default VPC, no NAT); with zero ingress rules the public IP receives nothing. `user_data` installs `uv` and stops - nothing auto-runs, no secret ever appears in user_data. |
| `versions.tf` local state | Deliberately boring: one root module, local state, no remote backend. State is gitignored because it can carry resource attributes that do not belong in a public repo. |

## The Makefile - lifecycle and the secret path

Six targets: `key`, `deploy`, `smoke`, `run`, `pull`, `destroy`. Two things
are worth reading closely:

**The secret path.** The Anthropic key travels
1Password -> (`op read`, in-memory) -> SSM SecureString -> runner process
env. Four places it never exists: a file on the laptop, a Terraform
variable, Terraform state, EC2 user_data. `make destroy` deletes the
parameter along with the rig, so there is no idle credential to forget.
Personal 1Password item names stay out of the tracked Makefile: the default
is generic and `local.mk` (gitignored) carries the real override.

**The dispatch loop.** `smoke` and `run` share `_ssm-run`: build an SSM
send-command that downloads the code zip, extracts it, and runs
`run_remote.sh`; then poll the invocation every 30 seconds until a terminal
status and print the stdout/stderr tails. No SSH session to babysit, and the
command, its parameters, and its output are all in the SSM audit trail.

`run_remote.sh` (on the runner) reads the key parameter into env and invokes
the same `bench.py` you ran locally - the dry run on the laptop and the real
run on the rig exercise identical code. `push_results.sh` writes
`run-metadata.json` (instance type, region, AZ, UTC timestamp, and a sha256
over the shipped files) next to the results, so every result set is
traceable to the exact code and vantage point that produced it.

## Honest notes - where the design is deliberately small

- **N=15 per cell.** Enough for stable medians, not for tails; at N=15 the
  p99 is roughly the worst request. The results table says so.
- **One task.** Structured extraction with a known answer key. The method
  generalizes to any scorable task; the findings do not generalize past this
  one.
- **One concurrency probe.** Not a saturation curve, and it does not claim
  to be.
- **The client is symmetric; the networks are not.** In-region EC2 removes
  the last mile from the client side, but Bedrock terminates in-region by
  construction while the first-party edge is wherever Anthropic runs it.
  This is a controlled vantage point, not a controlled network experiment.
- **Results are snapshots.** Serving stacks and price lists move. The
  harness, the matrix shape, and the metadata discipline are the durable
  assets; any numbers are dated readings.
