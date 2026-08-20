import { useState } from 'react';
import { GitBranch, ChevronDown, ChevronRight } from 'lucide-react';
import type { DependencyHealthSignals } from '../types/digest';

/**
 * Dependency/foundation-resolution health. Read-only observability for the
 * silent failure modes the dependency-resolution work exists to prevent —
 * otherwise invisible to operators and to the pipeline's own metrics:
 *   - parked blockers: BLOCKED/held tasks pushed off the recovery queue,
 *   - duplicate-foundation candidates: two tasks independently building the
 *     same files,
 *   - unresumable cancellations: blocked dependents the resume system gave up on.
 * Always renders as a true health GAUGE whenever dependency data is present:
 * an affirmative line (how many are currently blocked and waiting, how many
 * resumed this window) sits above the failure rows, which still surface when
 * non-zero. Returns null only when there is no data object at all.
 */
export function DependencyHealthCard({ data }: { data?: DependencyHealthSignals | null }) {
  const [open, setOpen] = useState<string | null>(null);
  if (!data) return null;

  const parked = data.parked_blockers?.total ?? 0;
  const dupes = data.duplicate_foundation_candidates?.total ?? 0;
  const cancels = data.unresumable_cancellations?.total ?? 0;
  const blocked = data.live_blocked?.total ?? 0;
  const resumed = data.resumes?.total ?? 0;
  const giveUps = data.finding_structural_give_up?.total ?? 0;
  const missingCap = data.missing_capability_demand?.total ?? 0;
  const anyFailure = parked > 0 || dupes > 0 || cancels > 0 || giveUps > 0 || missingCap > 0;

  const toggle = (key: string) => setOpen((cur) => (cur === key ? null : key));

  const row = (id: string, label: string, count: number, activeColor = 'text-amber-400') => (
    // eslint-disable-next-line viewport/min-touch-target -- dense dashboard panel row, not a touch toolbar
    <button
      type="button"
      onClick={() => count > 0 && toggle(id)}
      disabled={count === 0}
      className={`flex items-center justify-between w-full text-left text-ui-md py-1 ${count > 0 ? 'hover:text-neutral-200' : 'cursor-default'}`}
    >
      <span className="flex items-center gap-1 text-neutral-400">
        {count > 0 ? (
          open === id ? <ChevronDown className="w-3 h-3" /> : <ChevronRight className="w-3 h-3" />
        ) : (
          <span className="w-3" />
        )}
        {label}
      </span>
      <span className={`font-bold ${count > 0 ? activeColor : 'text-neutral-500'}`}>{count}</span>
    </button>
  );

  return (
    <div className="bg-workbench-base/50 border border-neutral-800/50 rounded-lg p-3">
      <div className="flex items-center gap-2 mb-2">
        <GitBranch className="w-3.5 h-3.5 text-amber-400 shrink-0" />
        <h3 className="text-ui-md font-bold text-neutral-400 uppercase tracking-widest">Dependency Health</h3>
      </div>

      {/* Affirmative health line — always shown so a clean fleet reads as healthy,
          not invisible. Green when there are no failures, neutral otherwise. */}
      <div className={`text-ui-md mb-2 ${anyFailure ? 'text-neutral-400' : 'text-emerald-400'}`}>
        {anyFailure ? '' : '✓ '}
        <span className="font-bold">{blocked}</span> blocked &amp; waiting
        {' · '}
        <span className="font-bold">{resumed}</span> resumed this window
      </div>

      {/* Currently-blocked population with age bucketing on expand. */}
      {row('blocked', 'Currently blocked', blocked, 'text-neutral-300')}
      {open === 'blocked' && data.live_blocked && (
        <ul className="ml-4 mb-1 space-y-0.5 text-ui-sm text-neutral-500">
          {Object.entries(data.live_blocked.by_age ?? {}).map(([bucket, n]) => (
            <li key={bucket}>
              <span className="text-neutral-400">{bucket}</span>: {n}
            </li>
          ))}
        </ul>
      )}

      {/* Successful resumes over the window with edge/completed breakdown. */}
      {row('resumes', 'Resumed this window', resumed, 'text-emerald-400')}
      {open === 'resumes' && data.resumes && (
        <ul className="ml-4 mb-1 space-y-0.5 text-ui-sm text-neutral-500">
          {Object.entries(data.resumes.by_resolved_by ?? {}).map(([kind, n]) => (
            <li key={kind}>
              <span className="text-neutral-400">{kind}</span>: {n}
            </li>
          ))}
        </ul>
      )}

      {row('parked', 'Parked blockers', parked)}
      {open === 'parked' && (
        <ul className="ml-4 mb-1 space-y-0.5 text-ui-sm text-neutral-500">
          {data.parked_blockers.tasks.map((t) => (
            <li key={t.task_id} className="truncate">
              <span className="text-neutral-400">{t.title}</span>
              {t.blocker_kind ? ` — ${t.blocker_kind}` : ''}
            </li>
          ))}
        </ul>
      )}

      {row('dupes', 'Duplicate foundations', dupes)}
      {open === 'dupes' && (
        <ul className="ml-4 mb-1 space-y-0.5 text-ui-sm text-neutral-500">
          {data.duplicate_foundation_candidates.candidates.map((c) => (
            <li key={c.task_ids.join('-')} className="truncate">
              <span className="text-neutral-400">{Math.round(c.overlap_ratio * 100)}% overlap</span>
              {c.shared_files.length ? ` — ${c.shared_files.join(', ')}` : ''}
            </li>
          ))}
        </ul>
      )}

      {row('cancels', 'Unresumable cancels', cancels)}
      {open === 'cancels' && (
        <ul className="ml-4 mb-1 space-y-0.5 text-ui-sm text-neutral-500">
          {data.unresumable_cancellations.tasks.map((t) => (
            <li key={t.task_id} className="truncate">
              <span className="text-neutral-400">{t.title}</span>
              {t.give_up_class ? ` — ${t.give_up_class}` : ''}
            </li>
          ))}
        </ul>
      )}

      {row('giveups', 'Structural give-ups', giveUps)}
      {open === 'giveups' && data.finding_structural_give_up && (
        <div className="ml-4 mb-1 space-y-1 text-ui-sm text-neutral-500">
          {Object.entries(data.finding_structural_give_up.by_reason).map(([reason, n]) => (
            <div key={reason}>
              <span className="text-neutral-400">{reason}</span>: {n}
            </div>
          ))}
          {data.finding_structural_give_up.findings.length > 0 && (
            <ul className="mt-0.5 space-y-0.5">
              {data.finding_structural_give_up.findings.map((f) => (
                <li key={f.task_id} className="truncate">
                  <span className="text-neutral-400">{f.title}</span>
                  {f.reason ? ` — ${f.reason}` : ''}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}

      {row('missingcap', 'Stranded prerequisites', missingCap)}
      {open === 'missingcap' && data.missing_capability_demand && (
        <div className="ml-4 mb-1 space-y-1 text-ui-sm text-neutral-500">
          <div>
            <span className="text-neutral-400">still blocked</span>: {data.missing_capability_demand.still_blocked}
            {' · '}
            <span className="text-neutral-400">already cancelled</span>: {data.missing_capability_demand.already_cancelled}
          </div>
          {Object.entries(data.missing_capability_demand.by_direction).map(([dir, n]) => (
            <div key={dir}>
              <span className="text-neutral-400">{dir}</span>: {n}
            </div>
          ))}
          {data.missing_capability_demand.demands.length > 0 && (
            <ul className="mt-0.5 space-y-0.5">
              {data.missing_capability_demand.demands.map((d, i) => (
                <li key={i} className="truncate">
                  <span className="text-neutral-400">{d.label ?? d.ref ?? d.kind}</span>
                  {` — ${d.dependent_count} dependent${d.dependent_count !== 1 ? 's' : ''}`}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </div>
  );
}
