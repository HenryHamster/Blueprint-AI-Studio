export type ProviderName = 'anthropic' | 'openai' | 'gemini';
export type AuthMode = 'oauth' | 'api_key' | 'service_account';

export interface DeployState {
  status: string;
  current_revision: string;
  current_sha: string;
  deployed_at: string;
  last_known_good_revision: string;
  last_known_good_sha: string;
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
}

export interface ModelPreference {
  provider: ProviderName;
  model: string;
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
  provider_metadata?: Record<string, string>;
  rl_hit_count?: number;
  disabled_models?: Record<string, { hits: number; at: string | null }>;
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
