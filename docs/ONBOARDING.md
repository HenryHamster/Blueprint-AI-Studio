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
| `build_config_override` | — | A `BuildConfig`-shaped dict merged **on top of** auto-detection (override wins). This is where you can supply `build_config.migration` for a non-standard Python layout; omit it to fall back to Blueprint defaults. The wizard does not send this today. |
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
- **`build_config.repo_is_empty`** is a **deterministic empty/near-empty repo
  signal** computed by `_classify_repo_emptiness` from the same root listing
  `_detect_build_config` already fetched (zero extra GitHub cost). It is `True`
  when the repo has no source directories and every root file is a recognized
  non-source scaffolding/config file (`README*`, `LICENSE*`, `CONTRIBUTING*`,
  `CODE_OF_CONDUCT*`, `.gitignore`, `.gitattributes`, CI/editor dotfiles; the
  `.github`/`.git` dirs are ignored), else `False`. It is surfaced on the agent
  claim response as the top-level `repo_is_empty` field, where the research/plan
  prompt uses it to gate the "nearly empty → bootstrap the foundation from the
  goal" guidance — greenfield agents reliably get it; mature-repo agents stop
  seeing it as noise. This is distinct from `first_task_on_repo` (a playbook-count
  proxy): `repo_is_empty` reflects actual source presence. Re-detecting build
  config (`POST /projects/{id}/detect-build-config`) refreshes the value, so once
  a greenfield repo is scaffolded the signal flips to `False`. Pre-existing
  projects without the field degrade gracefully to falsy.
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
- `migration` — agent-side migration-layout override for repos that do not use
  Blueprint's default Python layout. Set it either in `build_config_override`
  during onboarding or later via the project `build_config` PATCH endpoint.
  Fields are all optional: `versions_dir`, `models_dir`, `alembic_cwd`,
  `schema_revision_file`, `schema_revision_const`. Omit the object to use
  Blueprint defaults (`backend/alembic/versions`, `app/models`, `app/main.py`);
  an empty `schema_revision_file` remains a meaningful "no schema-revision gate"
  signal. This is separate from `alembic_versions_path`, which only feeds the
  backend migration-file guard.
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
response seam. The review and PR merge paths require human approval before
merging changes to configured sensitive files.

Blueprint's own self-repo is seeded with default infra/critical file lists by
Alembic migration `0120_seed_self_repo_approval_gate_config.py` (only where
`is_self_repo` is true). A freshly onboarded non-self project starts with empty
lists, so configure them via the PATCH endpoint above if your repo has sensitive
infra/critical paths that should require approval.

---

## 5. The release model — how a change reaches production

Once a project is onboarded and the pipelines are on, Blueprint moves a change
through **two distinct gates** on the way to your release branch. Both gates are
defined in terms of the project's *configured* branches — the
**integration branch** and the **promotion target** you set in section 3 — so
the model is identical for any repo regardless of what those branches are named.

> **Concrete example (Blueprint's own self-repo):** integration branch is
> `automain`, promotion target is `main`. Everywhere below that says
> "integration branch" / "promotion target", read `automain` / `main` for the
> self-repo. For your repo, substitute the values you configured via
> `PATCH /integration-branch` and `PATCH /promotion-target`.

### Gate 1 — the two-flag merge gate (PR → integration branch)

Every agent does its work on a task branch (`{namespace}/{prefix}-{hex}`) and
opens a PR **targeting the integration branch**. That PR merges only when **both**
independent gates have opened on the task's review loop
(`backend/app/ci_completion.py`):

- **`ci_passed`** — CI reported success for the PR's head commit.
- **`review_approved`** — a review task signed off on the change.

Whichever flag is set *second* triggers the merge. Neither alone is sufficient:
a green CI run with no approval will not merge, and an approval with red/pending
CI will not merge. (For changes that touch paths in `approval_gate_config`, human
approval is additionally required before merge — see section 4.) The result is
that the integration branch only ever accumulates work that has passed both CI
and review.

### Gate 2 — the qualify → promote release gate (integration branch → promotion target)

Merging to the integration branch does **not** ship to production. Promotion of
accumulated integration-branch work to the **promotion target** is governed by
the **release qualifier** (`backend/app/release_qualifier.py`), which scores the
integration branch HEAD and only promotes when a composite **confidence** score
clears a configured **threshold**.

Blueprint tracks this with a `ReleaseVersion` record per project (a "version" is
the span of the integration branch between two promotions). A version moves
through phases:

- **`build`** — normal feature work landing on the integration branch.
- **`stabilize`** — work-mix shifts to audit / cleanup / hardening ahead of a
  qualification run.
- **qualify** — the heavy gate runs (once per version): the qualifier computes a
  composite confidence from sub-scores (release audit, test-erosion, smoketest)
  and writes an immutable `QualificationVerdict` row. Hard blockers (e.g. a
  deleted test file) force confidence to `0.0`.
- **promote** — when confidence clears the threshold **and** promotion is enabled
  (`runtime_config["promotion_enabled"]`), the candidate is promoted to the
  promotion target and the next version opens.

> Until `promotion_enabled` is turned on, qualification runs **dark**: a passing
> candidate records a `dry_run` verdict instead of actually promoting. This lets
> you calibrate the threshold against a real track record before handing the
> qualifier the promotion lever.

### Observing and triggering the release lifecycle

Operators drive and inspect this from the release admin endpoints (all under
`/api/admin/projects/{project_id}`; see
[API_REFERENCE.md — Release Version Lifecycle](API_REFERENCE.md#release-version-lifecycle)):

- **`GET /version-lifecycle`** — the current open version (phase, qualification
  attempts, merged-task count, last verdict), recent closed versions, and whether
  `promotion_enabled` is on. Start here to see where a project sits.
- **`POST /version-phase`** — move the open version to `build` or `stabilize`.
- **`POST /qualify`** (`admin_operator_triggers.py`) — run the qualifier now
  against the open version instead of waiting for the strategic agent's
  conservative stall threshold. Combined with `version-phase`, this lets you
  advance to `stabilize` and produce a (dry-run) `QualificationVerdict`
  end-to-end on demand.
- **`GET /qualification-verdicts`** — the durable per-verdict history (confidence,
  sub-scores, blocker, would-promote).
- **`GET /audit-findings`** — the release-audit findings behind a verdict, so a
  low score is explainable.

See [DATA_MODELS.md — Release Lifecycle Models](DATA_MODELS.md#release-lifecycle-models)
for the `ReleaseVersion` and `QualificationVerdict` schemas.

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
8. Understand the release model (section 5): PRs merge to the integration branch
   only when CI **and** review both pass, and integration-branch work promotes to
   the promotion target only when the release qualifier's confidence clears its
   threshold. Watch it via `GET /api/admin/projects/{project_id}/version-lifecycle`.

See also: [API_REFERENCE.md — Project Onboarding](API_REFERENCE.md#project-onboarding)
and [DATA_MODELS.md — Project](DATA_MODELS.md#project).
