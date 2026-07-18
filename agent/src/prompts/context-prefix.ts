import { strVal, relativeTimeFromIso } from '../prompt.js';
import { formatProjectContext } from './project-context.js';

// Task types whose output ships through the CI-green + reviewer-sign-off
// code-review merge gate, mirroring backend CODE_REVIEW_GATED_TASK_TYPES
// (task_capabilities.py — the registry-derived set of capabilities with
// evaluation == "code-ci-review"). The two learning signals below
// (self-review calibration + first-review rejection patterns) are only
// meaningful for reviewed code work, so both render for exactly these types.
// Cannot import the Python frozenset cross-language; kept as a mirrored literal.
export const REVIEW_GATED_TASK_TYPES = ['create', 'fix', 'refactor', 'migrate', 'qa', 'docs', 'deps', 'salvage_bundle'];

// Utility task types that should receive only durable operator steering
// (user_purpose + constraints) and NOT the rolling agent current_focus narrative.
// Injecting current_focus into these tasks causes steering-context hijack: the
// utility agent follows the crisis narrative in current_focus instead of doing
// its actual job (incident 2026-06-11: repo_bootstrap agent produced a
// qualification-pipeline investigation report instead of the bootstrap overview).
const UTILITY_TASK_TYPES = new Set(['repo_bootstrap']);

type SRCatchRateSource = {
  categories?: Record<string, { miss_rate?: number; weighted_miss_rate?: number; current_hit_rate?: number; hit_rate?: number; below_floor?: boolean }>;
  by_task_type?: Record<string, Record<string, { miss_rate?: number; weighted_miss_rate?: number; current_hit_rate?: number; hit_rate?: number; below_floor?: boolean }>>;
  for_task_type?: Record<string, { miss_rate?: number; weighted_miss_rate?: number; current_hit_rate?: number; hit_rate?: number; below_floor?: boolean }>;
  prompt_version_changed?: boolean;
  // True when the measurement window spans multiple SR prompt versions. The
  // calibration surface is still surfaced (unlike prompt_version_changed which
  // fully suppresses it) but a caveat is prepended so the agent knows the
  // percentages may conflate regimes and should not be treated as a clean signal.
  mixed_prompt_versions?: boolean;
};

export function formatSelfReviewCalibrationSection(source: SRCatchRateSource | undefined, taskType = ''): string {
  if (!source || source.prompt_version_changed) return '';
  const categories = taskType ? (source.by_task_type?.[taskType] ?? source.for_task_type) : source.categories;
  if (!categories) return '';
  const rows = Object.entries(categories)
    .map(([category, data]) => ({ category, data, miss: typeof data.miss_rate === 'number' ? data.miss_rate : typeof data.hit_rate === 'number' ? 1 - data.hit_rate : typeof data.current_hit_rate === 'number' ? 1 - data.current_hit_rate : -1 }))
    .filter(({ data, miss }) => miss >= 0.5 && (taskType ? data.below_floor !== false : true))
    .sort((a, b) => (b.data.weighted_miss_rate ?? b.miss) - (a.data.weighted_miss_rate ?? a.miss))
    .slice(0, 2);
  if (rows.length === 0) return '';
  const caveat = source.mixed_prompt_versions
    ? '> Note: this calibration window spans multiple SR prompt versions — percentages may conflate regimes and are directional only.\n'
    : '';
  return `## Self-review calibration\n${caveat}Historical pattern for this task type:\n${rows.map(({ category, miss }) => `- Historical pattern: ${category} is missing ${Math.round(miss * 100)}% of issues a reviewer catches on this project. Scrutinize this category extra carefully.`).join('\n')}\n\n`;
}

// Backend shape: routers/agent.py builds this from
// app/rejection_patterns.py:build_rejection_patterns_block — the claiming
// task type's top first-review rejection categories over a recent
// time-decayed window, one line of guidance each.
export type RejectionPatternsSource = {
  task_type?: string;
  window_days?: number;
  sample_size?: number;
  // Entries are typed nullable because the block crosses the backend→agent
  // wire — render defensively rather than trusting the shape.
  categories?: Array<{
    category?: string;
    weight?: number;
    task_count?: number;
    share?: number;
    guidance?: string;
  } | null | undefined>;
};

// Render the per-type rejection-patterns block injected by the backend at
// claim time. Framed explicitly as context, NOT a checklist: these are
// observed frequencies from OTHER tasks' first reviews — the agent must not
// over-index on the listed categories or treat them as findings about its
// own change.
export function formatRejectionPatternsSection(
  source: RejectionPatternsSource | null | undefined,
  taskType = '',
): string {
  if (!source || typeof source !== 'object' || !Array.isArray(source.categories)) return '';
  const rows = source.categories
    .filter((c): c is { category: string; share?: number; task_count?: number; guidance?: string } =>
      !!c && typeof c === 'object' && typeof c.category === 'string' && c.category.length > 0)
    .slice(0, 3);
  if (rows.length === 0) return '';
  const windowDays = typeof source.window_days === 'number' && source.window_days > 0
    ? source.window_days
    : 7;
  const sampleSuffix = typeof source.sample_size === 'number' && source.sample_size > 0
    ? ` (${source.sample_size} rejected first attempts)`
    : '';
  const typeLabel = (typeof source.task_type === 'string' && source.task_type) || taskType || 'similar';
  const lines = rows.map((c) => {
    const pct = typeof c.share === 'number' && c.share > 0
      ? ` (~${Math.round(c.share * 100)}% of recent rejections)`
      : '';
    const guidance = typeof c.guidance === 'string' && c.guidance ? ` — ${c.guidance}` : '';
    return `- **${c.category}**${pct}${guidance}`;
  });
  return `## Recent rejection patterns (context, not a checklist)\nOver the last ${windowDays} days${sampleSuffix}, first review most often rejected ${typeLabel} tasks on this project for:\n${lines.join('\n')}\n\nThese are observed frequencies from other tasks, not findings about your change. Address this task's actual requirements first; give the categories above an honest pass, but do not over-index on them or pad your work to pre-empt every listed category.\n\n`;
}

// Detect test files across Python/JS/TS conventions used in this repo.
function isTestFilePath(f: string): boolean {
  return /(?:^|\/)(?:tests?|testing|__tests__)\//i.test(f)
    || /(?:^|\/)test_[^/]+$/i.test(f)
    || /(?:^|\/)[^/]+\.(?:test|spec)\.[a-z]+$/i.test(f)
    || /_test\.[a-z]+$/i.test(f);
}

// Synthesize concise tactical guidance from scope_success_rates and
// direction_{prior,successful}_analysis. Returns up to 2 sentences.
export function synthesizeSuccessPatterns(
  dirCtx: Record<string, unknown>,
  taskScope: string,
): string[] {
  const sentences: string[] = [];

  const scopeRates = dirCtx.scope_success_rates as Record<string, Record<string, unknown>> | undefined;
  if (scopeRates && typeof scopeRates === 'object' && taskScope) {
    const rateFor = (scope: string) => {
      const s = scopeRates[scope] as Record<string, unknown> | undefined;
      if (!s) return null;
      const created = Number(s.created ?? 0);
      const merged = Number(s.merged ?? 0);
      if (created < 1) return null;
      return { merged, created, pct: Math.round((merged / created) * 100) };
    };
    const current = rateFor(taskScope);
    if (current && current.pct < 50) {
      let best: { scope: string; pct: number; merged: number; created: number } | null = null;
      for (const [scope, counts] of Object.entries(scopeRates)) {
        if (scope === taskScope) continue;
        const created = Number(counts.created ?? 0);
        const merged = Number(counts.merged ?? 0);
        if (created < 2) continue;
        const pct = Math.round((merged / created) * 100);
        if (!best || pct > best.pct) best = { scope, pct, merged, created };
      }
      if (best && best.pct - current.pct >= 20) {
        sentences.push(
          `${taskScope}-scope tasks in this direction merge at ${current.pct}% (${current.merged}/${current.created}) while ${best.scope}-scope tasks merge at ${best.pct}% (${best.merged}/${best.created}) — consider constraining your changes to the smallest viable surface.`,
        );
      }
    }
  }

  const successful = Array.isArray(dirCtx.direction_successful_analysis)
    ? dirCtx.direction_successful_analysis as Array<Record<string, unknown>>
    : [];
  const failed = Array.isArray(dirCtx.direction_prior_analysis)
    ? dirCtx.direction_prior_analysis as Array<Record<string, unknown>>
    : [];
  const entryHasTest = (e: Record<string, unknown>): boolean => {
    const files = Array.isArray(e.pr_changed_files) ? e.pr_changed_files as unknown[] : [];
    return files.some(f => typeof f === 'string' && isTestFilePath(f));
  };
  if (successful.length >= 2 && failed.length >= 1) {
    const succWithTests = successful.filter(entryHasTest).length;
    const failWithTests = failed.filter(entryHasTest).length;
    const succRatio = succWithTests / successful.length;
    const failRatio = failWithTests / failed.length;
    if (succRatio >= 0.75 && failRatio <= 0.25) {
      sentences.push(
        'Successful tasks in this direction consistently touch test files while failed tasks typically did not — add or update tests for the behavior you change.',
      );
    }
  }

  return sentences;
}

// Aggregate pr_changed_files across failed prior-analysis entries to find
// files appearing in 2+ failures. Skips test files. Returns up to top 5.
export function synthesizeFailureHotspots(
  dirCtx: Record<string, unknown>,
): Array<{ file: string; count: number }> {
  const failed = Array.isArray(dirCtx.direction_prior_analysis)
    ? dirCtx.direction_prior_analysis as Array<Record<string, unknown>>
    : [];
  if (failed.length < 2) return [];
  const counts = new Map<string, number>();
  for (const entry of failed) {
    const files = Array.isArray(entry.pr_changed_files) ? entry.pr_changed_files as unknown[] : [];
    const seen = new Set<string>();
    for (const f of files) {
      const path = typeof f === 'string' ? f : '';
      if (!path || isTestFilePath(path) || seen.has(path)) continue;
      seen.add(path);
      counts.set(path, (counts.get(path) ?? 0) + 1);
    }
  }
  return Array.from(counts.entries())
    .filter(([, c]) => c >= 2)
    .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
    .slice(0, 5)
    .map(([file, count]) => ({ file, count }));
}

// Render a condensed direction-context section for co-batch tasks (3+ active
// siblings from the same direction). Omits verbose sections that duplicate what
// sibling tasks already receive (narrative, evidence, prior analysis, etc.),
// keeping only the structural metadata the agent needs to orient itself.
export function formatCondensedDirectionContext(dirCtx: Record<string, unknown>): string {
  const dcLines: string[] = ['## Direction Context (condensed — batch sibling)'];
  const title = strVal(dirCtx.title);
  if (title) dcLines.push(`**Goal:** ${title}`);
  const priority = strVal(dirCtx.priority);
  if (priority) dcLines.push(`**Priority:** ${priority}`);
  const implConstraints = strVal(dirCtx.implementation_constraints);
  if (implConstraints) dcLines.push(`**Implementation constraints:** ${implConstraints}`);

  // Contract — what "done" means
  const contract = dirCtx.contract as Record<string, unknown> | undefined;
  if (contract && typeof contract === 'object') {
    const completionCriteria = strVal(contract.completion_criteria);
    if (completionCriteria) dcLines.push(`**Completion criteria:** ${completionCriteria}`);
    const successEvidence = strVal(contract.success_evidence);
    if (successEvidence) dcLines.push(`**Success evidence:** ${successEvidence}`);
    const retirementCondition = strVal(contract.retirement_condition);
    if (retirementCondition) dcLines.push(`**Retirement condition:** ${retirementCondition}`);
  }

  // failed_approaches — compact list
  const failedApproaches = Array.isArray(dirCtx.failed_approaches) ? dirCtx.failed_approaches as unknown[] : [];
  if (failedApproaches.length > 0) {
    dcLines.push('**Avoid (already tried and failed):**');
    for (const fa of failedApproaches) {
      if (typeof fa === 'string') {
        dcLines.push(`- ${fa}`);
      } else if (fa && typeof fa === 'object') {
        const obj = fa as Record<string, unknown>;
        const text = strVal(obj.text);
        if (text) {
          const ftype = strVal(obj.failure_type);
          const parts = [`- ${text}`];
          if (ftype) parts.push(`[${ftype}]`);
          dcLines.push(parts.join(' '));
        }
      }
    }
  }

  // recent_outcomes — single line
  const recentOutcomes = Array.isArray(dirCtx.recent_outcomes) ? dirCtx.recent_outcomes as Array<Record<string, unknown>> : [];
  if (recentOutcomes.length > 0) {
    const parts = recentOutcomes.map(o => `${Number(o.prs_merged ?? 0)}/${Number(o.tasks_created ?? 0)} merged`);
    dcLines.push(`Recent cycles: ${parts.join(', ')}`);
  }

  // failure_stage_pattern
  const failureStagePattern = dirCtx.failure_stage_pattern as Record<string, unknown> | undefined;
  if (failureStagePattern) {
    const msg = strVal(failureStagePattern.message);
    if (msg) {
      const rate = Number(failureStagePattern.rate);
      const header = Number.isFinite(rate) && rate > 0
        ? `**Failure pattern (${Math.round(rate * 100)}% of recent failures):**`
        : `**Failure pattern:**`;
      dcLines.push(`${header} ${msg}`);
    }
  }

  // scope_success_rates — single-line summary only
  const scopeRates = dirCtx.scope_success_rates as Record<string, Record<string, unknown>> | undefined;
  if (scopeRates && typeof scopeRates === 'object') {
    const scopeOrder = ['small', 'medium', 'large', 'ci_failure'];
    const entries = Object.entries(scopeRates).sort(([a], [b]) => {
      const ai = scopeOrder.indexOf(a);
      const bi = scopeOrder.indexOf(b);
      return (ai === -1 ? 99 : ai) - (bi === -1 ? 99 : bi);
    });
    const parts: string[] = [];
    for (const [scope, counts] of entries) {
      const created = Number(counts.created ?? 0);
      const merged = Number(counts.merged ?? 0);
      if (created < 1) continue;
      const pct = Math.round((merged / created) * 100);
      const failed = Number(counts.failed ?? 0);
      const suffix = failed > 0 ? `, ${failed} failed` : '';
      parts.push(`${scope} ${merged}/${created} (${pct}%${suffix})`);
    }
    if (parts.length > 0) {
      dcLines.push(`Scope merge rates: ${parts.join(', ')}`);
    }
  }

  // review_iteration stats
  const rollingMedianIter = typeof dirCtx.review_iteration_rolling_median === 'number'
    ? dirCtx.review_iteration_rolling_median
    : null;
  if (rollingMedianIter !== null) {
    const lastCycleIter = typeof dirCtx.review_iteration_last_cycle === 'number'
      ? dirCtx.review_iteration_last_cycle
      : null;
    const lastCycleSuffix = lastCycleIter !== null && lastCycleIter !== rollingMedianIter
      ? ` (last cycle: ${lastCycleIter.toFixed(1)})`
      : '';
    dcLines.push(
      `Recent PRs on this direction averaged ${rollingMedianIter.toFixed(1)} review iterations${lastCycleSuffix}; tight scope merges faster.`,
    );
  }

  // failure_hotspots
  const failureHotspots = synthesizeFailureHotspots(dirCtx);
  if (failureHotspots.length > 0) {
    const parts = failureHotspots.map(h => `${h.file} (${h.count}x)`).join(', ');
    dcLines.push(`Files appearing in multiple recent failures: ${parts} — changes to these files need extra care.`);
  }

  // top 1 of top_first_review_issues
  type DirectionFirstReviewIssue = { description: string; count: number; category: string };
  const dirTopIssues = Array.isArray(dirCtx.top_first_review_issues)
    ? dirCtx.top_first_review_issues as DirectionFirstReviewIssue[]
    : [];
  if (dirTopIssues.length > 0) {
    const top = dirTopIssues[0];
    dcLines.push(`**Most common review issue in this direction:** "${top.description}" (seen ${top.count}x, category: ${top.category})`);
    if (dirTopIssues.length > 1) {
      dcLines.push(`Additional review issues exist — check full context via \`./fetch direction\` for the complete list.`);
    }
  }

  dcLines.push('');
  return dcLines.join('\n') + '\n';
}

export function formatProjectVision(vision: Record<string, unknown> | undefined): string {
  if (!vision || typeof vision !== 'object') return '';
  const lines: string[] = ['## Project Vision'];
  const purpose = strVal(vision.purpose);
  if (purpose) lines.push(`**Purpose:** ${purpose}`);
  const successCriteria = vision.success_criteria;
  if (typeof successCriteria === 'string' && successCriteria) {
    lines.push(`**Success criteria:** ${successCriteria}`);
  } else if (Array.isArray(successCriteria) && successCriteria.length > 0) {
    lines.push('**Success criteria:**');
    for (const sc of successCriteria) {
      const s = strVal(sc);
      if (s) lines.push(`- ${s}`);
    }
  }
  const priorities = Array.isArray(vision.priorities) ? vision.priorities as unknown[] : [];
  if (priorities.length > 0) {
    lines.push('**Priorities:**');
    for (const p of priorities) {
      const s = strVal(p);
      if (s) lines.push(`- ${s}`);
    }
  }
  const systems = Array.isArray(vision.systems) ? vision.systems as Array<Record<string, unknown>> : [];
  if (systems.length > 0) {
    lines.push('**Systems:**');
    for (const sys of systems) {
      const name = strVal(sys.name);
      if (!name) continue;
      const status = strVal(sys.status);
      const desc = strVal(sys.description);
      const statusSuffix = status ? ` (${status})` : '';
      const descSuffix = desc ? ` — ${desc}` : '';
      lines.push(`- **${name}**${statusSuffix}${descSuffix}`);
    }
  }
  const context = strVal(vision.context);
  if (context) lines.push(`**Context:** ${context}`);
  if (lines.length === 1) return '';
  lines.push('');
  return lines.join('\n') + '\n';
}

// Render the operator's current strategic intent (purpose + current_focus) as
// a distinct markdown section. Retained for the fix path (review-fix.ts), which
// assembles its prompt manually and never calls buildContextPrefix. NOT called
// inside buildContextPrefix itself — the canonical `## Strategic Intent` block
// below already surfaces task.strategic_intent (purpose, current focus, context,
// constraints), so calling this here too would double-render the same payload.
// Returns '' when the payload is absent or empty so no bare header is emitted.
// Render the repo_overview block shipped by the backend at claim time.
// Shows the required overview fields, optional bootstrap-enriched fields, and
// freshness/provenance signals when available. Presence-gated: returns '' when
// any required field is absent so the prompt is byte-identical for projects that
// never ran bootstrap.
export function formatRepoOverview(overview: unknown): string {
  if (!overview || typeof overview !== 'object') return '';
  const o = overview as Record<string, unknown>;
  const techStack = typeof o.tech_stack === 'string' ? o.tech_stack.trim() : '';
  const dirLayout = typeof o.directory_layout === 'string' ? o.directory_layout.trim() : '';
  const firstSteps = typeof o.first_steps_summary === 'string' ? o.first_steps_summary.trim() : '';
  if (!techStack || !dirLayout || !firstSteps) return '';
  const lines: string[] = ['## Repo Overview'];
  lines.push(`**Tech stack:** ${techStack}`);
  lines.push(`**Directory layout:** ${dirLayout}`);
  lines.push(`**First steps:** ${firstSteps}`);
  const entryPoints = typeof o.entry_points === 'string' ? o.entry_points.trim() : '';
  if (entryPoints) lines.push(`**Entry points:** ${entryPoints}`);
  const testLayout = typeof o.test_layout === 'string' ? o.test_layout.trim() : '';
  if (testLayout) lines.push(`**Test layout:** ${testLayout}`);
  const keyConventions = typeof o.key_conventions === 'string' ? o.key_conventions.trim() : '';
  if (keyConventions) lines.push(`**Key conventions:** ${keyConventions}`);
  const environmentRequirements = typeof o.environment_requirements === 'string' ? o.environment_requirements.trim() : '';
  if (environmentRequirements) lines.push(`**Environment:** ${environmentRequirements}`);
  const completedAt = typeof o.bootstrap_completed_at === 'string' ? o.bootstrap_completed_at.trim() : '';
  if (completedAt) lines.push(`**Bootstrapped:** ${completedAt}`);
  const provenance = o.provenance && typeof o.provenance === 'object'
    ? o.provenance as Record<string, unknown>
    : undefined;
  const refreshedAt = strVal(provenance?.refreshed_at);
  const sourceBranch = strVal(provenance?.source_branch);
  const sourceRevision = strVal(provenance?.source_revision);
  if (refreshedAt || sourceBranch || sourceRevision) {
    const parts: string[] = [];
    if (refreshedAt) parts.push(`refreshed ${refreshedAt}`);
    if (sourceBranch) parts.push(`branch ${sourceBranch}`);
    if (sourceRevision) parts.push(`revision ${sourceRevision}`);
    lines.push(`**Provenance:** ${parts.join('; ')}`);
  }
  lines.push('');
  return lines.join('\n') + '\n';
}

// Render the domain_overview block injected by the backend at claim time.
// The domain knowledge producer (SourceKind.REPO_DOMAIN) captures product/domain
// context during repo bootstrap; the backend injects it as {summary, body}.
// Presence-gated: returns '' when either required field is absent so the prompt
// is byte-identical for projects that never produced domain knowledge.
export function formatDomainOverview(overview: unknown): string {
  if (!overview || typeof overview !== 'object') return '';
  const o = overview as Record<string, unknown>;
  const summary = typeof o.summary === 'string' ? o.summary.trim() : '';
  const body = typeof o.body === 'string' ? o.body.trim() : '';
  if (!summary || !body) return '';
  return `## Domain Knowledge\n${summary}\n\n${body}\n\n`;
}

export function formatStrategicIntent(intent: Record<string, unknown> | undefined): string {
  if (!intent || typeof intent !== 'object') return '';
  const lines: string[] = ['## Operator Steering'];
  const currentFocus = strVal(intent.current_focus);
  if (currentFocus) lines.push(`**Current focus:** ${currentFocus}`);
  const purpose = strVal(intent.purpose);
  if (purpose) lines.push(`**Purpose:** ${purpose}`);
  if (lines.length === 1) return '';
  lines.push('');
  return lines.join('\n') + '\n';
}

export function buildContextPrefix(task: Record<string, unknown>, opts?: { suppressTaskLists?: boolean; nowMs?: number }): string {
  // Deterministic reference epoch for relative-time rendering (see the
  // "Avoid (already tried and failed)" block). undefined → relativeTimeFromIso
  // falls back to its live wall-clock default.
  const relativeTimeReference = typeof opts?.nowMs === 'number' && Number.isFinite(opts.nowMs)
    ? new Date(opts.nowMs)
    : undefined;
  const projectCtx = task.project_context as Record<string, unknown> | undefined;
  const payloads = Array.isArray(task.payloads) ? task.payloads as Record<string, unknown>[] : [];
  const payload = payloads[0] as Record<string, unknown> | undefined;
  const taskType = payload ? strVal(payload.type) : '';
  const taskContext = task.context as Record<string, unknown> | undefined;
  const taskScope = taskContext ? strVal(taskContext.scope) : '';

  let prefix = '';

  // Exact human-tool revisions explicitly shared with the Resident and copied
  // into a receipt-bound managed Task. Presence-gated for every other Task.
  const managedSource = taskContext?._resident_managed_task as Record<string, unknown> | undefined;
  if (managedSource && typeof managedSource === 'object') {
    const refs = Array.isArray(managedSource.artifact_refs)
      ? managedSource.artifact_refs as Array<Record<string, unknown>>
      : [];
    const sourceLines: string[] = ['## Resident Source Revisions'];
    sourceLines.push(
      'These are immutable, explicitly shared revisions from the frozen Resident turn. Retrieve and inspect the exact revision before relying on it.',
    );
    for (const ref of refs) {
      const id = strVal(ref.artifact_ref_id) || strVal(ref.id);
      const kind = strVal(ref.kind);
      const revision = typeof ref.revision === 'number'
        ? String(ref.revision)
        : strVal(ref.revision);
      const checksum = strVal(ref.checksum);
      const retrieval = ref.retrieval as Record<string, unknown> | undefined;
      const href = retrieval ? strVal(retrieval.href) : '';
      if (!id) continue;
      sourceLines.push(
        `- ${kind || 'artifact'} ${id} revision ${revision || '?'} checksum ${checksum || '?'}${href ? ` — GET ${href}` : ''}`,
      );
    }
    const evidence = Array.isArray(managedSource.evidence)
      ? managedSource.evidence.slice(0, 8) as Array<Record<string, unknown>>
      : [];
    for (const item of evidence) {
      const kind = strVal(item.kind);
      const sourceId = strVal(item.source_id);
      const revision = strVal(item.revision);
      const checksum = strVal(item.checksum);
      const inspectedAt = strVal(item.inspected_at);
      if (!sourceId) continue;
      sourceLines.push(
        `- evidence ${kind || 'source'} ${sourceId} revision ${revision || '?'} checksum ${checksum || '?'}${inspectedAt ? ` inspected ${inspectedAt}` : ''}`,
      );
    }
    sourceLines.push('');
    prefix += sourceLines.join('\n') + '\n';
  }

  // Project Vision — strategic "why" set by the strategic research agent.
  // Surfaced first so tactical agents can align their work to it.
  prefix += formatProjectVision(task.vision as Record<string, unknown> | undefined);

  // Strategic Intent — the LIVE, operator-editable problem-model steering
  // (purpose, current focus, project context framing, active constraints).
  // Surfaced separately from the legacy Project Vision; the backend sends at
  // most one of the two, so they never double-render.
  const strategicIntent = task.strategic_intent as Record<string, unknown> | undefined;
  if (strategicIntent && typeof strategicIntent === 'object') {
    const siLines: string[] = ['## Strategic Intent'];
    // Durable operator-owned purpose — set at onboarding and never overwritten by agents.
    // Rendered first and in a clearly labeled block so it reads as the immutable directive,
    // separate from the agent-volatile strategic purpose/focus below.
    const userPurpose = strVal(strategicIntent.user_purpose);
    if (userPurpose) siLines.push(`**Operator Purpose (immutable):** ${userPurpose}`);
    // Agent-volatile purpose — may have been rewritten by the strategic cycle.
    // Omit when it is identical to userPurpose (strategist has healed them to equal) to avoid
    // shipping the same string twice.
    const purpose = strVal(strategicIntent.purpose);
    if (purpose && purpose.trim() !== userPurpose.trim()) siLines.push(`**Purpose:** ${purpose}`);
    // Rolling current focus narrative — omitted for utility task types (repo_bootstrap, etc.)
    // that should act on durable steering only, not the strategist's volatile narrative.
    const currentFocus = strVal(strategicIntent.current_focus);
    if (currentFocus && !UTILITY_TASK_TYPES.has(taskType)) siLines.push(`**Current focus:** ${currentFocus}`);
    const ctx = strategicIntent.context as Record<string, unknown> | undefined;
    if (ctx && typeof ctx === 'object') {
      const ctxFields: Array<[string, string]> = [
        ['project_type', 'Project type'],
        ['users', 'Users'],
        ['team', 'Team'],
        ['deploy_cadence', 'Deploy cadence'],
        ['notes', 'Notes'],
      ];
      const ctxParts: string[] = [];
      for (const [key, label] of ctxFields) {
        const v = strVal(ctx[key]);
        if (v) ctxParts.push(`${label}: ${v}`);
      }
      if (ctxParts.length > 0) {
        siLines.push('**Context:**');
        for (const part of ctxParts) siLines.push(`- ${part}`);
      }
    }
    const constraints = Array.isArray(strategicIntent.constraints)
      ? (strategicIntent.constraints as unknown[]).map(c => strVal(c)).filter(Boolean)
      : [];
    if (constraints.length > 0) {
      siLines.push('**Constraints:**');
      for (const c of constraints) siLines.push(`- ${c}`);
    }
    if (siLines.length > 1) {
      siLines.push('');
      prefix += siLines.join('\n') + '\n';
    }
  }

  // Repo Overview — tech stack, directory layout, and first steps discovered
  // during repo_bootstrap. Presence-gated: no-op when absent.
  prefix += formatRepoOverview(task.repo_overview);

  // Domain Knowledge — product/domain context captured at bootstrap via
  // SourceKind.REPO_DOMAIN and injected by the backend at claim time.
  // Presence-gated: no-op when absent so existing prompts stay byte-identical.
  prefix += formatDomainOverview(task.domain_overview);

  // Existing knowledge documents — a compact, body-omitted index of
  // web_research / design output so the strategist / research agent knows what
  // knowledge already exists (and can fetch full bodies on demand) instead of
  // re-commissioning research it already paid for. Rendered only when the
  // backend shipped a non-empty index; absent otherwise (clean omission).
  const knowledgeIndex = Array.isArray(payload?.knowledge_index)
    ? payload.knowledge_index as Array<Record<string, unknown>>
    : [];
  if (knowledgeIndex.length > 0) {
    const kdLines: string[] = ['## Existing knowledge documents'];
    kdLines.push(
      'Knowledge already produced by prior web_research / design work. Build on it — fetch the full body on demand with `./fetch knowledge <id>` rather than re-deriving it.',
    );
    for (const entry of knowledgeIndex) {
      const id = strVal(entry.id);
      const title = strVal(entry.title);
      if (!id || !title) continue;
      const meta: string[] = [];
      const topic = strVal(entry.topic);
      if (topic) meta.push(`topic: ${topic}`);
      const directionId = strVal(entry.direction_id);
      if (directionId) meta.push(`direction: ${directionId}`);
      const sourceKind = strVal(entry.source_kind);
      if (sourceKind) meta.push(sourceKind);
      const lifecycleStatus = strVal(entry.lifecycle_status);
      if (lifecycleStatus && lifecycleStatus !== 'pending') {
        meta.push(`lifecycle: ${lifecycleStatus}`);
      }
      const metaSuffix = meta.length > 0 ? ` (${meta.join(', ')})` : '';
      let badges = '';
      if (lifecycleStatus === 'pending') {
        badges += ' [PENDING]';
      }
      if (sourceKind === 'compute_experiment') {
        const criterionMet = entry.criterion_met;
        const unavailableCause = strVal(entry.unavailable_cause);
        let verdict: string;
        if (unavailableCause) {
          verdict = `analysis_unavailable: ${unavailableCause}`;
        } else if (criterionMet === true) {
          verdict = 'MET';
        } else if (criterionMet === false) {
          verdict = 'NOT MET';
        } else {
          verdict = 'UNMEASURED';
        }
        badges += ` [${verdict}]`;
        if (!unavailableCause) {
          const grade = strVal(entry.compute_acceptance_grade);
          const acceptanceReason = strVal(entry.compute_acceptance_reason);
          if (grade === 'acceptance') {
            badges += ' [acceptance]';
          } else if (acceptanceReason) {
            badges += ` [measured_only: ${acceptanceReason}]`;
          } else {
            badges += ' [measured_only]';
          }
        }
      }
      kdLines.push(`- [${id}] ${title}${metaSuffix}${badges}`);
      const summary = strVal(entry.summary);
      if (summary) kdLines.push(`  ${summary}`);
    }
    kdLines.push('');
    prefix += kdLines.join('\n') + '\n';
  }

  // Completed experiment sweeps — comparative arm results grouped by sweep_id.
  // Presence-gated: rendered for strategic_review, research, and design_doc_update
  // tasks when the backend ships a non-empty compute_experiment_sweeps map.
  if (['strategic_review', 'research', 'design_doc_update'].includes(taskType)) {
    const sweepMap = payload?.compute_experiment_sweeps as Record<string, Array<Record<string, unknown>>> | undefined;
    if (sweepMap && typeof sweepMap === 'object' && !Array.isArray(sweepMap)) {
      const sweepIds = Object.keys(sweepMap);
      if (sweepIds.length > 0) {
        const sweepLines: string[] = ['## Completed experiment sweeps'];
        sweepLines.push(
          'Comparative arm results from completed multi-arm compute experiments. Use `./fetch knowledge <id>` to retrieve the full analysis body for any arm.',
        );
        let renderedSweepCount = 0;
        for (const sweepId of sweepIds) {
          const arms = sweepMap[sweepId];
          if (!Array.isArray(arms) || arms.length === 0) continue;
          if (arms.some(arm => arm.sweep_disposition != null && typeof arm.sweep_disposition === 'object' && !Array.isArray(arm.sweep_disposition))) {
            continue;
          }
          renderedSweepCount += 1;
          sweepLines.push(`\n### Sweep ${sweepId}`);
          for (const arm of arms) {
            const id = strVal(arm.id);
            const title = strVal(arm.title);
            if (!id || !title) continue;
            const armLabel = strVal(arm.arm_label);
            const unavailableCause = strVal(arm.unavailable_cause);
            const criterionMet = arm.criterion_met;
            let verdict: string;
            if (unavailableCause) {
              verdict = `unavailable (${unavailableCause})`;
            } else if (criterionMet === true) {
              verdict = 'MET';
            } else if (criterionMet === false) {
              verdict = 'NOT MET';
            } else {
              verdict = 'UNMEASURED';
            }
            const armPrefix = armLabel ? `**${armLabel}**` : id;
            sweepLines.push(`- ${armPrefix} [${verdict}] [${id}] ${title}`);
            const summary = strVal(arm.summary);
            if (summary) sweepLines.push(`  ${summary}`);
          }
        }
        if (renderedSweepCount > 0) {
          sweepLines.push('');
          prefix += sweepLines.join('\n') + '\n';
        }
      }
    }
  }

  if (projectCtx) {
    const hasData = Object.keys(projectCtx).some(k => {
      const v = projectCtx[k]; return Array.isArray(v) && v.length > 0;
    });
    if (hasData) {
      const currentDirectionId = taskContext ? strVal(taskContext.direction_id) : '';
      const relatedFiles =
        (payload?.related_files as string[] | undefined) ||
        (taskContext?.related_files as string[] | undefined) ||
        [];
      prefix += formatProjectContext(projectCtx, taskType, taskScope, currentDirectionId, relatedFiles.length, opts) + '\n\n';
    }
    // Per-type rejection patterns ship as an object (not an array), so they
    // render independently of the hasData array check above.
    if (REVIEW_GATED_TASK_TYPES.includes(taskType)) {
      prefix += formatRejectionPatternsSection(
        projectCtx.rejection_patterns as RejectionPatternsSource | undefined,
        taskType,
      );
    }
  }

  // Append direction context section when present (title, priority, failed approaches)
  const dirCtx = task.direction_context as Record<string, unknown> | undefined;
  if (dirCtx) {
    // Condensed mode for co-batch tasks: 3+ active siblings from the same
    // direction. Omit verbose sections (narrative, evidence, prior analysis)
    // that duplicate across sibling tasks.
    const batchSiblingCount = typeof dirCtx.co_batch_sibling_count === 'number'
      ? dirCtx.co_batch_sibling_count
      : 0;
    if (batchSiblingCount >= 3) {
      prefix += formatCondensedDirectionContext(dirCtx);
    } else {
    const dcLines: string[] = ['## Direction Context'];
    const title = strVal(dirCtx.title);
    if (title) dcLines.push(`**Goal:** ${title}`);
    const priority = strVal(dirCtx.priority);
    if (priority) dcLines.push(`**Priority:** ${priority}`);
    const implConstraints = strVal(dirCtx.implementation_constraints);
    if (implConstraints) dcLines.push(`**Implementation constraints:** ${implConstraints}`);
    // Contract — what "done" means for this direction (success evidence /
    // completion criteria / retirement condition). Rendered defensively; only
    // fields the backend actually sent appear.
    const contract = dirCtx.contract as Record<string, unknown> | undefined;
    if (contract && typeof contract === 'object') {
      const completionCriteria = strVal(contract.completion_criteria);
      if (completionCriteria) dcLines.push(`**Completion criteria:** ${completionCriteria}`);
      const successEvidence = strVal(contract.success_evidence);
      if (successEvidence) dcLines.push(`**Success evidence:** ${successEvidence}`);
      const retirementCondition = strVal(contract.retirement_condition);
      if (retirementCondition) dcLines.push(`**Retirement condition:** ${retirementCondition}`);
    }
    const narrative = dirCtx.narrative as Record<string, unknown> | undefined;
    if (narrative && typeof narrative === 'object') {
      const established = Array.isArray(narrative.established_findings)
        ? (narrative.established_findings as unknown[]).map(s => strVal(s)).filter(Boolean)
        : [];
      if (established.length > 0) {
        dcLines.push('**Established findings (what we know):**');
        for (const f of established) dcLines.push(`- ${f}`);
      }
      const openQuestions = Array.isArray(narrative.open_questions)
        ? (narrative.open_questions as unknown[]).map(s => strVal(s)).filter(Boolean)
        : [];
      if (openQuestions.length > 0) {
        dcLines.push('**Open questions (still investigating):**');
        for (const q of openQuestions) dcLines.push(`- ${q}`);
      }
      const lastCycle = narrative.last_cycle as Record<string, unknown> | undefined;
      if (lastCycle && typeof lastCycle === 'object') {
        const tc = Number(lastCycle.tasks_created ?? 0);
        const pm = Number(lastCycle.prs_merged ?? 0);
        const pr = Number(lastCycle.prs_rejected ?? 0);
        if (tc > 0) {
          dcLines.push(`**Last productive cycle:** ${pm}/${tc} merged${pr > 0 ? `, ${pr} rejected` : ''}`);
        }
      }
    }
    // recent_findings_evidence: structured claims+sources for the most recent findings.
    // Canonical evidence view — use this for cross-referencing claims and sources.
    const recentFindingsEvidence = Array.isArray(dirCtx.recent_findings_evidence)
      ? dirCtx.recent_findings_evidence as Array<Record<string, unknown>>
      : [];
    if (recentFindingsEvidence.length > 0) {
      dcLines.push('**Recent findings evidence (canonical — full claims and sources):**');
      for (const entry of recentFindingsEvidence) {
        const ftitle = strVal(entry.title);
        if (!ftitle) continue;
        dcLines.push(`- "${ftitle}"`);
        const claims = Array.isArray(entry.claims)
          ? (entry.claims as unknown[]).map(c => strVal(c)).filter(Boolean)
          : [];
        if (claims.length > 0) {
          dcLines.push(`  Claims: ${claims.join('; ')}`);
        }
        const sourceRefs = Array.isArray(entry.source_refs)
          ? entry.source_refs as Array<Record<string, unknown>>
          : [];
        if (sourceRefs.length > 0) {
          const parts = sourceRefs.map(s => {
            const ref = strVal(s.ref);
            const excerpt = strVal(s.excerpt);
            return excerpt ? `${ref} ("${excerpt}")` : ref;
          }).filter(Boolean);
          if (parts.length > 0) dcLines.push(`  Sources: ${parts.join('; ')}`);
        }
      }
    }
    const failedApproaches = Array.isArray(dirCtx.failed_approaches) ? dirCtx.failed_approaches as unknown[] : [];
    if (failedApproaches.length > 0) {
      dcLines.push('**Avoid (already tried and failed):**');
      for (const fa of failedApproaches) {
        if (typeof fa === 'string') {
          dcLines.push(`- ${fa}`);
        } else if (fa && typeof fa === 'object') {
          const obj = fa as Record<string, unknown>;
          const text = strVal(obj.text);
          const ftype = strVal(obj.failure_type);
          const failedAt = strVal(obj.failed_at);
          const stage = strVal(obj.failure_stage);
          if (text) {
            const headParts = [`- ${text}`];
            if (ftype) headParts.push(`[${ftype}]`);
            const qualifiers: string[] = [];
            // Render relative to a caller-supplied deterministic reference epoch
            // (the backend-baked payload timestamp) when provided, NOT the VM
            // wall clock — so this text stays byte-identical across two builds of
            // an identical payload and never busts a cacheable prompt prefix it
            // lands in (v24 cost lever, direction d-27685510). Handlers that place
            // this block only in their task-variable tail pass no nowMs; passing
            // undefined lets relativeTimeFromIso keep its live wall-clock default.
            const rel = relativeTimeFromIso(failedAt, relativeTimeReference);
            if (rel) qualifiers.push(rel);
            if (stage) qualifiers.push(`failed at ${stage}`);
            if (qualifiers.length > 0) headParts.push(`(${qualifiers.join(', ')})`);
            dcLines.push(headParts.join(' '));
          }
        }
      }
    }
    const recentOutcomes = Array.isArray(dirCtx.recent_outcomes) ? dirCtx.recent_outcomes as Array<Record<string, unknown>> : [];
    if (recentOutcomes.length > 0) {
      const parts = recentOutcomes.map(o => `${Number(o.prs_merged ?? 0)}/${Number(o.tasks_created ?? 0)} merged`);
      dcLines.push(`Recent cycles: ${parts.join(', ')}`);
    }
    const failureStagePattern = dirCtx.failure_stage_pattern as Record<string, unknown> | undefined;
    if (failureStagePattern) {
      const msg = strVal(failureStagePattern.message);
      if (msg) {
        const rate = Number(failureStagePattern.rate);
        const header = Number.isFinite(rate) && rate > 0
          ? `**Failure pattern (${Math.round(rate * 100)}% of recent failures):**`
          : `**Failure pattern:**`;
        dcLines.push(`${header} ${msg}`);
      }
    }
    const scopeRates = dirCtx.scope_success_rates as Record<string, Record<string, unknown>> | undefined;
    if (scopeRates && typeof scopeRates === 'object') {
      const scopeOrder = ['small', 'medium', 'large', 'ci_failure'];
      const entries = Object.entries(scopeRates).sort(([a], [b]) => {
        const ai = scopeOrder.indexOf(a);
        const bi = scopeOrder.indexOf(b);
        return (ai === -1 ? 99 : ai) - (bi === -1 ? 99 : bi);
      });
      const parts: string[] = [];
      for (const [scope, counts] of entries) {
        const created = Number(counts.created ?? 0);
        const merged = Number(counts.merged ?? 0);
        if (created < 1) continue;
        const pct = Math.round((merged / created) * 100);
        const failed = Number(counts.failed ?? 0);
        const suffix = failed > 0 ? `, ${failed} failed` : '';
        parts.push(`${scope} ${merged}/${created} (${pct}%${suffix})`);
      }
      if (parts.length > 0) {
        dcLines.push(`Scope merge rates: ${parts.join(', ')}`);
      }
      const expansionParts: string[] = [];
      for (const [scope, counts] of entries) {
        const ratio = Number(counts.avg_expansion_ratio);
        if (!Number.isFinite(ratio) || ratio <= 0) continue;
        expansionParts.push(`${scope}-scope averages ${ratio.toFixed(1)}x file expansion`);
      }
      if (expansionParts.length > 0) {
        dcLines.push(
          `Direction expansion: ${expansionParts.join('; ')} — budget headroom for related files beyond what you declare upfront.`,
        );
      }
      const crossParts: string[] = [];
      for (const [scope, counts] of entries) {
        const merged = Number(counts.merged ?? 0);
        const crossMerged = Number(counts.merged_cross_module ?? 0);
        if (merged < 1 || crossMerged < 1) continue;
        crossParts.push(`${crossMerged} of ${merged} ${scope} merges touched 2+ directories`);
      }
      if (crossParts.length > 0) {
        dcLines.push(`Cross-directory: ${crossParts.join('; ')} — multi-directory changes are proven to merge in this direction.`);
      }
    }
    const rollingMedianIter = typeof dirCtx.review_iteration_rolling_median === 'number'
      ? dirCtx.review_iteration_rolling_median
      : null;
    if (rollingMedianIter !== null) {
      const lastCycleIter = typeof dirCtx.review_iteration_last_cycle === 'number'
        ? dirCtx.review_iteration_last_cycle
        : null;
      const lastCycleSuffix = lastCycleIter !== null && lastCycleIter !== rollingMedianIter
        ? ` (last cycle: ${lastCycleIter.toFixed(1)})`
        : '';
      dcLines.push(
        `Recent PRs on this direction averaged ${rollingMedianIter.toFixed(1)} review iterations${lastCycleSuffix}; tight scope merges faster.`,
      );
    }
    const priorAnalysis = Array.isArray(dirCtx.direction_prior_analysis)
      ? dirCtx.direction_prior_analysis as Array<Record<string, unknown>>
      : [];
    if (priorAnalysis.length > 0) {
      dcLines.push('**Prior analysis from failed attempts:**');
      for (const entry of priorAnalysis) {
        const t = strVal(entry.title);
        const cat = strVal(entry.failure_category);
        const scope = strVal(entry.scope);
        const tags = [cat, scope].filter(Boolean).join(', ');
        const header = tags ? `- ${t} [${tags}]` : `- ${t}`;
        dcLines.push(header);
        const plan = strVal(entry.analysis_plan);
        if (plan) dcLines.push(`  - Analysis plan: ${plan}`);
        const tfs = strVal(entry.test_failure_summary);
        if (tfs) dcLines.push(`  - Test failure: ${tfs}`);
        const statusMsg = strVal(entry.latest_status_message);
        if (statusMsg) dcLines.push(`  - Error: ${statusMsg}`);
        const sro = entry.self_review_outcome;
        if (sro && typeof sro === 'object') {
          const sroObj = sro as Record<string, unknown>;
          const sroParts: string[] = [];
          const verdict = strVal(sroObj.verdict);
          if (verdict) sroParts.push(`verdict=${verdict}`);
          const failedChecks = Array.isArray(sroObj.failed_checks)
            ? (sroObj.failed_checks as unknown[]).map(f => strVal(f)).filter(Boolean)
            : [];
          if (failedChecks.length > 0) sroParts.push(`failed=${failedChecks.join(',')}`);
          const notes = strVal(sroObj.notes);
          if (notes) sroParts.push(`notes=${notes}`);
          if (sroParts.length > 0) dcLines.push(`  - Self-review: ${sroParts.join('; ')}`);
        }
        const diffSummary = Array.isArray(entry.pr_diff_summary)
          ? entry.pr_diff_summary as Array<Record<string, unknown>>
          : [];
        if (diffSummary.length > 0) {
          const parts = diffSummary.map(d => {
            const file = strVal(d.file);
            const added = typeof d.added === 'number' ? d.added : null;
            const removed = typeof d.removed === 'number' ? d.removed : null;
            if (added !== null || removed !== null) {
              return `${file} (+${added ?? 0}/-${removed ?? 0})`;
            }
            return file;
          }).filter(Boolean);
          if (parts.length > 0) dcLines.push(`  - Files changed: ${parts.join(', ')}`);
        } else {
          const changedFiles = Array.isArray(entry.pr_changed_files)
            ? (entry.pr_changed_files as unknown[]).map(f => strVal(f)).filter(Boolean)
            : [];
          if (changedFiles.length > 0) {
            dcLines.push(`  - Files changed: ${changedFiles.join(', ')}`);
          }
        }
        const fp = strVal(entry.previous_error_fingerprint);
        if (fp) dcLines.push(`  - Previous error fingerprint: ${fp}`);
        if (typeof entry.review_iterations === 'number') {
          const iter = entry.review_iterations;
          const label = iter === 0
            ? 'Review iterations: 0 (failed before review)'
            : `Review iterations: ${iter} (${iter === 1 ? '1 cycle' : `${iter} cycles`} before failure)`;
          dcLines.push(`  - ${label}`);
        }
        const reviewFeedback = entry.review_feedback;
        if (Array.isArray(reviewFeedback) && reviewFeedback.length > 0) {
          const parts = reviewFeedback.map(item => {
            if (!item || typeof item !== 'object') return '';
            const obj = item as Record<string, unknown>;
            const sev = strVal(obj.severity);
            const desc = strVal(obj.description);
            if (!desc) return '';
            const file = strVal(obj.file);
            const line = typeof obj.line === 'number' ? obj.line : null;
            const loc = file ? ` (${file}${line !== null ? `:${line}` : ''})` : '';
            return sev ? `[${sev}] ${desc}${loc}` : `${desc}${loc}`;
          }).filter(Boolean);
          if (parts.length > 0) dcLines.push(`  - Review feedback: ${parts.join(', ')}`);
        } else if (typeof reviewFeedback === 'string' && reviewFeedback) {
          dcLines.push(`  - Review feedback: ${reviewFeedback}`);
        }
      }
    }
    const failureHotspots = synthesizeFailureHotspots(dirCtx);
    if (failureHotspots.length > 0) {
      const parts = failureHotspots.map(h => `${h.file} (${h.count}x)`).join(', ');
      dcLines.push(`Files appearing in multiple recent failures: ${parts} — changes to these files need extra care.`);
    }
    const successfulAnalysis = Array.isArray(dirCtx.direction_successful_analysis)
      ? dirCtx.direction_successful_analysis as Array<Record<string, unknown>>
      : [];
    if (successfulAnalysis.length > 0) {
      dcLines.push('**Approaches that worked:**');
      for (const entry of successfulAnalysis) {
        const t = strVal(entry.title);
        const scope = strVal(entry.scope);
        const header = scope ? `- ${t} [${scope}]` : `- ${t}`;
        dcLines.push(header);
        const plan = strVal(entry.analysis_plan);
        if (plan) dcLines.push(`  - Analysis plan: ${plan}`);
        const changedFiles = Array.isArray(entry.pr_changed_files)
          ? (entry.pr_changed_files as unknown[]).map(f => strVal(f)).filter(Boolean)
          : [];
        if (changedFiles.length > 0) {
          dcLines.push(`  - Files changed: ${changedFiles.join(', ')}`);
        }
        if (typeof entry.review_iterations === 'number') {
          const iter = entry.review_iterations;
          const label = iter <= 1
            ? '1st-pass success'
            : `${iter} review cycles`;
          dcLines.push(`  - ${label}`);
        }
      }
    }
    const patterns = synthesizeSuccessPatterns(dirCtx, taskScope);
    if (patterns.length > 0) {
      dcLines.push('**What tends to work:**');
      for (const s of patterns) dcLines.push(`- ${s}`);
    }
    type DirectionFirstReviewIssue = { description: string; count: number; category: string };
    const dirTopIssues = Array.isArray(dirCtx.top_first_review_issues)
      ? dirCtx.top_first_review_issues as DirectionFirstReviewIssue[]
      : [];
    if (dirTopIssues.length > 0) {
      dcLines.push('**Recurring review issues in this direction (address before submitting):**');
      for (const issue of dirTopIssues) {
        dcLines.push(`- "${issue.description}" (seen ${issue.count}x, category: ${issue.category})`);
      }
    }
    const promptQualityPatterns = Array.isArray(dirCtx.prompt_quality_patterns)
      ? (dirCtx.prompt_quality_patterns as unknown[]).filter((s): s is string => typeof s === 'string')
      : [];
    if (promptQualityPatterns.length > 0) {
      dcLines.push('**Prompt quality patterns (apply when writing finding prompts):**');
      for (const note of promptQualityPatterns) dcLines.push(`- ${note}`);
    }
    type ReviewEffectivenessCategory = { trend?: string };
    const reviewEff = dirCtx.review_effectiveness as
      | { categories?: Record<string, ReviewEffectivenessCategory> }
      | null
      | undefined;
    const reviewEffCats = reviewEff?.categories;
    if (reviewEffCats && Object.keys(reviewEffCats).length > 0) {
      dcLines.push('**Review effectiveness trends (per category):**');
      for (const [cat, info] of Object.entries(reviewEffCats)) {
        const trend = info.trend;
        const trendIndicator = trend === 'decreasing' ? 'trending down'
          : trend === 'increasing' ? 'trending up ⚠'
          : trend === 'stable' ? 'persistent'
          : trend ?? 'unknown';
        dcLines.push(`- **${cat}**: ${trendIndicator}`);
      }
    }
    type ReviewQualityFeedback = {
      reviews_dismissed?: number;
      reviews_overridden?: number;
      user_issues_caught?: number;
      note?: string;
    };
    const rqf = dirCtx.review_quality_feedback as ReviewQualityFeedback | null | undefined;
    if (rqf && typeof rqf === 'object') {
      const rqfLines: string[] = [];
      if (typeof rqf.note === 'string' && rqf.note) {
        rqfLines.push(`- ${rqf.note}`);
      }
      const counts: string[] = [];
      if (typeof rqf.reviews_dismissed === 'number') counts.push(`dismissed: ${rqf.reviews_dismissed}`);
      if (typeof rqf.reviews_overridden === 'number') counts.push(`overridden: ${rqf.reviews_overridden}`);
      if (typeof rqf.user_issues_caught === 'number') counts.push(`user-caught: ${rqf.user_issues_caught}`);
      if (counts.length > 0) rqfLines.push(`- self-review calibration — ${counts.join(', ')}`);
      if (rqfLines.length > 0) {
        dcLines.push('**Self-review calibration:**');
        dcLines.push(...rqfLines);
      }
    }
    dcLines.push('');
    prefix += dcLines.join('\n') + '\n';
    }
  }

  // Append architecture context section when present
  const archCtx = projectCtx?.architecture_context as Array<Record<string, unknown>> | undefined;
  if (archCtx && archCtx.length > 0) {
    const archLines: string[] = ['## Architecture Context'];
    for (const sys of archCtx) {
      const name = strVal(sys.name);
      const status = strVal(sys.status);
      const ready = sys.ready_for_features === true ? 'ready' : 'not ready';
      const desc = strVal(sys.description);
      const line = desc
        ? `- **${name}**: ${status}, ${ready} for features — ${desc}`
        : `- **${name}**: ${status}, ${ready} for features`;
      archLines.push(line);
    }
    archLines.push('');
    prefix += archLines.join('\n') + '\n';
  }

  // Append task scope section for create/fix/refactor task types
  const scopeApplicable = ['create', 'fix', 'refactor'].includes(taskType);
  if (taskScope && scopeApplicable) {
    const scopeDescriptions: Record<string, string> = {
      small: 'expect <50 lines across 1-3 files. Keep changes minimal and focused.',
      medium: 'expect 50-200 lines across 2-5 files. Moderate cross-cutting changes. Plan your changes before executing — identify files, dependencies, and order.',
      large: 'cross-component, 200+ lines — plan before coding.',
    };
    const scopeDesc = scopeDescriptions[taskScope] ?? '';
    const effort = payload ? strVal(payload.effort) : '';
    const calibratedFrom = taskContext ? strVal(taskContext.scope_calibrated_from) : '';
    const scopeLines: string[] = ['## Task Scope'];
    const scopeLine = scopeDesc ? `Scope: ${taskScope} — ${scopeDesc}` : `Scope: ${taskScope}`;
    scopeLines.push(scopeLine);
    if (effort) scopeLines.push(`Effort: ${effort}`);
    if (calibratedFrom && calibratedFrom !== taskScope) {
      const order = ['small', 'medium', 'large'];
      const calibratedFromBase = calibratedFrom.split(':')[0];
      const fromIdx = order.indexOf(calibratedFromBase);
      const toIdx = order.indexOf(taskScope);
      const isUpgrade = fromIdx >= 0 && toIdx > fromIdx;
      const scopeRatesForCal = dirCtx?.scope_success_rates as
        | Record<string, Record<string, unknown>>
        | undefined;
      const formatRate = (scope: string): string | null => {
        const entry = scopeRatesForCal?.[scope];
        if (!entry) return null;
        const created = Number(entry.created) || 0;
        const merged = Number(entry.merged) || 0;
        if (created <= 0) return null;
        const pct = Math.round((merged / created) * 100);
        return `${scope}-scope tasks merge at ${pct}% (${merged}/${created})`;
      };
      const fromRate = formatRate(calibratedFromBase);
      const toRate = formatRate(taskScope);
      let ratesSuffix = '';
      if (fromRate && toRate) {
        ratesSuffix = ` Direction merge rates: ${toRate} vs ${fromRate}.`;
      } else if (fromRate) {
        ratesSuffix = ` Direction merge rate: ${fromRate}.`;
      } else if (toRate) {
        ratesSuffix = ` Direction merge rate: ${toRate}.`;
      }
      if (isUpgrade) {
        scopeLines.push(
          `Note: This task was originally scoped as ${calibratedFrom} but calibrated up to ${taskScope} because ${calibratedFrom}-scope tasks have struggled for this direction while smaller scopes have succeeded.${ratesSuffix} Decompose the work into independent smaller pieces — each piece should be small enough to succeed on its own.`,
        );
      } else {
        scopeLines.push(
          `Note: This task was originally scoped as ${calibratedFrom} but calibrated down to ${taskScope} because ${calibratedFrom}-scope tasks have struggled for this direction.${ratesSuffix} Stay tightly focused — the scope reduction is intentional to avoid the scope creep that caused prior ${calibratedFrom}-scope failures.`,
        );
      }
    }
    scopeLines.push('');
    prefix += scopeLines.join('\n') + '\n';
  }

  // Acceptance Evidence — the proof a task declared at spawn time that would
  // show its claim is true. The declaration can originate at plan time (from
  // the parent direction's planning step) or at research time (from a
  // criterion-carrying finding's acceptance_evidence field, injected via
  // criterion_keys). The backend validates the declaration and stamps it onto
  // this child task's context.acceptance_evidence (closed shape: kind ∈ {test,
  // behavior, artifact}, descriptor, optional selector). Presence gated: when
  // absent the prompt is byte-identical to today. This names the proof to AIM
  // for; it does NOT gate merge.
  const acceptanceEvidence = taskContext?.acceptance_evidence as Record<string, unknown> | undefined;
  if (acceptanceEvidence && typeof acceptanceEvidence === 'object') {
    const kind = strVal(acceptanceEvidence.kind);
    const descriptor = strVal(acceptanceEvidence.descriptor);
    if (kind || descriptor) {
      const aeLines: string[] = ['## Acceptance Evidence'];
      aeLines.push(
        'This task declared, at spawn time, the proof that would show its claim is true. Aim to produce it — this is the evidence the work should demonstrate, not a merge gate.',
      );
      const kindLabels: Record<string, string> = {
        test: 'test (a named test or selector exercising the seam end-to-end)',
        behavior: 'behavior (an observable runtime behavior)',
        artifact: 'artifact (an artifact a downstream task consumes)',
        visual: 'visual (a screenshot of a UI component proving it renders correctly at a given viewport width)',
      };
      if (kind) aeLines.push(`- Kind: ${kindLabels[kind] ?? kind}`);
      if (kind === 'visual') {
        // Visual evidence travels as PlanAcceptanceEvidence on the context
        // channel: descriptor = the component to capture, selector = the
        // viewport width (px). Read those fields (with a defensive fallback to
        // the payload-shape field names) and render an explicit, actionable
        // capture instruction plus how the proof is verified.
        const component = descriptor || strVal(acceptanceEvidence.component);
        const widthStr = strVal(acceptanceEvidence.selector) || strVal(acceptanceEvidence.viewport_width);
        const assertionType = strVal(acceptanceEvidence.assertion_type);
        if (component) aeLines.push(`- Component to capture: ${component}`);
        if (widthStr) aeLines.push(`- Viewport width: ${widthStr}px`);
        if (assertionType) aeLines.push(`- Assertion focus: ${assertionType}`);
        aeLines.push(
          `- How it is verified: on a successful run a viewport screenshot of ${component || 'the component'}${widthStr ? ` at ${widthStr}px` : ''} is captured and uploaded, and the acceptance gate matches that screenshot against this declaration. Ensure ${component || 'the component'} has a \`*.viewport.test.tsx\` that renders it at that width so the screenshot can be captured.`,
        );
      } else {
        if (descriptor) aeLines.push(`- Proof: ${descriptor}`);
        const selector = strVal(acceptanceEvidence.selector);
        if (selector) aeLines.push(`- Selector: ${selector}`);
      }
      aeLines.push('');
      prefix += aeLines.join('\n') + '\n';
    }
  }

  // Append hidden-signal section surfacing backend-computed quality signals
  // stored in task.context that would otherwise never reach the agent.
  if (taskContext) {
    const signalLines: string[] = [];
    const preventive = taskContext.preventive_context as Record<string, unknown> | undefined;
    if (preventive && typeof preventive === 'object') {
      const warning = strVal(preventive.warning);
      if (warning) {
        const overlapFiles = Array.isArray(preventive.overlap_files)
          ? (preventive.overlap_files as unknown[]).map(f => strVal(f)).filter(Boolean)
          : [];
        const fingerprint = strVal(preventive.fingerprint);
        const anomalyKey = strVal(preventive.anomaly_key);
        signalLines.push('**Preventive warning:** ' + warning);
        if (overlapFiles.length > 0) {
          signalLines.push(`- Overlap files: ${overlapFiles.join(', ')}`);
        }
        if (fingerprint) signalLines.push(`- Failure fingerprint: ${fingerprint}`);
        if (anomalyKey) signalLines.push(`- Anomaly key: ${anomalyKey}`);
      }
    }
    const budgetWarning = strVal(taskContext._file_budget_warning);
    if (budgetWarning) {
      signalLines.push(`**File budget pressure:** ${budgetWarning}`);
    }
    const lowConfRate = taskContext.low_confidence_spawn_rate;
    if (typeof lowConfRate === 'number' && Number.isFinite(lowConfRate)) {
      const pct = Math.round(lowConfRate * 100);
      signalLines.push(
        `**Low-confidence spawn:** predicted merge probability is ${pct}% — prefer the smallest viable change and avoid speculative scope expansion.`,
      );
    }
    const complexityScore = taskContext.complexity_score;
    if (typeof complexityScore === 'number' && Number.isFinite(complexityScore) && complexityScore < 0.5) {
      const pct = Math.round(complexityScore * 100);
      signalLines.push(`**Complexity score:** ${pct}/100 (low merge-probability estimate).`);
      const signals = taskContext.complexity_signals as Record<string, unknown> | undefined;
      if (signals && typeof signals === 'object') {
        const files = typeof signals.files === 'number' ? signals.files : null;
        const modules = typeof signals.modules === 'number' ? signals.modules : null;
        const expansionRatio = typeof signals.expansion_ratio === 'number' ? signals.expansion_ratio : null;
        const neighborRatio = typeof signals.neighbor_ratio === 'number' ? signals.neighbor_ratio : null;
        const factors = (signals.factors && typeof signals.factors === 'object')
          ? signals.factors as Record<string, unknown>
          : {};
        const f = (k: string): number | null =>
          typeof factors[k] === 'number' ? factors[k] : null;

        const sentences: string[] = [];
        if (files !== null && modules !== null) {
          sentences.push(`This task spans ${files} file${files === 1 ? '' : 's'} across ${modules} module${modules === 1 ? '' : 's'}.`);
        } else if (files !== null) {
          sentences.push(`This task spans ${files} file${files === 1 ? '' : 's'}.`);
        }
        const fRatio = f('ratio');
        if (fRatio !== null && fRatio < 0.8 && expansionRatio !== null) {
          sentences.push(`Historical expansion ratio of ${expansionRatio}x means agents working on similar tasks touched ${expansionRatio}x the listed files — constrain your initial changes aggressively.`);
        }
        const fNeighbors = f('neighbors');
        if (fNeighbors !== null && fNeighbors < 0.8 && neighborRatio !== null) {
          sentences.push(`High co-change neighbor ratio (${neighborRatio}) suggests unlisted files will likely need changes too — plan for scope pressure.`);
        }
        const fModules = f('modules');
        if (fModules !== null && fModules < 0.8 && modules !== null && modules > 1) {
          sentences.push(`Changes spanning ${modules} modules increase cross-system risk — verify each module's contract independently.`);
        }
        const fFiles = f('files');
        if (fFiles !== null && fFiles < 0.8 && files !== null) {
          sentences.push(`File count (${files}) is above the high-merge band — prefer the smallest viable subset.`);
        }
        if (sentences.length > 0) {
          signalLines.push(`- ${sentences.join(' ')}`);
        }
      }
    }
    if (signalLines.length > 0) {
      prefix += ['## Quality Signals', ...signalLines, ''].join('\n') + '\n';
    }
  }

  // Append remediation context when this task was spawned from an investigation.
  // The payload prompt already contains the root cause and recommended fix — this
  // section adds framing so the agent treats it as a diagnosed fix.
  if (taskContext && taskContext.diagnosis_task_id) {
    const remLines: string[] = ['## Remediation Context'];
    remLines.push(
      'This task is a diagnosed fix spawned from a prior investigation. The payload contains the root cause and recommended fix — validate that your changes resolve that specific root cause, not adjacent issues.',
    );
    const severity = strVal(taskContext.severity);
    if (severity) remLines.push(`- Severity: ${severity}`);
    const confidence = strVal(taskContext.diagnosis_confidence);
    if (confidence) remLines.push(`- Diagnosis confidence: ${confidence}`);
    const attemptsRaw = taskContext.remediation_attempts;
    let attempts: number | null = null;
    if (typeof attemptsRaw === 'number' && Number.isFinite(attemptsRaw)) {
      attempts = attemptsRaw;
    } else if (dirCtx && typeof dirCtx.remediation_attempts === 'number') {
      attempts = dirCtx.remediation_attempts;
    }
    if (attempts !== null && attempts > 0) {
      remLines.push(`- Prior remediation attempts on this direction: ${attempts} — if a previous remediation failed to hold, favor a different angle over repeating the prior fix shape.`);
    }
    if (confidence === 'low') {
      remLines.push('- Confidence is low: treat the recommended fix as a hypothesis, verify the root cause before applying, and prefer the smallest reversible change.');
    }
    remLines.push('');
    prefix += remLines.join('\n') + '\n';
  }

  return prefix;
}
