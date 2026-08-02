import { useState } from 'react';
import { ChevronDown, ChevronRight } from 'lucide-react';
import type { Task } from './TaskCard';

interface SelfReviewCheckResult {
  passed: boolean;
  issue?: string;
  reasoning?: string;
  severity?: string;
  evidence?: string;
}

interface EdgeConsidered {
  case: string;
  handled: boolean;
  evidence?: string;
  blind_spot?: string;
}

interface SelfReviewOutcome {
  verdict?: string;
  fixes_applied?: boolean;
  check_results?: Record<string, SelfReviewCheckResult>;
  blind_spots?: string[];
  edges_considered?: EdgeConsidered[];
  fix_iteration?: number;
  _filtered_blind_spots_count?: number;
}

// Cross-reference: backend/app/sr_blind_spots.py DISALLOWED_BLIND_SPOT_EXAMPLES
const DISALLOWED_BLIND_SPOT_LABELS = [
  'full backend test suite',
  'docker pytest',
  'viewport tests',
];

function CheckResultReasoningDisplay({ result }: { result: SelfReviewCheckResult }) {
  const [expanded, setExpanded] = useState(false);
  const reasoning = result.reasoning?.trim() ?? '';
  const hasReasoning = reasoning.length > 0;
  const isThinReasoning = hasReasoning && reasoning.length < 10;
  const shouldTruncate = reasoning.length > 200;
  const displayText = shouldTruncate && !expanded ? reasoning.slice(0, 200) + '…' : reasoning;

  if (!hasReasoning) return null;

  return (
    <div className={`text-xs ml-3.5 mt-1 break-words ${isThinReasoning ? 'italic text-neutral-500' : 'text-neutral-400'}`}>
      {displayText}
      {shouldTruncate && (
        <button
          onClick={() => setExpanded(!expanded)}
          className="ml-1 text-neutral-500 hover:text-neutral-300 underline"
        >
          {expanded ? 'less' : 'more'}
        </button>
      )}
      {result.severity && (
        <span className={`ml-1.5 inline-block text-ui-xs px-1.5 py-0.5 rounded font-semibold ${
          result.severity === 'CRITICAL' ? 'bg-red-950/40 text-red-400'
          : result.severity === 'HIGH' ? 'bg-orange-950/40 text-orange-400'
          : result.severity === 'MEDIUM' ? 'bg-yellow-950/40 text-yellow-400'
          : 'bg-neutral-800 text-neutral-400'
        }`}>
          {result.severity}
        </span>
      )}
    </div>
  );
}

export function SelfReviewPanel({ task }: { task: Task }) {
  const ctx = task.context as Record<string, unknown> | undefined;
  const outcome = ctx?._self_review_outcome as SelfReviewOutcome | undefined;
  const humanCategories = ctx?._first_attempt_rejection_categories as Record<string, unknown> | undefined;
  const [open, setOpen] = useState(false);
  if (!outcome) return null;

  const verdict = outcome.verdict ?? 'unknown';
  const checkResults = outcome.check_results ?? {};
  const isSoftFallback = verdict === 'unknown' && Object.keys(checkResults).length === 0;

  const verdictColor = verdict === 'pass' ? 'text-emerald-400' : verdict === 'fail' ? 'text-red-400' : 'text-neutral-400';
  const verdictBorderBg = verdict === 'pass' ? 'bg-emerald-950/30 border-emerald-800/40' : verdict === 'fail' ? 'bg-red-950/30 border-red-800/40' : 'bg-neutral-800 border-neutral-700';

  const failedCategories = Object.entries(checkResults).filter(([, r]) => !r.passed);
  const passedCategories = Object.entries(checkResults).filter(([, r]) => r.passed);

  const humanCats = humanCategories && Object.keys(humanCategories).length > 0 ? humanCategories : null;
  const allCategories = humanCats
    ? Array.from(new Set([...Object.keys(checkResults), ...Object.keys(humanCats)]))
    : [];
  const missedCount = humanCats
    ? allCategories.filter(cat => cat in humanCats && !(checkResults[cat] && !checkResults[cat].passed)).length
    : 0;

  return (
    <div className="bg-neutral-900/50 rounded-lg border border-neutral-800 mb-2">
      {/* reason: collapsible section header; text-xs py-2 single-row height matches drawer density convention */}
      <button
        data-viewport-allow-small-target
        onClick={() => setOpen(o => !o)}
        className="flex items-center gap-1.5 w-full text-left px-3 py-2 text-xs font-semibold uppercase tracking-wide text-neutral-400 hover:text-neutral-200 transition-colors"
      >
        {open ? <ChevronDown className="w-3.5 h-3.5" /> : <ChevronRight className="w-3.5 h-3.5" />}
        Self-Review
        <span className={`ml-auto text-ui-sm font-semibold uppercase px-1.5 py-0.5 rounded border ${verdictBorderBg} ${isSoftFallback ? 'text-neutral-500' : verdictColor}`}>
          {isSoftFallback ? 'no data' : verdict}
        </span>
      </button>
      {open && (
        <div className="px-3 pb-3 space-y-3 border-t border-neutral-800 pt-3">
          {isSoftFallback ? (
            <p className="text-xs text-neutral-500 italic">Soft-fallback envelope — no checks were recorded for this task.</p>
          ) : (
            <>
              <div className="flex items-center gap-4 flex-wrap text-xs">
                <div className="flex items-center gap-1.5">
                  <span className="text-neutral-500">Verdict</span>
                  <span className={`font-semibold uppercase ${verdictColor}`}>{verdict}</span>
                </div>
                {outcome.fix_iteration !== undefined && (
                  <div className="flex items-center gap-1.5">
                    <span className="text-neutral-500">Iteration</span>
                    <span className="font-semibold text-blue-400">#{outcome.fix_iteration}</span>
                  </div>
                )}
                {outcome.fixes_applied !== undefined && (
                  <div className="flex items-center gap-1.5">
                    <span className="text-neutral-500">Fixes applied</span>
                    <span className={outcome.fixes_applied ? 'text-emerald-400' : 'text-neutral-400'}>
                      {outcome.fixes_applied ? 'yes' : 'no'}
                    </span>
                  </div>
                )}
              </div>

              {failedCategories.length > 0 && (
                <div>
                  <p className="text-xs text-neutral-400 font-medium mb-1.5">Failed checks</p>
                  <div className="space-y-1.5">
                    {failedCategories.map(([cat, result]) => {
                      const isFromReviewer = result.issue?.startsWith('[reviewer]');
                      return (
                        <div key={cat} className="text-xs">
                          <div className="flex items-center gap-1.5">
                            <span className={`w-2 h-2 rounded-full shrink-0 ${isFromReviewer ? 'bg-orange-500' : 'bg-red-500'}`} />
                            <span className={`font-mono ${isFromReviewer ? 'text-orange-300' : 'text-red-300'}`}>{cat}</span>
                            {isFromReviewer && <span className="text-ui-xs px-1 py-0.5 rounded bg-orange-950/40 text-orange-300 font-semibold">from reviewer</span>}
                          </div>
                          {result.issue && <p className="text-neutral-400 ml-3.5 mt-0.5 break-words">{result.issue.startsWith('[reviewer]') ? result.issue.slice(10) : result.issue}</p>}
                          <CheckResultReasoningDisplay result={result} />
                        </div>
                      );
                    })}
                  </div>
                </div>
              )}

              {passedCategories.length > 0 && (
                <div>
                  <p className="text-xs text-neutral-400 font-medium mb-1.5">Passed checks</p>
                  <div className="space-y-1.5">
                    {passedCategories.map(([cat, result]) => {
                      const reasoning = result.reasoning?.trim() ?? '';
                      const isThinReasoning = reasoning.length > 0 && reasoning.length < 10;
                      return (
                        <div key={cat}>
                          <div className="flex items-center gap-1.5">
                            <span className={`w-2 h-2 rounded-full shrink-0 ${isThinReasoning ? 'bg-amber-500/60' : 'bg-emerald-500'}`} />
                            <span className={`text-ui-sm font-mono px-1.5 py-0.5 rounded border ${
                              isThinReasoning
                                ? 'bg-amber-950/30 border-amber-800/30 text-amber-400'
                                : 'bg-emerald-950/30 border-emerald-800/30 text-emerald-400'
                            }`}>
                              {cat}
                            </span>
                          </div>
                          {reasoning && (
                            <CheckResultReasoningDisplay result={result} />
                          )}
                        </div>
                      );
                    })}
                  </div>
                </div>
              )}

              {outcome.blind_spots && outcome.blind_spots.length > 0 && (
                <div>
                  <p className="text-xs text-neutral-400 font-medium mb-1.5">Blind spots</p>
                  <ul className="space-y-1">
                    {outcome.blind_spots.map((spot, i) => (
                      <li key={i} className="text-xs text-neutral-400 flex items-start gap-1.5">
                        <span className="mt-1 w-1.5 h-1.5 rounded-full bg-amber-500/60 shrink-0" />
                        <span className="break-words">{spot}</span>
                      </li>
                    ))}
                  </ul>
                </div>
              )}
              {(outcome._filtered_blind_spots_count ?? 0) > 0 && (
                <div className="bg-amber-950/20 border border-amber-800/30 rounded px-2.5 py-2">
                  <p className="text-xs text-amber-400">
                    Agent emitted {outcome._filtered_blind_spots_count} disallowed blind-spot pattern{outcome._filtered_blind_spots_count !== 1 ? 's' : ''} — filtered before aggregation
                  </p>
                  <p className="text-ui-sm text-amber-500/70 mt-0.5">
                    Disallowed categories: {DISALLOWED_BLIND_SPOT_LABELS.join(', ')}
                  </p>
                </div>
              )}

              {outcome.edges_considered && outcome.edges_considered.length > 0 && (
                <div>
                  <p className="text-xs text-neutral-400 font-medium mb-1.5">Edges considered ({outcome.edges_considered.length})</p>
                  <div className="rounded border border-neutral-700 overflow-hidden text-ui-sm">
                    <div className="grid grid-cols-[1fr_auto] bg-neutral-800/60 px-2 py-1 font-semibold text-neutral-400 uppercase tracking-wide gap-2">
                      <span>Case</span>
                      <span>Handled</span>
                    </div>
                    {outcome.edges_considered.map((edge, i) => (
                      <div key={i} className={`px-2 py-1.5 border-t border-neutral-700/50 ${!edge.handled ? 'bg-amber-950/20' : ''}`}>
                        <div className="grid grid-cols-[1fr_auto] gap-2 items-start min-w-0">
                          <span className="text-neutral-300 break-words overflow-hidden">{edge.case}</span>
                          <span className={`font-semibold shrink-0 ${edge.handled ? 'text-emerald-400' : 'text-amber-400'}`}>
                            {edge.handled ? '✓ yes' : '✗ no'}
                          </span>
                        </div>
                        {edge.evidence && <p className="text-neutral-500 mt-0.5 break-words">evidence: {edge.evidence}</p>}
                        {edge.blind_spot && <p className="text-amber-400/70 mt-0.5 break-words">blind spot: {edge.blind_spot}</p>}
                      </div>
                    ))}
                  </div>
                </div>
              )}

              {humanCats && allCategories.length > 0 && (
                <div>
                  <p className="text-xs text-neutral-400 font-medium mb-1.5">Agent vs. Human review categories</p>
                  <div className="rounded border border-neutral-700 overflow-hidden text-ui-sm">
                    <div className="grid grid-cols-3 bg-neutral-800/60 px-2 py-1 font-semibold text-neutral-400 uppercase tracking-wide">
                      <span>Category</span>
                      <span className="text-center">Agent</span>
                      <span className="text-center">Human</span>
                    </div>
                    {allCategories.map(cat => {
                      const agentResult = checkResults[cat];
                      const humanFlagged = humanCats != null && cat in humanCats;
                      const agentFailed = agentResult && !agentResult.passed;
                      const agentPassed = agentResult && agentResult.passed;
                      const missed = humanFlagged && !agentFailed;
                      return (
                        <div key={cat} className={`grid grid-cols-3 px-2 py-1.5 border-t border-neutral-700/50 ${missed ? 'bg-red-950/20' : ''}`}>
                          <span className="font-mono text-neutral-300 break-all">{cat}</span>
                          <span className="text-center">
                            {agentFailed ? <span className="text-red-400">✗ fail</span>
                              : agentPassed ? <span className="text-emerald-400">✓ pass</span>
                              : <span className="text-neutral-600">—</span>}
                          </span>
                          <span className="text-center">
                            {humanFlagged ? <span className="text-red-400">✗ flagged</span> : <span className="text-neutral-600">—</span>}
                          </span>
                        </div>
                      );
                    })}
                  </div>
                  {missedCount > 0 && (
                    <p className="text-ui-sm text-amber-400 mt-1.5">
                      {missedCount} human-flagged {missedCount === 1 ? 'category' : 'categories'} not caught by agent
                    </p>
                  )}
                </div>
              )}

            </>
          )}
        </div>
      )}
    </div>
  );
}
