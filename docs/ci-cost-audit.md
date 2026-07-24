# CI Cost Audit — Operator Edit List

**Date:** 2026-06-20
**Scope:** All five `.github/workflows/` files.
**Constraint:** Agent tokens cannot push `.github/workflows/` changes — apply these edits by hand.

Items are ordered by estimated savings (largest first). Each item includes the target
file and job, an apply-ready diff or YAML snippet, an estimated minutes-saved figure
with reasoning, and a risk note.

---

## Item 0 — Cloud Run min-instances: 3 → 1 (deploy.yml)

**File:** `.github/workflows/deploy.yml`
**Job:** `deploy-backend` (line 129)
**Estimated savings:** ~$40–60/month (continuous compute, not per-run minutes)

### Reasoning

This is the largest recurring cost lever in the entire file set — not a CI minutes
saving but a continuous GCP Compute cost.

`deploy.yml` line 129 deploys the Cloud Run service with `--min-instances 3`. Cloud
Run charges for minimum instances **24 hours a day, 7 days a week**, even when
there is zero inbound traffic. At `--min-instances 3`, three full Cloud Run
instances are always allocated:

- Cloud Run CPU/memory pricing for an always-on instance (e2 equivalent, 1 vCPU
  / 512 MB): ~$0.02–0.03/hour per instance
- Three instances × 24 h × 30 days = **2 160 always-on instance-hours/month**
- Rough monthly cost: **~$50–65/month** for idle minimum capacity

Reducing to `--min-instances 1` keeps one warm instance (zero cold-start latency
for the autonomous pipeline) and eliminates **~2 continuous instance-hours per
hour** of waste.

This saving dwarfs every per-run CI minutes item in this document, which are each
measured in cents per month.

### Apply-ready diff

```diff
--- a/.github/workflows/deploy.yml
+++ b/.github/workflows/deploy.yml
@@ -127,7 +127,7 @@ jobs:
           gcloud run deploy blueprint-orchestrator \
             --image "${{ env.AR_REGISTRY }}/${{ env.PROJECT_ID }}/blueprint-repo/${{ env.IMAGE }}:${{ github.sha }}" \
             --region ${{ env.REGION }} \
-            --min-instances 3 \
+            --min-instances 1 \
             --max-instances 12 \
             --concurrency 20 \
             --update-env-vars DEPLOY_SHA=${{ github.sha }}
```

### Risk / caveats

- **Cold starts:** With `--min-instances 1`, traffic spikes that exceed 1 concurrent
  request trigger cold starts for new instances. The autonomous pipeline generates
  steady low-frequency traffic, so a single warm instance is almost always sufficient.
  Monitor p95 latency in Cloud Run metrics after applying.
- **Revert path:** Trivially reversible — change `1` back to `3` in deploy.yml and
  re-deploy.
- **`--min-instances 0`:** Would save the remaining always-on cost entirely but
  causes cold-start delays when the orchestrator is first called after an idle
  period. Not recommended for a polling-based backend.

---

## Status: automain CI removal (already done — no edit needed)

`ci.yml` `on.pull_request.branches` is already `[main]` only. The header comment
(lines 17–27) documents the 2026-06-20 removal of the `automain` trigger that was
burning the bulk of GitHub Actions minutes. **No workflow edit is required here.**

The only residual action is a CLAUDE.md documentation fix (the automain text in the
testing section is stale); that is being handled by a separate sibling PR and is out
of scope for this document.

---

## Item 1 — Backend shard count: 8 → 6 (ci.yml)

**File:** `.github/workflows/ci.yml`
**Job:** `backend` (matrix, lines 339–343 and 470)
**Estimated savings:** ~8 runner-minutes per CI run, ~160–240 runner-minutes/month

### Reasoning

Each shard incurs a fixed startup overhead before any test runs:

| Step | Approx. time |
|------|-------------|
| Checkout | ~10 s |
| Pull postgres:15 (with retry/mirror logic) | ~30–50 s |
| Pull/build backend deps image from GHCR | ~20–30 s |
| `alembic upgrade head` | ~15–20 s |
| `python -c 'from app.main import app...'` import check | ~5 s |
| **Total fixed overhead per shard** | **~80–115 s ≈ 100 s** |

The committed `backend/.test_durations` file maps pytest node IDs to per-test
wall-clock durations and is version-controlled so CI shards can auto-detect it and
switch from count-based to time-based splitting. It is regenerated via:

```bash
bash scripts/store-backend-durations.sh
git add backend/.test_durations && git commit -m "chore: refresh test duration baselines"
```

A freshness guard in `backend/testing/test_audit_finding_class_guards.py`
(`TestTestDurationsCommittedAndFresh`) fails CI if the file is missing, malformed,
too sparse, or its sampled node-ID keys point at deleted test files. GitHub Actions
bills each job rounded up to the next whole minute.

| Shards | Tests/shard | Total/shard | Billed/shard | **Total billed** | Wall clock |
|--------|------------|-------------|--------------|-----------------|------------|
| 8 (current) | 112 s | 212 s → 3.5 min | **4 min** | **32 min** | ~3.5 min |
| 6 | 149 s | 249 s → 4.1 min | **5 min** | **30 min** | ~4.1 min |
| 4 | 223 s | 323 s → 5.4 min | **6 min** | **24 min** | ~5.4 min |

**6 shards saves about 2 runner-minutes per run vs. 8 shards, with only ~35 s of
added wall-clock latency.** 4 shards saves about 8 billed minutes but adds ~1.9 min
wall time; present both as operator options. At ~20–30 main-targeting PRs per
month, the monthly saving is ~40–60 runner-minutes for 6 shards or ~160–240
runner-minutes for 4 shards (~$0.30–$1.90/month at Linux rates).

### Apply-ready diff

```diff
--- a/.github/workflows/ci.yml
+++ b/.github/workflows/ci.yml
@@ -337,11 +337,11 @@ jobs:
-    # Sharded: the suite (~7k tests, ~11.5 min serial) is split into 8 parallel
+    # Sharded: the suite (~16k tests, ~15 min serial) is split into 8 parallel
-    # legs via pytest-split (--splits 8 --group N). Each leg is an ordinary
+    # legs via pytest-split (--splits 6 --group N). Each leg is an ordinary
     strategy:
       fail-fast: false
       matrix:
-        shard: [1, 2, 3, 4, 5, 6, 7, 8]
+        shard: [1, 2, 3, 4, 5, 6]

@@ -467,7 +467,7 @@ jobs:
           docker run --rm --network host -v "$PWD/backend:/app" \
             -e DATABASE_URL=${{ env.TEST_DB_URL }} \
             -e USE_TEST_POSTGRES=true \
             -e AGENT_JWT_SECRET=ci-test-secret \
             -e WS_TICKET_SECRET=ci-test-secret \
             ${{ steps.deps.outputs.image }} \
-            python -m pytest testing/ --splits 8 --group ${{ matrix.shard }} \
+            python -m pytest testing/ --splits 6 --group ${{ matrix.shard }} \
               -v --tb=short --timeout=30 --timeout-method=signal --log-cli-level=WARNING 2>&1 | tee test-output.txt
```

Also update the display name in the `Run tests` step name (line ~451) for clarity:

```diff
-      - name: Run tests (shard ${{ matrix.shard }}/8)
+      - name: Run tests (shard ${{ matrix.shard }}/6)
```

**If reducing to 4 shards instead:** substitute `[1, 2, 3, 4]`, `--splits 4`, and `/4` above.

### Risk / caveats

- **Test distribution:** pytest-split auto-detects `backend/.test_durations` and rebalances
  by time. If that file becomes stale or is absent, it falls back to even count-based splits
  (graceful). No shard isolation problems — each shard runs its own postgres. Regenerate
  when the freshness guard fails: `bash scripts/store-backend-durations.sh`.
- **Wall time:** 6 shards adds ~35 s to the critical path (4.1 min vs. 3.5 min). Unlikely
  to matter for the autonomous pipeline.

---

## Item 2 — Docker buildx layer cache for production backend build (deploy.yml)

**File:** `.github/workflows/deploy.yml`
**Job:** `build-backend` (lines 64–92)
**Estimated savings:** ~1–3 runner-minutes per deploy, ~20–60 runner-minutes/month

### Reasoning

The current `build-backend` job runs a plain `docker build` with no `--cache-from`:

```yaml
docker build -t "${IMAGE_URI}:${{ github.sha }}" .
docker push "${IMAGE_URI}:${{ github.sha }}"
```

The backend `Dockerfile` has at least two expensive layers:
1. `pip install` of all Python dependencies (~60–90 s)
2. `COPY . /app` of application source (~10 s)

When only Python source files change (the common case — `requirements.txt` is
stable between releases), layers up to the `COPY` step are identical. With a
buildx GHA layer cache, those layers are restored from the Actions cache instead
of rebuilt, saving ~1–2 min per deploy.

At ~20 main-branch pushes per month, this saves 20–60 runner-minutes (~$0.16–$0.48/month).
The absolute dollar saving is small; the value is faster deploys.

### Assessment: ci.yml `backend-deps` — no change needed

The `backend-deps` job in ci.yml already implements an equivalent pattern using
GHCR as a persistent layer cache: it builds a `--target deps` image tagged by
`sha256(requirements.txt + Dockerfile)` and pushes it to GHCR, where it is
shared across all PRs. GHCR is not subject to the 10 GB per-repo Actions cache
limit, and the cache persists indefinitely. This is already an efficient strategy
and does not need buildx GHA cache.

### Apply-ready snippet (deploy.yml `build-backend` job)

Replace the two existing steps ("Build + push" inline shell) with:

```yaml
      - name: Set up Docker Buildx
        uses: docker/setup-buildx-action@v3

      - name: Build and push
        uses: docker/build-push-action@v6
        with:
          context: ./backend
          push: true
          tags: ${{ env.AR_REGISTRY }}/${{ env.PROJECT_ID }}/blueprint-repo/${{ env.IMAGE }}:${{ github.sha }}
          cache-from: type=gha
          cache-to: type=gha,mode=max
```

The full replacement in context:

```diff
--- a/.github/workflows/deploy.yml
+++ b/.github/workflows/deploy.yml
@@ -83,9 +83,16 @@ jobs:
       - name: Configure Docker
         run: gcloud auth configure-docker ${{ env.AR_REGISTRY }} --quiet
 
-      - name: Build and push
-        working-directory: ./backend
-        run: |
-          IMAGE_URI="${{ env.AR_REGISTRY }}/${{ env.PROJECT_ID }}/blueprint-repo/${{ env.IMAGE }}"
-          docker build -t "${IMAGE_URI}:${{ github.sha }}" .
-          docker push "${IMAGE_URI}:${{ github.sha }}"
+      - name: Set up Docker Buildx
+        uses: docker/setup-buildx-action@v3
+
+      - name: Build and push
+        uses: docker/build-push-action@v6
+        with:
+          context: ./backend
+          push: true
+          tags: ${{ env.AR_REGISTRY }}/${{ env.PROJECT_ID }}/blueprint-repo/${{ env.IMAGE }}:${{ github.sha }}
+          cache-from: type=gha
+          cache-to: type=gha,mode=max
```

### Risk / caveats

- **GHA cache quota:** The 10 GB per-repo limit is shared with all other `actions/cache`
  entries. A full production Docker image with deps layers could be 1–3 GB compressed.
  Monitor usage at *Settings → Actions → Caches* after enabling.
- **Cache scope:** GHA cache is branch-scoped for writes; reads fall back to the default
  branch. The first run on a new branch will miss the layer cache and pay the full build
  time.
- **Alternative:** Using the Artifact Registry image itself as a cache source avoids the
  10 GB limit entirely:
  ```yaml
  cache-from: type=registry,ref=${{ env.AR_REGISTRY }}/${{ env.PROJECT_ID }}/blueprint-repo/${{ env.IMAGE }}:cache
  cache-to: type=registry,ref=${{ env.AR_REGISTRY }}/${{ env.PROJECT_ID }}/blueprint-repo/${{ env.IMAGE }}:cache,mode=max
  ```
  This requires no additional GHA quota but does consume Artifact Registry storage.
- **`docker/build-push-action`** requires Buildx (automatically installed on GitHub-hosted
  `ubuntu-latest` runners since mid-2023). The `setup-buildx-action` step is included
  for forward-compatibility.

---

## Item 3 — Playwright browser cache (ci.yml)

**File:** `.github/workflows/ci.yml`
**Job:** `frontend-viewport` (lines 151–224)
**Estimated savings:** ~1 runner-minute per frontend-touching PR on cache hit

### Reasoning

The `frontend-viewport` job currently runs:
```yaml
npx playwright install --with-deps chromium
```

`--with-deps` downloads both the ~100 MB Chromium binary and installs apt packages
(libatk, libnss, libxkbcommon, etc.). The binary download takes ~30–60 s on a fresh
runner; the apt installs take another ~20–30 s.

Only the binary can be cached (it lands in `~/.cache/ms-playwright`). The apt packages
must be installed fresh every run because GitHub-hosted runners start with a clean OS
image. Splitting the step lets the binary serve from cache on repeat runs while apt
installs remain unconditional.

The installed Playwright version resolved in `admin-ui/package-lock.json` is `1.60.0`.
Use that exact version in the cache key so a lockfile update (which changes the
resolved version) automatically busts the cache.

On a cache hit this step drops from ~60–90 s to ~5–10 s, saving ~1 min. At ~20–30
frontend-touching PRs per month: 20–30 runner-minutes/month.

### Apply-ready diff

```diff
--- a/.github/workflows/ci.yml
+++ b/.github/workflows/ci.yml
@@ -169,14 +169,22 @@ jobs:
-      - name: Install Playwright Chromium + system deps
-        # Required for the viewport suite (vitest browser-mode + chromium).
-        # Agent VMs lack libatk/libnss/libxkbcommon by default; CI gets a
-        # clean ubuntu-latest each run so we install on every job. Retry the
-        # Docker Hub / apt download to absorb transient registry blips.
-        working-directory: ./admin-ui
-        run: |
-          for i in 1 2 3; do
-            npx playwright install --with-deps chromium && exit 0
-            echo "::warning::playwright install failed (attempt $i/3) — retrying"
-            sleep $((i * 10))
-          done
-          echo "::error::playwright install failed after 3 attempts"
-          exit 1
+      - name: Cache Playwright Chromium browser
+        uses: actions/cache@v4
+        with:
+          path: ~/.cache/ms-playwright
+          # Key on the exact resolved version from package-lock.json.
+          # Update this string when upgrading Playwright.
+          key: playwright-chromium-1.60.0
+
+      - name: Install Playwright Chromium browser
+        # Downloads the ~100 MB binary only on a cache miss; reads from
+        # ~/.cache/ms-playwright on a hit. Retries absorb transient network blips.
+        working-directory: ./admin-ui
+        run: |
+          for i in 1 2 3; do
+            npx playwright install chromium && exit 0
+            echo "::warning::playwright install failed (attempt $i/3) — retrying"
+            sleep $((i * 10))
+          done
+          echo "::error::playwright install failed after 3 attempts"
+          exit 1
+
+      - name: Install Playwright system dependencies
+        # libatk/libnss/libxkbcommon — must run every job; cannot be cached.
+        working-directory: ./admin-ui
+        run: npx playwright install-deps chromium
```

### Risk / caveats

- **Cache invalidation:** The key is hardcoded to `1.60.0`. When `package-lock.json`
  upgrades Playwright, update the key string in the workflow to match the new resolved
  version, or use a computed key:
  ```yaml
  key: playwright-chromium-${{ hashFiles('admin-ui/package-lock.json') }}
  ```
  The hash-based key auto-invalidates on any lockfile change (including unrelated dep
  bumps); the version-string key is more surgical but requires a manual update.
- **`install-deps` must remain unconditional** — apt packages are not in
  `~/.cache/ms-playwright`, so omitting `install-deps` on a cache hit causes the
  browser to fail to launch.
- **Cache size:** The Chromium browser archive is ~300–400 MB uncompressed, ~100 MB
  compressed. Well within the 10 GB per-repo GHA cache limit.

---

## Item 4 — Firebase CLI install cache (deploy.yml)

**File:** `.github/workflows/deploy.yml`
**Job:** `deploy-frontend` (lines 177–217)
**Estimated savings:** ~0.5–1 runner-minute per frontend deploy, ~5–15 runner-minutes/month

### Reasoning

The `deploy-frontend` job runs `npm install -g firebase-tools@13` on every push to
main that touches `admin-ui/**`. This downloads ~70 MB of packages from npm and
installs them globally, taking ~30–60 s with a cold npm cache.

`actions/setup-node` with `cache: 'npm'` already restores `~/.npm` (the npm tarball
cache) keyed on `admin-ui/package-lock.json`. Because `firebase-tools` is not in
`package-lock.json`, its tarball is not included in that cache and is re-downloaded
every run.

Adding a separate cache step that includes `~/.npm` under a firebase-specific key
allows the `npm install -g` command to resolve the tarball from the local cache
instead of the registry, reducing the step to ~5–10 s.

### Apply-ready snippet

Insert the cache restore step immediately before the `Deploy to Firebase Hosting` step:

```diff
--- a/.github/workflows/deploy.yml
+++ b/.github/workflows/deploy.yml
@@ -211,6 +211,12 @@ jobs:
 
+      - name: Restore Firebase CLI npm cache
+        uses: actions/cache@v4
+        with:
+          path: ~/.npm
+          key: ${{ runner.os }}-npm-firebase-tools-13
+          restore-keys: ${{ runner.os }}-npm-firebase-tools-
+
       - name: Deploy to Firebase Hosting
         run: |
           npm install -g firebase-tools@13
```

### Risk / caveats

- **Cache key vs. minor version:** The key `firebase-tools-13` is major-version scoped.
  A `firebase-tools@13.x` patch bump is installed once and then cached. If you want to
  guarantee the latest 13.x on every run, omit the cache step (current behaviour) or
  adopt a weekly `restore-keys` sweep strategy.
- **Overlap with setup-node cache:** Both this step and `actions/setup-node` target
  `~/.npm`. Actions cache restore/save at different keys do not conflict — they write
  separate cache entries. On save, each key is independent. Net effect: two separate
  cache entries for `~/.npm`, one for package-lock deps and one for firebase-tools.
- **Alternative (cleanest):** Add `firebase-tools` to `admin-ui/package.json`
  `devDependencies`. Then `npm ci` installs it (covered by the existing setup-node
  npm cache) and the deploy step becomes `npx firebase ...`. Tradeoff: firebase-tools
  is ~200 MB; it inflates node_modules and the npm ci step slightly.

---

## GitHub Actions Billing Mechanics Reference

Use this to sanity-check the estimates above.

### Per-job minute rounding

GitHub bills each **job** (not each step) rounded up to the **next whole minute**.
A job that runs 61 s is billed as **2 minutes**. A job that runs 119 s is also billed
as **2 minutes**. A job that runs 120 s is billed as **2 minutes** exactly.

Consequence: when a job's runtime lands just above a minute boundary, shaving
30 s of work drops a full billed minute. When it lands just below, shaving 30 s
saves nothing. The shard count reduction (Item 1) exploits a boundary: at 8 shards
each shard is ~205 s (3.4 min → billed 4 min); at 6 shards each is ~240 s (4.0 min →
still billed 4 min), so no per-shard regression — but 2 fewer shards × 4 min = 8 min
saved per run.

### Linux runner rate

GitHub-hosted `ubuntu-latest` runners: **$0.008 per billed minute** (as of 2025).
Free-tier minutes (2 000 free/month on Team plans, 3 000 on Enterprise) apply first.

### Why fixed per-job overhead dominates short jobs

Every GitHub Actions job pays fixed costs before user steps run:
- Runner acquisition and initialization: ~5–10 s (not billed, but adds latency)
- `actions/checkout`: ~5–15 s
- Docker pull (postgres, base images): 20–60 s
- Image build or cache restore: 10–60 s

For a job whose test payload is 60–120 s, fixed overhead at ~100 s is 45–63% of
total runtime. Adding shards splits the test payload but multiplies the overhead.
The optimal shard count is where `ceil((overhead + test_time/N) / 60)` × N × rate
is minimized — which is where shards are not so many that per-job overhead dominates,
and not so few that the slowest shard limits wall time unacceptably.

### Savings unknowable without live billing data

The following figures are estimates based on step timing from workflow comments and
playbook notes. The operator should calibrate against the actual billing dashboard:

- PR frequency targeting main (affects Items 1 and 3)
- Frontend deploy frequency (affects Items 2 and 4)
- Actual per-step times (run a job with step-level timestamps enabled:
  add `env: ACTIONS_STEP_DEBUG: true` to any job to see per-step durations in the log)

The per-step timing data from a single real run is more reliable than any estimate
in this document.

---

## Item 5 — Remove terraform push trigger with no apply (terraform.yml)

**File:** `.github/workflows/terraform.yml`
**Job:** `terraform` (lines 3–8 trigger, lines 59–63 plan, lines 80–87 apply)
**Estimated savings:** ~2–3 runner-minutes per infra push to main, ~0–15 runner-minutes/month

### Reasoning

`terraform.yml` declares a `push` trigger on `main` (lines 3–8) that fires whenever
`infra/terraform/**` files land on the main branch. The workflow runs `terraform plan`
(lines 59–63) but the Apply step is permanently commented out (lines 80–87) with the
explicit note: `Apply is disabled until a remote backend (GCS) is configured.`

This means every merge of a terraform file to main pays a full job cost — checkout,
`terraform init`, `terraform plan` (which calls GCP APIs to refresh state) — for a
plan that **can never be applied** by this workflow. The plan output is displayed in
the job log and immediately discarded. The `pull_request` trigger already runs the
same plan for review purposes before the merge, so the post-merge push plan is
entirely redundant.

Removing the `push` trigger from the `on:` block eliminates these zero-ROI runs.
The `pull_request` and `workflow_dispatch` triggers should be kept: the former
validates infra changes in PRs, and the latter provides an operator escape hatch
to run a plan manually.

### Apply-ready diff

```diff
--- a/.github/workflows/terraform.yml
+++ b/.github/workflows/terraform.yml
@@ -1,10 +1,6 @@
 name: Terraform Infrastructure

 on:
-  push:
-    branches: [main]
-    paths:
-      - 'infra/terraform/**'
   pull_request:
     paths:
       - 'infra/terraform/**'
```

### Risk / caveats

- **No loss of plan visibility:** The `pull_request` trigger already runs `terraform
  plan` and posts a comment on the PR before merge. Removing the `push` trigger
  loses nothing from the code-review workflow.
- **Apply is still blocked:** Removing the push trigger does not enable or disable
  Apply — that step is commented out regardless. The only path to Apply today is
  `workflow_dispatch` with `action: apply`.
- **When to re-add:** Once a GCS remote backend is configured and Apply is
  uncommented, re-evaluate whether a post-merge `push` trigger makes sense. At that
  point a push-triggered Apply (not just Plan) would be meaningful.
