# Multi-provider LLM Support Plan v3

Date: 2026-04-20

## Goals

- Add support for multiple LLM providers in the pool: Anthropic, OpenAI, and Gemini.
- Preserve the current CLI-subprocess architecture in v1.
- Support cross-provider fallback when the preferred provider is exhausted or unavailable.
- Keep the system robust by resolving concrete runtime identity before provisioning.

## Decisions

1. Use a single Secret Manager manifest per VM, not multiple secrets per task or VM.
2. Resolve `provider`, `auth_mode`, and concrete `model` onto the task at claim time.
3. Replace Claude-specific runner assumptions with a provider-runner abstraction.
4. Keep `MaxPlanAccount` naming in v1 for compatibility; broaden semantics now and rename later.
5. Keep ChatGPT OAuth as optional follow-up work unless explicitly needed.

## Auth modes in v1

| Provider | Mode | Storage | Headless-safe | v1 |
|---|---|---|---|---|
| Anthropic | OAuth token | Secret Manager string/blob | Yes | Yes |
| Anthropic | API key | Secret Manager string/blob | Yes | Yes |
| OpenAI | API key | Secret Manager string/blob | Yes | Yes |
| Gemini | Vertex service account | Secret Manager JSON blob | Yes | Yes |
| Gemini | AI Studio API key | Secret Manager string/blob | Yes | Yes |
| OpenAI | ChatGPT OAuth `auth.json` | Secret Manager JSON blob | Single-runner only | Optional |
| Gemini | Sign-in-with-Google OAuth | Keychain / hybrid storage | No | No |

## Phase 1: Schema and provider abstraction

- Extend `backend/app/models.py::MaxPlanAccount` with:
  - `provider`
  - `auth_mode`
  - `secret_blob_ref`
  - `provider_metadata`
- Keep `oauth_token` and `claude_config_json` for backward-compatible reads during migration.
- Add provider registry in `backend/app/providers/`.
- Add concrete provider implementations:
  - `AnthropicProvider`
  - `OpenAIProvider`
  - `GeminiProvider`
- Bump `EXPECTED_SCHEMA_REVISION`.

## Phase 2: Generic pool model and runtime identity

- Keep the existing pool module but make entries provider-aware.
- Add provider-aware fields to in-memory pool entries.
- Persist runtime identity to task context before provisioning:
  - `llm_account_id`
  - `llm_provider`
  - `llm_auth_mode`
  - `llm_model`
- Keep `max_plan_account_id` in v1 for compatibility while the rest of the code migrates.

## Phase 3: Agent runner abstraction

- Replace the Claude-specific runtime path with a generic provider runner.
- Introduce a runner contract in `agent/src/`:
  - `validateAuth`
  - `buildInvocation`
  - `spawn`
  - `parseOutput`
  - `matchesRateLimit`
- Implement:
  - `AnthropicRunner`
  - `OpenAIRunner`
  - `GeminiRunner`
- Rename `runClaudeCode()` to a provider-neutral entrypoint.

## Phase 4: Secret injection and startup

- Build one Secret Manager manifest per VM.
- Manifest contains:
  - env vars
  - file payloads
  - file modes and paths
- Update GCP startup script to:
  - fetch one manifest
  - write `/etc/agent.env`
  - materialize files like `auth.json` or `sa.json`
- Update local mode to use the same manifest model.

## Phase 5: Provider-aware rate-limit handling

- Move rate-limit patterns into provider implementations by auth mode.
- Use resolved task runtime identity for rate-limit classification.
- Update:
  - `backend/app/failure_classification.py`
  - `backend/app/reaper.py`
  - `backend/app/routers/agent_status.py`
  - agent pre-claim validation and runtime error handling

## Phase 6: Cross-provider selection and fallback

- Add task-level inputs:
  - `provider_preference`
  - `quality_tier`
- Add runtime-config entries:
  - `task_type_provider_preference`
  - `provider_tier_model_map`
  - `cross_provider_fallback_enabled`
- Selection loop:
  - iterate provider preference order
  - resolve tier to concrete model for that provider
  - choose healthiest eligible account
  - persist resolved identity on task

## Phase 7: Health gating and diagnostics

- Replace Anthropic-only health assumptions with provider-aware health status.
- Report:
  - `anthropic_api`
  - `openai_api`
  - `gemini_api`
- Add aggregate dispatch health semantics so one provider being down does not stall all work if another can serve it.

## Phase 8: Admin API and UI

- Broaden account CRUD to support provider-aware fields and validation.
- Add provider metadata and secret-upload flows.
- Add provider-aware UI:
  - account forms
  - provider health
  - task-type provider preference
  - quality tier selection

## Phase 9: Packer image

- Install:
  - `@anthropic-ai/claude-code`
  - `@openai/codex`
  - `@google/gemini-cli`
- Warm each CLI in the image bake.
- Bump the image family/version note.

## Phase 10: ChatGPT OAuth follow-up

- Optional and likely v1.1.
- If implemented:
  - cap concurrency at 1 per account
  - write `auth.json` from the secret manifest
  - optionally upload refreshed auth state after task completion

## Testing

- Add provider-focused backend tests.
- Add generic pool selection tests.
- Add manifest secret and startup tests.
- Add provider-runner tests in the agent.
- Update invariant limits if frontend files grow.

## Rollout order

1. Schema and provider abstraction
2. Generic pool/runtime identity
3. Agent runner abstraction
4. Packer image update
5. Manifest secret injection
6. Provider-aware rate-limit handling
7. Admin API/UI
8. Cross-provider fallback behind flag
9. Provider-aware health gating
10. Optional ChatGPT OAuth

## Scope limits

- ChatGPT OAuth remains single-runner per account.
- Gemini Sign-in-with-Google OAuth remains unsupported.
- No in-app OAuth redirect flow.
- No SDK-direct path in v1.
- Tier-to-model mappings should live in runtime config, not code constants.

## Estimated size

- v1 without ChatGPT OAuth: roughly 16 to 18 engineering days.

