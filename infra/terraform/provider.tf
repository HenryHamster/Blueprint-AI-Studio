terraform {
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 5.0"
    }
  }

  # ----------------------------------------------------------------------------
  # GCS remote state backend
  #
  # Bootstrap (one-time, when standing up a new GCP project):
  #   gsutil mb -p <PROJECT_ID> -l <REGION> gs://<PROJECT_ID>-tf-state
  #   gsutil versioning set on gs://<PROJECT_ID>-tf-state
  #
  # The bucket is hardcoded below because terraform's backend block does NOT
  # support variable interpolation. With it left blank (and a comment saying
  # "pass at init time via -backend-config"), the `.github/workflows/terraform.yml`
  # bare `terraform init` step would prompt interactively for the bucket name
  # and hang the CI runner forever (saw a 1+ hour stall on 2026-05-03 before
  # diagnosing). Hardcoding here trades the intended single-config portability
  # for a workflow that actually finishes; if you fork to a new project, edit
  # this string OR override via `terraform init -reconfigure -backend-config="bucket=..."`.
  #
  # Why GCS instead of local: state was previously local-only (per project
  # memory `project_terraform_backend.md`). Multiple operators / CI runs can't
  # share state without a remote backend; local state on a single laptop is a
  # single point of failure for prod infra.
  # ----------------------------------------------------------------------------
  backend "gcs" {
    bucket = "blueprint-orchestrator-tf-state"
    prefix = "terraform/state"
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
  zone    = var.zone

  # Some Google APIs (Cloud Billing Budgets, Service Networking, others) reject
  # ADC requests that don't specify a quota/billing project. user_project_override
  # tells the provider to attribute every API call to var.project_id for quota
  # purposes, fixing 403 "API requires a quota project" errors at apply time.
  user_project_override = true
  billing_project       = var.project_id
}
