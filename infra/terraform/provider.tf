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
  # Then `terraform init -backend-config="bucket=<PROJECT_ID>-tf-state"` from
  # this directory. Bucket name is passed at init time so this file isn't
  # tied to a specific project ID.
  #
  # If you're standing up a brand-new project: comment this block out for the
  # FIRST `terraform apply`, run `gsutil mb` from the GCS bucket, then re-add
  # the block and `terraform init -migrate-state` to upload state to the bucket.
  #
  # Why GCS instead of local: state was previously local-only (per project
  # memory `project_terraform_backend.md`). Multiple operators / CI runs can't
  # share state without a remote backend; local state on a single laptop is a
  # single point of failure for prod infra.
  # ----------------------------------------------------------------------------
  backend "gcs" {
    # Bucket name passed at init: terraform init -backend-config="bucket=..."
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
