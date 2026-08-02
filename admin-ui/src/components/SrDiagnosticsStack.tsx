import { type PromptFingerprintAnalyticsResponse } from '../api';
import { SrMissedRetrospective, DirectionSrBreakdownPanel, type SrMissedTask, type DirectionSrEntry } from './SrTrendBreakdown';
import { SrUnhandledEdgesRetrospectivesPanel } from './SrUnhandledEdgesRetrospectivesPanel';
import { type SrTrendWithFixIterations } from './ReviewQualitySection';

// ---------- SR-only data-shape types ----------

// Per-direction SR catch-rate breakdown (endpoint: GET /projects/{id}/direction-sr-breakdown)
// DirectionSrEntry is re-exported from SrTrendBreakdown (canonical model)
export interface DirectionSrBreakdownData {
  directions: DirectionSrEntry[];
  min_sample: number;
}

interface BlindSpotExample {
  text: string;
  task_id: string;
  task_type: string;
}

interface BlindSpotTheme {
  theme: string;
  count: number;
  examples?: BlindSpotExample[];
  layer_distribution?: Record<string, number>;
}

export interface BlindSpotsData {
  themes: BlindSpotTheme[];
  total_distinct_themes: number;
  window_days: number;
}

// ---------- SR diagnostics stack ----------

interface SrDiagnosticsStackProps {
  loading: boolean;
  selectedProjectId: string | null;
  /**
   * Resolve and navigate to a task by id, fetching it when absent from the
   * dashboard's in-memory task slice. Used by SR drill-throughs that may
   * reference tasks outside the currently-loaded list.
   */
  onViewTaskById: (taskId: string) => void;
  onDirectionClick?: (directionId: string) => void;
  srRecentMisses: SrMissedTask[] | null;
  srRecentMissesTotal: number;
  selectedSrCategory: string;
  blindSpots: BlindSpotsData | null;
  expandedBlindSpotThemes: Set<string>;
  onToggleBlindSpotTheme: (theme: string) => void;
  directionSrBreakdown: DirectionSrBreakdownData | null;
  srTrends?: SrTrendWithFixIterations | null;
  fingerprintAnalytics: PromptFingerprintAnalyticsResponse | null;
}

export function SrDiagnosticsStack({
  loading,
  selectedProjectId,
  onViewTaskById,
  onDirectionClick,
  srRecentMisses,
  srRecentMissesTotal,
  selectedSrCategory,
  blindSpots,
  expandedBlindSpotThemes,
  onToggleBlindSpotTheme,
  directionSrBreakdown,
  srTrends,
  fingerprintAnalytics,
}: SrDiagnosticsStackProps) {
  return (
    <>
      {/* SR Recent Misses */}
      {!loading && srRecentMisses && srRecentMisses.length > 0 && (
        <section>
          <p className="text-ui-sm text-neutral-500 uppercase tracking-wide mb-2">
            Recent SR Misses
            <span className="ml-1 text-neutral-600 font-normal normal-case tracking-normal">· {srRecentMissesTotal} task{srRecentMissesTotal !== 1 ? 's' : ''}</span>
          </p>
          <SrMissedRetrospective
            tasks={srRecentMisses}
            selectedCategory={selectedSrCategory}
            onTaskClick={onViewTaskById}
          />
        </section>
      )}

      {/* SR Unhandled Edges */}
      <SrUnhandledEdgesRetrospectivesPanel
        selectedProjectId={selectedProjectId}
        onSelectTask={onViewTaskById}
      />

      {/* SR Blind Spots aggregation */}
      {!loading && blindSpots && blindSpots.themes.length > 0 && (
        <section>
          <p className="text-ui-sm text-neutral-500 uppercase tracking-wide mb-2">
            SR Blind Spots
            <span className="ml-1 text-neutral-600 font-normal normal-case tracking-normal">· {blindSpots.total_distinct_themes} themes · {blindSpots.window_days}d</span>
          </p>
          <div className="flex flex-wrap gap-2">
            {blindSpots.themes.slice(0, 8).map((t, i) => {
              const isExpanded = expandedBlindSpotThemes.has(t.theme);
              return (
                <div key={i} className="flex flex-col gap-0.5 min-w-0">
                  <button
                    onClick={() => onToggleBlindSpotTheme(t.theme)}
                    className="inline-flex items-center gap-1.5 text-ui-sm font-semibold px-2 py-0.5 rounded border bg-violet-500/10 text-violet-400 border-violet-500/20 cursor-pointer hover:bg-violet-500/20 transition-colors min-h-[44px]"
                    title={t.theme}
                  >
                    <span className="truncate max-w-[160px]">{t.theme}</span>
                    <span className="font-bold shrink-0">×{t.count}</span>
                  </button>
                  {isExpanded && t.examples && t.examples.length > 0 && (
                    <div className="flex flex-col gap-1 ml-1 border-l border-violet-500/20 pl-2">
                      {t.examples.map((ex, exIdx) => (
                        <div key={exIdx} className="text-ui-xs text-neutral-300">
                          <span className="text-neutral-500 block mb-0.5">{ex.text}</span>
                          <button
                            onClick={() => onViewTaskById(ex.task_id)}
                            className="text-violet-400 hover:text-violet-300 cursor-pointer underline"
                          >
                            View task
                          </button>
                        </div>
                      ))}
                    </div>
                  )}
                  {t.layer_distribution && Object.keys(t.layer_distribution).length > 0 && (
                    <div className="flex flex-wrap gap-1">
                      {Object.entries(t.layer_distribution)
                        .sort((a, b) => b[1] - a[1])
                        .map(([layer, cnt]) => (
                          <span
                            key={layer}
                            className="inline-flex items-center gap-0.5 text-ui-xs px-1.5 py-0.5 rounded border bg-neutral-500/10 text-neutral-400 border-neutral-500/20 max-w-[140px]"
                          >
                            <span className="truncate">{layer}</span>
                            <span className="shrink-0">×{cnt}</span>
                          </span>
                        ))}
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        </section>
      )}

      {/* Per-Direction SR Catch-Rate */}
      {!loading && directionSrBreakdown && directionSrBreakdown.directions.length > 0 && (
        <section data-testid="direction-sr-breakdown">
          <p className="text-ui-sm text-neutral-500 uppercase tracking-wide mb-2">
            SR Catch-Rate by Direction
            <span className="ml-1 text-neutral-600 font-normal normal-case tracking-normal">
              · {directionSrBreakdown.directions.length} direction{directionSrBreakdown.directions.length !== 1 ? 's' : ''} · worst-first
            </span>
          </p>
          <DirectionSrBreakdownPanel
            entries={directionSrBreakdown.directions}
            onDirectionClick={onDirectionClick}
            trends={srTrends ?? undefined}
          />
        </section>
      )}

      {/* Prompt Fingerprint SR Quality */}
      {!loading && fingerprintAnalytics && fingerprintAnalytics.fingerprints.length > 0 && (
        <section data-testid="prompt-fingerprint-sr-quality">
          <p className="text-ui-sm text-neutral-500 uppercase tracking-wide mb-2">
            Prompt Fingerprint SR Quality
            <span className="ml-1 text-neutral-600 font-normal normal-case tracking-normal">
              · {fingerprintAnalytics.fingerprints.length} regime{fingerprintAnalytics.fingerprints.length !== 1 ? 's' : ''} · {fingerprintAnalytics.window_days}d
            </span>
          </p>
          <div className="flex flex-col gap-1">
            {fingerprintAnalytics.fingerprints.map((fp) => {
              const catchRate = fp.sr_catch_rate;
              const catchColor = catchRate == null ? 'text-neutral-500' : catchRate >= 0.7 ? 'text-emerald-400' : catchRate >= 0.4 ? 'text-amber-400' : 'text-red-400';
              const delta = fp.sr_catch_rate_delta_vs_project_mean;
              const deltaColor = delta == null ? '' : delta >= 0 ? 'text-emerald-400' : 'text-red-400';
              return (
                <div key={fp.fingerprint} className="bg-neutral-900/50 border border-neutral-800/50 rounded px-2 py-1.5 text-ui-md">
                  <div className="flex items-center gap-2 mb-0.5">
                    <span className="font-mono text-neutral-400 text-ui-sm">{fp.fingerprint.slice(0, 12)}…</span>
                    <span className="text-neutral-600 text-ui-sm">n={fp.sample_size}</span>
                  </div>
                  <div className="flex flex-wrap gap-x-3 gap-y-0.5 text-ui-sm text-neutral-500">
                    <span>catch <span className={`font-semibold ${catchColor}`}>{catchRate != null ? `${Math.round(catchRate * 100)}%` : '--'}</span>{' '}<span className="text-neutral-600">n={fp.sr_catch_sample_size}</span>{delta != null && <span className={`ml-1 ${deltaColor}`}>({delta >= 0 ? '+' : ''}{Math.round(delta * 100)}pp)</span>}</span>
                    {fp.sr_false_positive_rate != null && <span>FP <span className="font-semibold text-neutral-300">{Math.round(fp.sr_false_positive_rate * 100)}%</span>{' '}<span className="text-neutral-600">n={fp.sr_false_positive_sample_size}</span></span>}
                  </div>
                </div>
              );
            })}
          </div>
        </section>
      )}
    </>
  );
}
