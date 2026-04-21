# Multi-provider Groundwork Plan v1

Date: 2026-04-20

## Purpose

This is the first implementation slice for multi-provider LLM support. It is intentionally narrow. The goal is to lay down the backend data model and provider abstraction without changing task dispatch behavior yet.

## Outcome of this slice

After this slice lands:

- the database can represent Anthropic, OpenAI, and Gemini accounts
- provider-specific secret validation lives behind a registry
- the codebase has a single place to define provider metadata, auth modes, and tier-to-model mapping hooks
- existing Anthropic behavior still works
- cross-provider fallback, agent runner changes, and manifest-based startup are not live yet

## Decisions locked for v1

- `provider` and `auth_mode` are database enums, not free-form strings.
- The provider contract should be durable enough to support v2 without immediate redesign.
- The admin API should accept the new provider-aware payload as the canonical shape in v1.
- Anthropic legacy request compatibility can remain as a thin shim if cheap, but new code should target the provider-aware payload.

## In scope

### 1. Schema groundwork

- Extend `backend/app/models.py::MaxPlanAccount` with:
  - `provider` enum
  - `auth_mode` enum
  - `secret_blob_ref`
  - `provider_metadata`
- Keep these fields as compatibility shims for now:
  - `oauth_token`
  - `claude_config_json`
- Add a new Alembic migration.
- Bump `EXPECTED_SCHEMA_REVISION` in `backend/app/main.py`.

### 2. Provider package

- Create `backend/app/providers/`.
- Add:
  - `base.py`
  - `registry.py`
  - `anthropic.py`
  - `openai.py`
  - `gemini.py`
- Define a future-facing `LLMProvider` contract for this slice:
  - `name`
  - `supported_auth_modes`
  - `validate_secret`
  - `tier_to_model`
  - `account_has_credentials`
  - `rate_limit_patterns`
  - `required_metadata_fields`

This slice does not need manifest generation yet. That belongs in the next slice.

### 3. Anthropic compatibility wiring

- Move existing Anthropic validation and rate-limit definitions into `AnthropicProvider`.
- Keep current callers working by reading through the provider registry.
- Do not change runtime dispatch semantics yet.

### 4. Admin API groundwork

- Update account create/update validation in `backend/app/routers/admin_pool_accounts.py`.
- Accept a provider-aware payload as the canonical API contract.
- Provider-aware payload should support:
  - `name`
  - `provider`
  - `auth_mode`
  - provider-specific secret input
  - `provider_metadata`
- Anthropic legacy request compatibility can remain temporarily if cheap, but should be treated as compatibility-only, not the primary API shape.

### 5. Runtime config groundwork

- Add config structure for future provider-aware selection, but do not turn it on yet:
  - `provider_tier_model_map`
  - `task_type_provider_preference`
- Keep existing `task_type_models` behavior unchanged in this slice.

## Out of scope

- Agent runner abstraction
- GCP startup script changes
- Secret manifest format
- Cross-provider fallback
- Provider-aware health gating
- ChatGPT OAuth support
- UI form expansion beyond what is needed to preserve compatibility

## File targets

- `backend/app/models.py`
- `backend/alembic/versions/NEW_*.py`
- `backend/app/main.py`
- `backend/app/providers/base.py`
- `backend/app/providers/registry.py`
- `backend/app/providers/anthropic.py`
- `backend/app/providers/openai.py`
- `backend/app/providers/gemini.py`
- `backend/app/routers/admin_pool_accounts.py`
- `backend/app/shared_state.py`
- `backend/testing/test_providers.py`
- targeted updates in existing backend tests for schema and account CRUD

## Acceptance criteria

- Existing Anthropic account creation still works.
- OpenAI and Gemini provider records can be validated and stored.
- New account create/update paths use the provider-aware payload shape.
- Startup and tests pass without changing dispatch behavior.
- No task execution path depends on new provider fields yet.

## Follow-on slice

The next reviewable slice should be:

1. generic pool/runtime identity
2. single-secret manifest design
3. provider-aware provisioning inputs
