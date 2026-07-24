# Activating the Cloud Run Jobs lightweight execution path

This is the operator runbook for turning on Cloud Run Jobs dispatch for
read-only / non-merging task types. The backend routing code and the
Terraform job resource ship **dark** — applying them changes no task routing
until the steps below are completed.

## What ships in code vs. what operators do

| Concern | Owner | Where |
| --- | --- | --- |
| Dispatch routing (`launch_agent_cloud_run_job`) | code (dark-gated) | `backend/app/compute_cloud_run.py` |
| Allowlist gate (`cloud_run_task_type_allowlist`, empty by default) | code (dark-gated) | runtime settings |
| Cloud Run Job resource + IAM | Terraform | `infra/terraform/cloud_run.tf` (`google_cloud_run_v2_job.blueprint_agent_job`) |
| Build, push, and deploy the agent image | CI on `agent/**` changes | `.github/workflows/deploy.yml` → `agent/Dockerfile` |
| `CLOUD_RUN_JOB_NAME` env on the orchestrator | **operator** | `backend/cloudbuild.yaml` `--set-env-vars` |
| Enabling the allowlist | **operator** | `PATCH /api/admin/settings` |

> The Job declares only **non-secret** base env in Terraform; it bakes **no**
> credentials. The agent resolves its LLM/JWT credentials at runtime from Secret
> Manager using the `AGENT_SECRET_VERSION` the backend injects per execution
> (authenticated by the job's service-account token), so no secrets live on the
> job. The orchestrator's own `CLOUD_RUN_JOB_NAME` value is added to
> `cloudbuild.yaml` — Cloud Run *service* env-var content is owned there, not by
> Terraform (see the WARNING block in `cloud_run.tf`).

## Enable Cloud Run Jobs for read-only task types

1. **Confirm the companion code PR is promoted to `main`.** The dispatch path
   (`compute_cloud_run.py`) and the empty-allowlist gate must be live on the
   serving revision before the job is useful.

2. **Apply the Terraform.** From `infra/terraform`:

   ```bash
   cd infra/terraform
   terraform apply -target=google_cloud_run_v2_job.blueprint_agent_job \
                   -target=google_cloud_run_v2_job_iam_member.orchestrator_runs_agent_job
   ```

   The plan must show a new `google_cloud_run_v2_job` resource and a
   `roles/run.jobsExecutorWithOverrides` IAM binding (orchestrator SA → job),
   with **no changes** to the existing
   `google_cloud_run_v2_service.orchestrator` service. (A targeted apply was
   verified safe — it touches only the job + its new `agent-job-sa`.)

3. **Deploy the agent image into the job.** The job is created with the
   `cloudrun/container/hello` placeholder (and `ignore_changes` on the image,
   so Terraform will not revert a deployed image). Merging an `agent/**`
   change to `main` builds a SHA-tagged image and updates the job through
   `.github/workflows/deploy.yml`. The image is built from
   **`agent/Dockerfile`** — a deliberately **read-only-only** image (Node 20 +
   the LLM CLIs + the agent app; no docker/postgres/playwright/pytest). Do NOT
   route heavy/write types to it (see step 8). Consider pinning the LLM-CLI
   versions aligned with the deployed VM image
   (`CLAUDE_CODE_VERSION` / `CODEX_VERSION` / `GEMINI_VERSION`). For an
   emergency manual rollout, build and point the job at an immutable tag:

   ```bash
   gcloud builds submit agent \
     --tag us-central1-docker.pkg.dev/$PROJECT_ID/blueprint-repo/blueprint-agent-job:$GIT_SHA
   gcloud run jobs update blueprint-agent-job --region us-central1 \
     --image us-central1-docker.pkg.dev/$PROJECT_ID/blueprint-repo/blueprint-agent-job:$GIT_SHA
   ```

   **Prerequisite:** the job's service account (`agent-job-sa`) must have
   `roles/secretmanager.secretAccessor` on the agent credential secret, or
   `resolveCredentialsFromSecretVersion` fails before the task is claimed. Grant
   it if the Terraform doesn't already.

4. **Verify the job exists** and is healthy:

   ```bash
   gcloud run jobs describe blueprint-agent-job --region us-central1
   ```

5. **Wire `CLOUD_RUN_JOB_NAME` into the orchestrator.** Add to
   `backend/cloudbuild.yaml`'s `--set-env-vars` list and redeploy the
   orchestrator service:

   ```text
   CLOUD_RUN_JOB_NAME=projects/$PROJECT_ID/locations/us-central1/jobs/blueprint-agent-job
   ```

   `compute_cloud_run.py` reads this exact fully-qualified form; an unset or
   malformed value makes dispatch fail closed (the task falls back to the GCE
   path / provision failure handling).

6. **Enable Tier A1 types first** (lowest risk — these never `git clone`):

   ```http
   PATCH /api/admin/settings
   {"cloud_run_task_type_allowlist": ["memo", "web_research"]}
   ```

7. **Watch per-type cost attribution** via the agent endpoint
   `GET /projects/{project_id}/vm-cost` (`compute_vm_cost_by_task_type` in
   `efficiency_snapshot.py`) for 24–48h. (There is no `./fetch` CLI — that
   earlier reference was aspirational.) Confirm the allowlisted types execute on
   Cloud Run (no dedicated `e2-standard-2` VM) and complete successfully.

8. **Expand the allowlist** to the other light/zero-checkout read-only types once
   healthy — append:

   ```text
   review, debug, perf
   ```

   > ⚠️ **Do NOT add `smoketest` / `verify_tests` / `compute_experiment` (or any
   > write type).** A previous version of this runbook claimed these "get the
   > heavier 2 vCPU / 4 GiB shape automatically" — that is **not implemented**:
   > `_resolve_machine_type` only stamps a cost-attribution sentinel string, the
   > `RunJobRequest` sets no `resources` override, and the job template is fixed
   > at 1 vCPU / 2 GiB. They also need docker-in-docker (`scripts/test-backend.sh`),
   > which `agent/Dockerfile` deliberately omits. Routing them here would OOM /
   > fail. Enabling them is a **follow-on project**: a real per-execution
   > `resources` override (code) **plus** a separate heavier image with docker —
   > not part of this activation.

9. **Rollback** is immediate and per-type: remove any task type from
   `cloud_run_task_type_allowlist` and it reverts to the GCE VM path on the
   next dispatch cycle — no redeploy, no Terraform change required. To fully
   tear down, `terraform destroy -target=google_cloud_run_v2_job.blueprint_agent_job`
   after the allowlist is empty.
