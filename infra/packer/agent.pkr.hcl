packer {
  required_plugins {
    googlecompute = {
      version = ">= 1.1.1"
      source  = "github.com/hashicorp/googlecompute"
    }
  }
}

variable "project_id" {
  type    = string
  default = env("GCP_PROJECT_ID")
}

variable "zone" {
  type    = string
  default = "us-central1-f"
}

# Optional GitHub token used at build time to seed the git object cache and
# warm the npm cache. When empty the build still succeeds — cache seeding is
# skipped and cold VMs fall back to a full clone / cold npm install.
variable "github_token" {
  type      = string
  default   = env("GITHUB_TOKEN")
  sensitive = true
}

# owner/repo to pre-bake into the git object cache (the dominant target repo).
variable "cache_repo" {
  type    = string
  default = "HenryHamster/BlueprintAIStudio"
}

# Rebuild trigger: memo handler fixes from PR #5792 (extractCodexAgentMessageText,
# lastResultText full-text capture) and PR #5921 (deferred_findings + memo reopen
# continuation retry). Extractor hardening: extractJsonObjectDetailed now validates
# that required keys are own properties of the parsed object, preventing Codex JSONL
# envelope objects from being silently mis-extracted as the memo result.
# - PR #6138 (aa0b8a3): own-property validation in json-extract — prevents envelope
#   objects from being mis-extracted as the memo result payload.
# - PR #6162 (45a2d0a): autonomy cluster fixes — corrects cluster-dispatch edge cases
#   that caused memo tasks to stall without emitting turn.completed.
# - Current HEAD memo handler state: full extraction path and reopen retry are correct
#   on main; this rebuild bakes these fixes into the deployed agent image.
# Rebuild trigger 2026-05-27: redeploy after memo task c461ab10 failed on stale handler — ensure PR #5792 / #5921 / #6138 are in active VM image.
source "googlecompute" "agent_image" {
  project_id          = var.project_id
  source_image_family = "ubuntu-2204-lts"
  zone                = var.zone
  image_name          = "blueprint-agent-v2-{{timestamp}}"
  image_family        = "blueprint-agent-v2"
  ssh_username        = "packer"
}

build {
  sources = ["source.googlecompute.agent_image"]

  provisioner "shell" {
    inline = [
      "# Resilient apt-get update: retry with fallback mirrors if GPG/signature errors occur",
      "sudo apt-get update || (sudo sed -i 's|http://archive.ubuntu.com|http://us.archive.ubuntu.com|g' /etc/apt/sources.list && sudo apt-get update) || (sudo sed -i 's|http://us.archive.ubuntu.com|http://mirrors.edge.kernel.org|g' /etc/apt/sources.list && sudo apt-get update)",
      "sudo apt-get install -y curl git wget gnupg",
      "curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -",
      "sudo apt-get install -y nodejs docker.io",
      "# Point every npm invocation (build-time globals + npm ci below, and the",
      "# agent's task-time npm ci) at a single shared cache dir that we pre-warm",
      "# and bake into the image, so cold VMs hit cache instead of re-downloading.",
      "sudo mkdir -p /opt/agent-cache/npm",
      "sudo npm config set cache /opt/agent-cache/npm --global",
      "sudo systemctl enable docker",
      "sudo systemctl start docker",
      "# Pre-pull postgres:15 (used by scripts/test-backend.sh --postgres) so each",
      "# agent VM doesn't hit the unauthenticated Docker Hub rate limit at test time.",
      "sudo docker pull postgres:15",
      "sudo npm install -g @anthropic-ai/claude-code @openai/codex @google/gemini-cli",
      "sudo apt-get install -y python3-pip",
      "sudo pip3 install ruff",
      "git config --global user.email 'blueprint-agent@automated.dev'",
      "git config --global user.name 'Blueprint Agent'",
      "sudo mkdir -p /opt/agent /tmp/workspace",
      "sudo chown -R packer:packer /opt/agent",
      "# Pre-create workspace and install gcloud/gsutil for faster startup",
      "sudo apt-get install -y apt-transport-https ca-certificates jq",
      "# Pre-warm CLI installs so the baked image already has first-run caches",
      "claude --version || true",
      "codex --version || true",
      "gemini --version || true"
    ]
  }

  provisioner "file" {
    source      = "../../agent/src"
    destination = "/opt/agent/src"
  }

  provisioner "file" {
    source      = "../../agent/package.json"
    destination = "/opt/agent/package.json"
  }

  provisioner "file" {
    source      = "../../agent/package-lock.json"
    destination = "/opt/agent/package-lock.json"
  }

  provisioner "shell" {
    inline = [
      "sudo useradd -m -s /bin/bash agent",
      "sudo usermod -aG docker agent",
      "# Passwordless sudo for the agent user. Justification: agent VMs are",
      "# ephemeral (one task per VM, destroyed at end), single-tenant, and",
      "# isolated by GCP service account scope. The real authority that",
      "# matters (orchestrator API, GCS, cross-VM) is the VM's SA, not",
      "# sudo. Granting sudo lets Claude self-heal precondition gaps inline",
      "# (apt-get install missing-lib, npx playwright install --with-deps,",
      "# pip install pkg, etc.) instead of burning retries on toolchain",
      "# issues that would otherwise need a packer rebuild loop. The",
      "# blast radius of accidental misuse is bounded by the VM lifecycle.",
      "echo 'agent ALL=(ALL) NOPASSWD: ALL' | sudo tee /etc/sudoers.d/agent",
      "sudo chmod 440 /etc/sudoers.d/agent",
      "sudo visudo -cf /etc/sudoers.d/agent",
      "# /opt/agent-cache/npm was created and populated as root in the previous",
      "# provisioner block (sudo mkdir + sudo npm install -g), so the cache _cacache",
      "# tree is root-owned. The npm ci below runs as the packer user (no sudo),",
      "# and npm 11+ refuses to write into a root-owned cache → EACCES → Packer",
      "# exits 243. Chown the cache to packer first; the second provisioner block",
      "# re-chowns the whole tree to agent:agent at runtime ownership boundary.",
      "sudo chown -R packer:packer /opt/agent-cache",
      "# npm ci wrapped in a single retry: esbuild's postinstall writes its native",
      "# binary then immediately execs it, which can intermittently race to ETXTBSY.",
      "cd /opt/agent && (npm ci || (sleep 5 && npm ci))",
      "sudo chown -R agent:agent /opt/agent",
      "sudo touch /etc/agent.env",
      "sudo chmod 644 /etc/agent.env",
      "echo '[Unit]' | sudo tee /etc/systemd/system/blueprint-agent.service",
      "echo 'Description=Blueprint Agent Service' | sudo tee -a /etc/systemd/system/blueprint-agent.service",
      "echo 'After=network.target' | sudo tee -a /etc/systemd/system/blueprint-agent.service",
      "echo '' | sudo tee -a /etc/systemd/system/blueprint-agent.service",
      "echo '[Service]' | sudo tee -a /etc/systemd/system/blueprint-agent.service",
      "echo 'Type=simple' | sudo tee -a /etc/systemd/system/blueprint-agent.service",
      "echo 'User=agent' | sudo tee -a /etc/systemd/system/blueprint-agent.service",
      "echo 'WorkingDirectory=/opt/agent' | sudo tee -a /etc/systemd/system/blueprint-agent.service",
      "echo 'EnvironmentFile=/etc/agent.env' | sudo tee -a /etc/systemd/system/blueprint-agent.service",
      "echo 'ExecStart=/usr/bin/npm start' | sudo tee -a /etc/systemd/system/blueprint-agent.service",
      "echo 'Restart=no' | sudo tee -a /etc/systemd/system/blueprint-agent.service",
      "echo 'MemoryMax=6G' | sudo tee -a /etc/systemd/system/blueprint-agent.service",
      "echo 'TimeoutStopSec=30' | sudo tee -a /etc/systemd/system/blueprint-agent.service",
      "echo '' | sudo tee -a /etc/systemd/system/blueprint-agent.service",
      "echo '[Install]' | sudo tee -a /etc/systemd/system/blueprint-agent.service",
      "echo 'WantedBy=multi-user.target' | sudo tee -a /etc/systemd/system/blueprint-agent.service",
      "sudo systemctl daemon-reload"
    ]
  }

  provisioner "shell" {
    inline = [
      "sudo mkdir -p /opt/agent-cache/git",
      "# Seed a bare git mirror so cold VMs clone with `git clone --reference-if-able`",
      "# and reuse pre-fetched objects instead of re-downloading the whole repo.",
      "# Path layout matches GIT_REFERENCE_CACHE_ROOT/<owner>/<repo>.git in agent/src/index.ts.",
      "if [ -n '${var.github_token}' ] && [ -n '${var.cache_repo}' ]; then sudo mkdir -p \"/opt/agent-cache/git/$(dirname '${var.cache_repo}')\" && sudo git clone --mirror \"https://x-access-token:${var.github_token}@github.com/${var.cache_repo}.git\" \"/opt/agent-cache/git/${var.cache_repo}.git\" && echo PACKER_GIT_CACHE_OK; else echo 'No github_token/cache_repo; skipping git object cache seed'; fi",
      "# Pre-warm the shared npm cache with the target repo's admin-ui + agent deps",
      "# so task-time `npm ci` is mostly cache hits. Best-effort: a dep-install",
      "# failure here must not fail the build (|| true), the cache still helps partially.",
      "if [ -d \"/opt/agent-cache/git/${var.cache_repo}.git\" ]; then sudo git clone \"/opt/agent-cache/git/${var.cache_repo}.git\" /tmp/cache-warm && (cd /tmp/cache-warm/admin-ui && sudo npm ci || true) && (cd /tmp/cache-warm/agent && sudo npm ci || true) && sudo rm -rf /tmp/cache-warm && echo PACKER_NPM_WARM_OK; fi",
      "# The agent user runs tasks, so it must own the caches it reads and writes.",
      "sudo chown -R agent:agent /opt/agent-cache"
    ]
  }

  provisioner "shell" {
    inline = [
      "# Prefetch Playwright Chromium browser + system libs (--with-deps).",
      "# Using Playwright's canonical dep list (which it maintains upstream)",
      "# instead of pinning a manual apt list — manual lists rot when upstream",
      "# package names change (t64 transition, package renames, etc.). The",
      "# agent user has passwordless sudo, so --with-deps' internal apt-get",
      "# install runs cleanly. Failures are fatal: a broken image must not be",
      "# tagged, as silent failures caused CODE_ERROR regressions at runtime.",
      "# playwright is not an agent dependency, so fetch the CLI via npx; pin the",
      "# version to admin-ui's playwright (keep in sync with check-viewport.sh).",
      "# Run as the agent user (-H sets HOME=/home/agent) from an agent-owned CWD:",
      "# npx spawns a child process, and as the agent user it cannot reach",
      "# /home/packer (the provisioner's default CWD), so that spawn fails EACCES.",
      "sudo -u agent -H bash -c 'cd /opt/agent && npx --yes playwright@1.59.1 install --with-deps chromium'"
    ]
  }

  provisioner "shell" {
    inline = [
      "# Smoke-check: verify Playwright CLI and Chromium binary exist before tagging image.",
      "# Hard-fail here prevents shipping a broken VM that causes CODE_ERROR at runtime.",
      "sudo -u agent -H bash -c 'cd /opt/agent && npx --yes playwright@1.59.1 --version'",
      "# The find runs as the agent user too — the browser cache lives under",
      "# /home/agent and is not readable by the packer provisioner user.",
      "sudo -u agent -H bash -c 'find /home/agent/.cache/ms-playwright -name chrome -type f | grep . && echo PACKER_SMOKE_OK || { echo PACKER_SMOKE_FAIL: Chromium binary not found after playwright install; exit 1; }'"
    ]
  }
}
