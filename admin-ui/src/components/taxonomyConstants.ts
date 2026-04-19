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

export const STAGE_ORDER = ['provision', 'clone', 'test', 'lint', 'build', 'pr_creation', 'merge', 'review'] as const;

export const STAGE_COLORS: Record<string, string> = {
  provision: 'bg-amber-500/20 text-amber-400 border-amber-500/30',
  clone: 'bg-amber-500/20 text-amber-400 border-amber-500/30',
  test: 'bg-red-500/20 text-red-400 border-red-500/30',
  lint: 'bg-red-500/20 text-red-400 border-red-500/30',
  build: 'bg-red-500/20 text-red-400 border-red-500/30',
  pr_creation: 'bg-cyan-500/20 text-cyan-400 border-cyan-500/30',
  merge: 'bg-cyan-500/20 text-cyan-400 border-cyan-500/30',
  review: 'bg-purple-500/20 text-purple-400 border-purple-500/30',
};
