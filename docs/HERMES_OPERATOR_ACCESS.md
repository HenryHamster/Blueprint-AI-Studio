# Project-scoped operator access for Hermes

Blueprint's `project-operator` ServiceCredential profile gives Hermes a useful
operator CLI without making it a platform administrator. It authenticates as a
fresh, revocable, non-owner service principal and receives only the explicit
`member` memberships named when the credential is minted.

This profile is for a trusted developer-hosted Hermes process. Blueprint task
workers still use their short-lived task token and runtime helpers. Do not put
the reusable operator token in a repository, prompt, task environment, or MCP
registration.

## Grant and configure

An administrator mints the credential with a separate operator config. The
plaintext token is shown once:

```powershell
$env:BLUEPRINT_CONFIG = "C:\secure\blueprint\platform-operator.json"
.\blueprint.ps1 agent-access grant hermes-project-operator `
  --profile project-operator `
  --project-id PROJECT-UUID `
  --description "Hermes project operator" `
  --expires-in-days 30 `
  --yes
```

Store the result outside the repository, for example
`C:\secure\blueprint\hermes-project-operator.json`:

```json
{
  "api_url": "https://YOUR-BLUEPRINT-HOST",
  "api_key": "bpsc_TOKEN_SHOWN_ONCE",
  "project_id": "PROJECT-UUID",
  "read_only": false
}
```

Select this file only for the Hermes CLI process:

```powershell
$env:BLUEPRINT_CONFIG = "C:\secure\blueprint\hermes-project-operator.json"
.\blueprint.ps1 agent-access whoami --json
.\blueprint.ps1 projects --json
.\blueprint.ps1 agent-access snapshot --project-id PROJECT-UUID --json
```

The `project_id` in the config is a default, not additional authority. Every
backend request rechecks the credential principal's ProjectMember row. Prefer
an explicit `--project-id` on mutations, especially when one credential has
several memberships.

## Supported reads

The profile inherits the bounded, redacted agent-access projections:

- authorized project catalog and effective identity;
- project snapshot, attention, health diagnostics, and resource availability;
- paginated task lists, one bounded task detail, and PR/task failure context;
- paginated operator-visible messages;
- paginated bounded directions.

Examples:

```powershell
.\blueprint.ps1 status --project-id PROJECT-UUID --json
.\blueprint.ps1 diagnostics --project-id PROJECT-UUID --json
.\blueprint.ps1 tasks list --project-id PROJECT-UUID --active --limit 50 --json
.\blueprint.ps1 tasks show TASK-UUID --json
.\blueprint.ps1 directions list --project-id PROJECT-UUID --limit 50 --offset 0 --json
.\blueprint.ps1 messages list --project-id PROJECT-UUID --limit 50 --offset 0 --json
```

The Blueprint MCP remains read-only even when it uses a `project-operator`
credential. It may use the same bounded snapshot/task/message/direction tools;
all mutations go through the explicit CLI commands below.

## Supported mutations

Task creation requires an existing authorized project. The backend derives the
repository and base branch from that project; scoped callers cannot supply a
repo, base/release branch, model/provider, effort, dependency gate, hotfix, or
local evidence file. `migrate` and non-user-launchable task types are rejected.

```powershell
.\blueprint.ps1 tasks create "Fix retry accounting" "Correct the retry counter." `
  --project-id PROJECT-UUID `
  --type create `
  --acceptance-behavior "A failed retry increments the durable counter once" `
  --idempotency-key hermes-retry-accounting-v1 `
  --yes

.\blueprint.ps1 tasks requeue TASK-UUID `
  --guidance "The failure is in the idempotent retry path; preserve existing tests." `
  --yes

.\blueprint.ps1 tasks request-changes TASK-UUID `
  "Add the missing cross-project isolation regression." `
  --action fix --yes

.\blueprint.ps1 tasks cancel TASK-UUID `
  --reason "Superseded by the corrected task" --yes
```

Cancellation first retrieves its exact child/dependent impact. If the impact
is non-empty, inspect it and repeat with `--confirm-cascade`; the backend also
requires that acknowledgement. This is not a bulk-cancel capability.

One active criterion on the open version may be corrected without changing its
key or references. The caller must supply the exact current and replacement
text; both files are read as raw UTF-8, including newlines. The backend locks
the exact project/version/key row, rejects a stale compare or duplicate text,
and returns hashes plus the preserved row lineage for CLI verification.

```powershell
.\blueprint.ps1 criteria rewrite c-1234abcd `
  --project-id PROJECT-UUID `
  --expected-text-file .\criterion-old.txt `
  --text-file .\criterion-new.txt --yes
```

The scoped route cannot change the criterion key, lifecycle status, evidence
contract, authorship, release version, or direction mappings. The equivalent
admin route requires project-owner membership.

Directions support strict create/update and staged lifecycle actions:

```powershell
.\blueprint.ps1 directions add "Improve retry observability" `
  --project-id PROJECT-UUID --priority high `
  --paths backend/app backend/testing `
  --completion-criteria "Failures identify the retry stage" `
  --idempotency-key hermes-direction-retry-observability --staged --yes

.\blueprint.ps1 directions update d-1234abcd `
  --project-id PROJECT-UUID --priority medium `
  --paths backend/app backend/testing --yes
.\blueprint.ps1 directions retarget d-1234abcd 42 --project-id PROJECT-UUID --yes
.\blueprint.ps1 directions activate d-1234abcd --project-id PROJECT-UUID --yes
.\blueprint.ps1 directions cancel d-1234abcd --project-id PROJECT-UUID --yes
```

Update, retarget, activate, and cancel require a currently staged direction.
The check and mutation occur under the same project direction lock. Scoped
direction cancel does not cancel queued tasks.

Project messages and Strategic continuation use narrow bodies without raw
context, workbench, attachments, source refs, runner selection, or commands:

```powershell
.\blueprint.ps1 messages send "Please investigate the new retry blocker." `
  --project-id PROJECT-UUID --type BLOCKER `
  --idempotency-key hermes-blocker-retry-v1
.\blueprint.ps1 messages answer MESSAGE-UUID "Use the durable counter as truth." `
  --project-id PROJECT-UUID

.\blueprint.ps1 strategic start --project-id PROJECT-UUID
.\blueprint.ps1 strategic send "Continue with the bounded retry plan." `
  --project-id PROJECT-UUID `
  --idempotency-key hermes-strategic-retry-v1
.\blueprint.ps1 strategic chat --project-id PROJECT-UUID
.\blueprint.ps1 strategic-cycle --project-id PROJECT-UUID `
  --phase strategic_review --yes
```

Work-starting mutations reject archived projects. Exact task/direction cleanup
remains available only where its dedicated contract permits it.

## Explicitly denied

A scoped-token gate rejects unsupported CLI commands before project resolution
or local subprocess/filesystem work, and the backend scope registry separately
returns 403 for methods and paths outside the allowlist. The profile does not
grant:

- deploy, migrate, promotion/release override, Docker, host, database, SSH,
  repository checkout, or compute commands;
- settings, credentials/secrets, provider or fleet accounts, runner enrollment,
  runner restart, or other platform administration;
- mass cancel/requeue, arbitrary task status or force-complete, generic task or
  direction patches, approval/merge bypass, or `require_admin`;
- Strategic stop, runner selection, voice/call, command execution, rich context,
  attachment, or unrestricted session endpoints;
- project creation or owner membership.

An error naming a scoped ServiceCredential denial is a real authorization
boundary, not an instruction to fall back to raw `curl` or an ambient admin key.

## Rotation and rollout

Use the separate platform-operator config to list and revoke credentials:

```powershell
$env:BLUEPRINT_CONFIG = "C:\secure\blueprint\platform-operator.json"
.\blueprint.ps1 agent-access list --json
.\blueprint.ps1 agent-access revoke CREDENTIAL-UUID --yes
```

Reminting creates a new credential-owned principal, so old project memberships
cannot silently carry into the new grant. Revocation takes effect immediately.

The server revision containing `project-operator` must be promoted and deployed
before a live credential is minted or used. Landing this code on `automain`
alone does not change the production API. Deployment and live Hermes token
minting are separate operator actions.
