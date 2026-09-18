#!/usr/bin/env bash
# Assert the cloud-verify teardown left no Project=slipstream resource billing.
# Karpenter's g5 and its gp3 root live outside Terraform state (Karpenter is an
# in-cluster controller), so `just cluster-down` runs `terraform destroy` and never sees
# them. This queries EC2 by the Project tag both those nodes and the Terraform
# stacks carry, filtered server-side to still-billing states, and hands the two
# JSON documents to the classifier (`slipstream-bench zero-leak`), which exits
# non-zero if anything survives. Region is passed in because after `just cluster-down`
# the eks stack has no outputs left to read it from.
set -euo pipefail

region="${1:?usage: zero-leak-sweep.sh <region>}"

tmp="$(mktemp -d)"
trap 'rm -rf "${tmp}"' EXIT

# Fetch everything carrying the Project tag and let the classifier decide what is
# still billing — the billing-state set lives in one place (slipstream_bench
# zero-leak), so a terminated instance or a deleting volume in this output is
# correctly ignored there rather than filtered (and duplicated) here.
aws ec2 describe-instances --region "${region}" \
  --filters "Name=tag:Project,Values=slipstream" \
  >"${tmp}/instances.json"

aws ec2 describe-volumes --region "${region}" \
  --filters "Name=tag:Project,Values=slipstream" \
  >"${tmp}/volumes.json"

uv run slipstream-bench zero-leak \
  --instances "${tmp}/instances.json" \
  --volumes "${tmp}/volumes.json"
