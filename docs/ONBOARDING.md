# Onboarding a New Repository

This guide walks an operator through bringing a **new, non-self GitHub
repository** under Blueprint management, end to end. It complements
[Deploy.md](../Deploy.md), which covers standing up the platform itself
(creating the GitHub App, deploying to Cloud Run, granting admin). Once the
platform is running, follow the steps below for each repository you want
Blueprint to work on.

Everything here is grounded in the actual columns and routers:
`backend/app/routers/admin_onboard.py`, `admin_project_config.py`,
`admin_projects.py`, `backend/app/schemas.py`, `backend/app/models.py`,
`backend/app/github_auth.py`, and `admin-ui/src/components/NewProjectModal.tsx`.

> Blueprint is repo-agnostic: there is no requirement that the target be
> Blueprint's own repo. The self-repo (`is_self_repo = true`) only differs in a
> few seeded defaults, noted explicitly below.

---

## 1. Install the GitHub App on the target repository

Blueprint authenticates to each repository as a **GitHub App installation**, not
as a user. Before onboarding a repo, install the platform's GitHub App on it (or
on the org, scoped to include the repo):

1. Go to the GitHub App's public page (the App you created in
   [Deploy.md → GitHub App Setup](../Deploy.md#github-app-setup)).
2. Click **Install** / **Configure** and grant access to the target repository.
3. Confirm the App has the required repository permissions: Contents (R&W),
   Pull requests (R&W), Commit statuses (R&W), Issues (R&W), Checks (R).

### How the installation token is resolved

The backend resolves a short-lived installation token on demand via
`get_repo_installation_token(owner, repo)` in `backend/app/github_auth.py`
(re-exported from `app.github`):

- It signs a GitHub App JWT using the `GITHUB_APP_ID` and `GITHUB_PRIVATE_KEY`
  environment variables (`generate_github_app_jwt`). If either is unset, the
  function returns `None` immediately — no token can be minted.
- It discovers the installation id for `{owner}/{repo}`
  (`GET /repos/{owner}/{repo}/installation`, cached for 24h) and exchanges the
  JWT for an installation access token scoped to that repo.
- If the App is **not installed** on the repo, the installation lookup returns a
  non-200 and `get_repo_installation_token` returns `None`.

**Consequence for onboarding:** the onboard endpoint needs a token to detect the
repo's default branch. If no installation token is available, it responds with
**HTTP 422**:

```
422 Unprocessable Entity
{"detail": "No installation token available — cannot detect default branch."}
```

So if onboarding returns 422, the fix is almost always "install the App on the
repo (and verify `GITHUB_APP_ID` / `GITHUB_PRIVATE_KEY` are set in the backend
environment)."

---

## 2. Create the project

Create the project through the admin UI **New Project** wizard
(`admin-ui/src/components/NewProjectModal.tsx`), which calls
`POST /api/admin/projects/onboard` (`OnboardPayload` in `backend/app/schemas.py`).
The endpoint requires an admin caller and is rate-limited to 10/min.

### Fields the operator provides

| Field | Required | Description |
|-------|----------|-------------|
| `github_owner` | ✅ | GitHub org/user (the wizard parses this from `owner/repo` or a full URL). |
| `github_repo` | ✅ | Repository name. |
| `name` | — | Display name. Defaults to `{owner}/{repo}` when omitted. |
| `build_config_override` | — | A `BuildConfig`-shaped dict merged **on top of** auto-detection (override wins). The wizard does not send this today. |
| `promotion_target` | — | Promotion/LTS branch. The wizard's "Promotion target" input maps here; blank means "same as integration". |

### Fields the handler auto-derives (not operator input)

The `onboard_project` handler in `admin_onboard.py` derives the rest:

- **`integration_branch` and `promotion_target`** default to the repo's **default
  branch** (resolved with `get_repo_default_branch` using the installation
  token). An explicit `promotion_target` in the payload overrides the default.
- **`build_config`** is **auto-detected** by `_detect_build_config`, which
  inspects the repo root and one level of subdirectories for `pyproject.toml`,
  `package.json`, `go.mod`, `Cargo.toml`, etc., and seeds per-directory
  install/lint/build/test commands. The detected dict is validated against the
  `BuildConfig` schema before being stored.
- **`task_branch_namespace`** is **slugified from the repo name** by
  `_derive_task_branch_namespace` (lowercased, non-alphanumerics → `-`, capped at
  40 chars) and stored inside `build_config`. Agent task branches are then named
  `{namespace}/{prefix}-{hex}`.
- **The design-doc automation pipelines are seeded OFF.** The onboarding flow
  writes `_auto_remediation`, `_issue_implementation`, and `_auto_review` config
  blocks into `design_doc` all with `enabled: false`, so none of those three
  pipelines act until you opt in (step 3).
  - **Caveat — `autonomous_enabled` is NOT disabled by onboarding.** The
    `Project.autonomous_enabled` column defaults to `True`
    (`backend/app/models.py`) and the onboard handler never sets it, so a freshly
    onboarded non-self project has `autonomous_enabled=True`. Autonomous task
    spawning is therefore enabled by default; flip it off via
    `PATCH /projects/{project_id}/autonomous` (`{ "enabled": false }`) if you want
    to gate it before the other pipelines are turned on.

On success the endpoint returns `{ "project_id", "build_config", "default_branch" }`,
and the wizard shows the detected build config plus toggles for the automation
features.

---

## 3. Tune the branch model and build config after onboarding

Onboarding seeds sensible defaults; per-project configuration endpoints let you
refine them. All live in `backend/app/routers/admin_project_config.py` under
`/api/admin/projects/{project_id}`. **GET** requires `member` role; **PATCH**
requires `owner`. For every "set" endpoint, sending `null` reverts the field to
its default, and the response carries an `is_default` flag.

### Branch model

| Endpoint | Purpose |
|----------|---------|
| `PATCH /integration-branch` | Trunk used for autonomous task spawning. PATCH validates the name and confirms the branch **exists on the remote** (422 if not, 502 if the GitHub credential is unavailable). |
| `PATCH /promotion-target` | Promotion/LTS target branch (e.g. `main` while integration is a staging trunk). |
| `PATCH /task-branch-namespace` | Leading segment of agent task branches. Must be a single segment (no `/`). |
| `PATCH /task-branch-prefix` | Prefix within the namespace; defaults to `{namespace}/`. |
| `PATCH /staging-branch-prefixes` | Branch prefixes that route into rollup/auto-merge (default `["research/", "plan/"]`). |

### Build config (project PATCH endpoint)

The broader `build_config` blob is edited through
**`PATCH /api/admin/projects/{project_id}`** (`ProjectUpdate` in
`admin_projects.py`; `build_config` is an owner-only field). The body's
`build_config` is validated against the `BuildConfig` schema and merged with the
existing value. Notable knobs (see `BuildConfig` in `schemas.py`):

- `task_branch_namespace`, `task_branch_prefix`, `pr_title_prefix`
- `staging_branch_prefixes`, `stale_branch_prefixes`, `protected_branches`
  (stale-branch cleanup prefixes and protected branches that must never be
  deleted/force-pushed)
- `enable_self_improvement` — opt a non-self project into self-improvement mode
- `directories`, `env_vars`, `git_user_name`, `git_user_email`

### Automation opt-in

When you're ready for Blueprint to act autonomously, enable the pipelines (all
default off):

- `PATCH /projects/{project_id}/auto-remediation` — auto-fix CI failures on the
  integration branch.
- `PATCH /projects/{project_id}/issue-implementation` — spawn tasks from labeled
  issues.
- `PATCH /projects/{project_id}/auto-review` — auto-review opened PRs.
- `PATCH /projects/{project_id}/autonomous` — toggle `autonomous_enabled`
  (`{ "enabled": true }`).

---

## 4. Configuring the file-path approval gate (`approval_gate_config`)

`Project.approval_gate_config` (`backend/app/models.py`) holds the file-path
approval gate:

```python
# Shape: {"infra_files": [...], "critical_files": [...]}
approval_gate_config: Dict  # default {}
```

At PR time, `task_lifecycle_github_helpers._get_approval_gate_file_lists` reads
`infra_files` / `critical_files` from this column to decide which changed files
require human approval before merge. **When the column is empty, both sets are
empty — every changed file skips the file-based approval gate.**

Set the lists per project through the admin API, following the same per-project
GET/PATCH config pattern as `integration-branch` and `promotion-target`:

- **`GET /api/admin/projects/{project_id}/approval-gate-config`** (member) returns
  `{ "approval_gate_config": { "infra_files": [...], "critical_files": [...] },
  "is_default": bool }`.
- **`PATCH /api/admin/projects/{project_id}/approval-gate-config`** (owner-only)
  sets either or both lists (partial update). Each entry must be a plausible
  repo-relative path string — no leading `/`, no `..` traversal, no shell
  metacharacters — or the request returns `422`.

Writes take effect for agents **immediately** through the existing task-claim
response seam, with no agent-side change: the agent's fresh-reviewer escalates
review effort when a sensitive path is touched, and the PR merge path requires
human approval before merging changes to those files.

Blueprint's own self-repo is seeded with default infra/critical file lists by
Alembic migration `0120_seed_self_repo_approval_gate_config.py` (only where
`is_self_repo` is true). A freshly onboarded non-self project starts with empty
lists, so configure them via the PATCH endpoint above if your repo has sensitive
infra/critical paths that should require approval.

---

## Quick checklist

1. Install the platform GitHub App on the target repo (Contents/PRs/Statuses/Issues/Checks).
2. Confirm `GITHUB_APP_ID` and `GITHUB_PRIVATE_KEY` are set in the backend env.
3. Create the project via the admin UI **New Project** wizard (or
   `POST /api/admin/projects/onboard`). A 422 means the App isn't installed / no token.
4. Review the auto-detected build config and default branch.
5. Tune `integration_branch` / `promotion_target` / namespace / prefixes if the
   repo uses a non-default branch model.
6. Enable the design-doc automation pipelines (`auto-remediation`,
   `issue-implementation`, `auto-review`) when ready — these are seeded off. Note
   that `autonomous_enabled` defaults **on**, so disable it via
   `PATCH /projects/{project_id}/autonomous` first if you want to gate autonomous
   spawning before turning the pipelines on.
7. Set `approval_gate_config` (`infra_files` / `critical_files`) via
   `PATCH /api/admin/projects/{project_id}/approval-gate-config` if your repo has
   sensitive paths that should require human approval before merge.

See also: [API_REFERENCE.md — Project Onboarding](API_REFERENCE.md#project-onboarding)
and [DATA_MODELS.md — Project](DATA_MODELS.md#project).
