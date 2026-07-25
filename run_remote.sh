#!/usr/bin/env bash
# Runs ON the EC2 runner (delivered inside code.zip, invoked by the SSM
# command that `make run` / `make smoke` sends). Not meant for the laptop.
#
# Usage: run_remote.sh <bucket> <region> [cells-filter]
#   no filter  -> full matrix, both providers
#   filter set -> smoke: one Anthropic cell (matches SMOKE_CELL in the Makefile)
#
# Secret rule: the Anthropic key exists only in the SSM SecureString parameter
# and in this process's environment. It is never written to disk.
set -euo pipefail

BUCKET="$1"
REGION="$2"
FILTER="${3:-}"

export AWS_DEFAULT_REGION="$REGION"

ANTHROPIC_API_KEY="$(aws ssm get-parameter \
  --name /serving-econ-bench/anthropic-key \
  --with-decryption \
  --region "$REGION" \
  --query Parameter.Value \
  --output text)"
export ANTHROPIC_API_KEY

if [ -n "$FILTER" ]; then
  # Provider follows the filter prefix so partial re-dispatches work for
  # either door (added 25 Jul after the bedrock-half redispatch matched zero
  # cells: this script originally hardcoded --provider anthropic for any
  # filtered run, which only fit the smoke case).
  case "$FILTER" in
    bedrock*)   PROV=bedrock ;;
    anthropic*) PROV=anthropic ;;
    *)          PROV=both ;;
  esac
  BENCH_ARGS=(--provider "$PROV" --cells "$FILTER")
else
  BENCH_ARGS=(--provider both)
fi

uv run bench.py "${BENCH_ARGS[@]}" --out-dir out

bash push_results.sh "$BUCKET" "$REGION"
