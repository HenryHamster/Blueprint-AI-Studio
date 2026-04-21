# Multi-provider Fix Plan v1

Date: 2026-04-20

## Purpose

Track the concrete fixes required after code review of the in-progress multi-provider LLM integration.

This plan is intentionally scoped to issues still present in the working tree at the time of writing. Another agent is also making changes, so every item below must be re-checked in code immediately before editing.

## Confirmed current issues

1. `backend/app/compute.py` currently fails to import because the startup-script f-string contains unescaped literal braces inside the embedded Python snippet.
2. Critical dispatch and recovery code still contains Anthropic-only health assumptions:
   - orchestrator external health gate
   - reaper infra recovery for `EXTERNAL_API`
   - direction hold recovery for `EXTERNAL_API`
3. Runtime identity clearing is incomplete in some requeue/clone flows because `llm_execution_mode` is not consistently stripped with the other resolved LLM context fields.
4. The deploy settings UI still shows `claude_api` as a primary external dependency instead of provider-aware dispatch/LLM health.
5. Provider-aware fallback and no-credential operator messaging still needs re-check after the above fixes land.

## Fix order

### Phase 1: Restore backend importability

- Re-check `backend/app/compute.py` to confirm the f-string issue is still present.
- Escape or restructure the startup script string so the embedded Python block no longer produces invalid f-string parsing.
- Re-run backend import/pytest in Docker before touching more backend logic.

### Phase 2: Remove stale Anthropic-only health gates

- Re-check:
  - `backend/app/orchestrator.py`
  - `backend/app/reaper.py`
  - any related helper paths
- Replace remaining `claude_api` gating where dispatchability should be provider-aware with `dispatch_api` or equivalent aggregate dispatch-health semantics.
- Preserve the `claude_api` compatibility alias only for legacy reporting surfaces, not control flow.

### Phase 3: Make runtime identity clearing consistent

- Re-check all retry/requeue/clone/admin retry flows before editing.
- Ensure `llm_execution_mode` is cleared anywhere the rest of the resolved runtime identity is cleared.
- Prefer shared helpers where practical instead of hand-maintained `pop(...)` lists.

### Phase 4: Align deploy/operator UI with backend semantics

- Re-check UI files before editing:
  - `admin-ui/src/components/SettingsDeploySection.tsx`
  - related types/hooks if needed
- Replace stale `claude_api`-centric dependency display with provider-aware / dispatch-aware health display.
- Keep messaging consistent with fallback semantics:
  - provider outage with other providers available => degraded
  - no dispatchable provider path => down

### Phase 5: Re-check no-credential and fallback messaging

- Re-check orchestrator selection and blockage messaging after earlier fixes.
- Tighten cases where “all accounts rate-limited” is emitted for what is actually missing provider credentials or provider mismatch.
- Keep this phase last because concurrent changes may already address part of it.

## Verification plan

Run these after each phase as applicable, preferring Docker:

1. Backend targeted tests:
   - `testing/test_providers.py`
   - `testing/test_provider_settings.py`
   - `testing/test_llm_runtime.py`
   - `testing/test_deploy_health.py`
2. Agent:
   - `agent/src/index.test.ts`
3. Frontend:
   - admin UI production build

## Notes for concurrent work

- Re-read the target files immediately before each patch.
- Do not revert unrelated edits from the other agent.
- If a targeted issue has already been fixed differently, update the plan mentally and move to the next still-open item instead of forcing this exact implementation.
