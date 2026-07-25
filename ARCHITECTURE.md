# Architecture

This document is the visual companion to `README.md` (what and why),
`RUN-EC2.md` (the operational checklist), and `WALKTHROUGH.md` (the code
tour). All diagrams are Mermaid: they render natively on GitHub, diff like
text, and age better than binary images.

---

## 1. System architecture

The rig has one deliberate shape: the laptop is a control plane only. Code
travels to the runner through S3, the secret travels through SSM Parameter
Store, and measurements travel back through S3. Nothing dials into the
instance - it has no SSH, no key pairs, and a security group with zero
ingress rules. The two "doors" being measured are the only things the runner
talks to besides AWS control services.

```mermaid
flowchart TD
    subgraph Laptop["Laptop - control plane only"]
        OP["1Password: op read"]
        MK["make targets: key / deploy / smoke / run / pull / destroy"]
        AN["analyze.py: results.md + chart.png"]
    end

    subgraph AWS["AWS account - us-east-1"]
        SSMP["SSM Parameter Store: SecureString /serving-econ-bench/anthropic-key"]
        subgraph EC2["EC2 runner - t3.micro"]
            RUN["run_remote.sh + bench.py"]
        end
        S3["S3 bucket: code/code.zip + results/timestamp/"]
        BR["Amazon Bedrock: ConverseStream"]
    end

    ANTH["Anthropic first-party Messages API"]

    OP -->|"make key: key value in transit only"| SSMP
    MK -->|"make deploy: terraform apply + code zip"| S3
    MK -->|"make run: SSM send-command"| RUN
    SSMP -->|"read into process env at run time"| RUN
    S3 -->|"code.zip"| RUN
    RUN -->|"IAM role - no API key"| BR
    RUN -->|"runtime-env API key"| ANTH
    RUN -->|"results.jsonl + run-metadata.json"| S3
    S3 -->|"make pull"| AN
```

Properties worth noticing:

| Property | Where it is enforced |
|---|---|
| No inbound network path to the runner | Security group with zero ingress rules; SSM Session Manager is the only access channel |
| API key never on disk, never in Terraform state | `make key` pipes `op read` straight into an SSM SecureString; `run_remote.sh` reads it into process env only |
| Least-privilege runner | Instance role grants exactly: Bedrock invoke, one SSM parameter path read, one bucket |
| Instance metadata hardened | IMDSv2 required (`http_tokens = "required"`) |
| Every result set traceable | `run-metadata.json` records instance type, region, AZ, UTC timestamp, and a sha256 over the shipped code |

## 2. Run lifecycle

Six make targets, one afternoon, teardown the same day. The sequence below is
the whole operational surface - there is no dashboard, no daemon, and nothing
that runs unattended. Note the last step: `make destroy` deletes the key
parameter along with the infrastructure, so an idle rig never leaves a live
credential behind.

```mermaid
sequenceDiagram
    participant L as Laptop
    participant P as SSM Parameter Store
    participant T as Terraform
    participant S as S3 bucket
    participant R as EC2 runner
    participant D as Both doors

    L->>P: make key - op read piped to SecureString
    L->>T: make deploy - terraform apply
    T-->>L: instance_id, bucket_name
    L->>S: make deploy - upload code.zip
    L->>R: make smoke - SSM send-command, one cheap cell
    R->>S: download code.zip
    R->>P: read key into process env
    R->>D: 15 streamed requests
    R->>S: results/timestamp-1/
    L->>R: make run - SSM send-command, full matrix
    R->>D: about 300 streamed requests
    R->>S: results/timestamp-2/ + run-metadata.json
    L->>S: make pull - sync newest results locally
    L->>L: analyze.py - results.md + chart.png
    L->>T: make destroy - terraform destroy
    L->>P: make destroy - delete key parameter
```

## 3. The measurement matrix

The matrix is 20 cells: 2 providers x 3 model tiers x the context/cache
combinations below, plus one deliberate concurrency probe per provider. The
cache axis exists only for the long context because the short prompt sits
below both providers' minimum cacheable prefix - a short+cache cell would
silently no-op and report a fake "no caching effect" result. Every cell runs
N=15 requests, one per eval document.

```mermaid
flowchart LR
    subgraph Providers["2 providers"]
        A1["anthropic - Messages API"]
        B1["bedrock - ConverseStream"]
    end
    subgraph Tiers["3 model tiers"]
        H["haiku"]
        S["sonnet"]
    end
    subgraph Cells["cells per provider x tier"]
        C1["short context, no cache, c1"]
        C2["long context, no cache, c1"]
        C3["long context, cache ON, c1"]
    end
    PROBE["+1 probe per provider: sonnet, long, no cache, concurrency 4"]
    CAP["captured per request: TTFT, ITL p50/p99, wall clock, input/output/cache tokens as provider-reported, validation retries, throttle retries, pass/fail"]

    Providers --> Tiers --> Cells
    Cells --> CAP
    PROBE --> CAP
```

Totals: (2 tiers x 3 context/cache cells + 1 concurrency probe) x 2
providers = 20 cells, 300 requests. Derived per cell by `analyze.py`: output
tokens/sec, cost per completed task, goodput (completed tasks per minute),
pass rate.

## 4. Trust boundaries

Two credentials exist and they never cross: the Bedrock door authenticates
with the instance's IAM role (no key material anywhere), and the first-party
door authenticates with an API key that lives only in an SSM SecureString and
the runner's process environment. The bills land in different places for the
same reason - that split is itself part of what the benchmark demonstrates.

```mermaid
flowchart TD
    subgraph LT["Trust zone: laptop"]
        K1["1Password vault - Anthropic API key at rest"]
    end
    subgraph AWSZ["Trust zone: AWS account"]
        K2["SSM SecureString - key at rest, KMS-encrypted"]
        K3["Runner process env - key in memory only"]
        ROLE["EC2 instance role - temporary credentials, auto-rotated"]
        BILL1["Bedrock usage billed to the AWS account"]
    end
    subgraph ANTHZ["Trust zone: Anthropic account"]
        BILL2["First-party usage billed to the Anthropic account"]
    end

    K1 -->|"make key - transit only, never on disk"| K2
    K2 -->|"run time only"| K3
    K3 -->|"Authorization header"| BILL2
    ROLE -->|"SigV4 - no static secret exists"| BILL1
```

What is deliberately NOT in any tracked file: account IDs (the bucket name
derives one at apply time, inside your own state only), API keys, personal
1Password item names (those live in the gitignored `local.mk`), and
Terraform state (gitignored - state can carry resource attributes you do not
want published).
