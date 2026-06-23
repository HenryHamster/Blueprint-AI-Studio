import { CheckCircle, Circle } from 'lucide-react';
import type { Project, OnboardingProof } from '../types/project';
import { formatAbsolute, formatDurationMs, parseTimestamp, staleFrontierWarning } from '../utils/time';
import { humanizeUnverifiedReason } from '../utils/unverifiedReasonLabels';

interface Props {
  project: Project;
}

interface Milestone {
  label: string;
  timestamp: string | null | undefined;
  durationFromStart?: string;
  extra?: React.ReactNode;
}

function GateVerdictBadge({ proof }: { proof: OnboardingProof }) {
  if (!proof.first_merge_gate_verdict_at) return null;

  if (proof.first_merge_gate_executed_tests) {
    return (
      <span className="ml-2 inline-flex items-center rounded px-1.5 py-0.5 text-ui-sm font-semibold bg-emerald-500/15 text-emerald-300 border border-emerald-500/20">
        Tests executed
      </span>
    );
  }

  const reason = proof.first_merge_gate_unverified_reason;
  return (
    <span className="ml-2 inline-flex items-center rounded px-1.5 py-0.5 text-ui-sm font-semibold bg-amber-500/15 text-amber-300 border border-amber-500/30">
      Fell open{reason ? ` — ${humanizeUnverifiedReason(reason)}` : ''}
    </span>
  );
}

export function OnboardingProofPanel({ project }: Props) {
  if (project.is_self_repo) return null;
  const proof = project.onboarding_proof;
  if (!proof) return null;

  const startMs = proof.repo_added_at
    ? parseTimestamp(proof.repo_added_at).getTime()
    : null;

  function elapsed(ts: string | null | undefined): string | undefined {
    if (!startMs || !ts) return undefined;
    const ms = parseTimestamp(ts).getTime() - startMs;
    return ms > 0 ? `+${formatDurationMs(ms)}` : undefined;
  }

  const milestones: Milestone[] = [
    {
      label: 'Repo added',
      timestamp: proof.repo_added_at,
    },
    {
      label: 'Bootstrap completed',
      timestamp: proof.bootstrap_completed_at,
      durationFromStart: elapsed(proof.bootstrap_completed_at),
    },
    {
      label: 'First task spawned',
      timestamp: proof.first_task_spawned_at,
      durationFromStart: elapsed(proof.first_task_spawned_at),
    },
    {
      label: 'First PR opened',
      timestamp: proof.first_pr_opened_at,
      durationFromStart: elapsed(proof.first_pr_opened_at),
    },
    {
      label: 'First gate verdict',
      timestamp: proof.first_merge_gate_verdict_at,
      durationFromStart: elapsed(proof.first_merge_gate_verdict_at),
      extra: <GateVerdictBadge proof={proof} />,
    },
    {
      label: 'First PR merged',
      timestamp: proof.first_pr_merged_at,
      durationFromStart: elapsed(proof.first_pr_merged_at),
    },
    {
      label: 'First autonomous improvement',
      timestamp: proof.first_autonomous_improvement_at,
      durationFromStart: elapsed(proof.first_autonomous_improvement_at),
    },
  ];

  const repoSlug = `${project.github_owner}/${project.github_repo}`;

  const timeToImprovement = elapsed(proof.first_autonomous_improvement_at);
  const timeToMerge = elapsed(proof.first_pr_merged_at);
  const showDora = timeToImprovement != null || timeToMerge != null;

  // Detect stalled frontier: ordered milestone timestamps, find first pending.
  const orderedTimestamps = [
    proof.repo_added_at,
    proof.bootstrap_completed_at,
    proof.first_task_spawned_at,
    proof.first_pr_opened_at,
    proof.first_merge_gate_verdict_at,
    proof.first_pr_merged_at,
    proof.first_autonomous_improvement_at,
  ];
  const firstPendingIdx = orderedTimestamps.findIndex((ts) => !ts);
  const isJourneyComplete = firstPendingIdx === -1;
  let stallWarning: { stalled: boolean; pendingFor: string } | null = null;
  if (!isJourneyComplete && firstPendingIdx > 0) {
    const completedMs = orderedTimestamps
      .slice(0, firstPendingIdx)
      .map((ts) => parseTimestamp(ts!).getTime());
    const stepDurationsMs: number[] = [];
    for (let i = 1; i < completedMs.length; i++) {
      stepDurationsMs.push(completedMs[i] - completedMs[i - 1]);
    }
    stallWarning = staleFrontierWarning(completedMs[completedMs.length - 1], stepDurationsMs);
  }

  const failedTaskCount = project.task_status_counts?.failed ?? 0;

  return (
    <div className="mt-3 rounded-xl border border-neutral-800 bg-neutral-950/60 p-3">
      <div className="text-ui-md font-bold text-neutral-300 uppercase tracking-widest mb-2">
        Onboarding proof — {repoSlug}
      </div>
      {stallWarning?.stalled && (
        <div className="mb-2.5 rounded-lg bg-amber-500/10 border border-amber-500/30 px-2.5 py-1.5">
          <span className="text-ui-md font-semibold text-amber-400">
            Journey stalled — pending for {stallWarning.pendingFor}
            {failedTaskCount > 0 && ` (${failedTaskCount} failed ${failedTaskCount === 1 ? 'task' : 'tasks'})`}
          </span>
        </div>
      )}
      {showDora && (
        <div className="mb-2.5 rounded-lg bg-neutral-900/60 border border-neutral-800 px-2.5 py-1.5 space-y-0.5">
          {timeToImprovement != null && (
            <div className="flex items-baseline gap-2 flex-wrap">
              <span className="text-ui-sm text-neutral-500 uppercase tracking-wide">Time to first improvement</span>
              {/* eslint-disable-next-line viewport/no-arbitrary-font-size */}
              <span className="text-[12px] font-semibold text-emerald-300">{timeToImprovement}</span>
            </div>
          )}
          {timeToMerge != null && (
            <div className="flex items-baseline gap-2 flex-wrap">
              <span className="text-ui-sm text-neutral-500 uppercase tracking-wide">Time to first merge</span>
              {/* eslint-disable-next-line viewport/no-arbitrary-font-size */}
              <span className="text-[12px] font-semibold text-emerald-300">{timeToMerge}</span>
            </div>
          )}
        </div>
      )}
      <div className="space-y-1.5">
        {milestones.map((m) => {
          const done = Boolean(m.timestamp);
          return (
            <div key={m.label} className="flex items-start gap-2">
              {done ? (
                <CheckCircle className="w-3.5 h-3.5 text-emerald-400 shrink-0 mt-px" />
              ) : (
                <Circle className="w-3.5 h-3.5 text-neutral-600 shrink-0 mt-px" />
              )}
              <div className="flex items-center flex-wrap gap-x-1 min-w-0">
                <span className={`text-ui-md ${done ? 'text-neutral-300' : 'text-neutral-500'}`}>
                  {m.label}
                </span>
                {done && m.timestamp ? (
                  <span className="text-ui-md text-neutral-400">
                    {formatAbsolute(m.timestamp)}
                  </span>
                ) : (
                  <span className="text-ui-md text-neutral-600">&mdash;</span>
                )}
                {done && m.durationFromStart && (
                  <span className="text-ui-sm text-neutral-500">{m.durationFromStart}</span>
                )}
                {done && m.extra}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
