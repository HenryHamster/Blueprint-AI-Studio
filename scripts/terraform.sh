#!/usr/bin/env bash
# Run Terraform plan/apply for Blueprint infrastructure.
#
# Usage:
#   ./scripts/terraform.sh plan          # Preview changes
#   ./scripts/terraform.sh apply         # Apply changes
#   ./scripts/terraform.sh destroy       # Tear down (with confirmation)
#   ./scripts/terraform.sh <any-cmd>     # Pass-through to terraform
#
# Configuration (auto-detected from gcloud, override with env vars):
#   TF_VAR_project_id          GCP project ID
#   TF_VAR_region              GCP region (default: us-central1)
#   TF_VAR_github_owner        GitHub owner (default: extracted from git remote)
#   TF_VAR_github_repository   GitHub repo (default: extracted from git remote)
#   TF_VAR_enable_cicd         Enable WIF for CI/CD (default: true)
#   TF_VAR_orchestrator_url    Cloud Run URL (default: auto-detected)
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TF_DIR="${SCRIPT_DIR}/../infra/terraform"

# --- Auto-detect configuration ---

# Project ID: from env, or gcloud config
if [[ -z "${TF_VAR_project_id:-}" ]]; then
  export TF_VAR_project_id="$(gcloud config get-value project 2>/dev/null || true)"
  if [[ -z "${TF_VAR_project_id}" ]]; then
    echo "ERROR: Could not detect GCP project. Set TF_VAR_project_id or run 'gcloud config set project <id>'" >&2
    exit 1
  fi
fi

# Region
export TF_VAR_region="${TF_VAR_region:-us-central1}"

# GitHub owner/repo: from git remote
if [[ -z "${TF_VAR_github_owner:-}" ]] || [[ -z "${TF_VAR_github_repository:-}" ]]; then
  REMOTE_URL="$(git -C "${SCRIPT_DIR}" remote get-url origin 2>/dev/null || true)"
  if [[ "${REMOTE_URL}" =~ github\.com[:/]([^/]+)/([^/.]+) ]]; then
    export TF_VAR_github_owner="${TF_VAR_github_owner:-${BASH_REMATCH[1]}}"
    export TF_VAR_github_repository="${TF_VAR_github_repository:-${BASH_REMATCH[1]}/${BASH_REMATCH[2]}}"
  fi
fi

# CI/CD enabled by default
export TF_VAR_enable_cicd="${TF_VAR_enable_cicd:-true}"

# Orchestrator URL: auto-detect from Cloud Run
if [[ -z "${TF_VAR_orchestrator_url:-}" ]]; then
  TF_VAR_orchestrator_url="$(gcloud run services describe blueprint-orchestrator \
    --region="${TF_VAR_region}" --format='value(status.url)' 2>/dev/null || true)"
  export TF_VAR_orchestrator_url="${TF_VAR_orchestrator_url:-}"
fi

# --- Run Terraform ---

echo "Project:      ${TF_VAR_project_id}"
echo "Region:       ${TF_VAR_region}"
echo "GitHub:       ${TF_VAR_github_owner:-<not set>} / ${TF_VAR_github_repository:-<not set>}"
echo "CI/CD:        ${TF_VAR_enable_cicd}"
echo "Orchestrator: ${TF_VAR_orchestrator_url:-<not set>}"
echo "---"

cd "${TF_DIR}"

# Init if needed
if [[ ! -d .terraform ]]; then
  echo "Running terraform init..."
  terraform init
fi

ACTION="${1:-plan}"
shift 2>/dev/null || true

terraform "${ACTION}" "$@"
