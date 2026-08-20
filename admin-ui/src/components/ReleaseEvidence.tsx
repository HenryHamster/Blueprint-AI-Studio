import { ShieldCheck } from 'lucide-react';
import { type LiveReleaseEvidence } from '../api';
import { timeAgo, formatAbsolute } from '../utils/time';
import { shortPromotedSha, servingStatusClass } from './ReleaseEvidence.utils';

/**
 * Operator-facing display of the promoted-and-serving release evidence that
 * authorized a live-release compute proof. The backend surfaces this object on
 * compute_experiment KnowledgeDocument responses (`release_evidence`) and on the
 * launching ComputeJob's `polling_metadata.live_release_evidence`; both carry the
 * same shape. These components render nothing when the evidence is null/absent so
 * non-live-proof documents and jobs stay clean.
 */

/**
 * Compact "Live Proof Context" card: version number, short promoted SHA, and the
 * aggregate serving status (colored). Used in the KnowledgeBrowser detail view
 * below the acceptance grade badge. Renders nothing when evidence is null/absent.
 */
export function ReleaseEvidencePanel({ evidence }: { evidence?: LiveReleaseEvidence | null }) {
  if (!evidence) return null;
  const requiredArtifacts = evidence.required_artifacts ?? [];
  const sha = shortPromotedSha(evidence.promoted_sha);
  const version =
    typeof evidence.release_version_number === 'number'
      ? `v${evidence.release_version_number}`
      : null;
  const status = evidence.aggregate_serving_status ?? null;
  return (
    <div
      className="rounded border border-neutral-800 bg-workbench-base/40 px-2 py-1.5 text-ui-md space-y-1"
      data-testid="release-evidence-panel"
    >
      <div className="flex items-center gap-1.5 text-ui-sm uppercase tracking-wide text-neutral-500">
        <ShieldCheck className="w-3 h-3 shrink-0 text-emerald-400" />
        Live Proof Context
      </div>
      <div className="flex items-center gap-2 flex-wrap text-neutral-300">
        {version && (
          <span className="px-1.5 py-0.5 rounded bg-blue-950/40 text-blue-300 font-semibold">{version}</span>
        )}
        {sha && (
          <span className="font-mono text-neutral-400">
            <span className="text-neutral-500">sha </span>
            {sha}
          </span>
        )}
        {status && (
          <span className={`${servingStatusClass(status)} border rounded px-1.5 py-0.5 font-semibold`}>
            {status}
          </span>
        )}
      </div>
      {requiredArtifacts.length > 0 && (
        <div className="space-y-1 pt-1">
          <div className="text-ui-sm uppercase tracking-wide text-neutral-500">Required artifacts</div>
          <div className="space-y-1">
            {requiredArtifacts.map((artifact) => {
              const kind = artifact.artifact_kind ?? 'unknown';
              const artifactStatus = artifact.serving_status ?? 'unconfirmed';
              return (
                <div key={`${kind}-${artifact.served_identifier ?? artifact.reason_code ?? artifactStatus}`} className="flex flex-wrap items-center gap-1.5">
                  <span className="font-mono text-neutral-300">{kind}</span>
                  <span className={`${servingStatusClass(artifactStatus)} border rounded px-1.5 py-0.5 font-semibold`}>
                    {artifactStatus}
                  </span>
                  {artifact.served_identifier && (
                    <span className="font-mono text-neutral-500">id {artifact.served_identifier}</span>
                  )}
                  {artifact.served_at && <span className="text-neutral-600" title={formatAbsolute(artifact.served_at)}>{timeAgo(artifact.served_at)}</span>}
                  {artifact.reason_code && <span className="text-red-300/80">{artifact.reason_code}</span>}
                </div>
              );
            })}
          </div>
        </div>
      )}
    </div>
  );
}

/**
 * Single-line inline indicator: "Live proof of v{n}" with the short promoted SHA
 * and serving status. Used in the task detail drawer and on compute job rows
 * where a full card would be too heavy. Renders nothing when evidence is null.
 */
export function ReleaseEvidenceChip({ evidence }: { evidence?: LiveReleaseEvidence | null }) {
  if (!evidence) return null;
  const requiredArtifacts = evidence.required_artifacts ?? [];
  const sha = shortPromotedSha(evidence.promoted_sha);
  const version =
    typeof evidence.release_version_number === 'number'
      ? `v${evidence.release_version_number}`
      : null;
  const status = evidence.aggregate_serving_status ?? null;
  return (
    <span
      className={`${servingStatusClass(status)} border text-ui-sm rounded inline-flex flex-col items-start gap-1 px-1.5 py-0.5`}
      data-testid="release-evidence-chip"
      title={status ? `Aggregate serving status: ${status}` : undefined}
    >
      <span className="inline-flex items-center gap-1">
        <ShieldCheck className="w-2.5 h-2.5 shrink-0" />
        Live proof{version ? ` of ${version}` : ''}
        {sha && <span className="font-mono opacity-90">{sha}</span>}
      </span>
      {requiredArtifacts.length > 0 && (
        <span className="flex flex-wrap gap-1">
          {requiredArtifacts.map((artifact) => {
            const kind = artifact.artifact_kind ?? 'unknown';
            const artifactStatus = artifact.serving_status ?? 'unconfirmed';
            return (
              <span
                key={`${kind}-${artifact.served_identifier ?? artifact.reason_code ?? artifactStatus}`}
                className={`${servingStatusClass(artifactStatus)} border rounded px-1 py-0.5 font-semibold`}
              >
                <span className="font-mono">{kind}</span>
                <span className="ml-1">{artifactStatus}</span>
                {artifact.reason_code && <span className="ml-1 text-red-300/80">{artifact.reason_code}</span>}
              </span>
            );
          })}
        </span>
      )}
    </span>
  );
}
