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
| Build & push the agent image to the job | **operator** | Cloud Build (manual / new config) |
| `CLOUD_RUN_JOB_NAME` env on the orchestrator | **operator** | `backend/cloudbuild.yaml` `--set-env-vars` |
| Enabling the allowlist | **operator** | `PATCH /api/admin/settings` |

> The Job's env-var **secrets** are declared in Terraform (it does not deploy
> via cloudbuild like the orchestrator service does), but the orchestrator's
> own `CLOUD_RUN_JOB_NAME` value is added to `cloudbuild.yaml` — Cloud Run
> service env-var content is owned there, not by Terraform (see the WARNING
> block in `cloud_run.tf`).

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
   `roles/run.developer` IAM binding, with **no changes** to the existing
   `google_cloud_run_v2_service.orchestrator` service.

3. **Build & push the agent image into the job.** The job is created with the
   `cloudrun/container/hello` placeholder (and `ignore_changes` on the image,
   so Terraform will not revert your push). Build the agent container from
   `agent/src` and deploy it to the job, e.g.:

   ```bash
   gcloud builds submit agent \
     --tag us-central1-docker.pkg.dev/$PROJECT_ID/blueprint-repo/blueprint-agent-job:latest
   gcloud run jobs update blueprint-agent-job --region us-central1 \
     --image us-central1-docker.pkg.dev/$PROJECT_ID/blueprint-repo/blueprint-agent-job:latest
   ```

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

7. **Watch per-type cost attribution** via `./fetch vm-cost-by-task-type` for
   24–48h. Confirm the allowlisted types execute on Cloud Run (no dedicated
   `e2-standard-2` VM) and complete successfully.

8. **Expand the allowlist** to short Tier A3 types once healthy — append:

   ```text
   review, panel_review, debug, perf, smoketest, verify_tests
   ```

   (`smoketest` / `verify_tests` get the heavier 2 vCPU / 4 GiB execution
   shape automatically via the per-execution timeout/override logic.)

9. **Rollback** is immediate and per-type: remove any task type from
   `cloud_run_task_type_allowlist` and it reverts to the GCE VM path on the
   next dispatch cycle — no redeploy, no Terraform change required. To fully
   tear down, `terraform destroy -target=google_cloud_run_v2_job.blueprint_agent_job`
   after the allowlist is empty.
