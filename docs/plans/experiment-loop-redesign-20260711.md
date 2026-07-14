# Experiment-Loop Redesign — Full Design (2026-07-11)

**Context.** Readiness audit for RL witness projects (soft-gripper) proved the execution substrate end-to-end (MJX 95k env-steps/s on L40S, PPO solved in 57s, EGL+OSMesa rendering, artifact retrieval) but identified six structural weak points in how agents set up, iterate on, improve, and report experiments. Six parallel design agents each verified current code state and produced the sections below; a cross-cutting critique (conflicts, gaps, sequencing, priorities) follows them.

**Operator decisions baked in as requirements:** (1) runner enrollment configures a per-runner project ALLOW-LIST; (2) long-run waste prevention ships BEFORE/WITH checkpoint-resume (no-progress watchdog); (3) images-first, digest-pinned project-baked images, NO platform credentials in image layers; (4) no hardcoded ship/cull rubrics — LLM-judged with numbers as evidence; (5) surface REASONS not just states, everywhere.


---

All claims verified against code. Composing the design section.

## Hardware-Aware Authoring & Capability-Based Runner Dispatch

### Problem
Agents author compute experiments blind to the fleet's actual hardware, and dispatch pins jobs to runners that cannot fulfill them. An agent cannot know that dgx-1 has 8×V100-16GB (so a 24GiB-VRAM job can never fit) or that a runner lacks a container runtime (so its `container_image` will be silently discarded). Eligibility reduces every accelerator request to a `has_gpu` boolean, and LLM-runtime selection hands codex credentials to runners whose codex CLI is absent or stale. Failures surface as runtime crashes, silent downgrades, or "Model metadata not found" — never as machine-readable pre-flight declines.

### Current state (verified)
1. **Authoring context is counts-only.** `_load_local_runner_status` (`backend/app/strategic_local_runner.py:39-83`) returns exactly `{ever_registered, active_total, active_gpu}`; `active_gpu` is the `capabilities.has_gpu` truthiness count (line 77). `strategic_spawning.py:7391-7399` injects this (plus `require_local_runner`) into the strategist context as `local_runner_status`. No GPU models, VRAM, device counts, or substrates reach any prompt.
2. **Eligibility is a boolean.** `_select_eligible_runner` (`backend/app/orchestrator_runner_dispatch.py:547-597`) filters on task-type allowlist, capacity, and — when any `accelerator` string is present — `(r.capabilities or {}).get("has_gpu")` (lines 577-578). "T4" vs "V100" vs "L40S" are indistinguishable; no VRAM fit, no device-model match, no container check. When `eligible` is empty it returns `None` with no reason recorded.
3. **Heartbeat already advertises rich data that dispatch ignores.** `scripts/local_runner.py:1332-1352` sends `isolation_backends` (incl. `docker_cdi_gpu`, via `capabilities_inventory`, `scripts/runner_isolation.py:543-549`), `agent_backend`, `compute_backend`, `gpu_names`, `gpu_count`, `gpu_compute_capabilities`, `memory_free_bytes` (host RAM — **not** VRAM; per-GPU VRAM is advertised nowhere, briefing slightly stale here), `disk_free_bytes`, `gpu_utilization_pct`. The backend persists these into `capabilities` (`backend/app/routers/runner.py:176-192, 528-557`). Only `has_gpu` and `task_types` drive dispatch.
4. **Image fulfillability is not an eligibility criterion, and downgrade is silent by design.** `decide_compute_backend` (`runner_isolation.py:503-517`): image + no container runtime + bwrap → run bare on the host toolchain. `local_runner.py:2100-2104` sets `image_downgraded` and the note lands only in `command_log` (lines 2240-2248). `compute_decline_reason` (`runner_isolation.py:520-529`) exists but fires only when *no* backend at all can be built.
5. **CLI inventory is not advertised or filtered.** `/next-task` (`backend/app/routers/runner.py:272`) calls `select_llm_runtime_for_task` with `user_id/task/runtime_config` only — the runner's installed CLIs never enter selection, so codex accounts get pinned to runners whose CLI can't run the selected model.

### Proposed design
**A. Inventory surface (heartbeat → authoring).**
- Heartbeat additions (all optional; backend tolerates absence, matching the existing schema style at `routers/runner.py:172-176`): `gpu_memory_total_mib: list[int]` and `gpu_memory_free_mib: list[int]` (from `nvidia-smi --query-gpu=memory.total,memory.free`), `cli_inventory: dict[str, str|null]` (e.g. `{"claude": "2.1.0", "codex": "0.48.0", "gemini": null}`, probed at startup + on auto-update). Persist into `capabilities` like the other fields.
- Extend `_load_local_runner_status` **additively** (the 3-key shape is load-bearing per the comment at `strategic_spawning.py:7397` and tests in `test_local_runner.py:4340+`): add `devices: list[{runner_name, gpu_count, gpu_models, gpu_vram_mib, container_ok, gpu_in_container_ok, compute_backend, active_work}]`, derived from live runners' capabilities (`container_ok` = any of `isolation_backends.{docker,podman,apptainer,enroot}_*`; `gpu_in_container_ok` = `docker_cdi_gpu`). Render `devices` in the strategist/authoring context block so agents can reason "8×V100 16GiB, containers OK" and author `min_vram_mib`/batch sizes accordingly.

**B. Accelerator vocabulary for operator hardware.**
- Keep `accelerator` as-is for cloud. Add optional launch-contract fields: `min_vram_mib: int`, `min_gpu_count: int` (validated in `compute_experiment_contract` authoring + `_build_compute_launch_kwargs` passthrough).
- A small static table `ACCELERATOR_CLASSES = {"T4": 16384, "V100": 16384, "L4": 24576, "A100": 40960, "L40S": 46068, "H100": 81920, ...}` maps cloud vocab to a **VRAM requirement floor**. On operator dispatch, `accelerator="A100"` means "≥40GiB VRAM per GPU", satisfied by an L40S — requirement semantics, not model-name equality. Unknown token → current `has_gpu`-only behavior (fail open, never strand).

**C. Eligibility redesign (`_select_eligible_runner`).** Return `(runner | None, declines: dict[str, list[runner_id]])` and apply, per candidate:
- `vram_insufficient`: `max(min_vram_mib, ACCELERATOR_CLASSES.get(accelerator, 0))` > runner's `min(gpu_memory_total_mib)`; skip check when runner doesn't advertise VRAM (old runner → permissive).
- `gpu_count_insufficient`: `min_gpu_count > gpu_count`.
- `no_container_runtime`: payload has `container_image` but no usable container runtime advertised.
- `no_gpu_in_container`: `container_image` + accelerator but `docker_cdi_gpu` false (and no other GPU-capable runtime).
- `cli_missing:<name>`: agent tasks only — see D.
When selection yields `None` with a non-empty declines map, stamp `task.context["runner_dispatch_declines"] = {reason: [runner_ids], "at": ts}` (and the compute-job equivalent) and include it in `/api/admin/tasks` output — reasons, not just states. Existing cloud-fallback behavior is unchanged: filtered-out means fall through to GCP exactly as an empty fleet does today.

**D. CLI-aware LLM-runtime selection.** `/next-task` and `_try_runner_dispatch` pass the pinned runner's `cli_inventory` into `select_llm_runtime_for_task` as a new optional `runner_cli_inventory` kwarg (`backend/app/max_plan_pool.py:2559`). Selection excludes providers whose CLI is absent (`codex` missing → skip openai codex accounts) and re-runs its existing fallback chain; when inventory is absent (old runner / cloud VM), behavior is byte-identical. Optional later: minimum-version pins per provider in runtime config (`runner_cli_min_versions`) to catch the stale-codex-CLI class.

**E. Downgrade becomes an explicit, reported decision.** New runtime-config key `compute_image_downgrade_policy: "allow" | "decline"` delivered per job (default `allow` = today). Under `decline`, the runner refuses at claim/pre-flight with `compute_decline_reason`-style structured payload (`{declined: true, reason: "image_unfulfillable", detail}`) via the existing job-status endpoint, and the orchestrator requeues (next runner or cloud). Independently: always copy `image_downgraded` out of `command_log` into a first-class job field (`ComputeJob.substrate_note` or `result_meta`) so downgrades are queryable.

### Rollout phases
1. **Advertise** (dark): heartbeat VRAM + `cli_inventory`; backend persists. No behavior change. Ships alone.
2. **Authoring surface**: `devices` in `_load_local_runner_status` + prompt block. Informational only.
3. **Shadow eligibility**: compute declines map + `runner_dispatch_declines` stamping while filters run in log-only mode (flag `capability_dispatch_enabled=false`); flip after a week of shadow data shows no false filtering.
4. **Downgrade reporting → decline**: first-class `image_downgraded` field (ship immediately), then `compute_image_downgrade_policy=decline` flip per project.
5. **CLI-aware runtime selection**: `runner_cli_inventory` kwarg + filter, permissive when absent.

### Risks & mitigations
- **Old runners lack new fields → false ineligibility.** Every new filter is skip-when-absent; only `has_gpu` remains mandatory. Shadow mode (phase 3) validates before enforcement.
- **Over-filtering strands `require_local_runner` compute jobs** (no cloud fallback). Declines map makes the wedge diagnosable; when *all* live runners decline for the same reason, surface it on the task and in strategist context rather than looping.
- **VRAM table drift / MIG / partial GPUs.** Floor semantics + fail-open on unknown tokens; table is additive-only config, not code.
- **CLI probe staleness after auto-update.** Re-probe inventory post-update (the auto-update hook at `local_runner.py:1357+` already re-enters heartbeat with fresh caps).
- **Return-shape churn in `_select_eligible_runner`** (two call sites, lines 360/803): change both in one commit; declines param defaults keep tests passing.

### Effort estimate
- Phase 1: **S** (nvidia-smi query + probe + 2 schema fields)
- Phase 2: **S**
- Phase 3: **M** (filters + reason plumbing + admin surfacing + shadow flag)
- Phase 4: **M** (runner decline path + orchestrator requeue handling)
- Phase 5: **M** (selection kwarg touches a heavily-tested function; permissive default limits blast radius)

Briefing corrections: heartbeat `memory_free_bytes` is host RAM, not GPU memory — per-GPU VRAM is not currently collected anywhere and is new work in phase 1; the image downgrade is a documented policy choice in `decide_compute_backend` (`runner_isolation.py:508`), not an accidental bug, but its only trace is `command_log` (`local_runner.py:2240-2248`).

---

All claims verified against the code. Notable briefing corrections: a platform pre-flight smoke already exists and is quite complete — but it is **explicitly skipped for the runner-subprocess backend**, which is exactly where GPU experiments run, so the brax/jax failure had no gate. Also `container_registry_auth` plumbing (runtime docker login, password persisted only for runner jobs) already exists and can carry Artifact Registry pull tokens unchanged.

## Experiment Environment Reproducibility: Project-Baked Images + Experiment Harness

### Problem
Agents pick `container_image` free-form at authoring time (or omit it), and commands `pip install` latest-everything inside. Today's RL proof job died because latest brax calls `jax.device_put_replicated`, removed in current jax — fixed only by hand-pinning `jax==0.6.2`. There is no project default image, no digest pinning, no rebuild discipline, and no pre-launch smoke on the operator-GPU path, so incompatible-deps failures burn full spend caps. Agents also re-invent result-file/checkpoint plumbing per experiment.

### Current state (verified)
- `container_image` is an optional free string on the launch contract (`backend/app/compute_experiment_contract.py:86`, `backend/app/admin.py:2166`); validation only checks non-empty string (`compute_experiment_contract.py:232-243`). No project default exists anywhere (`default_container_image` has zero hits); Artifact Registry (`us-central1-docker.pkg.dev/.../blueprint-repo`) is used only for orchestrator/agent images (`backend/cloudbuild.yaml:34`, `agent/Dockerfile:22`).
- Agents are told to choose an NGC image themselves (`agent/src/prompts/thinker.ts:442`, `agent/src/handlers/strategic.ts:1149`).
- Pre-flight smoke **exists**: `run_preflight_smoke` (`backend/app/compute_backends.py:258`) runs the command containerized (docker login→pull→run) on the orchestrator with a 30s timeout, checks the declared metric in `$BLUEPRINT_RESULT_FILE`, degrades to bare `bash -lc` without docker. But it is **skipped for runner-subprocess jobs** (`backend/app/compute_jobs.py:1462-1465`, comment: "a leader-local smoke is wrong… different substrate") — so iconlab/dgx-1 jobs get no smoke at all. Author-attested `smoke_sample` is advisory-only (`compute_experiment_contract.py:245-279`).
- Runner side: fail-closed isolation (docker/podman/apptainer/bwrap) via `iso.plan_compute_execution` (`scripts/local_runner.py:2058`); `_docker_login` handles per-job registry auth (`local_runner.py:1536`, password persisted only for runner jobs per `admin.py:2165`); missing runtime downgrades to image-less bwrap (`local_runner.py:2100`). Disk hygiene = workspace free-space claim guard only (`BLUEPRINT_RUNNER_MIN_DISK_BYTES`, `local_runner.py:721-741`); **no docker image GC/prune anywhere**.
- `$BLUEPRINT_RESULT_FILE`/`$BLUEPRINT_ARTIFACT_DIR` are injected on all backends (`compute_backends.py:89`, `scripts/runner_isolation.py:1340,1389,1430`); no in-repo harness convention consumes them.

### Proposed design

**(a) Project-baked images.**
- **Manifest hash**: sha256 over the repo's dependency manifests (`requirements*.txt`, `pyproject.toml`, `uv.lock`, `environment.yml`, optional `blueprint.image.toml` declaring the CUDA/NGC parent). New table `ProjectImage(project_id, manifest_hash, parent_image, image_uri_digest, status: BUILDING|READY|FAILED, build_log_excerpt, size_bytes, built_at, last_used_at)`.
- **Builder = Cloud Build** (primary): orchestrator tars the repo at HEAD to GCS and submits a build — `FROM <parent>` → copy manifests → `pip install` → **`pip freeze > /bp-lock.txt` baked in** (transitive pins captured) → `pip install blueprint-harness==<ver>` → push to `us-central1-docker.pkg.dev/$PROJECT/blueprint-experiments/<project_id>:<manifest_hash>`; store the returned **digest** (`@sha256:…`) as `image_uri_digest`. Workload identity already has `artifactregistry.writer` (`infra/terraform/workload_identity.tf:54`). NGC-parent auth: `nvcr.io` public images pull anonymously; gated parents use an NGC API key as a Cloud Build secret — used by the *builder daemon* login, never entering layers. **Security invariant**: build context = repo tarball + manifests only; a post-build gate scans `docker history` + image env for token patterns (`ghs_`, `sk-ant`, `BLUEPRINT_*_TOKEN`) and hard-fails the bake. Credentials remain runtime-injected exactly as today.
- **Rebuild trigger**: lazily at authoring — resolve `ProjectImage` for current manifest hash; on miss, enqueue a build (dedup by `(project_id, manifest_hash)`) and fall back (below).
- **Default injection**: in contract assembly/validation (`_author_compute_launch_contract` + `admin.py` create path), when `container_image` is absent and runtime-config `project_baked_images_enabled` is on, set `container_image = image_uri_digest` and stamp `container_image_source: "project_baked"` for diagnostics. Agent-explicit images still win.
- **Runner pull auth**: orchestrator mints a short-lived AR access token at dispatch and sends it through the existing `container_registry_auth` field (`username: oauth2accesstoken`) — zero runner-protocol change.
- **Fallback**: no READY image → current behavior (agent-chosen image or bare command); bwrap downgrade path untouched.
- **GC**: registry — AR cleanup policy: keep newest 3 digests per project repo, delete untagged >30d. Runner — new maintenance tick in `local_runner.py`: when free disk < 2× `BLUEPRINT_RUNNER_MIN_DISK_BYTES`, `docker image rm` LRU images matching `*/blueprint-experiments/*` not used by a live job (critical for dgx-1's ~30GB disk).

**(b) In-repo experiment harness.**
- `blueprint-harness` pip package (baked into every project image; also pip-installable for bare runs): `bp-run <stage>` CLI + `blueprint_harness` lib with `log_metric(name, value)` (atomic write to `$BLUEPRINT_RESULT_FILE`), `artifact_path(name)`, `save_checkpoint(state)/load_checkpoint()` under `$BLUEPRINT_ARTIFACT_DIR/checkpoints/` (durable via existing artifact upload), and `render_video/render_plot` helpers writing to the artifact dir.
- Repo convention: `[tool.blueprint.experiment]` in `pyproject.toml` declaring named stages (`smoke`, `train_stage_1..N`, `eval`) each with a command and max-minutes. **Short-chained stages**: each stage fits one task's deadline, ends with a checkpoint; a follow-on `compute_experiment` resumes from the prior task's downloaded checkpoint artifact — turning long training into a chain of budgetable jobs instead of one deadline-blowing run.
- **Authoring steering**: extend `thinker.ts:442` (and `strategic.ts` examples): when the project context includes a harness manifest (surfaced by the backend into the authoring context), the contract SHOULD be `command: "bp-run <stage>"` with `container_image` omitted. Contract validator logs an advisory (same pattern as smoke_sample advisories) when a harness exists but the command bypasses it — observability first, no hard gate.

**(c) Runner-side pre-flight smoke.** Close the `compute_jobs.py:1464` gap where it belongs — on the runner: add `preflight_command` + `preflight_timeout_seconds` (default 120) to the runner job body. Default synthesized command: `nvidia-smi -L && bp-run smoke` when a harness exists, else `python -c "<imports scraped from command>"`. Runner executes it under the **same** `plan_compute_execution` boundary/image before the main command; failure reports a new `exec_status: "preflight_failed"` mapping to existing `compute_preflight_smoke_failed` (`backend/app/failure_classification.py:290`) — job fails in ~2 min instead of after the spend cap. Config key `runner_preflight_mode: off|shadow|enforce` (shadow = run, log, continue).

### Rollout phases
1. **Harness package + repo convention + thinker guidance + advisory validator** — no infra, immediately improves authored experiments even on agent-chosen images.
2. **Runner pre-flight (shadow → enforce)** — protocol field is additive; old runners ignore it (dark-landable).
3. **Baked-image pipeline** behind `project_baked_images_enabled` (default off): schema, Cloud Build bake, digest pin, secret-scan gate, default injection, AR pull tokens. Dark-land: build+record without injecting; flip injection per-project.
4. **GC both sides** + AR cleanup policy + dashboards (image status on project page).

### Risks & mitigations
- **Stale baked image vs. repo HEAD skew** (manifests changed, build pending): injection only uses READY images matching current hash; otherwise fall back — never block authoring on a build.
- **Bake failures** (dep conflicts in repo manifests): surface `build_log_excerpt` as a project finding so agents fix manifests; fallback path keeps experiments running.
- **Runner disk blowup** (multi-GB NGC-derived images, dgx-1 ~30GB): LRU prune + per-project single-image reuse (digest pin means high cache-hit); alert when image > free-disk/2.
- **Credential leakage into layers**: repo-only build context + mandatory post-build history/env scan hard-fail; NGC key only as builder-daemon secret.
- **Preflight false-fails** (slow imports, JIT warmup): shadow mode first; timeout counts as pass (matching orchestrator smoke semantics at `compute_backends.py:394-395`).
- **Manifest hash misses unpinned transitive drift between bakes**: acceptable — digest pinning makes each bake immutable; baked `pip freeze` lockfile makes drift auditable.

### Effort estimate
- Phase 1: **M** (package + prompts + advisory)
- Phase 2: **M** (runner protocol + classification + shadow/enforce flag)
- Phase 3: **L** (schema, Cloud Build integration, secret-scan gate, injection, token minting)
- Phase 4: **S** (prune loop + AR policy + UI surfacing)

---

All claims verified against the code. Deliverable follows.

## Compute Experiment Iteration: Progress, Early Cut, Resume, and Reason Propagation

### Problem

A compute_experiment that stops learning burns its full deadline (up to 24h of L40S/V100 time) because nothing observes progress mid-run. When it fails, several reason channels are lossy: the runner's timeout path drops the captured log entirely, reap reasons live in a mutable status message instead of durable history, and every retry starts from scratch because launch contracts cannot reference a prior job's artifacts — so an agent iterating on a training run re-pays the full warm-up cost each attempt.

### Current state (verified)

- **Deadline, no progress signal.** `COMPUTE_JOB_MAX_DEADLINE_SECONDS` default 86400 (`backend/app/compute_jobs.py:91`), clamped at `clamp_deadline_seconds` (`compute_jobs.py:199`). The monitor (`compute_job_monitor_service`, `compute_jobs.py:1264`) times out jobs at `runtime_deadline_at` (`compute_jobs.py:1835-1868`) — the only mid-run cut. The runner enforces the same deadline locally (`scripts/local_runner.py:2209-2214`). No progress concept exists anywhere; `BLUEPRINT_PROGRESS_FILE` has zero hits.
- **Interim reports are empty.** The runner posts `exec_status="running"` heartbeats with no log and no metrics (`local_runner.py:2222-2223`); `command_log` and `result_metrics` arrive only on the terminal report (`local_runner.py:2257-2263`). The report endpoint (`backend/app/routers/runner.py:829-930`) stamps whatever arrives into `polling_metadata` and returns `cancel_requested` — a working cancel channel the runner already honors every heartbeat interval (`local_runner.py:2224-2228`).
- **Timeout path loses the log.** On runner-side deadline expiry the report is `exec_status="failed", exit_code=124` with **no** `command_log` (`local_runner.py:2210-2214`) even though `log_capture` holds the bounded log. Same on shutdown/cancel paths (`2216-2228`). The 24h-burned job is precisely the one whose log vanishes.
- **Briefing partially stale — follow-on context is decent.** `build_compute_result_logs` embeds redacted `command_log_text` (16k, head+tail) plus `terminal_reason` into the COMPUTE_RESULT (`compute_jobs.py:2612-2745`), and `_build_act_on_verdict_prompt` surfaces the log tail prominently in failure mode (`backend/app/task_completion.py:2859-2888`). Also stale: the admin UI **does** render `polling_metadata.command_log`, clipped to 30 lines (`admin-ui/src/components/ComputeJobsPanel.tsx:2069-2072`). The real gaps are the timeout-drops-log bug, the 30-line clip, and no progress timeline.
- **Reap reasons not durable.** `reaper.py` computes rich `reap_reason` strings + codes (`reaper.py:980-1056`) but writes them to mutable `task.latest_status_message` (`reaper.py:1843`) and context meta; zero `TaskHistory(` constructions in `reaper.py` — each reap overwrites the last. The requested fix has **not** landed.
- **No resume.** Launch contract has `accelerator`/`success_criterion`/`container_image` (`backend/app/payload_schemas.py:1013,1026,1046`) but no input-artifact field; the runner reads no such key from the claim body (`local_runner.py:1956-1966`) and `tmpdir` is deleted in `finally` (`local_runner.py:2290`). Server-side bundle machinery exists: upload persistence (`compute_backends.py:1562`), `download_compute_job_artifact_bundle` (`backend/app/storage.py:498`), bundle at `compute-jobs/<id>/artifacts/bundle.tar.gz` (`storage.py:268,502`).

### Proposed design

**1. Progress protocol.** Inject `BLUEPRINT_PROGRESS_FILE` (sibling of `BLUEPRINT_RESULT_FILE` in `runner_isolation.py` env plans). Contract: append-only JSONL, one object per line, numeric metric keys plus optional `step`. Runner tails it (offset-tracked, like `_BoundedDedupLog`) and attaches to each interim report: extend `ComputeJobReportRequest` with `progress: {latest: {...}, count: int, last_lines: [...]}` (last ≤20 records). Endpoint stamps `polling_metadata["runner_progress"] = {latest, history_tail, updated_at}`. Interim reports also start carrying a bounded `command_log` tail (~4KB) so mid-run logs are visible.

**2. Watchdog — orchestrator-side.** Evaluate in the existing monitor tick (`compute_jobs.py:1264` service), not the runner: policy is centrally tunable, applies uniformly to future backends, and the cut reuses the proven `cancel_requested` channel. Policy per job from launch contract: `progress_policy: {metric, mode: "improve"|"any_report", window_seconds, direction, min_delta}`; defaults from runtime config keys `compute_progress_stall_window_seconds` (default 5400), `compute_progress_watchdog_mode` (`off`→`advise`→`enforce`). Semantics: if the job declared `progress_policy` (or default mode is `enforce` and a progress file was ever written) and `metric` hasn't improved by `min_delta` within `window_seconds`, terminate with new outcome `STALLED_NO_PROGRESS`, `terminal_reason="no_progress: {metric} flat at {value} for {window}s (best={best} at {ts})"`. Jobs that never write progress are exempt in `enforce` unless the contract opts in — no silent breakage of existing experiments. Grace: no evaluation before `min(window, 25% of deadline)`.

**3. Checkpoint resume — ships with the watchdog, gated on it.** New launch-contract field `resume_from_job_id: UUID | None` (validated: same project, terminal, bundle exists). Claim response (`/api/runner/next-compute-job`) adds `input_bundle: {job_id, sha256, size_bytes}`; runner fetches via new `GET /api/runner/compute-job/{job_id}/input-bundle` (runner-token auth, project- and pin-scoped like `runner.py:854-861`, streams the GCS bundle) and extracts into `$BLUEPRINT_INPUT_DIR` inside `tmpdir` before command start; env var injected alongside the others. Anti-chaining guard: server rejects `resume_from_job_id` unless the new job carries a `progress_policy` **and** `compute_resume_max_chain_depth` (default 3, walked via a `resume_lineage` list stamped in `polling_metadata`) is not exceeded. Resume without a watchdog is refused with a reason string, not silently allowed.

**4. Reason propagation.** (a) Runner: include `log_capture.result()` snapshot in the timeout/cancel/shutdown terminal reports (`local_runner.py:2210-2228`) — one-line-class fix, biggest single waste-of-evidence bug. (b) Reaper and compute monitor append a durable `TaskHistory` row (`action="REAPED"` / `"COMPUTE_TERMINATED"`, detail = full reason string + code) wherever `reap_reason` / `terminal_reason` is computed — reasons survive subsequent reaps. (c) `build_compute_result_logs` adds `progress_summary` (best value, last value, records count, stall verdict) so the act-on-verdict follow-on prompt (`task_completion.py:2795`) can say "trained 14 epochs, loss plateaued at 2.31 after epoch 6" instead of inferring from a log tail.

**5. UI first-class.** ComputeJobsPanel: replace the 30-line clip with an expandable full-log view (data already present), a progress sparkline from `runner_progress.history_tail`, and a `terminal_reason` chip. Task detail timeline renders the new TaskHistory reason rows. Surface reasons, not states.

### Rollout phases

1. **P1 — evidence fixes (dark):** runner sends command_log on all terminal paths; interim log tail; TaskHistory reason rows for reaper + compute terminal. No behavior change to any job.
2. **P2 — progress protocol (dark):** env injection, runner tail, report/endpoint schema, `runner_progress` stamping, `progress_summary` in COMPUTE_RESULT, UI sparkline. Watchdog mode `off`.
3. **P3 — watchdog `advise`:** monitor evaluates and logs/annotates would-cut decisions into polling_metadata + task history; tune window on real jobs.
4. **P4 — watchdog `enforce` + resume:** flip config; ship `resume_from_job_id`, input-bundle endpoint, runner rehydration, chain-depth + watchdog-required gates.

### Risks & mitigations

- **False-positive cuts** (LR warm-up plateaus): `advise` phase first; per-job policy override; grace floor; exemption for undeclared jobs.
- **Progress spam bloating polling_metadata:** bounded tail (≤20 records, byte cap mirroring `_MAX_RUNNER_COMMAND_LOG_BYTES`).
- **Resume chains re-burning budget:** depth cap + watchdog prerequisite + lineage stamped for audit.
- **Bundle rehydration cost/poisoning:** sha256 in claim response verified by runner; size cap config; extraction confined to `tmpdir` with tar-path sanitization.
- **Schema drift for old runners:** all new report fields optional; endpoint tolerates absence (matches existing hostname/version tolerance pattern).

### Effort estimate

- P1: **S** (runner report call sites + two history-append sites)
- P2: **M** (protocol touches runner, isolation env, endpoint, artifact builder, UI)
- P3: **S** (monitor evaluation + config keys, reuses P2 data)
- P4: **M** (new endpoint, contract validation, rehydration, gates)

---

All verification done. Here is the deliverable.

## Goal-Hitting: Direction Lifecycle Judgment, Experiment Memory, and Follow-on Spawn Discretion

### Problem

The engine reliably finishes tasks but wastes lanes on goals that no longer deserve them: ~18% of research directions are redundant, ~29% duds; strategist opens directions for one-time fixes that dry up; agents re-author near-duplicate experiments because nothing shows them what was already run; and every measured compute_experiment — including operator smokes — auto-spawns a reason-and-ship follow-on regardless of whether there is anything to reason about. Retirement pressure today is mechanical (threshold ladders over merge counts); the *judgment* step ("is this direction dried up, redundant, or blocked-but-alive?") is left to the strategist with raw numbers and no classified verdict.

### Current state (verified, with file:line anchors)

The briefing is stale in three places; design targets reality:

- **Directions**: `Project.directions` is a JSONB list of dicts with statuses active/paused/proposed/rejected/expired (`backend/app/models.py:490-497`), dual-written to a first-class `Direction` table via `directions_repo.sync_directions` with a queryable per-direction event trail (`models.py:3859-3891`, `3981-3986`; `GET /api/admin/projects/{pid}/directions/{key}/events`).
- **Churn guardrail false-pause is FIXED** (briefing stale): the consolidated non-merge pause proxy `_direction_non_merge_pause` (`backend/app/strategic_completion.py:2384-2499`) shields on `merged_prs_any_base` for zero-merge (`:2427-2430`) and requires `real_merges == 0` for the churn branch (`:2472`) — merge-credit-propagation lag no longer pauses healthy directions.
- **Retirement/cooldown machinery exists but is metric-mechanical** (briefing stale on absence): refractory cooldowns with productive/unproductive/saturation multipliers (`backend/app/direction_refractory.py:19-57`; defaults `autonomous_direction_cooldown_minutes=240`, productive 45); churn-family pause ladder flags `churn_retirement_recommended` after 2 pauses instead of auto-retiring (`backend/app/direction_lifecycle.py:29-44`, `1715-1732`); lifetime-burn ladder auto-retires; strategist ops already include `create_direction`, `retire_direction`, `decide_churn_retirement`, `set_retirement_critiques` (`strategic_ops_manifest.json`; handler `routers/admin_agent_ops.py:2196`, operator seam `routers/directions.py:1445`). Proof-weighted merge credit tiers 1.0/0.7/0.4 (`direction_metrics.py:36-38`).
- **Follow-on spawning is unconditional judgment-free**: always-dispose spawns a reason-and-ship child for EVERY measured, non-prerequisite experiment; only intentional kills (budget/deadline/cancel) and prerequisite-mode advisory experiments (`task_completion.py:3526-3528`) are excluded (`backend/app/task_completion.py:3763-3793`). The authoring-time `act_on_verdict` opt-in was retired (`payload_schemas.py:1053-1054`). **No operator-smoke suppression flag exists** (verified by grep) — operator-launched smokes spawn follow-ons. Concurrency cap `max_concurrent_act_on_verdict_followons=4` and a replay-guard context key exist (`task_completion.py:2786`, `2943-2967`).
- **Sweep machinery exists**: `sweep_id` on the payload suppresses per-arm follow-ons for one aggregate (`payload_schemas.py:1031-1035`; `task_completion.py:3081-3089`).
- **No prior-experiment memory**: compute results persist as KnowledgeDocuments with acceptance grades (`knowledge_completion.py`, `knowledge_query.py:30-33`) and are *searchable*, but nothing injects "experiments already run in this direction" into authoring context; `research_context.py:195-310` accumulates established/failed *findings* per direction, not launch contracts. No near-duplicate check at spawn.
- **Goal-progress aggregation exists but only for the digest**: `compute_direction_progress` (`digest_direction_progress.py`) computes per-direction proven/declared/verified merges, challenge outcomes, spawn-gate reasons — it is not packaged into the strategist's per-tick context (`strategic_context_package.py` resolves direction refs to label/priority/status only, `:376-391`).

### Proposed design

**1. Direction verdict judge (LLM-classified retirement evidence).** New read-only task type `direction_review` (registered like `design`/`web_research`, terminal-leaf producing a KnowledgeDocument + a structured verdict). Trigger: when any ladder stamps `churn_retirement_recommended`, when a direction completes N cycles with ≤1 child per research task (the dud signature), or on strategist request. Input payload: the direction dict, `compute_direction_progress` output, event-trail tail, sibling-direction texts (for redundancy detection), and the project problem_model. Output schema (validated, stored on the direction as `judge_verdict`): `{classification: dried_up|redundant_with:<key>|dud_one_shot|blocked_recoverable|healthy, confidence, evidence_summary, recommended_action: retire|merge_into|cooldown_extend|reactivate}`. The verdict is *evidence for* the existing `decide_churn_retirement` seam — numbers as evidence, LLM as classifier, strategist/operator as decider. No deterministic auto-retire added (operator constraint). Config: `direction_judge_enabled` (default off), `direction_judge_dud_child_threshold` (children-per-research-task trigger, default 1).

**2. Prior-experiment ledger + authoring injection.** New accessor `get_direction_experiment_ledger(project_id, direction_id)` in a small `experiment_ledger.py`: queries terminal compute_experiment tasks (join ComputeJob) for the direction, returns compact rows `{command_digest, container_image, accelerator, metric, operator, threshold, outcome, measured_value, cost, task_id, concluded_at}`. Two consumers: (a) inject the last ~15 rows into research/create task payload context when the direction has any prior experiments (same pattern as `research_context` prior_history); (b) advisory near-duplicate stamp at compute_experiment spawn validation — if `command_digest` + success_criterion matches a prior terminal row, stamp `context[prior_duplicate_of]` and prepend a prompt line telling the *agent* the result of the prior run; the agent decides whether re-running is justified (no hard block — deterministic rejection is exactly what the operator rejects). Config: `experiment_ledger_context_rows` (default 15).

**3. Follow-on spawn discretion.** Add optional payload field `disposition: "full" | "none" | "aggregate_only"` to `ComputeExperimentPayload` (default absent = today's always-dispose). `"none"` skips the follow-on spawn at `task_completion.py:3779` (still faithful-captures — axis A untouched). Auto-default to `"none"` when `task.created_by` is a human operator and no direction_id is attached (the operator-smoke case), gated by config `operator_experiment_dispose_default` (`"none"`, overridable per-task). For agent-authored experiments, keep spawn but add one line to the follow-on prompt contract already present ("conclude with a recorded no-op…", `task_completion.py:2905-2907`): require the no-op record to name *why* (feeds the ledger's outcome column). This is authoring-time intent plus agent judgment, not a completion-time rubric.

**4. Goal-progress in the strategist's field of view.** Extend `strategic_context_package` direction descriptors (`:376-391`) with a `progress` sub-object sourced from `compute_direction_progress` (proven/declared merged counts, last-3-cycle trend, experiment ledger summary counts, `judge_verdict` if present). Add `GET /api/admin/projects/{pid}/directions/{key}/progress` returning the same object so operator UI and strategist read identical evidence. Retirement decisions then cite verdict + progress in `set_retirement_critiques`.

### Rollout phases

1. **Ledger read path** — `experiment_ledger.py` + admin endpoint; dark (no consumer). Ship alone.
2. **Authoring injection + duplicate advisory stamp** — consumes phase 1; flag `experiment_ledger_context_rows>0`.
3. **Disposition field + operator default** — payload schema addition (extra-tolerant, backward-safe), config default off; dark-lands as no-op until config flips.
4. **Direction judge task type + verdict storage** — behind `direction_judge_enabled`; verdicts write-only until consumed.
5. **Strategist/operator progress surfacing** — context-package + endpoint; consumes phases 1+4; pure read.

### Risks & mitigations

- **Judge cost/noise**: cap one open `direction_review` per direction (reuse `DIAGNOSIS_IN_PROGRESS_KEY` pattern, `models.py:385-387`); trigger only on ladder events, not per-tick.
- **Duplicate advisory suppresses legitimate re-runs** (seed sweeps, post-fix retries): advisory-only, agent decides; `sweep_id` arms exempt.
- **Disposition `"none"` hides real findings from operator smokes**: faithful capture + KnowledgeDocument persistence unchanged; only the spawned child is skipped.
- **Context bloat**: ledger rows are compact digests; hard cap rows and reuse `_ACT_ON_VERDICT_LOG_CHARS`-style byte budget.
- **JSONB/dual-write drift**: all direction writes go through `with_directions_locked`/`directions_repo` (`models.py:3873-3876`) — judge verdict stamped via the same seam.

### Effort estimate

- Phase 1: **S** — Phase 2: **M** — Phase 3: **S** — Phase 4: **M/L** (new task type + prompt + verdict schema + tests) — Phase 5: **S/M**.

---

All claims verified against the code (one briefing claim is stale — noted below).

## Operator Visibility: Experiment Artifact Previews & Logs

### Problem
compute_experiment jobs on operator GPU machines produce plots, renders, and videos, but operators cannot see any of it. Everything under `$BLUEPRINT_ARTIFACT_DIR` is persisted as one opaque tarball; the strategic canvas stubs image artifacts with "requires a signed preview URL" even though the signing machinery already exists and is used elsewhere. Debugging a failed run means downloading and untarring a bundle by hand.

### Current state (verified)
- **Image previews stubbed**: `backend/app/strategic_context_package.py:1152-1156` — `_artifact_descriptor` emits `image: {url: None}` plus `unsupported_reason: "Image artifact previews require a signed preview URL."` for image-kind artifacts.
- **Signing exists and works**: `generate_download_signed_url` is used for viewport failures/shots (`backend/app/main.py:1348`, `:1416`), and a fail-closed operator resolver `GET /api/admin/artifacts/download-url` exists (`main.py:1429-1520`). Critical constraint: for compute-job URIs, the resolver allowlists **only** `job.result_reference` and the linked `Artifact.storage_uri` (`main.py:1498-1508`) — any new per-file preview objects would 404 until registered.
- **Monolithic bundle**: the runner tars the whole artifact dir (25 MiB cap, `scripts/local_runner.py:817`, `:1802-1856`) and POSTs it to `/api/runner/compute-job/{job_id}/artifact`; `store_runner_artifact` (`backend/app/compute_backends.py:1555-1595`) writes it to `compute-jobs/<job_id>/artifacts/bundle.tar.gz` and the router stamps `polling_metadata["runner_artifact"]` (`backend/app/routers/runner.py:1062-1073`). Individual files are not addressable.
- **UI is ready for images**: `admin-ui/src/components/StrategicContextArtifactSlot.tsx:74-82,127` renders `descriptor.image.url` when it is a valid http(s) URL. No video support.
- **Briefing stale on logs**: `command_log` IS surfaced — `ComputeJobsPanel.tsx:2067-2101` renders an expandable terminal command log, and live output at `:2043-2065`. The remaining gap is only that logs/previews are invisible from the strategic canvas and task detail, not from the compute-jobs panel.
- **Evidence loader drops the URI**: the evidence query selects `Artifact.storage_uri` (`strategic_context_package.py:735`) but the item dict (`:800-822`) omits it, so `_artifact_descriptor` has nothing to sign. `load_strategic_context_evidence` (`:633`) is async with a session — signing at load time is feasible.
- **Evidence rules**: `knowledge_completion.py:286-299` rejects bare `http(s)://` evidence citations; `gs://` and `file:` references are the durable form. Signed URLs must stay transient.

### Proposed design
**1. Server-side preview extraction (no runner change).** The backend already holds the full ≤25 MiB tarball in memory in `store_runner_artifact`. Extract members matching a viewable allowlist and store each alongside the bundle:
- Object path: `compute-jobs/<job_id>/artifacts/previews/<index>-<sanitized-basename>`.
- Config keys (settings, hot-reloadable): `compute_artifact_preview_enabled` (default true), `compute_artifact_preview_exts` (default `png,jpg,jpeg,webp,gif,svg,mp4,webm`), `compute_artifact_preview_max_files` (default 12, deterministic order: root-first, then mtime desc), `compute_artifact_preview_max_file_bytes` (default 8 MiB).
- Safe extraction: `tarfile` with `data` filter, reject symlinks/absolute/`..` members, per-member decompressed-size cap enforced via bounded reads.
- Index schema stamped into `polling_metadata["runner_artifact"]["previews"]`:
  `[{"uri": "gs://…", "name": "loss_curve.png", "bytes": 41233, "content_type": "image/png", "kind": "image"|"video"}]` (a bundle-index, not Artifact rows — avoids schema migration; the linked bundle `Artifact` row remains the single durable artifact).

**2. Resolver allowlist extension.** In `main.py:1498-1508`, extend `allowed_uris` with the `previews[*].uri` entries from `job.polling_metadata["runner_artifact"]`. 404 semantics for unknown siblings unchanged.

**3. Compute-jobs UI gallery.** Expose `previews` in the admin job payload (already flows via `polling_metadata`; add typed field to the job schema in `schemas.py` and the TS type). `ComputeJobsPanel` renders a thumbnail strip per terminal job; each thumbnail lazily resolves via the existing `GET /api/admin/artifacts/download-url?storage_uri=&project_id=` and opens full-size. Videos render `<video controls preload="metadata">`.

**4. Strategic canvas signed previews.**
- `load_strategic_context_evidence` adds `storage_uri` (and, for compute-job artifacts, the best preview URI from the owning job's `runner_artifact.previews`) to artifact items, then signs image/video URIs inline (bounded: only the ≤4 items the descriptor slice consumes, `strategic_context_package.py:1264`) with `expiration_minutes=60`.
- `_artifact_descriptor` gains: `image: {url, alt, expires_at, storage_uri}` when a signed URL is present (drop `unsupported_reason`); new `kind: "video"` with `video: {url, mime, expires_at, storage_uri}`.
- **Expiry/refresh**: the canvas is long-lived. The slot component tracks `expires_at`; on expiry or `<img>`/`<video>` `onError`, it re-resolves through `GET /api/admin/artifacts/download-url` using the descriptor's `storage_uri` + current `project_id` — reusing the fail-closed path rather than a new refresh endpoint. Signed URLs are never persisted: they exist only in the in-flight context-package response, so `knowledge_completion.py` evidence rules (`:286-299`) are untouched.

**5. Log surfacing (scoped down — briefing was stale).** Skip the ComputeJobsPanel work (done). Add a `text`-kind descriptor for the newest failed compute job's `command_log` tail (last ~40 lines, server-truncated) to the strategic evidence artifacts, and a command-log section in the task-detail drawer for `compute_experiment` tasks, sourced from the same `polling_metadata.command_log`.

### Rollout phases
- **P1 (dark-landable)**: preview extraction in `store_runner_artifact` + `previews` index in `polling_metadata` + config keys. Nothing reads it yet; backfill not needed (new jobs only).
- **P2**: resolver allowlist extension + job-schema exposure + ComputeJobsPanel gallery. Ships operator-visible value independently of the canvas.
- **P3**: strategic evidence `storage_uri` plumb-through, load-time signing, image/video descriptor rendering, expiry/refresh in the slot component.
- **P4**: command-log text descriptor + task-drawer log section.
- **Later (out of scope, flagged)**: GCP-backend parity — its bundle is uploaded from the VM at poweroff, so previews would need lazy orchestrator-side extraction on first request; runner-subprocess is the active substrate (iconlab/dgx-1), so this can wait.

### Risks & mitigations
- **Tar-bomb / path traversal** on server-side extraction → tarfile `data` filter, member allowlist, per-file and total decompressed caps; extraction failure degrades to bundle-only (never blocks terminal completion, matching `store_runner_artifact`'s never-raise contract).
- **Signed-URL leakage into durable stores** → URLs live only in response payloads; `knowledge_completion.py` already rejects bare http(s) evidence; add a unit test asserting context-package persistence paths (if any snapshotting exists) strip `image.url`/`video.url`.
- **Resolver surface widening** → previews are enumerated from `polling_metadata` written only by the authenticated runner-upload path; unknown siblings still 404.
- **Evidence-load latency** from inline signing → bounded to ≤4 signs per package load; on signing failure fall back to the current stub (fail-soft).
- **Stale thumbnails after 60 min** in long-lived canvases → `expires_at` + onError re-resolve; worst case one broken image flash before refresh.
- **25 MiB bundle cap silently drops previews for large runs** (`local_runner.py:1836-1842`) — unchanged in this design; note in the gallery UI when `runner_artifact.stored=false` so absence is explained (per "surface reasons not just states").

### Effort estimate
- P1: **M** (extraction safety + tests dominate)
- P2: **S/M** (resolver: S; gallery UI: M-ish S)
- P3: **M** (plumbing + descriptor schema + refresh semantics + tests)
- P4: **S**

---

## Fleet-runner structural gaps: multi-project runners, split capacity pools, GPU bookkeeping, operator pinning, load-aware claiming

### Problem
A runner is hard-bound to one project, so serving two projects means two daemons on one host with independent GPU bookkeeping (double-booking risk). Server-side capacity lumps agent and compute work under one `max_concurrent`, so agent tasks starve idle compute slots. GPU assignment lives only in the daemon's process memory. Operators cannot pin an experiment to a specific machine via the launch contract, and a pegged host keeps claiming work.

### Current state (verified)
- **Project-scoped runner**: `LocalRunner.project_id` FK `ondelete=CASCADE` (backend/app/models.py:4656–4663). Enrollment is per-project, owner-only (backend/app/routers/admin_runners.py:350–404). Token is an opaque random secret, hashed on the runner row, with no project identity encoded (backend/app/auth.py:358–372) — auth changes are NOT required for multi-project.
- **Combined capacity**: `_runner_at_capacity` compares `_runner_active_work_count` (agent `active_task_ids` + compute `active_compute_job_ids`, orchestrator_runner_dispatch.py:163–164) against a single `max_concurrent` (orchestrator_runner_dispatch.py:173–203). Enrollment normalizes only one `max_concurrent` (admin_runners.py:100, 124–142). Yet the daemon already enforces **separate local pools** — `BLUEPRINT_RUNNER_MAX_CONCURRENT` vs `BLUEPRINT_RUNNER_MAX_CONCURRENT_COMPUTE_JOBS`, two semaphores (scripts/local_runner.py:3106–3108, 3264–3265) — and heartbeats the two active lists separately, but never reports the compute limit. Server and runner disagree about capacity semantics today.
- **Process-local GPU state**: allowlist + in-use set + `threading.Lock` built per daemon (scripts/local_runner.py:1602–1637); nothing host- or server-level prevents two daemons overlapping indices.
- **Selection & pinning**: runners are cached per `project_id` (orchestrator_runner_dispatch.py:244–266) and looked up via `active_runners_cache.get(project_id)` (line 569). Briefing item (4) is **half-stale**: `preferred_runner_target_id` already exists as a soft preference in `_select_eligible_runner` (lines 553, 581–588), but it is set only internally (strategic-session affinity, backend/app/strategic_local_runner.py:194–200; autonomous_research.py:1305–1308). The compute launch contract has no operator-facing field — `_build_compute_launch_kwargs` (line 600+) forwards no runner target. Ties: `min()` is stable, so equal-load ties deterministically favor DB row order (line 597).
- **Host load**: `cpu_load_avg` is heartbeat-reported (local_runner.py:1348) and stored (routers/runner.py:554–555) but is **display-only** (admin_runners.py:277, 322). No claim throttling landed — briefing (5) confirmed.
- **Pull path**: `POST /api/runner/next-task` selects by `vm_instance_id == f"runner:{id}"` with no project filter (routers/runner.py:214–229) — the pull side is already project-agnostic; only pinning is project-bound.

### Proposed design
**1. Multi-project runner (enrollment allow-list).**
New table `runner_project_access(runner_id FK→localrunner CASCADE, project_id FK→project CASCADE, max_concurrent_override int NULL, max_concurrent_compute_override int NULL, created_at)`, composite PK. `LocalRunner.project_id` stays as the **home project** (enrollment anchor, token lifecycle, UI ownership). New migration + `EXPECTED_SCHEMA_REVISION` bump; backfill inserts one access row per existing runner (home project). Endpoints: `POST/DELETE /api/admin/runners/{runner_id}/projects/{project_id}` (caller must be owner of **both** home and target project); enrollment payload gains optional `allowed_project_ids`. Auth: unchanged — token→runner row; allowed projects resolved by join. Dispatch: `_load_active_runners_cache` joins the access table and places the **same** `RunnerDispatchSnapshot` instance in every allowed project's bucket; because `runner_pins_this_cycle` and active counts key by `runner.id`, capacity bookkeeping is automatically unified across projects. Per-project carve-outs: `_runner_at_capacity` gains an optional `project_id` and enforces `min(global cap, access-row override)` using per-(runner, project) pin counts plus per-project outstanding work (task→project is known server-side; persisted compute jobs already loaded at lines 269–300). Fairness beyond carve-outs: leave FIFO-by-task-age (dispatch already iterates oldest-first); carve-outs are the guarantee mechanism. `next-task` needs **no change**. Project deletion: block deleting a runner's home project while other access rows exist (409 with reason), preserving CASCADE semantics without orphaning a shared runner.

**2. Capacity pool separation.**
Add `max_concurrent_compute` to capabilities: accepted at enrollment (`_normalize_capabilities`) and reported in the heartbeat body (heartbeat wins, like `has_gpu`). Split `_runner_at_capacity(runner, pins, kind)`: agent pool = `active_task_ids` + agent pins vs `max_concurrent`; compute pool = merged compute ids + compute pins vs `max_concurrent_compute`. `runner_pins_this_cycle` becomes `dict[(runner_id, kind), int]`. **Absent field ⇒ exact current combined behavior** (backward compatible with old daemons).

**3. Unified GPU bookkeeping.**
Steady state: one daemon per host owns all devices (multi-project removes the second-daemon motive). Migration window: a host-level lease registry in `runner_isolation.py` — `flock`-guarded JSON (`~/.config/blueprint/runner-gpu-leases.json`) mapping gpu_index→{pid, job_id, ts}; `_assign_gpus`/`_release_gpus` acquire/release through it, reaping leases whose pid is dead. Old daemons predate the registry, so the runbook requires marking the old runner `inactive` (stops pinning) and letting jobs drain before the new daemon claims compute. Server-side belt: warn (admin runner health) when two active runner rows share a `hostname` and both advertise `has_gpu`.

**4. Operator runner-target pin.**
Reuse the existing soft mechanism. Add launch-contract field `runner_target: {runner_id: uuid, strict: bool=false}` validated in the compute-experiment contract; it stamps `task.context["preferred_runner_target_id"]`. `strict=true` additionally holds the task QUEUED when that runner is ineligible/offline, reusing the `require_local_runner` hold path (orchestrator_runner_dispatch.py:816–831) with a **reason string naming the runner** in the hold log/attention surface. Admin UI: live-runner dropdown on the launch form.

**5. Host-load-aware claiming.**
Heartbeat adds `cpu_count` so load is comparable across hosts. Runner-side: `BLUEPRINT_RUNNER_LOAD_SHED_RATIO` (default off) — when `load1/cpu_count` exceeds it, skip claim iterations (still heartbeat), logging `[runner] load-shed: load=… ratio=…` every skip window. Server-side: extend `_dispatch_sort_key` with a third tuple element `is_overloaded` (ratio above `runner_load_deprioritize_ratio` runtime-config key, default off) — **deprioritize, never exclude**, since heartbeat lag makes hard exclusion oscillate and an all-overloaded fleet must still place work.

### Rollout phases
1. **Pool separation** — capability field + split `_runner_at_capacity` + per-kind pins. Dark: no runner reports the field yet. (S/M)
2. **Runner-target pin** — contract field → existing preference; strict-hold reuse; UI dropdown. (S)
3. **Load-aware claiming** — `cpu_count` telemetry + runner-side shed (env, default off) + server deprioritize (config, default off). (S)
4. **Multi-project schema/auth** — table, backfill migration, admin endpoints, home-project delete guard. Dark: every runner has exactly its home row; zero behavior change. (M)
5. **Multi-project dispatch + GPU registry** — multi-bucket cache, carve-out enforcement, flock lease registry, drain runbook, shared-hostname warning. (L)

### Risks & mitigations
- **Shared runner starves its home project** → per-project carve-out overrides (phase 4 schema, phase 5 enforcement); admin view shows per-project consumption.
- **Two-daemon window double-books GPUs** → flock registry + mandatory drain step; server hostname-collision warning as backstop.
- **Old daemons without `max_concurrent_compute`** → absent-field fallback preserves combined behavior byte-for-byte.
- **Load-shed oscillation from heartbeat lag** → deprioritize (sort) not exclude; runner-side shed uses local, lag-free load.
- **Strict pin wedges tasks on a dead runner** → hold surfaces reason + runner id via operator-attention; existing hold-timeout messaging (line 1385) extended to strict pins.
- **CASCADE on home-project delete kills a shared runner** → explicit 409 guard rather than silent orphan/delete.

### Effort estimate
Phase 1: **M** (capacity semantics touch dispatch hot path + tests). Phase 2: **S**. Phase 3: **S**. Phase 4: **M** (migration + auth + endpoints). Phase 5: **L** (dispatch restructure, per-project counting, isolation-layer registry, runbook).

Key anchors: backend/app/models.py:4643–4683; backend/app/orchestrator_runner_dispatch.py:173–203, 244–300, 547–597, 816–831; backend/app/routers/admin_runners.py:42–142, 350–404; backend/app/routers/runner.py:196–233, 554–555; backend/app/auth.py:358–372; scripts/local_runner.py:1332–1352, 1602–1637, 3106–3108, 3264–3265.

---

# Cross-cutting critique

All disputed points verified. Deliverable follows.

## Conflicts & overlaps

1. **Area 1 ↔ Area 6: same dispatch hot path, incompatible refactors.** Area 1 changes `_select_eligible_runner`'s return shape to `(runner, declines)` and adds capability filters; Area 6 rewrites the same function's inputs — `runner_pins_this_cycle` → `dict[(runner_id, kind)]`, split `_runner_at_capacity(kind)`, multi-bucket cache placing one snapshot in several project buckets. Both name the same call sites (verified: lines 360, 803, plus `preferred_runner_target_id` at 366/553/581/809). Landing these independently guarantees merge conflict and semantic drift (a decline computed against pre-split capacity is wrong post-split). Must be one owner or strictly serialized.
2. **Area 1 ↔ Area 6: two competing "why didn't it dispatch" surfaces.** Area 1 stamps `task.context["runner_dispatch_declines"]`; Area 6 extends hold-log reason strings for strict pins. Same question, two schemas. Also an interaction neither specifies: a `runner_target.strict` pin to a runner Area 1's VRAM/container filter declines — hold or decline? Undefined.
3. **Area 1 ↔ Area 2: baked-image injection vs container eligibility/downgrade policy.** Area 2 injects `container_image` into contracts that previously had none. Under Area 1's `no_container_runtime` filter (or `compute_image_downgrade_policy=decline`), that injection makes bwrap-only runners newly ineligible for jobs they run bare today — a silent fleet-capacity shrink neither design models. Injection must check runner fulfillability (Area 1's declines map) or default `allow` per-runner.
4. **Area 2 ↔ Area 3: Area 2's stage-chaining assumes Area 3's resume, and their conventions clash.** Area 2's harness checkpoints to `$BLUEPRINT_ARTIFACT_DIR/checkpoints/` and "resumes from the prior task's downloaded checkpoint" — but the only download mechanism is Area 3's `resume_from_job_id` + `$BLUEPRINT_INPUT_DIR` (harness `load_checkpoint()` must read there, unstated). Worse, Area 3's `compute_resume_max_chain_depth=3` caps exactly the `train_stage_1..N` chains Area 2 promotes, and Area 3 *refuses* resume without a `progress_policy` Area 2's stages don't declare.
5. **Area 3 ↔ Area 5: duplicated/contradicting ComputeJobsPanel log work.** Verified: terminal `command_log` already has a "Show all (N lines)" expander (`compute-command-log` block); only *live output* for active jobs is hard-clipped to 30 lines. Area 3's "replace the 30-line clip with an expandable full-log view" is stale — the real remaining gap is the live-output clip plus Area 3's own interim-log-tail feed. Area 5's scoped-down P4 is correct; Area 3 P5 should shrink to sparkline + terminal_reason chip. Both also add panels to the same component — coordinate.
6. **Areas 1, 2, 3, 6: four independent runner-protocol revs.** Heartbeat additions (A1 VRAM/CLI, A6 `max_concurrent_compute`/`cpu_count`), job-body additions (A2 `preflight_command`, A3 progress/resume, A6 target-pin), and report additions (A2 `preflight_failed`, A3 progress/log tails) each touch `local_runner.py` + `routers/runner.py` schema. Compatible individually, but four separate protocol bumps against a slow-updating fleet is operational churn — batch into at most two revs.
7. **Area 4 ↔ Area 3: both rewrite the act-on-verdict prompt/spawn region** (`task_completion.py` ~2795–2907 vs 3763–3793 + `progress_summary`). Complementary but same file/flow; Area 4's ledger `outcome` column should consume Area 3's `terminal_reason`/`STALLED_NO_PROGRESS` vocabulary — undefined today.

## Gaps

- **The 25 MiB artifact cap breaks three designs and nobody owns it.** Verified: the runner *skips the entire upload* when the artifact dir exceeds 25 MiB. RL checkpoints and videos routinely exceed this — so Area 3's resume gets no bundle, Area 2's checkpoint chaining silently dies, Area 5's previews vanish (it only annotates absence). Need chunked/large-object upload or per-file upload; this is a hard rollout dependency for A2 stage-chaining and A3 P4.
- **Cost accounting.** Henry's lever is cost/task 3–4×down; no design aggregates spend across resume chains, sweeps, or directions, or surfaces $/experiment to the strategist/judge (Area 4's ledger has a `cost` column but no budget consumer).
- **Cloud-backend parity for progress/watchdog.** Area 3's progress protocol rides runner interim reports; GCP jobs have no equivalent channel, so the watchdog silently applies only to operator hardware.
- **No success-metric for the program itself** — nothing measures "first-try experiment success rate" before/after, so no one can tell if the 30% that ships worked.
- **Missing stated dependencies:** A2's runner preflight default `bp-run smoke` needs A2 P1 harness *adopted in the repo*, not just shipped; A1's `devices` block and A4's ledger both compete for authoring-context token budget with no shared cap.

## Sequencing

**Days 0–3 (parallel, all dark, unblocks the RL witness):** A3 P1 (terminal-path logs + TaskHistory reasons — S), A6 P2 (operator runner-target pin, so the witness lands on dgx-1 — S), A2 P1 (harness package + convention + thinker guidance — the witness repo adopts `bp-run` from day one), A1 P1 (heartbeat VRAM/CLI advertise), batched with A6 P1+P3 telemetry as **protocol rev 1**.
**Week 1–2:** A1 P2 (devices in authoring context), A3 P2 + A2 P2 shadow preflight as **protocol rev 2**, A5 P1+P2 (previews + gallery — first visible RL curves/videos), A4 P1+P2 (ledger + injection). Resolve the 25 MiB cap here — prerequisite for everything week 3+.
**Week 3–4 (serialized on dispatch):** A6 P1 capacity split → A1 P3 shadow eligibility (same function, one owner) → A1 P4/P5. A3 P3 watchdog advise.
**Later:** A3 P4 resume (after cap fix, chain-depth reconciled with A2 stages), A2 P3 baked images (after A1 eligibility so injection can't strand), A4 P4 judge, A6 P4/P5 multi-project, A5 P3 canvas.

## Priority calls

Ship: **A2 P1, A3 P1+P2, A6 P2, A5 P1+P2, A1 P1+P2, A4 P2** (~30%, mostly S/M).
- *Correct-first-try:* harness convention (A2 P1) + hardware-aware authoring context (A1 P1/P2) kill the brax/jax and VRAM-blind classes at authoring time — cheaper than any enforcement.
- *Fast iteration:* A3 P1 is the highest ROI item in all six designs (the timeout-drops-log fix is one line and recovers evidence from exactly the most expensive failures); A3 P2 gives mid-run visibility without cut risk.
- *Goal-hitting:* A4 P2's ledger stops duplicate re-runs immediately; the judge can wait.
- *Visible results:* A5 P1/P2 puts plots/videos in front of the operator within days; canvas polish waits.
Defer all L items (baked images, multi-project, resume) — each depends on unresolved cross-area conflicts (#3, #4) or the unowned 25 MiB gap.

---

# Review verdict (2026-07-11, 5-lens adversarial review, top findings code-verified)

## CUT LIST (unnecessary new surface — remove or demote)
1. **CUT: direction_review judge task type (A4.1).** Duplicates a judge the system already has — the strategist — which A4.4 already feeds the same evidence and which already owns the decision ops (retire_direction, decide_churn_retirement). Keep A4.4 progress surfacing; drop the new task type, its schema, and its per-direction LLM spend.
2. **CUT: flock GPU lease registry (A6.3).** Scaffolding for a one-time, operator-supervised migration on a two-host fleet; cannot protect against the actual dangerous peer (an old daemon that predates it). The drain runbook + server-side hostname-collision warning are the right controls.
3. **CUT/SHRINK: A3 P5 log-view rebuild.** VERIFIED STALE: terminal command_log already has a Show-all expander (ComputeJobsPanel.tsx:2067-2101); only the live-output block for active jobs is hard-clipped. Remaining scope = live-tail feed + sparkline + terminal_reason chip.
4. **DEMOTE: A1 P5 CLI-aware runtime selection.** Plumbing runner cli_inventory into the fleet's most heavily-tested selection function is heavy for a fleet-hygiene problem; prefer graceful preflight decline (runtime_unavailable:<cli>) + inventory in heartbeat for visibility.
5. **ONE RULE, not machinery: follow-on spawn discipline.** Verified: every measured compute_experiment spawns a reason-and-ship child (task_completion.py:3777-3793); A2 stage-chaining × A3 resume would multiply this per stage. Fix inside existing vocabulary: intermediate chain stages (resume_lineage present / non-final stage) get disposition=none. A4.3 as designed only exempts operator smokes.

## CONFIRMED DEFECTS — amend before building
1. **Multi-project runners under-scoped (HIGH).** 'Pull side already project-agnostic / auth unchanged' is true only for agent tasks. All three compute-job runner endpoints hard-filter ComputeJob.project_id == runner.project_id (routers/runner.py:754 claim, :856 report, :1046 artifact upload); a non-home-project compute job wedges in STARTING (204 on claim) and report misses fire the cross-project security alert. These filters must become joins against runner_project_access — and the A3 resume endpoint copies the same single-project pattern into new code.
2. **AR pull-token design is a credential leak (HIGH).** oauth2accesstoken via persisted container_registry_auth = an UNSCOPED orchestrator-SA token handed to semi-trusted operator hosts and persisted in the DB. Require: downscoped token (Credential Access Boundary → artifactregistry.reader on the experiments repo) or dedicated pull-only SA; mint at CLAIM not dispatch; never persist. Also: the cited artifactregistry.writer grant belongs to the GitHub-Actions CI SA, not the orchestrator SA — the bake path needs its own scoped build SA (Cloud Build runs agent-authored pip installs next to the NGC secret; post-build pattern scan is a blocklist, not an invariant).
3. **Decline ping-pong wedge (HIGH).** Claim-time declines (image downgrade policy / preflight) have no decline-memory: under require_local_runner with 1-2 eligible runners, decline → requeue → re-pin SAME runner → infinite loop, while self-reported failure counts degrade the runner. Need per-(job,runner) decline memory feeding eligibility, and a park-with-reason terminal for all-runners-declined.
4. **Watchdog warmup false-cut (HIGH).** Anchor window_seconds at the FIRST progress record, not job start — image pull + JIT + dataset warmup exceeds the 90-min default grace on exactly the RL jobs this serves. Drop 'any_report' stall mode (conflates connectivity loss with stall); progress records are self-reported by sandboxed code, so server-side validation (monotonicity, bounds, count caps) before any cut decision.
5. **Shadow-mode validation ordering inverted.** Gate the eligibility flip on 'N runners advertising VRAM/CLI', not elapsed time (a clean shadow week with zero advertising runners validates nothing). Throttle runner_dispatch_declines stamping (it writes a task row per held task per dispatch cycle in the hot loop).
6. **Unowned cross-cutting gaps now owned:** 25 MiB artifact cap is enforced on BOTH sides (runner skip + backend 413) — fix both; two phases require alembic migrations + EXPECTED_SCHEMA_REVISION bumps never mentioned; authoring-context additions from four areas need a single token budget + priority order (the plan's cheapest thesis IS better context — do not let it crowd itself out); SVG must leave the preview allowlist (script execution via signed URL); validate runner_target.runner_id against the task's project's enrolled runners.

## REFUTED (plan stands)
- Fleet-uptake/protocol-rev sequencing concern — refuted; the update mechanism supports it.
- Split-pool pin miscount across the task→ComputeJob heartbeat gap — refuted.
- act-on-verdict anchor swap — anchors are co-cited, not swapped; facts the design relies on are real.
