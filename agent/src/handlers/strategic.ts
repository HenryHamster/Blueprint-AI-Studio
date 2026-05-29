import fs from 'fs';
import type WebSocket from 'ws';
import {
  WORKSPACE, strVal, sanitize, sendLog, readHandlerOutput,
  persistAndPostStatus, runClaudeCode,
  apiPost,
  resolveBaseBranch,
} from '../index.js';
import * as indexMod from '../index.js';
import { prepareWorkspaceForRole } from '../workspacePrep.js';
import {
  buildContextPrefix, configureAgentPermissions,
  writePlaybookHelper, writeMessagesHelper,
} from '../prompt.js';
import { writeStrategicHelpers } from './strategic-helpers.js';
import { parseBackendTimestamp } from '../utils/time.js';
import {
  formatPreviousDecisionOutcomes,
  formatPastRetirementCritiques,
  formatRetiredDirectionsSummary,
} from './research-direction-context.js';
import {
  computeResearchPlanPromptFingerprint,
  buildThinkerPrompt,
  runThinkerWithGate,
} from '../prompts/thinker.js';
import {
  calcMessageAge,
  parseResearchContext,
  buildStrategicCompletionLogs,
  readProgressArtifact,
  buildRejectedProposalsSection,
  buildUserPausedSection,
  buildVersionPhaseSection,
  buildMemoCitationsSection,
  resolveMemoHandles,
  buildEfficiencySnapshotSection,
  buildPortfolioComposition,
} from './research-plan.js';

export async function handleStrategic(
  taskId: string, payload: Record<string, unknown>, task: Record<string, unknown>,
  ghToken: string, owner: string, repo: string, ws: WebSocket | null, statusRef: { value: string },
  model?: string, effort?: string
) {
  const baseBranch = resolveBaseBranch(payload, task);

  // Clone — strategic still needs a workspace for writing helper scripts
  statusRef.value = `Cloning ${owner}/${repo} for research...`;
  sendLog(ws, `Starting codebase research...\n`);
  const cloneUrl = `https://x-access-token:${ghToken}@github.com/${owner}/${repo}.git`;
  // research uses a FULL clone (no --depth) so it can mine git history.
  // We pass depth=0 to signal full-clone semantics in the cold path; the
  // helper interprets a non-positive depth as "omit --depth".
  await prepareWorkspaceForRole({
    workspace: WORKSPACE, repoUrl: cloneUrl, githubToken: ghToken,
    branch: baseBranch, baseBranch, depth: 0,
  });
  writePlaybookHelper(taskId);
  writeMessagesHelper(taskId);
  const researchMode = strVal(payload.research_mode as string, 'research');
  const disallowedTools = configureAgentPermissions(researchMode);
  const promptFingerprint = computeResearchPlanPromptFingerprint('research');
  const setPromptFingerprint = (indexMod as { setCurrentPromptFingerprint?: (fp: string) => void }).setCurrentPromptFingerprint;
  if (setPromptFingerprint) setPromptFingerprint(promptFingerprint);
  const sendHeartbeat = (indexMod as { sendHeartbeatSync?: (taskId: string, status: string, extra?: Record<string, unknown>) => void }).sendHeartbeatSync;
  if (sendHeartbeat) sendHeartbeat(taskId, statusRef.value, { prompt_fingerprint: promptFingerprint });

  const contextPrefix = buildContextPrefix(task);

  // ── Strategic mode: product-level vision review ────────────────────────
    const vision = payload.vision as Record<string, unknown> | undefined;
    const directionsSummary = Array.isArray(payload.directions_summary)
      ? payload.directions_summary as Array<Record<string, unknown>>
      : [];
    const projectName = strVal(payload.project_name, 'Unknown Project');

    const openMessages = Array.isArray(payload.open_messages)
      ? payload.open_messages as Array<Record<string, unknown>>
      : [];

    const visionJson = vision && Object.keys(vision).length > 0
      ? JSON.stringify(vision, null, 2)
      : '';

    // Mode-awareness signals: system maturity snapshots + short-window drift
    const systemMaturity = Array.isArray(payload.system_maturity)
      ? payload.system_maturity as Array<Record<string, unknown>>
      : [];
    const driftSignals = payload.drift_signals && typeof payload.drift_signals === 'object'
      ? payload.drift_signals as Record<string, unknown>
      : null;

    let systemMaturitySection = '';
    if (systemMaturity.length > 0) {
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
      systemMaturitySection = lines.join('\n');
    }

    // Format an unknown scalar (number/string/bool) for embedding into a template literal.
    const fmtScalar = (v: unknown, fallback = '?'): string => {
      if (v === undefined || v === null) return fallback;
      if (typeof v === 'string' || typeof v === 'number' || typeof v === 'boolean') return String(v);
      return JSON.stringify(v);
    };

    let driftSection = '';
    if (driftSignals) {
      const byDir = driftSignals.by_direction as Record<string, Record<string, unknown>> | undefined;
      const bySys = driftSignals.by_system as Record<string, Record<string, unknown>> | undefined;
      const flaggedDirs = byDir ? Object.entries(byDir).filter(([, sig]) => sig.over_claim_flag || sig.stalled_despite_churn_flag) : [];
      const flaggedSys = bySys ? Object.entries(bySys).filter(([, sig]) => sig.over_claim_flag || sig.stalled_despite_churn_flag) : [];
      if (flaggedDirs.length > 0 || flaggedSys.length > 0) {
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
        driftSection = lines.join('\n');
      }
    }

    const efficiencySnapshotSection = buildEfficiencySnapshotSection(payload.efficiency_snapshot);

    // Build outcome evaluation section
    const outcomeEval = payload.outcome_evaluation as Record<string, unknown> | undefined;
    let outcomeSection = '';
    if (outcomeEval && typeof outcomeEval === 'object') {
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
            if (confirmedFindingTrend) {
              countLine += ` (${confirmedFindingTrend})`;
            }
            lines.push(countLine);
          }

          const confirmed = Array.isArray(d.confirmed) ? d.confirmed as string[] : [];
          if (confirmed.length > 0) {
            lines.push(`Confirmed (shipped): ${confirmed.join('; ')}`);
          }
          const proposed = Array.isArray(d.proposed) ? d.proposed as string[] : [];
          if (proposed.length > 0) {
            lines.push(`Proposed (in-flight): ${proposed.join('; ')}`);
          }
          const failedApproaches = Array.isArray(d.failed_approaches) ? d.failed_approaches as string[] : [];
          if (failedApproaches.length > 0) {
            lines.push(`Failed approaches: ${failedApproaches.slice(0, 5).join('; ')}`);
          }
        }
      }

      if (lines.length > 0) {
        outcomeSection = '\n' + lines.join('\n') + '\n';
      }
    }

    // Recent interventions — time-series view of what shipped + per-week rate deltas.
    // Given with explicit non-causal framing so the agent doesn't over-attribute.
    const recentInterventions = payload.recent_interventions as Record<string, unknown> | undefined;
    let clockHeader = '';
    let recentWorkSection = '';
    if (recentInterventions && typeof recentInterventions === 'object') {
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

      // Clock header: answer "since I last ran, what's happened?" before anything else.
      // Forces the agent to notice when a cycle elapsed without meaningful throughput
      // instead of silently re-running its last playbook.
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

      // Active in-flight counts per scope: preserves visibility of
      // still-running work now that merge rates are finished-only.
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
            // on_automain=false means the task PR was merged into a rollup staging
            // branch but the rollup itself has not yet landed on automain.
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
    }

    // Unified conversation thread — chronological, shows back-and-forth
    const conversationThread = Array.isArray(payload.conversation_thread)
      ? payload.conversation_thread as Array<Record<string, unknown>>
      : [];
    const threadSection = conversationThread.length > 0
      ? '\n## Conversation History\n' +
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
        }).join('\n') + '\n'
      : '';

    // Extract open FEEDBACK messages — these are direct user requests
    const feedbackMessages = openMessages.filter(m => strVal(m.type) === 'FEEDBACK');
    const feedbackSection = feedbackMessages.length > 0
      ? (() => {
          const feedbackLines = feedbackMessages.map(m => {
            const age = calcMessageAge(m);
            return `- (id: ${strVal(m.id)}, from: ${strVal(m.from)}, open ${age}) ${strVal(m.content).slice(0, 5000)}`;
          }).join('\n');
          return `\n## User Feature Requests\nThese FEEDBACK messages are direct user requests. Strongly consider creating a direction for each unaddressed request:\n${feedbackLines}\n`;
        })()
      : '';

    // Flat open messages list for executor resolve operations
    const openMessagesText = openMessages.length > 0
      ? openMessages.map(m => {
          const age = calcMessageAge(m);
          return `- [${strVal(m.type)}] (id: ${strVal(m.id)}, from: ${strVal(m.from)}, open ${age}) ${strVal(m.content).slice(0, 5000)}`;
        }).join('\n')
      : '';

    const dirSummaryText = directionsSummary.length > 0
      ? directionsSummary.map((d, i) => {
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

          // Research context: show established findings count, failed approaches, open questions
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
              ctxParts.push(`   \u26a0 Context trimmed: ${sections.eviction}. Earlier findings were evicted — check git history if you suspect overlap with prior work.`);
            }
            if (ctxParts.length > 0) ctxSection = '\n' + ctxParts.join('\n');
          }

          // Recent findings: what was actually discovered/built
          let findingsSection = '';
          const findings = Array.isArray(d.recent_findings) ? d.recent_findings as string[] : [];
          if (findings.length > 0) {
            findingsSection = `\n   Recent work: ${findings.slice(0, 5).join('; ')}`;
          }

          // Recent failures: why things failed (with category for infra vs code distinction)
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
        }).join('\n\n')
      : 'No directions exist yet.';

    // Build design doc section — this is the primary input for Phase 2
    const designDoc = payload.design_doc as Record<string, unknown> | undefined;
    const designDocJson = designDoc && Object.keys(designDoc).length > 0
      ? JSON.stringify(designDoc, null, 2)
      : '';

    // Write strategic helper scripts (strategic, fetch, design-doc)
    // ./fetch helper — pulls full state for items the strategic packer trimmed.
    // The strategic-spawn payload is packed under a tight byte budget (see
    // pack_under_budget in backend/app/payload_schemas.py); when it trims a
    // list or drops a section it records that in payload._packer_meta. The
    // agent inspects _packer_meta and uses ./fetch to retrieve detail it
    // actually needs (avoids a 256 KB context that the LLM has to scan).
    const projectMetadataForFetch = task.project_metadata as Record<string, unknown> | undefined;
    const fetchProjectId = (projectMetadataForFetch?.project_id as string | undefined) ?? '';
    writeStrategicHelpers(taskId, fetchProjectId, WORKSPACE);

    // ── Compute direction health summary for thinker prompt ──
    const maxActiveDirections = typeof payload.max_active_directions === 'number'
      ? payload.max_active_directions
      : 10;
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
    const dirHealthSummary = [
      `Active: ${activeCount}/${maxActiveDirections} slots used (${availableSlots} available). Saturated: ${saturatedCount}/${activeCount}. Product directions: ${productCount}.${pausedNote}`,
      gaps.length > 0 ? `GAPS DETECTED: ${gaps.join(', ')}` : 'No critical gaps detected.',
    ].join('\n');

    // Portfolio composition: category mix, system coverage gaps, concentration
    // risk, in-flight scope, direction age — so the thinker doesn't have to
    // re-compute these from scattered per-direction lines.
    const outcomeSystems = outcomeEval && Array.isArray(outcomeEval.systems)
      ? outcomeEval.systems as Array<Record<string, unknown>>
      : (vision && Array.isArray(vision.systems) ? vision.systems as Array<Record<string, unknown>> : []);
    const activeByScopeForPortfolio = (payload.recent_interventions as Record<string, unknown> | undefined)
      ?.active_by_scope as Record<string, number> | undefined;
    const portfolioCompositionText = buildPortfolioComposition(
      activeDirections,
      outcomeSystems,
      activeByScopeForPortfolio,
    );

    // ── Running context from prior cycles (design doc notes/strategic-context) ──
    const strategicContext = designDoc
      ? strVal((designDoc)['notes/strategic-context'])
      : '';

    // ── Problem model + running update + self-reflection rendering ──
    // These are the new strategic substrate — persistent durable state the
    // agent maintains across cycles. They replace vision as source of truth.
    const problemModel = payload.problem_model as Record<string, unknown> | undefined;
    const problemModelJson = problemModel && Object.keys(problemModel).length > 0
      ? JSON.stringify(problemModel, null, 2)
      : '';

    const runningUpdateDoc = typeof payload.running_update === 'string'
      ? payload.running_update
      : '';
    const runningUpdateAckAt = strVal(payload.running_update_acknowledged_at);
    const userLastActiveAt = strVal(payload.user_last_active_at);
    const selfReflectionEntries = Array.isArray(payload.self_reflection_log)
      ? payload.self_reflection_log as Array<Record<string, unknown>>
      : [];
    const isSelfRepo = payload.enable_self_improvement === true;

    const renderRunningUpdateDoc = (doc: string): string => {
      if (!doc.trim()) {
        return '_(running_update is empty — fresh project or nothing written yet. This cycle should populate it.)_';
      }
      return doc.trim();
    };

    const renderReflectionList = (entries: Array<Record<string, unknown>>): string => {
      if (entries.length === 0) {
        return '_(no prior self-reflections on record)_';
      }
      const lines: string[] = [];
      for (const e of entries.slice(-5)) {
        const ts = strVal(e.ts, '?');
        const hard = strVal(e.what_was_hard);
        const missing = strVal(e.missing_context);
        const prop = strVal(e.proposed_self_improvement);
        lines.push(`- **${ts}**`);
        if (hard) lines.push(`  - hard: ${hard}`);
        if (missing) lines.push(`  - missing: ${missing}`);
        if (prop) lines.push(`  - proposed: ${prop}`);
      }
      return lines.join('\n');
    };

    const formatRelativeDays = (iso: string): string => {
      if (!iso) return '';
      try {
        const d = parseBackendTimestamp(iso);
        if (!d) return '';
        const days = Math.max(0, Math.floor((Date.now() - d.getTime()) / 86400000));
        if (days === 0) return 'today';
        if (days === 1) return '1 day ago';
        return `${days} days ago`;
      } catch { return ''; }
    };

    const userActiveRel = formatRelativeDays(userLastActiveAt);
    const ackRel = formatRelativeDays(runningUpdateAckAt);
    const userStateLine = (userActiveRel || ackRel)
      ? `User last active: ${userActiveRel || 'unknown'}${ackRel ? ` | last acknowledged running_update: ${ackRel}` : ''}`
      : 'User activity: no record yet';

    // ── Packer metadata: tell the agent what was trimmed and how to recover it ──
    // The strategic-spawn payload is filled under a hard byte budget (see
    // pack_under_budget in backend/app/payload_schemas.py). When the packer
    // can't fit a section it either summarizes (list_trimmed) or drops it
    // wholesale; both forms land in payload._packer_meta. Render them so the
    // agent knows what's available-but-not-shipped and can call ./fetch.
    const packerMeta = payload._packer_meta as Record<string, unknown> | undefined;
    const renderPackerMeta = (meta: Record<string, unknown> | undefined): string => {
      if (!meta) return '';
      const trimmed = Array.isArray(meta.trimmed) ? meta.trimmed as Array<Record<string, unknown>> : [];
      const dropped = Array.isArray(meta.dropped) ? meta.dropped as Array<Record<string, unknown>> : [];
      if (trimmed.length === 0 && dropped.length === 0) return '';
      const lines: string[] = ['## Context not shipped this cycle (use ./fetch if you need it)'];
      if (trimmed.length > 0) {
        lines.push('', 'Trimmed (partial data shipped):');
        for (const t of trimmed) {
          const k = strVal(t.key, '?');
          const kind = strVal(t.kind, 'summarized');
          if (kind === 'list_trimmed') {
            const shown = (t.shown as number | undefined) ?? '?';
            const total = (t.total as number | undefined) ?? '?';
            lines.push(`  - ${k}: showing ${shown} of ${total} entries`);
          } else {
            lines.push(`  - ${k}: shipped in summarized form`);
          }
        }
      }
      if (dropped.length > 0) {
        lines.push('', 'Dropped entirely (not in this payload):');
        for (const d of dropped) {
          const k = strVal(d.key, '?');
          const reason = strVal(d.reason, 'budget exhausted');
          lines.push(`  - ${k}: ${reason}`);
        }
      }
      lines.push(
        '',
        'To pull full detail when you need it:',
        '  - `./fetch direction <id>` — full direction record (research_history capped at 20 cycles)',
        '  - `./fetch playbook <key>` — single playbook entry (e.g. `./fetch playbook frontend-build`)',
        '  - `./fetch gap <id>` — single gap with full history/past_moves/evidence',
      );
      return lines.join('\n');
    };
    const packerMetaSection = renderPackerMeta(packerMeta);

    const problemModelSection = problemModelJson
      ? `## Your current understanding (problem_model)\n\`\`\`json\n${problemModelJson}\n\`\`\``
      : `## Your current understanding (problem_model)\n_(empty — this is the first cycle. Bootstrap from the other sections below.)_`;

    const runningUpdateSection = `## Running update (living document — rewrite, don't append)\n${renderRunningUpdateDoc(runningUpdateDoc)}\n\n_This is the document the user currently sees. Refine it this cycle — remove stale content, integrate what's new, keep it coherent and short. It's a current snapshot, not a log._`;

    const reflectionSection = `## What you previously said was hard\n${renderReflectionList(selfReflectionEntries)}`;

    const pastRetirementCritiquesSection = formatPastRetirementCritiques(payload.past_retirement_critiques);

    const selfImprovementBlock = payload.enable_self_improvement === true ? `

# Self-improvement

This project's codebase includes your own prompts, schemas, tools, and
orchestration. When you notice a recurring limitation in yourself — missing
context, unclear instruction, schema friction — it's a gap. Name it.

Look at "What you previously said was hard" above. If the same theme
appears in 2+ reflections, open a gap tagged \`self_improvement\` and
propose a direction that modifies the code governing your behavior.
Cite the reflection entries via \`source.type=self_reflection\`.

You don't need user permission to improve yourself — that's the whole
point of self-repo. Just do it and tell the user in whats_changed.` : '';

    // ── Pass 1: Thinker — analyze freely, write prose ──
    // Let the agent do what it's good at: read code, reason about architecture,
    // write a strategic analysis. No API constraints, no JSON formatting.
    const versionPhase = payload.version_phase && typeof payload.version_phase === 'object'
      ? payload.version_phase as Record<string, unknown>
      : undefined;
    const thinkerPrompt = buildThinkerPrompt({
      contextPrefix,
      projectName,
      clockHeader,
      isSelfRepo,
      userStateLine,
      packerMetaSection,
      strategicContext,
      problemModelSection,
      reflectionSection,
      selfImprovementBlock,
      runningUpdateSection,
      designDocJson,
      visionJson,
      outcomeSection,
      previousDecisionOutcomes: formatPreviousDecisionOutcomes(payload.previous_decision_outcomes),
      pastRetirementCritiquesSection,
      retiredDirectionsSummarySection: formatRetiredDirectionsSummary(payload.retired_directions_summary),
      dirSummaryText,
      dirHealthSummary,
      userPausedSection: buildUserPausedSection(directionsSummary),
      rejectedProposalsSection: buildRejectedProposalsSection(Array.isArray(payload.rejected_proposals) ? payload.rejected_proposals as Array<Record<string, unknown>> : []),
      portfolioCompositionText,
      systemMaturitySection,
      driftSection,
      recentWorkSection,
      feedbackSection,
      threadSection,
      taskId,
      versionPhaseSection: buildVersionPhaseSection(versionPhase),
      memoCitationsSection: buildMemoCitationsSection(payload.memo_citations),
      efficiencySnapshotSection,
    });

    const thinkerGateResult = await runThinkerWithGate(
      thinkerPrompt, taskId, ws, statusRef, model, effort, disallowedTools,
    );
    if (!thinkerGateResult.ok) return;
    const analysisText = thinkerGateResult.analysisText;

    // Scan thinker analysis for cited memo handles and persist them via the API.
    // The completion handler reads task.context["_strategic_cited_memo_ids"] to
    // count memos_cited — scanning the analysis text (where memos were shown
    // to the agent) is reliable. Memos are cited by short handles (M0, M1, M2)
    // rather than task UUIDs, which are fragile for LLMs to reproduce exactly.
    const memoCitations = Array.isArray(payload.memo_citations)
      ? payload.memo_citations as Array<Record<string, unknown>>
      : [];
    if (memoCitations.length > 0) {
      const citedMemoIds = resolveMemoHandles(analysisText, memoCitations);
      try {
        await apiPost('/api/agent/strategic', {
          task_id: taskId,
          op: 'cite_memos',
          data: { cited_memo_ids: citedMemoIds },
        });
        sendLog(ws, `[INFO] Memo citations: ${citedMemoIds.length}/${memoCitations.length} cited\n`);
      } catch (e) {
        sendLog(ws, `[WARNING] Failed to persist memo citations (non-fatal): ${String(e)}\n`);
      }
    }

    // ── Pass 2: Executor — translate analysis into API calls ──
    // Short, focused pass. The analysis is done. This just calls the tools.
    // Note: do NOT overwrite CLAUDE.md here. The executor needs to inherit the
    // real project CLAUDE.md so the strategic state it writes is grounded in
    // the managed project. When CLAUDE.md was stripped to a bare "Executor
    // Agent" role blurb, the executor hallucinated problem_model content
    // describing its own role instead of the project.

    const executorPrompt = `# This task's scope

You are applying a strategic analysis that was already produced by a thinker agent. For THIS task only:

- Don't re-analyze the codebase — the thinker did that. Trust its findings unless they're internally inconsistent.
- Don't write prose output. Your output is bash commands and their results.
- You retain judgment: verify the thinker's findings against the analysis, construct alternatives when something is missing or malformed, review before submitting. But keep it bounded — this is application, not re-planning.
- The project you are managing is described in CLAUDE.md (read it if you need grounding). All strategic state you write (problem_model, vision, directions) describes THAT project — never this execution environment, this prompt, your role, or the ./strategic / ./design-doc commands themselves.

You are a strategic executor. Your job is to translate the analysis below into API calls, with JUDGMENT — not blind execution. You decide how the analysis maps to operations.

## Ownership
You own this project's direction. The analysis above is your reasoning — now execute it. If the data clearly supports a change, make it. Don't hedge, don't soften, don't ask permission for things the analysis already answered.

Only escalate when you literally cannot proceed without the user:
- Missing credentials, API keys, deploy targets, or external access
- Ambiguous user intent where the wrong call wastes cycles
- External blockers outside the system's control

Use \`./strategic pending-confirm\` for those — it's the structured channel for blocking asks. Use \`./messages send\` for general conversation or observations. NEVER \`./messages send\` to ask "should I do X?" for coding decisions — those you make.

If a direction is dead, retire it. If there's an obvious gap, open it. If a user asked for something, create a direction with a source citation on the gap.

Do NOT analyze code. Do NOT explore the codebase. Think briefly about each operation, then run the commands.

# Commands available

\`\`\`bash
# REQUIRED: Update the problem model (the strategic source of truth).
# The Thinker wrote the JSON to /tmp/problem_model_${taskId}.json. Read it verbatim.
./strategic set-problem-model "$(cat /tmp/problem_model_${taskId}.json)"

# REQUIRED: Replace the user's running_update document (a living markdown
# narrative of current state — NOT a log). The Thinker wrote the full
# refined doc to /tmp/running_update_${taskId}.md; pass the file path.
./strategic set-running-update /tmp/running_update_${taskId}.md

# Optional: reflect on what was hard this cycle (only if Section 5 has content).
./strategic reflect '{"what_was_hard": "...", "missing_context": "...", "proposed_self_improvement": "..."}'

# Optional: ask the user for external-blocker resolution.
./strategic pending-confirm '{"type": "credentials_needed|access_needed|intent_change|major_decision", "proposal": "...", "rationale": "..."}'

# Direction operations (link each create to a gap_id from the problem_model).
# conflicts_with: list direction IDs that touch overlapping code/systems — they won't run in parallel.
# recommended_mode: audit|fundamentals|ship|pause — your judgment for this direction's system health.
./strategic create-direction '{"text": "outcome statement", "priority": "high|medium|low", "category": "product|structural", "gap_id": "g-xxx", "rationale": "...", "conflicts_with": ["d-xxx"], "recommended_mode": "ship", "success_evidence": "observable outcome that proves this direction is done (required)", "retirement_condition": "measurable condition that triggers retirement (required)", "implementation_constraints": "optional constraints for implementors", "dedup_reasoning": {"closest_directions": ["d-xxx"], "distinction": "one-line justification why this is distinct in scope or mechanism"}}'
./strategic update-direction '{"direction_id": "d-xxx", "changes": {"priority": "...", "text": "...", "conflicts_with": [...], "recommended_mode": "audit"}, "rationale": "..."}'
./strategic retire-direction '{"direction_id": "d-xxx", "rationale": "..."}'

# REQUIRED: Submit the retired-direction critique from Section 3.5.
# Pass a JSON array of critique objects (or the no-eligible sentinel).
./strategic set-retirement-critiques '[{"direction_id":"d-xxx","what_it_addressed":"...","pattern_that_produced_it":"...","lesson":"..."}]'
# If no retirements in last 90 days:
./strategic set-retirement-critiques '{"no_eligible_retirements": true}'

# OPTIONAL: Submit evidence-progress verdicts from Section 3.6 for long-running
# active directions. status ∈ advancing|stalled|met; advancing/met suppress the
# advisory scaffolding flag, stalled reinforces it. Omit entirely if Section 3.6
# assessed no directions — it's an optional precision refinement, never required.
./strategic set-evidence-progress '[{"direction_id":"d-xxx","status":"advancing","rationale":"..."}]'

# Spawn a targeted investigation task (research or plan only) when something
# from the Recent Work section needs investigation NOW instead of waiting for
# the next autonomous cycle.
./strategic spawn-task '{"type": "research", "title": "...", "prompt": "Full investigation prompt...", "direction_id": "d-xxx"}'

# Pull full detail for items the strategic packer trimmed (see "Context not
# shipped this cycle" section in your prompt). Use only when you actually
# need the detail — extra context hurts reasoning quality.
./fetch direction <direction_id>   # full direction record
./fetch playbook <key>             # single playbook entry
./fetch gap <gap_id>               # single problem_model gap

# Respond to or resolve user messages
./messages resolve <message-id> 'How it was addressed'
./messages send 'Observation or note to the user'
\`\`\`

# Existing directions (for update/retire operations)
${dirSummaryText}

${feedbackSection}${openMessagesText ? `# Open Messages (resolve addressed ones, respond to questions)\n${openMessagesText}\n` : ''}
# Strategic analysis to execute

${analysisText.slice(0, 50_000)}

# Instructions

1. **Problem model (REQUIRED).** First do step 5 below: if the Thinker emitted \`version_phase_decision\` and/or \`qualify_request\`, edit \`/tmp/problem_model_${taskId}.json\` IN PLACE to add those top-level sibling fields. Then pass \`"$(cat /tmp/problem_model_${taskId}.json)"\` to \`./strategic set-problem-model\` — the \`cat\` re-reads the file, so the sibling fields you added in step 5 are included. Don't summarize or re-emit the model's own content — read-and-pass; the only edit permitted is step 5's sibling fields.

   Fallback (use judgment): if the file is missing, empty, or the JSON is malformed, you may construct a corrected problem_model yourself — but ground it STRICTLY in the thinker's analysis below (Sections 1-6) and in the project described by CLAUDE.md. Verify findings against the analysis before writing. If even the analysis is insufficient, call \`./strategic pending-confirm\` with type "major_decision" to surface the gap rather than ship bad state. NEVER describe this execution environment, this prompt, your role, or the ./strategic / ./design-doc commands — the problem_model describes the managed project from CLAUDE.md.

   This is the canonical source of truth — EVERY cycle must emit it so the revision bumps and the cycle is auditable.

2. **Running update (REQUIRED).** Primary path: run \`./strategic set-running-update /tmp/running_update_${taskId}.md\`. The helper reads the file and wraps it as a JSON string for the API — don't try to pass markdown as argv.

   Fallback (use judgment): if the file is missing or empty, you may write the markdown yourself to that path before running the command — but ground it STRICTLY in the thinker's Section 4 and in the project described by CLAUDE.md. Describe the MANAGED PROJECT as a current snapshot (what's going on, what you're working on, blockers, next). Never describe this execution environment, this prompt, your role, or the ./strategic / ./design-doc commands.

   This doc is the user's primary view of the project — never skip it.

3. **Direction ops.** From Section 3, retire first, then create/update. For each create, set \`gap_id\` pointing to the gap_id defined in the problem_model. Parallel moves are fine — file the gap_id and let the orchestrator handle concurrency.

   - Does an existing direction already cover this? → UPDATE it; don't duplicate.
   - Does this depend on another direction being done first? → Keep it paused or don't create yet.
   - Direction text describes OUTCOME ("users can X", "the system handles Y"), not implementation. Implementation details go in rationale.

   Priority is scarce:
   - high: 1-2 directions max — top work RIGHT NOW
   - medium: the next tier
   - low: opportunistic

4. **Retired-direction critique (REQUIRED).** From Section 3.5, call \`./strategic set-retirement-critiques\` with a JSON array of critique objects. If the Thinker wrote "No eligible retirements", pass \`'{"no_eligible_retirements": true}'\`. Never skip this call.

   *Optional companion:* If the Thinker's Section 3.6 recorded \`evidence_progress\` verdicts for long-running active directions, persist them with \`./strategic set-evidence-progress '[{"direction_id":"d-xxx","status":"advancing|stalled|met","rationale":"..."}]'\`. Skip entirely when Section 3.6 is empty — this is an optional precision refinement, never a required call.

5. **Version phase / qualify siblings (conditional — do this BEFORE step 1's \`set-problem-model\` call).** If the Thinker's analysis includes a \`version_phase_decision\` field (look in Section 6 and throughout the analysis), edit \`/tmp/problem_model_${taskId}.json\` IN PLACE to add it as a top-level sibling key, so the file becomes:
   \`{"version_phase_decision": {"phase": "build" or "stabilize", "reasoning": "..."}, ...rest_of_problem_model}\`
   If the Thinker did NOT emit \`version_phase_decision\`, SKIP — do not fabricate one, and do not add the field. Absence means "no phase change this cycle."

   Likewise, if the Thinker's analysis includes a \`qualify_request\` field (look in Section 6 and throughout the analysis), edit \`/tmp/problem_model_${taskId}.json\` IN PLACE to add it as a top-level sibling key too:
   \`{"qualify_request": {"reason": "..."}, ...rest_of_problem_model}\`
   If the Thinker did NOT emit \`qualify_request\`, SKIP — do not fabricate one, and do not add the field. Absence means "do not qualify this cycle." \`version_phase_decision\` and \`qualify_request\` are independent siblings; add whichever the Thinker emitted (either, both, or neither).

   Because step 1 passes \`"$(cat /tmp/problem_model_${taskId}.json)"\` to \`./strategic set-problem-model\`, editing the file here is what carries these siblings through — step 1 will re-read the file and the fields will be included. If the file is missing/malformed and you took step 1's fallback (constructing the model inline), include these siblings in that inline JSON instead.

6. **Self-reflection (optional).** If Section 5 has non-empty content, emit via \`./strategic reflect\`. If the Thinker wrote nothing (or wrote "none" / "N/A" / empty), SKIP — do not fabricate.

7. **Pending confirmations (optional).** For each external blocker the Thinker identified in Section 4's blockers field (credentials, access, major pivots), emit one \`./strategic pending-confirm\`. Do NOT create these for coding questions.

8. **Messages.** Resolve any user messages the analysis addressed: \`./messages resolve <id> 'Answered — [summary]'\`. Check the conversation for your prior QUESTION messages — if a user reply answers one, resolve it. Do not post a cycle-summary message — the running_update is the voice now.

9. **Guardrails.** Ensure at least 3 active directions remain after retires.

10. Done. Do not write anything else.`;

    statusRef.value = 'Running strategic execution (pass 2: applying decisions)...';
    let { exitCode, allOutput, lastResultText } = await runClaudeCode(executorPrompt, ws, statusRef, model, effort, disallowedTools);

    // Post-check: verify the executor actually called ./strategic
    // set-problem-model AND ./strategic set-running-update. These are
    // the new canonical required calls — problem_model is the strategic
    // source of truth, running_update is the voice to the user. If either
    // is missing the cycle is incomplete; retry once with a focused prompt.
    try {
      // Accept three forms of evidence the call landed:
      //   1. JSON op name in API request/response: "op":"set_problem_model"
      //   2. CLI invocation:                       ./strategic set-problem-model
      //   3. Success message from the strategic CLI: "Problem model set at revision N"
      // Earlier this only matched form 1, so agents that used the CLI happy-path
      // (which prints the success message but never echoes the JSON op key) got
      // tagged as "missing" → unnecessary retry double-writing the same data.
      const problemModelSetInOutput =
        /"op"\s*:\s*"set_problem_model"/.test(allOutput)
        || /\.\/strategic\s+set-problem-model\b/.test(allOutput)
        || /Problem model set at revision/i.test(allOutput);
      const runningUpdateSetInOutput =
        /"op"\s*:\s*"set_running_update"/.test(allOutput)
        || /\.\/strategic\s+set-running-update\b/.test(allOutput)
        || /Running update set/i.test(allOutput);
      const retirementCritiquesSetInOutput =
        /"op"\s*:\s*"set_retirement_critiques"/.test(allOutput)
        || /\.\/strategic\s+set-retirement-critiques\b/.test(allOutput)
        || /Retirement critiques recorded/i.test(allOutput);

      if (exitCode === 0 && (!problemModelSetInOutput || !runningUpdateSetInOutput || !retirementCritiquesSetInOutput)) {
        const missing: string[] = [];
        if (!problemModelSetInOutput) missing.push('./strategic set-problem-model');
        if (!runningUpdateSetInOutput) missing.push('./strategic set-running-update');
        if (!retirementCritiquesSetInOutput) missing.push('./strategic set-retirement-critiques');
        sendLog(ws, `\n[WARNING] Strategic executor completed but required calls missing: ${missing.join(', ')}. Retrying...\n`);
        const retryInstructions = [
          'Your previous attempt did not call one or more REQUIRED commands.',
          '',
          `Missing: ${missing.join(', ')}`,
          '',
          'Run these NOW using data from the analysis:',
          !problemModelSetInOutput ? `- ./strategic set-problem-model "$(cat /tmp/problem_model_${taskId}.json)"  (if file missing: construct from analysis grounded in the managed project — NEVER self-describe)` : '',
          !runningUpdateSetInOutput ? `- ./strategic set-running-update /tmp/running_update_${taskId}.md  (if file missing: write a single clean markdown doc to that path first — describe the managed project's current state, NEVER self-describe)` : '',
          !retirementCritiquesSetInOutput ? `- ./strategic set-retirement-critiques '[{"direction_id":"d-xxx","what_it_addressed":"...","pattern_that_produced_it":"...","lesson":"..."}]'  OR  ./strategic set-retirement-critiques '{"no_eligible_retirements": true}'  (one of these two payloads, depending on whether there are retired directions to critique)` : '',
          '',
          'Nothing else.',
        ].filter(Boolean).join('\n');
        const retryResult = await runClaudeCode(
          retryInstructions, ws, statusRef, model, effort, disallowedTools
        );
        exitCode = retryResult.exitCode;
        allOutput = allOutput + '\n' + retryResult.allOutput;
        lastResultText = retryResult.lastResultText || lastResultText;
      }
    } catch (e) {
      sendLog(ws, `[WARNING] Could not verify strategic required calls: ${String(e)}\n`);
    }

    // Post-check: verify direction operations were executed if thinker recommended them
    try {
      const directionCreatedInOutput = /"status"\s*:\s*"ok"[\s\S]{0,100}"op"\s*:\s*"create_direction"/.test(allOutput)
        || /"op"\s*:\s*"create_direction"[\s\S]{0,100}"status"\s*:\s*"ok"/.test(allOutput);
      const directionUpdatedInOutput = /"status"\s*:\s*"ok"[\s\S]{0,100}"op"\s*:\s*"(update_direction|retire_direction)"/.test(allOutput)
        || /"op"\s*:\s*"(update_direction|retire_direction)"[\s\S]{0,100}"status"\s*:\s*"ok"/.test(allOutput);
      const anyDirectionOp = directionCreatedInOutput || directionUpdatedInOutput;

      // Detect constraint rejections (cap/dedup) so we don't retry wastefully
      const directionRejectedInOutput = /\[CONSTRAINT\].*create_direction/.test(allOutput) || /"status"\s*:\s*"skipped"/.test(allOutput);

      // Check if thinker's structured output contained direction recommendations
      const structuredSection = analysisText.match(/## Structured Output[\s\S]*$/i)?.[0] ?? '';
      const thinkerRecommendedCreates = /create[_-]direction|"op"\s*:\s*"create"/i.test(structuredSection);

      if (directionRejectedInOutput && !anyDirectionOp) {
        sendLog(ws, '\n[INFO] Direction creation was attempted but rejected by constraints (cap/duplicate). Not retrying.\n');
      } else if (exitCode === 0 && thinkerRecommendedCreates && !anyDirectionOp) {
        sendLog(ws, '\n[WARNING] Strategic executor completed but no direction operations were executed. Retrying...\n');
        const retryResult = await runClaudeCode(
          'Your previous attempt did not execute any ./strategic create-direction commands.\n\n' +
          'The strategic analysis recommended creating new directions. Run the create-direction commands NOW:\n\n' +
          'For each new direction in the analysis, run:\n' +
          './strategic create-direction \'{"text": "...", "priority": "high|medium|low", "category": "product|structural", "system_id": "sys-x", "rationale": "..."}\' \n\n' +
          'Extract the directions from the analysis and call the commands. Nothing else.',
          ws, statusRef, model, effort, disallowedTools
        );
        exitCode = retryResult.exitCode;
        allOutput = allOutput + '\n' + retryResult.allOutput;
        lastResultText = retryResult.lastResultText || lastResultText;
      }
    } catch (e) {
      sendLog(ws, `[WARNING] Could not verify strategic direction ops: ${String(e)}\n`);
    }

    // Legacy: try to read /tmp output file when present (backward compat).
    // The command-based strategic flow does not write this file.
    let logs = '';
    if (fs.existsSync(`/tmp/strategic_output_${taskId}.json`)) {
      const strategicOutput = readHandlerOutput(taskId, 'strategic_output', ws);
      logs = strategicOutput !== null ? sanitize(JSON.stringify(strategicOutput)) : '';
    }
    if (!logs) {
      const completionLogs = buildStrategicCompletionLogs(allOutput, lastResultText);
      if (completionLogs.warning) {
        sendLog(ws, `[WARNING] ${completionLogs.warning}\n`);
      }
      logs = completionLogs.logs;
    }

    const strategicProgress = readProgressArtifact(taskId, 'research');
    try {
      await persistAndPostStatus(taskId, {
        task_id: taskId,
        status: exitCode === 0 ? 'COMPLETED' : 'FAILED',
        ...(exitCode !== 0 ? { artifact_type: 'ERROR_LOG' } : { artifact_type: 'RESEARCH_FINDINGS' }),
        logs,
        ...(exitCode !== 0 ? { failure_category: 'claude_code_failed' } : {}),
        ...(strategicProgress ? { analysis_plan: strategicProgress.slice(0, 5000) } : {}),
      });
    } catch (e) {
      sendLog(ws, `[ERROR] Failed to post strategic completion status: ${String(e)}\n`);
    }
    return;
}
