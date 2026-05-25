export interface Direction {
  id: string;
  text: string;
  priority: 'high' | 'medium' | 'low';
  added_at: string;
  status: 'active' | 'completed' | 'paused' | 'proposed';
  paths?: string[];
  last_researched_at?: string | null;
  proposed_by?: string;
  proposal_rationale?: string;
  proposed_at?: string;
  pending_update?: {
    suggested_text?: string;
    suggested_priority?: string;
    suggested_paths?: string[];
    rationale: string;
    proposed_at: string;
    proposed_by: string;
  };
}

export interface BuildConfigDirectory {
  path: string;
  type: string;
  install_cmd?: string;
  lint_cmd?: string;
  build_cmd?: string;
  test_cmd?: string;
}

export interface BuildConfig {
  language?: string | null;
  directories?: BuildConfigDirectory[];
  git_user_name?: string;
  git_user_email?: string;
  env_vars?: Record<string, string>;
  detected_at?: string;
  enable_fresh_reviewer?: boolean;
  enable_parallel_review?: boolean;
  enable_parallel_fix?: boolean;
}

export interface Project {
  id: string;
  name: string;
  status: string;
  github_owner: string;
  github_repo: string;
  autonomous_enabled: boolean;
  /** Full directions array — present on detail endpoint; absent on list endpoint. */
  directions?: Direction[];
  /** Count of active directions — present on list endpoint. */
  directions_count?: number;
  created_at: string;
  my_role?: 'owner' | 'member' | 'viewer';
  build_config?: BuildConfig | null;
  is_self_repo?: boolean;
  sr_catch_rate_targets?: Record<string, number>;
}

export interface AutoRemediationConfig {
  enabled: boolean;
  branch_whitelist: string[];
  max_per_hour: number;
  max_concurrent: number;
  require_review: boolean;
}

export interface AutoRemediationRecentTask {
  id: string;
  title: string;
  status: string;
  branch: string;
  created_at: string | null;
}

export interface AutoRemediationStats {
  spawned_24h: number;
  spawned_7d: number;
  success_rate: number | null;
  recent_tasks: AutoRemediationRecentTask[];
}

export interface IssueImplementationConfig {
  enabled: boolean;
  trigger_label: string;
  max_per_hour: number;
  max_concurrent: number;
  base_branch: string;
  default_effort: 'low' | 'medium' | 'high';
}

export interface AutoReviewConfig {
  enabled: boolean;
  max_per_hour: number;
  max_concurrent: number;
}

export interface ProjectMemberEntry {
  id: string;
  user_id: string;
  email?: string;
  role: 'owner' | 'member' | 'viewer';
  created_at: string | null;
}
