export const TAXONOMY_ORDER = ['scope_related', 'review_quality', 'infra_related', 'code_defect'] as const;

export const TAXONOMY_LABELS: Record<string, string> = {
  scope_related: 'scope',
  review_quality: 'review',
  infra_related: 'infra',
  code_defect: 'code',
};

export const TAXONOMY_COLORS: Record<string, string> = {
  scope_related: 'bg-cyan-500/20 text-cyan-400 border-cyan-500/30',
  review_quality: 'bg-purple-500/20 text-purple-400 border-purple-500/30',
  infra_related: 'bg-amber-500/20 text-amber-400 border-amber-500/30',
  code_defect: 'bg-red-500/20 text-red-400 border-red-500/30',
};

export const STAGE_DROPOUT_LABELS: Record<string, string> = {
  tasks_failed_code_error: 'code error',
  tasks_failed_infra: 'infra',
  tasks_required_approval: 'approval',
  tasks_merged_staging_only: 'staging only',
};
