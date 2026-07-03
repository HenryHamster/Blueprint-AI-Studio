# Multi-provider Final Remaining Work Plan v1

Date: 2026-04-20

## Purpose

This plan covers everything still required before deploying multi-provider execution from the current repo state.

The backend/provider groundwork, runtime identity, manifest startup model, shared agent image setup, provider runners, and provider-aware agent status payloads are already in place. This plan is only the remaining work.

## Current state summary

Already implemented:

- `MaxPlanAccount` supports provider/auth metadata and provider-specific validation.
- Tasks persist resolved runtime identity:
  - `llm_account_id`
  - `llm_provider`
  - `llm_auth_mode`
  - `llm_model`
  - `llm_execution_mode`
- Provider runners exist in the agent for Anthropic, OpenAI, and Gemini.
- Startup/provisioning uses one provider-aware manifest per VM.
- Shared image configuration installs all three CLIs and points to `blueprint-agent-v2`.
- Backend runtime settings already expose:
  - `provider_tier_model_map`
  - `task_type_provider_preference`
  - `task_type_model_preferences`
  - `provider_execution_modes`
  - `provider_mode_capabilities`
  - `task_type_execution_defaults`
- Agent status payloads already include:
  - `provider`
  - `auth_mode`
  - `model`
  - `execution_mode`

Still missing before deploy:

- frontend/admin UI for the new provider/model/mode controls
- provider-aware health aggregation and provider-aware health UI
- explicit cross-provider fallback rollout controls and validation
- real staging validation with live credentials
- final deploy checklist and rollback guidance

## Locked decisions

1. Use one shared agent image with all three CLIs.
2. Do not support Gemini Sign-in-with-Google OAuth in the deploy path.
3. Do not include ChatGPT OAuth in the main deploy path unless explicitly requested later.
4. Keep provider selection separate from execution mode selection.
5. Keep operator control over provider order and concrete model priority.

## Phase 1: Finish runtime-config and admin API completeness

Goal: make the backend settings surface complete and explicit before wiring the UI.

### Scope

- Add `cross_provider_fallback_enabled` to runtime config defaults, settings read responses, settings validation, and persistence.
- Confirm `provider_mode_capabilities` is shaped for UI consumption and not just pass-through JSON.
- Add or tighten backend validation for:
  - `task_type_provider_preference`
  - `task_type_model_preferences`
  - `provider_execution_modes`
  - `provider_mode_capabilities`
  - `task_type_execution_defaults`
- Add provider metadata/schema response for UI form rendering if missing:
  - supported providers
  - supported auth modes
  - required metadata fields
  - supported execution modes per provider

### Acceptance

- Admin/settings API exposes every provider/model/mode/fallback knob needed for operations.
- Backend rejects malformed provider priority and model priority payloads cleanly.
- UI can fetch one authoritative shape instead of hardcoding provider capabilities.

## Phase 2: Provider-aware health aggregation and dispatch semantics

Goal: remove the remaining Anthropic-only health assumptions from deploy gating and health reporting.

### Scope

- Replace `claude_api`-only dependency health with provider-aware health in:
  - `backend/app/health_check_primitives.py`
  - `backend/app/deploy_health.py`
  - any admin/debug surfaces that assume Anthropic-only credentials
- Report provider health separately:
  - `anthropic_api`
  - `openai_api`
  - `gemini_api`
- Define aggregate dispatch health semantics:
  - healthy if at least one enabled provider can still serve required work
  - degraded if preferred providers are impaired but fallback providers remain
  - failed only when no eligible provider path remains
- Update deploy auto-pause / resume logic so one provider outage does not globally stall the pipeline when another provider can handle work.

### Acceptance

- Health endpoints distinguish provider-specific outages.
- Deploy gating keys off real dispatchability, not Anthropic-only availability.
- Rate-limited Anthropic accounts no longer make the whole orchestrator appear down if OpenAI or Gemini can still run tasks.

## Phase 3: Cross-provider fallback rollout

Goal: turn the provider-aware selection groundwork into an explicit, controllable deploy feature.

### Scope

- Add `cross_provider_fallback_enabled` checks in selection/dispatch code paths.
- Ensure provider-order selection uses:
  - task payload overrides when present
  - otherwise `task_type_model_preferences`
  - otherwise `task_type_provider_preference` + `provider_tier_model_map`
- Confirm cooldown and disabled-model logic remain scoped to:
  - account
  - provider
  - concrete model
- Tighten status/retry/reaper messages so operators can tell when fallback occurred and why.
- Add backend tests for:
  - preferred provider selected when healthy
  - fallback to second provider when first is exhausted
  - no cross-provider leakage of cooldowns
  - fallback disabled path still waits rather than silently switching

### Acceptance

- Fallback behavior is explicitly flag-controlled.
- Provider fallback is observable in task state and logs.
- Exhausting one provider causes the next eligible provider to be selected on the next attempt.

## Phase 4: Admin UI and operator controls

Goal: expose the new backend controls in the actual admin UX.

### Scope

- Update `admin-ui/src/types/settings.ts` for provider-aware settings and account fields.
- Update `admin-ui/src/hooks/useSettings.ts` to fetch/store:
  - `provider_tier_model_map`
  - `task_type_provider_preference`
  - `task_type_model_preferences`
  - `provider_execution_modes`
  - `provider_mode_capabilities`
  - `task_type_execution_defaults`
  - `cross_provider_fallback_enabled`
- Update `admin-ui/src/components/SettingsSectionRouter.tsx` wiring so the current settings hosts expose the provider-aware sections.
- Update `admin-ui/src/components/SettingsAccountsSection.tsx` for:
  - provider-aware account creation/edit form
  - auth-mode-specific fields
  - provider metadata inputs
- Update `admin-ui/src/components/ProviderSettingsPanel.tsx` for:
  - task-type provider order controls
  - task-type concrete model priority controls
  - task-type execution mode defaults
  - fallback enable/disable toggle
- Keep the UI capability-driven:
  - only show modes supported by the chosen provider
  - only show provider metadata fields required by that auth mode

### Acceptance

- Operators can configure providers, model priority, and execution mode defaults from the UI.
- The UI does not assume Anthropic-only account fields.
- Provider-aware settings round-trip cleanly through the existing admin API.

## Phase 5: Provider-aware health UI

Goal: make the orchestration surface understandable once multiple providers are active.

### Scope

- Update `admin-ui/src/components/OrchestratorHealthBar.tsx` to show provider-level health instead of just one max-plan pool summary.
- Show at least:
  - provider health state
  - provider availability count or eligible-account count
  - degraded vs exhausted vs healthy distinction
- If helpful, expose a compact provider panel in settings/deploy views as well.
- Ensure messaging matches fallback semantics:
  - “Anthropic exhausted, OpenAI available” should read as degraded, not dead.

### Acceptance

- Operators can tell which provider is impaired without inspecting logs.
- The health bar no longer implies one global provider pool.

## Phase 6: Staging validation and rollout checklist

Goal: verify the real provider paths before deploy.

### Scope

- Run one staging task each with real credentials for:
  - Anthropic OAuth or API key
  - OpenAI API key
  - Gemini API key
  - Gemini service account
- Run one forced fallback scenario:
  - primary provider exhausted or disabled
  - secondary provider selected automatically
- Run one execution-mode validation case:
  - Anthropic `advisor` default on a task type that supports it
- Confirm the baked image in CI or staging actually contains:
  - `claude`
  - `codex`
  - `gemini`
- Write a short operator runbook covering:
  - required env and secrets per provider
  - how to enable fallback
  - how to order provider/model priority
  - what health states mean
  - rollback path

### Acceptance

- Each supported provider path completes a real staging task.
- Fallback works with real credentials, not just unit tests.
- Operators have a short deploy/runbook document for the first rollout.

## Phase 7: Final regression pass

Goal: reduce deploy risk after the operator surface lands.

### Scope

- Backend targeted regression suite:
  - provider settings
  - selection/fallback
  - reaper/retry/cooldown
  - compute/provisioning
  - agent status persistence
  - health/deploy gating
- Agent suite:
  - runner tests
  - rate-limit tests
  - typecheck
- Frontend:
  - typecheck/build
  - tests for settings serialization if available

### Acceptance

- No regressions in Anthropic execution.
- Multi-provider selection and staging validations are green.
- Frontend compiles with the new settings surfaces.

## Recommended implementation order

1. Phase 1: runtime-config and admin API completeness
2. Phase 2: provider-aware health aggregation and dispatch semantics
3. Phase 3: cross-provider fallback rollout
4. Phase 4: admin UI and operator controls
5. Phase 5: provider-aware health UI
6. Phase 6: staging validation and rollout checklist
7. Phase 7: final regression pass

## Files most likely to change

Backend:

- `backend/app/shared_state.py`
- `backend/app/routers/admin_settings.py`
- `backend/app/max_plan_pool.py`
- `backend/app/orchestrator.py`
- `backend/app/reaper.py`
- `backend/app/failure_classification.py`
- `backend/app/health_check_primitives.py`
- `backend/app/deploy_health.py`
- backend tests around settings, fallback, health, and orchestration

Frontend:

- `admin-ui/src/types/settings.ts`
- `admin-ui/src/hooks/useSettings.ts`
- `admin-ui/src/components/SettingsSectionRouter.tsx`
- `admin-ui/src/components/SettingsAccountsSection.tsx`
- `admin-ui/src/components/ProviderSettingsPanel.tsx`
- `admin-ui/src/components/OrchestratorHealthBar.tsx`
- any small supporting components/helpers added for provider preference and model priority editing

## Explicit non-goals for this deploy cut

- ChatGPT OAuth support
- Gemini Sign-in-with-Google OAuth
- splitting the agent fleet into provider-specific images
- SDK-direct execution instead of CLI subprocesses

## Definition of done

This is ready to deploy when all of the following are true:

- operators can configure provider order, concrete model priority, and execution mode defaults from the UI
- deploy health reflects provider-aware dispatchability rather than Anthropic-only status
- cross-provider fallback is flaggable, observable, and tested
- Anthropic, OpenAI API-key, Gemini API-key, and Gemini service-account paths all succeed in staging
- the shared image is confirmed in CI/staging
- rollback/runbook guidance exists for the first production rollout
