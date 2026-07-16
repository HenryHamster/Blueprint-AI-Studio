# Container image for running the Blueprint agent as a one-shot Cloud Run Job
# (read-only / non-merging task types ONLY — see docs/cloud-run-jobs-activation.md).
#
# The agent already supports Cloud Run Job mode at runtime (CLOUD_RUN_JOB=1): it
# claims its task over the orchestrator REST API, resolves credentials from
# Secret Manager via the job service-account token (the Cloud Run metadata server
# exposes .../service-accounts/default/token), runs exactly one role, then exits
# — the Cloud Run Job contract. No agent code changes are required; this image is
# the only missing piece for activating read-only task types on Cloud Run Jobs.
#
# This image deliberately mirrors the READ-ONLY slice of
# infra/packer/agent.pkr.hcl (the GCE VM image) and intentionally OMITS docker,
# the postgres:15 / python:3.11-slim pre-pulls, the backend test-deps bake,
# playwright/chromium, and the pinned pytest stack. Those are only needed by
# heavy/write task types (verify_tests, smoketest, compute_experiment, and the
# create/fix/refactor/migrate/... write types), which MUST NOT be routed to this
# thin image — keep cloud_run_task_type_allowlist to the zero-checkout/read-only
# types until a separate heavier image (with docker-in-docker) exists.
#
# Build (docs/cloud-run-jobs-activation.md step 3):
#   gcloud builds submit agent \
#     --tag us-central1-docker.pkg.dev/$PROJECT_ID/blueprint-repo/blueprint-agent-job:latest

FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive

# Base toolchain — mirrors agent.pkr.hcl:91,123. git: read-only handlers clone
# the target repo (review/audit/etc.); curl + jq: the agent's ambient shell
# helpers and the orchestrator REST PR path (the gh CLI is never used);
# universal-ctags: the code-reading handlers.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        curl git wget gnupg ca-certificates apt-transport-https jq universal-ctags \
    && rm -rf /var/lib/apt/lists/*

# Node.js 20.x via NodeSource — exactly as agent.pkr.hcl:92-93.
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# python3 + ruff for the lint/audit read-only handlers (e.g. security_audit,
# self_audit). The pinned pytest stack from the VM image is deliberately
# excluded — no read-only task type runs pytest.
RUN apt-get update \
    && apt-get install -y --no-install-recommends python3-pip \
    && rm -rf /var/lib/apt/lists/* \
    && pip3 install --no-cache-dir ruff

# LLM provider CLIs — mirrors agent.pkr.hcl. Codex is pinned because model
# compatibility and its platform-native optional package are dispatch-critical;
# an unpinned rebuild or npm configured with omit=optional can leave a PATH shim
# that crashes before inference.
ARG CLAUDE_CODE_VERSION=2.1.197
ARG CODEX_VERSION=0.144.1
ARG GEMINI_VERSION=latest
# The version smoke enters Codex's JS-shim -> native-package startup path
# without an API call. Missing @openai/codex-linux-x64 or drift fails the build.
RUN npm install -g --include=optional \
        "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
        "@openai/codex@${CODEX_VERSION}" \
        "@google/gemini-cli@${GEMINI_VERSION}" \
    && codex_version="$(codex --version | awk '{print $NF}')" \
    && test "${codex_version}" = "${CODEX_VERSION}" \
    && claude_version="$(claude --version | awk '{print $1}')" \
    && test "${claude_version}" = "${CLAUDE_CODE_VERSION}"

# Default git identity for any commits the agent makes — agent.pkr.hcl:118-119.
# --system so it applies regardless of which user the container runs as.
RUN git config --system user.email 'blueprint-agent@automated.dev' \
    && git config --system user.name 'Blueprint Agent'

# Agent application — mirrors agent.pkr.hcl:131-144 + the npm ci at :237.
# package*.json copied before src so an app-code change doesn't bust the npm
# layer. npm ci MUST install devDependencies — `npm start` is `tsx src/index.ts`
# and tsx is a devDependency; do NOT set NODE_ENV=production or pass --omit=dev.
WORKDIR /opt/agent
COPY package.json package-lock.json ./
RUN npm ci
COPY src ./src

# Run as a NON-ROOT user — mandatory, not cosmetic. Claude Code (and codex's
# --dangerously-bypass-approvals-and-sandbox) HARD-REFUSE their permission-skip
# flags under root/sudo: `claude -p ... --dangerously-skip-permissions` exits
# immediately with "--dangerously-skip-permissions cannot be used with root/sudo
# privileges for security reasons" (runners.ts:619,646). The GCE VM image runs
# the agent as user `agent` (agent.pkr.hcl:214,246,255) so the flag is allowed;
# this image previously ran as root, so EVERY LLM run died in ~5s and the
# container still exited 0 (false green). The user name + home MUST be
# `agent` / /home/agent and /opt/agent must be agent-owned: the agent writes LLM
# credentials to hardcoded paths /home/agent/.claude.json and
# /opt/agent/{openai,gemini}/ (index.ts:424-427), and Claude Code reads
# ~/.claude.json via $HOME — so HOME must equal /home/agent for the written
# config and the CLI's read to line up.
RUN useradd -m -s /bin/bash agent \
    && mkdir -p /opt/agent/openai /opt/agent/gemini \
    && chown -R agent:agent /opt/agent /home/agent
ENV HOME=/home/agent
USER agent

# Cloud Run Job entrypoint. The backend injects CLOUD_RUN_JOB=1 plus the
# per-execution env (TASK_ID, AGENT_SECRET_VERSION, ORCHESTRATOR_URL, ...); the
# agent claims one task, runs it, and exits.
CMD ["npm", "start"]
