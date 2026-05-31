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
# Rebuild trigger 2026-05-28: pipeline default Opus bumped claude-opus-4-7 -> claude-opus-4-8.
# The @anthropic-ai/claude-code install below is unpinned, so this rebuild bakes the current
# CLI that recognizes `--model claude-opus-4-8`. Bake + deploy this image BEFORE flipping the
# live runtime_config to opus-4-8 — otherwise every Anthropic task fails on an unknown --model.
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
      "sudo pip3 install ruff pytest pytest-asyncio pytest-mock pytest-timeout",
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
      "# Compute a stable content-hash fingerprint of the baked agent source so",
      "# heartbeats and stale-image diagnostics can identify exactly which runtime",
      "# revision is running, independent of image name or build timestamp.",
      "AGENT_FINGERPRINT=$(find /opt/agent/src -type f | LC_ALL=C sort | xargs sha256sum | sha256sum | awk '{print $1}') && echo \"AGENT_RUNTIME_FINGERPRINT=$AGENT_FINGERPRINT\" | sudo tee /opt/agent/AGENT_RUNTIME_FINGERPRINT && echo PACKER_FINGERPRINT_OK",
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
      "# Actually launch the installed chrome-headless-shell so a missing shared",
      "# lib (the failure --with-deps guards against) fails the bake here instead",
      "# of at task time. Runs as the agent user (the browser cache lives under",
      "# /home/agent). We exec the binary directly rather than `import 'playwright'`",
      "# — playwright is intentionally not an agent dependency, so a bare ESM import",
      "# can't resolve from /opt/agent.",
      "sudo -u agent -H bash -c 'BIN=$(find /home/agent/.cache/ms-playwright -type f -name chrome-headless-shell | head -n1); if [ -z \"$BIN\" ]; then echo PACKER_CHROMIUM_LAUNCH_FAIL_no_binary; exit 1; fi; \"$BIN\" --no-sandbox --disable-gpu --dump-dom about:blank >/dev/null 2>&1 && echo PACKER_CHROMIUM_LAUNCH_OK || { echo PACKER_CHROMIUM_LAUNCH_FAIL_launch; exit 1; }'",
      "# Exercise the production readAgentRuntimeFingerprint() reader against the",
      "# file written earlier in the bake. Catches drift between the write line in",
      "# this HCL and the strict regex in agent/src/index.ts before the image tags.",
      "sudo -u agent -H bash -c 'cd /opt/agent && node --import tsx ./src/fingerprint-bake-smoke.ts && echo PACKER_FINGERPRINT_READBACK_OK'",
      "# Exercise the shipped memo handler through the runtime TS entrypoint with a",
      "# deterministic dispatcher memo payload and a localhost fetch stub.",
      "sudo -u agent -H bash -lc 'cd /opt/agent && ORCHESTRATOR_URL=http://127.0.0.1:9 AGENT_TOKEN=packer-smoke node --import tsx ./src/handlers/memo-bake-smoke.ts && echo PACKER_MEMO_SMOKE_OK'",
      "# Second memo smoke: exercise the runClaudeCode + Codex JSONL extractor",
      "# path that the dispatcher-synth short-circuit above deliberately skips.",
      "# The smoke stubs runClaudeCode through the __setRunClaudeCodeOverrideForSmoke",
      "# hook in src/index.ts so no real Claude/Codex subprocess is spawned at",
      "# bake time, and feeds the handler a Codex item.completed JSONL envelope",
      "# whose item.text is a >200-char memo JSON — locking in the regression",
      "# fix for the historical Codex 200-char item.text truncation bug. The",
      "# smoke prints PACKER_MEMO_CLAUDE_PATH_OK on success; on failure it throws",
      "# (non-zero exit) and the bake fails before the image is tagged.",
      "sudo -u agent -H bash -lc 'cd /opt/agent && ORCHESTRATOR_URL=http://127.0.0.1:9 AGENT_TOKEN=packer-smoke node --import tsx ./src/handlers/memo-bake-smoke-codex.ts'",
      "# Third memo smoke: exercise the investigation_unactionable arm of the",
      "# dispatcher synth short-circuit in handlers/memo.ts. The original",
      "# memo-bake-smoke.ts only covers regression_false_positive; this one",
      "# covers the sibling branch (root_cause / recommended_fix / summary /",
      "# outcome) emitted by backend/app/task_completion.py in production.",
      "# Prints PACKER_MEMO_INVESTIGATION_OK on success; on failure it throws",
      "# (non-zero exit) and the bake fails before the image is tagged.",
      "sudo -u agent -H bash -lc 'cd /opt/agent && ORCHESTRATOR_URL=http://127.0.0.1:9 AGENT_TOKEN=packer-smoke node --import tsx ./src/handlers/memo-bake-smoke-investigation.ts && echo PACKER_MEMO_INVESTIGATION_OK'",
      "# Fourth memo smoke: exercise the runClaudeCode + lastResultText primary",
      "# extractor source — the most common production outcome where memo JSON",
      "# arrives clean in lastResultText and the handler's first extractor",
      "# source wins without ever touching allOutput or the Codex envelope",
      "# fallback. The smoke stubs runClaudeCode through the",
      "# __setRunClaudeCodeOverrideForSmoke hook in src/index.ts so no real",
      "# Claude/Codex subprocess is spawned at bake time. It prints",
      "# PACKER_MEMO_PRIMARY_PATH_OK on success; on failure it throws",
      "# (non-zero exit) and the bake fails before the image is tagged.",
      "sudo -u agent -H bash -lc 'cd /opt/agent && ORCHESTRATOR_URL=http://127.0.0.1:9 AGENT_TOKEN=packer-smoke node --import tsx ./src/handlers/memo-bake-smoke-primary.ts'",
      "# Fourth memo smoke: exercise the reopen continuation retry path that fires",
      "# when the first runClaudeCode call returns exitCode=0 but no parseable",
      "# memo JSON. The single-shot codex/primary smokes above cannot reach this branch.",
      "# The smoke uses a call-counting override: call 0 returns non-JSON narrative",
      "# (triggering the reopen branch at memo.ts:202), call 1 returns valid memo",
      "# JSON containing a distinct sentinel that the fetch stub asserts appears",
      "# in the COMPLETED logs. Prints PACKER_MEMO_REOPEN_RETRY_OK on success.",
      "sudo -u agent -H bash -lc 'cd /opt/agent && ORCHESTRATOR_URL=http://127.0.0.1:9 AGENT_TOKEN=packer-smoke node --import tsx ./src/handlers/memo-bake-smoke-reopen.ts'",
      "# Fifth memo smoke: exercise the full agent claim → dispatch → complete",
      "# lifecycle for a memo task. Unlike the per-handler smokes above, this smoke",
      "# invokes the main agent entry point (src/index.ts) via a thin wrapper that",
      "# spins up a local HTTP server stubbing /api/agent/claim (returns a memo task",
      "# fixture), /api/agent/status (asserts COMPLETED), /api/agent/heartbeat (200",
      "# ok), and /api/agent/github/token. This validates that the dispatcher routing,",
      "# handler registration, and end-to-end status post chain are intact in the",
      "# baked image. A rename of the 'memo' task_type constant, a missing handler",
      "# registration, or a broken dispatch path would cause a non-zero exit and fail",
      "# the bake before the image is tagged. Prints PACKER_MEMO_LIFECYCLE_OK on success.",
      "sudo -u agent -H bash -lc 'cd /opt/agent && node --import tsx ./src/handlers/memo-bake-smoke-lifecycle.ts'"
    ]
  }
}
