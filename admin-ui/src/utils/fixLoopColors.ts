export const EXIT_REASON_COLORS: Record<string, string> = {
  passed: 'bg-emerald-500/20 text-emerald-400 border-emerald-500/30',
  early_unchanged: 'bg-yellow-500/20 text-yellow-400 border-yellow-500/30',
  max_attempts: 'bg-red-500/20 text-red-400 border-red-500/30',
  divergence_detected: 'bg-orange-500/20 text-orange-400 border-orange-500/30',
  divergence_oscillation: 'bg-orange-400/20 text-orange-300 border-orange-400/30',
  divergence_similar_error: 'bg-amber-500/20 text-amber-400 border-amber-500/30',
  divergence_diff_revert: 'bg-rose-500/20 text-rose-400 border-rose-500/30',
  runaway_detected: 'bg-red-600/20 text-red-500 border-red-600/30',
};
