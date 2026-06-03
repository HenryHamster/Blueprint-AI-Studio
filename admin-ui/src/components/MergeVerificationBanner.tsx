import { AlertTriangle, CheckCircle, ShieldCheck } from 'lucide-react';
import type { MergeVerificationVerdict } from '../types/project';

interface MergeVerificationBannerProps {
  /** Backend-computed verdict. Renders nothing when null/undefined (older responses). */
  verdict?: MergeVerificationVerdict | null;
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
export function MergeVerificationBanner({ verdict, className }: MergeVerificationBannerProps) {
  if (!verdict) return null;

  const wrap = `rounded-lg border px-3 py-2.5 ${className ?? ''}`;

  if (verdict.state === 'ci_checks') {
    return (
      <div
        className={`${wrap} bg-blue-500/5 border-blue-500/20`}
        data-testid="merge-verification-banner"
        data-verdict-state="ci_checks"
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
          </div>
        </div>
      </div>
    );
  }

  if (verdict.state === 'tests_gated') {
    return (
      <div
        className={`${wrap} bg-emerald-500/5 border-emerald-500/20`}
        data-testid="merge-verification-banner"
        data-verdict-state="tests_gated"
      >
        <div className="flex items-start gap-2">
          <CheckCircle className="w-3.5 h-3.5 text-emerald-400 shrink-0 mt-0.5" />
          <div className="min-w-0">
            <p className="text-[11px] font-semibold text-emerald-300">
              Merges are gated on your detected tests
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
        </div>
      </div>
    </div>
  );
}
