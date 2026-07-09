import {
  strVal, sanitize,
} from '../index.js';
import { differentiationHintFor } from './research-direction-context.js';
import { isGreenfieldRepo } from '../prompt.js';

/**
 * Builds a compact (0-2 line) failure-calibration signal from direction history.
 * Line 1: last cycle's failures with class and actionability hint.
 * Line 2 (optional): recurring CODE_ERROR trend → urge smaller scope.
 */
export function buildScopeCalibrationSignal(history: Array<Record<string, unknown>>): string {
  if (history.length === 0) return '';
  const lastEntry = history[history.length - 1];
  const failedDetails = Array.isArray(lastEntry.failed_task_details)
    ? lastEntry.failed_task_details as Array<{title: string; reason: string; category: string}>
    : [];

  const classified = failedDetails.map(fd => ({ ...fd, cls: classifyFailureCategory(fd.category) }));
  const codeErrors = classified.filter(fd => fd.cls === 'CODE_ERROR');
  const infraErrors = classified.filter(fd => _INFRA_CLASSES.has(fd.cls));

  const parts: string[] = [];
  if (codeErrors.length > 0) {
    const names = codeErrors.slice(0, 2).map(fd => `"${fd.title.slice(0, 40)}"`).join(', ');
    parts.push(`${codeErrors.length} CODE_ERROR (${names}) → over-scoped/under-specified`);
  }
  if (infraErrors.length > 0) {
    parts.push(`${infraErrors.length} infra failure(s) → retry ok`);
  }
  const other = classified.length - codeErrors.length - infraErrors.length;
  if (other > 0) {
    parts.push(`${other} other`);
  }

  const line1 = parts.length > 0 ? `\n  Last cycle failures: ${parts.join('; ')}` : '';

  // Prior-cycles failure recap: surface 2-3 code-error titles from cycles before
  // the last one so agents can avoid re-proposing the same failed findings.
  let priorFailuresLine = '';
  if (history.length >= 2) {
    const priorEntries = history.slice(-3, -1);
    const priorItems: Array<{title: string; reason: string; cls: string; cycle: number}> = [];
    priorEntries.forEach((h, idx) => {
      const details = Array.isArray(h.failed_task_details)
        ? h.failed_task_details as Array<{title: string; reason: string; category: string}>
        : [];
      for (const fd of details) {
        const cls = classifyFailureCategory(fd.category);
        if (cls === 'CODE_ERROR') {
          priorItems.push({ title: fd.title || '?', reason: fd.reason || '', cls, cycle: priorEntries.length - idx });
        }
      }
    });
    if (priorItems.length > 0) {
      const snippets = priorItems.slice(0, 3).map(it => {
        const t = it.title.slice(0, 60);
        const r = it.reason ? `: ${it.reason.slice(0, 50)}` : '';
        return `"${t}"${r} (${it.cycle} cycle${it.cycle === 1 ? '' : 's'} ago)`;
      });
      priorFailuresLine = `\n  Prior CODE_ERRORs to avoid repeating: ${snippets.join('; ')}`;
    }
  }

  // Scope trend: CODE_ERRORs recurring in prior cycles too → strongly urge smaller scope
  let line2 = '';
  if (codeErrors.length > 0 && history.length >= 2) {
    const prevEntries = history.slice(-3, -1);
    let prevCodeErrors = 0;
    for (const h of prevEntries) {
      const prevDetails = Array.isArray(h.failed_task_details)
        ? h.failed_task_details as Array<{category: string}>
        : [];
      prevCodeErrors += prevDetails.filter(fd => classifyFailureCategory(fd.category) === 'CODE_ERROR').length;
    }
    if (prevCodeErrors > 0) {
      line2 = `\n  Scope trend: CODE_ERRORs in ${Math.min(history.length, 3)} consecutive cycles → strongly prefer smaller, narrower findings`;
    }
  }

  // Scope success from last 3 cycles: aggregate scope_breakdown to show merged/created per scope,
  // and surface distinguishing characteristics (complexity, expansion ratio) + calibration flow
  const recent = history.slice(-3);
  type ScopeAgg = {
    created: number; merged: number;
    cplxMSum: number; cplxMN: number;
    cplxFSum: number; cplxFN: number;
    expSum: number; expN: number;
    calIn: number; calOut: number;
  };
  const aggScope: Record<string, ScopeAgg | undefined> = {};
  for (const h of recent) {
    const sb = h.scope_breakdown as Record<string, Record<string, number> | undefined> | undefined;
    if (!sb || typeof sb !== 'object') continue;
    for (const [scope, counts] of Object.entries(sb)) {
      if (!counts) continue;
      const a = aggScope[scope] ?? (aggScope[scope] = {
        created: 0, merged: 0, cplxMSum: 0, cplxMN: 0, cplxFSum: 0, cplxFN: 0, expSum: 0, expN: 0, calIn: 0, calOut: 0,
      });
      a.created += (counts.created) || 0;
      a.merged += (counts.merged) || 0;
      if (typeof counts.avg_complexity_merged === 'number') { a.cplxMSum += counts.avg_complexity_merged; a.cplxMN += 1; }
      if (typeof counts.avg_complexity_failed === 'number') { a.cplxFSum += counts.avg_complexity_failed; a.cplxFN += 1; }
      if (typeof counts.avg_expansion_ratio === 'number') { a.expSum += counts.avg_expansion_ratio; a.expN += 1; }
      a.calIn += counts.calibrated_in || 0;
      a.calOut += counts.calibrated_out || 0;
    }
  }
  const scopeOrder = ['small', 'medium', 'large'];
  const orderedScopes = [...scopeOrder, ...Object.keys(aggScope).filter(s => !scopeOrder.includes(s))];
  const scopeParts: string[] = [];
  const profileParts: string[] = [];
  const calParts: string[] = [];
  for (const scope of orderedScopes) {
    const a = aggScope[scope];
    if (!a) continue;
    if (a.created > 0) scopeParts.push(`${scope} ${a.merged}/${a.created}`);
    const mBits: string[] = [];
    if (a.cplxMN > 0) mBits.push(`cplx ${(a.cplxMSum / a.cplxMN).toFixed(2)}`);
    if (a.expN > 0) mBits.push(`expand ${(a.expSum / a.expN).toFixed(1)}x`);
    const fBits = a.cplxFN > 0 ? [`cplx ${(a.cplxFSum / a.cplxFN).toFixed(2)}`] : [];
    const sides: string[] = [];
    if (mBits.length) sides.push(`merged ${mBits.join('/')}`);
    if (fBits.length) sides.push(`failed ${fBits.join('/')}`);
    if (sides.length) profileParts.push(`${scope}(${sides.join(' vs ')})`);
    if (a.calIn > 0 || a.calOut > 0) {
      const c: string[] = [];
      if (a.calIn > 0) c.push(`in=${a.calIn}`);
      if (a.calOut > 0) c.push(`out=${a.calOut}`);
      calParts.push(`${scope} ${c.join(' ')}`);
    }
  }
  const cycleLabel = recent.length === 1 ? '1 cycle' : `${recent.length} cycles`;
  const line3 = scopeParts.length > 0
    ? `\n  Scope success (last ${cycleLabel}): ${scopeParts.join(', ')}`
    : '';
  const line4 = profileParts.length > 0
    ? `\n  Per-scope profile: ${profileParts.join('; ')}`
    : '';
  const line5 = calParts.length > 0
    ? `\n  Calibration (in=narrowed from larger, out=auto-escalated up): ${calParts.join(', ')}`
    : '';

  // Scope accuracy: per-scope avg_expansion_ratio (actual files / declared related_files).
  // Flag overflow (>2.0x) per scope and emit a summary warning so research agents
  // learn when their file-count estimates chronically underestimate reality.
  const expansionParts: string[] = [];
  let maxRatio = 0;
  for (const scope of orderedScopes) {
    const a = aggScope[scope];
    if (!a || a.expN === 0) continue;
    const avg = a.expSum / a.expN;
    if (avg > maxRatio) maxRatio = avg;
    const flag = avg > 2.0 ? ' ⚠ scope overflow' : '';
    expansionParts.push(`${scope} ${avg.toFixed(1)}x${flag}`);
  }
  const line6 = expansionParts.length > 0
    ? `\n  Scope accuracy (actual files / declared, last ${cycleLabel}): ${expansionParts.join(', ')}`
    : '';
  const line7 = maxRatio > 2.0
    ? `\n  ⚠ Expansion ratio exceeds 2.0x — halve your file count estimates or prefer smaller scope`
    : '';

  if (!line1 && !line2 && !line3 && !line4 && !line5 && !line6 && !line7 && !priorFailuresLine) return '';
  return line1 + line2 + line3 + line4 + line5 + line6 + line7 + priorFailuresLine;
}

/**
 * Renders a compact "Recently filtered (avoid)" section from the last few
 * research_history entries. Flattens entries' filtered_findings lists
 * (most recent cycle last), dedups by title, and caps at 5 items so the
 * prompt does not bloat. Returns an empty string when no entries carry
 * filtered_findings so existing prompts are unchanged.
 */
export function buildFilteredFindingsSection(history: Array<Record<string, unknown>>): string {
  if (history.length === 0) return '';
  const recent = history.slice(-3);
  const seen = new Set<string>();
  const items: Array<{title: string; reason: string}> = [];
  for (let i = recent.length - 1; i >= 0; i--) {
    const entry = recent[i];
    const raw = Array.isArray(entry.filtered_findings)
      ? entry.filtered_findings as Array<Record<string, unknown>>
      : [];
    for (const rec of raw) {
      const title = typeof rec.title === 'string' ? rec.title : '';
      const reason = typeof rec.reason === 'string' ? rec.reason : '';
      if (!title) continue;
      const key = title.toLowerCase();
      if (seen.has(key)) continue;
      seen.add(key);
      items.push({ title, reason });
      if (items.length >= 5) break;
    }
    if (items.length >= 5) break;
  }
  if (items.length === 0) return '';
  const lines = items.map(it => {
    const hint = differentiationHintFor(it.reason);
    if (it.reason && hint) return `  * "${it.title}" — ${it.reason} [hint: ${hint}]`;
    if (it.reason) return `  * "${it.title}" — ${it.reason}`;
    return `  * "${it.title}"`;
  });
  return `\n  Recently filtered (avoid — propose different approaches):\n${lines.join('\n')}`;
}

export function buildRejectedProposalsSection(
  rejectedProposals: Array<Record<string, unknown>>
): string {
  if (!Array.isArray(rejectedProposals) || rejectedProposals.length === 0) return '';
  const lines = rejectedProposals.slice(0, 10).map(p => {
    const text = strVal(p.text).slice(0, 300);
    const when = strVal(p.rejected_at).slice(0, 10) || '?';
    const rationale = strVal(p.rationale).slice(0, 200);
    const tail = rationale ? ` — ${rationale}` : '';
    return `- (${when}) ${text}${tail}`;
  });
  return `\n## Recently Rejected Proposals (last 30 days)\nThe user explicitly rejected these direction proposals. Do NOT re-propose similar work — find a different angle or move on.\n${lines.join('\n')}\n`;
}

export function buildOperatorOverridesSection(
  overrides: Array<Record<string, unknown>>
): string {
  if (!Array.isArray(overrides) || overrides.length === 0) return '';
  const lines = overrides.slice(0, 8).map(o => {
    const document = strVal(o.document) || 'document';
    const field = strVal(o.field);
    const when = strVal(o.at).slice(0, 10) || '?';
    const agentValue = strVal(o.agent_value).slice(0, 240);
    const operatorValue = strVal(o.operator_value).slice(0, 240);
    const fieldTail = field ? ` \`${field}\`` : '';
    const head = `- (${when}) ${document}${fieldTail}`;
    if (document === 'running_update') {
      // Running-update overrides carry no scalar operator value — the operator's
      // winning version IS the current running_update body in this payload.
      return `${head}: you proposed "${agentValue}"; the operator kept their own running_update edit (see the Running update above).`;
    }
    return `${head}: you proposed "${agentValue}"; the operator kept "${operatorValue}".`;
  });
  return `\n## Operator Overrides Since Last Cycle\nThe operator edited these documents mid-cycle and their value won the reconciliation — your previous proposal was overridden. Reconcile with the operator's intent: do NOT simply re-propose the overridden value. If you still believe a change is warranted, route it as a new proposal that respects what the operator chose.\n${lines.join('\n')}\n`;
}

export function buildRunningUpdateSupersededSection(
  signal: Record<string, unknown> | null | undefined
): string {
  if (!signal || typeof signal !== 'object') return '';
  const count = Number(signal.count) || 0;
  if (count <= 0) return '';
  const when = strVal(signal.most_recent_at).slice(0, 10);
  const whenTail = when ? ` (most recent: ${when})` : '';
  return `\n_Last cycle the operator preserved their own running_update over your proposed rewrite${whenTail}. The operator is actively editing this document — treat it as operator-authoritative. Refine it; do NOT rewrite it from a blank slate or re-impose your superseded version._\n`;
}

export function buildUserPausedSection(
  directionsSummary: Array<Record<string, unknown>>
): string {
  if (!Array.isArray(directionsSummary) || directionsSummary.length === 0) return '';
  const userPaused = directionsSummary.filter(
    d => strVal(d.status) === 'paused' && d.auto_paused !== true
  );
  if (userPaused.length === 0) return '';
  const lines = userPaused.map(d => {
    const reason = strVal(d.paused_reason);
    const tail = reason ? ` — reason: ${reason}` : '';
    return `- ${strVal(d.text)} (id: ${strVal(d.id)})${tail}`;
  });
  return `\n## User-Paused Directions\nThese directions were paused by the user (not auto-paused by the system). Treat as an explicit user constraint — do NOT unpause, retire, or duplicate them without a strong reason backed by user intent.\n${lines.join('\n')}\n`;
}

const QUALIFICATION_SCORE_KEYS = ['audit', 'test_erosion', 'smoketest'] as const;
// Canonical vocabulary of all qualification states produced by the backend's
// summarize_qualification_state. Mirrors QUALIFICATION_STATE_VOCAB in
// release_version.py and the QualificationStateValue union in admin-ui/src/api.ts.
// Exported so contract tests can assert it equals the shared fixture at
// backend/testing/__fixtures__/qualification_state_vocab.json.
export const QUALIFICATION_STATE_VOCAB: ReadonlySet<string> = new Set([
  'audit_crashed',
  'deferred_unsynced',
  'dry_run',
  'error',
  'infra_failure',
  'not_promoted',
  'promoted',
  'promoted_unconfirmed',
  'promotion_check_wait',
  'promotion_confirmation_timed_out',
  'promotion_deploy_failed',
  'promotion_failed',
  'promotion_in_flight',
  'protection_blocked_review',
  'qualifying',
  'required_check_failed',
]);
// Derived qualification states that mean an audit / promotion run is actively
// in flight for the current candidate. When the effective status is any of
// these, the agent must NOT emit `qualify_request` — a run is already going.
// `'pending'` is the raw stamp; `'qualifying'` is the derived state the backend
// surfaces while the audit is actually executing. Missing `'qualifying'` here
// was the wait-guard regression: the pending warning was silently dropped once
// the raw status was superseded by the derived `'qualifying'` state.
const AUDIT_IN_PROGRESS_STATES = new Set(['pending', 'qualifying']);
const QUALIFICATION_SEVERITY_ORDER: Record<string, number> = {
  blocker: 0,
  critical: 1,
  high: 2,
  medium: 3,
  moderate: 3,
  warning: 4,
  low: 5,
  info: 6,
};

function formatBlockerProvenance(qualification: Record<string, unknown>, blocker: string): string | null {
  // Surface the blocker's provenance when available. For CI blockers, include
  // the resolution state/detail. For audit blockers, include the missing reason.
  if (blocker.startsWith('ci_')) {
    const ciResolution = qualification.ci_resolution;
    if (ciResolution && typeof ciResolution === 'object' && !Array.isArray(ciResolution)) {
      const ciRes = ciResolution as Record<string, unknown>;
      const state = typeof ciRes.state === 'string' ? ciRes.state : null;
      const detail = typeof ciRes.detail === 'string' ? ciRes.detail : null;
      if (state || detail) {
        const parts: string[] = [];
        if (state) parts.push(`state: ${state}`);
        if (detail) parts.push(detail);
        return parts.join(', ');
      }
    }
  } else if (blocker === 'audit_missing') {
    const reason = typeof qualification.audit_missing_reason === 'string'
      ? qualification.audit_missing_reason
      : null;
    if (reason) return `reason: ${reason}`;
  }
  return null;
}

function formatQualificationDetails(qualification: Record<string, unknown>): string[] {
  const lines: string[] = [];

  // When the smoketest subsystem was disabled for the qualifying run it spawned
  // no smoketest child, so its sub-score is an unconditional 0.0 placeholder —
  // NOT a real low score. Mirror the backend (`smoketest_enabled` defaults True
  // for legacy records that predate the flag) and exclude the dimension from
  // both the rendered sub-scores and the binding determination when disabled.
  const smoketestEnabled = qualification.smoketest_enabled !== false;
  // A hard blocker (CI states, deleted test, missing audit, smoketest startup
  // failure) forces the composite to zero — the named blocker, not a numeric
  // minimum over placeholder scores, is the true constraint. Some blockers (CI
  // states) map to no sub-score at all.
  const blocker = typeof qualification.blocker === 'string' && qualification.blocker
    ? qualification.blocker
    : null;

  const subScoresRaw = qualification.sub_scores;
  if (subScoresRaw && typeof subScoresRaw === 'object' && !Array.isArray(subScoresRaw)) {
    const subScores = subScoresRaw as Record<string, unknown>;
    const scoreBits: string[] = [];
    let bindingKey: string | null = null;
    let bindingValue: number | null = null;
    for (const key of QUALIFICATION_SCORE_KEYS) {
      if (key === 'smoketest' && !smoketestEnabled) continue;
      const value = typeof subScores[key] === 'number' ? subScores[key] : null;
      if (value === null) continue;
      scoreBits.push(`${key}=${value.toFixed(2)}`);
      if (bindingValue === null || value < bindingValue) {
        bindingKey = key;
        bindingValue = value;
      }
    }
    if (scoreBits.length > 0) {
      let bindingClause: string | null = null;
      if (blocker) {
        // A hard blocker is the constraint regardless of the numeric minimum.
        // Surface the blocker's provenance when available.
        const provenance = formatBlockerProvenance(qualification, blocker);
        bindingClause = provenance ? `binding=${blocker} (${provenance})` : `binding=${blocker}`;
      } else if (bindingKey !== null && bindingValue !== null) {
        bindingClause = `binding=${bindingKey}=${bindingValue.toFixed(2)}`;
      }
      const suffix = bindingClause ? ` (${bindingClause})` : '';
      lines.push(`  Qualification details: sub-scores: ${scoreBits.join(', ')}${suffix}`);
    }
  }

  // Audit-score caveat — the audit sub-score reflects only *verified* findings;
  // higher-severity findings lacking a substantive verification note were
  // excluded from the score (their deductions never applied). Surface how many
  // were excluded, and any material model-reported-vs-findings-derived score
  // divergence the verdict already recorded, so the agent does not read an
  // inflated audit sub-score as a clean audit.
  const reconRaw = qualification.audit_reconciliation;
  if (reconRaw && typeof reconRaw === 'object' && !Array.isArray(reconRaw)) {
    const recon = reconRaw as Record<string, unknown>;
    const excludedCount = typeof recon.excluded_findings_count === 'number'
      ? recon.excluded_findings_count : 0;
    if (excludedCount > 0) {
      const bits: string[] = [
        `audit sub-score reflects only verified findings; ${excludedCount} higher-severity finding${excludedCount === 1 ? '' : 's'} excluded for lacking verification`,
      ];
      const modelScore = typeof recon.model_reported_score === 'number' ? recon.model_reported_score : null;
      const derivedScore = typeof recon.findings_derived_score === 'number' ? recon.findings_derived_score : null;
      const delta = typeof recon.score_delta === 'number' ? recon.score_delta : null;
      if (modelScore !== null && derivedScore !== null && delta !== null && delta >= 0.10) {
        bits.push(`model-reported audit ${modelScore.toFixed(2)} vs findings-derived ${derivedScore.toFixed(2)} (Δ${delta.toFixed(2)})`);
      }
      lines.push(`  Audit score caveat: ${bits.join('; ')}`);
    }
  }

  const findingsRaw = qualification.audit_findings;
  if (Array.isArray(findingsRaw) && findingsRaw.length > 0) {
    const findings = findingsRaw
      .map((finding, idx) => {
        if (!finding || typeof finding !== 'object' || Array.isArray(finding)) return null;
        const rec = finding as Record<string, unknown>;
        const severity = typeof rec.severity === 'string' ? rec.severity.trim() : '';
        const summary = typeof rec.summary === 'string' ? rec.summary.trim() : '';
        if (!severity || !summary) return null;
        return {
          severity,
          summary,
          order: QUALIFICATION_SEVERITY_ORDER[severity.toLowerCase()] ?? 99,
          idx,
        };
      })
      .filter((finding): finding is { severity: string; summary: string; order: number; idx: number } => finding !== null)
      .sort((a, b) => (a.order - b.order) || (a.idx - b.idx))
      .slice(0, 3);

    if (findings.length > 0) {
      const moreCount = findingsRaw.length - findings.length;
      const findingBits = findings.map(f => `${f.severity}: ${f.summary}`);
      const tail = moreCount > 0 ? ` (+${moreCount} more)` : '';
      lines.push(`  Audit findings: ${findingBits.join('; ')}${tail}`);
    }
  }

  // Excluded audit findings — higher-severity findings the verification gate
  // dropped from scoring (and from `audit_findings`). Rendered explicitly as
  // UNVERIFIED so the agent can investigate the omitted risk behind an inflated
  // audit sub-score rather than assume the audit was clean. These are NOT
  // confirmed blockers.
  const excludedRaw = qualification.excluded_audit_findings;
  if (Array.isArray(excludedRaw) && excludedRaw.length > 0) {
    const excluded = excludedRaw
      .map((finding, idx) => {
        if (!finding || typeof finding !== 'object' || Array.isArray(finding)) return null;
        const rec = finding as Record<string, unknown>;
        const severity = typeof rec.severity === 'string' ? rec.severity.trim() : '';
        const summary = typeof rec.summary === 'string' ? rec.summary.trim() : '';
        if (!severity || !summary) return null;
        return {
          severity,
          summary,
          order: QUALIFICATION_SEVERITY_ORDER[severity.toLowerCase()] ?? 99,
          idx,
        };
      })
      .filter((finding): finding is { severity: string; summary: string; order: number; idx: number } => finding !== null)
      .sort((a, b) => (a.order - b.order) || (a.idx - b.idx))
      .slice(0, 3);

    if (excluded.length > 0) {
      const moreCount = excludedRaw.length - excluded.length;
      const excludedBits = excluded.map(f => `${f.severity}: ${f.summary}`);
      const tail = moreCount > 0 ? ` (+${moreCount} more)` : '';
      lines.push(`  Excluded audit findings (UNVERIFIED — not confirmed blockers): ${excludedBits.join('; ')}${tail}`);
    }
  }

  // Test-erosion detail — the specific deleted test files / negative deltas
  // behind a low (or hard-blocking) test-erosion sub-score, so the agent can
  // scope "restore file X" / "recover N assertions" instead of blind cleanup.
  const erosionRaw = qualification.test_erosion_detail;
  if (erosionRaw && typeof erosionRaw === 'object' && !Array.isArray(erosionRaw)) {
    const erosion = erosionRaw as Record<string, unknown>;
    const bits: string[] = [];
    const deleted = Array.isArray(erosion.deleted_test_files)
      ? erosion.deleted_test_files.filter((p): p is string => typeof p === 'string' && p.trim() !== '')
      : [];
    if (deleted.length > 0) {
      const moreDeleted = typeof erosion.deleted_test_files_truncated === 'number'
        ? erosion.deleted_test_files_truncated : 0;
      const tail = moreDeleted > 0 ? ` (+${moreDeleted} more)` : '';
      bits.push(`deleted ${deleted.join(', ')}${tail}`);
    }
    if (typeof erosion.test_count_delta === 'number' && erosion.test_count_delta < 0) {
      bits.push(`${erosion.test_count_delta} tests`);
    }
    if (typeof erosion.assertion_delta === 'number' && erosion.assertion_delta < 0) {
      bits.push(`${erosion.assertion_delta} assertions`);
    }
    if (bits.length > 0) {
      lines.push(`  Test erosion: ${bits.join('; ')}`);
    }
  }

  // Smoketest detail — which suite / named tests failed behind a low (or
  // hard-blocking) smoketest sub-score, so the agent can target the failing
  // startup suite or the named tests instead of generic stabilize work.
  const smokeRaw = qualification.smoketest_detail;
  if (smokeRaw && typeof smokeRaw === 'object' && !Array.isArray(smokeRaw)) {
    const smoke = smokeRaw as Record<string, unknown>;
    const bits: string[] = [];
    if (smoke.startup_failed === true) {
      bits.push('startup suite failed');
    } else if (typeof smoke.failed_suite === 'string' && smoke.failed_suite.trim()) {
      bits.push(`suite ${smoke.failed_suite.trim()} failed`);
    }
    const failedTests = Array.isArray(smoke.failed_tests)
      ? smoke.failed_tests.filter((t): t is string => typeof t === 'string' && t.trim() !== '')
      : [];
    if (failedTests.length > 0) {
      const moreFailed = typeof smoke.failed_tests_truncated === 'number'
        ? smoke.failed_tests_truncated : 0;
      const tail = moreFailed > 0 ? ` (+${moreFailed} more)` : '';
      bits.push(`failed: ${failedTests.join(', ')}${tail}`);
    }
    if (bits.length > 0) {
      lines.push(`  Smoketest: ${bits.join('; ')}`);
    }
  }

  return lines;
}

function formatCriterionCoverage(versionPhase: Record<string, unknown>): string[] {
  const coverageRaw = versionPhase.criterion_coverage;
  if (!coverageRaw || typeof coverageRaw !== 'object' || Array.isArray(coverageRaw)) return [];

  const coverage = coverageRaw as Record<string, unknown>;
  const total = typeof coverage.total === 'number' ? coverage.total : null;
  const covered = typeof coverage.covered === 'number' ? coverage.covered : null;
  const partial = typeof coverage.partial === 'number' ? coverage.partial : null;
  const uncovered = typeof coverage.uncovered === 'number' ? coverage.uncovered : null;
  if (total === null || total <= 0 || covered === null || partial === null || uncovered === null) return [];

  const lines = [
    `**Acceptance criteria coverage (canonical): ${covered}/${total} covered, ${partial} partial, ${uncovered} uncovered.**`,
    `Canonical release-readiness path: close uncovered and partial acceptance criteria with evidence; legacy goal/proven heuristics below are secondary context only.`,
  ];

  if (Array.isArray(coverage.criteria)) {
    const criteriaLines: string[] = [];
    for (const item of coverage.criteria) {
      if (!item || typeof item !== 'object' || Array.isArray(item)) continue;
      const criterion = item as Record<string, unknown>;
      const criterionCoverage = typeof criterion.coverage === 'string' ? criterion.coverage.trim() : '';
      if (criterionCoverage !== 'uncovered' && criterionCoverage !== 'partial') continue;

      const status = typeof criterion.status === 'string' ? criterion.status.trim() : 'active';
      if (status !== 'active') continue;

      const key = typeof criterion.criterion_key === 'string' && criterion.criterion_key.trim()
        ? criterion.criterion_key.trim()
        : null;
      const text = typeof criterion.text === 'string' && criterion.text.trim()
        ? criterion.text.trim()
        : null;
      if (!key || !text) continue;

      const evidenceCount = typeof criterion.evidence_count === 'number'
        ? `, evidence=${criterion.evidence_count}`
        : '';
      const stuckReason = typeof criterion.stuck_reason === 'string' && criterion.stuck_reason
        ? ` [stuck: ${criterion.stuck_reason}]`
        : '';
      criteriaLines.push(`  - ${key}: ${criterionCoverage}${evidenceCount}${stuckReason} — ${text}`);
    }
    if (criteriaLines.length > 0) {
      lines.push(`Acceptance criteria needing evidence:`);
      lines.push(...criteriaLines);
    }
  }

  return lines;
}

/**
 * Builds the version-phase status block for the strategic thinker prompt.
 * Explains the current phase, what each phase means, and instructs the agent
 * to emit version_phase_decision when it decides to flip.
 */
export function buildVersionPhaseSection(versionPhase: Record<string, unknown> | undefined): string {
  if (!versionPhase || typeof versionPhase !== 'object') return '';

  // Failure marker — the backend assembly failed; surface as an explicit advisory
  // so the strategist knows the omission is a build error, not missing lifecycle.
  if (versionPhase.lifecycle_signal === 'unavailable' || versionPhase.build_unavailable === true) {
    const signalStatus = typeof versionPhase.signal_status === 'string' ? versionPhase.signal_status : 'build_failed';
    const integrationBranch = typeof versionPhase.integration_branch === 'string' ? versionPhase.integration_branch : null;
    const promotionTarget = typeof versionPhase.promotion_target === 'string' ? versionPhase.promotion_target : null;
    const errorSnippet = typeof versionPhase.error === 'string' ? versionPhase.error : null;
    const branchCtx = integrationBranch && promotionTarget
      ? ` (integration: \`${integrationBranch}\` → promotion: \`${promotionTarget}\`)`
      : integrationBranch ? ` (integration: \`${integrationBranch}\`)` : '';
    const lifecycleSignal = typeof versionPhase.lifecycle_signal === 'string' ? versionPhase.lifecycle_signal : 'unavailable';
    const lines: string[] = [
      '\n## Version Phase',
      `**lifecycle_signal: ${lifecycleSignal} / ${signalStatus}**${branchCtx}`,
      'The version-phase lifecycle block could not be assembled this cycle due to a backend error. ' +
      'This is an infrastructure failure — not evidence that no release lifecycle exists. ' +
      'Do not treat the absence of version-phase data as a signal to change phase or qualification strategy.',
    ];
    if (errorSnippet) lines.push(`Error: \`${errorSnippet}\``);
    return lines.join('\n');
  }

  const versionNumber = typeof versionPhase.version_number === 'number' ? versionPhase.version_number : null;
  const phase = typeof versionPhase.phase === 'string' ? versionPhase.phase : null;
  if (versionNumber === null || (phase !== 'build' && phase !== 'stabilize')) return '';

  const phaseChangedAt = typeof versionPhase.phase_changed_at === 'string' ? versionPhase.phase_changed_at : null;
  const tasksMergedThisVersion = typeof versionPhase.tasks_merged_this_version === 'number'
    ? versionPhase.tasks_merged_this_version : null;
  const goal = typeof versionPhase.goal === 'string' && versionPhase.goal.trim()
    ? versionPhase.goal.trim() : null;
  const goalNeeded = versionPhase.goal_needed === true;
  // The backend always sends last_qualification as an object (the structured
  // qualification record: {status, candidate_sha, confidence, blocker, ...}) or
  // null — never a string. Render a compact summary line so the agent can see
  // when a qualification is pending / finalized.
  const lastQualRaw = versionPhase.last_qualification;
  const lastQualification = lastQualRaw && typeof lastQualRaw === 'object' && !Array.isArray(lastQualRaw)
    ? lastQualRaw as Record<string, unknown>
    : null;
  const qualStatus = lastQualification && typeof lastQualification.status === 'string'
    ? lastQualification.status
    : null;
  const qualificationStateRaw = versionPhase.qualification_state;
  const qualificationState = qualificationStateRaw && typeof qualificationStateRaw === 'object' && !Array.isArray(qualificationStateRaw)
    ? qualificationStateRaw as Record<string, unknown>
    : null;
  const effectiveQualStatus = qualStatus === 'pending' && typeof qualificationState?.state === 'string'
    ? qualificationState.state
    : qualStatus;
  const qualificationPending = typeof effectiveQualStatus === 'string'
    && AUDIT_IN_PROGRESS_STATES.has(effectiveQualStatus);
  // A finalized conclusive verdict (dry_run, not_promoted, or promoted) means
  // the candidate has been fully evaluated and the outcome is settled. In the
  // shipped-dark default (promotion_enabled=False) a passing candidate records
  // status='dry_run' and the version stays open + stabilize. Re-emitting
  // `qualify_request` for the SAME candidate just re-spawns two agent VMs for
  // an identical, already-scored commit — the orchestrator's cached_verdict
  // guard ignores it. Only a candidate change (new work merged onto the
  // integration branch) makes a fresh qualification meaningful.
  const qualificationDryRun = qualStatus === 'dry_run';

  const parts: string[] = [];
  parts.push(`\n## Version Phase`);
  parts.push(`Current: **v${versionNumber} — ${phase.toUpperCase()}**`);
  if (goal) {
    parts.push(`Goal: ${goal}`);
  } else if (goalNeeded) {
    parts.push(`**ACTION NEEDED: Author the version goal.** Emit \`new_version_goal\` in Section 6 problem_model update (a one-sentence statement of what this version is for).`);
  }

  if (phaseChangedAt) {
    parts.push(`Phase entered: ${phaseChangedAt}`);
  }
  if (tasksMergedThisVersion !== null) {
    parts.push(`Tasks merged this version: ${tasksMergedThisVersion}`);
  }
  parts.push(...formatCriterionCoverage(versionPhase));
  if (lastQualification) {
    // Compact one-line summary: status, then confidence / candidate_sha /
    // blocker when present. Keeps the prompt small while surfacing whether a
    // qualification is already running (status='pending').
    const qualBits: string[] = [];
    qualBits.push(`status=${effectiveQualStatus ?? 'unknown'}`);
    if (typeof lastQualification.confidence === 'number') {
      qualBits.push(`confidence=${lastQualification.confidence}`);
    }
    if (typeof lastQualification.candidate_sha === 'string' && lastQualification.candidate_sha) {
      qualBits.push(`candidate_sha=${lastQualification.candidate_sha.slice(0, 12)}`);
    }
    if (typeof lastQualification.blocker === 'string' && lastQualification.blocker) {
      qualBits.push(`blocker=${lastQualification.blocker}`);
    }
    if (qualificationDryRun && lastQualification.would_promote === true) {
      qualBits.push(`would_promote=true`);
    }
    parts.push(`Last qualification: ${qualBits.join(', ')}`);
    parts.push(...formatQualificationDetails(lastQualification));
    if (qualificationPending) {
      parts.push(
        `A qualification is currently **PENDING** (running now). Do NOT emit ` +
        `\`qualify_request\` this cycle — wait for it to finalize.`,
      );
    } else if (qualificationDryRun) {
      const dryRunSha = typeof lastQualification.candidate_sha === 'string' && lastQualification.candidate_sha
        ? ` (commit ${lastQualification.candidate_sha.slice(0, 12)})`
        : '';
      parts.push(
        `The last qualification was a **DRY RUN**${dryRunSha} — it already ` +
        `fully evaluated that candidate. Do NOT emit \`qualify_request\` ` +
        `again UNLESS new work has since merged onto the integration branch ` +
        `(i.e. the candidate commit has changed). Re-qualifying the same ` +
        `already-scored candidate just re-spawns agent VMs and is ignored.`,
      );
    }
  }

  // Qualification-state disagreement — surfaced when a finalized verdict
  // superseded a stale pending last_qualification record. Tells the strategist
  // the displayed qualification state reflects the finalized verdict, not the
  // stale 'pending' record that was stored on the version.
  const disagreementRaw = versionPhase.qualification_state_disagreement;
  if (disagreementRaw && typeof disagreementRaw === 'object' && !Array.isArray(disagreementRaw)) {
    const disagreement = disagreementRaw as Record<string, unknown>;
    const lqStoredStatus = typeof disagreement.last_qualification_status === 'string' ? disagreement.last_qualification_status : null;
    const verdictFinalStatus = typeof disagreement.verdict_terminal_status === 'string' ? disagreement.verdict_terminal_status : null;
    if (lqStoredStatus && verdictFinalStatus) {
      parts.push(`  Note: qualification_state_disagreement — stored last_qualification.status='${lqStoredStatus}' but finalized verdict reports '${verdictFinalStatus}'. The qualification state above reflects the finalized verdict.`);
    }
  }

  // Version-phase candidate currency: is the last qualification's candidate SHA still HEAD?
  const lqCurrencyRaw = versionPhase.last_qualification_candidate_currency;
  if (lqCurrencyRaw && typeof lqCurrencyRaw === 'object' && !Array.isArray(lqCurrencyRaw)) {
    const lqc = lqCurrencyRaw as Record<string, unknown>;
    const verdict = typeof lqc.verdict === 'string' ? lqc.verdict : null;
    // Only a RESOLVED comparison can be stale: a boolean `current === false` or an
    // explicit `verdict: 'stale'`. An api_unreachable / unknown verdict (no live
    // comparison) must NOT be treated as stale — that would push the strategist to
    // re-ground load-bearing findings on a transient GitHub outage alone.
    const isResolved = typeof lqc.current === 'boolean';
    const isCurrent = lqc.current === true || verdict === 'fresh';
    const isStale = (isResolved && lqc.current === false) || verdict === 'stale';
    const candidateSha = typeof lqc.candidate_sha === 'string' && lqc.candidate_sha
      ? lqc.candidate_sha.slice(0, 12) : '?';
    const headSha = typeof lqc.head_sha === 'string' && lqc.head_sha
      ? lqc.head_sha.slice(0, 12) : '?';
    if (verdict === 'api_unreachable') {
      const reason = typeof lqc.reason === 'string' && lqc.reason ? ` (${lqc.reason})` : '';
      parts.push(`Qualification candidate currency: API UNREACHABLE${reason} — could not reach GitHub to compare candidate \`${candidateSha}\` against integration HEAD after a bounded retry. This is a temporary reachability problem, NOT a stale-payload signal; do not re-ground findings as stale on this basis alone.`);
    } else if (isStale) {
      parts.push(`Qualification candidate currency: STALE — last qualification candidate \`${candidateSha}\` is behind integration HEAD \`${headSha}\`. Audit findings were scored against an older commit; consider requesting a fresh qualification.`);
    } else if (isCurrent) {
      parts.push(`Qualification candidate currency: CURRENT — candidate \`${candidateSha}\` matches integration HEAD. Audit findings reflect live HEAD.`);
    } else {
      parts.push(`Qualification candidate currency: UNKNOWN — freshness could not be determined for candidate \`${candidateSha}\`. Not confirmed stale; a fresh qualification will resolve currency.`);
    }
  }

  // Goal-coverage drift signal (continuous BUILD/STABILIZE early-warning).
  const driftRaw = versionPhase.goal_coverage_drift;
  if (driftRaw && typeof driftRaw === 'object' && !Array.isArray(driftRaw)) {
    const drift = driftRaw as Record<string, unknown>;
    const driftSignal = typeof drift.signal === 'string' ? drift.signal : null;
    if (driftSignal) {
      const driftBits: string[] = [`signal=${driftSignal}`];
      if (typeof drift.coverage === 'number') driftBits.push(`coverage=${drift.coverage}`);
      if (typeof drift.biggest_gap === 'string' && drift.biggest_gap) driftBits.push(`biggest_gap=${drift.biggest_gap}`);
      if (typeof drift.merges_since_grade === 'number') driftBits.push(`merges_since_grade=${drift.merges_since_grade}`);
      if (drift.stale === true) driftBits.push(`stale=true`);
      const driftDetail = typeof drift.detail === 'string' && drift.detail ? drift.detail : null;
      const driftLine = `Legacy heuristic - Goal-coverage drift: ${driftBits.join(', ')}`;
      if (driftSignal === 'drifting') {
        parts.push(`**${driftLine}** ← accumulated work diverging from version goal; course-correct or propose a goal amendment before qualification deadlock`);
      } else {
        parts.push(driftLine);
      }
      if (driftDetail) parts.push(`  Drift detail: ${driftDetail}`);
    }
  }

  // Goal-alignment verdict from the last qualification (carried inside last_qualification.goal_alignment).
  if (lastQualification) {
    const goalAlignmentRaw = lastQualification.goal_alignment;
    if (goalAlignmentRaw && typeof goalAlignmentRaw === 'object' && !Array.isArray(goalAlignmentRaw)) {
      const ga = goalAlignmentRaw as Record<string, unknown>;
      const gaStatus = typeof ga.status === 'string' ? ga.status : (typeof ga.state === 'string' ? ga.state : null);
      if (gaStatus) {
        const gaBits: string[] = [`status=${gaStatus}`];
        if (typeof ga.coverage === 'number') gaBits.push(`coverage=${ga.coverage}`);
        if (typeof ga.biggest_gap === 'string' && ga.biggest_gap) gaBits.push(`biggest_gap=${ga.biggest_gap}`);
        if (typeof ga.goal_alignment === 'string' && ga.goal_alignment) gaBits.push(`alignment=${ga.goal_alignment}`);
        parts.push(`Legacy heuristic - Last qualification goal alignment: ${gaBits.join(', ')}`);
      }
    }
  }

  // Promotion-readiness trend (cross-version aggregation).
  const promotionReadinessRaw = versionPhase.promotion_readiness;
  if (promotionReadinessRaw && typeof promotionReadinessRaw === 'object' && !Array.isArray(promotionReadinessRaw)) {
    const pr = promotionReadinessRaw as Record<string, unknown>;
    const prReady = pr.ready === true ? 'YES' : 'NO';
    const prRate = typeof pr.would_promote_rate === 'number' ? pr.would_promote_rate : null;
    const prStreak = typeof pr.consecutive_clean_passes === 'number' ? pr.consecutive_clean_passes : null;
    const prEvaluated = typeof pr.verdicts_evaluated === 'number' ? pr.verdicts_evaluated : null;
    const prReason = typeof pr.reason === 'string' ? pr.reason : null;
    const prConf = pr.confidence && typeof pr.confidence === 'object' && !Array.isArray(pr.confidence)
      ? pr.confidence as Record<string, unknown> : null;
    const prRecurring = pr.recurring_blockers && typeof pr.recurring_blockers === 'object' && !Array.isArray(pr.recurring_blockers)
      ? pr.recurring_blockers as Record<string, unknown> : null;

    const prBits: string[] = [`ready=${prReady}`];
    if (prRate !== null) prBits.push(`would_promote_rate=${prRate}`);
    if (prStreak !== null) prBits.push(`consecutive_clean_passes=${prStreak}`);
    if (prEvaluated !== null) prBits.push(`verdicts_evaluated=${prEvaluated}`);
    if (prConf) {
      const confMean = typeof prConf.mean === 'number' ? prConf.mean : null;
      const confStdev = typeof prConf.stdev === 'number' ? prConf.stdev : null;
      const confStable = prConf.stable === true ? 'stable' : (prConf.stable === false ? 'volatile' : null);
      if (confMean !== null) prBits.push(`confidence_mean=${confMean}`);
      if (confStdev !== null) prBits.push(`confidence_stdev=${confStdev}`);
      if (confStable !== null) prBits.push(`confidence=${confStable}`);
    }
    parts.push(`Promotion readiness: ${prBits.join(', ')}`);
    if (prReason) parts.push(`  Readiness reason: ${prReason}`);
    if (prRecurring && Object.keys(prRecurring).length > 0) {
      const blockerStr = Object.entries(prRecurring).map(([code, n]) => `${code}x${String(n)}`).join(', ');
      parts.push(`  Recurring blockers: ${blockerStr}`);
    }
  }

  // Recent closed versions (newest-first, up to 3).
  const recentVersionsRaw = versionPhase.recent_versions;
  if (Array.isArray(recentVersionsRaw) && recentVersionsRaw.length > 0) {
    const rvLines: string[] = ['Recent versions (newest-first):'];
    for (const rv of recentVersionsRaw) {
      if (!rv || typeof rv !== 'object' || Array.isArray(rv)) continue;
      const rvRec = rv as Record<string, unknown>;
      const rvNum = typeof rvRec.version_number === 'number' ? rvRec.version_number : '?';
      const rvStatus = typeof rvRec.status === 'string' ? rvRec.status : '?';
      const lq = rvRec.last_qualification;
      const lqRec = lq && typeof lq === 'object' && !Array.isArray(lq) ? lq as Record<string, unknown> : null;
      const lqStatus = lqRec && typeof lqRec.status === 'string' ? lqRec.status : null;
      const lqConf = lqRec && typeof lqRec.confidence === 'number' ? lqRec.confidence : null;
      const lqBlocker = lqRec && typeof lqRec.blocker === 'string' && lqRec.blocker ? lqRec.blocker : null;
      const rvQs = rvRec.qualification_state;
      const rvQsRec = rvQs && typeof rvQs === 'object' && !Array.isArray(rvQs) ? rvQs as Record<string, unknown> : null;
      const lqBits: string[] = [];
      if (lqStatus) lqBits.push(`qual=${lqStatus}`);
      if (lqConf !== null) lqBits.push(`confidence=${lqConf}`);
      if (lqBlocker) lqBits.push(`blocker=${lqBlocker}`);
      // Enriched qualification state and serving status for closed versions.
      if (rvQsRec && typeof rvQsRec.state === 'string') {
        lqBits.push(`qual_state=${rvQsRec.state}`);
      }
      const rvServingStatus = typeof rvRec.serving_status === 'string' ? rvRec.serving_status :
        (rvQsRec && typeof rvQsRec.serving_status === 'string' ? rvQsRec.serving_status : null);
      if (rvServingStatus) lqBits.push(`serving=${rvServingStatus}`);
      const lqSuffix = lqBits.length > 0 ? ` [${lqBits.join(', ')}]` : '';
      rvLines.push(`  - v${rvNum} (${rvStatus})${lqSuffix}`);
      if (lqRec) {
        rvLines.push(...formatQualificationDetails(lqRec).map(line => `    ${line.trimStart()}`));
      }
    }
    if (rvLines.length > 1) parts.push(rvLines.join('\n'));
  }

  parts.push(`
**Phase meanings:**
- **build** — pursue the version's feature and improvement work at full pace. The version is actively developing; continue creating and executing directions that advance the intent.
- **stabilize** — shift the work-mix toward audit, cleanup, and hardening. The version is approaching a release-qualifiable state; prioritize directions that reduce risk, fix known issues, and improve test coverage over net-new feature work. New feature directions should be deferred to the next version unless they directly unblock stabilization.

**Your judgment on phase:**
Assess whether the current phase still fits the project's state. When a **Goal** is shown above, treat it as what this version is *for* — weigh phase and qualification decisions against whether that goal is substantially met. You are not bound to a schedule or a fixed rule — phase transitions are YOUR call, based on what you observe.

- Flip from **build → stabilize** when: enough feature work has landed that further additions would outpace the project's ability to qualify them, or when there are enough known issues, technical debt, or stability concerns that they should be addressed before continuing to build.
- Flip from **stabilize → build** when: the version has been sufficiently hardened, qualification criteria look achievable, and continued stabilization work has diminishing returns.

If you decide to flip the phase, emit a \`version_phase_decision\` field in your Section 6 problem_model update (as a top-level JSON field alongside the model):
\`\`\`json
{
  "version_phase_decision": {
    "phase": "build" | "stabilize",
    "reasoning": "One or two sentences explaining why you're flipping now."
  }
}
\`\`\`

If you are staying in the current phase, do NOT emit \`version_phase_decision\` — omitting it means "no change."

**Requesting qualification (stabilize only):**
When the version is in **stabilize** and you judge it ready for release — hardening is sufficient, known issues are addressed, and the canonical acceptance criteria above are covered or have a credible closure path — request qualification. Treat uncovered or partial acceptance criteria as the primary release-readiness work queue; legacy goal-alignment and drift heuristics are secondary context. The orchestrator then snapshots the integration branch and runs the automated release qualifier (CI, test-erosion, audit, smoketest). Emit a \`qualify_request\` field in your Section 6 problem_model update (a top-level JSON field alongside the model):
\`\`\`json
{
  "qualify_request": {
    "reason": "One sentence explaining why the version is ready to qualify now."
  }
}
\`\`\`
Only request qualification from the **stabilize** phase — it is ignored in **build**. If the version is not yet release-ready, do NOT emit \`qualify_request\` — omitting it means "not ready." \`version_phase_decision\` and \`qualify_request\` are independent: you may emit either, both, or neither.

**Hold qualification only for legitimate blockers, and keep work flowing while you wait.** Do NOT rush to qualify — readiness is a real judgment, and shipping a half-hardened version to hit a cadence is its own failure. But the trap this phase actually fell into is the opposite: *stalling* — holding qualification on something that is not a real blocker and that nothing is actively working. A legitimate hold is a genuine must-ship concern (a correctness regression, an unmet acceptance criterion, a broken core path) that is being actively driven to resolution. A straggler you are "waiting to land" from a paused or churn-stalled direction is NOT a legitimate hold — nothing is driving it, so the wait is open-ended. In that state the answer is not to rush-qualify; it is to get honest about the gate: if the pending work is genuinely must-ship, re-activate it as an actively-worked fix; if it is only nice-to-have, stop treating it as a blocker. And never let the phase sit idle while you wait — there is always hardening, cleanup, or test-coverage work to keep flowing.

**Do NOT re-request while a qualification is in flight.** If the "Last qualification" line above shows \`status=pending\`, a qualification is already running for this version — the orchestrator has snapshotted a candidate and spawned the audit + smoketest. Do NOT emit \`qualify_request\` again; wait for the in-flight run to finalize (its status will change to \`promoted\`, \`not_promoted\`, \`dry_run\`, or an error). Re-requesting while \`status=pending\` is wasted work and is ignored.

**Do NOT re-request after a dry-run of the same candidate.** If the "Last qualification" line shows \`status=dry_run\`, the qualifier already ran end-to-end (CI, test-erosion, audit, smoketest) on that candidate commit and recorded the result — a dry-run means the candidate would promote but promotion currently ships dark, so the version stays open in \`stabilize\`. Re-emitting \`qualify_request\` for the SAME candidate re-spawns two agent VMs to re-score an identical, already-evaluated commit; the orchestrator's already-dry-run guard ignores it. Only emit \`qualify_request\` again once new work has merged onto the integration branch since that dry-run — i.e. the candidate commit has actually changed. If nothing has merged since the \`dry_run\` line above, do NOT re-request.

**Three distinct version-authoring concepts — keep them separate:**
- \`intent.purpose\` = enduring PROJECT north star, operator-owned, spans all versions — never change it here.
- \`version.goal\` = THIS version's frozen objective — a derivative of purpose, authored once per version.
- \`intent.current_focus\` = live "working on now" status — NOT the goal, advances across cycles.

**Authoring the version goal (when none is set):**
If the open version has no Goal shown above, you may propose one by emitting a \`new_version_goal\` field as a top-level sibling in your Section 6 problem_model update:
\`\`\`json
{
  "new_version_goal": "One concise sentence stating what this version is for."
}
\`\`\`
Only emit \`new_version_goal\` when the Goal is currently unset — once authored, the goal is frozen and a re-emit is silently ignored. Keep it under 500 characters and avoid placeholder or generic text. \`new_version_goal\` is independent of \`version_phase_decision\` and \`qualify_request\` — you may emit any combination.`);

  return parts.join('\n');
}

/**
 * Builds the memo-citations block for the strategic thinker prompt.
 * Memos are terminal reasoning artifacts spawned from research findings the
 * packer dropped under budget pressure; the backend surfaces a digest of the
 * recent ones so the strategic reviewer can cite them by short handle. Each memo
 * is assigned a stable short handle (M0, M1, M2, ...) alongside its real task_id
 * for traceability. Returns '' when there are no memos for this cycle window.
 */
export function buildMemoCitationsSection(memoCitations: unknown): string {
  if (!Array.isArray(memoCitations) || memoCitations.length === 0) return '';
  const lines: string[] = ['\n## Memo Citations (cross-cycle institutional memory)'];
  lines.push(
    'Memos are durable cross-cycle institutional memory: reasoning artifacts from ' +
    'prior cycles that capture why findings were set aside, why directions were ' +
    'retired, or why patterns emerged. Each memo is a completed terminal reasoning ' +
    'task — genuinely useful context that transcends a single cycle.\n' +
    'As you reason about gaps and directions below, actively check whether any memo ' +
    'is relevant to your thinking. If a memo genuinely informs a decision or shapes ' +
    'your understanding of a pattern, cite it by its short handle (e.g., "memo M0", ' +
    '"memo M1"). The handle is stable and maps reliably back to the full task_id for ' +
    'traceability. Cite only when a memo authentically informs your reasoning — ' +
    'valuable citations strengthen the record; performative ones do not.',
  );
  for (let i = 0; i < memoCitations.length; i++) {
    // eslint-disable-next-line @typescript-eslint/no-unsafe-assignment
    const m = memoCitations[i];
    if (!m || typeof m !== 'object') continue;
    const rec = m as Record<string, unknown>;
    const compactText = (value: string) => value.replace(/\s+/g, ' ').trim();
    const taskId = typeof rec.task_id === 'string' && rec.task_id ? rec.task_id : '?';
    const excerpt = typeof rec.summary_excerpt === 'string' ? rec.summary_excerpt.trim() : '';
    const handle = `M${i}`;
    const metaParts: string[] = [`task_id: ${taskId}`];
    if (typeof rec.reasoning_kind === 'string' && rec.reasoning_kind) {
      metaParts.push(`kind: ${rec.reasoning_kind}`);
    }
    if (typeof rec.direction_id === 'string' && rec.direction_id) {
      metaParts.push(`direction: ${rec.direction_id}`);
    }
    const tags = Array.isArray(rec.tags) && rec.tags.length > 0
      ? (rec.tags as unknown[]).filter((t) => typeof t === 'string').join(', ')
      : '';
    if (tags) metaParts.push(`tags: ${tags}`);
    lines.push(`- ${handle} (${metaParts.join('; ')}): ${excerpt || '(no summary)'}`);
    const citationRefs = typeof rec.citation_refs === 'string' ? rec.citation_refs.trim() : '';
    if (citationRefs) {
      lines.push(`  citation_refs: ${citationRefs}`);
    }
    if (typeof rec.success_evidence === 'string' && rec.success_evidence) {
      lines.push(`  success_evidence: ${rec.success_evidence}`);
    }
    if (typeof rec.retirement_condition === 'string' && rec.retirement_condition) {
      lines.push(`  retirement_condition: ${rec.retirement_condition}`);
    }
    const finalMetricsText = typeof rec.final_metrics_excerpt === 'string'
      ? rec.final_metrics_excerpt.trim()
      : (rec.final_metrics && typeof rec.final_metrics === 'object'
        ? Object.entries(rec.final_metrics as Record<string, unknown>)
          .map(([key, value]) => `${key}=${String(value)}`)
          .join(', ')
        : '');
    if (finalMetricsText) {
      lines.push(`  final_metrics: ${finalMetricsText}`);
    }
    const rationaleExcerpt = typeof rec.rationale_excerpt === 'string' && rec.rationale_excerpt
      ? compactText(rec.rationale_excerpt)
      : typeof rec.rationale === 'string'
        ? compactText(rec.rationale).slice(0, 200)
        : '';
    if (rationaleExcerpt) {
      lines.push(`  rationale: ${rationaleExcerpt}`);
    }
    const reasoningExcerpt = typeof rec.reasoning_excerpt === 'string' && rec.reasoning_excerpt
      ? compactText(rec.reasoning_excerpt)
      : typeof rec.reasoning === 'string'
        ? compactText(rec.reasoning).slice(0, 200)
        : '';
    if (reasoningExcerpt) {
      lines.push(`  reasoning: ${reasoningExcerpt}`);
    }
  }
  return lines.join('\n');
}

/**
 * Resolves memo handle references (e.g., "M0", "M1") back to their real task_ids.
 * Scans analysisText for handle patterns and returns the corresponding task_ids
 * in the order they appear. Handles are format like M0, M1, M2, etc.
 * Used to make memo citation capture robust to UUID reproduction fragility.
 */
export function resolveMemoHandles(
  analysisText: string,
  memoCitations: Array<Record<string, unknown> | null | undefined>
): string[] {
  if (memoCitations.length === 0) return [];
  const matches: Array<{ taskId: string; position: number }> = [];

  for (let i = 0; i < memoCitations.length; i++) {
    const m = memoCitations[i];
    if (!m || typeof m !== 'object') continue;
    const taskId = m.task_id;
    if (typeof taskId !== 'string' || !taskId) continue;

    // Look for the handle pattern M{i} (e.g., M0, M1, M2...). The word
    // boundaries keep "M01" from matching handle "M0" and also cover
    // phrase contexts like "memo M0" and "(M0)".
    const handlePattern = new RegExp(`\\bM${i}\\b`);
    const match = handlePattern.exec(analysisText);
    if (match) {
      matches.push({ taskId, position: match.index });
    }
  }

  // Contract (see docstring): return task_ids in the order their handles
  // appear in analysisText, sorted by first-match position.
  matches.sort((a, b) => a.position - b.position);
  return matches.map((entry) => entry.taskId);
}

/**
 * Builds the efficiency-axes section for the strategic thinker prompt.
 * Shows per-axis current value, baseline, and drift_pct so the strategic
 * agent can track whether pipeline efficiency is improving or regressing.
 * Returns a 'data unavailable' marker (not empty string) when absent so the
 * agent is never left guessing whether efficiency was intentionally omitted.
 */
export function buildEfficiencySnapshotSection(efficiencySnapshot: unknown): string {
  const AXIS_LABELS: Record<string, string> = {
    cost_per_merged_pr: 'Cost/merged PR (USD)',
    llm_cost_per_merged_pr: 'LLM cost/merged PR (USD)',
    agent_timeout_rate: 'Agent timeout rate',
    vm_cold_start_p50_ms: 'VM cold-start p50 (ms)',
    vm_reuse_rate: 'VM reuse rate',
    median_api_calls_per_task: 'Median API calls/task',
    median_review_iterations: 'Median review iterations',
    fleet_utilization: 'Fleet slot utilization',
  };

  if (!efficiencySnapshot || typeof efficiencySnapshot !== 'object') {
    return '\n## Pipeline Efficiency Axes\n_data unavailable — no snapshot recorded yet_\n';
  }
  const snap = efficiencySnapshot as Record<string, unknown>;
  const axes = snap.axes && typeof snap.axes === 'object' ? snap.axes as Record<string, unknown> : {};
  const axisKeys = Object.keys(axes);
  if (axisKeys.length === 0) {
    return '\n## Pipeline Efficiency Axes\n_data unavailable — no snapshot recorded yet_\n';
  }

  const fmt = (v: unknown): string => {
    if (v === null || v === undefined) return 'n/a';
    if (typeof v === 'number') return v.toString();
    if (typeof v === 'string' || typeof v === 'boolean') return String(v);
    return 'n/a';
  };
  const fmtDrift = (v: unknown): string => {
    if (v === null || v === undefined) return 'n/a';
    if (typeof v !== 'number') return typeof v === 'string' ? v : 'n/a';
    const sign = v > 0 ? '+' : '';
    return `${sign}${v.toFixed(1)}%`;
  };

  const lines: string[] = ['\n## Pipeline Efficiency Axes'];
  const lastAt = typeof snap.last_snapshot_at === 'string' ? snap.last_snapshot_at.slice(0, 10) : 'unknown';
  const baseAt = typeof snap.baseline_captured_at === 'string' ? snap.baseline_captured_at.slice(0, 10) : 'unknown';
  lines.push(`Baseline: ${baseAt}  |  Last snapshot: ${lastAt}`);
  lines.push('');
  lines.push('axis | current | baseline | drift');
  lines.push('--- | --- | --- | ---');
  for (const key of axisKeys) {
    const data = axes[key] as Record<string, unknown> | undefined;
    if (!data || typeof data !== 'object') continue;
    const label = AXIS_LABELS[key] ?? key;
    const cur = fmt(data.current);
    const base = fmt(data.baseline);
    const drift = fmtDrift(data.drift_pct);
    lines.push(`${label} | ${cur} | ${base} | ${drift}`);
  }
  if ('fleet_utilization' in axes) {
    lines.push('');
    lines.push(
      '_Fleet slot utilization is the share of max-plan account slots in use (now + short trailing average) across the whole fleet. Chronically low utilization WITH verified non-clashing backlog means throughput is being left on the table — parallelize existing work or open more non-clashing directions. But distinguish idle headroom from cooled accounts (shown only when present): cooldowns are a rate-limit problem, not a reason to manufacture work. Near capacity is healthy, not a deficit._',
    );
  }

  const fc = snap.format_compliance && typeof snap.format_compliance === 'object'
    ? snap.format_compliance as Record<string, unknown>
    : null;
  if (fc) {
    const fmtRate = (key: string): string => {
      const r = fc[key] as Record<string, unknown> | undefined;
      if (!r || typeof r !== 'object') return 'n/a';
      return `${fmt(r.current)} (baseline ${fmt(r.baseline)}, drift ${fmtDrift(r.drift_pct)})`;
    };
    lines.push('');
    lines.push('**Format compliance:**');
    lines.push(`  format_compliance_rate: ${fmtRate('format_compliance_rate')}`);
    lines.push(`  self_repair_success_rate: ${fmtRate('self_repair_success_rate')}`);
  }

  const atr = snap.agent_timeout_rate && typeof snap.agent_timeout_rate === 'object'
    ? snap.agent_timeout_rate as Record<string, unknown>
    : null;
  if (atr) {
    const overall = atr.overall && typeof atr.overall === 'object'
      ? atr.overall as Record<string, unknown>
      : null;
    const overallStr = overall
      ? `${fmt(overall.current)} (baseline ${fmt(overall.baseline)}, drift ${fmtDrift(overall.drift_pct)})`
      : 'n/a';
    const rawSubtype = atr.dominant_subtype;
    const dominantSubtype = typeof rawSubtype === 'string' || typeof rawSubtype === 'number' ? String(rawSubtype) : null;
    const rawShare = atr.dominant_share;
    const dominantShare = typeof rawShare === 'string' || typeof rawShare === 'number' ? String(rawShare) : null;
    const dominantStr = dominantSubtype
      ? ` — dominant: ${dominantSubtype}${dominantShare != null ? ` (${dominantShare})` : ''}`
      : '';
    lines.push('');
    lines.push(`**Agent timeout rate:** ${overallStr}${dominantStr}`);
  }

  const costAttr = snap.llm_cost_attribution && typeof snap.llm_cost_attribution === 'object'
    ? snap.llm_cost_attribution as Record<string, unknown>
    : null;
  if (costAttr) {
    const fmtShare = (v: unknown): string => {
      if (v === null || v === undefined) return 'n/a';
      if (typeof v === 'number') return `${(v * 100).toFixed(1)}%`;
      if (typeof v === 'string') return v;
      return 'n/a';
    };
    const fmtRow = (row: unknown, labelKey: string): string => {
      if (!row || typeof row !== 'object') return 'n/a';
      const r = row as Record<string, unknown>;
      const label = typeof r[labelKey] === 'string' || typeof r[labelKey] === 'number' ? String(r[labelKey]) : 'n/a';
      const cost = typeof r.cost_usd === 'number' ? `$${r.cost_usd.toFixed(4)}` : 'n/a';
      const share = typeof r.share === 'number' ? ` (${(r.share * 100).toFixed(1)}%)` : '';
      return `${label} ${cost}${share}`;
    };
    lines.push('');
    lines.push('**LLM cost attribution (vs genesis baseline):**');
    lines.push(`  total: $${typeof costAttr.total_llm_usd === 'number' ? costAttr.total_llm_usd.toFixed(4) : 'n/a'}`);
    lines.push(`  dominant model: ${fmtRow(costAttr.dominant_model, 'model')}`);
    lines.push(`  dominant task type: ${fmtRow(costAttr.dominant_task_type, 'task_type')}`);
    lines.push(`  dominant failure class: ${fmtRow(costAttr.dominant_failure_class, 'failure_class')}`);
    lines.push(`  rework_share: ${fmtShare(costAttr.rework_share)}`);
    lines.push(`  self_repair_burn_share: ${fmtShare(costAttr.self_repair_burn_share)}`);
  }

  const windowDelta = snap.llm_cost_attribution_window_delta && typeof snap.llm_cost_attribution_window_delta === 'object'
    ? snap.llm_cost_attribution_window_delta as Record<string, unknown>
    : null;
  if (windowDelta) {
    const meta = windowDelta.window_meta && typeof windowDelta.window_meta === 'object'
      ? windowDelta.window_meta as Record<string, unknown>
      : null;
    const windowLabel = meta && typeof meta.recent_n === 'number' && typeof meta.prior_count === 'number'
      ? `recent ${meta.recent_n} vs prior ${meta.prior_count} snapshots`
      : 'recent vs prior window';
    lines.push('');
    lines.push(`**LLM cost-mix shift (${windowLabel}):**`);

    const fmtScalarDelta = (field: unknown): string => {
      if (!field || typeof field !== 'object') return 'n/a';
      const f = field as Record<string, unknown>;
      const abs = typeof f.abs_delta === 'number' ? f.abs_delta : null;
      const rel = typeof f.rel_pct === 'number' ? f.rel_pct : null;
      if (abs == null) return 'n/a';
      const sign = abs >= 0 ? '+' : '';
      return rel != null ? `${sign}${abs.toFixed(4)} (${sign}${rel.toFixed(1)}%)` : `${sign}${abs.toFixed(4)}`;
    };

    const totalWd = windowDelta.total_llm_usd;
    const reworkWd = windowDelta.rework_share;
    const repairWd = windowDelta.self_repair_burn_share;
    lines.push(`  total_llm_usd avg/snapshot: ${fmtScalarDelta(totalWd)}`);
    lines.push(`  rework_share: ${fmtScalarDelta(reworkWd)}`);
    lines.push(`  self_repair_burn_share: ${fmtScalarDelta(repairWd)}`);

    const byModelDelta = Array.isArray(windowDelta.by_model_delta) ? windowDelta.by_model_delta : [];
    if (byModelDelta.length > 0) {
      const top = byModelDelta[0] as Record<string, unknown>;
      const model = typeof top.model === 'string' ? top.model : 'unknown';
      const costDelta = typeof top.cost_delta_usd === 'number' ? top.cost_delta_usd : null;
      const shareDelta = typeof top.share_delta === 'number' ? top.share_delta : null;
      const sign = costDelta != null && costDelta >= 0 ? '+' : '';
      const costStr = costDelta != null ? `${sign}$${costDelta.toFixed(4)}` : 'n/a';
      const shareStr = shareDelta != null ? ` (${shareDelta >= 0 ? '+' : ''}${(shareDelta * 100).toFixed(1)}% share)` : '';
      lines.push(`  top model mover: ${model} ${costStr}${shareStr}`);
    }

    const byTypeDelta = Array.isArray(windowDelta.by_task_type_delta) ? windowDelta.by_task_type_delta : [];
    if (byTypeDelta.length > 0) {
      const top = byTypeDelta[0] as Record<string, unknown>;
      const taskType = typeof top.task_type === 'string' ? top.task_type : 'unknown';
      const costDelta = typeof top.cost_delta_usd === 'number' ? top.cost_delta_usd : null;
      const sign = costDelta != null && costDelta >= 0 ? '+' : '';
      const costStr = costDelta != null ? `${sign}$${costDelta.toFixed(4)}` : 'n/a';
      lines.push(`  top task-type mover: ${taskType} ${costStr}`);
    }
  }

  const peff = snap.panel_review_effectiveness && typeof snap.panel_review_effectiveness === 'object'
    ? snap.panel_review_effectiveness as Record<string, unknown>
    : null;
  const peffPd = snap.panel_review_effectiveness_post_deploy && typeof snap.panel_review_effectiveness_post_deploy === 'object'
    ? snap.panel_review_effectiveness_post_deploy as Record<string, unknown>
    : null;
  if (peff || peffPd) {
    const pdGh = peffPd?.gate_health && typeof peffPd.gate_health === 'object'
      ? peffPd.gate_health as Record<string, unknown>
      : null;
    const pdState = pdGh && typeof pdGh.state === 'string' ? pdGh.state : null;
    // Use post-deploy as primary when it has sufficient data.
    // Fall back for NO_DATA (0 gate invocations) and INSUFFICIENT (<min_sample) — both are
    // low-sample states. Keep DEGRADED as primary: it fires at >=5% even on tiny samples
    // and is an intentional alarm, not a data-gap.
    const usePostDeploy = peffPd !== null && pdState !== null
      && pdState !== 'INSUFFICIENT' && pdState !== 'NO_DATA';
    // Within this if-block, at least one of peff/peffPd is non-null.
    // Prefer post-deploy when it has enough data; fall back to rolling otherwise.
    let primary: Record<string, unknown>;
    if (usePostDeploy) {
      primary = peffPd;
    } else if (peff) {
      primary = peff;
    } else {
      primary = peffPd as Record<string, unknown>;
    }
    const primaryGh = primary.gate_health && typeof primary.gate_health === 'object'
      ? primary.gate_health as Record<string, unknown>
      : null;
    const state = primaryGh && typeof primaryGh.state === 'string' ? primaryGh.state : 'NO_DATA';
    const failOpenRate = typeof primary.fail_open_rate === 'number'
      ? `${(primary.fail_open_rate * 100).toFixed(2)}%`
      : 'n/a';
    const totalChallenged = typeof primary.total_challenged === 'number' ? String(primary.total_challenged) : 'n/a';
    const coverage = typeof primary.effective_coverage === 'number'
      ? `${(primary.effective_coverage * 100).toFixed(1)}%`
      : 'n/a';
    let label: string;
    if (usePostDeploy) {
      label = 'Adversarial gate health (post-deploy)';
    } else if (peffPd !== null && peff !== null) {
      // Post-deploy present but low-sample — primary is rolling; annotate accumulation progress.
      const pdTotal = pdGh && typeof pdGh.total_invocations === 'number' ? pdGh.total_invocations : 0;
      const pdMinSample = pdGh && typeof pdGh.min_sample === 'number' ? pdGh.min_sample : 30;
      label = `Adversarial gate health (rolling; post-deploy accumulating ${pdTotal}/${pdMinSample} invocations)`;
    } else if (peffPd !== null && peff === null) {
      // No rolling window; post-deploy present but low-sample — label must name the actual source.
      const pdTotal = pdGh && typeof pdGh.total_invocations === 'number' ? pdGh.total_invocations : 0;
      const pdMinSample = pdGh && typeof pdGh.min_sample === 'number' ? pdGh.min_sample : 30;
      label = `Adversarial gate health (post-deploy low-sample: ${pdTotal}/${pdMinSample} invocations)`;
    } else {
      label = 'Adversarial gate health';
    }
    lines.push('');
    lines.push(`**${label}:** ${state} — fail_open_rate ${failOpenRate} (n=${totalChallenged}, coverage ${coverage})`);
  }

  return lines.join('\n') + '\n';
}

// Portfolio-level view computed from per-direction data — surfaces imbalances
// (category skew, system coverage gaps, concentration, age) as explicit
// decision inputs for the strategic thinker.
export function buildPortfolioComposition(
  activeDirections: Array<Record<string, unknown>>,
  systems: Array<Record<string, unknown>>,
  activeByScope: Record<string, number> | undefined,
  authoritativeActiveTotal?: number,
  authoritativeSystemCounts?: Record<string, number>,
): string {
  const shipped = activeDirections.length;
  // Anchor the empty-state guard and percentage denominator on the trim-proof
  // authoritative active total when present; fall back to the (possibly trimmed)
  // shipped row count for old payloads/tests that omit it. Never render
  // "portfolio is empty" while the authoritative count says directions are active.
  const total = (typeof authoritativeActiveTotal === 'number' && authoritativeActiveTotal >= 0)
    ? authoritativeActiveTotal
    : shipped;
  if (total === 0) return 'No active directions — portfolio is empty.';
  if (shipped === 0) {
    return `Portfolio has ${total} active direction(s), but their per-direction detail was not shipped this cycle (trimmed/dropped under the byte budget) — category mix unavailable. Fetch with \`./fetch direction <id>\`.`;
  }

  const catCounts: Record<string, number> = { product: 0, structural: 0, mixed: 0, uncategorized: 0 };
  const systemCounts: Record<string, number> = {};
  const ageFlags: string[] = [];
  for (const d of activeDirections) {
    const rawCat = typeof d.category === 'string' ? (d.category) : '';
    const cat = ['product', 'structural', 'mixed'].includes(rawCat) ? rawCat : 'uncategorized';
    catCounts[cat] = (catCounts[cat] || 0) + 1;
    const sys = typeof d.system_id === 'string' ? (d.system_id) : '';
    if (sys) systemCounts[sys] = (systemCounts[sys] || 0) + 1;
    const cycles = typeof d.total_cycles === 'number' ? (d.total_cycles) : 0;
    if (cycles >= 10) {
      const id = typeof d.id === 'string' ? (d.id) : '?';
      const text = typeof d.text === 'string' ? (d.text).slice(0, 60) : '';
      ageFlags.push(`${id} (${cycles} cycles): ${text}`);
    }
  }

  const pct = (n: number) => total > 0 ? Math.round((n / total) * 100) : 0;
  const productPct = pct(catCounts.product);
  // The shipped detail list is incomplete when fewer active rows shipped than
  // the authoritative total — i.e. some active directions were trimmed.
  const listIncomplete = shipped < total;
  const lines: string[] = [];
  // When the shipped rows are fewer than the authoritative total, the category
  // mix is computed from the shipped subset but percentages divide by the true
  // total — flag that so the reader does not mistake a partial mix for the whole.
  if (listIncomplete) {
    lines.push(`_Category mix below computed from ${shipped} of ${total} active directions shipped this cycle (${total - shipped} trimmed for budget — fetch with \`./fetch direction <id>\`); percentages use the authoritative total of ${total}._`);
  }
  const catParts = (['product', 'structural', 'mixed', 'uncategorized'] as const)
    .filter(c => catCounts[c] > 0)
    .map(c => `${c}=${catCounts[c]} (${pct(catCounts[c])}%)`);
  // When the direction list is trimmed the category counts only cover the shipped
  // subset, so a definitive low-product alarm is misleading — trimmed-away
  // product directions could bring the full-portfolio ratio above 30%.
  const productFlag = productPct < 30
    ? (listIncomplete
        ? ' [LOW PRODUCT RATIO IN SHOWN SUBSET — <30%; omitted directions may alter the full mix]'
        : ' [LOW PRODUCT RATIO — <30%]')
    : '';
  lines.push(`Category mix: ${catParts.join(', ')}${productFlag}`);

  // Per-system signals (coverage gaps + concentration risk). Prefer the
  // authoritative per-system active map when present — it is computed from the
  // FULL direction set before trimming, so a system whose only direction was
  // trimmed out of the shipped rows is never falsely reported uncovered, and a
  // trimmed direction still counts toward concentration. Fall back to the
  // shipped-row map for older payloads/tests that omit it.
  const hasAuthSystemCounts = authoritativeSystemCounts != null
    && typeof authoritativeSystemCounts === 'object';
  const effectiveSystemCounts: Record<string, number> = hasAuthSystemCounts
    ? authoritativeSystemCounts
    : systemCounts;

  const allSystems = systems.filter(s => typeof s.id === 'string').map(s => ({
    id: s.id as string,
    name: typeof s.name === 'string' ? (s.name) : (s.id as string),
  }));
  if (allSystems.length > 0) {
    const mapped = allSystems.filter(s => (effectiveSystemCounts[s.id] || 0) > 0);
    if (hasAuthSystemCounts || !listIncomplete) {
      // Authoritative map present, OR the shipped rows are complete — uncovered
      // claims are trustworthy.
      const uncovered = allSystems.filter(s => (effectiveSystemCounts[s.id] || 0) === 0);
      lines.push(`System coverage: ${mapped.length}/${allSystems.length} vision systems have directions`
        + (uncovered.length > 0 ? ` — gaps: ${uncovered.map(s => `${s.name} (${s.id})`).join(', ')}` : ''));
    } else {
      // Map absent AND shipped rows incomplete — cannot prove a system is
      // uncovered (its only direction may have been trimmed for budget).
      // Suppress the false gap list; report only the verifiable lower bound.
      lines.push(`System coverage: at least ${mapped.length}/${allSystems.length} vision systems have directions (coverage gaps not shown — per-direction detail trimmed for budget, so an unmapped system may still be covered; fetch with \`./fetch direction <id>\`)`);
    }
  }

  const concentrated = Object.entries(effectiveSystemCounts).filter(([, n]) => n >= 3);
  if (concentrated.length > 0) {
    const parts = concentrated.map(([sid, n]) => `${sid}=${n}`);
    // When derived from the shipped subset only (no authoritative map, list
    // trimmed), the count may under-report directions that were trimmed.
    const partialNote = (!hasAuthSystemCounts && listIncomplete)
      ? ' [from shipped subset only — may under-count trimmed directions]'
      : '';
    lines.push(`CONCENTRATION RISK: 3+ directions on same system: ${parts.join(', ')}${partialNote}`);
  }

  if (activeByScope) {
    const scopeParts: string[] = [];
    for (const s of ['small', 'medium', 'large', 'unknown']) {
      const n = activeByScope[s];
      if (typeof n === 'number' && n > 0) scopeParts.push(`${s}=${n}`);
    }
    if (scopeParts.length > 0) lines.push(`In-flight scope mix: ${scopeParts.join(', ')}`);
  }

  if (ageFlags.length > 0) {
    lines.push(`Long-running (10+ cycles without completion): ${ageFlags.join('; ')}`);
  }

  return lines.join('\n');
}

/**
 * Bootstrap-from-goal research guidance, gated on the unified greenfield
 * predicate (`isGreenfieldRepo`) so the research bootstrap gate and the plan
 * handler's GREENFIELD-MODE gate consult the identical signal and can never
 * disagree. The predicate is greenfield when the durable onboarding signal
 * (`task.repo_is_empty`, persisted as `build_config.repo_is_empty`) is truthy
 * OR the live full-clone `detectGreenfield()` scan says so.
 *
 * Returns the "nearly empty codebase → bootstrap the foundation" paragraph ONLY
 * when the repo is greenfield. Mature-repo agents get `''`, so the guidance
 * stops appearing as noise in every research prompt. The leading blank line
 * keeps spacing when the paragraph is interpolated between other mission
 * bullets.
 */
export function buildEmptyRepoGuidance(task: Record<string, unknown>): string {
  if (!isGreenfieldRepo(task)) return '';
  return `\n**If the codebase is nearly empty** (few source files, mostly config/scaffolding): this is
normal for new projects. Use the design doc specs and the project's config files to create
foundational structure. Output a large-scope finding that bootstraps the target system.\n`;
}

/**
 * Renders the CAPABILITY VERIFICATION section for the tactical research prompt,
 * mirroring the inline SYSTEM VERIFICATION block. The backend's research-side
 * verification loop historically classified only design-doc `systems/` entries;
 * design-doc CAPABILITIES claimed working/partial were never verified for the
 * research agent. So a capability whose backing code is absent on the
 * integration branch slipped through, and the agent reasoned about — and
 * proposed improvements to — behavior that does not exist.
 *
 * The backend now emits a parallel `capability_verification` map (same shape as
 * `system_verification`: per-capability `status` of claimed_absent/partial/
 * verified plus `claimed_status`/`present`/`missing`). This helper surfaces it
 * as a prominent prompt section the agent cannot miss. The classification is
 * conservative by construction (computed backend-side the same way systems are):
 * a capability is flagged claimed_absent only when EVERY referenced backing path
 * is absent; a present path makes it `partial`; a capability with no path
 * pointer is never classified and so never appears here. Returns '' when no
 * verification map is provided, so existing prompts are unchanged.
 */
export function buildCapabilityVerificationSection(
  capVerif: Record<string, Record<string, unknown>> | undefined,
): string {
  if (!capVerif || typeof capVerif !== 'object' || Object.keys(capVerif).length === 0) return '';
  const _pm = capVerif as Record<string, unknown>;
  const computedAt = typeof _pm['_computed_at'] === 'string' ? _pm['_computed_at'] : null;
  const branchSha = typeof _pm['_branch_sha'] === 'string' ? _pm['_branch_sha'] : null;
  const absent: string[] = [];
  const partial: string[] = [];
  const verified: string[] = [];
  for (const capName of Object.keys(capVerif).sort()) {
    if (capName.startsWith('_')) continue;
    const v = capVerif[capName];
    const status = strVal(v.status);
    const missing = Array.isArray(v.missing) ? v.missing as string[] : [];
    const present = Array.isArray(v.present) ? v.present as string[] : [];
    const claimed = strVal(v.claimed_status);
    const claimedNote = claimed ? ` (design-doc status: ${claimed})` : '';
    if (status === 'claimed_absent') {
      absent.push(`- **${capName}**${claimedNote}: CLAIMED-BUT-ABSENT — all ${missing.length} declared backing path(s) missing: ${missing.join(', ')}`);
    } else if (status === 'partial') {
      partial.push(`- **${capName}**${claimedNote}: PARTIAL — missing: ${missing.join(', ')}; present: ${present.join(', ')}`);
    } else if (status === 'verified') {
      verified.push(`- **${capName}**: verified — ${present.length} declared path(s) exist`);
    }
  }
  if (absent.length === 0 && partial.length === 0 && verified.length === 0) return '';
  const vlines: string[] = ['# CAPABILITY VERIFICATION (code existence vs. design-doc claims)'];
  if (computedAt) {
    const shaNote = branchSha ? ` against ${branchSha.slice(0, 12)}` : '';
    vlines.push(`_Verdicts computed ${computedAt}${shaNote}. If this task was retried after a delay, the branch may have changed — treat older stamps as advisory._`);
  } else {
    vlines.push('_⚠️ STAMP MISSING — verdicts may be from an earlier run and could reflect a stale branch state._');
  }
  vlines.push('The backend verified each design-doc capability\'s declared backing paths against the integration-branch file tree. Do NOT trust a capability\'s status over this evidence — if a capability is flagged below, treat its claimed status (working/partial) as unreliable, design from what actually exists, and do NOT propose improvements to behavior that is not built.');
  if (absent.length > 0) {
    vlines.push('\n## ⚠️ Claimed-but-absent (effectively missing — backing code does not exist)');
    vlines.push(...absent);
  }
  if (partial.length > 0) {
    vlines.push('\n## ⚠️ Partial (some declared backing paths missing)');
    vlines.push(...partial);
  }
  if (verified.length > 0) {
    vlines.push('\n## Verified (all declared backing paths present)');
    vlines.push(...verified);
  }
  return vlines.join('\n') + '\n\n';
}

/**
 * Renders the shared "AVAILABLE CAPABILITIES" vocabulary section from the
 * backend-supplied `available_capabilities` payload field. This is the SINGLE
 * shared namespace both producers (a `provides_capability` stamp) and consumers
 * (a `prerequisite` capability ref) draw from, so they converge on the same
 * string instead of inventing divergent free prose that the EXACT (non-fuzzy)
 * matcher can never bridge.
 *
 * Each entry is `{name, status?}` where `name` is a canonical kebab-case
 * capability name from `problem_model.capabilities` and `status` is its declared
 * working/partial/planned state. Rendered uniformly across research, plan,
 * strategic, and design prompts. Returns '' when the field is absent/empty so
 * existing prompts are byte-for-byte unchanged.
 */
export function buildAvailableCapabilitiesSection(caps: unknown): string {
  if (!Array.isArray(caps) || caps.length === 0) return '';
  const lines: string[] = [];
  let anyProofStatus = false;
  for (const c of caps) {
    if (!c || typeof c !== 'object' || Array.isArray(c)) continue;
    const rec = c as Record<string, unknown>;
    const name = strVal(rec.name);
    if (!name) continue;
    const status = strVal(rec.status);
    // proof_status is the SECOND, durable axis (proven / unproven) the strategist
    // records at merge time — independent of the declared status. Render it
    // alongside status when present so a working-but-unproven capability reads
    // differently from a proven one. Absent-field callers render exactly as
    // before (back-compat: `(working)` / `(planned)` / bare name).
    const proofStatus = strVal(rec.proof_status);
    if (proofStatus) anyProofStatus = true;
    const qualifiers = [status, proofStatus].filter(Boolean);
    lines.push(qualifiers.length ? `- \`${name}\` (${qualifiers.join(', ')})` : `- \`${name}\``);
  }
  if (lines.length === 0) return '';
  const legend = anyProofStatus
    ? 'Each entry shows its declared `status` (working / partial / planned) and, when known, its durable `proof_status` (proven / unproven). `proven` means evidence-grounded proof records exist; `working, unproven` means the registry declares it working but no proof has been recorded yet — prefer proven capabilities when a prerequisite must be reliable, and treat unproven ones as claims still awaiting verification.'
    : null;
  return [
    '# AVAILABLE CAPABILITIES (shared vocabulary)',
    'These are the project\'s canonical capability names — the strategist-curated registry (`problem_model.capabilities`). It is the SHARED VOCABULARY for cross-lane prerequisites: when you declare a `prerequisite` capability ref OR a `provides_capability` stamp, DRAW THE NAME FROM THIS LIST so producers and consumers converge on the SAME string (the matcher is EXACT, never fuzzy). Propose a NEW kebab-case name (same convention) ONLY for a genuinely new capability not already listed here.',
    ...(legend ? [legend] : []),
    ...lines,
  ].join('\n') + '\n\n';
}

// ── Extracted ─────────────────────────────────────────────────────────────────

export function buildStrategicCompletionLogs(
  allOutput: string,
  lastResultText: string | null | undefined,
): { logs: string } {
  return { logs: sanitize(lastResultText || allOutput.slice(-50_000)) };
}

/** Maps raw agent-reported failure_category to a high-level failure class. */
export function classifyFailureCategory(rawCat: string): string {
  const CODE_ERROR_CATS: Record<string, true | undefined> = {
    build_failed: true, test_failed: true, lint_failed: true,
    claude_code_failed: true, parse_failed: true, fatal: true,
  };
  if (CODE_ERROR_CATS[rawCat]) return 'CODE_ERROR';
  if (rawCat === 'clone_failed') return 'PROVISION_FAILURE';
  if (rawCat === 'rate_limited') return 'RATE_LIMIT';
  if (rawCat === 'pr_creation_failed') return 'EXTERNAL_API';
  // Pass through if already a high-level class name or unknown
  return rawCat || 'unknown';
}

const _INFRA_CLASSES = new Set(['PROVISION_FAILURE', 'RATE_LIMIT', 'AGENT_TIMEOUT', 'EXTERNAL_API']);
