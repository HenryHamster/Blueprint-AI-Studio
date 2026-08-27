# User, Project, and Collaborator Onboarding

This is the canonical guide for joining Blueprint, creating a project from a
**new, non-self GitHub repository**, and adding collaborators. It complements
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

## Who controls what

Platform access and project membership are separate. Being allowed to sign in
does not make someone a platform administrator, and a project role grants no
control over other projects.

| Identity / role | Resources it can use | Typical authority |
|---|---|---|
| Allowlisted user | Blueprint UI plus projects where they have membership | Link a GitHub identity; create a project only for a repo where that identity has GitHub `push` or `admin`; become owner of the project they create. |
| Project viewer | One project's read surfaces | Inspect project state and results; no task or configuration mutation. |
| Project member | One project's ordinary work surfaces | Viewer access plus member-authorized task and direction work; secrets and owner-only settings stay hidden. |
| Project owner | One project's configuration and membership | Manage project configuration, collaborators, automation, and owner-authorized actions. |
| Platform admin | Deployment-wide resources | Configure the server, users, providers, runners, and paid/shared resources such as onboarding-time warm-pool capacity. Admin status does not replace the GitHub App's repository access. |

Invite collaborators from the project's member management UI and give the
least-powerful role they need. Project configuration endpoints document their
exact `viewer`, `member`, or `owner` requirement in
[API_REFERENCE.md](API_REFERENCE.md); deployment-wide settings remain admin-only.

---

## 1. Establish both GitHub identities

Self-service onboarding requires two independent identities. They answer
different security questions and neither substitutes for the other. A trusted
platform administrator may use the API without a linked caller credential, but
the App installation is still required; the **New Project** wizard consistently
requires the linked identity for its preview-first flow.

| Identity | What it proves | Where it is used |
|---|---|---|
| Caller's linked GitHub OAuth credential or fine-grained PAT | The person creating the project has `push` or `admin` permission on this exact repository. | Repository picker and onboarding authorization. The token is stored through the existing user secret reference; it is never placed in the OAuth resume snapshot or onboarding payload. |
| Blueprint GitHub App installation | The Blueprint service can reach the repository with its configured Actions, Contents, Pull requests, Commit statuses, Issues, and Checks permissions. | Repository detection, initialization, agent Git operations, checks, issues, PR workflows, and bounded promotion-CI reruns. |

In **New Project**, connect your GitHub account first. The repository picker is
the intersection of repositories writable by your linked identity and
repositories reachable by the App. Manual entry does not bypass the same
server-side checks. While OAuth redirects away, the wizard preserves an explicit
allowlist of form fields in `sessionStorage` and never serializes a credential or
token field; free-form project text should still not contain secrets. It reopens
the snapshot only after a successful `github_connected` return.

Then install the platform's GitHub App on the repository (or on the org, scoped
to include it):

1. Go to the GitHub App's public page (the App you created in
   [Deploy.md → GitHub App Setup](../Deploy.md#github-app-setup)).
2. Click **Install** / **Configure** and grant access to the target repository.
3. Confirm the App has the required repository permissions: Actions (R&W),
   Contents (R&W), Pull requests (R&W), Commit statuses (R&W), Issues (R&W),
   Checks (R). Existing installations must approve permission changes before
   Blueprint can use them.

### How the App installation token is resolved

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

**Consequence for onboarding:** after caller authorization succeeds, the onboard
endpoint still needs an App token to detect the repo's default branch. If the
App cannot reach the repository, it responds with **HTTP 422**:

```
422 Unprocessable Entity
{"error":{"code":422,"message":"Your GitHub authorization is valid, but the Blueprint GitHub App is not installed for this repository."}}
```

This is distinct from **403**, which means the caller's linked identity lacks
`push`/`admin` or is not linked, and from **502/503**, which indicate a transient
GitHub or credential-resolution failure.

---

## 2. Create the project

Create the project through the admin UI **New Project** wizard
(`admin-ui/src/components/NewProjectModal.tsx`), which calls
`POST /api/admin/projects/onboard` (`OnboardPayload` in `backend/app/schemas.py`).
The endpoint accepts any authenticated, allowlisted user; non-admin callers must
pass the caller GitHub permission check, while a trusted platform admin has the
documented API bypass. The new project records the caller as `owner`. It is
rate-limited to 10/min. Only the optional onboarding-time warm-pool mutation is
platform-admin-only.

### Fields the operator provides

| Field | Required | Description |
|-------|----------|-------------|
| `github_owner` | ✅ | GitHub org/user (the wizard parses this from `owner/repo` or a full URL). |
| `github_repo` | ✅ | Repository name. |
| `preview_only` | — | `true` performs authorization, App reachability, empty-repo initialization, default-branch lookup, and build detection but creates no Blueprint project. The wizard uses this before its confirmation step. |
| `name` | — | Display name. Defaults to `{owner}/{repo}` when omitted. |
| `build_config_override` | — | A `BuildConfig`-shaped dict merged **on top of** auto-detection (override wins). The wizard sends the operator-edited merge-gate draft shown after preview. |
| `promotion_target` | — | Promotion/LTS branch. The wizard's "Promotion target" input maps here; blank means "same as integration". |
| `goal` | — | User purpose (up to 5,000 characters), seeded into strategic intent and the project conversation. |
| `initial_steering` | — | Optional `context` and `constraints` used to ground the first strategic cycle; unsupported keys are ignored. |
| `warm_pool_target_size` | — | Enables and seeds paid warm capacity for this project. Platform-admin-only; normal project creation does not require it. |

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
  - **Safe onboarding default:** the onboard handler explicitly creates new
    projects with `autonomous_enabled=False`. The legacy model default remains
    unchanged for existing code paths, but self-service onboarding does not begin
    autonomous task spawning until a project owner deliberately enables it after
    reviewing repository access, build/merge verification, resource limits, and
    approval gates.

### Preview, creation result, and failures are different

| Outcome | Durable effect | Response / operator action |
|---|---|---|
| Preview success (`preview_only=true`) | No Blueprint `Project`, membership, goal message, warm-pool setting, or bootstrap task is created. An empty GitHub repo may receive its required initial commit because branch detection must target a real branch. | Returns `build_config`, `default_branch`, and `merge_verification_verdict`. Review and edit the draft before creation. |
| Create success | Creates the project in `DEPLOYING` status with `autonomous_enabled=False`, creates owner membership, stores branch/build configuration, seeds disabled automation and strategic intent, and may spawn the best-effort bootstrap task even though recurring autonomy is off. | Returns HTTP 200 with `project_id`, `build_config`, `default_branch`, and `merge_verification_verdict`. When an admin requested `warm_pool_target_size`, `warm_pool` additionally reports either the enabled target/seed summary or `{ "enabled": false, "error": "setup_failed" }`; that optional post-create failure does not roll back the project. |
| Authorization failure | No project is created. | For non-admin self-service, `403`: link GitHub or use an identity with `push`/`admin` to the exact repo. `401`: linked credential expired; reconnect. A trusted platform admin is the explicit caller-check bypass, but still needs App reachability. |
| App reachability failure | No project is created. | `422`: install/configure the Blueprint App for the repo. This is not a caller-role failure. |
| Request validation failure | No project is created. | `422` with `"message":"Validation Error"` and a `details` array means the payload itself is invalid (for example, an out-of-range warm-pool size), not that the App is unreachable. |
| Rate or dependency failure | No project is created unless an explicitly documented post-create optional action reports a warning. | `429`: retry after GitHub/rate-limit window. `502/503`: GitHub, App token, default-branch, initialization, or stored-credential service was unavailable; retry or inspect server configuration. |
| Build detection incomplete | Creation can proceed safely with a durable `detection_incomplete` marker. | The landing-time refresh retries detection; distinguish this from an intentionally test-less repo, whose completed detection records that fact without the marker. |

The wizard presents these distinctions separately: caller connection, App
installation reachability, preview errors, editable detected configuration, and
the final creation result are not collapsed into a generic "GitHub failed."

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

When repository access, verification, limits, and approval gates are ready,
enable only the pipelines you intend to operate. New onboarded projects begin
with autonomous spawning and all three automation pipelines off:

- `PATCH /projects/{project_id}/auto-remediation` — auto-fix CI failures on the
  integration branch.
- `PATCH /projects/{project_id}/issue-implementation` — spawn tasks from labeled
  issues.
- `PATCH /projects/{project_id}/auto-review` — admit eligible opened/synchronized
  PRs to review when webhook targeting, branch, rate, concurrency, dedupe, and
  project-policy checks pass. Enabling it is not a promise that every PR is
  reviewed.
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

1. Link the creating user's GitHub OAuth identity or fine-grained PAT; confirm it
   has `push` or `admin` permission on the exact repository.
2. Install the platform GitHub App on the target repo
   (Actions/Contents/PRs/Statuses/Issues/Checks) and confirm `GITHUB_APP_ID` /
   `GITHUB_PRIVATE_KEY` are configured server-side.
3. Create the project via **New Project** (or
   `POST /api/admin/projects/onboard`). Treat 403 caller authorization, 422 App
   reachability or payload validation (distinguished by the response message),
   and 502/503 dependency failures as different problems.
4. Review the previewed build config, merge-verification verdict, and default
   branch before creation.
5. Tune `integration_branch` / `promotion_target` / namespace / prefixes if the
   repo uses a non-default branch model.
6. Keep the safe onboarding default while validating the project:
   `autonomous_enabled`, `auto-remediation`, `issue-implementation`, and
   `auto-review` all start off. Owners should enable each deliberately after
   resource limits and approval policy are ready.
7. Set `approval_gate_config` (`infra_files` / `critical_files`) via
   `PATCH /api/admin/projects/{project_id}/approval-gate-config` if your repo has
   sensitive paths that should require human approval before merge.
8. Understand the release model (section 5): PRs merge to the integration branch
   only when CI **and** review both pass, and integration-branch work promotes to
   the promotion target only when the release qualifier's confidence clears its
   threshold. Watch it via `GET /api/admin/projects/{project_id}/version-lifecycle`.

See also: [API_REFERENCE.md — Project Onboarding](API_REFERENCE.md#project-onboarding)
and [DATA_MODELS.md — Project](DATA_MODELS.md#project).
