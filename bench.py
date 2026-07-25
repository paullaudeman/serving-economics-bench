#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "anthropic>=0.66",
#   "boto3>=1.35",
# ]
# ///
"""serving-economics-bench: provider-comparative inference benchmark harness.

Runs the SAME structured-extraction matrix against (a) Amazon Bedrock
ConverseStream and (b) the Anthropic first-party Messages API, and records
per-request serving metrics (TTFT, inter-token latency, wall clock, tokens as
reported by the provider, retries, pass/fail) to results.jsonl.

Design notes baked in from the source-of-truth skills:
  * Bedrock: Converse/ConverseStream only, never legacy InvokeModel.
  * Bedrock: region passed explicitly (AWS_DEFAULT_REGION or --region);
    AWS_REGION is the lowest-priority fallback in boto3's chain.
  * Bedrock: prompt caching via a {"cachePoint": {"type": "default"}} block at
    the end of the static system prefix; usage reports cacheReadInputTokens /
    cacheWriteInputTokens.
  * Bedrock: max_tokens is kept small (1024). At request start Bedrock reserves
    input_tokens + max_tokens 1:1 against the TPM quota, and bills actual
    output at 5x burndown - an oversized max_tokens throttles concurrency.
  * Anthropic: caching via cache_control {"type": "ephemeral"} on the last
    static system block; usage reports cache_creation_input_tokens /
    cache_read_input_tokens.
  * No temperature/top_p anywhere: claude-opus-4-8 rejects sampling params
    with a 400, and omitting them keeps every cell identical.
  * No `thinking` config anywhere: the chosen models run thinking-off
    when the field is omitted, which keeps the comparison apples-to-apples.
  * The short-context cells are BELOW the minimum cacheable prefix on both
    providers (1024-4096 tokens depending on model), where caching silently
    no-ops. The cache axis therefore exists only for the long context.

Usage (see RUN.md for the full checklist):
  uv run bench.py --dry-run                      # zero-spend pipeline check
  uv run bench.py --estimate                     # cost estimate from pricing.json
  uv run bench.py --provider anthropic --cells "anthropic:sonnet:short"   # smoke
  uv run bench.py --provider both                # full matrix
"""

from __future__ import annotations

import argparse
import concurrent.futures
import dataclasses
import json
import os
import pathlib
import random
import statistics
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent
EVAL_DOCS = ROOT / "eval" / "documents"
EVAL_EXPECTED = ROOT / "eval" / "expected"
RESULTS_PATH = ROOT / "results.jsonl"
PRICING_PATH = ROOT / "pricing.json"

N_PER_CELL = 15
MAX_TOKENS = 1024
MAX_VALIDATION_ATTEMPTS = 3   # 1 attempt + up to 2 retries on invalid output
MAX_THROTTLE_RETRIES = 5

# Model IDs. Anthropic IDs are exact alias strings (never append date
# suffixes). Bedrock IDs are US geo cross-region inference profiles - verify
# them against the model catalog in the Bedrock console for your account
# (Console > Bedrock > Model catalog > the model card lists its inference
# profile IDs). Sonnet 4.6 is used as the Sonnet tier instead of Sonnet 5
# deliberately: Sonnet 5 runs adaptive thinking by default (a confound for a
# serving benchmark) and carries introductory pricing; Sonnet 4.6 runs
# thinking-off when the field is omitted, like Haiku 4.5 and Opus 4.8.
MODELS = {
    "haiku": {
        "anthropic": "claude-haiku-4-5",
        "bedrock": "us.anthropic.claude-haiku-4-5-20251001-v1:0",
    },
    "sonnet": {
        "anthropic": "claude-sonnet-4-6",
        "bedrock": "us.anthropic.claude-sonnet-4-6",
    },
    "opus": {
        "anthropic": "claude-opus-4-8",
        "bedrock": "us.anthropic.claude-opus-4-8",
    },
}
MODEL_ORDER = ["haiku", "sonnet", "opus"]
PROVIDER_ORDER = ["anthropic", "bedrock"]

PROHIBITED_LEXICON = [
    "guaranteed", "risk-free", "no risk", "can't lose", "free money",
    "act now", "limited time", "everyone qualifies", "no medical exam ever",
    "cannot be denied", "zero cost", "instant approval",
]

SYSTEM_BASE = (
    "You are a precise compliance-extraction engine for insurance marketing "
    "review. You always respond with a single valid JSON object and nothing "
    "else: no markdown fences, no commentary, no trailing text."
)

# Token-count assumptions used only by --estimate (not by analyze.py, which
# uses provider-reported counts).
EST_SHORT_INPUT = 500
EST_LONG_INPUT = 9000
EST_CACHED_PREFIX = 8500
EST_OUTPUT = 300


# --------------------------------------------------------------------------
# Prompt construction
# --------------------------------------------------------------------------

def build_long_preamble() -> str:
    """Deterministic static compliance reference, about 8.5K tokens.

    Byte-identical on every call and every run, so both providers can cache it
    as a stable prefix. Content is synthetic filler shaped like a real
    compliance manual; it is context ballast, not ground truth.
    """
    states = [
        "Alabama", "Alaska", "Arizona", "Arkansas", "California", "Colorado",
        "Connecticut", "Delaware", "Florida", "Georgia", "Hawaii", "Idaho",
        "Illinois", "Indiana", "Iowa", "Kansas", "Kentucky", "Louisiana",
        "Maine", "Maryland", "Massachusetts", "Michigan", "Minnesota",
        "Mississippi", "Missouri", "Montana", "Nebraska", "Nevada",
        "New Hampshire", "New Jersey", "New Mexico", "New York",
        "North Carolina", "North Dakota", "Ohio", "Oklahoma", "Oregon",
        "Pennsylvania", "Rhode Island", "South Carolina", "South Dakota",
        "Tennessee", "Texas", "Utah", "Vermont", "Virginia", "Washington",
        "West Virginia", "Wisconsin", "Wyoming",
    ]
    sections = [
        "REFERENCE MANUAL: DIRECT-MAIL MARKETING REVIEW FOR INSURANCE PRODUCTS "
        "(SYNTHETIC TRAINING COPY - NOT LEGAL GUIDANCE). The sections below "
        "describe generic review considerations for direct-mail solicitation "
        "copy. They exist to provide realistic long-context ballast for a "
        "serving benchmark and must not be cited as regulatory guidance.\n"
    ]
    for i, state in enumerate(states, start=1):
        sections.append(
            f"Section {i:02d}: Review considerations for {state}. "
            f"Solicitation copy distributed to {state} households is reviewed "
            "for the clarity of benefit descriptions, the prominence of "
            "premium disclosures, and the accuracy of any statement about "
            "eligibility, underwriting, or claim timing. Reviewers confirm "
            "that any figure quoted in the copy, such as a coverage amount, a "
            "monthly premium, or a claims-processing interval, is traceable "
            "to the policy form on file, and that superlative or absolute "
            "language is flagged for the prohibited-phrase screen. Reviewers "
            "also confirm that the issuing entity is identified by its full "
            "legal name, that the copy states where the policy is not "
            "available if distribution is limited, and that reply materials "
            "route to a monitored address. Where the copy references a "
            "specific age band, the band is checked against the filed "
            "eligibility schedule; where it references a refund or review "
            "period, the stated day count is checked against the contract. "
            "Finally, reviewers note the postmark window of the campaign and "
            "record whether the mailer includes any time-pressure device, "
            "since time-pressure language interacts with the prohibited-"
            "phrase screen and can change the overall risk classification of "
            "the piece under the three-tier scheme described in the scoring "
            "appendix of this manual.\n"
        )
    sections.append(
        "Scoring appendix. A reviewed piece is classified into one of three "
        "tiers based solely on the count of distinct prohibited phrases "
        "present in the copy: three or more distinct phrases place the piece "
        "in the high tier, one or two place it in the medium tier, and zero "
        "place it in the low tier. The prohibited-phrase list is supplied "
        "with each review request and is matched case-insensitively as "
        "literal substrings. This appendix is the controlling rule when any "
        "narrative section of this manual appears to suggest otherwise.\n"
    )
    return "\n".join(sections)


LONG_PREAMBLE = build_long_preamble()


def build_system_text(context: str) -> str:
    if context == "long":
        return SYSTEM_BASE + "\n\n" + LONG_PREAMBLE
    return SYSTEM_BASE


def build_user_text(doc_text: str) -> str:
    lexicon = "; ".join(PROHIBITED_LEXICON)
    return (
        "Audit the direct-mail insurance marketing copy in DOCUMENT below.\n"
        "Return ONLY a JSON object with exactly these keys:\n"
        '  "jurisdiction": the US state the mailer targets, or "unspecified"\n'
        '  "claims_made": array of the marketing claims asserted, quoted or '
        "closely paraphrased from the copy\n"
        '  "prohibited_phrases": array of the phrases from the PROHIBITED '
        "LIST that appear in the copy (case-insensitive literal substring "
        "match), lowercase, deduplicated\n"
        '  "risk_level": "high" if 3 or more prohibited phrases appear, '
        '"medium" if 1 or 2, "low" if 0\n\n'
        f"PROHIBITED LIST: {lexicon}\n\n"
        f"DOCUMENT:\n{doc_text}"
    )


# --------------------------------------------------------------------------
# Scoring
# --------------------------------------------------------------------------

def _norm(s: str) -> str:
    return " ".join("".join(c.lower() if c.isalnum() or c.isspace() else " " for c in s).split())


def strip_fences(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        first_nl = t.find("\n")
        if first_nl != -1:
            t = t[first_nl + 1:]
        if t.rstrip().endswith("```"):
            t = t.rstrip()[:-3]
    return t.strip()


def score_output(raw_text: str, expected: dict) -> tuple[bool, str]:
    """Return (passed, reason). A completed task = valid JSON, schema keys
    present, jurisdiction + risk_level exact (case-insensitive),
    prohibited_phrases set-equal to expected (normalized), and claims recall
    >= 0.5 against the expected claims list."""
    try:
        obj = json.loads(strip_fences(raw_text))
    except (json.JSONDecodeError, ValueError):
        return False, "invalid_json"
    if not isinstance(obj, dict):
        return False, "not_an_object"
    required = ["jurisdiction", "claims_made", "prohibited_phrases", "risk_level"]
    if any(k not in obj for k in required):
        return False, "missing_keys"
    if not isinstance(obj["claims_made"], list) or not isinstance(obj["prohibited_phrases"], list):
        return False, "wrong_types"
    if _norm(str(obj["jurisdiction"])) != _norm(expected["jurisdiction"]):
        return False, "jurisdiction_mismatch"
    if _norm(str(obj["risk_level"])) != _norm(expected["risk_level"]):
        return False, "risk_level_mismatch"
    got_phrases = {_norm(str(p)) for p in obj["prohibited_phrases"]}
    want_phrases = {_norm(p) for p in expected["prohibited_phrases"]}
    if got_phrases != want_phrases:
        return False, "prohibited_phrases_mismatch"
    want_claims = [_norm(c) for c in expected["claims_made"]]
    got_claims = [_norm(str(c)) for c in obj["claims_made"]]
    if want_claims:
        hits = 0
        for wc in want_claims:
            for gc in got_claims:
                w_tok, g_tok = set(wc.split()), set(gc.split())
                jac = len(w_tok & g_tok) / max(1, len(w_tok | g_tok))
                if wc in gc or gc in wc or jac >= 0.5:
                    hits += 1
                    break
        if hits / len(want_claims) < 0.5:
            return False, "claims_recall_below_0.5"
    return True, "ok"


# --------------------------------------------------------------------------
# Matrix
# --------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class Cell:
    provider: str
    model: str          # haiku | sonnet | opus
    context: str        # short | long
    cache: bool
    concurrency: int

    @property
    def cell_id(self) -> str:
        c = "cache" if self.cache else "nocache"
        return f"{self.provider}:{self.model}:{self.context}:{c}:c{self.concurrency}"


def build_cells(providers: list[str]) -> list[Cell]:
    cells: list[Cell] = []
    for provider in providers:
        for model in MODEL_ORDER:
            cells.append(Cell(provider, model, "short", False, 1))
            cells.append(Cell(provider, model, "long", False, 1))
            # Cache axis only exists for the long context: the short prompt is
            # below the minimum cacheable prefix on both providers.
            cells.append(Cell(provider, model, "long", True, 1))
        # Concurrency probe: Sonnet tier only, long/nocache, concurrency 4.
        cells.append(Cell(provider, "sonnet", "long", False, 4))
    return cells


# --------------------------------------------------------------------------
# Provider runners
# --------------------------------------------------------------------------

@dataclasses.dataclass
class StreamResult:
    text: str
    ttft_s: float | None
    delta_times: list[float]      # monotonic timestamps of text deltas
    wall_s: float
    input_tokens: int | None
    output_tokens: int | None
    cache_read_tokens: int
    cache_write_tokens: int
    stop_reason: str | None
    throttle_retries: int


def _backoff_sleep(attempt: int) -> None:
    time.sleep(min(30.0, (2 ** attempt) + random.uniform(0, 1)))


class AnthropicRunner:
    name = "anthropic"

    def __init__(self) -> None:
        import anthropic  # lazy: only needed for real runs
        if not os.environ.get("ANTHROPIC_API_KEY"):
            sys.exit(
                "ANTHROPIC_API_KEY is not set. Run:\n"
                '  export ANTHROPIC_API_KEY="$(op read \'op://Personal/Anthropic API Key/credential\')"'
            )
        self._anthropic = anthropic
        self._client = anthropic.Anthropic()

    def run(self, cell: Cell, system_text: str, user_text: str) -> StreamResult:
        anthropic = self._anthropic
        model_id = MODELS[cell.model]["anthropic"]
        if cell.cache:
            system_param = [{
                "type": "text",
                "text": system_text,
                "cache_control": {"type": "ephemeral"},
            }]
        else:
            system_param = system_text
        throttle_retries = 0
        while True:
            t0 = time.perf_counter()
            ttft = None
            delta_times: list[float] = []
            try:
                with self._client.messages.stream(
                    model=model_id,
                    max_tokens=MAX_TOKENS,
                    system=system_param,
                    messages=[{"role": "user", "content": user_text}],
                ) as stream:
                    for event in stream:
                        if event.type == "content_block_delta" and getattr(event.delta, "type", "") == "text_delta":
                            now = time.perf_counter()
                            if ttft is None:
                                ttft = now - t0
                            delta_times.append(now)
                    final = stream.get_final_message()
                wall = time.perf_counter() - t0
                text = "".join(b.text for b in final.content if getattr(b, "type", "") == "text")
                usage = final.usage
                return StreamResult(
                    text=text,
                    ttft_s=ttft,
                    delta_times=delta_times,
                    wall_s=wall,
                    input_tokens=usage.input_tokens,
                    output_tokens=usage.output_tokens,
                    cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
                    cache_write_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0,
                    stop_reason=final.stop_reason,
                    throttle_retries=throttle_retries,
                )
            except anthropic.RateLimitError:
                throttle_retries += 1
                if throttle_retries > MAX_THROTTLE_RETRIES:
                    raise
                _backoff_sleep(throttle_retries)
            except anthropic.APIStatusError as e:
                if e.status_code >= 500:
                    throttle_retries += 1
                    if throttle_retries > MAX_THROTTLE_RETRIES:
                        raise
                    _backoff_sleep(throttle_retries)
                else:
                    raise


class BedrockRunner:
    name = "bedrock"

    def __init__(self, region: str) -> None:
        import boto3  # lazy: only needed for real runs
        from botocore.exceptions import ClientError
        self._ClientError = ClientError
        # Explicit region: AWS_REGION is the LOWEST-priority fallback in the
        # boto3 resolution chain; we pass region_name directly.
        self._client = boto3.client("bedrock-runtime", region_name=region)
        self.region = region

    def run(self, cell: Cell, system_text: str, user_text: str) -> StreamResult:
        model_id = MODELS[cell.model]["bedrock"]
        system_param: list[dict] = [{"text": system_text}]
        if cell.cache:
            system_param.append({"cachePoint": {"type": "default"}})
        throttle_retries = 0
        while True:
            t0 = time.perf_counter()
            ttft = None
            delta_times: list[float] = []
            try:
                response = self._client.converse_stream(
                    modelId=model_id,
                    system=system_param,
                    messages=[{"role": "user", "content": [{"text": user_text}]}],
                    inferenceConfig={"maxTokens": MAX_TOKENS},
                )
                chunks: list[str] = []
                stop_reason = None
                usage: dict = {}
                for event in response["stream"]:
                    if "contentBlockDelta" in event:
                        delta = event["contentBlockDelta"]["delta"]
                        if "text" in delta:
                            now = time.perf_counter()
                            if ttft is None:
                                ttft = now - t0
                            delta_times.append(now)
                            chunks.append(delta["text"])
                    elif "messageStop" in event:
                        stop_reason = event["messageStop"]["stopReason"]
                    elif "metadata" in event:
                        usage = event["metadata"].get("usage", {})
                wall = time.perf_counter() - t0
                return StreamResult(
                    text="".join(chunks),
                    ttft_s=ttft,
                    delta_times=delta_times,
                    wall_s=wall,
                    input_tokens=usage.get("inputTokens"),
                    output_tokens=usage.get("outputTokens"),
                    cache_read_tokens=usage.get("cacheReadInputTokens", 0) or 0,
                    cache_write_tokens=usage.get("cacheWriteInputTokens", 0) or 0,
                    stop_reason=stop_reason,
                    throttle_retries=throttle_retries,
                )
            except self._ClientError as e:
                code = e.response.get("Error", {}).get("Code", "")
                if code == "ThrottlingException":
                    throttle_retries += 1
                    if throttle_retries > MAX_THROTTLE_RETRIES:
                        raise
                    _backoff_sleep(throttle_retries)
                elif code == "AccessDeniedException":
                    sys.exit(
                        f"Bedrock AccessDeniedException for {model_id} in {self.region}.\n"
                        "Anthropic models need one-time model access (the FTU form) - see the\n"
                        "troubleshooting table in RUN.md. Enable access in the Bedrock console\n"
                        "under Model access, then rerun (resume is automatic)."
                    )
                else:
                    raise


class MockRunner:
    """Dry-run runner: exercises the whole pipeline with zero API spend.

    Deterministic per (cell, request): fabricated latency profiles per
    provider/model, fabricated provider-style token counts, and an injected
    invalid-JSON first attempt on every 5th request to exercise the retry
    path. All rows it produces are flagged mock=true.
    """
    name = "mock"

    _TTFT = {"haiku": 0.006, "sonnet": 0.010, "opus": 0.016}
    _ITL = {"haiku": 0.0004, "sonnet": 0.0007, "opus": 0.0011}

    def __init__(self, provider_label: str) -> None:
        self.provider_label = provider_label
        self._fail_next: set[tuple[str, int]] = set()

    def arm_failure(self, cell: Cell, request_index: int) -> None:
        self._fail_next.add((cell.cell_id, request_index))

    def run(self, cell: Cell, system_text: str, user_text: str,
            expected: dict | None = None, request_index: int = 0) -> StreamResult:
        seed = hash((cell.cell_id, request_index)) & 0xFFFF
        jitter = 1.0 + (seed % 200) / 1000.0          # 1.00-1.20x
        provider_penalty = 1.25 if self.provider_label == "bedrock" else 1.0
        ttft = self._TTFT[cell.model] * jitter * provider_penalty
        itl = self._ITL[cell.model] * jitter * provider_penalty
        if cell.cache and request_index > 0:
            ttft *= 0.6  # simulated cache-hit prefill speedup

        key = (cell.cell_id, request_index)
        if key in self._fail_next:
            self._fail_next.discard(key)
            payload = '{"jurisdiction": "Cal'   # truncated JSON -> retry path
        else:
            payload = json.dumps(expected or {})

        t0 = time.perf_counter()
        time.sleep(ttft)
        delta_times: list[float] = []
        chunk = 12
        for _ in range(0, max(1, len(payload)), chunk):
            time.sleep(itl)
            delta_times.append(time.perf_counter())
        wall = time.perf_counter() - t0

        approx_in = len(system_text + user_text) // 4
        out_tokens = max(1, len(payload) // 4)
        cache_read = cache_write = 0
        if cell.cache:
            cached = max(0, approx_in - 500)
            if request_index == 0:
                cache_write = cached
            else:
                cache_read = cached
            approx_in = approx_in - cached
        return StreamResult(
            text=payload,
            ttft_s=ttft,
            delta_times=delta_times,
            wall_s=wall,
            input_tokens=approx_in,
            output_tokens=out_tokens,
            cache_read_tokens=cache_read,
            cache_write_tokens=cache_write,
            stop_reason="end_turn",
            throttle_retries=0,
        )


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------

def load_corpus() -> list[tuple[str, str, dict]]:
    corpus = []
    for doc_path in sorted(EVAL_DOCS.glob("doc*.txt")):
        doc_id = doc_path.stem
        expected = json.loads((EVAL_EXPECTED / f"{doc_id}.json").read_text())
        corpus.append((doc_id, doc_path.read_text(), expected))
    if len(corpus) != 15:
        sys.exit(f"Expected 15 eval documents, found {len(corpus)} in {EVAL_DOCS}")
    return corpus


def load_completed() -> tuple[set[tuple[str, int]], bool]:
    done: set[tuple[str, int]] = set()
    has_mock = False
    if RESULTS_PATH.exists():
        for line in RESULTS_PATH.read_text().splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            done.add((row["cell_id"], row["request_index"]))
            has_mock = has_mock or row.get("mock", False)
    return done, has_mock


def run_one_request(runner, cell: Cell, request_index: int,
                    doc_id: str, doc_text: str, expected: dict,
                    dry_run: bool) -> dict:
    system_text = build_system_text(cell.context)
    user_text = build_user_text(doc_text)
    totals = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
    throttle_retries = 0
    last: StreamResult | None = None
    passed, reason = False, "not_run"
    attempts = 0
    for attempt in range(MAX_VALIDATION_ATTEMPTS):
        attempts = attempt + 1
        if dry_run:
            last = runner.run(cell, system_text, user_text,
                              expected=expected, request_index=request_index)
        else:
            last = runner.run(cell, system_text, user_text)
        totals["input"] += last.input_tokens or 0
        totals["output"] += last.output_tokens or 0
        totals["cache_read"] += last.cache_read_tokens
        totals["cache_write"] += last.cache_write_tokens
        throttle_retries += last.throttle_retries
        passed, reason = score_output(last.text, expected)
        if passed:
            break
    assert last is not None
    itls = [b - a for a, b in zip(last.delta_times, last.delta_times[1:])]
    itl_p50 = statistics.median(itls) if itls else None
    itl_p99 = (sorted(itls)[max(0, int(len(itls) * 0.99) - 1)] if itls else None)
    return {
        "cell_id": cell.cell_id,
        "provider": cell.provider,
        "model": cell.model,
        "context": cell.context,
        "cache": cell.cache,
        "concurrency": cell.concurrency,
        "request_index": request_index,
        "doc_id": doc_id,
        "ts": time.time(),
        "ttft_s": last.ttft_s,
        "itl_p50_s": itl_p50,
        "itl_p99_s": itl_p99,
        "wall_s": last.wall_s,
        "input_tokens": totals["input"],
        "output_tokens": totals["output"],
        "cache_read_tokens": totals["cache_read"],
        "cache_write_tokens": totals["cache_write"],
        "stop_reason": last.stop_reason,
        "retries": attempts - 1,
        "throttle_retries": throttle_retries,
        "passed": passed,
        "fail_reason": None if passed else reason,
        # Raw model output, truncated - added 25 Jul after the first smoke run:
        # five risk_level_mismatch failures were undiagnosable without seeing
        # what the model actually said. Observability amendment only; prompt
        # and scoring are unchanged.
        "output_head": (last.text[:1500] if getattr(last, "text", None) else None),
        "mock": dry_run,
    }


def append_row(row: dict) -> None:
    with RESULTS_PATH.open("a") as f:
        f.write(json.dumps(row) + "\n")


def run_matrix(args: argparse.Namespace) -> None:
    corpus = load_corpus()
    done, has_mock = load_completed()
    if not args.dry_run and has_mock:
        sys.exit(
            "results.jsonl contains DRY-RUN (mock) rows. Delete it before a real "
            "run so mock and real data never mix:\n  rm results.jsonl"
        )
    providers = PROVIDER_ORDER if args.provider == "both" else [args.provider]
    cells = build_cells(providers)
    if args.cells:
        cells = [c for c in cells if args.cells in c.cell_id]
        if not cells:
            sys.exit(f"--cells '{args.cells}' matched no cell IDs. Example IDs: "
                     + ", ".join(c.cell_id for c in build_cells(providers)[:4]))

    runners: dict[str, object] = {}
    for p in providers:
        if args.dry_run:
            runners[p] = MockRunner(p)
        elif p == "anthropic":
            runners[p] = AnthropicRunner()
        else:
            runners[p] = BedrockRunner(args.region)

    total = sum(N_PER_CELL for _ in cells)
    print(f"Matrix: {len(cells)} cells x {N_PER_CELL} requests = {total} requests"
          + (" [DRY RUN - zero spend]" if args.dry_run else ""))

    for cell in cells:
        pending = [i for i in range(N_PER_CELL) if (cell.cell_id, i) not in done]
        if not pending:
            print(f"  {cell.cell_id}: complete, skipping")
            continue
        print(f"  {cell.cell_id}: running {len(pending)} request(s)")
        runner = runners[cell.provider]
        if args.dry_run and isinstance(runner, MockRunner):
            for i in pending:
                if i % 5 == 3:   # deterministic invalid-output injection
                    runner.arm_failure(cell, i)

        def work(i: int) -> dict:
            doc_id, doc_text, expected = corpus[i % len(corpus)]
            return run_one_request(runner, cell, i, doc_id, doc_text, expected,
                                   dry_run=args.dry_run)

        if cell.concurrency > 1:
            with concurrent.futures.ThreadPoolExecutor(max_workers=cell.concurrency) as ex:
                for row in ex.map(work, pending):
                    append_row(row)
        else:
            if cell.cache and 0 in pending:
                # Run request 0 first and alone so it writes the cache; the
                # rest read it within the 5-minute TTL.
                append_row(work(0))
                pending = [i for i in pending if i != 0]
            for i in pending:
                append_row(work(i))

    print(f"\nDone. Results appended to {RESULTS_PATH}")
    print("Next: uv run analyze.py")


# --------------------------------------------------------------------------
# Cost estimation
# --------------------------------------------------------------------------

def estimate(args: argparse.Namespace) -> None:
    pricing = json.loads(PRICING_PATH.read_text())
    providers = PROVIDER_ORDER if args.provider == "both" else [args.provider]
    cells = build_cells(providers)
    print("Cost estimate (from pricing.json + matrix assumptions: "
          f"{EST_SHORT_INPUT} short-input / {EST_LONG_INPUT} long-input / "
          f"{EST_OUTPUT} output tokens per request, N={N_PER_CELL}, "
          "no retry overhead - add about 10% margin):\n")
    grand = 0.0
    unknown = False
    for provider in providers:
        ptotal = 0.0
        pknown = True
        for cell in [c for c in cells if c.provider == provider]:
            p = pricing[provider]["models"][cell.model]
            if min(p["input_per_mtok"], p["output_per_mtok"]) < 0:
                pknown = False
                continue
            in_tok = EST_LONG_INPUT if cell.context == "long" else EST_SHORT_INPUT
            if cell.cache:
                write = EST_CACHED_PREFIX
                reads = EST_CACHED_PREFIX * (N_PER_CELL - 1)
                uncached = (in_tok - EST_CACHED_PREFIX) * N_PER_CELL
                cost = (uncached * p["input_per_mtok"]
                        + write * p["cache_write_per_mtok"]
                        + reads * p["cache_read_per_mtok"]) / 1e6
            else:
                cost = in_tok * N_PER_CELL * p["input_per_mtok"] / 1e6
            cost += EST_OUTPUT * N_PER_CELL * p["output_per_mtok"] / 1e6
            ptotal += cost
            print(f"  {cell.cell_id:<38s} ${cost:8.4f}")
        if pknown:
            print(f"  {provider} subtotal: ${ptotal:.2f}\n")
            grand += ptotal
        else:
            unknown = True
            print(f"  {provider} subtotal: UNKNOWN - pricing.json still has "
                  "PLACEHOLDER (-1.0) values. Token volume mirrors the other "
                  "provider's cells; fill the real prices to compute.\n")
    if grand:
        print(f"Computable total: ${grand:.2f}")
    if unknown:
        print("Bedrock cost cannot be estimated until pricing.json is filled "
              "from the AWS Bedrock pricing page.")


# --------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--provider", choices=["anthropic", "bedrock", "both"],
                    default="both")
    ap.add_argument("--dry-run", action="store_true",
                    help="Run the full pipeline with a mock client and zero spend")
    ap.add_argument("--cells", default=None,
                    help="Substring filter on cell IDs, e.g. 'anthropic:sonnet:short'")
    ap.add_argument("--region", default=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"),
                    help="AWS region for Bedrock (default: AWS_DEFAULT_REGION or us-east-1)")
    ap.add_argument("--estimate", action="store_true",
                    help="Print a cost estimate from pricing.json and exit")
    ap.add_argument("--out-dir", default=None,
                    help="Directory for results.jsonl (default: repo root). "
                         "Used by the EC2 runner so results land where "
                         "push_results.sh expects them.")
    args = ap.parse_args()
    if args.out_dir:
        global RESULTS_PATH
        RESULTS_PATH = pathlib.Path(args.out_dir).resolve() / "results.jsonl"
        RESULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    if args.estimate:
        estimate(args)
        return
    run_matrix(args)


if __name__ == "__main__":
    main()
