# Multi-provider Runtime Groundwork Plan v1

Date: 2026-04-20

## Purpose

This is the next implementation slice after the provider/schema groundwork. The goal is to make runtime selection explicit and provider-aware before we touch agent runners, VM startup, or cross-provider fallback.

## Outcome of this slice

After this slice lands:

- tasks carry an explicit resolved LLM runtime identity
- pool selection can resolve provider, auth mode, account, and concrete model in one place
- retry, reaper, and cooldown paths can read resolved runtime identity instead of inferring Anthropic defaults
- provisioning code has a single manifest shape ready for later multi-provider startup work
- current execution behavior remains Anthropic-only

## Decisions locked for this slice

- `quality_tier` remains an input to selection, not a runtime identity field
- runtime identity is persisted onto the task before provisioning starts
- `max_plan_account_id` remains temporarily for compatibility, but new code should read the generic LLM fields first
- the new secret manifest shape is introduced in backend code now, but not turned on for provider-specific file materialization yet
- no cross-provider execution changes ship in this slice

## In scope

### 1. Task runtime identity

- Add resolved runtime fields to task context / payload handling:
  - `llm_account_id`
  - `llm_provider`
  - `llm_auth_mode`
  - `llm_model`
- Keep `max_plan_account_id` during the transition.
- Update task claim / dispatch code to write these fields before provisioning.

### 2. Provider-aware selection groundwork

- Introduce a provider-aware selection result object in the backend, for example:
  - account id
  - provider
  - auth mode
  - concrete model
  - model tier used for selection
- Refactor selection internals so they resolve model and account together instead of relying on Anthropic-only global assumptions.
- Keep effective behavior the same for now:
  - current task types still prefer Anthropic
  - only Anthropic accounts are runnable

### 3. Runtime-config integration

- Wire the existing config scaffolding into selection internals:
  - `provider_tier_model_map`
  - `task_type_provider_preference`
- Resolve provider preference and tier-to-model mapping at claim time.
- Do not enable cross-provider fallback yet.
- Do not expose new task-level controls yet unless already needed by the backend path.

### 4. Reaper / retry / cooldown groundwork

- Update backend code that currently infers runtime identity from Anthropic-specific fields to first read:
  - `llm_provider`
  - `llm_auth_mode`
  - `llm_model`
  - `llm_account_id`
- Preserve compatibility when those fields are missing on old tasks.
- Keep existing Anthropic behavior unchanged for legacy tasks.

### 5. Secret manifest groundwork

- Add a generic manifest structure in provisioning code for future use, for example:

```json
{
  "env": {
    "PROVIDER": "anthropic",
    "AUTH_MODE": "oauth"
  },
  "files": []
}
```

- Keep current startup behavior effectively unchanged in this slice.
- Do not turn on provider-specific auth file writing yet.
- Do not require agent changes yet.

## Out of scope

- Agent runner abstraction
- `agent/src/index.ts` provider routing
- VM startup script file materialization changes
- OpenAI or Gemini execution
- Cross-provider fallback
- Provider-aware health gating
- ChatGPT OAuth support
- Admin UI expansion

## File targets

- `backend/app/orchestrator.py`
- `backend/app/max_plan_pool.py`
- `backend/app/reaper.py`
- `backend/app/failure_classification.py`
- `backend/app/compute.py`
- targeted updates in task-routing or agent-status paths where runtime identity is read or written
- backend tests covering claim, retry, cooldown, and provisioning identity handling

## Acceptance criteria

- Newly claimed tasks have resolved LLM runtime identity persisted before provisioning.
- Selection resolves provider, auth mode, account, and concrete model together.
- Old Anthropic-only tasks still work if the new runtime fields are absent.
- Current dispatch behavior does not change for existing workloads.
- Provisioning code can construct a manifest object even if startup still consumes the old behavior internally.
- Tests cover resolved runtime identity and legacy compatibility paths.

## Follow-on slice

The next reviewable slice after this should be:

1. provider-runner abstraction in the agent
2. provider-aware startup/materialization from the manifest
3. provider-specific rate-limit handling
4. then cross-provider fallback behind a flag
