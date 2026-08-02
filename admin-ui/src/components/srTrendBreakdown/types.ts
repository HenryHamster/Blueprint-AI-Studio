export interface SrMissExample {
  severity: 'CRITICAL' | 'MAJOR' | 'MINOR';
  desc: string;
}

export interface SrCategoryTrend {
  current_hit_rate?: number | null;
  previous_hit_rate?: number | null;
  current_reviewer_finding_count?: number | null;
  previous_reviewer_finding_count?: number | null;
  current_human_count?: number | null;
  previous_human_count?: number | null;
  miss_rate?: number | null;
  all_iterations_miss_rate?: number | null;
  weighted_miss_rate?: number | null;
  false_positive_rate?: number | null;
  precision?: number | null;
  trend?: 'increasing' | 'decreasing' | 'stable';
  trend_significant?: boolean | null;
  rate_history?: number[];
  prompt_version_changed?: boolean | null;
  examples?: SrMissExample[];
  below_floor?: boolean | null;
  reasoning_engagement_rate?: number | null;
  reasoning_engagement_with?: number | null;
  reasoning_engagement_total?: number | null;
  precision_recall_quadrant?: 'healthy' | 'conservative_shift' | 'noisy_shift' | 'both_declining' | 'mixed' | 'flat' | null;
  precision_delta?: number | null;
  recall_delta?: number | null;
  engagement_thinning?: boolean | null;
  time_to_miss_detect_p50_sec?: number | null;
  time_to_miss_detect_p95_sec?: number | null;
  time_to_miss_detect_sample_count?: number | null;
  time_to_miss_detect_lone_checker?: boolean | null;
  current_finding_count?: number | null;
  previous_finding_count?: number | null;
  severity_calibration?: {
    under_rated: number;
    over_rated: number;
    matched: number;
    total: number;
  } | null;
}

export interface SrTaskTypeCategoryStats {
  miss_rate: number;
  all_iterations_miss_rate?: number | null;
  sample_count: number;
  examples?: SrMissExample[];
  rate_history?: number[];
  trend?: 'increasing' | 'decreasing' | 'stable';
  prompt_version_changed?: boolean | null;
}

export interface SrCheckResultEntry {
  passed: boolean;
  issue?: string | null;
  origin?: string | null;
}

export interface SrSeverityBucket {
  current_miss_rate?: number | null;
  previous_miss_rate?: number | null;
  current_hit_rate?: number | null;
  previous_hit_rate?: number | null;
  current_sample?: number | null;
  previous_sample?: number | null;
  trend?: 'improving' | 'stable' | 'regressing';
}

export interface SrCoFailurePair {
  cat_a: string;
  cat_b: string;
  co_fail_count: number;
  p_b_given_a: number;
  base_rate_b: number;
  lift: number;
}

export interface SrInvestigationSignal {
  category: string;
  current_hit_rate?: number | null;
  previous_hit_rate?: number | null;
  sample_size?: number | null;
  signals: string[];
}

export const SR_CANONICAL_CATEGORIES: { id: string; label: string }[] = [
  { id: 'correctness', label: 'Correctness' },
  { id: 'error-handling', label: 'Error Handling' },
  { id: 'security', label: 'Security' },
  { id: 'style-conventions', label: 'Style Conventions' },
  { id: 'type-safety', label: 'Type Safety' },
  { id: 'documentation', label: 'Documentation' },
  { id: 'performance', label: 'Performance' },
  { id: 'architecture', label: 'Architecture' },
  { id: 'test-coverage', label: 'Test Coverage' },
  { id: 'scope-drift', label: 'Scope Drift' },
  { id: 'regression_risk', label: 'Regression Risk' },
  { id: 'cross-system-contract', label: 'Cross-System Contract' },
];

export interface SrTrendData {
  computed_at?: string | null;
  is_stale?: boolean | null;
  data_sufficient?: boolean | null;
  last_sufficient_computed_at?: string | null;
  bucket_mode?: 'cycle' | 'calendar' | null;
  current_cycle_id?: string | null;
  previous_cycle_id?: string | null;
  prompt_version?: string | null;
  mixed_prompt_versions?: boolean | null;
  prompt_version_changed?: boolean | null;
  overall_hit_rate?: number | null;
  previous_overall_hit_rate?: number | null;
  overall_trend_verdict?: 'improving' | 'declining' | 'stable' | null;
  overall_trend_significant?: boolean | null;
  categories?: Record<string, SrCategoryTrend> | null;
  by_task_type?: Record<string, Record<string, SrTaskTypeCategoryStats>> | null;
  by_scope?: Record<string, Record<string, SrTaskTypeCategoryStats>> | null;
  by_fix_iteration?: Record<string, Record<string, SrTaskTypeCategoryStats>> | null;
  sr_skip_rate?: number | null;
  sr_skip_reason_distribution?: Record<string, number> | null;
  by_severity?: Record<string, SrSeverityBucket> | null;
  by_direction?: Record<string, Record<string, DirectionSrCatStat>> | null;
  co_failure_pairs?: SrCoFailurePair[] | null;
}

export interface EdgesTrendStats {
  sample_size?: number | null;
  tasks_with_edges_rate?: number | null;
  avg_edges_per_task?: number | null;
  unhandled_edge_flagged_rate?: number | null;
  correctness_rejection_rate_with_edges?: number | null;
  correctness_rejection_rate_without_edges?: number | null;
  correctness_rejection_rate_with_unhandled_flag?: number | null;
  correctness_rejection_rate_with_all_handled?: number | null;
}

export interface EdgesTrend {
  computed_at?: string | null;
  bucket_mode?: 'cycle' | 'calendar' | null;
  current_cycle_id?: string | null;
  previous_cycle_id?: string | null;
  current?: EdgesTrendStats | null;
  previous?: EdgesTrendStats | null;
  tasks_with_edges_rate_delta?: number | null;
  trend?: 'improving' | 'regressing' | 'stable' | null;
  rate_history?: number[] | null;
}

export interface DirectionSrCatStat {
  sample_count: number;
  hit_rate?: number | null;
  miss_rate: number | null;
  all_iterations_miss_rate?: number | null;
  weighted_miss_rate?: number | null;
  false_positive_rate?: number | null;
  precision?: number | null;
  examples?: Array<{ desc: string; severity: string }>;
}

export interface DirectionSrEntry {
  direction_id: string;
  direction_text?: string;
  sample_size: number;
  overall_catch_rate: number | null;
  worst_category: string | null;
  prompt_version?: string | null;
  mixed_prompt_versions?: boolean | null;
  prompt_version_changed?: boolean | null;
  categories: Record<string, DirectionSrCatStat>;
}

export interface SrMissedTask {
  task_id: string;
  title: string;
  task_type?: string | null;
  scope?: string | null;
  created_at?: string | null;
  missed_categories: string[];
  caught_categories: string[];
  pr_url?: string | null;
  sr_check_results?: Record<string, SrCheckResultEntry>;
}

export interface SrScopeDriftVsExpansion {
  consistent?: number | null;
  miss_by_expansion?: number | null;
  hit_by_expansion?: number | null;
  fp_candidate?: number | null;
  gray_zone?: number | null;
  miss_by_expansion_rate?: number | null;
  numerator?: number | null;
  denominator?: number | null;
  sample_size?: number | null;
}

export interface SrUnhandledEdge {
  case: string;
  handled: boolean;
  blind_spot?: string | null;
  origin?: string | null;
}

export interface SrUnhandledEdgesTask {
  task_id: string;
  title: string;
  task_type?: string | null;
  scope?: string | null;
  created_at?: string | null;
  unhandled_edges: SrUnhandledEdge[];
  pr_url?: string | null;
}
