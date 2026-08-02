import { strVal } from '../prompt.js';
import { MEDIUM_ANALYSIS_PLAN_FILE_LIMIT } from '../constants.js';
import { formatProjectBlindSpotSummary, formatSrRecentMissExamplesForSrPrompt } from './self-review.js';

function parseResearchSummary(raw: string): string {
  try {
    const parsed = JSON.parse(raw) as Record<string, unknown>;
    const items = Array.isArray(parsed) ? parsed : (Array.isArray(parsed.logs) ? parsed.logs : []);
    if (Array.isArray(items) && items.length > 0) {
      return (items as Record<string, unknown>[]).slice(0, 3)
        .map((f) => `${strVal(f.title, '?')}: ${(strVal(f.description) || strVal(f.prompt)).slice(0, 120)}`)
        .join(' | ');
    }
  } catch { /* ignore */ }
  return raw.slice(0, 400);
}

// ── Shared: Format project_context into a markdown section ────────────────
export function formatProjectContext(ctx: Record<string, unknown>, taskType = '', taskScope = '', currentDirectionId = '', relatedFilesCount = 0, opts?: { suppressTaskLists?: boolean }): string {
  const CONTEXT_BUDGET = 8000;

  const header = [
    '## Project Context',
    'The following tasks are already active or completed in this project. Avoid duplicating their work.',
    '',
  ];

  // Build active tasks section (always included in full, unless suppressTaskLists)
  const activeLines: string[] = [];
  const activeTasks = ctx.active_tasks as Array<Record<string, unknown>> | undefined;
  if (!opts?.suppressTaskLists && activeTasks && activeTasks.length > 0) {
    activeLines.push('### Active Tasks');
    const hasBlocked = activeTasks.some((t) => strVal(t.status) === 'BLOCKED');
    if (hasBlocked) {
      activeLines.push('NOTE: Tasks marked BLOCKED are stalled in-flight but not done — do NOT propose findings for problems they already address.');
    }
    for (const t of activeTasks) {
      const files = (t.related_files as string[] | undefined);
      const filesSuffix = files && files.length > 0 ? ` (files: ${files.join(', ')})` : '';
      activeLines.push(`- [${strVal(t.task_type)}] **${strVal(t.title)}** (${strVal(t.status)}) — ${strVal(t.prompt).slice(0, 400)}${filesSuffix}`);
    }
    activeLines.push('');
  }

  // Build open PRs section (always included in full)
  const prLines: string[] = [];
  const openPRs = ctx.open_prs as string[] | undefined;
  if (openPRs && openPRs.length > 0) {
    prLines.push('### Open PRs');
    for (const pr of openPRs) {
      prLines.push(`- ${pr}`);
    }
    prLines.push('');
  }

  // Build sibling tasks section (always included in full)
  const siblingLines: string[] = [];
  const siblingTasks = ctx.sibling_tasks as Array<Record<string, unknown>> | undefined;
  if (siblingTasks && siblingTasks.length > 0) {
    siblingLines.push('## Sibling Tasks (parallel tasks from the same parent — avoid duplicating their work)');
    for (const t of siblingTasks) {
      const status = strVal(t.status);
      const isActive = t.active === true;
      const failureClass = strVal(t.failure_class);
      const errorExcerpt = strVal(t.error_excerpt);
      const changedFiles = (t.changed_files as string[] | undefined);
      const relatedFiles = (t.related_files as string[] | undefined);
      let suffix = '';
      if (!isActive && (failureClass || errorExcerpt)) {
        const inner = failureClass && errorExcerpt
          ? `${failureClass}: ${errorExcerpt}`
          : (failureClass || errorExcerpt);
        suffix += ` [${inner}]`;
        if (changedFiles && changedFiles.length > 0) {
          suffix += ` (changed: ${changedFiles.join(', ')})`;
        } else if (relatedFiles && relatedFiles.length > 0) {
          suffix += ` (files: ${relatedFiles.join(', ')})`;
        }
      } else if (!isActive && changedFiles && changedFiles.length > 0) {
        suffix = ` (modified: ${changedFiles.join(', ')})`;
      } else if (relatedFiles && relatedFiles.length > 0) {
        suffix = ` (files: ${relatedFiles.join(', ')})`;
      }
      siblingLines.push(`- [${status}] ${strVal(t.title)}${suffix}`);
    }
    siblingLines.push('');
  }

  // Build co-change affinity section — always included for create/fix/refactor (concise, actionable)
  const cochangeFiles = ctx.cochange_files as string[] | undefined;
  const cochangeLines: string[] = [];
  if (cochangeFiles && cochangeFiles.length > 0) {
    cochangeLines.push('## Frequently Co-Changed Files');
    cochangeLines.push('Based on merged PRs in this project, these files are commonly modified together with your task\'s related files — review and update them if relevant:');
    for (const f of cochangeFiles) {
      cochangeLines.push(`- ${f}`);
    }
    if (taskScope === 'medium') {
      const remaining = Math.max(0, MEDIUM_ANALYSIS_PLAN_FILE_LIMIT - relatedFilesCount);
      cochangeLines.push(`Budget note: you have ${remaining} production file slot${remaining === 1 ? '' : 's'} remaining in the ${MEDIUM_ANALYSIS_PLAN_FILE_LIMIT}-file analysis plan budget — include co-changed files only if essential to correctness.`);
    }
    cochangeLines.push('');
  }

  const fixedContent = [...header, ...activeLines, ...prLines, ...siblingLines, ...cochangeLines].join('\n');
  let budget = CONTEXT_BUDGET - fixedContent.length;

  // Build lessons section first — lessons directly prevent mistakes and take priority over informational sections
  const lessonsData = ctx.lessons as Array<Record<string, unknown>> | undefined;
  let lessonsLines: string[] = [];
  if (lessonsData && lessonsData.length > 0) {
    const sortedLessons = currentDirectionId
      ? [...lessonsData].sort((a, b) => {
          const aMatch = strVal(a.direction_id) === currentDirectionId ? 0 : 1;
          const bMatch = strVal(b.direction_id) === currentDirectionId ? 0 : 1;
          return aMatch - bMatch;
        })
      : lessonsData;
    // Collapse near-duplicate lessons: same normalized last_feedback or same task_title + failure_class
    const seenFeedback = new Set<string>();
    const seenTitleClass = new Set<string>();
    const dedupedLessons = sortedLessons.filter((l) => {
      const feedbackKey = strVal(l.last_feedback).trim().toLowerCase();
      const titleClassKey = `${strVal(l.task_title)}|${strVal(l.failure_class)}`;
      if (feedbackKey && seenFeedback.has(feedbackKey)) return false;
      if (titleClassKey !== '|' && seenTitleClass.has(titleClassKey)) return false;
      if (feedbackKey) seenFeedback.add(feedbackKey);
      if (titleClassKey !== '|') seenTitleClass.add(titleClassKey);
      return true;
    });
    const candidateLines: string[] = ['### Lessons from Past Reviews'];
    for (const l of dedupedLessons) {
      const fcSuffix = l.failure_class ? ` [${strVal(l.failure_class)}]` : '';
      const dirSuffix = currentDirectionId && strVal(l.direction_id) === currentDirectionId ? ' [same direction]' : '';
      candidateLines.push(`- **${strVal(l.task_title)}** (${String(l.iterations)} cycles)${fcSuffix}${dirSuffix}: ${strVal(l.last_feedback)}`);
    }
    candidateLines.push('');
    const candidateContent = candidateLines.join('\n');
    if (candidateContent.length <= budget) {
      lessonsLines = candidateLines;
      budget -= candidateContent.length;
    }
  }

  // Build queued tasks section — one-liners only to preserve budget for higher-priority content
  const queuedTasks = ctx.queued_tasks as Array<Record<string, unknown>> | undefined;
  let queuedLines: string[] = [];
  if (!opts?.suppressTaskLists && queuedTasks && queuedTasks.length > 0) {
    const shortLines: string[] = ['### Queued Tasks'];
    for (const t of queuedTasks) {
      shortLines.push(`- [${strVal(t.task_type)}] **${strVal(t.title)}**`);
    }
    shortLines.push('');
    const shortContent = shortLines.join('\n');
    if (shortContent.length <= budget) {
      queuedLines = shortLines;
      budget -= shortContent.length;
    }
  }

  const renderCompletedTaskLine = (t: Record<string, unknown>): string => {
    const pr = t.pr_url ? ` (${strVal(t.pr_url)})` : '';
    const summary = t.research_findings_summary ? ` — ${parseResearchSummary(strVal(t.research_findings_summary))}` : '';
    const failureClass = t.failure_class ? ` [${strVal(t.failure_class)}]` : '';
    const failureSuffix = t.failure_reason ? ` (FAILED${failureClass}: ${strVal(t.failure_reason)})` : '';
    const reviewSuffix = (typeof t.review_iterations === 'number' && t.review_iterations > 1) ? ` (${t.review_iterations} review cycles)` : '';
    const extras: string[] = [];
    if (t.direction_id) extras.push(`dir=${strVal(t.direction_id)}`);
    if (t.scope) extras.push(`scope=${strVal(t.scope)}`);
    const relatedFiles = t.related_files;
    if (Array.isArray(relatedFiles) && relatedFiles.length > 0) {
      const files = relatedFiles.slice(0, 3).map((f) => strVal(f)).join(', ');
      extras.push(`files: ${files}${relatedFiles.length > 3 ? ` +${relatedFiles.length - 3}` : ''}`);
    }
    const changedFiles = t.changed_files;
    if (Array.isArray(changedFiles) && changedFiles.length > 0) {
      const files = changedFiles.slice(0, 3).map((f) => strVal(f)).join(', ');
      const label = t.failure_reason ? 'changed' : 'modified';
      extras.push(`${label}: ${files}${changedFiles.length > 3 ? ` +${changedFiles.length - 3}` : ''}`);
    }
    let metaSuffix = extras.length > 0 ? ` [${extras.join('; ')}]` : '';
    if (metaSuffix.length > 100) metaSuffix = metaSuffix.slice(0, 97) + '...]';
    let promptSuffix = '';
    if (t.prompt) {
      const p = strVal(t.prompt).replace(/\s+/g, ' ').trim();
      if (p.length > 0) {
        const excerpt = p.length > 80 ? p.slice(0, 77) + '...' : p;
        promptSuffix = `\n  prompt: ${excerpt}`;
      }
    }
    const landedMarker = t.landed_on_head === true ? ' [landed on HEAD]' : '';
    return `- [${strVal(t.task_type)}] **${strVal(t.title)}**${pr}${landedMarker}${summary}${failureSuffix}${reviewSuffix}${metaSuffix}${promptSuffix}`;
  };

  // Build direction-filtered recent tasks section — prioritized, shown before general recent
  const directionRecentTasks = ctx.direction_recent_tasks as Array<Record<string, unknown>> | undefined;
  let directionRecentLines: string[] = [];
  if (!opts?.suppressTaskLists && directionRecentTasks && directionRecentTasks.length > 0) {
    const headerLine = '### Recent Tasks in This Direction';
    const entries = directionRecentTasks.map(renderCompletedTaskLine);
    const candidate = [headerLine, ...entries, ''].join('\n');
    if (candidate.length <= budget) {
      directionRecentLines = [headerLine, ...entries, ''];
      budget -= candidate.length;
    }
  }

  // Build recently completed section — drop oldest entries to fit budget
  const recentTasks = ctx.recently_completed_tasks as Array<Record<string, unknown>> | undefined;
  let recentLines: string[] = [];
  let recentDropped = 0;
  if (!opts?.suppressTaskLists && recentTasks && recentTasks.length > 0) {
    const allEntries: string[] = recentTasks.map(renderCompletedTaskLine);

    // Keep most recent entries that fit within budget
    const headerLine = '### Recently Completed Tasks';
    const kept = allEntries.slice(); // most recent assumed to be last
    while (kept.length > 0) {
      const candidate = [headerLine, ...kept, ''].join('\n');
      if (candidate.length <= budget) break;
      kept.shift(); // drop oldest (first entry)
      recentDropped++;
    }
    if (kept.length > 0) {
      recentLines = [headerLine, ...kept, ''];
      budget -= recentLines.join('\n').length;
    } else {
      recentDropped = allEntries.length;
    }
  }

  // Build pipeline health section — only if budget allows, capped at 400 chars
  const pipelineHealth = ctx.pipeline_health as Record<string, unknown> | null | undefined;
  let healthLines: string[] = [];
  if (pipelineHealth) {
    const parts: string[] = [];

    const successRate = pipelineHealth.success_rate;
    const trend = pipelineHealth.trend as string | undefined;
    if (successRate !== null && successRate !== undefined) {
      const pct = Math.round((successRate as number) * 100);
      const trendStr = trend ? ` (${trend})` : '';
      parts.push(`Success rate: ${pct}%${trendStr}.`);
    }

    const fasr = pipelineHealth.first_attempt_success_rate as Record<string, unknown> | undefined;
    if (fasr && fasr.overall !== null && fasr.overall !== undefined && (fasr.sample_size as number) > 0) {
      const faPct = Math.round((fasr.overall as number) * 100);
      const faTrend = fasr.trend as string | undefined;
      const faTrendStr = faTrend ? ` (${faTrend})` : '';
      const byType = fasr.by_type as Partial<Record<string, number>> | undefined;
      let perTypeSuffix = '';
      const typeRate = taskType ? byType?.[taskType] : undefined;
      if (typeRate !== undefined) {
        const typePct = Math.round(typeRate * 100);
        perTypeSuffix = ` Your task type (${taskType}): ${typePct}%.`;
      }
      const byScope = fasr.by_scope as Partial<Record<string, number>> | undefined;
      let perScopeSuffix = '';
      const scopeRate = taskScope ? byScope?.[taskScope] : undefined;
      if (scopeRate !== undefined) {
        const scopePct = Math.round(scopeRate * 100);
        perScopeSuffix = ` Your scope (${taskScope}): ${scopePct}%.`;
      }
      parts.push(`First-attempt success: ${faPct}%${faTrendStr}, sample: ${String(fasr.sample_size)}.${perTypeSuffix}${perScopeSuffix}`);
    }

    const failureDist = pipelineHealth.failure_distribution as Record<string, number> | undefined;
    if (failureDist && Object.keys(failureDist).length > 0) {
      const failStr = Object.entries(failureDist)
        .map(([cls, cnt]) => `${cls} (${cnt})`)
        .join(', ');
      parts.push(`Top failures: ${failStr}.`);
    }

    const dirStats = pipelineHealth.direction_stats as Array<Record<string, unknown>> | undefined;
    if (dirStats && dirStats.length > 0) {
      const dirStr = dirStats
        .map((d) => {
          const did = strVal(d.direction_id).slice(-8);
          const sat = d.possibly_saturated ? ', saturated' : '';
          const cvr = typeof d.conversion_rate === 'number' || typeof d.conversion_rate === 'string'
            ? String(d.conversion_rate)
            : 'n/a';
          const proven = Number(d.proven_prs_merged ?? 0);
          const consumed = Number(d.consumed_prs_merged ?? 0);
          const proof = proven > 0 || consumed > 0
            ? `, proof ${proven} proven${consumed > 0 ? ` (${consumed} consumed downstream)` : ''}`
            : '';
          return `d-${did} cvr ${cvr} (${String(d.cycles)} cycles${sat}${proof})`;
        })
        .join(', ');
      parts.push(`Direction ROI: ${dirStr}.`);
    }

    if (taskType === 'review') {
      const reviewAccuracy = pipelineHealth.review_accuracy as Record<string, unknown> | null | undefined;
      if (reviewAccuracy) {
        const precision = reviewAccuracy.precision as number;
        const overrides = reviewAccuracy.overrides as number;
        const precPct = Math.round(precision * 100);
        const recall = reviewAccuracy.recall as number | null | undefined;
        const misses = reviewAccuracy.misses as number;
        let accuracyStr = `Review accuracy: ${precPct}% precision (${overrides} overrides)`;
        if (recall !== null && recall !== undefined) {
          const recPct = Math.round(recall * 100);
          accuracyStr += `, ${recPct}% recall (${misses} user-caught issues)`;
        }
        accuracyStr += '.';
        parts.push(accuracyStr);
      }
    }

    // Persistent review issues: categories above 20% for 3+ consecutive 14-day windows
    const persistentIssues = pipelineHealth.persistent_review_issues as string[] | null | undefined;
    if (persistentIssues && persistentIssues.length > 0) {
      parts.push(`Persistent review issues (structural fix needed): ${persistentIssues.join(', ')}.`);
    }

    if (parts.length > 0) {
      const body = parts.join(' ');
      const candidate = `### Pipeline Health (last 24h)\n${body}\n`;
      if (candidate.length <= 500 && candidate.length <= budget) {
        healthLines = ['### Pipeline Health (last 24h)', body, ''];
        budget -= candidate.length;
      }
    }
  }

  // Build review issue distribution section — only if budget allows
  const reviewIssueDist = ctx.review_issue_distribution as Record<string, number> | null | undefined;
  // First-attempt-specific breakdown from pipeline health (more actionable than all-iterations data)
  const fasrForReview = pipelineHealth?.first_attempt_success_rate as Record<string, unknown> | undefined;
  const firstAttemptByCat = fasrForReview?.first_attempt_by_review_category as Partial<Record<string, number>> | null | undefined;
  // Review effectiveness: per-category trend data from two consecutive 14-day windows
  type ReviewEffectivenessCategory = { trend?: string };
  const reviewEffectiveness = ctx.review_effectiveness as { categories?: Record<string, ReviewEffectivenessCategory> } | null | undefined;
  const reviewEffCats = reviewEffectiveness?.categories;
  let reviewIssueLines: string[] = [];
  if (reviewIssueDist && Object.keys(reviewIssueDist).length > 0) {
    const hasFirstAttemptCats = firstAttemptByCat && Object.keys(firstAttemptByCat).length > 0;
    const descLine = hasFirstAttemptCats
      ? 'Issues flagged in this project\'s review cycles — first-attempt rejection rate shown in parentheses (prioritize these before submitting):'
      : 'Issues flagged most often in this project\'s review cycles (prioritize these before submitting):';
    const candidateLines = ['### Common Review Issues', descLine];
    const entries = Object.entries(reviewIssueDist).sort((a, b) => b[1] - a[1]);
    for (const [cat, pct] of entries) {
      const faRate = hasFirstAttemptCats ? firstAttemptByCat[cat] : undefined;
      const faPct = faRate !== undefined
        ? ` (${Math.round(faRate * 100)}% of first-attempt rejections)`
        : '';
      const trend = reviewEffCats?.[cat]?.trend;
      const trendIndicator = trend === 'decreasing' ? ', trending down'
        : trend === 'increasing' ? ', trending up ⚠'
        : trend === 'stable' ? ', persistent'
        : '';
      candidateLines.push(`- **${cat}**: ${Math.round(pct * 100)}% of review feedback${faPct}${trendIndicator}`);
    }
    // Append specific recurring patterns if available
    type FirstReviewIssue = { description: string; count: number; category: string };
    const topIssues = ctx.top_first_review_issues as FirstReviewIssue[] | null | undefined;
    if (topIssues && topIssues.length > 0) {
      candidateLines.push('**Specific patterns to avoid:**');
      for (const issue of topIssues) {
        candidateLines.push(`- "${issue.description}" (seen ${issue.count}x, category: ${issue.category})`);
      }
    }
    candidateLines.push('');
    const candidate = candidateLines.join('\n');
    if (candidate.length <= budget) {
      reviewIssueLines = candidateLines;
      budget -= candidate.length;
    }
  }

  // Build SR catch-rate blind spots block — only if budget allows
  type SRCatchRateExample = { severity: string; desc: string };
  type SRCatchRateCategory = {
    miss_rate: number;
    weighted_miss_rate?: number;
    examples?: SRCatchRateExample[];
    current_hit_rate?: number;
    previous_hit_rate?: number;
    trend?: string;
    rate_history?: number[];
    false_positive_rate?: number;
    precision?: number;
    sr_flag_sample_size?: number;
    engagement_thinning?: boolean;
    below_floor?: boolean;
  };
  const srCatchRate = ctx.self_review_catch_rate as {
    categories?: Record<string, SRCatchRateCategory>;
    for_task_type?: Record<string, SRCatchRateCategory>;
  } | null | undefined;
  let srBlindSpotLines: string[] = [];
  // Prefer task-type-specific rates when available — they reflect miss rates for this task type only
  const forTaskType = srCatchRate?.for_task_type;
  const srSource = (forTaskType && Object.keys(forTaskType).length > 0) ? forTaskType : srCatchRate?.categories;
  const srIsTypeFiltered = srSource === forTaskType && !!forTaskType;
  if (srSource && Object.keys(srSource).length > 0) {
    // Qualifying = categories with active misses OR engagement thinning (reasoning rate dropping).
    // Thinning categories carry the same reasoning requirement as regressing ones — preventive,
    // before the miss-rate officially regresses.
    const qualifying = Object.entries(srSource)
      .filter(([, data]) =>
        (typeof data.miss_rate === 'number' && data.miss_rate > 0) ||
        data.engagement_thinning === true
      )
      // sort by severity-weighted miss rate when available, so high-impact misses surface over trivial ones
      .sort((a, b) => (b[1].weighted_miss_rate ?? b[1].miss_rate) - (a[1].weighted_miss_rate ?? a[1].miss_rate))
      .slice(0, 5);
    if (qualifying.length > 0) {
      const scopeNote = srIsTypeFiltered && taskType
        ? ` (${taskType} tasks only)`
        : '';

      const buildTrajectoryHint = (data: SRCatchRateCategory): string => {
        if (typeof data.previous_hit_rate === 'number' && typeof data.current_hit_rate === 'number') {
          const prevMissRate = Math.round((1 - data.previous_hit_rate) * 100);
          const currMissRate = Math.round((1 - data.current_hit_rate) * 100);
          const delta = currMissRate - prevMissRate;
          const trend = data.trend === 'decreasing' ? 'regressing' : delta > 0 ? 'regressing' : 'improving';
          const sign = delta > 0 ? '−' : '+';
          const absDelta = Math.abs(delta);
          return ` (last cycle: ${prevMissRate}% → now: ${currMissRate}%, ${sign}${absDelta}pp ${trend})`;
        }
        return '';
      };

      const buildCatLines = (withExamples: boolean): string[] => {
        const lines: string[] = [
          '### Self-Review Blind Spots',
          `Your past self-reviews missed issues in these categories${scopeNote} — apply extra scrutiny. For EACH of these categories, add a \`reasoning\` field to your \`check_results\` entry (~1 sentence: what you inspected and what evidence supports the pass/fail call). Required for both pass and fail outcomes. Categories marked ⚠ Thinning carry the same requirement preventively — their reasoning engagement is dropping and they are at risk of becoming regressing before the next measurement cycle. Examples from recent tasks:`,
        ];
        for (const [cat, data] of qualifying) {
          const rawPct = Math.round(data.miss_rate * 100);
          const trajectoryHint = buildTrajectoryHint(data);
          const trendWarning = data.trend === 'decreasing' ? ' ⚠ Regression: scrutinize extra carefully.' : '';
          const thinningWarning = data.engagement_thinning === true && data.trend !== 'decreasing' ? ' ⚠ Thinning: reasoning engagement dropping — treat same as regressing.' : '';
          const suffix = data.weighted_miss_rate !== undefined
            ? ` (${rawPct}% missed, ${Math.round(data.weighted_miss_rate * 100)}% severity-weighted${trajectoryHint}):${trendWarning}${thinningWarning}`
            : ` (${rawPct}% missed${trajectoryHint}):${trendWarning}${thinningWarning}`;
          lines.push(`- ${cat}${suffix}`);
          if (withExamples && data.examples && data.examples.length > 0) {
            for (const ex of data.examples) {
              lines.push(`  - [${ex.severity}] ${ex.desc}`);
            }
          } else if (!withExamples || !data.examples || data.examples.length === 0) {
            // fallback: no sub-bullets for this category
          }
        }
        lines.push('');
        return lines;
      };

      // Try full block with examples first; fall back to percentages-only if over budget
      for (const withEx of [true, false]) {
        const candidateLines = buildCatLines(withEx);
        const candidate = candidateLines.join('\n');
        if (candidate.length <= budget) {
          srBlindSpotLines = candidateLines;
          budget -= candidate.length;
          break;
        }
      }
    }
  }

  // Build direction-scoped SR calibration block — sibling to the project-wide
  // Self-Review Blind Spots section. Backend (agent.py) only injects this field
  // when task has direction_id and there is enough signal for that direction.
  type SRCatchRateForDirection = {
    direction_id?: string;
    direction_text?: string;
    sample_count?: number;
    miss_rate?: number;
    categories?: Record<string, SRCatchRateCategory>;
  };
  const srForDirection = ctx.self_review_catch_rate_for_direction as SRCatchRateForDirection | null | undefined;
  let srForDirectionLines: string[] = [];
  if (srForDirection && srForDirection.categories && Object.keys(srForDirection.categories).length > 0) {
    const dirCats = srForDirection.categories;
    const dirQualifying = Object.entries(dirCats)
      .filter(([, data]) => typeof data.miss_rate === 'number' && data.miss_rate > 0)
      .sort((a, b) => (b[1].weighted_miss_rate ?? b[1].miss_rate) - (a[1].weighted_miss_rate ?? a[1].miss_rate))
      .slice(0, 5);
    if (dirQualifying.length > 0) {
      const dirLabel = srForDirection.direction_text
        ? `"${srForDirection.direction_text.slice(0, 100)}"`
        : (srForDirection.direction_id ?? 'this direction');
      const sampleSuffix = typeof srForDirection.sample_count === 'number'
        ? ` (${srForDirection.sample_count} task${srForDirection.sample_count === 1 ? '' : 's'} sampled)`
        : '';

      const buildDirLines = (withExamples: boolean): string[] => {
        const lines: string[] = [
          '### For This Direction: SR Calibration',
          `In this direction (${dirLabel})${sampleSuffix}, your past self-reviews missed issues in these categories — apply extra scrutiny here:`,
        ];
        for (const [cat, data] of dirQualifying) {
          const rawPct = Math.round(data.miss_rate * 100);
          const suffix = data.weighted_miss_rate !== undefined
            ? ` (${rawPct}% missed, ${Math.round(data.weighted_miss_rate * 100)}% severity-weighted)`
            : ` (${rawPct}% missed)`;
          lines.push(`- ${cat}${suffix}`);
          if (withExamples && data.examples && data.examples.length > 0) {
            for (const ex of data.examples) {
              lines.push(`  - [${ex.severity}] ${ex.desc}`);
            }
          }
        }
        lines.push('');
        return lines;
      };

      for (const withEx of [true, false]) {
        const candidateLines = buildDirLines(withEx);
        const candidate = candidateLines.join('\n');
        if (candidate.length <= budget) {
          srForDirectionLines = candidateLines;
          budget -= candidate.length;
          break;
        }
      }
    }
  }

  // Build SR False-Positive Calibration block — only if budget allows.
  // Surfaces categories where the agent's self-review has been over-flagging
  // (high FP rate) so the agent can apply lighter scrutiny there. Phrasing
  // explicitly nudges selectivity without weakening genuine catches.
  let srFPCalibrationLines: string[] = [];
  if (srSource && Object.keys(srSource).length > 0) {
    const fpQualifying = Object.entries(srSource)
      .filter(([, data]) =>
        typeof data.false_positive_rate === 'number'
        && data.false_positive_rate >= 0.5
        && typeof data.sr_flag_sample_size === 'number'
        && data.sr_flag_sample_size >= 10,
      )
      .sort((a, b) => (b[1].false_positive_rate ?? 0) - (a[1].false_positive_rate ?? 0))
      .slice(0, 5);
    if (fpQualifying.length > 0) {
      const fpScopeNote = srIsTypeFiltered && taskType
        ? ` (${taskType} tasks only)`
        : '';

      const buildFPCatLines = (verbose: boolean): string[] => {
        const lines: string[] = [
          '### Self-Review False-Positive Calibration',
          `Categories where past self-reviews have flagged issues that human review did not confirm${fpScopeNote}. Apply lighter scrutiny here unless evidence is concrete — but do not weaken genuine catches.`,
        ];
        for (const [cat, data] of fpQualifying) {
          const fpPct = Math.round((data.false_positive_rate ?? 0) * 100);
          const sample = data.sr_flag_sample_size ?? 0;
          const precisionPct = typeof data.precision === 'number'
            ? Math.round(data.precision * 100)
            : Math.round((1 - (data.false_positive_rate ?? 0)) * 100);
          if (verbose) {
            lines.push(
              `- **${cat}**: flagged in ${sample} recent SR checks, only ${precisionPct}% confirmed by review (${fpPct}% false-positive rate). Apply lighter scrutiny here unless evidence is concrete — but do not weaken genuine catches.`,
            );
          } else {
            lines.push(`- **${cat}**: ${fpPct}% false-positive (precision ${precisionPct}%, sample ${sample})`);
          }
        }
        lines.push('');
        return lines;
      };

      for (const verbose of [true, false]) {
        const candidateLines = buildFPCatLines(verbose);
        const candidate = candidateLines.join('\n');
        if (candidate.length <= budget) {
          srFPCalibrationLines = candidateLines;
          budget -= candidate.length;
          break;
        }
      }
    }
  }

  // Build project-wide SR blind-spot themes (last 14 days) — only if budget allows.
  // Surfaced from backend agent.py injection: ctx.sr_blind_spot_themes is
  // [{theme, count}, ...] sorted by count desc, capped at 8.
  const srBlindSpotThemesRaw = ctx.sr_blind_spot_themes as Array<{ theme: string; count: number }> | null | undefined;
  let srBlindSpotThemesLines: string[] = [];
  if (Array.isArray(srBlindSpotThemesRaw) && srBlindSpotThemesRaw.length > 0) {
    const rendered = formatProjectBlindSpotSummary(srBlindSpotThemesRaw);
    if (rendered) {
      const candidateLines = rendered.split('\n');
      const candidate = candidateLines.join('\n');
      if (candidate.length <= budget) {
        srBlindSpotThemesLines = candidateLines;
        budget -= candidate.length;
      }
    }
  }

  // Build SR recent miss examples block — concrete examples from the last cycle, placed after
  // the themes section to deepen the signal from generic themes to specific instances.
  // Reads `sr_recent_miss_examples` injected by backend agent.py (shape: {cat: [{severity, desc}]}).
  const srRecentMissExamples = ctx.sr_recent_miss_examples as
    | Record<string, Array<{ desc: string; severity: string }>>
    | null
    | undefined;
  let srRecentMissLines: string[] = [];
  if (srRecentMissExamples) {
    const rendered = formatSrRecentMissExamplesForSrPrompt(srRecentMissExamples);
    if (rendered) {
      const candidateLines = rendered.split('\n');
      const candidate = candidateLines.join('\n');
      if (candidate.length <= budget) {
        srRecentMissLines = candidateLines;
        budget -= candidate.length;
      }
    }
  }

  // Build per-module review feedback warnings — only if budget allows
  type ModuleFeedbackWarning = { file: string; top_categories: Record<string, number>; sample_size: number };
  const moduleFeedbackWarnings = ctx.module_feedback_warnings as ModuleFeedbackWarning[] | null | undefined;
  let moduleFeedbackLines: string[] = [];
  if (moduleFeedbackWarnings && moduleFeedbackWarnings.length > 0) {
    const candidateLines: string[] = ['### Module-Specific Review History'];
    for (const w of moduleFeedbackWarnings) {
      const catParts = Object.entries(w.top_categories)
        .sort((a, b) => b[1] - a[1])
        .map(([cat, pct]) => `${Math.round(pct * 100)}% ${cat}`)
        .join(', ');
      candidateLines.push(`- When modifying \`${w.file}\`, review history shows: ${catParts} (${w.sample_size} issues)`);
    }
    candidateLines.push('');
    const candidate = candidateLines.join('\n');
    if (candidate.length <= budget) {
      moduleFeedbackLines = candidateLines;
      budget -= candidate.length;
    }
  }

  // Build direction quality warning — only if budget allows, placed after Pipeline Health
  const directionQuality = ctx.direction_quality as Record<string, unknown> | null | undefined;
  let dirQualityLines: string[] = [];
  if (directionQuality) {
    const rate = directionQuality.acceptance_rate as number;
    const merged = directionQuality.prs_merged as number;
    const rejected = directionQuality.prs_rejected as number;
    const avgTtm = directionQuality.avg_time_to_merge_hours as number | null | undefined;
    const rejCtx = directionQuality.rejection_context as Array<Record<string, unknown>> | undefined;

    const qualityParts: string[] = [
      `Operator acceptance rate for this direction: ${Math.round(rate * 100)}% (${merged} merged, ${rejected} rejected)`,
    ];
    if (avgTtm !== null && avgTtm !== undefined) {
      qualityParts.push(`Average time for operators to merge PRs in this direction: ${avgTtm}h`);
    }
    if (rejCtx && rejCtx.length > 0) {
      qualityParts.push('Recent rejections:');
      for (const r of rejCtx) {
        qualityParts.push(`- "${strVal(r.title)}" — ${strVal(r.reason)}`);
      }
    }
    qualityParts.push('Pay extra attention to code quality, test coverage, and alignment with project conventions.');

    const candidateLines = ['### Direction Quality Warning', ...qualityParts, ''];
    const candidate = candidateLines.join('\n');
    if (candidate.length <= budget) {
      dirQualityLines = candidateLines;
      budget -= candidate.length;
    }
  }

  // Append footer if anything was omitted
  const footerLines: string[] = [];
  const omittedCount = recentDropped;
  if (omittedCount > 0) {
    footerLines.push(`_(${omittedCount} more tasks not shown)_`);
    footerLines.push('');
  }

  const allLines = [...header, ...activeLines, ...lessonsLines, ...queuedLines, ...directionRecentLines, ...recentLines, ...healthLines, ...reviewIssueLines, ...srBlindSpotLines, ...srForDirectionLines, ...srFPCalibrationLines, ...srBlindSpotThemesLines, ...srRecentMissLines, ...moduleFeedbackLines, ...dirQualityLines, ...prLines, ...siblingLines, ...cochangeLines, ...footerLines];
  return allLines.join('\n');
}
