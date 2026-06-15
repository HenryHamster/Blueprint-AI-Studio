import { CheckCircle, Circle } from 'lucide-react';
import type { Project, OnboardingProof } from '../types/project';
import { formatAbsolute } from '../utils/time';
import { humanizeUnverifiedReason } from '../utils/unverifiedReasonLabels';

interface Props {
  project: Project;
}

interface Milestone {
  label: string;
  timestamp: string | null | undefined;
  extra?: React.ReactNode;
}

function GateVerdictBadge({ proof }: { proof: OnboardingProof }) {
  if (!proof.first_merge_gate_verdict_at) return null;

  if (proof.first_merge_gate_executed_tests) {
    return (
      <span className="ml-2 inline-flex items-center rounded px-1.5 py-0.5 text-[10px] font-semibold bg-emerald-500/15 text-emerald-300 border border-emerald-500/20">
        Tests executed
      </span>
    );
  }

  const reason = proof.first_merge_gate_unverified_reason;
  return (
    <span className="ml-2 inline-flex items-center rounded px-1.5 py-0.5 text-[10px] font-semibold bg-amber-500/15 text-amber-300 border border-amber-500/30">
      Fell open{reason ? ` — ${humanizeUnverifiedReason(reason)}` : ''}
    </span>
  );
}

export function OnboardingProofPanel({ project }: Props) {
  if (project.is_self_repo) return null;
  const proof = project.onboarding_proof;
  if (!proof) return null;

  const milestones: Milestone[] = [
    {
      label: 'Repo added',
      timestamp: proof.repo_added_at,
    },
    {
      label: 'Bootstrap completed',
      timestamp: proof.bootstrap_completed_at,
    },
    {
      label: 'First task spawned',
      timestamp: proof.first_task_spawned_at,
    },
    {
      label: 'First PR opened',
      timestamp: proof.first_pr_opened_at,
    },
    {
      label: 'First gate verdict',
      timestamp: proof.first_merge_gate_verdict_at,
      extra: <GateVerdictBadge proof={proof} />,
    },
    {
      label: 'First PR merged',
      timestamp: proof.first_pr_merged_at,
    },
  ];

  const repoSlug = `${project.github_owner}/${project.github_repo}`;

  return (
    <div className="mt-3 rounded-xl border border-neutral-800 bg-neutral-950/60 p-3">
      <div className="text-[11px] font-bold text-neutral-300 uppercase tracking-widest mb-2">
        Onboarding proof — {repoSlug}
      </div>
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
                <span className={`text-[11px] ${done ? 'text-neutral-300' : 'text-neutral-500'}`}>
                  {m.label}
                </span>
                {done && m.timestamp ? (
                  <span className="text-[11px] text-neutral-400">
                    {formatAbsolute(m.timestamp)}
                  </span>
                ) : (
                  <span className="text-[11px] text-neutral-600">&mdash;</span>
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
