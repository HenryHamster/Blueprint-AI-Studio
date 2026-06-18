import { AlertTriangle, CheckCircle, ShieldCheck } from 'lucide-react';
import type { MergeGateRuntime, MergeVerificationVerdict } from '../types/project';
import { formatAbsolute, parseTimestamp } from '../utils/time';
import { humanizeUnverifiedReason } from '../utils/unverifiedReasonLabels';
import { formatMergeGateSummary } from '../utils/mergeGateSummary';

interface MergeVerificationBannerProps {
  /** Backend-computed verdict. Renders nothing when null/undefined (older responses). */
  verdict?: MergeVerificationVerdict | null;
  /** Optional runtime evidence from the live no-CI merge gate. */
  runtime?: MergeGateRuntime | null;
  /** When true, the honest-block policy is active: degraded fall-opens are held
   *  instead of merging unverified. Only relevant when the no-CI gate is active. */
  blockDegradedEnabled?: boolean;
  className?: string;
}

/**
 * Three-state readiness banner telling the operator — before any merge happens —
 * whether Blueprint's merge gate will run their tests or merge PRs UNVERIFIED.
 *
 * The verdict is the backend's single source of truth ({@link MergeVerificationVerdict});
 * this component only renders it and never re-derives gate behavior from raw config.
 * Shared between the onboarding success step (NewProjectModal) and project settings
 * (ProjectBuildConfigSection) so both surfaces stay consistent.
 */
export function MergeVerificationBanner({ verdict, runtime, blockDegradedEnabled, className }: MergeVerificationBannerProps) {
  if (!verdict) return null;

  const wrap = `rounded-lg border px-3 py-2.5 ${className ?? ''}`;
  const verifiedAt = runtime?.executed_real_tests && runtime.last_run_at ? parseTimestamp(runtime.last_run_at) : null;
  const unverifiedAt = runtime?.last_unverified_at ? parseTimestamp(runtime.last_unverified_at) : null;
  const unverifiedReason = runtime?.last_unverified_reason ? humanizeUnverifiedReason(runtime.last_unverified_reason) : null;
  const summaryLine = formatMergeGateSummary(runtime?.summary);
  const lastBlockAt = runtime?.last_degraded_block_at ? parseTimestamp(runtime.last_degraded_block_at) : null;
  const lastBlockReason = runtime?.last_degraded_block_reason ? humanizeUnverifiedReason(runtime.last_degraded_block_reason) : null;
  const mismatch = verdict.state === 'tests_gated' && Boolean(unverifiedAt && (!verifiedAt || unverifiedAt.getTime() > verifiedAt.getTime()));
  const runtimeState = runtime
    ? (mismatch
      ? 'unverified-fallback'
      : verifiedAt
        ? 'verified'
        : unverifiedAt
          ? 'unverified'
          : 'unknown')
    : 'absent';

  if (verdict.state === 'ci_checks') {
    return (
      <div
        className={`${wrap} bg-blue-500/5 border-blue-500/20`}
        data-testid="merge-verification-banner"
        data-verdict-state="ci_checks"
        data-runtime-state={runtimeState}
      >
        <div className="flex items-start gap-2">
          <ShieldCheck className="w-3.5 h-3.5 text-blue-400 shrink-0 mt-0.5" />
          <div className="min-w-0">
            <p className="text-[11px] font-semibold text-blue-300">
              Merges wait for the repository's GitHub CI checks
            </p>
            <p className="text-[10px] text-neutral-400 mt-0.5">
              Blueprint will not merge a PR until the repository's required GitHub checks pass.
            </p>
            {summaryLine ? (
              <p className="text-[10px] text-neutral-400 mt-1 break-words" data-testid="merge-gate-summary">
                {summaryLine}
              </p>
            ) : null}
          </div>
        </div>
      </div>
    );
  }

  if (verdict.state === 'tests_gated') {
    const title = mismatch
      ? `Config says tests-gated, but the last live merge fell back to UNVERIFIED${unverifiedAt ? ` at ${formatAbsolute(unverifiedAt.getTime())}` : ''}${unverifiedReason ? ` because ${unverifiedReason}.` : '.'}`
      : `Blueprint runs these command(s) on the PR head as the merge verdict${verifiedAt ? `. Last verified real-test run: ${formatAbsolute(verifiedAt.getTime())}.` : '.'}`;
    return (
      <div
        className={`${wrap} ${mismatch ? 'bg-amber-500/5 border-amber-500/20' : 'bg-emerald-500/5 border-emerald-500/20'}`}
        data-testid="merge-verification-banner"
        data-verdict-state="tests_gated"
        data-runtime-state={runtimeState}
        title={title}
      >
        <div className="flex items-start gap-2">
          {mismatch ? (
            <AlertTriangle className="w-3.5 h-3.5 text-amber-400 shrink-0 mt-0.5" />
          ) : (
            <CheckCircle className="w-3.5 h-3.5 text-emerald-400 shrink-0 mt-0.5" />
          )}
          <div className="min-w-0">
            <p className={`text-[11px] font-semibold ${mismatch ? 'text-amber-300' : 'text-emerald-300'}`}>
              {mismatch ? 'Config says tests-gated, but the live gate fell back to UNVERIFIED' : 'Merges are gated on your detected tests'}
            </p>
            <p className="text-[10px] text-neutral-400 mt-0.5">
              Blueprint runs these command(s) on the PR head as the merge verdict — passing merges, failing blocks:
            </p>
            <ul className="mt-1 space-y-1">
              {verdict.test_commands.map((tc, i) => (
                <li key={i} className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
                  <span className="text-[10px] font-mono text-neutral-500 break-all">{tc.path || '.'}</span>
                  <code className="text-[10px] font-mono text-emerald-300 break-all">{tc.test_cmd}</code>
                </li>
              ))}
            </ul>
            {mismatch ? (
              <div className="mt-2 rounded-md border border-amber-500/20 bg-amber-500/5 px-2 py-1.5 space-y-0.5">
                <p className="text-[10px] text-amber-200">
                  Last live merge fell back to UNVERIFIED{unverifiedAt ? ` at ${formatAbsolute(unverifiedAt.getTime())}` : ''}.
                </p>
                {unverifiedReason ? (
                  <p className="text-[10px] text-neutral-300">
                    Reason: {unverifiedReason}
                  </p>
                ) : null}
                {verifiedAt ? (
                  <p className="text-[10px] text-neutral-400">
                    Last verified real-test run: {formatAbsolute(verifiedAt.getTime())}
                  </p>
                ) : null}
              </div>
            ) : verifiedAt ? (
              <p className="mt-1 text-[10px] text-neutral-400">
                Last verified real-test run: {formatAbsolute(verifiedAt.getTime())}
              </p>
            ) : null}
            {summaryLine ? (
              <p className="text-[10px] text-neutral-400 mt-1 break-words" data-testid="merge-gate-summary">
                {summaryLine}
              </p>
            ) : null}
            {blockDegradedEnabled ? (
              <div className="mt-2 rounded-md border border-indigo-500/20 bg-indigo-500/5 px-2 py-1.5 space-y-0.5" data-testid="honest-block-badge">
                <p className="text-[10px] text-indigo-300 font-medium">
                  Honest-block policy active — degraded fall-opens are held
                </p>
                {lastBlockAt ? (
                  <p className="text-[10px] text-neutral-400">
                    Last block: {formatAbsolute(lastBlockAt.getTime())}{lastBlockReason ? ` — ${lastBlockReason}` : ''}
                  </p>
                ) : null}
              </div>
            ) : null}
          </div>
        </div>
      </div>
    );
  }

  // unverified
  return (
    <div
      className={`${wrap} bg-yellow-500/5 border-yellow-500/30`}
      data-testid="merge-verification-banner"
      data-verdict-state="unverified"
      data-runtime-state={runtimeState}
      title={runtime?.last_unverified_at ? `Last live merge fell back to UNVERIFIED${unverifiedAt ? ` at ${formatAbsolute(unverifiedAt.getTime())}` : ''}${unverifiedReason ? ` because ${unverifiedReason}.` : '.'}` : undefined}
    >
      <div className="flex items-start gap-2">
        <AlertTriangle className="w-3.5 h-3.5 text-yellow-400 shrink-0 mt-0.5" />
        <div className="min-w-0">
          <p className="text-[11px] font-semibold text-yellow-300">
            No test command detected — PRs will merge UNVERIFIED (no tests run)
          </p>
          <p className="text-[10px] text-neutral-400 mt-0.5">
            Nothing gates these merges. Add a test command for a directory so Blueprint verifies PRs before merging.
          </p>
          {unverifiedAt ? (
            <div className="mt-2 rounded-md border border-yellow-500/20 bg-yellow-500/5 px-2 py-1.5 space-y-0.5">
              <p className="text-[10px] text-yellow-200">
                Last live merge fell back to UNVERIFIED at {formatAbsolute(unverifiedAt.getTime())}.
              </p>
              {unverifiedReason ? (
                <p className="text-[10px] text-neutral-300">
                  Reason: {unverifiedReason}
                </p>
              ) : null}
            </div>
          ) : null}
          {summaryLine ? (
            <p className="text-[10px] text-neutral-400 mt-1 break-words" data-testid="merge-gate-summary">
              {summaryLine}
            </p>
          ) : null}
          {blockDegradedEnabled ? (
            <div className="mt-2 rounded-md border border-indigo-500/20 bg-indigo-500/5 px-2 py-1.5 space-y-0.5" data-testid="honest-block-badge">
              <p className="text-[10px] text-indigo-300 font-medium">
                Honest-block policy active — degraded fall-opens are held
              </p>
              {lastBlockAt ? (
                <p className="text-[10px] text-neutral-400">
                  Last block: {formatAbsolute(lastBlockAt.getTime())}{lastBlockReason ? ` — ${lastBlockReason}` : ''}
                </p>
              ) : null}
            </div>
          ) : null}
        </div>
      </div>
    </div>
  );
}
