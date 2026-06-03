# Multi-stage. The `deps` stage holds everything that only changes when
# requirements.txt (or this Dockerfile) changes — the apt + pip layers. The
# final `app` stage adds the source. `docker build` with no --target builds the
# last stage (`app`), so production / Deploy is unchanged. CI builds just the
# `deps` stage (`--target deps`), caches it in GHCR keyed by a deps hash, and
# runs tests by mounting the checked-out source over /app — so the ~8 backend
# shards no longer each rebuild the image from scratch every run.
FROM python:3.11-slim AS deps

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

# ── Final image: deps + application source ──────────────────────────────────
FROM deps AS app

# Copy application code
COPY . .

# Expose port (Cloud Run defaults to 8080)
EXPOSE 8080

# Start the production server
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--timeout-graceful-shutdown", "60"]
