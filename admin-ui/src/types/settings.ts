export type ProviderName = 'anthropic' | 'openai' | 'gemini';
export type AuthMode = 'oauth' | 'api_key' | 'service_account';

// One entry of the backend-published task-capability contract (the
// `task_capabilities` map embedded in GET /settings, also exposed standalone
// at GET /api/admin/task-capabilities). Field names mirror the backend
// `_capability_to_contract` flattener verbatim (snake_case). This is the
// authoritative, backend-owned metadata the operator UI consumes instead of
// maintaining a private task-type table. Fields are optional so a partial /
// older payload still type-checks; the resolver in taskCapabilities.ts applies
// safe defaults.
export interface TaskCapabilityContractEntry {
  type: string;
  label?: string;
  badge?: string;
  agent_mode?: 'write' | 'read_only';
  default_category?: string | null;
  description?: string;
  user_launchable?: boolean;
  provider_configurable?: boolean;
  filter_visible?: boolean;
  delivers_product?: boolean;
  reservable?: boolean;
  periodic_eligible?: boolean;
}

// The published contract keyed by task-type value. Absent / empty when talking
// to an older backend that does not yet publish the contract — consumers fall
// back to the built-in capability table in that case.
export type TaskCapabilityContract = Record<string, TaskCapabilityContractEntry>;
export type EffortLevel = 'low' | 'medium' | 'high' | 'max';
export const EFFORT_LEVELS: readonly EffortLevel[] = ['low', 'medium', 'high', 'max'];

export interface CategoryRoutingEntry {
  providers: ProviderName[];
  effort: EffortLevel;
}

export interface TaskTypeRoutingEntry {
  providers?: ProviderName[];
  effort?: EffortLevel;
}

export type ProviderSettingsByEffort = Record<
  ProviderName,
  Partial<Record<EffortLevel, Record<string, unknown>>>
>;

export interface DeployState {
  status: string;
  current_revision: string;
  current_sha: string;
  deployed_at: string;
  last_known_good_revision: string;
  last_known_good_sha: string;
  // SHA the live revision was rolled back FROM (the bad revision), preserved
  // as provenance after a manual/auto rollback advances current_sha to the
  // rolled-back target. Optional/nullable so older payloads still type-check.
  rolled_back_from_sha?: string | null;
  rollback_reason: string | null;
  rollback_count: number;
  pause_reason: string | null;
  paused_at: string | null;
  reason: string | null;
  pre_pause_max_vms: number | null;
  failure_rate: number;
  monitoring_window: { elapsed_seconds: number; total_seconds: number; percent: number } | null;
  task_results_count: number;
  gcp_project_id: string;
  health_recovery_streak: number;
  health_recovery_streak_required: number;
  // True when the auto-resume gate raised the required healthy-probe count
  // because the pipeline has been flapping (repeatedly auto-pausing/resuming).
  // Optional/additive so older payloads still type-check.
  health_recovery_flap_escalated?: boolean;
  health_recovery_history?: boolean[];
  health_recovery_history_size?: number;
  health_failure_streak: number;
  health_failure_streak_threshold: number;
  health_check_interval_seconds?: number;
  external_health?: Record<string, unknown>;
}

export interface ProviderModeCapabilities {
  [capability: string]: boolean | number | string | null;
}

export interface ProviderCatalogEntry {
  auth_modes: Record<string, { required_metadata_fields: string[] }>;
  supported_execution_modes: string[];
  execution_modes: string[];
  mode_capabilities: Record<string, ProviderModeCapabilities>;
  // JSON-Schema-shaped declaration of this provider's tunable settings bag.
  // Consumed by ProviderSettingsPanel to render a dynamic knob editor; shape
  // is provider-defined and validated server-side.
  settings_schema?: {
    type: 'object';
    additionalProperties?: boolean;
    properties: Record<string, unknown>;
  };
}

// Opaque per-provider settings bag. Shape varies by provider (see each
// provider's settings_schema). Validated server-side; UI treats as a
// generic record until the schema-driven renderer consumes it.
export type ProviderSettingsBag = Record<string, unknown>;

// { "anthropic": {...}, "openai": {...}, "gemini": {...} }
export type ProviderSettingsDefaults = Partial<Record<ProviderName, ProviderSettingsBag>>;

// { "review": { "anthropic": { effort: "high" } }, ... }
export type TaskTypeProviderOverrides = Record<string, Partial<Record<ProviderName, ProviderSettingsBag>>>;

export interface ModelPreference {
  provider: ProviderName;
  model: string;
}

// Per-account provider metadata. Carries both static config (Gemini
// service-account project/location) and the cached model-availability
// snapshot populated by the backend's refresh-models endpoint.
// `available_models` is absent until a successful refresh — missing here
// means "not yet probed", which the dispatcher treats as may-have-access.
export interface ProviderMetadata {
  project?: string;
  location?: string;
  available_models?: string[];
  available_models_refreshed_at?: string;
  available_models_error?: string | null;
  // Forward-compat: provider adapters may stash provider-specific keys.
  [key: string]: unknown;
}

export interface MaxPlanAccount {
  id: string;
  name: string;
  enabled: boolean;
  active_tasks: number;
  total_tasks: number;
  token_created_at: string | null;
  provider: ProviderName;
  auth_mode: AuthMode;
  provider_metadata?: ProviderMetadata;
  rl_hit_count?: number;
  disabled_models?: Record<string, { hits: number; at: string | null }>;
  daily_task_cap?: number | null;
  max_concurrent_tasks?: number | null;
  auto_disable_after_rate_limits?: number | null;
  daily_task_count?: number;
  daily_task_count_reset_at?: string | null;
  disabled_reason?: string | null;
}

export interface ProviderHealthSnapshot {
  provider: ProviderName;
  dep_name: string;
  configured: boolean;
  dispatch_relevant: boolean;
  status: 'ok' | 'degraded' | 'down' | 'inactive' | 'unknown';
  reason: string | null;
  has_raw_credentials: boolean;
  total_accounts: number;
  enabled_accounts: number;
  ready_accounts: number;
  available_accounts: number;
  primary_model: string | null;
  dispatch_available: boolean;
}

export interface ProviderApisHealth {
  status: 'ok' | 'degraded' | 'down' | 'unknown';
  fallback_enabled: boolean;
  required_providers: ProviderName[];
  available_providers: ProviderName[];
  degraded_providers: ProviderName[];
  down_providers: ProviderName[];
  providers: Record<string, ProviderHealthSnapshot>;
}

export interface OrchestratorHealth {
  last_cycle_seconds_ago: number | null;
  // Progress-based liveness — the orchestrator's real liveness contract,
  // judged by app.orchestrator.evaluate_orchestrator_liveness() off the
  // in-cycle progress heartbeat (not cycle-completion age). Optional/nullable
  // so older payloads that predate these fields still type-check; consumers
  // fall back to cycle-age thresholds when they are absent.
  orchestrator_alive?: boolean;
  orchestrator_hung?: boolean;
  orchestrator_progress_age_seconds?: number | null;
  orchestrator_cycle_age_seconds?: number | null;
  orchestrator_liveness_reason?: string | null;
  queued_tasks: number;
  active_tasks: number;
  pool_info: Record<string, unknown>;
  deploy_state?: {
    status: string;
    rollback_count: number;
    rollback_reason: string | null;
    pause_reason: string | null;
    monitoring_since: string | null;
    recent_outcomes: { created_at: string; status: string; failure_class: string | null }[];
  } | null;
  deploy_monitoring?: {
    external_health?: Record<string, unknown>;
  };
}

export interface ZoneHealthEntry {
  status: string;
  last_success: string | null;
  last_failure: string | null;
  last_failure_reason: string | null;
  consecutive_failures: number;
  cooldown_active: boolean;
}


