# RUN-EC2.md - the Saturday flow, EC2 path (primary)

This is the repeatable-rig path: the benchmark client runs on a t3.micro in
us-east-1 instead of the laptop. Six make targets, one command up, one command
down. The laptop-local path in `RUN.md` still works and is still the right
place for the zero-spend dry run.

## Why in-region (the mental model first)

Running the client from a laptop puts the home ISP inside every TTFT
measurement - the last mile becomes an uncontrolled variable that dwarfs the
provider-side differences the benchmark exists to measure. Moving the client
onto an EC2 instance in the same region removes the last mile:

```
laptop path:    laptop --(home ISP, variable)--> provider endpoint
                        ^^^^^^^^^^^^^^^^^^^^^
                        inside every TTFT number

EC2 path:       laptop --(make targets, control plane only)--> AWS
                EC2 t3.micro (us-east-1) --(AWS network)--> provider endpoint
                results --> S3 --> laptop (make pull, analysis stays local)
```

The rig is: Terraform'd EC2 runner (no SSH, no keys, SSM-only), an S3 bucket
for code and results, and an SSM SecureString carrying the Anthropic key
out-of-band. The instance never auto-runs anything; every run is an explicit
SSM command you dispatch and watch.

**Honesty note for the writeup:** this makes the *client* symmetric and
in-region. The two doors' *server-side* paths remain different by nature: the
Bedrock endpoint is in-region by construction, while the first-party API's
edge is wherever Anthropic runs it. The comparison is now "same client, same
vantage point, two doors" - not "two identical network paths." Name that in
any writeup; don't hide it.

## Prerequisites

- Terraform >= 1.5, AWS CLI authenticated to the right account
  (`aws sts get-caller-identity` - eyeball it), `op` signed in, `zip`, `uv`.
- **Bedrock model access enabled** for the Anthropic models in this
  account/region (Bedrock console > Model access - the one-time use-case
  form). Without it the Bedrock cells die on AccessDeniedException; see the
  RUN.md troubleshooting table.
- `pricing.json` verified against current price pages before quoting any
  cost number (RUN.md step 3b). The rig runs fine either way; placeholder
  prices make costs report n/a.

## Cost

| Item | Rate | A full Saturday |
|---|---|---|
| t3.micro, us-east-1 on-demand | about $0.01/hr | a few cents |
| S3 (one code zip, a few MB of results) | pennies | pennies |
| EBS 8 GB gp3 root | about $0.64/mo, prorated | about a cent |
| The API calls themselves | see README estimate | roughly $5-6 |

The infrastructure costs less than the API calls. The discipline that matters
is not the hourly rate, it is teardown: **destroy the same day** (see below).

## The flow

```bash
cd serving-economics-bench

make key      # 1. Anthropic key: 1Password -> SSM SecureString (out-of-band)
make deploy   # 2. terraform apply + zip code to S3
make smoke    # 3. one cheap Anthropic cell on the runner (about $0.03)
make run      # 4. full matrix, both providers (30-60 min; polls every 30s)
make pull     # 5. sync latest results to ./results-ec2/<ts>/ + analyze locally
make destroy  # 6. terraform destroy + delete the key parameter
```

### Step notes and eyeballs

**make key** - writes the key from 1Password straight into an SSM
SecureString. Adjust `OP_ANTHROPIC_KEY_PATH` at the top of the Makefile to
your actual item path first (`op item list | grep -i anthropic`). The key
never lands in a file, a Terraform variable, state, or user_data; the runner
reads it into process env at run time only.

**make deploy** - `terraform apply` (approve the plan), then zips `bench.py`,
`analyze.py`, `pricing.json`, `run_remote.sh`, `push_results.sh`, `eval/` and
uploads to `s3://<bucket>/code/code.zip`. Rerun after any code edit -
deploy is idempotent and just refreshes the zip. Eyeball: the plan creates
roughly a dozen resources, all tagged `Project = serving-economics-bench`.

**make smoke** - dispatches one cell (`anthropic:haiku:short`) via SSM.
Eyeball: status walks Pending -> InProgress -> Success, and the stdout tail
shows 15 requests and an upload line `s3://.../results/<ts>/`. If it sits in
Pending for minutes, the instance may still be booting or registering with
SSM - give it two or three minutes after deploy.

**make run** - full matrix. The poll loop prints status every 30s; the SSM
execution timeout is 3 hours. bench.py is resumable, but note each dispatch
starts from a fresh results file on the runner - a smoke row and a full run
land in *separate* `results/<ts>/` prefixes, by design (no mixing).

**make pull** - syncs the newest `results/<ts>/` prefix into
`./results-ec2/<ts>/` and runs `analyze.py` against it. Your repo-root
`results.jsonl` / `results.md` / `chart.png` are untouched. The prefix also
contains `run-metadata.json`: instance type, region, AZ, UTC timestamp, and a
sha256 over the shipped code - quote the metadata, not your memory.

**make destroy** - tears down every Terraform resource (the bucket has
`force_destroy`, so results in S3 die with it - pull first) and deletes the
key parameter. **Teardown discipline: run this the same day.** The rig is
one `make deploy` away from existing again; there is nothing to keep warm,
and an idle instance plus a live key parameter is pure downside.

## Troubleshooting deltas vs RUN.md

| Symptom | Cause | Fix |
|---|---|---|
| smoke/run stuck in Pending | Instance still booting or SSM agent not registered yet | Wait 2-3 min after deploy; check EC2 console instance status |
| `InvocationDoesNotExist` forever | Wrong region between Makefile REGION and terraform var | Keep `REGION` and `-var region` identical (the Makefile passes them together) |
| run_remote.sh fails on `ParameterNotFound` | `make key` not run (or run in another region) | `make key` with the same REGION |
| Bedrock cells AccessDenied | Model access not enabled in this account/region | Bedrock console > Model access; RUN.md troubleshooting |
| Results missing after destroy | Teardown deletes the bucket (`force_destroy`) | Always `make pull` before `make destroy` |

---

## Enablement reality - as actually run (July 2026)

The troubleshooting table above references the Bedrock console "Model access" page. **That page is retired.** What the first real run actually encountered, in order:

1. **Serverless models now enable on first invocation** - there is no access page to click. For Anthropic models, first-time users additionally submit a one-time use-case form.
2. **Anthropic Bedrock models are AWS Marketplace-listed.** The first enablement requires the calling principal to hold `aws-marketplace:ViewSubscriptions` and `aws-marketplace:Subscribe` (the managed policy `AWSMarketplaceManageSubscriptions` covers it). Without them: AccessDeniedException naming those actions.
3. **Enablement is per model** - clearing Haiku does not clear Sonnet. Each model needs one first invoke from a subscribe-capable principal (console playground or a one-line `aws bedrock-runtime converse`).
4. **Least-privilege tension, by design:** this rig's runner role deliberately has no Marketplace permissions, so it cannot self-subscribe. A broader-privileged human walks through each model's door once; the locked-down workload uses it thereafter. Plan for this on any fresh account.
5. **Verify model availability before planning a matrix:** newest flagship tiers can be sales-managed on Bedrock for some account types. Confirm every model in the plan actually serves in the target account and region before treating a like-for-like comparison as measurable.
