variable "project_id" {
  description = "The GCP Project ID"
  type        = string
}

variable "region" {
  description = "The GCP Region"
  type        = string
  default     = "us-central1"
}

variable "zone" {
  description = "The GCP Zone"
  type        = string
  default     = "us-central1-a"
}

variable "github_repository" {
  description = "The GitHub repository (e.g., 'owner/repo') that is allowed to deploy via Workload Identity. Set to empty string to skip CI/CD setup."
  type        = string
  default     = ""
}

variable "enable_cicd" {
  description = "Whether to create Workload Identity Federation resources for GitHub Actions CI/CD"
  type        = bool
  default     = false
}

variable "orchestrator_url" {
  description = "The public URL of the Cloud Run orchestrator (set after first deploy)"
  type        = string
  default     = ""
}
