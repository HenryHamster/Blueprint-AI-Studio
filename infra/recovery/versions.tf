terraform {
  required_version = ">= 1.5.0"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 5.0"
    }
    google-beta = {
      source  = "hashicorp/google-beta"
      version = "~> 5.0"
    }
  }

  # This intentionally has no bucket configured in source. Initialize this
  # separate recovery root with a recovery-project state bucket, for example:
  # terraform init -backend-config="bucket=<recovery-project>-tf-state"
  backend "gcs" {}
}
