import { useState, type JSX } from 'react';
import type { ChangelogEntry, VersionCriterionCoverage, CriterionCoverageEntry, EvidencePreviewEntry, QualificationCategoryVerdict } from '../api';
import { isSafeUrl } from './TaskCard';
import { EvidenceTierBadge } from './EvidenceTierBadge';

function formatExpiresIn(expiresAt: string): string {
  try {
    const diffMs = new Date(expiresAt).getTime() - Date.now();
    if (diffMs <= 0) return 'expired';
    const diffDays = Math.ceil(diffMs / (1000 * 60 * 60 * 24));
    return `expires ${diffDays}d`;
  } catch {
    return '';
  }
}

// Group changelog entries by task_type, preserving newest-first order within each group.
function groupByType(entries: ChangelogEntry[]): Map<string, ChangelogEntry[]> {
  const groups = new Map<string, ChangelogEntry[]>();
  for (const entry of entries) {
    const key = entry.task_type ?? 'other';
    const group = groups.get(key);
    if (group) {
      group.push(entry);
    } else {
      groups.set(key, [entry]);
    }
  }
  return groups;
}

function exportChangelogMarkdown(entries: ChangelogEntry[], heading?: string): string {
  if (entries.length === 0) return 'No tasks merged into this version.\n';
  const lines: string[] = [];
  if (heading) lines.push(`## ${heading}\n`);
  const groups = groupByType(entries);
  for (const [type, group] of groups.entries()) {
    lines.push(`### ${type} (${group.length})`);
    for (const entry of group) {
      const pr = entry.pr_number != null ? `#${entry.pr_number} ` : '';
      lines.push(`- ${pr}${entry.title ?? ''}`);
    }
    lines.push('');
  }
  return lines.join('\n');
}

function TaskRow({ entry }: { entry: ChangelogEntry }): JSX.Element {
  const prRef = entry.pr_number != null ? (
    entry.pr_url && isSafeUrl(entry.pr_url) ? (
      <a
        href={entry.pr_url}
        target="_blank"
        rel="noopener noreferrer"
        className="font-mono text-ui-sm text-slate-500 hover:text-slate-300 shrink-0 pt-px"
      >
        #{entry.pr_number}
      </a>
    ) : (
      <span className="font-mono text-ui-sm text-slate-500 shrink-0 pt-px">
        #{entry.pr_number}
      </span>
    )
  ) : null;

  return (
    <li className="flex items-start gap-2 py-0.5">
      {prRef}
      <span className="text-xs text-slate-300 break-words min-w-0">{entry.title}</span>
    </li>
  );
}

function TypeSection({
  taskType,
  entries,
  defaultOpen,
}: {
  taskType: string;
  entries: ChangelogEntry[];
  defaultOpen?: boolean;
}): JSX.Element {
  const [open, setOpen] = useState(defaultOpen ?? true);

  return (
    <div className="mb-2 last:mb-0">
      <button
        onClick={() => setOpen((v) => !v)}
        data-testid={`type-section-toggle-${taskType}`}
        className="flex items-center gap-1 w-full text-left min-h-[44px] py-0.5 group"
        aria-expanded={open}
      >
        <span className="text-ui-sm font-semibold uppercase tracking-wide text-slate-500 group-hover:text-slate-400 transition-colors">
          {taskType}
        </span>
        <span className="text-slate-600 font-normal text-ui-sm">({entries.length})</span>
        <span className="ml-auto text-ui-sm text-slate-600 group-hover:text-slate-400 transition-colors">
          {open ? '▾' : '▸'}
        </span>
      </button>
      {open && (
        <ul className="space-y-0.5 pl-1 pb-1">
          {entries.map((entry, i) => (
            <TaskRow key={i} entry={entry} />
          ))}
        </ul>
      )}
    </div>
  );
}

function TypeCountBadge({ taskType, count }: { taskType: string; count: number }): JSX.Element {
  return (
    <span className="inline-flex items-center gap-0.5 rounded bg-slate-700/60 px-1.5 py-0.5 text-ui-sm text-slate-400">
      <span className="font-medium">{count}</span>
      <span className="text-slate-500">{taskType}</span>
    </span>
  );
}

const GAP_REASON_LABELS: Record<string, string> = {
  no_criteria: 'no criteria',
  verdict_not_proven: 'verdict not proven',
  no_active_criteria: 'no active criteria',
  no_open_version: 'no open version',
};

function MergeEvidenceGapBreakdown({ gaps }: { gaps: Record<string, number | string[]> | null | undefined }): JSX.Element | null {
  if (gaps == null) return null;
  const entries = Object.entries(gaps)
    .filter(([key, val]) => key !== 'unmatched_criterion_ids' && typeof val === 'number' && val > 0)
    .map(([key, val]) => ({
      key,
      count: val as number,
      label: GAP_REASON_LABELS[key] ?? key.replace(/_/g, ' '),
    }));
  if (entries.length === 0) return null;

  const unmatchedIds: string[] = (Array.isArray(gaps.unmatched_criterion_ids) ? gaps.unmatched_criterion_ids : []) as string[];

  return (
    <span className="flex flex-wrap items-center gap-x-2 gap-y-0.5" data-testid="merge-evidence-gap-breakdown">
      {entries.map(e => (
        <span key={e.key} className="text-ui-sm text-amber-400/70" data-testid={`gap-reason-${e.key}`}>
          {e.count} {e.label}
        </span>
      ))}
      {unmatchedIds.length > 0 && (
        <span
          className="text-ui-sm text-amber-400/50 cursor-default"
          data-testid="gap-unmatched-ids"
          title={`Unmatched criterion IDs: ${unmatchedIds.join(', ')}`}
        >
          {unmatchedIds.length} unmatched
        </span>
      )}
    </span>
  );
}

const BUCKET_META: Record<string, { label: string; className: string }> = {
  undeclared:                { label: 'no criteria',    className: 'text-amber-400' },
  declared_unsatisfied:      { label: 'criteria failed', className: 'text-red-400' },
  satisfied_unproven:        { label: 'satisfied',       className: 'text-slate-400' },
  merged_unverified_degraded:{ label: 'degraded',        className: 'text-amber-400' },
  malformed:                 { label: 'malformed',       className: 'text-red-400' },
};


const STATUS_BADGE: Record<NonNullable<CriterionCoverageEntry['status']>, { label: string; className: string }> = {
  active:     { label: 'active',     className: 'bg-slate-700/50 text-slate-500 border border-slate-600/40' },
  retracted:  { label: 'retracted',  className: 'bg-rose-500/15 text-rose-300 border border-rose-500/30' },
  superseded: { label: 'superseded', className: 'bg-sky-500/15 text-sky-300 border border-sky-500/30' },
};

function EvidenceRow({ entry, idx, criterionKey }: { entry: EvidencePreviewEntry; idx: number; criterionKey: string }): JSX.Element {
  const provenance = entry.provenance;
  const provenanceLabel = provenance
    ? [provenance.executing_backend, provenance.runner_hostname ?? provenance.runner_id]
        .filter(Boolean)
        .join(' / ')
    : null;
  return (
    <li
      className="flex items-center gap-1.5 text-ui-sm text-slate-400"
      data-testid={`criterion-evidence-${criterionKey}-${idx}`}
    >
      <EvidenceTierBadge tier={entry.tier ?? null} />
      <span className="text-slate-500">{entry.source_kind}</span>
      {provenanceLabel && (
        <span className="text-slate-600 truncate" title={provenanceLabel}>{provenanceLabel}</span>
      )}
      {entry.recorded_at && (
        <span className="text-slate-600 ml-auto shrink-0">{new Date(entry.recorded_at).toLocaleDateString()}</span>
      )}
    </li>
  );
}

function CriterionCoveragePanel({ coverage, onReinstate, onAttest, onLinkDirection }: { coverage: VersionCriterionCoverage; onReinstate?: (criterionKey: string) => Promise<void>; onAttest?: (criterionKey: string) => void; onLinkDirection?: (criterionKey: string) => void }): JSX.Element {
  const phantom = coverage.phantom_bookkeeping ?? null;
  const hasDrift = phantom !== null && phantom.has_drift;
  const phantomKeys = phantom?.phantom_keys ?? [];
  if (coverage.criteria.length === 0) {
    return (
      <div className="mb-3" data-testid="criterion-coverage-panel">
        <div className="text-ui-sm font-semibold uppercase tracking-wide text-slate-500 mb-1">Version acceptance criteria</div>
        {hasDrift ? (
          <div className="flex flex-wrap items-center gap-x-2 gap-y-0.5">
            <span className="text-ui-sm text-slate-500 italic">No criteria registered</span>
            <span
              className="text-ui-sm text-amber-400"
              data-testid="phantom-drift-badge"
              title="Criterion keys are referenced in direction prose or version goal but have no registered criteria — rubric may be broken or mis-keyed"
            >
              {phantomKeys.length > 0
                ? `${phantomKeys.length} phantom key${phantomKeys.length === 1 ? '' : 's'} referenced`
                : 'keys referenced but none registered'}
            </span>
          </div>
        ) : (
          <div className="text-ui-sm text-slate-500 italic">No acceptance criteria defined for this version.</div>
        )}
        <MergeEvidenceGapBreakdown gaps={coverage.merge_evidence_gaps} />
      </div>
    );
  }

  const rowInactiveCount = coverage.criteria.filter((c) => (c.status ?? 'active') !== 'active').length;
  const inactiveCount = coverage.inactive ?? rowInactiveCount;
  const retractedCount = coverage.retracted ?? coverage.criteria.filter((c) => c.status === 'retracted').length;
  const supersededCount = coverage.superseded ?? coverage.criteria.filter((c) => c.status === 'superseded').length;
  // Attested criteria are a distinct, lower-confidence tier (operator's word,
  // not machine-verified) and are NOT part of `covered`. Older backends omit
  // the field entirely — treat undefined as 0.
  const attestedCount = coverage.attested ?? 0;
  const unmappedCount = coverage.criteria.filter(
    (c) => (c.status ?? 'active') === 'active'
      && c.coverage === 'uncovered'
      && c.stuck_reason === 'no_mapping',
  ).length;
  const awaitingEvidenceCount = coverage.criteria.filter(
    (c) => (c.status ?? 'active') === 'active'
      && c.coverage === 'uncovered'
      && c.stuck_reason == null,
  ).length;

  return (
    <div className="mb-3" data-testid="criterion-coverage-panel">
      <div className="flex flex-wrap items-center gap-x-2 gap-y-0.5 mb-1">
        <span className="text-ui-sm font-semibold uppercase tracking-wide text-slate-500">Machine-verified criterion coverage</span>
        <span className="text-ui-sm text-slate-400 font-medium" data-testid="criterion-coverage-fraction">
          {coverage.total === 0
            ? '0 active criteria'
            : attestedCount > 0
              ? `${coverage.covered}/${coverage.total} machine-verified + ${attestedCount} attested`
              : `${coverage.covered}/${coverage.total} machine-verified`}
        </span>
        {attestedCount > 0 && (
          <span
            className="text-ui-sm text-amber-400"
            data-testid="attested-count-badge"
            title="Covered only by operator attestation (not machine-verified) — lower confidence and expiring"
          >
            {attestedCount} attested
          </span>
        )}
        {(coverage.backfilled ?? 0) > 0 && <span className="text-ui-sm text-slate-500" data-testid="backfilled-count-badge">{coverage.backfilled} backfilled</span>}
        {(coverage.probed ?? 0) > 0 && <span className="text-ui-sm text-sky-300" data-testid="probed-count-badge">{coverage.probed} probed</span>}
        {coverage.partial > 0 && <span className="text-ui-sm text-amber-400">{coverage.partial} partial</span>}
        {coverage.uncovered > 0 && <span className="text-ui-sm text-slate-500">{coverage.uncovered} uncovered</span>}
        {unmappedCount > 0 && (
          <span className="text-ui-sm text-red-400" data-testid="unmapped-criteria-count">
            {unmappedCount} unmapped
          </span>
        )}
        {awaitingEvidenceCount > 0 && (
          <span className="text-ui-sm text-blue-300" data-testid="awaiting-evidence-count">
            {awaitingEvidenceCount} awaiting evidence
          </span>
        )}
        {inactiveCount > 0 && (
          <span className="text-ui-sm text-slate-500" data-testid="inactive-count-badge">{inactiveCount} inactive</span>
        )}
        {retractedCount > 0 && <span className="text-ui-sm text-rose-300">{retractedCount} retracted</span>}
        {supersededCount > 0 && <span className="text-ui-sm text-sky-300">{supersededCount} superseded</span>}
        {coverage.stuck_count != null && coverage.stuck_count > 0 && (
          <span className="text-ui-sm text-red-400" data-testid="stuck-count-badge" title="Uncovered criteria with no active direction mapping — need remapping or operator attestation">
            {coverage.stuck_count} stuck
          </span>
        )}
        {hasDrift && phantomKeys.length > 0 && (
          <span
            className="text-ui-sm text-amber-400"
            data-testid="phantom-drift-badge"
            title="Criterion keys referenced in prose that have no registered criteria entry — dangling references"
          >
            {phantomKeys.length} phantom key{phantomKeys.length === 1 ? '' : 's'}
          </span>
        )}
      </div>
      <MergeEvidenceGapBreakdown gaps={coverage.merge_evidence_gaps} />
      <ul className="space-y-1">
        {coverage.criteria.map((c) => {
          const status = c.status ?? 'active';
          const statusBadge = STATUS_BADGE[status];
          const stuckLabel = c.stuck_reason === 'no_mapping'
            ? 'no direction'
            : c.stuck_reason === 'direction_retired'
              ? 'direction retired'
              : c.stuck_reason === 'direction_inactive'
                ? 'direction inactive'
                : null;
          const isInProgress = status === 'active' && c.coverage === 'uncovered' && stuckLabel == null;
          return (
            <li
              key={c.criterion_key}
              className={status !== 'active' ? 'opacity-80' : ''}
              data-testid={`criterion-row-${c.criterion_key}`}
            >
              <div className="flex items-start gap-2">
                <EvidenceTierBadge tier={c.coverage} />
                {status !== 'active' && (
                  <span
                    className={`shrink-0 rounded px-1.5 py-0.5 text-ui-sm font-medium ${statusBadge.className}`}
                    data-testid={`criterion-status-${c.criterion_key}`}
                  >
                    {statusBadge.label}
                  </span>
                )}
                <span className="text-xs text-slate-300 break-words min-w-0">{c.text}</span>
                {stuckLabel != null && (
                  <span className="shrink-0 text-ui-sm text-red-400 ml-1" data-testid={`stuck-reason-${c.criterion_key}`} title={`Stuck: ${c.stuck_reason}`}>
                    {stuckLabel}
                  </span>
                )}
                {isInProgress && (
                  <span className="shrink-0 text-ui-sm text-slate-400 ml-1" data-testid={`in-progress-${c.criterion_key}`} title="Active direction is working on this criterion">
                    in progress
                  </span>
                )}
                {c.coverage === 'attested' && c.attestation_expires_at != null && (
                  <span
                    className="shrink-0 text-ui-sm text-amber-500 ml-1"
                    data-testid={`attestation-expiry-${c.criterion_key}`}
                    title={`Attestation expires at ${c.attestation_expires_at}`}
                  >
                    {formatExpiresIn(c.attestation_expires_at)}
                  </span>
                )}
                {c.evidence_count > 0 && (
                  <span className="shrink-0 text-ui-sm text-slate-500 ml-auto">{c.evidence_count} ev.</span>
                )}
                {status === 'active' && stuckLabel != null && onAttest != null && (
                  <button
                    className="shrink-0 text-ui-sm text-amber-400 hover:text-amber-200 ml-1 transition-colors"
                    data-testid={`attest-btn-${c.criterion_key}`}
                    onClick={() => onAttest(c.criterion_key)}
                  >
                    Attest
                  </button>
                )}
                {status === 'active' && stuckLabel != null && onLinkDirection != null && (
                  <button
                    className="shrink-0 text-ui-sm text-sky-400 hover:text-sky-200 ml-1 transition-colors"
                    data-testid={`link-direction-btn-${c.criterion_key}`}
                    onClick={() => onLinkDirection(c.criterion_key)}
                  >
                    Link direction
                  </button>
                )}
                {isInProgress && onLinkDirection != null && (
                  <button
                    className="shrink-0 text-ui-sm text-sky-400 hover:text-sky-200 ml-1 transition-colors"
                    data-testid={`relink-direction-btn-${c.criterion_key}`}
                    onClick={() => onLinkDirection(c.criterion_key)}
                  >
                    Relink
                  </button>
                )}
                {status !== 'active' && onReinstate != null && (
                  <button
                    className="shrink-0 text-ui-sm text-sky-400 hover:text-sky-200 ml-1 transition-colors"
                    data-testid={`reinstate-btn-${c.criterion_key}`}
                    onClick={() => onReinstate(c.criterion_key)}
                  >
                    Reinstate
                  </button>
                )}
              </div>
              {c.evidence != null && c.evidence.length > 0 && (
                <ul className="ml-5 mt-0.5 space-y-0.5">
                  {c.evidence.map((ev, idx) => (
                    <EvidenceRow key={idx} entry={ev} idx={idx} criterionKey={c.criterion_key} />
                  ))}
                </ul>
              )}
            </li>
          );
        })}
      </ul>
    </div>
  );
}

const CATEGORY_VERDICT_STATUS: Record<string, { label: string; className: string }> = {
  not_met:    { label: 'not met',    className: 'bg-red-500/15 text-red-300 border border-red-500/30' },
  partial:    { label: 'partial',    className: 'bg-amber-500/15 text-amber-300 border border-amber-500/30' },
  unverified: { label: 'unverified', className: 'bg-orange-500/15 text-orange-300 border border-orange-500/30' },
  met:        { label: 'met',        className: 'bg-emerald-500/15 text-emerald-300 border border-emerald-500/30' },
};

function QualificationCategoryVerdictsBlock({
  verdicts,
  residualGap,
}: {
  verdicts?: QualificationCategoryVerdict[] | null;
  residualGap?: string | null;
}): JSX.Element | null {
  const unmet = (verdicts ?? []).filter((v) => v.status !== 'met');
  if (unmet.length === 0 && !residualGap) return null;
  return (
    <div className="mb-3" data-testid="qualification-category-verdicts">
      <div className="text-ui-sm font-semibold uppercase tracking-wide text-slate-500 mb-1">
        Why promotion is blocked — category verdicts
      </div>
      <ul className="space-y-0.5">
        {unmet.map((v) => {
          const statusBadge = CATEGORY_VERDICT_STATUS[v.status] ?? {
            label: v.status,
            className: 'bg-slate-700/50 text-slate-400 border border-slate-600/40',
          };
          return (
            <li key={v.category_key} className="flex items-start gap-2" data-testid={`category-verdict-${v.category_key}`}>
              <span className={`shrink-0 rounded px-1.5 py-0.5 text-ui-sm font-medium ${statusBadge.className}`}>
                {statusBadge.label}
              </span>
              {v.tier != null && v.tier !== 'judged' && (
                <span className="shrink-0 text-ui-sm text-slate-500">{v.tier}</span>
              )}
              <span className="font-mono text-ui-sm text-slate-400 shrink-0">{v.category_key}</span>
              {v.gap && <span className="text-xs text-slate-300 break-words min-w-0">{v.gap}</span>}
            </li>
          );
        })}
      </ul>
      {residualGap && (
        <p className="text-ui-sm text-amber-400/80" data-testid="residual-uncaptured-gap">
          Residual gap (no criterion captures this): {residualGap}
        </p>
      )}
    </div>
  );
}

function AcceptanceBucketBreakdown({ aggregate }: { aggregate: Record<string, number> }): JSX.Element | null {
  const entries = Object.entries(BUCKET_META)
    .map(([key, meta]) => ({ key, count: aggregate[key] ?? 0, ...meta }))
    .filter(e => e.count > 0);
  if (entries.length === 0) return null;
  return (
    <div className="flex flex-wrap gap-x-3 gap-y-0.5 text-ui-sm mb-2" data-testid="acceptance-bucket-breakdown">
      {entries.map(e => (
        <span key={e.key} className={e.className}>
          {e.count} {e.label}
        </span>
      ))}
    </div>
  );
}

export function VersionSummaryPanel({
  tasksMerged,
  heading,
  acceptanceAggregate,
  criterionCoverage,
  categoryVerdicts,
  residualUncapturedGap,
  onReinstate,
  onAttest,
  onLinkDirection,
}: {
  tasksMerged: ChangelogEntry[] | undefined;
  heading?: string;
  acceptanceAggregate?: Record<string, number> | null;
  criterionCoverage?: VersionCriterionCoverage | null;
  categoryVerdicts?: QualificationCategoryVerdict[] | null;
  residualUncapturedGap?: string | null;
  onReinstate?: (criterionKey: string) => Promise<void>;
  onAttest?: (criterionKey: string) => void;
  onLinkDirection?: (criterionKey: string) => void;
}): JSX.Element | null {
  const [copied, setCopied] = useState(false);
  const [filter, setFilter] = useState('');

  if (tasksMerged === undefined) return null;

  if (tasksMerged.length === 0) {
    return (
      <div
        data-testid="version-summary-panel-empty"
        className="mt-3 rounded border border-slate-700/60 bg-slate-800/40 px-3 py-2"
      >
        {criterionCoverage != null && <CriterionCoveragePanel coverage={criterionCoverage} onReinstate={onReinstate} onAttest={onAttest} onLinkDirection={onLinkDirection} />}
        <QualificationCategoryVerdictsBlock verdicts={categoryVerdicts} residualGap={residualUncapturedGap} />
        <div className="text-ui-md text-slate-500 italic">No tasks merged into this version yet.</div>
      </div>
    );
  }

  const normalizedFilter = filter.trim().toLowerCase();
  const filteredEntries = normalizedFilter
    ? tasksMerged.filter((e) => {
        const titleMatch = (e.title ?? '').toLowerCase().includes(normalizedFilter);
        const prMatch = e.pr_number != null && String(e.pr_number).includes(normalizedFilter);
        return titleMatch || prMatch;
      })
    : tasksMerged;

  const groups = groupByType(filteredEntries);

  const handleCopyMarkdown = () => {
    const md = exportChangelogMarkdown(tasksMerged, heading ?? 'What shipped');
    navigator.clipboard.writeText(md).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    }).catch(() => {});
  };

  // Default: collapse sections when total tasks > 20 to keep the panel scannable
  const defaultOpen = tasksMerged.length <= 20;

  return (
    <div
      data-testid="version-summary-panel"
      className="mt-3 rounded border border-slate-700/60 bg-slate-800/40 px-3 py-2"
    >
      <div className="flex items-center justify-between mb-2">
        <div className="text-ui-md font-semibold text-slate-400">
          {heading ?? 'What shipped'} ({tasksMerged.length} task{tasksMerged.length !== 1 ? 's' : ''})
        </div>
        <button
          onClick={handleCopyMarkdown}
          data-testid="copy-markdown-button"
          className="text-ui-sm text-slate-500 hover:text-slate-300 transition-colors min-h-[44px] min-w-[44px] flex items-center justify-center"
          title="Copy changelog as Markdown"
        >
          {copied ? 'Copied!' : 'Copy markdown'}
        </button>
      </div>
      {criterionCoverage != null && <CriterionCoveragePanel coverage={criterionCoverage} onReinstate={onReinstate} onAttest={onAttest} onLinkDirection={onLinkDirection} />}
      <QualificationCategoryVerdictsBlock verdicts={categoryVerdicts} residualGap={residualUncapturedGap} />
      {acceptanceAggregate && <AcceptanceBucketBreakdown aggregate={acceptanceAggregate} />}
      {/* Per-type count summary — lets operator grasp shape of release at a glance */}
      <div className="flex flex-wrap gap-1 mb-2" data-testid="type-count-summary">
        {Array.from(groupByType(tasksMerged).entries()).map(([type, entries]) => (
          <TypeCountBadge key={type} taskType={type} count={entries.length} />
        ))}
      </div>
      <input
        type="search"
        data-testid="changelog-filter-input"
        placeholder="Filter by title or PR…"
        value={filter}
        onChange={(e) => setFilter(e.target.value)}
        className="w-full mb-2 rounded bg-slate-700/50 border border-slate-600/50 px-2 py-1 text-ui-md text-slate-300 placeholder-slate-500 outline-none workbench-focus"
        aria-label="Filter changelog entries"
      />
      <div className="max-h-64 overflow-y-auto pr-1">
        {filteredEntries.length === 0 ? (
          <div data-testid="changelog-filter-no-results" className="text-ui-md text-slate-500 italic py-1">
            No entries match &ldquo;{filter}&rdquo;.
          </div>
        ) : (
          Array.from(groups.entries()).map(([type, entries]) => (
            <TypeSection key={type} taskType={type} entries={entries} defaultOpen={defaultOpen} />
          ))
        )}
      </div>
    </div>
  );
}
