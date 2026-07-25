# CLAIMS.md - calibrated claims

The rule: every sentence below the MAY line is defensible under a hostile
drill-down after one completed run. Everything below the MAY NOT line is
inflation, and one inflated claim poisons the credible ones.

**Precondition:** these claims are licensed only after a real full run
(`--provider both`, no mock rows) and, for any Bedrock cost number, only after
`pricing.json` is filled with real Bedrock prices.

---

## What MAY be claimed

The core sentence, verbatim-safe:

> "I ran a provider-comparative serving-economics benchmark - TTFT,
> inter-token latency, and cost per completed task across the three Claude
> tiers on Bedrock and first-party, with and without prompt caching; the
> interesting part was [the delta you actually measured]."

Fill the bracket only from `results.md`, never from memory.

Supporting claims, each true by construction:

- "Cost per completed task, not per token - the harness validates every
  response against a known-good answer key, and failed extractions retry,
  so quality failures show up as cost."
- "Same requests, same prompts, byte-identical cacheable prefix on both
  providers; token counts taken from the provider's own usage reporting, not
  estimated client-side."
- "I measured the prompt-caching effect where it can actually exist - the
  short-context cells sit below both providers' minimum cacheable prefix, so
  the cache axis only runs on the long context. Knowing where caching
  silently no-ops is part of the result."
- "One deliberate concurrency probe at 4 on the Sonnet tier - enough to show
  the effect without paying for a load test."
- "Small N - 15 requests per cell. It's a method demonstration; the p50s are
  meaningful, the p99s are indicative."
- "I kept max_tokens tight because Bedrock reserves input plus max_tokens
  against the TPM quota at request start - sizing that wrong is a real
  production footgun."
- "I stood up a Terraform'd, repeatable eval rig - in-region EC2 client,
  SSM-only access (no SSH, no keys), the API key delivered out-of-band as an
  SSM SecureString, results pushed to S3 with run metadata, one command up
  and one command down. Moving the client in-region took my home ISP out of
  every TTFT number."

## What may NOT be claimed

- **Not fleet scale.** No claim about production traffic, sustained load,
  autoscaling, or capacity planning. 300 requests is a probe.
- **Not load testing.** One concurrency-4 cell is not a saturation curve.
- **Not Neuron / Trainium / Inferentia / accelerator work.** This never goes
  below the hosted API. Do not let "serving layer" drift into implying
  hardware-level measurement.
- **Not production serving experience.** This is a weekend benchmark the
  author designed and ran, not a system operated at scale. Say "I ran",
  never "we serve".
- **Not a model-quality benchmark.** The task is deliberately easy; pass
  rates say nothing about model capability rankings.
- **No Bedrock cost numbers while pricing.json holds placeholders.** "I have
  the token volumes; I priced the first-party side; Bedrock pricing gets
  filled from the console" is the honest sentence until then.
- **No generalized platform verdicts.** "Bedrock is slower/cheaper than
  first-party" is not licensed; "on this workload, this week, in us-east-1, I
  measured X" is.
- **Not "a model evaluation platform."** It is one task, one rig, the first
  rung. "Eval rig" and "first rung of an evaluation setup" are the licensed
  phrases; "platform" implies multi-task coverage, versioning, dashboards,
  and users it does not have.
- **The rig does not equalize the providers' networks.** The client is
  symmetric and in-region; the server-side paths remain different by nature
  (Bedrock terminates in-region, the first-party edge is wherever Anthropic
  runs it). Claim a controlled client vantage point, not a controlled
  network experiment.
