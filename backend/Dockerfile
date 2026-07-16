# Multi-stage. The `base` stage holds everything that only changes when
# requirements.txt (or this Dockerfile) changes — the apt + pip layers. CI
# adds test-only environment in `deps`, while the final production `app` stage
# starts from `base` so test credentials can never leak into a deployed image.
FROM python:3.11-slim AS base

WORKDIR /app

# System tools the test suite relies on:
# - shellcheck: lints our dynamically-generated startup scripts and the
#   static scripts/*.sh to catch shell bugs before they hit a VM (see
#   backend/testing/test_generated_shell.py). Adding ~5MB for a check
#   that would have saved hours on the 0bcfd031 debugging loop.
RUN apt-get update && apt-get install -y --no-install-recommends shellcheck && \
    rm -rf /var/lib/apt/lists/*

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt uvicorn

# CI builds and caches this target, then mounts the checked-out source over
# /app. Migration 0609 fails closed without a Fernet key, so the full-chain
# migration check needs an explicit, non-production test key in this image.
FROM base AS deps
ENV GITHUB_PAT_ENCRYPTION_KEY=AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=

# ── Final image: base + application source ──────────────────────────────────
FROM base AS app

# Copy application code
COPY . .

# Expose port (Cloud Run defaults to 8080)
EXPOSE 8080

# Start the production server
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--timeout-graceful-shutdown", "60"]
