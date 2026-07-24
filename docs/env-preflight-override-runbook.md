# env_preflight_enabled — Operator Override Runbook

When the admin-API hotfix cannot reach `main` (e.g., a circular promotion deadlock
blocks the deploy pipeline), the operator must flip `env_preflight_enabled`
directly on the deployed engine. This runbook documents the two override
mechanisms in order of precedence.

## Background

The `env_preflight_enabled` flag gates the boot/env-prerequisite hard-block in the
release qualification system. When armed (`true`), every candidate must pass the
env-preflight check — the deployed orchestrator judges the candidate's declared
boot/env prerequisites against its own `os.environ` and hard-blocks promotion
(`boot_prereq_missing`) when a required var is absent.

In a deadlock scenario where the deployed engine has it armed and the admin API
is unavailable (e.g., the eng team cannot ship the hotfix because qualification
is deadlocked on the very preflight check the hotfix would disarm), the operator
flips it directly.

### Precedence order (highest to lowest)

| Priority | Source | How to change | Persistence | Takes effect |
|---|---|---|---|---|---|
| 1 (highest) | `project.build_config["env_preflight_enabled"]` — JSONB column on the `project` table | Direct DB update | Survives deploys | Next qualification attempt |
| 2 | `_runtime_config["env_preflight_enabled"]` — fleet runtime flag | Admin settings API or direct DB update to runtime_config | Survives deploys (persisted to `DeploymentState.runtime_config_json` on every PATCH) | Next qualification attempt |
| 3 (lowest) | `promotion_policy.yaml` key `env_preflight_enabled` | Edit YAML on Cloud Run instance | Lost on next deploy | Next qualification attempt spawn (not mid-flight) |

The release qualifier resolves the flag at `release_qualifier.py:~2844` and
records the resolved value on the pending qualification record so scoring uses
the flag state in force when the run *started*, not a value that flipped mid-run.

---

## When to use this procedure

Use this procedure when **all** of the following are true:

1. `env_preflight_enabled` is `true` on the deployed engine.
2. The admin-API hotfix cannot reach `main` (e.g., qualification deadlock).
3. The operator needs to unblock the qualification pipeline *now*.

Wait for the admin-API fix when:

- The fix is already merged and waiting for the next deploy cycle (a single
  qualification attempt will promote it).
- The deadlock is not urgent and the normal deploy cadence (≤1 day) is
  acceptable.

---

## Method 1: Direct DB update (highest precedence)

This is the preferred method. The release qualifier reads
`project.build_config["env_preflight_enabled"]` first, so a DB update takes
immediate precedence over all other sources.

### 1.1 Connect to the production database

Connection details are in `docs/db-operations.md`. The short form:

```bash
# Fetch the DATABASE_URL secret
DB_URL=$(gcloud secrets versions access latest --secret=DATABASE_URL)

# Connect
psql "$DB_URL"
```

If `gcloud` is not available, retrieve the connection string from Secret Manager
in the Cloud Console, or from the Cloud Run service's environment variables.

### 1.2 Identify the Blueprint project row

```sql
-- List projects to find the Blueprint project id
SELECT id, name, github_owner, github_repo
FROM project
WHERE is_self_repo = true;
```

Expected output — a single row for the Blueprint self-repo:

```
                  id                  |   name    | github_owner | github_repo
--------------------------------------+-----------+--------------+-------------
 a1b2c3d4-e5f6-7890-abcd-ef1234567890 | Blueprint | <org>        | blueprint
```

Store the `id` for the next commands:

```sql
-- Use in a psql variable for convenience
\set bp_id 'a1b2c3d4-e5f6-7890-abcd-ef1234567890'
```

### 1.3 Read the current value

```sql
SELECT build_config -> 'env_preflight_enabled' AS current_value
FROM project
WHERE id = :'bp_id';
```

If the key is absent (NULL), the flag resolves through lower-precedence sources
and is effectively armed because `promotion_policy.yaml` defaults to `true`.

### 1.4 Set the override to false

```sql
UPDATE project
SET build_config = jsonb_set(
    COALESCE(build_config, '{}'::jsonb),
    '{env_preflight_enabled}',
    'false'::jsonb
)
WHERE id = :'bp_id';
```

**What this does:** sets `build_config.env_preflight_enabled` to `false` on the
Blueprint project row, preserving all other keys in the JSONB column. If
`build_config` was NULL, it initialises it to `{"env_preflight_enabled": false}`.

### 1.5 Verify the update

```sql
SELECT
    build_config -> 'env_preflight_enabled' AS env_preflight_enabled,
    build_config AS full_build_config
FROM project
WHERE id = :'bp_id';
```

Expected output:

```
 env_preflight_enabled |          full_build_config
-----------------------+-------------------------------------
 false                 | {"env_preflight_enabled": false, ...}
```

### 1.6 Verify the qualifier is reading it

Wait for the next qualification attempt to spawn (or trigger one manually via
the admin API). Then check the `last_qualification` JSONB blob on the
`releaseversion` row (`env_preflight_enabled` is stored as a key inside that
blob, not as a dedicated column):

```sql
SELECT
    id,
    (last_qualification ->> 'env_preflight_enabled')::boolean AS env_preflight_enabled,
    (last_qualification ->> 'confidence')::numeric            AS confidence,
    last_qualification -> 'audit_findings' -> 'blockers'      AS blockers
FROM releaseversion
WHERE project_id = :'bp_id'
ORDER BY created_at DESC
LIMIT 1;
```

Look for `env_preflight_enabled: false` in the output. The next attempt should
also NOT spawn a smoketest child.

### 1.7 Rollback: restore the previous value

To re-arm the flag, remove the key (the flag will resolve from the next source
in the precedence chain) or set it back to `true`:

```sql
-- Option A: remove the key entirely (flag resolves from runtime_config or policy)
UPDATE project
SET build_config = build_config - 'env_preflight_enabled'
WHERE id = :'bp_id';

-- Option B: explicitly set back to true
UPDATE project
SET build_config = jsonb_set(
    COALESCE(build_config, '{}'::jsonb),
    '{env_preflight_enabled}',
    'true'::jsonb
)
WHERE id = :'bp_id';
```

---

## Method 2: Edit promotion_policy.yaml (fallback)

Use this method when the database is also unreachable. The file is baked into
the Cloud Run container image, so changes take effect on the *next* qualification
attempt spawn (not mid-flight) and **are lost on the next deploy**.

### 2.1 Access the running Cloud Run instance

```bash
# List running revisions
gcloud run revisions list \
    --service=blueprint-orchestrator \
    --region=<region> \
    --format='table(name, active, created)'

# Open a shell on the active revision
gcloud run jobs execute shell \
    --image=<active-revision-image> \
    --region=<region> \
    --command=/bin/bash
```

Alternatively, use the Cloud Console: **Cloud Run → blueprint-orchestrator →
ACTIONS → Edit and deploy new revision**.

### 2.2 Locate and edit the file

The file is at `backend/promotion_policy.yaml` inside the container. Current
relevant section (lines 25–34):

```yaml
# Slice 3 of the boot/env-prerequisite promotion gate (design 245212a4).
# When true, the DEPLOYED orchestrator judges each candidate's declared boot/env
# prerequisites (presence-only) against its OWN os.environ — a faithful proxy of
# the deploy-target env — and HARD-BLOCKS promotion (boot_prereq_missing) when an
# in-scope required var is absent. A failed preflight yields a deterministic,
# actionable blocker that names the exact missing prerequisite — it never degrades
# into a vague infra-failure retry loop (the v22 migration-gate lesson). A
# per-project build_config.env_preflight_enabled override and the
# runtime_config.env_preflight_enabled fleet flag take precedence over this.
env_preflight_enabled: true
```

Change `true` to `false`:

```yaml
env_preflight_enabled: false
```

**If editing in-place on a running container:** the change is ephemeral and will
not survive a restart. Use the Cloud Console to deploy a new revision with the
edited file instead.

**To deploy a new revision with the override (Cloud Console):**
1. **Cloud Run → blueprint-orchestrator → EDIT & DEPLOY NEW REVISION**
2. Under **Container**, update the image or keep it the same.
3. Under **Variables & Secrets**, add an override or mount a modified YAML.
4. Deploy. The new revision starts with `env_preflight_enabled: false`.

### 2.3 Verify the edit is live

Check the admin settings endpoint once the revision is serving:

```bash
curl -s "https://<orchestrator-url>/api/admin/settings" \
    -H "Authorization: Bearer $ADMIN_API_KEY" \
    | jq '.env_preflight_enabled'
```

Expected: `false`. The `/api/admin/settings` endpoint returns a flat JSON
object; `env_preflight_enabled` is a top-level key, not nested under
`runtime_config`.

Alternatively, check the orchestrator logs for the resolved flag value on the
next qualification attempt.

### 2.4 Rollback

Restore the original value in `promotion_policy.yaml`:

```yaml
env_preflight_enabled: true
```

Deploy a new revision with the restored file. If you edited the file on the live
container without deploying a new revision, the change is already lost on the
next restart — no rollback is needed.

---

## Verification checklist

After applying either method, confirm the change took effect:

1. **Flag resolution:** the next qualification attempt's record shows
   `env_preflight_enabled: false`.
2. **No smoketest child:** the qualification attempt no longer spawns a
   `smoketest` task.
3. **Qualification proceeds:** the attempt runs the audit and test-erosion judges
   and produces a confidence score without the `boot_prereq_missing` blocker.
4. **Promotion unblocks:** if this was the only blocker, promotion proceeds
   normally.

---

## Quick reference

| What | Method 1 (DB) | Method 2 (YAML) |
|---|---|---|
| Precedence | Highest (overrides all) | Lowest (fallback) |
| Persistence | Survives deploys | Lost on next deploy |
| Granularity | Per-project | Fleet-wide |
| Rollback | Remove key or set `true` | Restore `true` and re-deploy |
| Requires DB access | Yes | No |
| Requires Cloud Run access | No | Yes |
