import { CheckCircle, DollarSign } from 'lucide-react';
import type { DirectionProgress } from './DirectionProgressSection';

interface ReviewCapExhaustionSummary {
  count: number;
  avg_iterations: number | null;
  total_cost_usd: number;
  avg_cost_usd: number | null;
  top_directions: { direction_id: string; count: number }[];
}

interface RetryWasteByDirection {
  direction_id: string;
  direction_text: string;
  retry_cost_usd: number;
  retry_task_count: number;
}

interface ResurrectionOutcomeSummary {
  total: number;
  completed: number;
  failed_again: number;
  success_rate: number | null;
}

interface MigrationHealth {
  current_schema_revision: string | null;
  migration_tasks_in_window: number;
  migration_tasks_succeeded: number;
  migration_tasks_failed: number;
  migration_errors_in_window: number;
  auto_generated_migrations: number;
}

interface PreventionEfficacy {
  preventive_tasks_created: number;
  preventive_tasks_merged: number;
  preventive_tasks_failed: number;
  preventive_merge_rate: number | null;
  overall_merge_rate: number | null;
  assessment: 'positive' | 'negative' | 'neutral' | 'no_data';
}

interface ReleaseQualifierInfraMetrics {
  qualifier_runs_total: number;
  release_infra_failure_total: number;
  audit_report_missing_count: number;
  audit_report_parseable_but_unscoreable_count: number;
  smoketest_report_missing_count: number;
  release_infra_failure_streak_max: number;
}

interface RetryPatternSummary {
  total_multi_attempt_failures: number;
  repeat_class_count: number;
  top_patterns: { sequence: string; count: number }[];
}

interface AnalysisOverflowSummary {
  total_count: number;
  by_scope: Record<string, number>;
  cost_usd: number;
}

interface ProvisionWasteByDirection {
  direction_id: string;
  direction_text: string;
  provision_cost_usd: number;
  provision_task_count: number;
}

interface EscalationOutcomeSummary {
  total_attempted: number;
  total_merged: number;
  total_failed: number;
  overall_success_rate: number | null;
  by_path: Record<string, { attempted: number; merged: number; failed: number; success_rate: number | null }>;
}

interface CatchUpOperationalMetricsSectionProps {
  directionProgress: DirectionProgress[];
  reviewCapExhaustionSummary?: ReviewCapExhaustionSummary | null;
  totalMergeRetriesSaved?: number | null;
  mergeEarlyExitCount?: number | null;
  retryTaskCount?: number | null;
  retryOverheadCostUsd?: number | null;
  avgRetriesBeforeFailure?: number | null;
  retryWasteByDirection?: RetryWasteByDirection[] | null;
  retrySuccessCount?: number | null;
  retryWastedRate?: number | null;
  retryRecoveryCostUsd?: number | null;
  resurrectionOutcomeSummary?: ResurrectionOutcomeSummary | null;
  migrationHealth?: MigrationHealth | null;
  preventionEfficacy?: PreventionEfficacy | null;
  releaseQualifierInfraMetrics?: ReleaseQualifierInfraMetrics | null;
  retryPatternSummary?: RetryPatternSummary | null;
  analysisOverflowSummary?: AnalysisOverflowSummary | null;
  provisionWasteCostUsd?: number | null;
  provisionWasteByDirection?: ProvisionWasteByDirection[] | null;
  provisioningOverheadPct?: number | null;
  escalationOutcomeSummary?: EscalationOutcomeSummary | null;
  retrySuccessRateByClass?: Record<string, { merged: number; failed: number; success_rate: number }> | null;
}

export function CatchUpOperationalMetricsSection({
  directionProgress,
  reviewCapExhaustionSummary,
  totalMergeRetriesSaved,
  mergeEarlyExitCount,
  retryTaskCount,
  retryOverheadCostUsd,
  avgRetriesBeforeFailure,
  retryWasteByDirection,
  retrySuccessCount,
  retryWastedRate,
  retryRecoveryCostUsd,
  resurrectionOutcomeSummary,
  migrationHealth,
  preventionEfficacy,
  releaseQualifierInfraMetrics,
  retryPatternSummary,
  analysisOverflowSummary,
  provisionWasteCostUsd,
  provisionWasteByDirection,
  provisioningOverheadPct,
  escalationOutcomeSummary,
  retrySuccessRateByClass,
}: CatchUpOperationalMetricsSectionProps) {
  return (
    <>
      {/* Review cap exhaustion summary */}
      {reviewCapExhaustionSummary && reviewCapExhaustionSummary.count > 0 && (
        <div className="flex flex-col gap-1">
          <div className="flex flex-wrap items-center gap-1.5">
            <span className="text-[10px] font-bold text-neutral-500 uppercase tracking-widest">Review cap exhaustion</span>
            <span className="text-[10px] text-neutral-400">
              {reviewCapExhaustionSummary.count} task{reviewCapExhaustionSummary.count !== 1 ? 's' : ''} hit cap
            </span>
            {reviewCapExhaustionSummary.avg_iterations != null && (
              <span className="text-[10px] text-neutral-500">
                · avg {reviewCapExhaustionSummary.avg_iterations.toFixed(1)} iter
              </span>
            )}
            {reviewCapExhaustionSummary.total_cost_usd > 0 && (
              <span className="text-[10px] text-red-400">
                · ${reviewCapExhaustionSummary.total_cost_usd.toFixed(2)} wasted
              </span>
            )}
            {reviewCapExhaustionSummary.avg_cost_usd != null && (
              <span className="text-[10px] text-neutral-500">
                · avg ${reviewCapExhaustionSummary.avg_cost_usd.toFixed(2)}/task
              </span>
            )}
          </div>
          {reviewCapExhaustionSummary.top_directions.length > 0 && (
            <div className="flex flex-col gap-0.5 ml-1">
              {(() => {
                const dirTextById = new Map<string, string>(
                  directionProgress.map(dp => [dp.direction_id, dp.direction_text || dp.direction_id]),
                );
                return (reviewCapExhaustionSummary?.top_directions ?? []).map(d => {
                  const text = dirTextById.get(d.direction_id) ?? d.direction_id;
                  const label = text.length > 60 ? text.slice(0, 60) + '…' : text;
                  return (
                    <span key={d.direction_id} className="text-[10px] text-neutral-500 truncate">
                      · {label}{' '}
                      <span className="text-red-400">×{d.count}</span>
                    </span>
                  );
                });
              })()}
            </div>
          )}
        </div>
      )}

      {/* Merge retries saved (early-exit efficiency) */}
      {totalMergeRetriesSaved != null && totalMergeRetriesSaved > 0 && (
        <div className="flex flex-wrap items-center gap-1.5">
          <span className="text-[10px] font-bold text-neutral-500 uppercase tracking-widest">Early exit</span>
          <span className="text-[10px] text-emerald-400 inline-flex items-center gap-0.5">
            <CheckCircle className="w-2.5 h-2.5" />
            {totalMergeRetriesSaved} {totalMergeRetriesSaved === 1 ? 'retry' : 'retries'} saved
          </span>
          {mergeEarlyExitCount != null && mergeEarlyExitCount > 0 && (
            <span className="text-[10px] text-neutral-500">
              · across {mergeEarlyExitCount} task{mergeEarlyExitCount === 1 ? '' : 's'}
            </span>
          )}
        </div>
      )}

      {/* Retry overhead cost */}
      {retryTaskCount != null && retryTaskCount > 0 && (
        <div className="flex flex-wrap items-center gap-1.5">
          <span className="text-[10px] font-bold text-neutral-500 uppercase tracking-widest">Retry waste</span>
          <span className="text-[10px] text-amber-400 inline-flex items-center gap-0.5">
            <DollarSign className="w-2.5 h-2.5" />{(retryOverheadCostUsd ?? 0).toFixed(2)} wasted
          </span>
          <span className="text-[10px] text-neutral-400">
            · {retryTaskCount} over-retried task{retryTaskCount === 1 ? '' : 's'}
          </span>
          {avgRetriesBeforeFailure != null && (
            <span className="text-[10px] text-neutral-500">
              · avg {avgRetriesBeforeFailure.toFixed(1)} attempts before failure
            </span>
          )}
          {retryWasteByDirection && retryWasteByDirection.length > 0 && (
            <ul className="w-full flex flex-col gap-0.5 mt-0.5 ml-2">
              {retryWasteByDirection.map((d) => (
                <li key={d.direction_id} className="text-[10px] text-neutral-400">
                  · <span className="text-neutral-300">{d.direction_text || d.direction_id}</span>
                  <span className="text-amber-400 ml-1">${d.retry_cost_usd.toFixed(2)}</span>
                  <span className="text-neutral-500 ml-1">({d.retry_task_count} task{d.retry_task_count === 1 ? '' : 's'})</span>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}

      {/* Retry effectiveness */}
      {((retrySuccessCount ?? 0) > 0 || retryWastedRate != null) && (
        <div className="flex flex-wrap items-center gap-1.5">
          <span className="text-[10px] font-bold text-neutral-500 uppercase tracking-widest">Retry effectiveness</span>
          {(retrySuccessCount ?? 0) > 0 && (
            <span className="text-[10px] text-emerald-400">
              {retrySuccessCount} recovered
            </span>
          )}
          {retryWastedRate != null && (
            <span className="text-[10px] text-amber-400">
              · {Math.round(retryWastedRate * 100)}% wasted
            </span>
          )}
          {(retryRecoveryCostUsd ?? 0) > 0 && (
            <span className="text-[10px] text-neutral-500 inline-flex items-center gap-0.5">
              · <DollarSign className="w-2.5 h-2.5" />{(retryRecoveryCostUsd ?? 0).toFixed(2)} recovery cost
            </span>
          )}
        </div>
      )}

      {/* Resurrection outcomes */}
      {resurrectionOutcomeSummary && resurrectionOutcomeSummary.total > 0 && (
        <div className="flex flex-wrap items-center gap-1.5">
          <span className="text-[10px] font-bold text-neutral-500 uppercase tracking-widest">Resurrection outcomes</span>
          <span className="text-[10px] text-neutral-400">
            {resurrectionOutcomeSummary.total} attempted
          </span>
          <span className="text-[10px] text-emerald-400">
            · {resurrectionOutcomeSummary.completed} succeeded
          </span>
          <span className="text-[10px] text-amber-400">
            · {resurrectionOutcomeSummary.failed_again} failed again
          </span>
          {resurrectionOutcomeSummary.success_rate != null && (
            <span className="text-[10px] text-neutral-500">
              · {Math.round(resurrectionOutcomeSummary.success_rate * 100)}% success
            </span>
          )}
        </div>
      )}

      {/* Migration health */}
      {migrationHealth && (() => {
        const mh = migrationHealth;
        const highlight = mh.migration_tasks_failed > 0 || mh.migration_errors_in_window > 0 || mh.auto_generated_migrations > 0;
        return (
          <div className={`flex flex-wrap items-center gap-1.5 ${highlight ? 'rounded px-1.5 py-1 ring-1 ring-amber-500/40 bg-amber-500/5' : ''}`}>
            <span className="text-[10px] font-bold text-neutral-500 uppercase tracking-widest">Migration health</span>
            {mh.current_schema_revision && (
              <span className="text-[10px] text-neutral-400 font-mono">rev {mh.current_schema_revision}</span>
            )}
            <span className="text-[10px] text-neutral-400">· {mh.migration_tasks_in_window} task{mh.migration_tasks_in_window === 1 ? '' : 's'}</span>
            <span className="text-[10px] text-emerald-400">· {mh.migration_tasks_succeeded} ok</span>
            <span className={`text-[10px] ${mh.migration_tasks_failed > 0 ? 'text-red-400' : 'text-neutral-500'}`}>· {mh.migration_tasks_failed} failed</span>
            <span className={`text-[10px] ${mh.migration_errors_in_window > 0 ? 'text-red-400' : 'text-neutral-500'}`}>· {mh.migration_errors_in_window} errors</span>
            {mh.auto_generated_migrations > 0 && (
              <span className="text-[10px] text-amber-400 font-semibold">· ⚠ {mh.auto_generated_migrations} auto-generated</span>
            )}
          </div>
        );
      })()}

      {/* Prevention efficacy */}
      {preventionEfficacy && (() => {
        const pe = preventionEfficacy;
        const badge = pe.assessment === 'positive' ? 'text-emerald-400'
          : pe.assessment === 'negative' ? 'text-red-400'
          : pe.assessment === 'neutral' ? 'text-neutral-400' : 'text-neutral-500';
        return (
          <div className="flex flex-wrap items-center gap-1.5">
            <span className="text-[10px] font-bold text-neutral-500 uppercase tracking-widest">Prevention efficacy</span>
            <span className={`text-[10px] font-semibold uppercase ${badge}`}>{pe.assessment.replace('_', ' ')}</span>
            {pe.assessment === 'no_data' ? (
              <span className="text-[10px] text-neutral-500">· no preventive-context data in window</span>
            ) : (
              <>
                <span className="text-[10px] text-neutral-400">· preventive {pe.preventive_merge_rate != null ? `${Math.round(pe.preventive_merge_rate * 100)}%` : '—'}</span>
                <span className="text-[10px] text-neutral-500">vs overall {pe.overall_merge_rate != null ? `${Math.round(pe.overall_merge_rate * 100)}%` : '—'}</span>
                <span className="text-[10px] text-neutral-500">· {pe.preventive_tasks_merged}/{pe.preventive_tasks_created} merged</span>
              </>
            )}
          </div>
        );
      })()}

      {/* Release-qualifier infra failures */}
      {releaseQualifierInfraMetrics && (() => {
        const rq = releaseQualifierInfraMetrics;
        const alert = rq.release_infra_failure_total > 0 || rq.release_infra_failure_streak_max > 0;
        if (!alert) return null;
        return (
          <div className="flex flex-wrap items-center gap-1.5 rounded px-1.5 py-1 ring-1 ring-red-500/40 bg-red-500/5">
            <span className="text-[10px] font-bold text-red-400 uppercase tracking-widest">Release infra failures</span>
            <span className="text-[10px] text-red-400">{rq.release_infra_failure_total} of {rq.qualifier_runs_total} runs</span>
            {rq.release_infra_failure_streak_max > 0 && (
              <span className="text-[10px] text-amber-400">· streak ×{rq.release_infra_failure_streak_max}</span>
            )}
            {rq.audit_report_missing_count > 0 && (
              <span className="text-[10px] text-neutral-400">· {rq.audit_report_missing_count} audit missing</span>
            )}
            {rq.audit_report_parseable_but_unscoreable_count > 0 && (
              <span className="text-[10px] text-neutral-400">· {rq.audit_report_parseable_but_unscoreable_count} unscoreable</span>
            )}
            {rq.smoketest_report_missing_count > 0 && (
              <span className="text-[10px] text-neutral-400">· {rq.smoketest_report_missing_count} smoketest missing</span>
            )}
          </div>
        );
      })()}

      {/* Retry pattern summary */}
      {retryPatternSummary && retryPatternSummary.total_multi_attempt_failures > 0 && (
        <div className="flex flex-col gap-1">
          <div className="flex flex-wrap items-center gap-1.5">
            <span className="text-[10px] font-bold text-neutral-500 uppercase tracking-widest">Retry patterns</span>
            <span className="text-[10px] text-neutral-400">
              {retryPatternSummary.total_multi_attempt_failures} multi-attempt
            </span>
            {retryPatternSummary.total_multi_attempt_failures > 0 && (
              <span className="text-[10px] text-neutral-500">
                · {Math.round((retryPatternSummary.repeat_class_count / retryPatternSummary.total_multi_attempt_failures) * 100)}% same class
              </span>
            )}
          </div>
          {retryPatternSummary.top_patterns.length > 0 && (
            <div className="flex flex-col gap-0.5 ml-1">
              {retryPatternSummary.top_patterns.slice(0, 3).map((p) => (
                <span key={p.sequence} className="text-[10px] text-neutral-500 truncate font-mono">
                  · {p.sequence}{' '}
                  <span className="text-amber-400">×{p.count}</span>
                </span>
              ))}
            </div>
          )}
        </div>
      )}

      {/* Analysis overflow summary */}
      {analysisOverflowSummary && analysisOverflowSummary.total_count > 0 && (
        <div className="flex flex-wrap items-center gap-1.5">
          <span className="text-[10px] font-bold text-neutral-500 uppercase tracking-widest">Analysis overflow</span>
          <span className="text-[10px] text-neutral-400">
            {analysisOverflowSummary.total_count} task{analysisOverflowSummary.total_count === 1 ? '' : 's'} hit planning gate
          </span>
          {analysisOverflowSummary.cost_usd > 0 && (
            <span className="text-[10px] text-amber-400 inline-flex items-center gap-0.5">
              <DollarSign className="w-2.5 h-2.5" />{analysisOverflowSummary.cost_usd.toFixed(2)}
            </span>
          )}
          {Object.entries(analysisOverflowSummary.by_scope).map(([scope, count]) => (
            <span key={scope} className="text-[10px] text-neutral-500">
              · {scope} ×{count}
            </span>
          ))}
        </div>
      )}

      {/* Provision waste */}
      {provisionWasteCostUsd != null && provisionWasteCostUsd > 0 && (
        <div className="flex flex-col gap-1">
          <div className="flex flex-wrap items-center gap-1.5">
            <span className="text-[10px] font-bold text-neutral-500 uppercase tracking-widest">Provision waste</span>
            <span className="text-[10px] text-amber-400 inline-flex items-center gap-0.5">
              <DollarSign className="w-2.5 h-2.5" />{provisionWasteCostUsd.toFixed(2)} burned
            </span>
            {provisioningOverheadPct != null && (
              <span className="text-[10px] text-neutral-500">
                · {(provisioningOverheadPct * 100).toFixed(1)}% of total compute
              </span>
            )}
          </div>
          {provisionWasteByDirection && provisionWasteByDirection.length > 0 && (
            <ul className="w-full flex flex-col gap-0.5 mt-0.5 ml-2">
              {provisionWasteByDirection.map((d) => (
                <li key={d.direction_id} className="text-[10px] text-neutral-400">
                  · <span className="text-neutral-300">{d.direction_text || d.direction_id}</span>
                  <span className="text-amber-400 ml-1">${d.provision_cost_usd.toFixed(2)}</span>
                  <span className="text-neutral-500 ml-1">({d.provision_task_count} task{d.provision_task_count === 1 ? '' : 's'})</span>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}

      {/* Provisioning overhead (standalone if no waste block) */}
      {(provisionWasteCostUsd == null || provisionWasteCostUsd === 0) &&
        provisioningOverheadPct != null && provisioningOverheadPct > 0 && (
        <div className="flex flex-wrap items-center gap-1.5">
          <span className="text-[10px] font-bold text-neutral-500 uppercase tracking-widest">Provisioning overhead</span>
          <span className="text-[10px] text-neutral-400">
            {(provisioningOverheadPct * 100).toFixed(1)}% of total compute
          </span>
        </div>
      )}

      {/* Escalation outcomes */}
      {escalationOutcomeSummary && escalationOutcomeSummary.total_attempted > 0 && (
        <div className="flex flex-col gap-1">
          <div className="flex flex-wrap items-center gap-1.5">
            <span className="text-[10px] font-bold text-neutral-500 uppercase tracking-widest">Escalation outcomes</span>
            <span className="text-[10px] text-neutral-400">
              {escalationOutcomeSummary.total_attempted} attempted
            </span>
            <span className="text-[10px] text-emerald-400">
              · {escalationOutcomeSummary.total_merged} merged
            </span>
            <span className="text-[10px] text-red-400">
              · {escalationOutcomeSummary.total_failed} failed
            </span>
            {escalationOutcomeSummary.overall_success_rate != null && (
              <span className="text-[10px] text-neutral-500">
                · {Math.round(escalationOutcomeSummary.overall_success_rate * 100)}% success
              </span>
            )}
          </div>
          {Object.entries(escalationOutcomeSummary.by_path).length > 0 && (
            <div className="flex flex-col gap-0.5 ml-1">
              {Object.entries(escalationOutcomeSummary.by_path).map(([path, b]) => (
                <span key={path} className="text-[10px] text-neutral-500 truncate font-mono">
                  · {path} <span className="text-neutral-400">{b.merged}/{b.attempted}</span>
                  {b.success_rate != null && (
                    <span className="text-neutral-500"> ({Math.round(b.success_rate * 100)}%)</span>
                  )}
                </span>
              ))}
            </div>
          )}
        </div>
      )}

      {/* Retry success rate by class */}
      {retrySuccessRateByClass && Object.keys(retrySuccessRateByClass).length > 0 && (
        <div className="flex flex-col gap-1">
          <div className="flex flex-wrap items-center gap-1.5">
            <span className="text-[10px] font-bold text-neutral-500 uppercase tracking-widest">Retry success by class</span>
          </div>
          <div className="flex flex-col gap-0.5 ml-1">
            {Object.entries(retrySuccessRateByClass)
              .sort((a, b) => (b[1].merged + b[1].failed) - (a[1].merged + a[1].failed))
              .map(([cls, b]) => (
                <span key={cls} className="text-[10px] text-neutral-500 truncate font-mono">
                  · {cls} <span className="text-emerald-400">{b.merged}</span>
                  <span className="text-neutral-500">/</span>
                  <span className="text-red-400">{b.failed}</span>
                  <span className="text-neutral-400"> ({Math.round(b.success_rate * 100)}%)</span>
                </span>
              ))}
          </div>
        </div>
      )}
    </>
  );
}
