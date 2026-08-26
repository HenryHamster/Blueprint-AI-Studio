import { strVal } from '../index.js';
import { calcMessageAge, parseResearchContext, buildPortfolioComposition } from './research-plan.js';
import { formatRetiredDirectionGrounding } from './research-direction-context.js';

export function fmtScalar(v: unknown, fallback = '?'): string {
  if (v === undefined || v === null) return fallback;
  if (typeof v === 'string' || typeof v === 'number' || typeof v === 'boolean') return String(v);
  return JSON.stringify(v);
}

export function buildSystemMaturitySection(systemMaturity: Array<Record<string, unknown>>): string {
  if (systemMaturity.length === 0) return '';
  const lines: string[] = ['\n## System Maturity'];
  lines.push('Per-system health snapshots from the last self_audit run. Use these to calibrate which systems need attention vs which can absorb new feature work.');
  for (const s of systemMaturity) {
    const sid = strVal(s.system_id, '?');
    const score = typeof s.score === 'number' ? s.score.toFixed(2) : '?';
    const mode = strVal(s.recommended_mode, 'unknown');
    const audited = strVal(s.last_audited_at, 'never');
    lines.push(`\n### ${sid}  score=${score}  recommended_mode=${mode}  last_audited=${audited}`);
    if (s.top_issues && Array.isArray(s.top_issues) && (s.top_issues as unknown[]).length > 0) {
      lines.push('Top issues: ' + (s.top_issues as string[]).join('; '));
    }
  }
  return lines.join('\n');
}

/**
 * Render the backend `model_health` signal — a SOFT indicator of how large the
 * durable problem_model has grown (honest pre-trim counts). Thresholds are
 * cited so the thinker understands the basis, not just the label. The status is
 * advisory: a 'large'/'oversized' model is a nudge to prune resolved gaps and
 * consolidate overlapping capabilities BEFORE growing the model — never a hard
 * cap that blocks opening a genuinely needed new gap. Returns '' when absent.
 */
export function buildModelHealthSection(modelHealth: Record<string, unknown> | null): string {
  if (!modelHealth) return '';
  const status = strVal(modelHealth.model_size_status, 'unknown');
  const gaps = fmtScalar(modelHealth.active_gap_count);
  const caps = fmtScalar(modelHealth.capability_count);
  const friction = fmtScalar(modelHealth.active_friction_count);
  const footprint = fmtScalar(modelHealth.estimated_token_footprint);
  const lines: string[] = ['\n## Problem-model size (soft signal)'];
  lines.push(
    `Status: **${status}** — active gaps=${gaps}, capabilities=${caps}, active friction=${friction}, ~${footprint} est. tokens (approximate, not precise).`
  );
  lines.push(
    'Healthy is <=12 active gaps / <=20 capabilities / <=8 active friction. ' +
    'A dimension at >=1.5x its threshold is `growing`, >=2x is `large`, >=3x is `oversized`; worst dimension wins.'
  );
  if (status === 'large' || status === 'oversized') {
    lines.push(
      `The model is **${status}** — it is getting expensive to audit in one pass. ` +
      'BEFORE proposing new gaps/capabilities this cycle, prioritize closing or archiving resolved gaps and consolidating overlapping capabilities. ' +
      'This is a soft signal to weigh, NOT a hard cap: if a genuinely new gap is warranted, still open it.'
    );
  }
  return lines.join('\n');
}

export function buildDriftSection(driftSignals: Record<string, unknown> | null): string {
  if (!driftSignals) return '';
  const byDir = driftSignals.by_direction as Record<string, Record<string, unknown>> | undefined;
  const bySys = driftSignals.by_system as Record<string, Record<string, unknown>> | undefined;
  const flaggedDirs = byDir ? Object.entries(byDir).filter(([, sig]) => sig.over_claim_flag || sig.stalled_despite_churn_flag) : [];
  const flaggedSys = bySys ? Object.entries(bySys).filter(([, sig]) => sig.over_claim_flag || sig.stalled_despite_churn_flag) : [];
  if (flaggedDirs.length === 0 && flaggedSys.length === 0) return '';
  const lines: string[] = ['\n## Drift Signals (short-window claim-vs-shipped consistency)'];
  lines.push(`Window: ${strVal(driftSignals.window_days, '?')} days.`);
  lines.push('Directions or systems where claimed changes significantly exceed merged PRs — a signal of over-optimistic task output or stalled work.');
  for (const [did, sig] of flaggedDirs) {
    const flags = [sig.over_claim_flag ? 'over_claim' : '', sig.stalled_despite_churn_flag ? 'stalled_despite_churn' : ''].filter(Boolean).join(', ');
    const consecutiveTail = typeof sig.consecutive_flagged_cycles === 'number' && sig.consecutive_flagged_cycles > 1
      ? `, flagged_cycles=${sig.consecutive_flagged_cycles}`
      : '';
    lines.push(`- Direction ${did}: claimed=${fmtScalar(sig.claimed_changes_count)}, merged=${fmtScalar(sig.actually_merged_prs)}, consistency=${typeof sig.claim_shipped_consistency === 'number' ? sig.claim_shipped_consistency.toFixed(2) : '?'} [${flags}${consecutiveTail}]`);
  }
  for (const [sid, sig] of flaggedSys) {
    const flags = [sig.over_claim_flag ? 'over_claim' : '', sig.stalled_despite_churn_flag ? 'stalled_despite_churn' : ''].filter(Boolean).join(', ');
    lines.push(`- System ${sid}: claimed=${fmtScalar(sig.claimed_changes_count)}, merged=${fmtScalar(sig.actually_merged_prs)}, consistency=${typeof sig.claim_shipped_consistency === 'number' ? sig.claim_shipped_consistency.toFixed(2) : '?'} [${flags}]`);
  }
  return lines.join('\n');
}

/**
 * Render the backend-computed held-directions block (payload field `held_directions`,
 * built in backend/app/strategic_spawning.py). Surfaces the dominant error fingerprint
 * and recent raw failure messages for each direction the hold-manager has flagged as
 * stuck, so the strategist can decide whether to refine or retire it.
 *
 * READ-ONLY planning context: no spawner, no enforcement, no hard block.
 * Returns '' when the block is absent or empty.
 */
export function buildHeldDirectionsSection(heldDirections: Array<Record<string, unknown>> | undefined): string {
  if (!heldDirections || heldDirections.length === 0) return '';
  const lines: string[] = ['\n## Held directions — stuck on consecutive failures'];
  lines.push(
    'These directions have accumulated consecutive task failures and are held by the hold-manager. ' +
    'Review the dominant error fingerprint and recent failure messages. ' +
    'Options: refine the direction scope, spawn a targeted investigation, or retire if no longer viable.',
  );
  for (const h of heldDirections) {
    const text = strVal(h.direction_text, '?');
    const did = strVal(h.direction_id, '?');
    const consecutiveFailures = fmtScalar(h.consecutive_failures, '?');
    const lastClass = strVal(h.last_failure_class, 'unknown');
    const fingerprint = strVal(h.dominant_error_fingerprint, '(none)');
    lines.push(`\n### ${text} (id: ${did})`);
    lines.push(`Consecutive failures: ${consecutiveFailures}  Last failure class: ${lastClass}  Dominant fingerprint: ${fingerprint}`);
    const messages = Array.isArray(h.recent_failure_messages)
      ? h.recent_failure_messages as string[]
      : [];
    if (messages.length > 0) {
      lines.push('Recent failure messages:');
      for (const msg of messages) {
        lines.push(`  - ${strVal(msg, '?').slice(0, 240)}`);
      }
    }
  }
  return lines.join('\n');
}

/**
 * Render the backend-computed forced-strategist-disposition block (payload field
 * `saturation_dispositions`, built by `build_saturation_dispositions` in
 * backend/app/direction_saturation.py). Each entry is a direction whose research
 * keeps producing findings but spawns no work — the prior behavior was a silent
 * longer refractory wait; this block forces an explicit bounded disposition:
 * `commission work` (spawn concrete tasks in the direction), `narrow scope`
 * (tighten the direction's contract so it can produce work again), or `retire`
 * (take the direction off the board as exhausted).
 *
 * Unlike ordinary advisory sections, a pending item here is NOT optional — the
 * strategist MUST emit a disposition this cycle. Resolving happens via the
 * existing strategic ops: `spawn-task` (commission), `update-direction`
 * (narrow), or `retire-direction` (retire). The item persists on the backend
 * until one of those answers it. Returns '' when the block is absent/empty.
 */
export function buildSaturationDispositionsSection(
  saturationDispositions: Array<Record<string, unknown>> | undefined,
): string {
  if (!saturationDispositions || saturationDispositions.length === 0) return '';
  const lines: string[] = ['\n## REQUIRED — saturated direction dispositions'];
  lines.push(
    'The following direction(s) keep producing research findings but have spawned no ' +
    'work, so they saturate: research output accumulates with nothing to show for it. ' +
    'This is NOT an advisory section — you MUST pick a bounded disposition for each ' +
    'listed direction THIS CYCLE and carry it out with the matching strategic op:',
  );
  lines.push('  - `spawn-task` with the direction\'s `direction_id` → **commission work** (concrete tasks now exist in the direction).');
  lines.push('  - `update-direction` narrowing the direction\'s scope/success_evidence/retirement_condition → **narrow scope** (make the direction produce work again).');
  lines.push('  - `retire-direction` → **retire** (the outcome is exhausted or no longer worth staffing).');
  lines.push('A direction left unaddressed stays flagged and will keep surfacing here every cycle until you dispose of it.');
  for (const entry of saturationDispositions) {
    const did = strVal(entry.id, '?');
    const text = strVal(entry.text, '?');
    const priority = strVal(entry.priority, 'unknown');
    const flaggedAt = strVal(entry.flagged_at, '?');
    const cycles = fmtScalar(entry.cycles_researched);
    const findings = fmtScalar(entry.findings_produced);
    const tasks = fmtScalar(entry.recent_tasks_created);
    lines.push(`\n### ${text} (id: ${did})`);
    lines.push(
      `Priority: ${priority}  Flagged: ${flaggedAt}  Cycles researched: ${cycles}  ` +
      `Accumulated findings: ${findings}  Recent tasks spawned: ${tasks}`,
    );
    lines.push('Required disposition: commission work | narrow scope | retire');
  }
  return lines.join('\n');
}

export function buildCapabilityVerificationSection(problemModel: Record<string, unknown> | undefined): string {
  if (!problemModel || typeof problemModel !== 'object') return '';
  const capabilities = Array.isArray(problemModel.capabilities)
    ? problemModel.capabilities as Array<Record<string, unknown>>
    : [];
  if (capabilities.length === 0) return '';

  const flagged: string[] = [];
  for (const capability of capabilities) {
    const verification = capability.verification as Record<string, unknown> | undefined;
    if (!verification || typeof verification !== 'object') continue;
    const effectiveStatus = strVal(verification.effective_status);
    if (effectiveStatus !== 'absent' && effectiveStatus !== 'partial') continue;

    const name = strVal(capability.name, 'Unnamed capability');
    const claimedStatus = strVal(capability.status);
    const missing = Array.isArray(verification.missing) ? verification.missing as string[] : [];
    const present = Array.isArray(verification.present) ? verification.present as string[] : [];
    const claimedNote = claimedStatus ? ` (claimed status: ${claimedStatus})` : '';

    if (effectiveStatus === 'absent') {
      flagged.push(`- **${name}**${claimedNote}: CLAIMED-BUT-ABSENT — all declared backing paths are missing: ${missing.join(', ')}`);
    } else {
      flagged.push(`- **${name}**${claimedNote}: PARTIAL — missing: ${missing.join(', ')}; present: ${present.join(', ')}`);
    }
  }

  if (flagged.length === 0) return '';

  const lines: string[] = ['\n## Capability Verification (advisory)'];
  lines.push('The backend checked declared capability backing paths against the integration-branch file tree. Use flagged entries to avoid hallucinating current behavior, but do not treat missing code as a ban on proposing future work.');
  lines.push(...flagged);
  return lines.join('\n');
}

/**
 * Render the backend-computed cycle-start reconciliation block (payload field
 * `cycle_start_reconciliation`, built by `_build_cycle_start_reconciliation` in
 * backend/app/strategic_spawning.py) as the authoritative starting state for the
 * cycle. It replaces the older scattered capability-verification advisory: rather
 * than make the agent hand-reconcile capabilities, gaps, directions, and research
 * pipeline from separate sections, the backend surfaces only the conflicts between
 * those four surfaces and this renders them as one block.
 */
export function buildCycleStartReconciliationSection(recon: Record<string, unknown> | undefined): string {
  if (!recon || typeof recon !== 'object') return '';

  const capContradictions = Array.isArray(recon.capability_contradictions)
    ? recon.capability_contradictions as Array<Record<string, unknown>> : [];
  const orphanedGaps = Array.isArray(recon.orphaned_gaps)
    ? recon.orphaned_gaps as Array<Record<string, unknown>> : [];
  const staffedButDeferred = Array.isArray(recon.staffed_but_deferred) ? recon.staffed_but_deferred as Array<Record<string, unknown>> : [];
  const ungroundedDirs = Array.isArray(recon.ungrounded_directions)
    ? recon.ungrounded_directions as Array<Record<string, unknown>> : [];
  const staleActiveMoves = Array.isArray(recon.stale_active_moves) ? recon.stale_active_moves as Array<Record<string, unknown>> : [];
  const phantomRefs = Array.isArray(recon.phantom_direction_refs) ? recon.phantom_direction_refs as Array<Record<string, unknown>> : [];
  const pipeline = Array.isArray(recon.direction_pipeline)
    ? recon.direction_pipeline as Array<Record<string, unknown>> : [];
  const counts = (recon.counts && typeof recon.counts === 'object')
    ? recon.counts as Record<string, unknown> : {};
  const countOr = (value: unknown, fallback: number): number => {
    if (typeof value === 'number') return value;
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : fallback;
  };

  const lines: string[] = ['\n## Cycle-start reconciliation (your starting state)'];
  lines.push('The backend reconciled your four surfaces — problem-model capabilities, grounded file-tree verification, active directions (gap_id linkage + deferrals), and each direction\'s research pipeline — and surfaces only the CONFLICTS below. This is where you start: resolve these before reasoning further. An empty list means that surface is consistent.');

  // Capabilities whose stored status disagrees with the grounded file-tree check.
  if (capContradictions.length > 0) {
    lines.push(`\n### Capability status contradicted by code (${fmtScalar(counts.capability_contradictions, String(capContradictions.length))})`);
    lines.push('Stored status disagrees with what the integration-branch file tree actually shows. Not a ban on future work — but do not reason as if the capability already works.');
    for (const c of capContradictions) {
      const name = strVal(c.name, 'Unnamed capability');
      const stored = strVal(c.stored_status, '?');
      const effective = strVal(c.effective_status, '?');
      const missing = Array.isArray(c.missing) ? c.missing as string[] : [];
      const missingTotal = typeof c.missing_total === 'number' ? c.missing_total : missing.length;
      const missingOverflow = Math.max(0, missingTotal - missing.length);
      const missTail = missing.length > 0 ? ` — missing: ${missing.join(', ')}` : '';
      const overflowTail = missingOverflow > 0 ? ` (+${missingOverflow} more not shown)` : '';
      lines.push(`- **${name}**: stored=${stored} but code shows ${effective.toUpperCase()}${missTail}${overflowTail}`);
    }
  }

  // Open gaps (active or blocked) with no direction working them and no
  // intentional deferral. Blocked orphans are flagged so they read distinctly.
  if (orphanedGaps.length > 0) {
    lines.push(`\n### Orphaned open gaps — unstaffed, not deferred (${fmtScalar(counts.orphaned_gaps, String(orphanedGaps.length))})`);
    lines.push('No active direction advances these and no deferral is recorded. Open a direction, defer the gap explicitly, or close it.');
    for (const g of orphanedGaps) {
      const name = strVal(g.name, 'Unnamed gap');
      const gid = strVal(g.gap_id, '?');
      const priority = strVal(g.priority, 'medium');
      const statusTail = strVal(g.status, 'active') === 'blocked' ? ' — BLOCKED' : '';
      lines.push(`- **${name}** [${priority}] (gap ${gid})${statusTail}`);
    }
  }

  if (staffedButDeferred.length > 0) {
    lines.push(`\n### Staffed-but-deferred gaps — contradictory staffing state (${fmtScalar(counts.staffed_but_deferred, String(staffedButDeferred.length))})`);
    lines.push('These gaps have an active direction pointing at them while also carrying a deferral. Either remove the deferral or park/retarget the direction.');
    for (const g of staffedButDeferred) {
      lines.push(`- **${strVal(g.name, 'Unnamed gap')}** [${strVal(g.priority, 'medium')}] (gap ${strVal(g.gap_id, '?')}, status=${strVal(g.status, 'active')})`);
    }
  }

  // Active directions whose gap_id no longer resolves to an active gap.
  if (ungroundedDirs.length > 0) {
    lines.push(`\n### Ungrounded active directions — advance no tracked gap (${fmtScalar(counts.ungrounded_directions, String(ungroundedDirs.length))})`);
    lines.push('These directions trace to no active gap. Link each to a gap (set gap_id), or retire it if it no longer serves the intent.');
    for (const d of ungroundedDirs) {
      const text = strVal(d.text, '?');
      const did = strVal(d.direction_id, '?');
      const reason = strVal(d.reason) === 'gap_missing'
        ? 'its gap_id no longer resolves to an active gap'
        : 'no gap_id linkage';
      lines.push(`- ${text} (id: ${did}) — ${reason}`);
    }
  }

  if (staleActiveMoves.length > 0) {
    lines.push(`\n### Stale gap active_moves — listed direction is no longer active (${fmtScalar(counts.stale_active_moves, String(staleActiveMoves.length))})`);
    lines.push('These gap active_moves entries point at retired, completed, deleted, or otherwise non-active directions. Prune the stale IDs before trusting gap staffing.');
    for (const g of staleActiveMoves) {
      const staleIds = Array.isArray(g.stale_direction_ids) ? (g.stale_direction_ids as string[]).join(', ') : '?';
      lines.push(`- **${strVal(g.gap_name, 'Unnamed gap')}** (gap ${strVal(g.gap_id, '?')}) — stale active_moves: ${staleIds}`);
    }
  }

  // Active directions whose criterion_keys map to inactive or unknown
  // (dangling / wrong-version) acceptance criteria for the open version.
  const criterionLinkConflicts = Array.isArray(recon.criterion_link_conflicts)
    ? recon.criterion_link_conflicts as Array<Record<string, unknown>> : [];
  if (criterionLinkConflicts.length > 0) {
    lines.push(`\n### Broken criterion links — direction maps to a non-active criterion (${fmtScalar(counts.criterion_link_conflicts, String(criterionLinkConflicts.length))})`);
    lines.push('These directions carry criterion_keys that are retracted/superseded (inactive) or absent from the open version (dangling). Remap each to an active criterion or clear the stale key.');
    for (const c of criterionLinkConflicts) {
      const text = strVal(c.direction_text ?? c.text, '?');
      const did = strVal(c.direction_id, '?');
      const key = strVal(c.criterion_key, '?');
      const cls = strVal(c.classification) === 'inactive'
        ? 'retracted or superseded'
        : 'not in the open version';
      const hint = c.recovery_hint ? ` — ${strVal(c.recovery_hint)}` : '';
      lines.push(`- ${text} (id: ${did}) → criterion ${key} is ${cls}${hint}`);
    }
  }

  if (phantomRefs.length > 0) {
    lines.push(`\n### Phantom direction refs — prose cites missing directions (${fmtScalar(counts.phantom_direction_refs, String(phantomRefs.length))})`);
    lines.push('These prose references do not resolve to any known direction. Rewrite the stale narrative or link it to a real direction.');
    for (const p of phantomRefs) {
      lines.push(`- ${strVal(p.direction_id, '?')} cited in ${strVal(p.cited_in, '?')}`);
    }
  }

  const qualProseDrift = Array.isArray(recon.qualification_prose_drift)
    ? recon.qualification_prose_drift as Array<Record<string, unknown>> : [];
  if (qualProseDrift.length > 0) {
    lines.push(`\n### Qualification prose-drift — stale candidate SHA in prose (${fmtScalar(counts.qualification_prose_drift, String(qualProseDrift.length))})`);
    lines.push(`Active candidate: \`${strVal(qualProseDrift[0]?.active_candidate_sha, '?')}\`. The prose fields below reference a different SHA. Rewrite the stale narrative to reflect the current candidate or remove the specific SHA reference.`);
    for (const q of qualProseDrift) {
      lines.push(`- \`${strVal(q.stale_sha, '?')}\` cited in ${strVal(q.cited_in, '?')} (active: \`${strVal(q.active_candidate_sha, '?')}\`)`);
    }
  }

  const qualSurfaceConflicts = Array.isArray(recon.qualification_surface_conflicts)
    ? recon.qualification_surface_conflicts as Array<Record<string, unknown>> : [];
  if (qualSurfaceConflicts.length > 0) {
    const qualConflictsTotal = countOr(counts.qualification_surface_conflicts, qualSurfaceConflicts.length);
    lines.push(`\n### Qualification surface conflicts — coherence disagreements (${qualConflictsTotal})`);
    lines.push('The backend detected disagreements between qualification surfaces (candidate currency, qualification state, criterion coverage, promotion readiness). Each entry names a specific cross-surface contradiction. Resolve the conflict before advancing the qualification.');
    for (const c of qualSurfaceConflicts) {
      lines.push(`- [${strVal(c.conflict_type, '?')}] ${strVal(c.description, '?')}`);
    }
    const overflow = qualConflictsTotal - qualSurfaceConflicts.length;
    if (overflow > 0) {
      lines.push(`- +${overflow} more not shown`);
    }
  }

  // Per-direction research pipeline: where is each active direction stuck/moving.
  const waiting = pipeline.filter(p => strVal(p.state as string) === 'research_waiting_on_action');
  const inProgress = pipeline.filter(p => strVal(p.state as string) === 'tasks_in_progress');
  const held = pipeline.filter(p => strVal(p.state as string) === 'held_on_dependency');
  const recent = pipeline.filter(p => strVal(p.state as string) === 'recently_converted');
  const idle = pipeline.filter(p => strVal(p.state as string) === 'idle');
  const waitingTotal = countOr(counts.pipeline_waiting_on_action, waiting.length);
  const inProgressTotal = countOr(counts.pipeline_tasks_in_progress, inProgress.length);
  const heldTotal = countOr(counts.pipeline_held_on_dependency, held.length);
  const recentTotal = countOr(counts.pipeline_recently_converted, recent.length);
  const idleTotal = countOr(counts.pipeline_idle, idle.length);
  const pipelineOverflow = countOr(counts.direction_pipeline_overflow, 0);
  if (
    pipeline.length > 0 ||
    waitingTotal > 0 ||
    inProgressTotal > 0 ||
    heldTotal > 0 ||
    recentTotal > 0 ||
    idleTotal > 0
  ) {
    const deferredGaps = fmtScalar(counts.deferred_gaps, '0');
    lines.push(`\n### Research pipeline by direction (${inProgressTotal} with tasks in progress, ${heldTotal} live-held, ${recentTotal} recently converted, ${waitingTotal} waiting on action, ${idleTotal} idle; ${deferredGaps} gaps deferred)`);
    if (pipelineOverflow > 0) {
      lines.push(`Only the first ${pipeline.length} active directions are listed below (+${pipelineOverflow} more not shown).`);
    }
    if (inProgressTotal > 0) {
      lines.push('Already moving (tasks in flight — do not double-spawn):');
      for (const p of inProgress) {
        lines.push(`- ${strVal(p.text, '?')} (id: ${strVal(p.direction_id, '?')}) — ${fmtScalar(p.in_flight, '0')} task(s) in flight`);
      }
      if (inProgressTotal > inProgress.length) {
        lines.push(`- +${inProgressTotal - inProgress.length} more in-progress direction(s) not shown`);
      }
    }
    if (heldTotal > 0) {
      lines.push('Live-held on dependency (blocked or parked work is not waiting-on-action):');
      for (const p of held) {
        lines.push(`- ${strVal(p.text, '?')} (id: ${strVal(p.direction_id, '?')}) — ${fmtScalar(p.held, '0')} held task(s), ${fmtScalar(p.findings, '0')} recent finding(s)`);
      }
      if (heldTotal > held.length) {
        lines.push(`- +${heldTotal - held.length} more held direction(s) not shown`);
      }
    }
    if (recentTotal > 0) {
      lines.push('Recently converted/completed (fresh work before the cycle start — not stalled):');
      for (const p of recent) {
        lines.push(`- ${strVal(p.text, '?')} (id: ${strVal(p.direction_id, '?')}) — ${fmtScalar(p.recently_completed, '0')} recently completed task(s), ${fmtScalar(p.findings, '0')} recent finding(s)`);
      }
      if (recentTotal > recent.length) {
        lines.push(`- +${recentTotal - recent.length} more recently-converted direction(s) not shown`);
      }
    }
    if (waitingTotal > 0) {
      lines.push('Research surfaced findings but NOTHING is acting on them — these are the directions most likely to need a plan/create task spawned:');
      for (const p of waiting) {
        lines.push(`- ${strVal(p.text, '?')} (id: ${strVal(p.direction_id, '?')}) — ${fmtScalar(p.findings, '0')} recent finding(s), 0 tasks in flight`);
      }
      if (waitingTotal > waiting.length) {
        lines.push(`- +${waitingTotal - waiting.length} more waiting direction(s) not shown`);
      }
    }
    if (idleTotal > 0) {
      const idleNames = idle.map(p => strVal(p.text as string, '?')).join('; ');
      if (idle.length > 0) {
        const idleTail = idleTotal > idle.length ? `; +${idleTotal - idle.length} more not shown` : '';
        lines.push(`Idle (no findings, no tasks in flight): ${idleNames}${idleTail}`);
      } else {
        lines.push(`Idle (no findings, no tasks in flight): +${idleTotal} direction(s) not shown`);
      }
    }
  }

  // Merge-accounting drift: the merge badge asserts a PR shipped, but ancestry
  // verification over the same recent window says the merge commit is NOT
  // reachable from the integration branch. Only verified-not-landed surfaces as
  // a conflict; unverifiable items are counted (so nothing is silently dropped)
  // but never treated as a contradiction.
  const mergeAccounting = Array.isArray(recon.merge_accounting)
    ? recon.merge_accounting as Array<Record<string, unknown>> : [];
  const mergeNotLandedTotal = countOr(counts.merge_not_landed, 0);
  const mergeUnverifiableTotal = countOr(counts.merge_unverifiable, 0);
  const mergePendingInOpenRollupTotal = countOr(counts.merge_pending_in_open_rollup, 0);
  const mergeDirTotal = countOr(counts.merge_accounting, mergeAccounting.length);
  if (mergeAccounting.length > 0 || mergeNotLandedTotal > 0) {
    lines.push(`\n### Merge-accounting drift — badge says shipped, ancestry says NOT landed (${mergeDirTotal} direction(s), ${mergeNotLandedTotal} title(s); ${mergeUnverifiableTotal} unverifiable)`);
    lines.push('The merge badge marked these PRs shipped, but an ancestry check against the integration branch shows the merge commit is NOT reachable — the work did not actually land. Reconcile before crediting these directions. Unverifiable items (ancestry could not be confirmed) are tallied but are NOT treated as conflicts.');
    for (const m of mergeAccounting) {
      const text = strVal(m.direction_text, '?');
      const did = strVal(m.direction_id, '?');
      const notLanded = countOr(m.not_landed, 0);
      const landed = countOr(m.landed, 0);
      const unverifiable = countOr(m.unverifiable, 0);
      const titles = Array.isArray(m.not_landed_titles) ? m.not_landed_titles as string[] : [];
      const titlesOverflow = countOr(m.not_landed_titles_overflow, 0);
      lines.push(`- ${text} (id: ${did}) — ${notLanded} NOT landed, ${landed} landed, ${unverifiable} unverifiable`);
      for (const t of titles) {
        lines.push(`  - ✗ ${strVal(t, '?')}`);
      }
      if (titlesOverflow > 0) {
        lines.push(`  - +${titlesOverflow} more not-landed title(s) not shown`);
      }
    }
    if (mergeDirTotal > mergeAccounting.length) {
      lines.push(`- +${mergeDirTotal - mergeAccounting.length} more direction(s) with drift not shown`);
    }
  }

  // Pending rollup merges: PRs merged into staging branches awaiting rollup squash-merge
  // into the integration branch. These are normal work-in-progress, not accounting conflicts.
  const pendingRollupAccounting = Array.isArray(recon.pending_in_open_rollup)
    ? recon.pending_in_open_rollup as Array<Record<string, unknown>> : [];
  if (pendingRollupAccounting.length > 0 || mergePendingInOpenRollupTotal > 0) {
    const rollupDirTotal = countOr(counts.merge_pending_in_open_rollup_accounting, pendingRollupAccounting.length);
    lines.push(`\n### Pending rollup merges — work awaiting squash-merge to integration branch (${rollupDirTotal} direction(s), ${mergePendingInOpenRollupTotal} title(s))`);
    lines.push('These PRs are merged into staging branches and awaiting the rollup to squash-merge into the integration branch. This is normal in-progress work, not a lost-work alarm.');
    for (const m of pendingRollupAccounting) {
      const text = strVal(m.direction_text, '?');
      const did = strVal(m.direction_id, '?');
      const pending = countOr(m.pending_in_open_rollup, 0);
      const titles = Array.isArray(m.pending_in_open_rollup_titles) ? m.pending_in_open_rollup_titles as string[] : [];
      const titlesOverflow = countOr(m.pending_in_open_rollup_titles_overflow, 0);
      lines.push(`- ${text} (id: ${did}) — ${pending} pending rollup merge`);
      for (const t of titles) {
        lines.push(`  - ⧗ ${strVal(t, '?')}`);
      }
      if (titlesOverflow > 0) {
        lines.push(`  - +${titlesOverflow} more pending title(s) not shown`);
      }
    }
    if (rollupDirTotal > pendingRollupAccounting.length) {
      lines.push(`- +${rollupDirTotal - pendingRollupAccounting.length} more direction(s) not shown`);
    }
  }

  return lines.join('\n');
}

export function buildPruneCandidatesSection(prune: Record<string, unknown> | undefined): string {
  if (!prune || typeof prune !== 'object') return '';

  const resolvedGaps = Array.isArray(prune.resolved_gaps)
    ? prune.resolved_gaps as Array<Record<string, unknown>> : [];
  const absentCaps = Array.isArray(prune.absent_capabilities)
    ? prune.absent_capabilities as Array<Record<string, unknown>> : [];
  const expiredHolds = Array.isArray(prune.expired_holds)
    ? prune.expired_holds as Array<Record<string, unknown>> : [];
  const ungroundableCaps = Array.isArray(prune.ungroundable_capabilities)
    ? prune.ungroundable_capabilities as Array<Record<string, unknown>> : [];
  const domainMergeGroups = Array.isArray(prune.domain_merge_groups)
    ? prune.domain_merge_groups as Array<Record<string, unknown>> : [];
  const heavyGaps = Array.isArray(prune.heavy_gaps)
    ? prune.heavy_gaps as Array<Record<string, unknown>> : [];
  const longNames = Array.isArray(prune.long_names)
    ? prune.long_names as Array<Record<string, unknown>> : [];
  if (
    resolvedGaps.length === 0 && absentCaps.length === 0 && expiredHolds.length === 0 &&
    ungroundableCaps.length === 0 && domainMergeGroups.length === 0 &&
    heavyGaps.length === 0 && longNames.length === 0
  ) return '';

  const counts = (prune.counts && typeof prune.counts === 'object')
    ? prune.counts as Record<string, unknown> : {};
  const status = strVal(prune.model_size_status, 'large');

  const lines: string[] = ['\n## Concrete prune candidates (model is ' + status + ')'];
  lines.push(
    'The problem model is **' + status + '** and only shrinks when you explicitly close/archive entries. ' +
    'These specific entries are resolved-in-fact-but-still-open, drawn from signals the backend already has. ' +
    'This is an advisory suggestion list — never an auto-deletion. Closing/consolidating these is a one-cycle move to shrink the model.'
  );

  // Active gaps whose work landed or stopped, yet the gap is still open.
  if (resolvedGaps.length > 0) {
    lines.push(`\n### Resolved-yet-open gaps (${fmtScalar(counts.resolved_gaps, String(resolvedGaps.length))})`);
    lines.push('Active gaps whose direction has retired or shipped merged PRs — the work is done or abandoned but the gap is still open. Close or archive these.');
    for (const g of resolvedGaps) {
      const name = strVal(g.name, 'Unnamed gap');
      const gid = strVal(g.gap_id, '?');
      const reason = strVal(g.reason) === 'direction_shipped'
        ? 'its direction shipped merged PRs'
        : 'its direction was retired';
      const dirs = Array.isArray(g.directions) ? (g.directions as string[]).join(', ') : '';
      const dirTail = dirs ? ` [${dirs}]` : '';
      lines.push(`- **${name}** (gap ${gid}) — ${reason}${dirTail}`);
    }
    if (resolvedGaps.length > 0 && countOrLocal(counts.resolved_gaps, resolvedGaps.length) > resolvedGaps.length) {
      lines.push(`- +${countOrLocal(counts.resolved_gaps, resolvedGaps.length) - resolvedGaps.length} more not shown`);
    }
  }

  // Capabilities the grounded file-tree check says are gone.
  if (absentCaps.length > 0) {
    lines.push(`\n### Claimed-but-absent capabilities (${fmtScalar(counts.absent_capabilities, String(absentCaps.length))})`);
    lines.push('The grounded file-tree check shows the backing code does not exist. These are stale assertions safe to consolidate or drop.');
    for (const c of absentCaps) {
      const name = strVal(c.name, 'Unnamed capability');
      const stored = strVal(c.stored_status, '?');
      lines.push(`- **${name}** (stored status=${stored}) — code is absent`);
    }
    if (countOrLocal(counts.absent_capabilities, absentCaps.length) > absentCaps.length) {
      lines.push(`- +${countOrLocal(counts.absent_capabilities, absentCaps.length) - absentCaps.length} more not shown`);
    }
  }

  // Gaps whose deferral hold has elapsed — due for a decision.
  if (expiredHolds.length > 0) {
    lines.push(`\n### Expired deferral holds (${fmtScalar(counts.expired_holds, String(expiredHolds.length))})`);
    lines.push('Active gaps whose deferral hold_until has already passed — the hold you set has elapsed, so each is due for a decision (resume, re-defer, or close).');
    for (const g of expiredHolds) {
      const name = strVal(g.name, 'Unnamed gap');
      const gid = strVal(g.gap_id, '?');
      const until = strVal(g.hold_until, '?');
      lines.push(`- **${name}** (gap ${gid}) — hold expired ${until}`);
    }
    if (countOrLocal(counts.expired_holds, expiredHolds.length) > expiredHolds.length) {
      lines.push(`- +${countOrLocal(counts.expired_holds, expiredHolds.length) - expiredHolds.length} more not shown`);
    }
  }

  // Capabilities with no backing_paths, no source, and no verification — unverifiable claims.
  if (ungroundableCaps.length > 0) {
    lines.push(`\n### Ungroundable capabilities — no backing paths, no source, no verification (${fmtScalar(counts.ungroundable_capabilities, String(ungroundableCaps.length))})`);
    lines.push('These capabilities have never been grounded: no file-tree backing paths, no source provenance, and no verification check has run. They are unverifiable claims. Ground them by adding backing_paths or a source, consolidate them into a grounded capability, or drop them.');
    for (const c of ungroundableCaps) {
      const name = strVal(c.name, 'Unnamed capability');
      const capStatus = strVal(c.status, '?');
      lines.push(`- **${name}** (status=${capStatus})`);
    }
    if (countOrLocal(counts.ungroundable_capabilities, ungroundableCaps.length) > ungroundableCaps.length) {
      lines.push(`- +${countOrLocal(counts.ungroundable_capabilities, ungroundableCaps.length) - ungroundableCaps.length} more not shown`);
    }
  }

  // Advisory merge groups: capabilities sharing a domain keyword bucket.
  if (domainMergeGroups.length > 0) {
    lines.push(`\n### Advisory capability merge groups — shared domain keyword (${fmtScalar(counts.domain_merge_groups, String(domainMergeGroups.length))} group(s))`);
    lines.push('Live capabilities that share a domain keyword and may overlap in scope. These are advisory consolidation candidates — review whether they describe the same underlying capability or can be merged.');
    for (const g of domainMergeGroups) {
      const bucket = strVal(g.bucket, '?');
      const names = Array.isArray(g.names) ? (g.names as string[]).join(', ') : '?';
      lines.push(`- **${bucket}**: ${names}`);
    }
  }

  // Heaviest active gaps by serialized history size — compaction targets.
  if (heavyGaps.length > 0) {
    lines.push(`\n### Heaviest active gap histories — compaction targets (${fmtScalar(counts.heavy_gaps, String(heavyGaps.length))})`);
    lines.push('Active gaps with the largest serialized history logs. Long histories inflate every strategic payload. Consider compacting old history entries for these gaps.');
    for (const g of heavyGaps) {
      const name = strVal(g.name, 'Unnamed gap');
      const gid = strVal(g.gap_id, '?');
      const entries = fmtScalar(g.history_entry_count);
      const bytes = fmtScalar(g.approx_bytes);
      lines.push(`- **${name}** (gap ${gid}) — ${entries} history entries, ~${bytes} bytes`);
    }
  }

  // Long-name hygiene: capabilities and gaps with names exceeding the threshold.
  if (longNames.length > 0) {
    lines.push(`\n### Long-name hygiene — names exceeding 80 chars (${fmtScalar(counts.long_names, String(longNames.length))})`);
    lines.push('Capability and gap names longer than 80 characters inflate every payload that ships the capability vocabulary. Rename to shorter, equally descriptive identifiers.');
    for (const e of longNames) {
      const name = strVal(e.name, '?');
      const kind = strVal(e.kind, '?');
      const length = fmtScalar(e.length);
      lines.push(`- **${name}** (${kind}, ${length} chars)`);
    }
    if (countOrLocal(counts.long_names, longNames.length) > longNames.length) {
      lines.push(`- +${countOrLocal(counts.long_names, longNames.length) - longNames.length} more not shown`);
    }
  }

  return lines.join('\n');
}

function countOrLocal(value: unknown, fallback: number): number {
  if (typeof value === 'number') return value;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

function formatMetricNumber(value: number): string {
  return Number.isInteger(value) ? String(value) : value.toFixed(4).replace(/\.?0+$/, '');
}

function metricDirection(operator: unknown): 'higher' | 'lower' | null {
  const op = strVal(operator);
  if (op === '>' || op === '>=') return 'higher';
  if (op === '<' || op === '<=') return 'lower';
  return null;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return !!value && typeof value === 'object' && !Array.isArray(value);
}

function pickOwnVerdict(arm: Record<string, unknown>): Record<string, unknown> | null {
  const verdicts = arm.per_arm_verdicts;
  if (!isRecord(verdicts)) return null;
  const armKey = strVal(arm.arm_label) || strVal(arm.id) || null;
  if (armKey !== null && isRecord(verdicts[armKey])) return verdicts[armKey];
  const verdictValues = Object.values(verdicts);
  return verdictValues.length === 1 && isRecord(verdictValues[0])
    ? verdictValues[0]
    : null;
}

function bestArmByMetric(
  arms: Array<{ id: string; label: string; value: number }>,
  direction: 'higher' | 'lower' | null,
): { id: string; label: string; value: number } | null {
  if (arms.length === 0 || direction === null) return null;
  return arms.reduce<{ id: string; label: string; value: number } | null>((best, arm) => {
    if (best === null) return arm;
    const better = direction === 'higher' ? arm.value > best.value : arm.value < best.value;
    return better ? arm : best;
  }, null);
}

function renderMetricTail(
  metricName: string,
  arm: { label: string; value: number },
  runnerUp: { label: string; value: number } | null,
  direction: 'higher' | 'lower' | null,
): string {
  const valuePart = `${metricName}=${formatMetricNumber(arm.value)}`;
  if (!runnerUp || direction === null) return ` (${valuePart})`;
  const margin = direction === 'higher'
    ? arm.value - runnerUp.value
    : runnerUp.value - arm.value;
  return ` (${valuePart}; margin=${formatMetricNumber(Math.abs(margin))} over ${runnerUp.label}=${formatMetricNumber(runnerUp.value)})`;
}

/**
 * Render a mandatory synthesis block for fully-verdicted multi-arm compute-experiment
 * sweeps (all arms MET or NOT MET — no UNMEASURED or unavailable entries). A completed
 * sweep cannot be silently skipped: the thinker MUST produce a comparative conclusion
 * (which arm won, by what margin, what it implies) and exactly one follow-on move
 * (adopt winning arm, commission deeper follow-on, or retire hypothesis as settled).
 *
 * Input is the `compute_experiment_sweeps` payload field: a map from sweep_id to an
 * array of knowledge-index arm entries (shape: {id, title, arm_label, criterion_met,
 * unavailable_cause, summary}).
 *
 * Returns '' when there are no fully-verdicted sweeps, so the section is omitted on
 * the common path.
 */
export function buildSweepComparisonSection(
  sweepMap: Record<string, Array<Record<string, unknown>>> | undefined,
): string {
  if (!sweepMap || typeof sweepMap !== 'object' || Array.isArray(sweepMap)) return '';

  // Collect fully-verdicted sweeps: every arm is MET (criterion_met===true) or
  // NOT MET (criterion_met===false). Arms that are UNMEASURED (null) or unavailable
  // disqualify the sweep — the strategist cannot synthesize without all verdicts.
  // Sweeps that already have a disposition task in flight are tracked separately so
  // the strategist is not prompted to re-synthesize them.
  type FullSweep = { sweepId: string; arms: Array<Record<string, unknown>> };
  type DisposedSweep = { sweepId: string; disposition: Record<string, unknown> };
  const completed: FullSweep[] = [];
  const disposed: DisposedSweep[] = [];
  for (const [sweepId, arms] of Object.entries(sweepMap)) {
    if (!Array.isArray(arms) || arms.length === 0) continue;
    const dispArm = arms.find(a => a.sweep_disposition != null && typeof a.sweep_disposition === 'object' && !Array.isArray(a.sweep_disposition));
    if (dispArm) {
      disposed.push({ sweepId, disposition: dispArm.sweep_disposition as Record<string, unknown> });
      continue;
    }
    const labels = new Set<string>();
    const identities = new Set<string>();
    const allVerdicted = arms.every(arm => {
      const armId = strVal(arm.id);
      const armLabel = strVal(arm.arm_label);
      // identity = label when present; falls back to doc id. Arms sharing a doc id
      // (single-document arms-block shape from build_compute_sweep_groups) are
      // distinguished by arm_label, so duplicate doc ids are allowed when labels differ.
      const identity = armLabel || armId;
      if (!identity) return false;
      if (armLabel) {
        if (labels.has(armLabel)) return false;
        labels.add(armLabel);
      }
      if (identities.has(identity)) return false;
      identities.add(identity);
      if (strVal(arm.unavailable_cause)) return false;
      return arm.criterion_met === true || arm.criterion_met === false;
    });
    if (allVerdicted && identities.size >= 2) completed.push({ sweepId, arms });
  }

  if (completed.length === 0 && disposed.length === 0) return '';

  const lines: string[] = [];
  if (completed.length > 0) {
    lines.push('\n## Completed experiment sweeps — synthesis required');
    lines.push(
      'Every arm below is fully verdicted. For EACH sweep you MUST (in Section 3): ' +
      '(1) name the winning arm and state by how much it won, ' +
      '(2) state what the result implies, and ' +
      '(3) propose exactly ONE follow-on move: adopt the winning arm in an implementation direction, ' +
      'commission a deeper follow-on experiment to resolve remaining ambiguity, or close the hypothesis as settled. ' +
      'Do NOT leave a completed sweep without a move.',
    );
  }

  for (const { sweepId, arms } of completed) {
    const criterion = arms.find(a => isRecord(a.criterion))?.criterion as Record<string, unknown> | undefined;
    const direction = metricDirection(criterion?.operator);
    const metric = strVal(criterion?.metric, 'value');
    const sharedVerdicts = arms.find(a => isRecord(a.per_arm_verdicts))?.per_arm_verdicts as Record<string, Record<string, unknown>> | undefined;
    type MeasuredArm = { id: string; label: string; value: number };
    const armRows = arms.map(arm => {
      const label = strVal(arm.arm_label) || strVal(arm.id);
      const verdict = label && sharedVerdicts?.[label]
        ? sharedVerdicts[label]
        : pickOwnVerdict(arm) ?? undefined;
      const value = typeof verdict?.value === 'number' ? verdict.value : Number(verdict?.value);
      return {
        label,
        id: strVal(arm.id),
        met: verdict?.met === true || arm.criterion_met === true,
        value: Number.isFinite(value) ? value : null,
        summary: strVal(arm.summary),
      };
    });
    const measuredArms: MeasuredArm[] = armRows.flatMap(arm => (
      arm.value === null ? [] : [{ id: arm.id, label: arm.label, value: arm.value }]
    )).sort((a, b) => {
      const aValue = a.value;
      const bValue = b.value;
      if (direction === 'lower') {
        return aValue !== bValue ? aValue - bValue : a.label.localeCompare(b.label);
      }
      return aValue !== bValue ? bValue - aValue : a.label.localeCompare(b.label);
    });
    const metArms: MeasuredArm[] = armRows.flatMap(arm => (
      arm.met && arm.value !== null ? [{ id: arm.id, label: arm.label, value: arm.value }] : []
    ));
    const metricWinner = bestArmByMetric(metArms, direction);
    const winnerKey = metricWinner ? (metricWinner.label || metricWinner.id) : '';
    const runnerUp = metricWinner && measuredArms.length > 1
      ? measuredArms.find(arm => (arm.label || arm.id) !== winnerKey) ?? null
      : measuredArms[1] ?? null;
    const metLabels = armRows.filter(arm => arm.met).map(arm => arm.label);
    const winnerLabel = metricWinner
      ? metricWinner.label
      : metLabels.length > 0
        ? metLabels.join(', ')
        : 'none (all NOT MET)';

    lines.push(`\n### Sweep ${sweepId}`);
    lines.push(
      metricWinner
        ? `Winner: **${winnerLabel}**${renderMetricTail(metric, metricWinner, runnerUp, direction)}`
        : `Winner: **${winnerLabel}**`,
    );
    if (metricWinner && runnerUp) {
      const margin = direction === 'lower'
        ? runnerUp.value - metricWinner.value
        : metricWinner.value - runnerUp.value;
      lines.push(`margin: +${formatMetricNumber(margin)} ${metric} over ${runnerUp.label}`);
    } else if (!metricWinner) {
      if (measuredArms.length > 0) {
        const measuredFallback = measuredArms[0];
        lines.push(
          `Best measured arm: **${measuredFallback.label}**${renderMetricTail(metric, measuredFallback, runnerUp, direction)}`,
        );
      } else {
        lines.push('Best measured arm: none (no numeric metric values available)');
      }
    }

    for (const arm of armRows) {
      const armLabel = arm.label || arm.id;
      const verdict = arm.met ? 'MET' : 'NOT MET';
      const metricTail = arm.value === null ? '' : ` (${strVal(criterion?.metric, 'value')}=${formatMetricNumber(arm.value)})`;
      const summaryTail = arm.summary ? ` — ${arm.summary.slice(0, 200)}` : '';
      lines.push(`- **${armLabel}** [${verdict}] [${arm.id}]${metricTail}${summaryTail}`);
    }

    lines.push(
      `Use \`./fetch knowledge <id>\` for each arm's full analysis before concluding. ` +
      `Winning: ${armRows.filter(a => a.met).length}/${armRows.length} arm(s) MET, ${armRows.filter(a => !a.met).length}/${armRows.length} NOT MET.`,
    );
  }

  if (disposed.length > 0) {
    lines.push('\n## Sweep dispositions in flight — do not re-synthesize');
    lines.push('These sweeps are fully verdicted but already have a disposition task in progress. Skip re-synthesis and wait for the follow-on to complete.');
    for (const { sweepId, disposition } of disposed) {
      const status = typeof disposition.status === 'string' ? disposition.status : 'unknown';
      const taskId = typeof disposition.task_id === 'string' ? disposition.task_id : 'n/a';
      lines.push(`- **${sweepId}**: disposition task \`${taskId}\` (${status})`);
    }
  }

  return lines.join('\n');
}

export function buildOutcomeSection(outcomeEval: Record<string, unknown> | undefined): string {
  if (!outcomeEval || typeof outcomeEval !== 'object') return '';
  const window = outcomeEval.window as Record<string, unknown> | undefined;
  const overall = outcomeEval.overall as Record<string, unknown> | undefined;
  const systems = Array.isArray(outcomeEval.systems) ? outcomeEval.systems as Array<Record<string, unknown>> : [];
  const dirOutcomes = Array.isArray(outcomeEval.directions) ? outcomeEval.directions as Array<Record<string, unknown>> : [];

  const lines: string[] = [];
  if (window) {
    lines.push(`# Outcome evaluation (since vision rev ${fmtScalar(window.vision_revision, '0')})`);
    lines.push(`Window: ${strVal(window.from, '?')} to ${strVal(window.to, '?')}`);
  }
  if (overall) {
    const sr = typeof overall.success_rate === 'number' ? Math.round(overall.success_rate * 100) : '?';
    const productRatioStr = typeof overall.product_ratio === 'number' ? `, product ratio: ${Math.round((overall.product_ratio) * 100)}%` : '';
    lines.push(`Overall: ${fmtScalar(overall.cycles, '0')} cycles, ${fmtScalar(overall.merged, '0')} merged, ${fmtScalar(overall.failed, '0')} failed (${sr}% success rate, trend: ${strVal(overall.trend, 'stable')}${productRatioStr})`);
  }

  if (systems.length > 0) {
    lines.push('');
    lines.push('## System progress');
    lines.push('Each vision system compared against actual direction outcomes:');
    for (const sys of systems) {
      const gap = (sys.directions_count as number) === 0 ? ' [NO DIRECTIONS MAPPED]' : '';
      lines.push(`- ${strVal(sys.name)} (${strVal(sys.id)}): planned=${strVal(sys.planned_status)}, ${fmtScalar(sys.directions_count, '0')} directions, ${fmtScalar(sys.merged, '0')} merged, ${fmtScalar(sys.failed, '0')} failed${gap}`);
    }
  }

  if (dirOutcomes.length > 0) {
    lines.push('');
    lines.push('## Direction outcomes');
    lines.push('What each direction actually produced. Use confirmed findings to judge real impact:');
    for (const d of dirOutcomes) {
      const sysTag = d.system_id ? ` (system: ${strVal(d.system_id)})` : '';
      lines.push(`\n### ${strVal(d.text)} (${strVal(d.id)})${sysTag} [${strVal(d.status)}]`);
      lines.push(`Stats: ${fmtScalar(d.cycles, '0')} cycles, ${fmtScalar(d.merged, '0')} merged, ${fmtScalar(d.failed, '0')} failed`);
      const typeDist = d.type_distribution as Record<string, number> | undefined;
      if (typeDist && Object.keys(typeDist).length > 0) {
        const typeParts = Object.entries(typeDist).sort((a, b) => b[1] - a[1]).map(([t, c]) => `${c} ${t}`);
        const productRatio = typeof d.product_ratio === 'number' ? ` (${Math.round((d.product_ratio) * 100)}% product)` : '';
        lines.push(`Type mix: ${typeParts.join(', ')}${productRatio}`);
      }
      const lifetime = d.lifetime as Record<string, number> | undefined;
      if (lifetime) {
        lines.push(`Lifetime: ${lifetime.cycles || 0} total cycles, ${lifetime.merged || 0} total merged`);
      }
      const confirmedFindingCount = typeof d.confirmed_finding_count === 'number' ? d.confirmed_finding_count : null;
      const confirmedFindingTrend = typeof d.confirmed_finding_trend === 'string' ? d.confirmed_finding_trend : null;
      if (confirmedFindingCount !== null) {
        let countLine = `Cumulative confirmed findings: ${confirmedFindingCount}`;
        if (confirmedFindingTrend) countLine += ` (${confirmedFindingTrend})`;
        lines.push(countLine);
      }
      const confirmed = Array.isArray(d.confirmed) ? d.confirmed as string[] : [];
      if (confirmed.length > 0) lines.push(`Confirmed (shipped): ${confirmed.join('; ')}`);
      const proposed = Array.isArray(d.proposed) ? d.proposed as string[] : [];
      if (proposed.length > 0) lines.push(`Proposed (in-flight): ${proposed.join('; ')}`);
      const failedApproaches = Array.isArray(d.failed_approaches) ? d.failed_approaches as string[] : [];
      if (failedApproaches.length > 0) lines.push(`Failed approaches: ${failedApproaches.slice(0, 5).join('; ')}`);
      if (d.outcome_scaffolding_flagged === true) {
        lines.push('⚠ OUTCOME-SCAFFOLDING-FLAGGED: direction keeps shipping merges but confirmed findings have not grown — assess whether this is delivering real outcomes or churn scaffolding.');
        const ev = d.outcome_scaffolding_evidence as Record<string, unknown> | undefined;
        if (ev && typeof ev === 'object') {
          lines.push(`  Scaffolding evidence: ${fmtScalar(ev.total_cycles)} cycles, ${fmtScalar(ev.lifetime_merged)} lifetime merged (rate=${fmtScalar(ev.lifetime_merge_rate)}), stagnant for ${fmtScalar(ev.progress_stagnant_cycles)} cycles`);
        }
      }
    }
  }

  if (lines.length === 0) return '';
  return '\n' + lines.join('\n') + '\n';
}

export function buildClockAndRecentWorkSection(recentInterventions: Record<string, unknown> | undefined): { clockHeader: string; recentWorkSection: string } {
  if (!recentInterventions || typeof recentInterventions !== 'object') return { clockHeader: '', recentWorkSection: '' };

  const merges = Array.isArray(recentInterventions.merges)
    ? recentInterventions.merges as Array<Record<string, unknown>>
    : [];
  const rollingRates = recentInterventions.rolling_rates as Record<string, Record<string, unknown> | undefined> | undefined;
  const perDirectionDelta = Array.isArray(recentInterventions.per_direction_delta)
    ? recentInterventions.per_direction_delta as Array<Record<string, unknown>>
    : [];
  const hoursSinceLast = typeof recentInterventions.hours_since_last_strategic === 'number'
    ? recentInterventions.hours_since_last_strategic
    : null;

  let clockHeader = '';
  if (hoursSinceLast !== null && rollingRates) {
    const sinceLast = rollingRates.since_last_strategic || {};
    const mergedSince = typeof sinceLast.merged === 'number' ? sinceLast.merged : 0;
    const finishedSince = typeof sinceLast.finished === 'number' ? sinceLast.finished
      : typeof sinceLast.created === 'number' ? sinceLast.created : 0;
    const spawnedSince = typeof sinceLast.spawned === 'number' ? sinceLast.spawned : null;
    const rateSince = typeof sinceLast.merge_rate === 'number' ? `${Math.round((sinceLast.merge_rate) * 100)}%` : 'n/a';
    const spawnNote = spawnedSince != null && spawnedSince !== finishedSince
      ? ` (${spawnedSince} spawned, some still in flight)`
      : '';
    clockHeader = `\n# Clock\n`
      + `${hoursSinceLast}h since your last strategic cycle. In that window: `
      + `${mergedSince} of ${finishedSince} finished tasks merged (${rateSince})${spawnNote}.\n`
      + `If that's a low-value ${hoursSinceLast}h relative to the ambition you set last time, that's information — ask why before doubling down on the same plays.\n`;
  }

  const activeByScope = recentInterventions.active_by_scope as Record<string, number> | undefined;
  if (activeByScope) {
    const parts: string[] = [];
    for (const s of ['small', 'medium', 'large', 'unknown']) {
      const n = activeByScope[s];
      if (typeof n === 'number' && n > 0) parts.push(`${s}=${n}`);
    }
    if (parts.length > 0) {
      clockHeader += `Currently in flight: ${parts.join(', ')} (not in the merge-rate numbers above — those only count finished tasks).\n`;
    }
  }

  let recentWorkSection = '';
  if (merges.length > 0 || rollingRates || perDirectionDelta.length > 0) {
    const lines: string[] = ['\n# Recent Work & Rolling Outcomes'];
    lines.push('IMPORTANT — read this carefully before acting:');
    lines.push('- Rate numerator/denominator semantics: merge_rate = merged/finished, where "finished" means the task reached a terminal state inside the window. Tasks spawned mid-window that have not finished yet do NOT count in the denominator — so short-window rates are not biased downward by in-flight slower-scope tasks.');
    lines.push('- "spawned" = tasks created in the window (throughput signal). "finished" = reached terminal state in the window (quality denominator). Divergence between the two is normal for slow scopes.');
    lines.push('- Correlation is NOT causation. Rate swings can reflect task difficulty shifts, account health, rate limits, CI outages, or small-sample noise.');
    lines.push('- Weekly buckets were replaced with rolling windows (last_24h / last_3d / last_7d / since_last_strategic) because weekly is too coarse for this pipeline\'s velocity.');
    lines.push('- Only act on patterns visible across 2+ of the overlapping windows. A spike in last_24h that does not show in last_3d is probably noise.');
    lines.push('- Prefer "investigate whether X helps" over "revert X".');

    if (rollingRates) {
      lines.push('\nRolling merge-rate windows (merged / finished):');
      const order = ['since_last_strategic', 'last_24h', 'last_3d', 'last_7d'];
      for (const w of order) {
        const r = rollingRates[w];
        if (!r) continue;
        const finished = typeof r.finished === 'number' ? r.finished
          : typeof r.created === 'number' ? r.created : 0;
        const merged = typeof r.merged === 'number' ? r.merged : 0;
        const spawned = typeof r.spawned === 'number' ? r.spawned : null;
        const rate = typeof r.merge_rate === 'number' ? `${Math.round((r.merge_rate) * 100)}%` : 'n/a';
        const spawnTail = spawned != null && spawned !== finished ? `, ${spawned} spawned` : '';
        lines.push(`  ${w}: ${merged}/${finished} merged (${rate}${spawnTail})`);
      }
    }

    if (perDirectionDelta.length > 0) {
      lines.push('\nPer-direction outcomes in the rolling windows:');
      for (const d of perDirectionDelta) {
        const dirText = strVal(d.direction_text, '').slice(0, 100);
        lines.push(`\n- ${dirText} (${strVal(d.direction_id)})`);
        const sinceLast = d.since_last_strategic as Record<string, unknown> | undefined;
        const last24h = d.last_24h as Record<string, unknown> | undefined;
        const last3d = d.last_3d as Record<string, unknown> | undefined;
        const fmt = (r: Record<string, unknown> | undefined): string => {
          if (!r) return '0/0';
          const c = typeof r.created === 'number' ? r.created : 0;
          const m = typeof r.merged === 'number' ? r.merged : 0;
          return `${m}/${c}`;
        };
        const parts: string[] = [];
        if (sinceLast) parts.push(`since_last_strategic: ${fmt(sinceLast)}`);
        parts.push(`last_24h: ${fmt(last24h)}`, `last_3d: ${fmt(last3d)}`);
        lines.push(`  ${parts.join(', ')}`);
        const recentTitles = Array.isArray(d.recent_merged_titles) ? d.recent_merged_titles : [];
        if (recentTitles.length > 0) {
          const formatted = recentTitles.map((t: unknown) => {
            if (typeof t === 'string') return `"${t}"`;
            const entry = t as Record<string, unknown>;
            const titleStr = strVal(entry.title ?? '', '?');
            const autoSuffix = entry.on_automain === false ? ' [NOT on automain]' : '';
            return `"${titleStr}"${autoSuffix}`;
          });
          lines.push(`  Recently shipped: ${formatted.join('; ')}`);
        }
      }
    }

    if (merges.length > 0) {
      lines.push('\nRecent merges (newest first, with body when informative):');
      for (const m of merges) {
        const title = strVal(m.pr_title, '?').slice(0, 120);
        const pr = typeof m.pr_number === 'number' || typeof m.pr_number === 'string' ? `#${m.pr_number}` : '';
        const hoursAgo = typeof m.hours_ago === 'number' ? `${m.hours_ago}h ago` : '?';
        const dirText = strVal(m.direction_text, '').slice(0, 80);
        const dirTag = dirText ? ` [direction: ${dirText}]` : ' [no direction]';
        const autoTag = m.on_automain === false ? ' [staged, NOT on automain]' : '';
        lines.push(`  - ${title} ${pr} (${hoursAgo})${dirTag}${autoTag}`);
        const body = strVal(m.body, '').trim();
        if (body) {
          const indented = body.split('\n').map(l => `    ${l}`).join('\n');
          lines.push(indented);
        }
      }
    }
    recentWorkSection = lines.join('\n') + '\n';
  }

  return { clockHeader, recentWorkSection };
}

export function buildConversationThreadSection(conversationThread: Array<Record<string, unknown>>): string {
  if (conversationThread.length === 0) return '';
  return '\n## Conversation History\n' +
    conversationThread.map(m => {
      const age = calcMessageAge(m);
      const status = strVal(m.status);
      const from = strVal(m.from);
      const tag = status === 'resolved' ? `${from}, ${age}, RESOLVED` : `${from}, ${age}, OPEN`;
      let line = `[${tag}] [${strVal(m.type)}] ${strVal(m.content).slice(0, 5000)}`;
      if (status === 'resolved' && m.resolution) {
        line += `\n  -> ${strVal(m.resolution).slice(0, 500)}`;
      }
      if (status === 'open') {
        line += ` (id: ${strVal(m.id)})`;
      }
      return line;
    }).join('\n') + '\n';
}

export function buildQuestionSection(openMessages: Array<Record<string, unknown>>): string {
  const questionMessages = openMessages.filter(m => strVal(m.type) === 'QUESTION');
  if (questionMessages.length === 0) return '';
  const questionLines = questionMessages.map(m => {
    const age = calcMessageAge(m);
    return `- (id: ${strVal(m.id)}, from: ${strVal(m.from)}, open ${age}) ${strVal(m.content).slice(0, 5000)}`;
  }).join('\n');
  return `\n## Operator Questions Awaiting Answers\nThese QUESTION messages are direct operator questions. Answer each unanswered one by resolving it with a substantive answer via \`./messages resolve <id> '<answer>'\`:\n${questionLines}\n`;
}

export function buildFeedbackSection(openMessages: Array<Record<string, unknown>>): string {
  const feedbackMessages = openMessages.filter(m => strVal(m.type) === 'FEEDBACK');
  if (feedbackMessages.length === 0) return '';
  const feedbackLines = feedbackMessages.map(m => {
    const age = calcMessageAge(m);
    return `- (id: ${strVal(m.id)}, from: ${strVal(m.from)}, open ${age}) ${strVal(m.content).slice(0, 5000)}`;
  }).join('\n');
  return `\n## User Feature Requests\nThese FEEDBACK messages are direct user requests. Strongly consider creating a direction for each unaddressed request:\n${feedbackLines}\n`;
}

export function buildOpenMessagesText(openMessages: Array<Record<string, unknown>>): string {
  if (openMessages.length === 0) return '';
  return openMessages.map(m => {
    const age = calcMessageAge(m);
    const line = `- [${strVal(m.type)}] (id: ${strVal(m.id)}, from: ${strVal(m.from)}, open ${age}) ${strVal(m.content).slice(0, 5000)}`;
    // The backend stamps a reproduce-on-HEAD citation signal onto the message
    // from its cited code's grounding on the integration branch. Surface it
    // inline so the strategist sees the report's grounding state instead of
    // trusting it blindly. Two cases:
    //  - citation_state === 'absent': cited code is unambiguously gone on HEAD.
    //    The report is fully stale and DISCOUNTED — do not route a fix until it
    //    is independently re-confirmed to still reproduce on HEAD.
    //  - any non-absent state that still carries broken/ungrounded refs: the
    //    report is only PARTIALLY grounded. The unconfirmed refs stay VISIBLE so
    //    the strategist can inspect them, but the report is NOT discounted —
    //    fix/investigation routing remains allowed.
    const broken = Array.isArray(m.broken_refs) ? (m.broken_refs as unknown[]).map(r => strVal(r)) : [];
    const ungrounded = Array.isArray(m.ungrounded_refs) ? (m.ungrounded_refs as unknown[]).map(r => strVal(r)) : [];
    const parts: string[] = [];
    if (broken.length > 0) parts.push(`files ${broken.join(', ')}`);
    if (ungrounded.length > 0) parts.push(`symbols ${ungrounded.join(', ')}`);
    const refs = parts.length > 0 ? `: ${parts.join('; ')}` : '';
    if (strVal(m.citation_state) === 'absent') {
      return `${line}\n  ⚠ STALE — cited code gone on HEAD${refs}. DISCOUNTED/UNPROVEN: do NOT route into a fix/investigation task unless the defect is independently re-confirmed to still reproduce on HEAD.`;
    }
    if (parts.length > 0) {
      return `${line}\n  ⚠ PARTIALLY GROUNDED — some cited refs unconfirmed on HEAD${refs}. The report is NOT stale; fix/investigation routing is allowed — verify the unconfirmed refs during triage.`;
    }
    return line;
  }).join('\n');
}

/**
 * Format the recently-ANSWERED operator conversation tail (backend
 * `recent_handled_conversation`) into a compact, read-only transcript.
 *
 * Every message here is already status handled/resolved — so this is explicitly
 * NOT an inbox and must NOT be acted on again. It exists only so a correction
 * the operator just made, that the strategist forgot to capture into the
 * durable decisions register (the OPERATOR-DECISION marker), survives a restart
 * or compaction — otherwise the message vanishes from the open feed the instant
 * it is marked handled and the strategist re-proposes what the operator already
 * settled. Returns '' when there is nothing to show so the caller omits the
 * section entirely.
 *
 * Mirrors the field access of `buildOpenMessagesText` (type/id/from/content)
 * but drops the open-inbox age + citation decorations, which do not apply to
 * already-answered messages.
 */
export function formatRecentHandledConversation(
  messages: Array<Record<string, unknown>>,
): string {
  if (!Array.isArray(messages) || messages.length === 0) return '';
  return messages
    .map((m) => {
      const type = strVal(m.type) || 'message';
      const from = strVal(m.from) || 'unknown';
      const id = strVal(m.id);
      const content = strVal(m.content).slice(0, 5000);
      const idPart = id ? `id: ${id}, ` : '';
      return `- [${type}] (${idPart}from: ${from}, answered) ${content}`;
    })
    .join('\n');
}

export function buildDirSummaryText(
  directionsSummary: Array<Record<string, unknown>>,
  authoritativeActiveCount?: number,
): string {
  if (directionsSummary.length === 0) {
    // Distinguish "genuinely no directions" from "detail list dropped for
    // budget while directions are actually active". The authoritative count
    // (shipped trim-proof by the backend) is the ground truth — never collapse
    // to "No directions exist yet." when it says directions are active.
    if (typeof authoritativeActiveCount === 'number' && authoritativeActiveCount > 0) {
      return `_${authoritativeActiveCount} active direction(s) exist but the per-direction detail list was not shipped this cycle (trimmed/dropped under the byte budget). Fetch them with \`./fetch direction <id>\` before reasoning about specific directions._`;
    }
    return 'No directions exist yet.';
  }
  const body = directionsSummary.map((d, i) => {
    const parts = [`${fmtScalar(d.total_cycles, '0')} cycles`];
    if (d.prs_merged || d.reachable_prs_merged) {
      const prs_merged = Number(d.prs_merged ?? 0);
      const verified = d.verified_prs_merged !== undefined && d.verified_prs_merged !== null
        ? Number(d.verified_prs_merged)
        : 0;
      // null = field absent = legacy-unknown (no reachability annotation).
      // 0 = field present but zero = known-zero (explicit alarm annotation).
      // >0 = confirmed reachable (reachable-vs-badge split).
      const reachable: number | null = d.reachable_prs_merged !== undefined && d.reachable_prs_merged !== null
        ? Number(d.reachable_prs_merged)
        : null;

      // An ancestry check is the only proof that a merge actually landed on the
      // integration branch. Keep raw task/PR badges visible for diagnostics,
      // but never present them as the primary shipped-work count.
      let mergedText = reachable !== null
        ? `${reachable} landed`
        : `${prs_merged} recorded merge badge${prs_merged === 1 ? '' : 's'} (ancestry unknown)`;
      const annotations: string[] = [];

      // Add verified annotation if present and less than prs_merged
      if (verified > 0 && verified < prs_merged) {
        annotations.push(`${verified} verified`);
      }

      // Add reachable-vs-badge split when the field is present (including known-zero).
      // null = field absent (legacy-unknown) → omit entirely so legacy directions
      // are not annotated with misleading zero counts.
      // "badge-only" here means merged but NOT ancestry-reachable (prs_merged - reachable).
      // The backend also defines badge_only_prs_merged = prs_merged - verified_prs_merged
      // (a different axis); the agent does not consume that field for this text.
      if (reachable !== null) {
        const badgeOnly = Math.max(0, prs_merged - reachable);
        if (badgeOnly > 0) {
          annotations.push(`${prs_merged} recorded merge badges; ${badgeOnly} ancestry-unconfirmed`);
        }
      }

      if (annotations.length > 0) {
        mergedText += ` (${annotations.join('; ')})`;
      }

      parts.push(mergedText);
    }
    const proven = Number(d.proven_prs_merged ?? 0);
    const consumed = Number(d.consumed_prs_merged ?? 0);
    if (proven > 0 || consumed > 0) {
      const consumedSuffix = consumed > 0 ? ` (${consumed} consumed downstream)` : '';
      parts.push(`${proven} proven${consumedSuffix}`);
    }
    if (d.tasks_failed) {
      const infraFailed = (d.tasks_failed_infra as number) || 0;
      parts.push(infraFailed > 0 ? `${fmtScalar(d.tasks_failed)} failed (${infraFailed} infra)` : `${fmtScalar(d.tasks_failed)} failed`);
    }
    const satNote = d.possibly_saturated ? ' [SATURATED]' : '';
    const catNote = d.category ? ` [${strVal(d.category)}]` : '';
    const sysNote = d.system_id ? ` (system: ${strVal(d.system_id)})` : '';
    const dStatus = strVal(d.status, 'active');
    let statusNote = '';
    if (dStatus !== 'active') {
      if (dStatus === 'paused') {
        const tag = d.auto_paused === true ? 'AUTO_PAUSED' : 'USER_PAUSED';
        const reason = strVal(d.paused_reason).slice(0, 80);
        statusNote = reason ? ` [${tag}: ${reason}]` : ` [${tag}]`;
      } else {
        statusNote = ` [${dStatus.toUpperCase()}]`;
      }
    }

    let ctxSection = '';
    if (typeof d.research_context === 'string' && (d.research_context).length > 0) {
      const sections = parseResearchContext(d.research_context);
      const ctxParts: string[] = [];
      if (sections.established.length > 0) {
        const confirmed = sections.established.filter(s => s.includes('(confirmed)'));
        const proposed = sections.established.filter(s => s.includes('(proposed)'));
        ctxParts.push(`   ${confirmed.length} confirmed findings, ${proposed.length} proposed`);
      }
      if (sections.failed.length > 0) {
        ctxParts.push(`   Failed approaches: ${sections.failed.slice(0, 3).join('; ')}`);
      }
      if (sections.open.length > 0) {
        ctxParts.push(`   Open questions: ${sections.open.slice(0, 3).join('; ')}`);
      }
      if (sections.eviction) {
        ctxParts.push(`   ⚠ Context trimmed: ${sections.eviction}. Earlier findings were evicted — check git history if you suspect overlap with prior work.`);
      }
      if (ctxParts.length > 0) ctxSection = '\n' + ctxParts.join('\n');
    }

    let findingsSection = '';
    const findings = Array.isArray(d.recent_findings) ? d.recent_findings as string[] : [];
    if (findings.length > 0) {
      findingsSection = `\n   Recent work: ${findings.slice(0, 5).join('; ')}`;
    }

    let failuresSection = '';
    const failures = Array.isArray(d.recent_failures) ? d.recent_failures as Array<Record<string, string>> : [];
    if (failures.length > 0) {
      const failLines = failures.slice(0, 3).map(f => {
        const cat = f.category ? ` [${f.category}]` : '';
        return `${f.title || '?'}${cat}: ${f.reason || 'unknown'}`;
      });
      failuresSection = `\n   Recent failures: ${failLines.join('; ')}`;
    }

    let spawnGateSection = '';
    const sgm = d.spawn_gate_metrics as Record<string, unknown> | undefined;
    if (sgm && typeof sgm === 'object') {
      const challenged = Number(sgm.challenged ?? sgm.challenged_count ?? 0);
      const refuted = Number(sgm.refuted ?? sgm.refuted_count ?? 0);
      const survived = Number(sgm.survived ?? sgm.survived_count ?? 0);
      // Decision-quality breakdown: explicit challenger pass vs fail-open/no-verdict
      // pass (subsets of survived) and unresolved/no-verdict (subset of refuted).
      const explicitPass = Number(sgm.explicit_pass_count ?? 0);
      const failOpen = Number(sgm.fail_open_count ?? 0);
      const unresolved = Number(sgm.unresolved_count ?? 0);
      const manifestExcluded = Number(sgm.manifest_excluded_fail_closed_count ?? 0);
      const cmr = sgm.challenged_merge_rate != null ? `${Math.round(Number(sgm.challenged_merge_rate) * 100)}%` : 'n/a';
      const umr = sgm.unchallenged_merge_rate != null ? `${Math.round(Number(sgm.unchallenged_merge_rate) * 100)}%` : 'n/a';
      // Preserve the merge-rate sample sizes so each percentage travels with its
      // merged/spawned denominator (a rate like 85% is meaningless without its n).
      const cDenom = `${Number(sgm.challenged_merged ?? 0)}/${Number(sgm.challenged_spawned ?? 0)}`;
      const uDenom = `${Number(sgm.unchallenged_merged ?? 0)}/${Number(sgm.unchallenged_spawned ?? 0)}`;
      const reasons = Array.isArray(sgm.refuted_reasons)
        ? (sgm.refuted_reasons as unknown[]).map(r => String(r)).filter(Boolean)
        : [];
      const reasonsPart = reasons.length > 0
        ? `; refuted/unresolved reasons: ${reasons.slice(0, 3).join('; ')}${reasons.length > 3 ? ` (+${reasons.length - 3} more)` : ''}`
        : '';
      const excludedSamples = Array.isArray(sgm.manifest_excluded_fail_closed_samples)
        ? (sgm.manifest_excluded_fail_closed_samples as Array<Record<string, unknown>>)
          .map(sample => {
            if (typeof sample.title === 'string') return sample.title;
            if (typeof sample.reason === 'string') return sample.reason;
            if (typeof sample.hash === 'string') return sample.hash;
            return '';
          })
          .filter(Boolean)
        : [];
      const excludedPart = manifestExcluded > 0
        ? ` · ${manifestExcluded} manifest-excluded fail-closed${excludedSamples.length > 0 ? ` (${excludedSamples.slice(0, 3).join('; ')}${excludedSamples.length > 3 ? ` +${excludedSamples.length - 3} more` : ''})` : ''}`
        : '';
      spawnGateSection = `\n   Research spawn-gate: ${challenged} challenged → ${survived} survived (${explicitPass} explicit-pass, ${failOpen} fail-open) · ${refuted} refuted (${unresolved} unresolved)${excludedPart} — merge rate: ${cmr} challenged (${cDenom}) vs ${umr} unchallenged (${uDenom})${reasonsPart}`;
    }

    const scaffoldingNote = d.outcome_scaffolding_flagged === true ? ' [SCAFFOLDING]' : '';
    const churnRetireNote = d.churn_retirement_recommended === true ? ' [CHURN-RETIRE-RECOMMENDED]' : '';

    let churnRetireSection = '';
    if (d.churn_retirement_recommended === true) {
      const ev = d.churn_retirement_evidence as Record<string, unknown> | undefined;
      const detail = ev && typeof ev === 'object'
        ? ` (${fmtScalar(ev.churn_family_pause_count)}x churn/unproductive/saturated paused, merge_rate=${fmtScalar(ev.merge_rate, 'n/a')})`
        : '';
      churnRetireSection = `\n   ⚠ CHURN-RETIREMENT-RECOMMENDED${detail}: this direction has repeatedly churn-paused with no escape and now stays PAUSED pending your explicit decision. It will NOT auto-retire — either retire it via the retirement path or give it a concrete new approach.`;
    }

    // Cost-efficiency retirement consideration. The backend has raised this
    // signal for a long time but never delivered it anywhere a decision could be
    // made, so cost-outlier directions kept being selected forever. Render it
    // with its reason and the decision that is owed.
    const costRetireNote = d.retirement_consideration_needed === true ? ' [COST-RETIRE-CONSIDER]' : '';

    let costRetireSection = '';
    if (d.retirement_consideration_needed === true) {
      const ev = d.retirement_consideration_evidence as Record<string, unknown> | undefined;
      const detail = ev && typeof ev === 'object'
        ? ` (cost/merge $${fmtScalar(ev.direction_cost_per_merge_usd, 'n/a')} vs $${fmtScalar(ev.threshold_usd, 'n/a')} threshold over ${fmtScalar(ev.window_cycles, 'n/a')} cycles)`
        : '';
      const reason = strVal(d.retirement_consideration_reason, '');
      const since = strVal(d.retirement_consideration_at, '');
      const sincePart = since ? ` Raised ${since} and unanswered since.` : '';
      costRetireSection = `\n   ⚠ COST-RETIREMENT-CONSIDERATION${detail}: ${reason || 'this direction is a cost-per-merge outlier for this project.'}${sincePart} A keep-or-retire decision is owed. It stays ACTIVE and will keep consuming cycles until you decide — a cost breach alone is NOT proof of low value, so either retire it via the retirement path, narrow its scope, or state why the spend is justified.`;
    }

    const groundingText = formatRetiredDirectionGrounding(d.retired_direction_grounding);
    const groundingSection = groundingText ? `\n   ${groundingText}` : '';

    return `${i + 1}. [${strVal(d.priority, 'medium')}]${catNote} ${strVal(d.text)} (id: ${strVal(d.id)})${statusNote}${satNote}${sysNote}${scaffoldingNote}${churnRetireNote}${costRetireNote}\n   Stats: ${parts.join(', ')}${ctxSection}${findingsSection}${failuresSection}${spawnGateSection}${churnRetireSection}${costRetireSection}${groundingSection}`;
  }).join('\n\n');
  // When the shipped detail list is shorter than the authoritative active count,
  // render an honest "showing M of N" header instead of letting the reader infer
  // the trimmed list is the full slate.
  const shippedActive = directionsSummary.filter(d => strVal(d.status, 'active') === 'active').length;
  if (typeof authoritativeActiveCount === 'number' && shippedActive < authoritativeActiveCount) {
    const hidden = authoritativeActiveCount - shippedActive;
    return `_Showing ${shippedActive} of ${authoritativeActiveCount} active directions — ${hidden} trimmed for budget; fetch the rest with \`./fetch direction <id>\`._\n\n${body}`;
  }
  return body;
}

/**
 * Render a single consolidated `## Direction Portfolio` section that combines
 * the health header, per-direction listing, and portfolio statistics (category
 * mix, system coverage, concentration, scope) into one block. Replaces the
 * prior three separate sections (dir_summary + dir_health + portfolio_composition),
 * eliminating redundant section headers and interstitial prose.
 */
export function buildDirectionPortfolioSection(
  directionsSummary: Array<Record<string, unknown>>,
  maxActiveDirections: number,
  systems: Array<Record<string, unknown>>,
  activeByScope: Record<string, number> | undefined,
  authoritativeActiveCount?: number,
  authoritativePausedCount?: number,
  authoritativeSystemCounts?: Record<string, number>,
): string {
  // ── Health header (consolidated from buildDirHealthSummary) ──
  const shippedActiveDirections = directionsSummary.filter(d => strVal(d.status, 'active') === 'active');
  const shippedActiveCount = shippedActiveDirections.length;
  const shippedPaused = directionsSummary.filter(d => strVal(d.status) === 'paused');
  const activeCount = authoritativeActiveCount ?? shippedActiveCount;
  const pausedCount = authoritativePausedCount ?? shippedPaused.length;
  const autoPausedCount = shippedPaused.filter(d => d.auto_paused === true).length;
  const userPausedCount = Math.max(0, pausedCount - autoPausedCount);
  const saturatedCount = shippedActiveDirections.filter(d => d.possibly_saturated === true).length;
  const productCount = shippedActiveDirections.filter(d => strVal(d.category) === 'product').length;
  const availableSlots = Math.max(0, maxActiveDirections - activeCount);

  const pausedNote = pausedCount > 0
    ? ` Paused: ${pausedCount} (${autoPausedCount} auto, ${userPausedCount} user) — listed below but not consuming active slots.`
    : '';
  const trimNote = (authoritativeActiveCount !== undefined && shippedActiveCount < activeCount)
    ? ` Showing ${shippedActiveCount} of ${activeCount} active directions below — ${activeCount - shippedActiveCount} trimmed for budget; saturation/product counts reflect only the shipped subset. Fetch the rest with \`./fetch direction <id>\`.`
    : '';

  const healthHeader = [
    `Active: ${activeCount}/${maxActiveDirections} slots used (${availableSlots} available). Saturated: ${saturatedCount}/${activeCount}. Product directions: ${productCount}.${pausedNote}${trimNote}`,
    'Portfolio counts, categories, saturation, and available slots are descriptive state, not targets or evidence that another direction is required.',
  ].join('\n');

  // ── Direction listing ──
  const dirList = buildDirSummaryText(directionsSummary, authoritativeActiveCount);

  // ── Portfolio stats ──
  const portfolio = buildPortfolioComposition(
    shippedActiveDirections,
    systems,
    activeByScope,
    activeCount,
    authoritativeSystemCounts,
  );

  return [
    '## Direction Portfolio',
    healthHeader,
    '',
    dirList,
    '',
    portfolio,
  ].join('\n');
}

/**
 * Render the backend-computed CI compliance snapshot for the current integration
 * branch HEAD. Surfaces module-size breaches and branch-literal offenders so the
 * strategist sees compliance drift immediately instead of discovering it at
 * qualification time. Returns '' when the payload field is absent/null.
 */
export function buildCiComplianceSection(ciCompliance: unknown): string {
  if (!ciCompliance || typeof ciCompliance !== 'object') return '';
  const c = ciCompliance as Record<string, unknown>;
  // compact_ci_compliance shape: head_sha, compliant (bool), violations, branch_literal_violations
  if (c.compliant === true) return '';

  const lines: string[] = ['\n## CI Compliance (automain HEAD)'];
  const headSha = strVal(c.head_sha);
  lines.push(`Compliant: **no**${headSha ? `  (SHA: ${headSha.slice(0, 8)})` : ''}`);

  const violations = Array.isArray(c.violations)
    ? c.violations as Array<Record<string, unknown>>
    : [];
  if (violations.length > 0) {
    lines.push('\n### Module-size violations (run `python3 backend/testing/check_size_limits.py --fix` to bump caps):');
    for (const v of violations) {
      const section = strVal(v.section, '?');
      const rel = strVal(v.rel, '?');
      const count = fmtScalar(v.count);
      const cap = v.cap !== null && v.cap !== undefined ? fmtScalar(v.cap) : 'uncapped';
      lines.push(`  - [${section}] ${rel}: ${count} lines (limit: ${cap})`);
    }
  }

  const branchLiteralViolations = Array.isArray(c.branch_literal_violations)
    ? c.branch_literal_violations as Array<Record<string, unknown>>
    : [];
  if (branchLiteralViolations.length > 0) {
    lines.push('\n### Branch-literal violations (use `resolveBaseBranch()` instead):');
    for (const v of branchLiteralViolations) {
      const rel = strVal(v.rel, '?');
      const line = fmtScalar(v.line);
      const text = strVal(v.text, '');
      lines.push(`  - ${rel}:${line}: ${text}`);
    }
  }

  lines.push('\n_Compliance is checked against the live automain HEAD. Fix breaches before the next qualification attempt._');
  return lines.join('\n');
}

// Statuses that indicate a linked task has successfully completed its work.
const _DEBT_COMPLETED_STATUSES = new Set(['COMPLETED']);

export function buildCompletionDebtSection(debt: unknown): string {
  // Accept the new dict shape {items: [...], total_open: N} or the legacy array shape.
  let entries: Array<Record<string, unknown>>;
  let totalOpen: number;
  if (debt && typeof debt === 'object' && !Array.isArray(debt)) {
    const d = debt as Record<string, unknown>;
    entries = Array.isArray(d.items) ? d.items as Array<Record<string, unknown>> : [];
    totalOpen = typeof d.total_open === 'number' ? d.total_open : entries.length;
  } else if (Array.isArray(debt)) {
    entries = debt as Array<Record<string, unknown>>;
    totalOpen = entries.length;
  } else {
    return '';
  }
  if (totalOpen === 0 && entries.length === 0) return '';
  const lines: string[] = ['\n## Open Completion Debt'];
  const hiddenCount = Math.max(0, totalOpen - entries.length);
  const tailNote = hiddenCount > 0 ? ` (+${hiddenCount} more not shown)` : '';
  lines.push(`${totalOpen} open obligation(s)${tailNote} — unfinished work from merged features, knob activations, or retired directions:`);
  for (const e of entries) {
    const kind = strVal(e.kind, 'unknown');
    const title = strVal(e.title, '');
    const desc = strVal(e.description, '');
    // Always lead with the human title. Append description only when it adds
    // information beyond the title (e.g. no_ci_gate detail text). For
    // dormant_knob rows the backend sets description == title so nothing extra
    // is appended, avoiding the bare "off"/"observe" knob state showing up.
    const label = title || desc || '(no description)';
    const extraDesc = desc && desc !== label ? ` — ${desc}` : '';
    const ageDays = typeof e.age_days === 'number' ? ` (${e.age_days}d old)` : '';
    const id = strVal(e.id);
    const idTag = id ? ` \`${id}\`` : '';
    lines.push(`  - [${kind}]${idTag} ${label}${extraDesc}${ageDays}`);
    // Render linked task statuses so the strategist knows if follow-up work
    // is in flight, already completed (dispose-ready), or failed.
    const linkedTasks = Array.isArray(e.linked_tasks) ? e.linked_tasks as Array<Record<string, unknown>> : [];
    if (linkedTasks.length > 0) {
      const hasCompleted = linkedTasks.some(lt => _DEBT_COMPLETED_STATUSES.has(strVal(lt.status, '').toUpperCase()));
      const taskSummaries = linkedTasks.map(lt => {
        const tid = strVal(lt.task_id, '?');
        const shortId = tid.length > 8 ? tid.slice(0, 8) : tid;
        const status = strVal(lt.status, 'unknown');
        return `${shortId}: ${status}`;
      }).join(', ');
      const disposeNudge = hasCompleted ? ' ← work landed; consider dispose-completion-debt' : '';
      lines.push(`    Linked task(s): ${taskSummaries}${disposeNudge}`);
    }
  }
  lines.push('\n_Dispose an obligation once its work lands: `./strategic dispose-completion-debt \'{"debt_id":"<id>","disposition":"resolved","evidence":"<what proved it done>"}\'` (use `"dismissed"` with a reason if it no longer applies). When spawning follow-up work, pass `"debt_id":"<id>"` in the spawn-task DATA to link the task to its obligation._');
  lines.push('_Address open completion debt before adding new obligations in the same area._');
  return lines.join('\n');
}

export function buildPendingFollowupsSection(followups: unknown): string {
  const entries = Array.isArray(followups) ? followups as Array<Record<string, unknown>> : [];
  if (entries.length === 0) return '';
  const lines: string[] = ['\n## Pending Knowledge Follow-ups'];
  lines.push(`${entries.length} design/web_research document(s) completed but not yet consumed or dismissed — the strategist should review and act on these:`);
  const now = Date.now();
  for (const e of entries) {
    const id = strVal(e.id);
    const title = strVal(e.title, '(no title)');
    const summary = strVal(e.summary, '');
    const sourceKind = strVal(e.source_kind, 'unknown');
    const topic = strVal(e.topic);
    const dirId = strVal(e.direction_id);
    const createdAt = strVal(e.created_at);
    let ageStr = '';
    if (createdAt) {
      const ageMs = now - new Date(createdAt).getTime();
      const ageDays = Math.floor(ageMs / (1000 * 60 * 60 * 24));
      ageStr = ageDays > 0 ? ` (${ageDays}d ago)` : ' (today)';
    }
    const tagList = Array.isArray(e.tags) ? (e.tags as string[]).slice(0, 5) : [];
    const tagStr = tagList.length > 0 ? ` [${tagList.join(', ')}]` : '';
    const topicStr = topic ? ` — topic: ${topic}` : '';
    const idTag = id ? ` \`${id}\`` : '';
    lines.push(`  - [${sourceKind}]${idTag} ${title}${ageStr}${topicStr}${tagStr}`);
    if (summary) {
      lines.push(`    ${summary}`);
    }
    if (dirId) {
      lines.push(`    Direction: ${dirId}`);
    }
  }
  lines.push('\n_To consume (acted on / attached to an owning outcome): `./strategic consume-knowledge-followup \'{"document_id":"<id>","reason":"<what was done / owning direction or task>"}\'`_');
  lines.push('_To dismiss (not actionable): `./strategic dismiss-knowledge-followup \'{"document_id":"<id>","reason":"<why not actionable>"}\'`_');
  lines.push('_Act on pending knowledge with bounded tasks under the direction that owns the outcome. Create a direction only when the knowledge establishes a genuinely distinct durable outcome; otherwise consume it if already addressed._');
  return lines.join('\n');
}

export function buildDirHealthSummary(
  directionsSummary: Array<Record<string, unknown>>,
  maxActiveDirections: number,
  authoritative?: { active: number; paused: number },
): string {
  const shippedActiveDirections = directionsSummary.filter(d => strVal(d.status, 'active') === 'active');
  const shippedActiveCount = shippedActiveDirections.length;
  const shippedPaused = directionsSummary.filter(d => strVal(d.status) === 'paused');
  // Anchor the headline count and slots math on the trim-proof authoritative
  // count when the backend shipped it; fall back to the
  // (possibly trimmed) shipped list only for old payloads/tests that omit it.
  const activeCount = authoritative ? authoritative.active : shippedActiveCount;
  const pausedCount = authoritative ? authoritative.paused : shippedPaused.length;
  const autoPausedCount = shippedPaused.filter(d => d.auto_paused === true).length;
  const userPausedCount = Math.max(0, pausedCount - autoPausedCount);
  const saturatedCount = shippedActiveDirections.filter(d => d.possibly_saturated === true).length;
  const productCount = shippedActiveDirections.filter(d => strVal(d.category) === 'product').length;
  const availableSlots = Math.max(0, maxActiveDirections - activeCount);
  const pausedNote = pausedCount > 0
    ? ` Paused: ${pausedCount} (${autoPausedCount} auto, ${userPausedCount} user) — listed below but not consuming active slots.`
    : '';
  const trimNote = (authoritative && shippedActiveCount < activeCount)
    ? ` Showing ${shippedActiveCount} of ${activeCount} active directions below — ${activeCount - shippedActiveCount} trimmed for budget; saturation/product counts reflect only the shipped subset. Fetch the rest with \`./fetch direction <id>\`.`
    : '';
  return [
    `Active: ${activeCount}/${maxActiveDirections} slots used (${availableSlots} available). Saturated: ${saturatedCount}/${activeCount}. Product directions: ${productCount}.${pausedNote}${trimNote}`,
    'Portfolio counts, categories, saturation, and available slots are descriptive state, not targets or evidence that another direction is required.',
  ].join('\n');
}

/**
 * Render payload currency (cycle-start staleness check). When the strategic
 * payload was compiled against an older qualification candidate than the current
 * integration HEAD, audit findings are scored against stale code and must not
 * be blindly trusted as current. Returns '' when staleness is absent entirely.
 * An 'unknown' verdict (GitHub API unreachable at spawn time) renders explicitly
 * so the agent cannot mistake an indeterminate freshness check for confirmed-fresh.
 */
export function buildPayloadStalenessSection(staleness: Record<string, unknown> | undefined): string {
  if (!staleness || typeof staleness !== 'object') return '';

  const lines: string[] = ['\n## Payload Currency (cycle-start check)'];

  // Transient GitHub/API failure — the freshness check could not reach GitHub
  // (5xx / rate-limit / transport / timeout) after a bounded retry. This is a
  // TEMPORARY reachability problem, NOT evidence the payload is stale, so it must
  // render distinctly from an indeterminate ('unknown') check and must NOT push
  // the strategist to re-ground every load-bearing payload claim as stale.
  if (staleness.verdict === 'api_unreachable') {
    const checkedAt = typeof staleness.checked_at === 'string' ? staleness.checked_at : '';
    const reason = typeof staleness.reason === 'string' && staleness.reason ? ` (${staleness.reason})` : '';
    lines.push(`⚠ API UNREACHABLE — the freshness check could not reach GitHub at spawn time${reason} after a bounded retry.${checkedAt ? ` Checked at: ${checkedAt}.` : ''}`);
    lines.push('This is a TEMPORARY API-reachability problem, NOT evidence that this payload is stale. Do NOT re-ground every load-bearing payload claim as stale/unknown on this basis — treat the prior payload SHAs as last-known-good and re-check currency once GitHub is reachable.');
    return lines.join('\n');
  }

  // Unknown/indeterminate verdict — currency check could not be completed
  if (staleness.verdict === 'unknown') {
    const checkedAt = typeof staleness.checked_at === 'string' ? staleness.checked_at : '';
    lines.push(`⚠ UNKNOWN — freshness check could not be completed at spawn time (no candidate SHA, unavailable repo credentials, or the integration branch was not found).${checkedAt ? ` Checked at: ${checkedAt}.` : ''}`);
    lines.push('Treat this payload as potentially stale. Do NOT assume audit findings are scored against current HEAD until a fresh qualification confirms currency.');
    return lines.join('\n');
  }

  // Resolved verdict requires a boolean stale field
  if (typeof staleness.stale !== 'boolean') return '';

  // payload_sha is the primary field; payload_compiled_sha is the backward-compat fallback
  const rawPayloadSha = typeof staleness.payload_sha === 'string' && staleness.payload_sha
    ? staleness.payload_sha
    : (typeof staleness.payload_compiled_sha === 'string' ? staleness.payload_compiled_sha : '');
  const rawCandidateSha = typeof staleness.active_candidate_sha === 'string' ? staleness.active_candidate_sha : '';
  const rawIntegrationHeadSha = typeof staleness.integration_head_sha === 'string' ? staleness.integration_head_sha : '';
  const generatedAt = typeof staleness.generated_at === 'string' ? staleness.generated_at : '';

  const payloadShaShort = rawPayloadSha ? rawPayloadSha.slice(0, 12) : '?';
  const candidateShaShort = rawCandidateSha ? rawCandidateSha.slice(0, 12) : '?';
  const integrationHeadShaShort = rawIntegrationHeadSha ? rawIntegrationHeadSha.slice(0, 12) : '';

  if (staleness.stale) {
    lines.push(`⚠ STALE — this payload was compiled against an older commit than the current active state. Specific divergences:`);
    // Emit payload-vs-candidate bullet only when both SHAs are available and differ
    if (rawPayloadSha && rawCandidateSha && rawPayloadSha !== rawCandidateSha) {
      lines.push(`- payload-vs-candidate: payload compiled at \`${payloadShaShort}\` but active qualification candidate is \`${candidateShaShort}\``);
    }
    // Emit payload-vs-integration-head bullet only when both SHAs are available and differ
    if (rawPayloadSha && rawIntegrationHeadSha && rawPayloadSha !== rawIntegrationHeadSha) {
      lines.push(`- payload-vs-integration-head: payload compiled at \`${payloadShaShort}\` but integration HEAD is \`${integrationHeadShaShort}\``);
    }
    lines.push('Audit findings, CI compliance, and direction scores in this payload were evaluated against the OLDER candidate commit, not the current integration HEAD.');
    lines.push('Do NOT treat unverified audit findings as current promotion blockers before confirming they still reproduce on current HEAD. Consider fetching a fresh qualification before acting on stale findings.');
    if (generatedAt) {
      lines.push(`_Generated at ${generatedAt}_`);
    }
  } else {
    lines.push(`Candidate is CURRENT — payload compiled against \`${candidateShaShort}\`, which matches the active qualification candidate. Audit findings are scored against live HEAD.`);
    if (generatedAt) {
      lines.push(`_Checked at ${generatedAt}_`);
    }
  }
  return lines.join('\n');
}

