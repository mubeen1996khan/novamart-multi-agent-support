#!/usr/bin/env bash
# Checks AWS credentials and Bedrock model access for the NovaMart project.
# Usage: ./scripts/check_access.sh
set -uo pipefail

REGION="${AWS_REGION:-us-east-1}"
MODELS=(
  "us.anthropic.claude-haiku-4-5-20251001-v1:0"    # OrchestratorAgent
  "us.anthropic.claude-sonnet-4-5-20250929-v1:0"   # worker agents
)
EMBEDDING_MODEL="amazon.titan-embed-text-v2:0"     # Knowledge Bases
failures=0

pass() { echo "  [PASS] $1"; }
fail() { echo "  [FAIL] $1"; failures=$((failures + 1)); }

echo "Region: $REGION"

echo "1. AWS credentials"
if account=$(aws sts get-caller-identity --query Account --output text 2>&1); then
  pass "signed in to account ****${account: -4}"
else
  fail "credentials not working: $account"
  echo "     Fix: get fresh keys from the Udacity lab and run 'aws configure'"
  exit 1
fi

echo "2. Model access"
for model in "${MODELS[@]}"; do
  if reply=$(aws bedrock-runtime converse --region "$REGION" \
      --model-id "$model" \
      --messages '[{"role":"user","content":[{"text":"Say OK"}]}]' \
      --query 'output.message.content[0].text' --output text 2>&1); then
    pass "$model replied: $reply"
  else
    fail "$model: $reply"
  fi
done

echo "3. Embedding model"
found=$(aws bedrock list-foundation-models --region "$REGION" \
  --query "modelSummaries[?modelId=='$EMBEDDING_MODEL'].modelId" --output text 2>&1)
if [[ "$found" == "$EMBEDDING_MODEL" ]]; then
  pass "$EMBEDDING_MODEL available"
else
  fail "$EMBEDDING_MODEL not found: $found"
fi

echo
if (( failures == 0 )); then
  echo "All checks passed."
else
  echo "$failures check(s) failed."
  exit 1
fi
