#!/bin/bash
set -e

echo "Building Agent VM Image with Packer..."

# Ensure GCP_PROJECT_ID is set
if [ -z "$GCP_PROJECT_ID" ]; then
  echo "Error: GCP_PROJECT_ID environment variable is not set."
  echo "Please set it using: export GCP_PROJECT_ID=your-project-id"
  exit 1
fi

# Initialize Packer plugins
packer init agent.pkr.hcl

# Build the image
packer build -var "project_id=$GCP_PROJECT_ID" agent.pkr.hcl

echo "Packer build complete. The image family 'blueprint-agent-v2' is now available in project '$GCP_PROJECT_ID'."
