import { strVal } from '../index.js';
import { calcMessageAge, parseResearchContext } from './research-plan.js';

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
    const sid = strVal(s.system_id as string, '?');
    const score = typeof s.score === 'number' ? s.score.toFixed(2) : '?';
    const mode = strVal(s.recommended_mode as string, 'unknown');
    const audited = strVal(s.last_audited_at as string, 'never');
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
  const status = strVal(modelHealth.model_size_status as string, 'unknown');
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
  lines.push(`Window: ${strVal(driftSignals.window_days as string, '?')} days.`);
  lines.push('Directions or systems where claimed changes significantly exceed merged PRs — a signal of over-optimistic task output or stalled work.');
  for (const [did, sig] of flaggedDirs) {
    const flags = [sig.over_claim_flag ? 'over_claim' : '', sig.stalled_despite_churn_flag ? 'stalled_despite_churn' : ''].filter(Boolean).join(', ');
    lines.push(`- Direction ${did}: claimed=${fmtScalar(sig.claimed_changes_count)}, merged=${fmtScalar(sig.actually_merged_prs)}, consistency=${typeof sig.claim_shipped_consistency === 'number' ? sig.claim_shipped_consistency.toFixed(2) : '?'} [${flags}]`);
  }
  for (const [sid, sig] of flaggedSys) {
    const flags = [sig.over_claim_flag ? 'over_claim' : '', sig.stalled_despite_churn_flag ? 'stalled_despite_churn' : ''].filter(Boolean).join(', ');
    lines.push(`- System ${sid}: claimed=${fmtScalar(sig.claimed_changes_count)}, merged=${fmtScalar(sig.actually_merged_prs)}, consistency=${typeof sig.claim_shipped_consistency === 'number' ? sig.claim_shipped_consistency.toFixed(2) : '?'} [${flags}]`);
  }
  return lines.join('\n');
}

export function buildGapDirectionLinkageSection(linkageAudit: Record<string, unknown> | undefined): string {
  if (!linkageAudit || typeof linkageAudit !== 'object') return '';
  const orphanedGaps = Array.isArray(linkageAudit.orphaned_gaps)
    ? linkageAudit.orphaned_gaps as Array<Record<string, unknown>>
    : [];
  const ungroundedDirections = Array.isArray(linkageAudit.ungrounded_directions)
    ? linkageAudit.ungrounded_directions as Array<Record<string, unknown>>
    : [];
  if (orphanedGaps.length === 0 && ungroundedDirections.length === 0) return '';

  const lines: string[] = ['\n## Gap-Direction Alignment'];
  lines.push('A single-pass audit of the linkage between problem-model gaps and active directions. **Orphaned gaps** are known problems with no active direction working on them — the strategy has stopped pushing on them. **Ungrounded directions** are active effort with no documented gap behind them — work without a stated problem. This is advisory, not a defect list.');
  lines.push('STEERING: For each orphaned gap, either create a direction targeting it or explicitly acknowledge (in your reasoning / the gap history) that it is intentionally on hold. For each ungrounded direction, either link it to an existing gap (set its gap_id) or propose a new gap that captures the problem it addresses.');

  if (orphanedGaps.length > 0) {
    lines.push('\n### Orphaned gaps (no active direction)');
    for (const g of orphanedGaps) {
      const gid = strVal(g.id as string, '?');
      const name = strVal(g.name as string, '(unnamed)');
      lines.push(`- ${gid}: ${name}`);
    }
  }

  if (ungroundedDirections.length > 0) {
    lines.push('\n### Ungrounded directions (no documented gap)');
    for (const d of ungroundedDirections) {
      const did = strVal(d.id as string, '?');
      const text = strVal(d.text as string, '(no text)');
      lines.push(`- ${did}: ${text}`);
    }
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
    const effectiveStatus = strVal(verification.effective_status as string);
    if (effectiveStatus !== 'absent' && effectiveStatus !== 'partial') continue;

    const name = strVal(capability.name as string, 'Unnamed capability');
    const claimedStatus = strVal(capability.status as string);
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
  const ungroundedDirs = Array.isArray(recon.ungrounded_directions)
    ? recon.ungrounded_directions as Array<Record<string, unknown>> : [];
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
      const name = strVal(c.name as string, 'Unnamed capability');
      const stored = strVal(c.stored_status as string, '?');
      const effective = strVal(c.effective_status as string, '?');
      const missing = Array.isArray(c.missing) ? c.missing as string[] : [];
      const missTail = missing.length > 0 ? ` — missing: ${missing.join(', ')}` : '';
      lines.push(`- **${name}**: stored=${stored} but code shows ${effective.toUpperCase()}${missTail}`);
    }
  }

  // Active gaps with no direction working them and no intentional deferral.
  if (orphanedGaps.length > 0) {
    lines.push(`\n### Orphaned active gaps — open, unstaffed, not deferred (${fmtScalar(counts.orphaned_gaps, String(orphanedGaps.length))})`);
    lines.push('No active direction advances these and no deferral is recorded. Open a direction, defer the gap explicitly, or close it.');
    for (const g of orphanedGaps) {
      const name = strVal(g.name as string, 'Unnamed gap');
      const gid = strVal(g.gap_id as string, '?');
      const priority = strVal(g.priority as string, 'medium');
      lines.push(`- **${name}** [${priority}] (gap ${gid})`);
    }
  }

  // Active directions whose gap_id no longer resolves to an active gap.
  if (ungroundedDirs.length > 0) {
    lines.push(`\n### Ungrounded active directions — advance no tracked gap (${fmtScalar(counts.ungrounded_directions, String(ungroundedDirs.length))})`);
    lines.push('These directions trace to no active gap. Link each to a gap (set gap_id), or retire it if it no longer serves the intent.');
    for (const d of ungroundedDirs) {
      const text = strVal(d.text as string, '?');
      const did = strVal(d.direction_id as string, '?');
      const reason = strVal(d.reason as string) === 'gap_missing'
        ? 'its gap_id no longer resolves to an active gap'
        : 'no gap_id linkage';
      lines.push(`- ${text} (id: ${did}) — ${reason}`);
    }
  }

  // Per-direction research pipeline: where is each active direction stuck/moving.
  const waiting = pipeline.filter(p => strVal(p.state as string) === 'research_waiting_on_action');
  const inProgress = pipeline.filter(p => strVal(p.state as string) === 'tasks_in_progress');
  const idle = pipeline.filter(p => strVal(p.state as string) === 'idle');
  const waitingTotal = countOr(counts.pipeline_waiting_on_action, waiting.length);
  const inProgressTotal = countOr(counts.pipeline_tasks_in_progress, inProgress.length);
  const idleTotal = countOr(counts.pipeline_idle, idle.length);
  const pipelineOverflow = countOr(counts.direction_pipeline_overflow, 0);
  if (pipeline.length > 0 || waitingTotal > 0 || inProgressTotal > 0 || idleTotal > 0) {
    const deferredGaps = fmtScalar(counts.deferred_gaps, '0');
    lines.push(`\n### Research pipeline by direction (${inProgressTotal} with tasks in progress, ${waitingTotal} waiting on action, ${idleTotal} idle; ${deferredGaps} gaps deferred)`);
    if (pipelineOverflow > 0) {
      lines.push(`Only the first ${pipeline.length} active directions are listed below (+${pipelineOverflow} more not shown).`);
    }
    if (waitingTotal > 0) {
      lines.push('Research surfaced findings but NOTHING is acting on them — these are the directions most likely to need a plan/create task spawned:');
      for (const p of waiting) {
        lines.push(`- ${strVal(p.text as string, '?')} (id: ${strVal(p.direction_id as string, '?')}) — ${fmtScalar(p.findings, '0')} recent finding(s), 0 tasks in flight`);
      }
      if (waitingTotal > waiting.length) {
        lines.push(`- +${waitingTotal - waiting.length} more waiting direction(s) not shown`);
      }
    }
    if (inProgressTotal > 0) {
      lines.push('Already moving (tasks in flight — do not double-spawn):');
      for (const p of inProgress) {
        lines.push(`- ${strVal(p.text as string, '?')} (id: ${strVal(p.direction_id as string, '?')}) — ${fmtScalar(p.in_flight, '0')} task(s) in flight`);
      }
      if (inProgressTotal > inProgress.length) {
        lines.push(`- +${inProgressTotal - inProgress.length} more in-progress direction(s) not shown`);
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
    lines.push(`Window: ${strVal(window.from as string, '?')} to ${strVal(window.to as string, '?')}`);
  }
  if (overall) {
    const sr = typeof overall.success_rate === 'number' ? Math.round(overall.success_rate * 100) : '?';
    const productRatioStr = typeof overall.product_ratio === 'number' ? `, product ratio: ${Math.round((overall.product_ratio) * 100)}%` : '';
    lines.push(`Overall: ${fmtScalar(overall.cycles, '0')} cycles, ${fmtScalar(overall.merged, '0')} merged, ${fmtScalar(overall.failed, '0')} failed (${sr}% success rate, trend: ${strVal(overall.trend as string, 'stable')}${productRatioStr})`);
  }

  if (systems.length > 0) {
    lines.push('');
    lines.push('## System progress');
    lines.push('Each vision system compared against actual direction outcomes:');
    for (const sys of systems) {
      const gap = (sys.directions_count as number) === 0 ? ' [NO DIRECTIONS MAPPED]' : '';
      lines.push(`- ${strVal(sys.name as string)} (${strVal(sys.id as string)}): planned=${strVal(sys.planned_status as string)}, ${fmtScalar(sys.directions_count, '0')} directions, ${fmtScalar(sys.merged, '0')} merged, ${fmtScalar(sys.failed, '0')} failed${gap}`);
    }
  }

  if (dirOutcomes.length > 0) {
    lines.push('');
    lines.push('## Direction outcomes');
    lines.push('What each direction actually produced. Use confirmed findings to judge real impact:');
    for (const d of dirOutcomes) {
      const sysTag = d.system_id ? ` (system: ${strVal(d.system_id as string)})` : '';
      lines.push(`\n### ${strVal(d.text as string)} (${strVal(d.id as string)})${sysTag} [${strVal(d.status as string)}]`);
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
    return `- [${strVal(m.type)}] (id: ${strVal(m.id)}, from: ${strVal(m.from)}, open ${age}) ${strVal(m.content).slice(0, 5000)}`;
  }).join('\n');
}

export function buildDirSummaryText(directionsSummary: Array<Record<string, unknown>>): string {
  if (directionsSummary.length === 0) return 'No directions exist yet.';
  return directionsSummary.map((d, i) => {
    const parts = [`${fmtScalar(d.total_cycles, '0')} cycles`];
    if (d.prs_merged) parts.push(`${fmtScalar(d.prs_merged)} merged`);
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

    return `${i + 1}. [${strVal(d.priority, 'medium')}]${catNote} ${strVal(d.text)} (id: ${strVal(d.id)})${statusNote}${satNote}${sysNote}\n   Stats: ${parts.join(', ')}${ctxSection}${findingsSection}${failuresSection}`;
  }).join('\n\n');
}

export function buildDirHealthSummary(
  directionsSummary: Array<Record<string, unknown>>,
  maxActiveDirections: number,
): string {
  const activeDirections = directionsSummary.filter(d => strVal(d.status, 'active') === 'active');
  const activeCount = activeDirections.length;
  const pausedDirections = directionsSummary.filter(d => strVal(d.status) === 'paused');
  const autoPausedCount = pausedDirections.filter(d => d.auto_paused === true).length;
  const userPausedCount = pausedDirections.length - autoPausedCount;
  const saturatedCount = activeDirections.filter(d => d.possibly_saturated === true).length;
  const productCount = activeDirections.filter(d => strVal(d.category) === 'product').length;
  const availableSlots = maxActiveDirections - activeCount;
  const gaps: string[] = [];
  if (productCount === 0) gaps.push('NO_PRODUCT_DIRECTIONS');
  if (saturatedCount === activeCount && activeCount > 0) gaps.push('ALL_SATURATED');
  if (activeCount < 3) gaps.push('BELOW_MINIMUM');
  const pausedNote = pausedDirections.length > 0
    ? ` Paused: ${pausedDirections.length} (${autoPausedCount} auto, ${userPausedCount} user) — listed below but not consuming active slots.`
    : '';
  return [
    `Active: ${activeCount}/${maxActiveDirections} slots used (${availableSlots} available). Saturated: ${saturatedCount}/${activeCount}. Product directions: ${productCount}.${pausedNote}`,
    gaps.length > 0 ? `GAPS DETECTED: ${gaps.join(', ')}` : 'No critical gaps detected.',
  ].join('\n');
}
