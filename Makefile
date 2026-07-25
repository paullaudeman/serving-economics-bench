# serving-economics-bench - EC2 runner rig
#
# Saturday flow (details in RUN-EC2.md):
#   make key -> make deploy -> make smoke -> make run -> make pull -> make destroy
#
# Secret rule: the Anthropic key travels laptop (1Password, op read) ->
# SSM SecureString (make key) -> runner process env at run time. It never
# touches disk, Terraform variables, Terraform state, or user_data.

# Personal overrides live in local.mk (gitignored, never committed).
-include local.mk

# 1Password item path for the Anthropic key - override in local.mk.
# Find yours with: op item list | grep -i anthropic
OP_ANTHROPIC_KEY_PATH ?= op://Personal/Anthropic API Key/credential

REGION     ?= us-east-1
PARAM_NAME := /serving-econ-bench/anthropic-key
SMOKE_CELL := anthropic:haiku:short
TF         := terraform -chdir=terraform

.PHONY: key deploy run smoke pull destroy _ssm-run

## key: write the Anthropic key from 1Password into an SSM SecureString (out-of-band, never in state)
key:
	aws ssm put-parameter --region $(REGION) --name $(PARAM_NAME) --type SecureString --overwrite --value "$$(op read '$(OP_ANTHROPIC_KEY_PATH)')"
	@echo "Anthropic key stored as SecureString $(PARAM_NAME) in $(REGION). Not on disk, not in Terraform state."

## deploy: terraform apply the rig, then zip + upload the bench code to the bucket
deploy:
	$(TF) init -input=false
	$(TF) apply -var "region=$(REGION)"
	@set -e; \
	BUCKET=$$($(TF) output -raw bucket_name); \
	TMP=$$(mktemp -d); \
	zip -q -r "$$TMP/code.zip" bench.py analyze.py pricing.json run_remote.sh push_results.sh eval; \
	aws s3 cp "$$TMP/code.zip" "s3://$$BUCKET/code/code.zip" --region $(REGION); \
	rm -rf "$$TMP"; \
	echo "Code uploaded to s3://$$BUCKET/code/code.zip"

## run: full matrix (--provider both) on the runner via SSM, wait and report
run:
	@$(MAKE) --no-print-directory _ssm-run FILTER= LABEL=full

## smoke: one cheap Anthropic short-context cell on the runner
smoke:
	@$(MAKE) --no-print-directory _ssm-run FILTER=$(SMOKE_CELL) LABEL=smoke

# Shared SSM dispatch: download code zip, extract, run run_remote.sh (which
# reads the key parameter into env at runtime), poll until terminal status.
_ssm-run:
	@set -e; \
	IID=$$($(TF) output -raw instance_id); \
	BUCKET=$$($(TF) output -raw bucket_name); \
	PARAMS=$$(mktemp); \
	printf '{"commands":["set -euo pipefail","export HOME=/root","mkdir -p /opt/bench","cd /opt/bench","rm -rf run && mkdir run && cd run","aws s3 cp s3://%s/code/code.zip code.zip --region %s","python3 -m zipfile -e code.zip .","bash run_remote.sh %s %s %s"],"executionTimeout":["10800"]}' \
	  "$$BUCKET" "$(REGION)" "$$BUCKET" "$(REGION)" "$(FILTER)" > "$$PARAMS"; \
	CMD_ID=$$(aws ssm send-command --region $(REGION) --instance-ids "$$IID" \
	  --document-name AWS-RunShellScript --comment "serving-econ-bench $(LABEL)" \
	  --parameters "file://$$PARAMS" --query Command.CommandId --output text); \
	rm -f "$$PARAMS"; \
	echo "SSM command $$CMD_ID dispatched to $$IID ($(LABEL)). Polling every 30s..."; \
	STATUS=Pending; \
	while :; do \
	  STATUS=$$(aws ssm get-command-invocation --region $(REGION) --command-id "$$CMD_ID" --instance-id "$$IID" --query Status --output text 2>/dev/null || echo Pending); \
	  echo "  $$(date '+%H:%M:%S')  $$STATUS"; \
	  case "$$STATUS" in Success|Failed|Cancelled|TimedOut) break;; esac; \
	  sleep 30; \
	done; \
	echo "--- stdout (tail) ---"; \
	aws ssm get-command-invocation --region $(REGION) --command-id "$$CMD_ID" --instance-id "$$IID" --query StandardOutputContent --output text | tail -40; \
	echo "--- stderr (tail) ---"; \
	aws ssm get-command-invocation --region $(REGION) --command-id "$$CMD_ID" --instance-id "$$IID" --query StandardErrorContent --output text | tail -40; \
	test "$$STATUS" = Success

## pull: sync the latest results/<timestamp>/ prefix locally and analyze it
pull:
	@set -e; \
	BUCKET=$$($(TF) output -raw bucket_name); \
	LATEST=$$(aws s3 ls "s3://$$BUCKET/results/" --region $(REGION) | awk '{print $$NF}' | tr -d '/' | sort | tail -1); \
	test -n "$$LATEST" || { echo "No results under s3://$$BUCKET/results/ yet - run make smoke or make run first."; exit 1; }; \
	mkdir -p "results-ec2/$$LATEST"; \
	aws s3 sync "s3://$$BUCKET/results/$$LATEST/" "results-ec2/$$LATEST/" --region $(REGION); \
	uv run analyze.py "results-ec2/$$LATEST"; \
	echo "Analyzed: results-ec2/$$LATEST/results.md and results-ec2/$$LATEST/chart.png"

## destroy: tear down the rig AND delete the key parameter (same-day discipline)
destroy:
	$(TF) destroy -var "region=$(REGION)"
	-aws ssm delete-parameter --region $(REGION) --name $(PARAM_NAME)
	@echo "Rig destroyed. The SecureString $(PARAM_NAME) is DELETED - rerun 'make key' before the next deploy."
