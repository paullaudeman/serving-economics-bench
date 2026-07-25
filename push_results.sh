#!/usr/bin/env bash
# Runs ON the EC2 runner, from the extracted code directory, after bench.py.
# Uploads out/results.jsonl plus a run-metadata.json (instance type, region,
# AZ, UTC timestamp, git-less code hash) to s3://<bucket>/results/<timestamp>/.
#
# Usage: push_results.sh <bucket> <region>
set -euo pipefail

BUCKET="$1"
REGION="$2"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"

# Instance identity via IMDSv2 (the instance enforces http_tokens=required).
TOKEN="$(curl -sf -X PUT http://169.254.169.254/latest/api/token \
  -H 'X-aws-ec2-metadata-token-ttl-seconds: 300' || true)"
imds() {
  curl -sf -H "X-aws-ec2-metadata-token: $TOKEN" \
    "http://169.254.169.254/latest/meta-data/$1" || echo unknown
}
INSTANCE_TYPE="$(imds instance-type)"
AZ="$(imds placement/availability-zone)"

# Git-less code hash: sha256 over the sorted set of shipped files (results
# excluded), so a result set is traceable to the exact code that produced it.
CODE_HASH="$(find . -type f ! -path './out/*' ! -name 'code.zip' -print0 \
  | sort -z | xargs -0 sha256sum | sha256sum | cut -d' ' -f1)"

cat > out/run-metadata.json <<EOF
{
  "instance_type": "$INSTANCE_TYPE",
  "region": "$REGION",
  "availability_zone": "$AZ",
  "timestamp_utc": "$STAMP",
  "code_sha256": "$CODE_HASH"
}
EOF

aws s3 cp out/results.jsonl "s3://$BUCKET/results/$STAMP/results.jsonl" --region "$REGION"
aws s3 cp out/run-metadata.json "s3://$BUCKET/results/$STAMP/run-metadata.json" --region "$REGION"

echo "Uploaded results to s3://$BUCKET/results/$STAMP/"
