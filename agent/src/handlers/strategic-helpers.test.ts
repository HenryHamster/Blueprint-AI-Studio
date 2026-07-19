import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import fs from 'fs';
import os from 'os';
import path from 'path';
import { execFileSync } from 'child_process';
import {
  writeStrategicHelpers,
  writeFetchHelper,
  buildFetchGuidance,
  renderStrategicFetchVerbHelpLines,
  RESEARCH_FETCH_VERBS,
  PLAN_FETCH_VERBS,
  DESIGN_DOC_FETCH_VERBS,
  STRATEGIC_FETCH_VERBS,
  parseStrategicChallengeRecord,
  buildStrategicChallengePrompt,
  buildStrategicChallengeExecutorSection,
  runStrategicChallenge,
  STRATEGIC_CHALLENGE_VERSION,
  portfolioOperationFingerprint,
} from './strategic-helpers.js';
import { VmConfigMissingError, provisionAmbientHelpers } from '../providers/runners.js';
import {
  writePlaybookHelper,
  writeMessagesHelper,
  writeDesignDocHelper,
} from '../prompt.js';

// Use the OS temp dir (not a hardcoded /tmp) so this suite runs on Windows too.
const testWorkspace = path.join(os.tmpdir(), 'test-strategic-helpers-workspace');

describe('writeStrategicHelpers', () => {
  beforeEach(() => {
    if (!fs.existsSync(testWorkspace)) {
      fs.mkdirSync(testWorkspace, { recursive: true });
    }
  });

  afterEach(() => {
    // Clean up test workspace
    if (fs.existsSync(testWorkspace)) {
      try {
        fs.rmSync(testWorkspace, { recursive: true });
      } catch { /* ignore */ }
    }
  });

  it('writes three helper scripts to workspace', () => {
    const taskId = 'test-task-123';
    const projectId = 'test-project-456';
    writeStrategicHelpers(taskId, projectId, testWorkspace);

    expect(fs.existsSync(`${testWorkspace}/strategic`)).toBe(true);
    expect(fs.existsSync(`${testWorkspace}/fetch`)).toBe(true);
    expect(fs.existsSync(`${testWorkspace}/design-doc`)).toBe(true);
  });

  it('writes scripts with executable permissions', () => {
    const taskId = 'test-task-123';
    const projectId = 'test-project-456';
    writeStrategicHelpers(taskId, projectId, testWorkspace);

    const stat = fs.statSync(`${testWorkspace}/strategic`);
    // POSIX execute bits are not modeled on Windows (mode & 0o111 is always 0
    // there regardless of the requested 0o755), so the bit assertion only makes
    // sense on POSIX. On Windows just assert the file was created.
    if (process.platform === 'win32') {
      expect(stat.isFile()).toBe(true);
    } else {
      expect((stat.mode & 0o111) !== 0).toBe(true); // Check execute bit
    }
  });

  it('embeds taskId in strategic script safely', () => {
    const taskId = 'test-task-123';
    const projectId = 'test-project-456';
    writeStrategicHelpers(taskId, projectId, testWorkspace);

    const content = fs.readFileSync(`${testWorkspace}/strategic`, 'utf-8');
    expect(content).toContain(`TASK="${taskId}"`);
    expect(content).toContain('set-problem-model');
    expect(content).toContain('create-direction');
    expect(content).toContain('set-running-update');
    expect(content).toContain('approve-direction');
    expect(content).toContain('reject-direction');
  });

  it('routes the spawn-fix op to the spawn_fix backend operation', () => {
    const taskId = 'test-task-123';
    const projectId = 'test-project-456';
    writeStrategicHelpers(taskId, projectId, testWorkspace);

    const content = fs.readFileSync(`${testWorkspace}/strategic`, 'utf-8');
    // The one-time-fix dispatch arm exists and maps the CLI verb to the API op.
    expect(content).toContain('spawn-fix)');
    expect(content).toContain('_call "spawn_fix"');
    // It is advertised in the usage string alongside spawn-task.
    expect(content).toContain('spawn-fix');
  });

  it('routes acceptance-criteria authoring to the set_acceptance_criteria backend operation', () => {
    const taskId = 'test-task-123';
    const projectId = 'test-project-456';
    writeStrategicHelpers(taskId, projectId, testWorkspace);

    const content = fs.readFileSync(`${testWorkspace}/strategic`, 'utf-8');
    expect(content).toContain('set-acceptance-criteria');
    // The case arms are now derived from the canonical registry with uniform
    // single-space padding (was hand-aligned columns); the alias→op mapping is
    // unchanged.
    expect(content).toContain('set-acceptance-criteria) _call "set_acceptance_criteria"');
  });

  it('embeds projectId in fetch script safely', () => {
    const taskId = 'test-task-123';
    const projectId = 'test-project-456';
    writeStrategicHelpers(taskId, projectId, testWorkspace);

    const content = fs.readFileSync(`${testWorkspace}/fetch`, 'utf-8');
    expect(content).toContain(`PROJECT="${projectId}"`);
    expect(content).toContain('problem-model');
    expect(content).toContain('direction');
    expect(content).toContain('playbook');
    expect(content).toContain('gap');
    expect(content).toContain('message');
  });

  it('includes task and tasks fetch verbs in the strategic fetch script', () => {
    const taskId = 'test-task-123';
    const projectId = 'test-project-456';
    writeStrategicHelpers(taskId, projectId, testWorkspace);

    const content = fs.readFileSync(`${testWorkspace}/fetch`, 'utf-8');
    // Single-task lookup: task <id> -> tasks/$ID
    expect(content).toContain('task)');
    expect(content).toContain('PATH_SUFFIX="tasks/$ID"');
    // List lookup: tasks -> tasks (no id required)
    expect(content).toContain('tasks)');
    expect(content).toContain('PATH_SUFFIX="tasks"');
    // tasks (list) must not require an id argument — it should be in the no-id exclusion list
    expect(content).toContain('"tasks"');
  });

  it('task and tasks verbs are in STRATEGIC_FETCH_VERBS and have help text', () => {
    expect(STRATEGIC_FETCH_VERBS).toContain('task');
    expect(STRATEGIC_FETCH_VERBS).toContain('tasks');

    writeStrategicHelpers('t', 'p', testWorkspace);
    const fetchContent = fs.readFileSync(`${testWorkspace}/fetch`, 'utf-8');
    // Both verbs appear in the generated case statement
    expect(fetchContent).toContain('task)');
    expect(fetchContent).toContain('tasks)');
  });

  it('tools verb is wired into STRATEGIC_FETCH_VERBS, the fetch script, and help text', () => {
    expect(STRATEGIC_FETCH_VERBS).toContain('tools');
    expect(renderStrategicFetchVerbHelpLines().some(l => l.includes('./fetch tools'))).toBe(true);

    writeStrategicHelpers('t', 'p', testWorkspace);
    const content = fs.readFileSync(`${testWorkspace}/fetch`, 'utf-8');
    // Resolves to the /tools endpoint suffix and takes no id argument.
    expect(content).toContain('tools)');
    expect(content).toContain('PATH_SUFFIX="tools"');
    expect(content).toContain('[ "$KIND" != "tools" ]');
  });

  // ── Live token resolution (resident write-token-expiry fix) ─────────────────
  // The strategic/fetch helpers must read the ROTATED token FRESH on each
  // invocation (from $AGENT_TOKEN_FILE, kept current by the agent's ~45min
  // refresh) instead of the stale spawn-time AGENT_API_TOKEN env snapshot; a
  // long-lived resident session otherwise write-401s once the 1h TTL elapses.
  describe('live token resolution', () => {
    it('strategic reads the live token file each invocation with an env fallback', () => {
      writeStrategicHelpers('t', 'p', testWorkspace);
      const s = fs.readFileSync(`${testWorkspace}/strategic`, 'utf-8');
      expect(s).toContain('_load_agent_token()');
      expect(s).toContain('TOKEN=$(cat "${AGENT_TOKEN_FILE}" 2>/dev/null)');
      expect(s).toContain('TOKEN="${AGENT_API_TOKEN:-}"');
      // The stale hard-`:?` snapshot binding must be gone.
      expect(s).not.toContain('TOKEN="${AGENT_API_TOKEN:?AGENT_API_TOKEN not set}"');
      // Curl still authenticates with the resolved $TOKEN.
      expect(s).toContain('-H "X-Agent-Token: $TOKEN"');
      // _call re-reads the token each attempt so a mid-session rotation is used.
      expect(s).toContain('_load_agent_token || return 1');
    });

    it('strategic surfaces a loud "restart the session" error when the token is unrecoverably expired', () => {
      writeStrategicHelpers('t', 'p', testWorkspace);
      const s = fs.readFileSync(`${testWorkspace}/strategic`, 'utf-8');
      // 401/403 is treated as an auth failure and retried...
      expect(s).toContain('[AUTH]');
      expect(s).toContain('"$http_code" -eq 401');
      // ...then, if still failing, fails LOUD rather than silently going write-dead.
      expect(s).toContain('agent token expired');
      expect(s).toContain('resident session must be restarted');
    });

    it('fetch reads the live token file and retries once on a 401', () => {
      writeStrategicHelpers('t', 'p', testWorkspace);
      const s = fs.readFileSync(`${testWorkspace}/fetch`, 'utf-8');
      expect(s).toContain('_load_agent_token()');
      expect(s).toContain('TOKEN=$(cat "${AGENT_TOKEN_FILE}" 2>/dev/null)');
      expect(s).not.toContain('TOKEN="${AGENT_API_TOKEN:?AGENT_API_TOKEN not set}"');
      // 401/403 → re-read the rotated token and retry the GET once.
      expect(s).toContain('[ "$http_code" -eq 401 ] || [ "$http_code" -eq 403 ]');
      expect(s).toContain('_load_agent_token || exit 1');
    });

    it('generated strategic + fetch scripts pass `bash -n`', () => {
      writeStrategicHelpers('t', 'p', testWorkspace);
      for (const name of ['strategic', 'fetch']) {
        const p = `${testWorkspace}/${name}`;
        // Throws on a syntax error (non-zero exit) — the assertion is that it does not.
        expect(() => execFileSync('bash', ['-n', p], { stdio: 'pipe' })).not.toThrow();
      }
    });

    it('_call retries only on 429 and 5xx — not on permanent 4xx (400/422/404)', () => {
      writeStrategicHelpers('t', 'p', testWorkspace);
      const s = fs.readFileSync(`${testWorkspace}/strategic`, 'utf-8');
      // 429 is in the retry branch (string comparison form used in the condition)
      expect(s).toContain('"429"');
      // 5xx range is retried alongside 429
      expect(s).toContain('-ge 500');
      // Non-retryable permanent 4xx exits immediately with a clear label
      expect(s).toContain('non-retryable');
      // The non-retryable branch prints the body before reporting the error
      expect(s).toContain('"$body" >&2');
    });

    it('_call retries a curl transport failure (http_code 000) instead of treating it as permanent', () => {
      writeStrategicHelpers('t', 'p', testWorkspace);
      const s = fs.readFileSync(`${testWorkspace}/strategic`, 'utf-8');
      // curl reports http_code "000" on a connect timeout / DNS / TLS / dropped
      // connection — no HTTP response. It must be retried alongside 429/5xx, not
      // fall through to the non-retryable else branch (which would make a network
      // blip a permanent failure).
      expect(s).toContain('[ "$http_code" = "000" ]');
      // The 000 check shares the SAME retry condition as 429 (one `if` line).
      expect(s).toMatch(/\[ "\$http_code" = "000" \] \|\| \[ "\$http_code" = "429" \]/);
    });

    it('_call honors Retry-After header on 429 contention', () => {
      writeStrategicHelpers('t', 'p', testWorkspace);
      const s = fs.readFileSync(`${testWorkspace}/strategic`, 'utf-8');
      // Headers are captured to a dump file so Retry-After can be parsed
      expect(s).toContain('-D "$dump"');
      // Retry-After is read from the dump file (case-insensitive header match)
      expect(s).toContain("grep -i '^retry-after:'");
      // Delay is honoured in the sleep call
      expect(s).toContain('sleep "$delay"');
    });

    it('_call terminal error includes HTTP status code', () => {
      writeStrategicHelpers('t', 'p', testWorkspace);
      const s = fs.readFileSync(`${testWorkspace}/strategic`, 'utf-8');
      // Non-auth terminal error reports the actual HTTP status so the agent can diagnose
      expect(s).toContain('failed after $max attempts (HTTP $http_code)');
    });

    it('_call prints response body on failed non-retryable attempt', () => {
      writeStrategicHelpers('t', 'p', testWorkspace);
      const s = fs.readFileSync(`${testWorkspace}/strategic`, 'utf-8');
      // Body is surfaced to stderr on non-retryable 4xx so the error detail is visible
      expect(s).toContain('"$body" >&2');
    });
  });

  it('supports fetching a message by short ref via the messages endpoint', () => {
    const taskId = 'test-task-123';
    const projectId = 'test-project-456';
    writeStrategicHelpers(taskId, projectId, testWorkspace);

    const content = fs.readFileSync(`${testWorkspace}/fetch`, 'utf-8');
    // The new subcommand maps `fetch message [ref]` to the read-only lookup
    // endpoint that resolves a short ref or full UUID backend-side. When no ref
    // is supplied it lists open messages (compact) via /messages/open;
    // messages/${ID:-open} substitutes the ref when one is provided.
    expect(content).toContain('message)    PATH_SUFFIX="messages/${ID:-open}"');
    expect(content).toContain('fetch problem-model|direction|playbook|gap|message|section|knowledge|compute-sweeps|qualification|version-phase|vm-cost|efficiency-snapshot|ci-compliance|capability-trials|sections|task|tasks|completion-debt|ui-load-timing|skills|tools [id]');
    // Reuses the existing X-Agent-Token GET path — no new auth/curl block.
    expect(content).toContain('-H "X-Agent-Token: $TOKEN"');
    // Both direction and message are excluded from the id-required guard: they
    // support a no-id compact-listing mode (direction via ${ID:+/$ID};
    // message via messages/${ID:-open}).
    expect(content).toContain('[ "$KIND" != "direction" ]');
    expect(content).toContain('[ "$KIND" != "message" ]');
    // The id-required guard still fires for verbs that do require an id.
    expect(content).toContain('fetch $KIND requires an id argument');
    expect(content).toContain('[ "$KIND" != "version-phase" ]');
  });

  it('allows fetching the full problem model without an id', () => {
    const taskId = 'test-task-123';
    const projectId = 'test-project-456';
    writeStrategicHelpers(taskId, projectId, testWorkspace);

    const content = fs.readFileSync(`${testWorkspace}/fetch`, 'utf-8');
    expect(content).toContain('Usage: fetch problem-model|direction|playbook|gap|message|section|knowledge|compute-sweeps|qualification|version-phase|vm-cost|efficiency-snapshot|ci-compliance|capability-trials|sections|task|tasks|completion-debt|ui-load-timing|skills|tools [id]');
    expect(content).toContain('problem-model) PATH_SUFFIX="problem-model"');
    expect(content).toContain('fetch $KIND requires an id argument');
  });

  it('maps `fetch section <key>` to the read-only design-doc endpoint', () => {
    const taskId = 'test-task-123';
    const projectId = 'test-project-456';
    writeStrategicHelpers(taskId, projectId, testWorkspace);

    const content = fs.readFileSync(`${testWorkspace}/fetch`, 'utf-8');
    // `fetch section systems/auth` -> GET .../design-doc/systems/auth (key may
    // contain a slash; the backend route is `{key:path}`).
    expect(content).toContain('section)    PATH_SUFFIX="design-doc/$ID"');
    // Section requires an id (key) — covered by the shared id-required guard.
    expect(content).toContain('fetch $KIND requires an id argument');
  });

  it('embeds taskId in design-doc script safely', () => {
    const taskId = 'test-task-123';
    const projectId = 'test-project-456';
    writeStrategicHelpers(taskId, projectId, testWorkspace);

    const content = fs.readFileSync(`${testWorkspace}/design-doc`, 'utf-8');
    // Shared durable generator (prompt.ts) bakes the task id into a TASK var and
    // interpolates it via printf, rather than inlining it in each curl body.
    expect(content).toContain(`TASK="${taskId}"`);
    expect(content).toContain('get|set|delete|list');
  });

  it('prevents shell injection in taskId with special characters', () => {
    const taskId = 'test$(whoami)task';
    const projectId = 'project-456';
    writeStrategicHelpers(taskId, projectId, testWorkspace);

    const strategicContent = fs.readFileSync(`${testWorkspace}/strategic`, 'utf-8');
    // The `$` is backslash-escaped before being baked into the double-quoted
    // TASK assignment, so bash sees a literal string, not a command substitution.
    expect(strategicContent).toContain('TASK="test\\$(whoami)task"');
    // The dangerous *unescaped* command-substitution form must NOT appear — that
    // is the form that would actually run `whoami` at script load time. (We can't
    // just assert the absence of the substring "$(whoami)" because the safe,
    // escaped form "\$(whoami)" legitimately contains it.)
    expect(strategicContent).not.toContain('TASK="test$(whoami)task"');
    // Every "$(whoami)" occurrence must be backslash-escaped.
    expect(/(^|[^\\])\$\(whoami\)/.test(strategicContent)).toBe(false);
  });

  it('prevents shell injection in projectId with special characters', () => {
    const taskId = 'test-task';
    const projectId = 'project$(echo hacked)id';
    writeStrategicHelpers(taskId, projectId, testWorkspace);

    const fetchContent = fs.readFileSync(`${testWorkspace}/fetch`, 'utf-8');
    // The `$` is backslash-escaped so the substitution never executes.
    expect(fetchContent).toContain('PROJECT="project\\$(echo hacked)id"');
    expect(fetchContent).not.toContain('PROJECT="project$(echo hacked)id"');
    // Every "$(echo hacked)" occurrence must be backslash-escaped.
    expect(/(^|[^\\])\$\(echo hacked\)/.test(fetchContent)).toBe(false);
  });

  it('produces valid bash scripts with proper escaping', () => {
    const taskId = 'test-task-123';
    const projectId = 'test-project-456';
    writeStrategicHelpers(taskId, projectId, testWorkspace);

    // Verify scripts have proper bash shebangs
    const strategicContent = fs.readFileSync(`${testWorkspace}/strategic`, 'utf-8');
    expect(strategicContent.startsWith('#!/bin/bash')).toBe(true);

    const fetchContent = fs.readFileSync(`${testWorkspace}/fetch`, 'utf-8');
    expect(fetchContent.startsWith('#!/bin/bash')).toBe(true);

    const designDocContent = fs.readFileSync(`${testWorkspace}/design-doc`, 'utf-8');
    expect(designDocContent.startsWith('#!/bin/bash')).toBe(true);
  });

  it('contains critical operational patterns in strategic script', () => {
    const taskId = 'test-task-123';
    const projectId = 'test-project-456';
    writeStrategicHelpers(taskId, projectId, testWorkspace);

    const content = fs.readFileSync(`${testWorkspace}/strategic`, 'utf-8');
    const requiredPatterns = [
      'set_problem_model',
      'set_running_update',
      'create_direction',
      'update_direction',
      'retire_direction',
      'approve_direction',
      'reject_direction',
      'set_retirement_critiques',
    ];
    requiredPatterns.forEach(pattern => {
      expect(content).toContain(pattern);
    });
  });

  it('contains critical operational patterns in fetch script', () => {
    const taskId = 'test-task-123';
    const projectId = 'test-project-456';
    writeStrategicHelpers(taskId, projectId, testWorkspace);

    const content = fs.readFileSync(`${testWorkspace}/fetch`, 'utf-8');
    expect(content).toContain('directions');
    expect(content).toContain('playbook');
    expect(content).toContain('gaps');
  });

  it('contains critical operational patterns in design-doc script', () => {
    const taskId = 'test-task-123';
    const projectId = 'test-project-456';
    writeStrategicHelpers(taskId, projectId, testWorkspace);

    const content = fs.readFileSync(`${testWorkspace}/design-doc`, 'utf-8');
    expect(content).toContain('design-doc');
    expect(content).toContain('/api/agent/design-doc');
  });

  it('handles HTTP 409 (ambiguous ref) on message fetch with clear signal', () => {
    const taskId = 'test-task-123';
    const projectId = 'test-project-456';
    writeStrategicHelpers(taskId, projectId, testWorkspace);

    const content = fs.readFileSync(`${testWorkspace}/fetch`, 'utf-8');
    // Verify the fetch script captures HTTP status and handles 409
    expect(content).toContain('http_code=$(printf');
    expect(content).toContain('if [ "$http_code" -eq 409 ]');
    expect(content).toContain('[AMBIGUOUS]');
    expect(content).toContain('multiple candidates found');
    // 409 should exit with error code (exit 1) so agent knows to retry
    expect(content).toContain('exit 1');
  });

  it('handles HTTP 404 and other non-2xx errors on fetch', () => {
    const taskId = 'test-task-123';
    const projectId = 'test-project-456';
    writeStrategicHelpers(taskId, projectId, testWorkspace);

    const content = fs.readFileSync(`${testWorkspace}/fetch`, 'utf-8');
    // Verify the fetch script has generic error handling for non-2xx responses
    expect(content).toContain('[ERROR] fetch $KIND failed');
    expect(content).toContain('HTTP $http_code');
    // Non-success responses should exit with error (exit 1)
    expect(content).toMatch(/exit 1[\s\n]*$/);
  });

  it('fetch script returns 0 only on 2xx success', () => {
    const taskId = 'test-task-123';
    const projectId = 'test-project-456';
    writeStrategicHelpers(taskId, projectId, testWorkspace);

    const content = fs.readFileSync(`${testWorkspace}/fetch`, 'utf-8');
    // First success path should be 2xx check with exit 0
    expect(content).toContain('if [ "$http_code" -ge 200 ] && [ "$http_code" -lt 300 ]');
    expect(content).toContain('exit 0');
  });

  it('ci-compliance is a no-id verb with correct case arm, usage entry, and id guard exemption', () => {
    const taskId = 'test-task-123';
    const projectId = 'test-project-456';
    writeStrategicHelpers(taskId, projectId, testWorkspace);

    const content = fs.readFileSync(`${testWorkspace}/fetch`, 'utf-8');
    // ci-compliance appears in the usage string.
    expect(content).toContain('fetch problem-model|direction|playbook|gap|message|section|knowledge|compute-sweeps|qualification|version-phase|vm-cost|efficiency-snapshot|ci-compliance|capability-trials|sections|task|tasks|completion-debt|ui-load-timing|skills|tools [id]');
    // ci-compliance has the correct case arm and PATH_SUFFIX.
    expect(content).toContain('ci-compliance)        PATH_SUFFIX="ci-compliance"');
    // ci-compliance is exempt from the id-required guard (no id needed).
    expect(content).toContain('[ "$KIND" != "ci-compliance" ]');
    // ci-compliance help text is meaningful.
    expect(content).toContain('ci-compliance');
  });

  it('completion-debt verb is reachable in generated fetch helper', () => {
    const taskId = 'test-task-123';
    const projectId = 'test-project-456';
    writeStrategicHelpers(taskId, projectId, testWorkspace);

    const content = fs.readFileSync(`${testWorkspace}/fetch`, 'utf-8');
    // completion-debt appears in the usage string.
    expect(content).toContain('fetch problem-model|direction|playbook|gap|message|section|knowledge|compute-sweeps|qualification|version-phase|vm-cost|efficiency-snapshot|ci-compliance|capability-trials|sections|task|tasks|completion-debt|ui-load-timing|skills|tools [id]');
    // completion-debt has the correct case arm and PATH_SUFFIX.
    expect(content).toContain('completion-debt)      PATH_SUFFIX="completion-debt"');
    // completion-debt is exempt from the id-required guard (no id needed).
    expect(content).toContain('[ "$KIND" != "completion-debt" ]');
  });
});

describe('writeFetchHelper (verb-subset read-only fetch)', () => {
  beforeEach(() => {
    if (!fs.existsSync(testWorkspace)) {
      fs.mkdirSync(testWorkspace, { recursive: true });
    }
  });
  afterEach(() => {
    if (fs.existsSync(testWorkspace)) {
      try { fs.rmSync(testWorkspace, { recursive: true }); } catch { /* ignore */ }
    }
  });

  const readFetch = (): string => fs.readFileSync(`${testWorkspace}/fetch`, 'utf-8');

  it('full strategic verb set is byte-identical to writeStrategicHelpers output', () => {
    // The strategic path must stay regression-free: the standalone writer with
    // STRATEGIC_FETCH_VERBS must emit the exact same script writeStrategicHelpers
    // emits, so the refactor cannot have changed the strategic agent's tooling.
    writeFetchHelper('t', 'proj-strat', testWorkspace, { verbs: STRATEGIC_FETCH_VERBS });
    const viaWriter = readFetch();
    writeStrategicHelpers('t', 'proj-strat', testWorkspace);
    const viaStrategic = readFetch();
    expect(viaWriter).toBe(viaStrategic);
    // Spot-check the load-bearing usage + case lines the strategic suite pins.
    expect(viaWriter).toContain('fetch problem-model|direction|playbook|gap|message|section|knowledge|compute-sweeps|qualification|version-phase|vm-cost|efficiency-snapshot|ci-compliance|capability-trials|sections|task|tasks|completion-debt|ui-load-timing|skills|tools [id]');
    expect(viaWriter).toContain('problem-model) PATH_SUFFIX="problem-model"');
    expect(viaWriter).toContain('message)    PATH_SUFFIX="messages/${ID:-open}"');
    expect(viaWriter).toContain('section)    PATH_SUFFIX="design-doc/$ID"');
    expect(viaWriter).toContain('compute-sweeps) PATH_SUFFIX="compute-sweeps${ID:+/$ID}"');
    expect(viaWriter).toContain('version-phase) PATH_SUFFIX="version-phase"');
    expect(viaWriter).toContain('completion-debt)      PATH_SUFFIX="completion-debt"');
  });

  it('bakes ONLY the granted verbs into the case statement (research subset)', () => {
    writeFetchHelper('t', 'proj-1', testWorkspace, { verbs: RESEARCH_FETCH_VERBS });
    const content = readFetch();
    // Granted verbs are real case branches. direction uses ${ID:+/$ID} so it
    // lists all directions when called without an id (compact form).
    expect(content).toContain('direction)  PATH_SUFFIX="directions${ID:+/$ID}"');
    expect(content).toContain('problem-model) PATH_SUFFIX="problem-model"');
    expect(content).toContain('gap)        PATH_SUFFIX="gaps/$ID"');
    // Non-granted verbs have NO resolving branch (check both old and new literal forms).
    expect(content).not.toContain('PATH_SUFFIX="playbook/$ID"');
    expect(content).not.toContain('PATH_SUFFIX="messages/${ID:-open}"');
    expect(content).not.toContain('PATH_SUFFIX="design-doc/$ID"');
    // Usage line lists only the granted subset (canonical order; qualification last).
    expect(content).toContain('Usage: fetch problem-model|direction|gap|knowledge|qualification [id]');
  });

  it('rejects a disallowed-but-valid verb with a clear not-permitted message', () => {
    writeFetchHelper('t', 'proj-1', testWorkspace, { verbs: RESEARCH_FETCH_VERBS });
    const content = readFetch();
    // playbook/message/section are full-vocabulary verbs not granted here — they
    // get an explicit reject branch rather than a generic "unknown kind".
    expect(content).toContain('playbook|message|section|compute-sweeps|version-phase|vm-cost|efficiency-snapshot|ci-compliance|capability-trials|sections|task|tasks|completion-debt|ui-load-timing|skills|tools) echo "Error: ./fetch \'$KIND\' is not permitted in this workspace (allowed: problem-model|direction|gap|knowledge|qualification)."');
  });

  it('different subsets bake different verb vocabularies (plan subset)', () => {
    writeFetchHelper('t', 'proj-2', testWorkspace, { verbs: PLAN_FETCH_VERBS });
    const content = readFetch();
    // direction uses ${ID:+/$ID}: lists all directions when called without an id.
    expect(content).toContain('direction)  PATH_SUFFIX="directions${ID:+/$ID}"');
    expect(content).toContain('section)    PATH_SUFFIX="design-doc/$ID"');
    expect(content).toContain('playbook)   PATH_SUFFIX="playbook/$ID"');
    expect(content).toContain('Usage: fetch direction|playbook|section|knowledge|qualification [id]');
    // problem-model / gap / message are rejected here.
    expect(content).toContain('problem-model|gap|message|compute-sweeps|version-phase|vm-cost|efficiency-snapshot|ci-compliance|capability-trials|sections|task|tasks|completion-debt|ui-load-timing|skills|tools) echo "Error: ./fetch \'$KIND\' is not permitted in this workspace (allowed: direction|playbook|section|knowledge|qualification)."');
  });

  it('different subsets bake different verb vocabularies (design-doc subset)', () => {
    writeFetchHelper('t', 'proj-3', testWorkspace, { verbs: DESIGN_DOC_FETCH_VERBS });
    const content = readFetch();
    // direction uses ${ID:+/$ID}; message uses messages/${ID:-open}: both give a
    // compact listing when called without an id.
    expect(content).toContain('direction)  PATH_SUFFIX="directions${ID:+/$ID}"');
    expect(content).toContain('section)    PATH_SUFFIX="design-doc/$ID"');
    expect(content).toContain('playbook)   PATH_SUFFIX="playbook/$ID"');
    expect(content).toContain('gap)        PATH_SUFFIX="gaps/$ID"');
    expect(content).toContain('message)    PATH_SUFFIX="messages/${ID:-open}"');
    expect(content).toContain('Usage: fetch direction|playbook|gap|message|section|knowledge [id]');
    // problem-model and qualification are rejected here because they are not part of the design-doc subset.
    expect(content).toContain('problem-model|compute-sweeps|qualification|version-phase|vm-cost|efficiency-snapshot|ci-compliance|capability-trials|sections|task|tasks|completion-debt|ui-load-timing|skills|tools) echo "Error: ./fetch \'$KIND\' is not permitted in this workspace (allowed: direction|playbook|gap|message|section|knowledge)."');
  });

  it('bakes the project id and escapes shell-injection attempts', () => {
    writeFetchHelper('t', 'proj$(echo hacked)id', testWorkspace, { verbs: RESEARCH_FETCH_VERBS });
    const content = readFetch();
    expect(content).toContain('PROJECT="proj\\$(echo hacked)id"');
    expect(content).not.toContain('PROJECT="proj$(echo hacked)id"');
    expect(/(^|[^\\])\$\(echo hacked\)/.test(content)).toBe(false);
  });

  it('writes an executable fetch script and a generalized empty-project guard', () => {
    writeFetchHelper('t', 'proj-1', testWorkspace, { verbs: RESEARCH_FETCH_VERBS });
    const stat = fs.statSync(`${testWorkspace}/fetch`);
    if (process.platform !== 'win32') {
      expect((stat.mode & 0o111) !== 0).toBe(true);
    }
    const content = readFetch();
    expect(content.startsWith('#!/bin/bash')).toBe(true);
    // The empty-project guard is generalized (no longer strategic-only wording).
    expect(content).toContain('no project_id available; ./fetch is unavailable in this workspace');
    expect(content).not.toContain('strategic-mode tasks');
  });

  it('provisions the SEARCH-shaped knowledge verb against the token-scoped endpoint', () => {
    writeFetchHelper('t', 'proj-k', testWorkspace, { verbs: STRATEGIC_FETCH_VERBS });
    const content = readFetch();
    // knowledge is a granted case arm.
    expect(content).toContain('  knowledge)');
    // Get-by-id hits /knowledge-documents/$1 (full body), NOT the project path.
    expect(content).toContain('-X GET "$API/api/agent/knowledge-documents/$1"');
    // List/search hits the bare list endpoint with URL-encoded query filters.
    expect(content).toContain('--get');
    expect(content).toContain('"$API/api/agent/knowledge-documents"');
    expect(content).toContain('--data-urlencode "topic=${2:-}"');
    expect(content).toContain('--data-urlencode "direction=${2:-}"');
    expect(content).toContain('--data-urlencode "q=${2:-}"');
    expect(content).toContain('--data-urlencode "source_kind=${2:-}"');
    expect(content).toContain('--data-urlencode "sweep_id=${2:-}"');
    expect(content).toContain('--data-urlencode "tag=${2:-}"');
    // It is token-scoped: no project_id is baked into the knowledge URL.
    expect(content).not.toContain('knowledge-documents/$PROJECT');
  });

  it('rejects the knowledge verb in subsets that do not grant it', () => {
    // A bespoke subset without knowledge must reject it as not-permitted.
    writeFetchHelper('t', 'proj-k', testWorkspace, { verbs: ['direction'] });
    const content = readFetch();
    expect(content).not.toContain('  knowledge)\n    shift');
    expect(content).toContain('not permitted in this workspace');
    // The reject branch enumerates knowledge among the disallowed verbs.
    expect(content).toMatch(/knowledge\|compute-sweeps\|qualification\|version-phase\|vm-cost\|efficiency-snapshot\|ci-compliance\|capability-trials\|sections\|task\|tasks\|completion-debt\|ui-load-timing\|skills\|tools\) echo "Error: \.\/fetch/);
  });

  it('ci-compliance is rejected in non-strategic subsets with a clear not-permitted message', () => {
    // ci-compliance is a strategic-only verb; research/plan/design-doc subsets must
    // reject it explicitly rather than silently passing to the "unknown kind" arm.
    writeFetchHelper('t', 'proj-ci', testWorkspace, { verbs: RESEARCH_FETCH_VERBS });
    const content = readFetch();
    // ci-compliance must NOT appear as a granted case arm.
    expect(content).not.toContain('ci-compliance)        PATH_SUFFIX="ci-compliance"');
    // ci-compliance appears in the reject branch with the not-permitted message.
    expect(content).toContain('not permitted in this workspace');
    expect(content).toContain('ci-compliance');
    // It is listed among the rejected verbs (not the allowed list).
    expect(content).toContain('playbook|message|section|compute-sweeps|version-phase|vm-cost|efficiency-snapshot|ci-compliance|capability-trials|sections|task|tasks|completion-debt|ui-load-timing|skills|tools) echo "Error: ./fetch');
  });
});

describe('buildFetchGuidance', () => {
  it('lists ONLY the granted verbs and advertises ./fetch', () => {
    const block = buildFetchGuidance(RESEARCH_FETCH_VERBS);
    expect(block).toContain('./fetch');
    // direction now uses [id] (optional) — compact list when id omitted.
    expect(block).toContain('`./fetch direction [id]`');
    expect(block).toContain('`./fetch problem-model`');
    expect(block).toContain('`./fetch gap <id>`');
    // Verbs not granted to research are absent from the guidance.
    expect(block).not.toContain('`./fetch section');
    expect(block).not.toContain('`./fetch message');
  });

  it('describes completion-debt when that strategic-only verb is granted', () => {
    const block = buildFetchGuidance(STRATEGIC_FETCH_VERBS);
    expect(block).toContain('`./fetch completion-debt`');
    expect(block).toContain('open completion-debt entries');
  });

  it('lists the architecture-focused design-doc verbs', () => {
    const block = buildFetchGuidance(DESIGN_DOC_FETCH_VERBS);
    // direction and message now use [id]/[ref] (optional) — compact list when omitted.
    expect(block).toContain('`./fetch direction [id]`');
    expect(block).toContain('`./fetch section <key>`');
    expect(block).toContain('`./fetch playbook <key>`');
    expect(block).toContain('`./fetch gap <id>`');
    expect(block).toContain('`./fetch message [ref]`');
    expect(block).not.toContain('`./fetch problem-model`');
  });

  it('describes the SEARCH-shaped knowledge verb when granted', () => {
    const block = buildFetchGuidance(RESEARCH_FETCH_VERBS);
    expect(block).toContain('`./fetch knowledge');
    expect(block).toContain('--topic');
    expect(block).toContain('--direction');
    expect(block).toContain('--sweep-id');
    // Not present when the subset omits it.
    expect(buildFetchGuidance(['direction'])).not.toContain('`./fetch knowledge');
  });

  it('describes version-phase when that strategic-only verb is granted', () => {
    const block = buildFetchGuidance(STRATEGIC_FETCH_VERBS);
    expect(block).toContain('`./fetch version-phase`');
    expect(block).toContain('version phase/lifecycle context');
  });

  it('describes compute-sweeps when that strategic-only verb is granted', () => {
    const block = buildFetchGuidance(STRATEGIC_FETCH_VERBS);
    expect(block).toContain('`./fetch compute-sweeps [sweep_id]`');
    expect(block).toContain('completed compute experiment sweeps');
  });

  it('returns empty string when no verbs are granted', () => {
    expect(buildFetchGuidance([])).toBe('');
  });
});

describe('renderStrategicFetchVerbHelpLines', () => {
  it('lists every verb in STRATEGIC_FETCH_VERBS with its description', () => {
    const lines = renderStrategicFetchVerbHelpLines();
    expect(lines).toHaveLength(STRATEGIC_FETCH_VERBS.length);
    for (const verb of STRATEGIC_FETCH_VERBS) {
      const match = lines.find(l => l.includes(`./fetch ${verb}`));
      expect(match, `expected a line advertising ./fetch ${verb}`).toBeDefined();
    }
  });

  it('includes the previously-omitted verbs', () => {
    const joined = renderStrategicFetchVerbHelpLines().join('\n');
    expect(joined).toContain('./fetch efficiency-snapshot');
    expect(joined).toContain('./fetch ci-compliance');
    expect(joined).toContain('./fetch qualification');
    expect(joined).toContain('./fetch compute-sweeps');
    expect(joined).toContain('./fetch vm-cost');
    expect(joined).toContain('./fetch knowledge');
  });
});

describe('parseStrategicChallengeRecord', () => {
  it('parses a well-formed ok record and normalizes evidence verdicts', () => {
    const raw = `Here is my verdict:
{
  "status": "ok",
  "evidence_progress": [
    {"direction_id": "d-1", "status": "advancing", "verdict": "approved", "rationale": "concrete CI evidence"},
    {"direction_id": "d-2", "status": "met", "verdict": "rejected", "rationale": "no proof"}
  ],
  "retirement_critique_quality": {"verdict": "solid", "rationale": "names the pattern"},
  "portfolio_shape_risk": {"verdict": "elevated", "rationale": "concentrated in release"}
}`;
    const rec = parseStrategicChallengeRecord(raw);
    expect(rec.status).toBe('ok');
    expect(rec.challenge_version).toBe(STRATEGIC_CHALLENGE_VERSION);
    expect(rec.evidence_progress).toHaveLength(2);
    expect(rec.evidence_progress[0]).toEqual({
      direction_id: 'd-1', status: 'advancing', verdict: 'approved', rationale: 'concrete CI evidence',
    });
    expect(rec.portfolio_shape_risk.verdict).toBe('elevated');
    expect(rec.retirement_critique_quality.verdict).toBe('solid');
  });

  it('fails closed: unknown/absent verdict on an evidence entry becomes "rejected"', () => {
    const raw = `{"status":"ok","evidence_progress":[
      {"direction_id":"d-3","status":"advancing","rationale":"maybe"},
      {"direction_id":"d-4","status":"met","verdict":"maybe-ish"}
    ],"retirement_critique_quality":{},"portfolio_shape_risk":{"verdict":"low"}}`;
    const rec = parseStrategicChallengeRecord(raw);
    expect(rec.evidence_progress.map(e => e.verdict)).toEqual(['rejected', 'rejected']);
  });

  it('drops evidence entries with no direction_id or an invalid status', () => {
    const raw = `{"status":"ok","evidence_progress":[
      {"status":"advancing","verdict":"approved"},
      {"direction_id":"d-5","status":"bogus","verdict":"approved"},
      {"direction_id":"d-6","status":"stalled","verdict":"rejected","rationale":"ok"}
    ],"retirement_critique_quality":{},"portfolio_shape_risk":{"verdict":"low"}}`;
    const rec = parseStrategicChallengeRecord(raw);
    expect(rec.evidence_progress).toHaveLength(1);
    expect(rec.evidence_progress[0].direction_id).toBe('d-6');
  });

  it('a self-declared blocked record approves nothing even if it carried verdicts', () => {
    const raw = `{"status":"blocked","evidence_progress":[
      {"direction_id":"d-7","status":"advancing","verdict":"approved"}
    ],"retirement_critique_quality":{},"portfolio_shape_risk":{"verdict":"low"}}`;
    const rec = parseStrategicChallengeRecord(raw);
    expect(rec.status).toBe('blocked');
    expect(rec.evidence_progress).toEqual([]);
  });

  it('returns an invalid record (no approvals) when output is not parseable JSON', () => {
    const rec = parseStrategicChallengeRecord('the challenger refused to answer');
    expect(rec.status).toBe('invalid');
    expect(rec.evidence_progress).toEqual([]);
    expect(rec.notes).toContain('not parseable');
  });

  it('fails closed: missing status is NOT treated as ok — evidence_progress is dropped', () => {
    // A response with no status field must not default to 'ok' and allow evidence through.
    const raw = `{"evidence_progress":[
      {"direction_id":"d-1","status":"advancing","verdict":"approved","rationale":"r"}
    ],"retirement_critique_quality":{"verdict":"solid"},"portfolio_shape_risk":{"verdict":"low"}}`;
    const rec = parseStrategicChallengeRecord(raw);
    expect(rec.status).toBe('blocked');
    expect(rec.evidence_progress).toEqual([]);
  });

  it('fails closed: unknown status value is NOT treated as ok — evidence_progress is dropped', () => {
    // An unrecognized status like 'error' or 'invalid' must not silently become 'ok'.
    const raw = `{"status":"error","evidence_progress":[
      {"direction_id":"d-2","status":"advancing","verdict":"approved","rationale":"r"}
    ],"retirement_critique_quality":{"verdict":"solid"},"portfolio_shape_risk":{"verdict":"low"}}`;
    const rec = parseStrategicChallengeRecord(raw);
    expect(rec.status).toBe('blocked');
    expect(rec.evidence_progress).toEqual([]);
  });

  it('fails closed: missing retirement_critique_quality returns invalid record', () => {
    // A missing required section means the challenge cannot be trusted — fail closed.
    const raw = `{"status":"ok","evidence_progress":[
      {"direction_id":"d-3","status":"advancing","verdict":"approved","rationale":"r"}
    ],"portfolio_shape_risk":{"verdict":"low"}}`;
    const rec = parseStrategicChallengeRecord(raw);
    expect(rec.status).toBe('invalid');
    expect(rec.evidence_progress).toEqual([]);
    expect(rec.notes).toContain('retirement_critique_quality');
  });

  it('fails closed: non-object portfolio_shape_risk returns invalid record', () => {
    // A present-but-non-object portfolio_shape_risk must NOT be treated as clean —
    // coerceVerdictBlock would map it to {verdict:'unspecified'} which the backend
    // treats as clean, so we must reject it before it reaches that path.
    const raw = `{"status":"ok","evidence_progress":[
      {"direction_id":"d-3","status":"advancing","verdict":"approved","rationale":"r"}
    ],"retirement_critique_quality":{"verdict":"solid"},"portfolio_shape_risk":"high"}`;
    const rec = parseStrategicChallengeRecord(raw);
    expect(rec.status).toBe('invalid');
    expect(rec.evidence_progress).toEqual([]);
    expect(rec.notes).toContain('portfolio_shape_risk');
  });

  it('normalizes retirement_approvals: approved and rejected verdicts', () => {
    const raw = `{
      "status": "ok",
      "evidence_progress": [],
      "retirement_approvals": [
        {"direction_id": "d-10", "verdict": "approved", "rationale": "real evidenced pattern"},
        {"direction_id": "d-11", "verdict": "rejected", "rationale": "shallow boilerplate"}
      ],
      "retirement_critique_quality": {"verdict": "solid"},
      "portfolio_shape_risk": {"verdict": "low"}
    }`;
    const rec = parseStrategicChallengeRecord(raw);
    expect(rec.status).toBe('ok');
    expect(rec.retirement_approvals).toHaveLength(2);
    expect(rec.retirement_approvals[0]).toEqual({
      direction_id: 'd-10', verdict: 'approved', rationale: 'real evidenced pattern',
    });
    expect(rec.retirement_approvals[1]).toEqual({
      direction_id: 'd-11', verdict: 'rejected', rationale: 'shallow boilerplate',
    });
  });

  it('retirement_approvals fails closed: unknown/absent verdict becomes "rejected"', () => {
    const raw = `{
      "status": "ok",
      "evidence_progress": [],
      "retirement_approvals": [
        {"direction_id": "d-12", "rationale": "no verdict field"},
        {"direction_id": "d-13", "verdict": "maybe", "rationale": "unknown verdict"}
      ],
      "retirement_critique_quality": {"verdict": "solid"},
      "portfolio_shape_risk": {"verdict": "low"}
    }`;
    const rec = parseStrategicChallengeRecord(raw);
    expect(rec.retirement_approvals).toHaveLength(2);
    expect(rec.retirement_approvals.map(r => r.verdict)).toEqual(['rejected', 'rejected']);
  });

  it('retirement_approvals drops entries with no direction_id', () => {
    const raw = `{
      "status": "ok",
      "evidence_progress": [],
      "retirement_approvals": [
        {"verdict": "approved", "rationale": "no direction_id"},
        {"direction_id": "d-14", "verdict": "approved", "rationale": "valid"}
      ],
      "retirement_critique_quality": {"verdict": "solid"},
      "portfolio_shape_risk": {"verdict": "low"}
    }`;
    const rec = parseStrategicChallengeRecord(raw);
    expect(rec.retirement_approvals).toHaveLength(1);
    expect(rec.retirement_approvals[0].direction_id).toBe('d-14');
  });

  it('empty/absent retirement_approvals parses to empty array on ok record', () => {
    const raw = `{
      "status": "ok",
      "evidence_progress": [],
      "retirement_critique_quality": {"verdict": "solid"},
      "portfolio_shape_risk": {"verdict": "low"}
    }`;
    const rec = parseStrategicChallengeRecord(raw);
    expect(rec.retirement_approvals).toEqual([]);
  });

  it('blocked record produces retirement_approvals=[] even if challenger emitted them', () => {
    const raw = `{
      "status": "blocked",
      "evidence_progress": [],
      "retirement_approvals": [{"direction_id": "d-15", "verdict": "approved"}],
      "retirement_critique_quality": {"verdict": "solid"},
      "portfolio_shape_risk": {"verdict": "low"}
    }`;
    const rec = parseStrategicChallengeRecord(raw);
    expect(rec.status).toBe('blocked');
    expect(rec.retirement_approvals).toEqual([]);
  });

  it('emptyChallengeRecord (blocked/invalid) has retirement_approvals=[]', () => {
    const rec = parseStrategicChallengeRecord('not json');
    expect(rec.status).toBe('invalid');
    expect(rec.retirement_approvals).toEqual([]);
  });

  it('normalizes portfolio operation verdicts', () => {
    const raw = `{
      "status": "ok",
      "evidence_progress": [],
      "retirement_critique_quality": {"verdict": "solid", "rationale": "names the pattern"},
      "portfolio_shape_risk": {"verdict": "low", "rationale": "balanced"},
      "portfolio_operations": [
        {"op": "retire_direction", "direction_id": "d-keep", "verdict": "Rejected", "rationale": "no proof the lane is exhausted"},
        {"op": "create", "text": "Build a Widget Pipeline", "verdict": "warned", "rationale": "over-concentrates release"},
        {"op": "stage_direction", "direction_id": "d-next", "verdict": "approved"},
        {"op": "bogus_op", "direction_id": "d-x", "verdict": "rejected"},
        {"op": "update_direction", "direction_id": "d-y", "verdict": "maybe-ish"},
        {"op": "retire_direction", "direction_id": "d-keep", "verdict": "warned", "rationale": "duplicate fingerprint — dropped"}
      ]
    }`;
    const rec = parseStrategicChallengeRecord(raw);
    expect(rec.status).toBe('ok');
    expect(rec.portfolio_operations).toHaveLength(3);

    const retire = rec.portfolio_operations[0];
    expect(retire.op).toBe('retire_direction');
    expect(retire.verdict).toBe('rejected');
    expect(retire.direction_id).toBe('d-keep');
    expect(retire.fingerprint).toBe(
      portfolioOperationFingerprint('retire_direction', { directionId: 'd-keep' }),
    );

    const create = rec.portfolio_operations[1];
    expect(create.op).toBe('create_direction');
    expect(create.verdict).toBe('warned');
    expect(create.direction_id).toBe('');
    expect(create.text).toBe('Build a Widget Pipeline');
    expect(create.fingerprint).toBe(
      portfolioOperationFingerprint('create_direction', { text: 'build a widget pipeline' }),
    );
    expect(create.fingerprint).toMatch(/^create_direction:text:[0-9a-f]{16}$/);

    expect(rec.portfolio_operations[2]).toMatchObject({
      op: 'stage_direction', direction_id: 'd-next', verdict: 'approved',
    });
    expect(rec.retirement_critique_quality.verdict).toBe('solid');
    expect(rec.portfolio_shape_risk.verdict).toBe('low');
  });

  it('blocked record drops portfolio_operations entirely', () => {
    const raw = `{"status":"blocked","evidence_progress":[],
      "retirement_critique_quality":{"verdict":"not_assessed"},
      "portfolio_shape_risk":{"verdict":"not_assessed"},
      "portfolio_operations":[
        {"op":"retire_direction","direction_id":"d-1","verdict":"rejected"}
      ]}`;
    const rec = parseStrategicChallengeRecord(raw);
    expect(rec.status).toBe('blocked');
    expect(rec.portfolio_operations).toEqual([]);
  });

  it('reclassifies a text-only stage_direction verdict to create_direction', () => {
    // A stage_direction verdict carrying text but no direction_id is a vote to
    // STAGE A NEW direction, which executes as create_direction(staged=true). The
    // agent-side fingerprint must match the backend's stored create_direction op,
    // while staging an EXISTING direction by id stays a real stage_direction.
    const raw = `{
      "status": "ok",
      "evidence_progress": [],
      "retirement_critique_quality": {"verdict": "solid", "rationale": "ok"},
      "portfolio_shape_risk": {"verdict": "low", "rationale": "ok"},
      "portfolio_operations": [
        {"op": "stage_direction", "text": "Stand up a local-GPU runner lane", "verdict": "approved", "rationale": "clean new lane"},
        {"op": "stage_direction", "direction_id": "d-keep", "verdict": "approved", "rationale": "existing dir staged by id"}
      ]
    }`;
    const rec = parseStrategicChallengeRecord(raw);
    expect(rec.status).toBe('ok');
    expect(rec.portfolio_operations).toHaveLength(2);

    const stagedCreate = rec.portfolio_operations[0];
    expect(stagedCreate.op).toBe('create_direction');
    expect(stagedCreate.direction_id).toBe('');
    expect(stagedCreate.text).toBe('Stand up a local-GPU runner lane');
    expect(stagedCreate.fingerprint).toBe(
      portfolioOperationFingerprint('create_direction', { text: 'Stand up a local-GPU runner lane' }),
    );
    expect(stagedCreate.fingerprint).toMatch(/^create_direction:text:[0-9a-f]{16}$/);

    const stagedExisting = rec.portfolio_operations[1];
    expect(stagedExisting.op).toBe('stage_direction');
    expect(stagedExisting.direction_id).toBe('d-keep');
    expect(stagedExisting.fingerprint).toBe(
      portfolioOperationFingerprint('stage_direction', { directionId: 'd-keep' }),
    );
  });
});

describe('buildStrategicChallengeExecutorSection', () => {
  const base = {
    challenge_version: 1,
    retirement_approvals: [] as Array<{ direction_id: string; verdict: 'approved' | 'rejected'; rationale: string }>,
    retirement_critique_quality: { verdict: 'solid', rationale: '' },
    portfolio_shape_risk: { verdict: 'low', rationale: '' },
    portfolio_operations: [] as Array<{ op: string; direction_id: string; text: string; fingerprint: string; verdict: 'approved' | 'rejected' | 'warned' | 'uncertain'; rationale: string }>,
  };

  it('lists only challenge-approved advancing/met pairs as the gate', () => {
    const section = buildStrategicChallengeExecutorSection({
      ...base,
      status: 'ok',
      evidence_progress: [
        { direction_id: 'd-1', status: 'advancing', verdict: 'approved', rationale: '' },
        { direction_id: 'd-2', status: 'met', verdict: 'rejected', rationale: '' },
        { direction_id: 'd-3', status: 'stalled', verdict: 'approved', rationale: '' },
      ],
    });
    expect(section).toContain('d-1 → advancing');
    // Rejected (d-2) and stalled (d-3) are not gated approvals — they may appear
    // verbatim in the embedded record JSON, but never as an approved "→" pair.
    expect(section).not.toContain('d-2 → met');
    expect(section).not.toContain('d-3 → stalled');
    expect(section).toContain('Evidence-progress gate (REQUIRED');
  });

  it('a blocked record approves nothing and says so', () => {
    const section = buildStrategicChallengeExecutorSection({
      ...base,
      status: 'blocked',
      evidence_progress: [],
      retirement_approvals: [],
    });
    expect(section).toContain('approving NOTHING');
    expect(section).toContain('(none —');
  });

  it('includes portfolio-operation gate output', () => {
    const section = buildStrategicChallengeExecutorSection({
      ...base,
      status: 'ok',
      evidence_progress: [],
      retirement_approvals: [],
      portfolio_operations: [
        { op: 'retire_direction', direction_id: 'd-1', text: '', fingerprint: 'retire_direction:d-1', verdict: 'rejected', rationale: 'unsafe' },
        { op: 'stage_direction', direction_id: 'd-2', text: '', fingerprint: 'stage_direction:d-2', verdict: 'warned', rationale: 'advisory' },
        { op: 'create_direction', direction_id: '', text: 'build x', fingerprint: 'create_direction:text:1234abcd1234abcd', verdict: 'approved', rationale: 'clean' },
      ],
    });
    expect(section).toContain('Portfolio-operation gate');
    expect(section).toContain('REJECTED');
    expect(section).toContain('WARNED');
    expect(section).toContain('APPROVED');
  });
});

describe('runStrategicChallenge', () => {
  const baseOpts = {
    analysisText: 'analysis',
    dirSummaryText: 'dirs',
    pastRetirementCritiquesSection: '',
    portfolioCompositionText: 'portfolio',
    ws: null,
    statusRef: { value: '' },
    disallowedTools: [] as string[],
    sendLog: vi.fn(),
  };

  it('returns a parsed ok record when the challenge pass exits 0 with valid JSON', async () => {
    const runClaudeCode = vi.fn().mockResolvedValue({
      exitCode: 0,
      allOutput: '',
      lastResultText: '{"status":"ok","evidence_progress":[{"direction_id":"d-1","status":"advancing","verdict":"approved"}],"retirement_critique_quality":{"verdict":"solid"},"portfolio_shape_risk":{"verdict":"low"}}',
    });
    const rec = await runStrategicChallenge({ ...baseOpts, runClaudeCode });
    expect(rec.status).toBe('ok');
    expect(rec.evidence_progress).toHaveLength(1);
    // The prompt the challenger received carries the thinker analysis verbatim.
    expect(String(runClaudeCode.mock.calls[0][0])).toContain('analysis');
  });

  it('records a blocked record (no approvals) when the pass exits non-zero', async () => {
    const runClaudeCode = vi.fn().mockResolvedValue({ exitCode: 1, allOutput: 'boom', lastResultText: '' });
    const rec = await runStrategicChallenge({ ...baseOpts, runClaudeCode });
    expect(rec.status).toBe('blocked');
    expect(rec.evidence_progress).toEqual([]);
  });

  it('records a blocked record when the runner throws a generic error', async () => {
    const runClaudeCode = vi.fn().mockRejectedValue(new Error('network down'));
    const rec = await runStrategicChallenge({ ...baseOpts, runClaudeCode });
    expect(rec.status).toBe('blocked');
  });

  it('propagates a VmConfigMissingError instead of swallowing it as blocked', async () => {
    const runClaudeCode = vi.fn().mockRejectedValue(new VmConfigMissingError('provider config missing'));
    await expect(runStrategicChallenge({ ...baseOpts, runClaudeCode })).rejects.toBeInstanceOf(
      VmConfigMissingError,
    );
  });

  it('records an invalid record when the pass exits 0 but output is unparseable', async () => {
    const runClaudeCode = vi.fn().mockResolvedValue({ exitCode: 0, allOutput: 'no json here', lastResultText: 'nope' });
    const rec = await runStrategicChallenge({ ...baseOpts, runClaudeCode });
    expect(rec.status).toBe('invalid');
    expect(rec.evidence_progress).toEqual([]);
  });
});

describe('buildStrategicChallengePrompt', () => {
  it('embeds the analysis and demands a strict JSON verdict object', () => {
    const prompt = buildStrategicChallengePrompt({
      analysisText: 'THINKER_SAID_THIS',
      dirSummaryText: 'D-LIST',
      pastRetirementCritiquesSection: 'CRITIQUES',
      portfolioCompositionText: 'PORTFOLIO',
    });
    expect(prompt).toContain('THINKER_SAID_THIS');
    expect(prompt).toContain('D-LIST');
    expect(prompt).toContain('portfolio_shape_risk');
    expect(prompt).toContain('portfolio_operations');
    expect(prompt).toContain('Output ONLY a single JSON object');
  });

  it('includes retirement_approvals field name and instruction', () => {
    const prompt = buildStrategicChallengePrompt({
      analysisText: 'analysis',
      dirSummaryText: 'dirs',
      pastRetirementCritiquesSection: '',
      portfolioCompositionText: 'portfolio',
    });
    expect(prompt).toContain('retirement_approvals');
    expect(prompt).toContain('"verdict": "approved|rejected"');
    // Instructs challenger to emit an entry per proposed retirement.
    expect(prompt).toContain('EVERY direction the thinker proposes to retire');
    // Fail-closed instruction present.
    expect(prompt).toContain('When in doubt, REJECT');
  });
});

describe('buildStrategicChallengeExecutorSection (retirement gate)', () => {
  const base = {
    challenge_version: 1,
    retirement_critique_quality: { verdict: 'solid', rationale: '' },
    portfolio_shape_risk: { verdict: 'low', rationale: '' },
    portfolio_operations: [] as Array<{ op: string; direction_id: string; text: string; fingerprint: string; verdict: 'approved' | 'rejected' | 'warned' | 'uncertain'; rationale: string }>,
  };

  it('lists challenge-approved retirement direction IDs in the retirement gate block', () => {
    const section = buildStrategicChallengeExecutorSection({
      ...base,
      status: 'ok',
      evidence_progress: [],
      retirement_approvals: [
        { direction_id: 'd-20', verdict: 'approved', rationale: 'evidenced' },
        { direction_id: 'd-21', verdict: 'rejected', rationale: 'shallow' },
      ],
    });
    expect(section).toContain('Retirement gate');
    expect(section).toContain('  - d-20');
    // Rejected retirement must not appear in the approved-retirements list section.
    // (d-21 appears in the embedded JSON record, but not as an approved "  - d-21" bullet.)
    expect(section).not.toContain('  - d-21');
  });

  it('retirement gate shows none when no retirements were approved', () => {
    const section = buildStrategicChallengeExecutorSection({
      ...base,
      status: 'ok',
      evidence_progress: [],
      retirement_approvals: [
        { direction_id: 'd-22', verdict: 'rejected', rationale: 'unsupported' },
      ],
    });
    expect(section).toContain('Retirement gate');
    expect(section).toContain('(none — no retirement was approved this cycle)');
  });

  it('explains that a deferred retire response is a soft non-error outcome', () => {
    const section = buildStrategicChallengeExecutorSection({
      ...base,
      status: 'ok',
      evidence_progress: [],
      retirement_approvals: [],
    });
    expect(section).toContain('"status": "deferred"');
    expect(section).toContain('SOFT NON-ERROR OUTCOME');
    expect(section).toContain('Do NOT re-attempt the retire-direction call');
  });

  it('blocked record retirement gate shows none and mentions approving NOTHING', () => {
    const section = buildStrategicChallengeExecutorSection({
      ...base,
      status: 'blocked',
      evidence_progress: [],
      retirement_approvals: [],
    });
    expect(section).toContain('approving NOTHING');
    expect(section).toContain('(none — no retirement was approved this cycle)');
  });
});

// ── Shared _post_with_retry function across all write helpers ─────────────────
// These tests verify that the bounded 429/5xx retry logic with Retry-After
// honoring is present in ALL generated write-capable helper scripts.
describe('shared _post_with_retry retry logic across all write helpers', () => {
  const ws = path.join(os.tmpdir(), 'test-shared-retry-workspace');

  beforeEach(() => {
    if (!fs.existsSync(ws)) fs.mkdirSync(ws, { recursive: true });
  });
  afterEach(() => {
    if (fs.existsSync(ws)) {
      try { fs.rmSync(ws, { recursive: true }); } catch { /* ignore */ }
    }
  });

  // Helper: verify the _post_with_retry function is present in a script and has
  // the correct retry structure: 429, 5xx, Retry-After, max=4, 5s/15s/30s backoff.
  function assertRetryPresent(script: string, label: string): void {
    // Shared function signature
    expect(script, `${label}: missing _post_with_retry function`).toContain('_post_with_retry()');
    // max=4 attempts (3 retry slots + initial)
    expect(script, `${label}: expected max=4`).toContain('max=4');
    // Retry on 429 (lock contention) and 5xx (transient)
    expect(script, `${label}: missing 429 retry`).toContain('"$http_code" = "429"');
    expect(script, `${label}: missing 5xx retry`).toContain('"$http_code" -ge 500');
    // Retry-After header honored
    expect(script, `${label}: missing dump file for Retry-After`).toContain('-D "$dump"');
    expect(script, `${label}: missing retry-after header parse`).toContain("grep -i '^retry-after:'");
    expect(script, `${label}: missing sleep "$delay"`).toContain('sleep "$delay"');
    // Backoff schedule: 5s/15s/30s (not attempt*2)
    expect(script, `${label}: missing 5s backoff`).toContain('delay=5');
    expect(script, `${label}: missing 15s backoff`).toContain('delay=15');
    expect(script, `${label}: missing 30s backoff`).toContain('delay=30');
    // Auth retry: re-reads token on 401/403
    expect(script, `${label}: missing auth retry`).toContain('_load_agent_token || return 1');
    // Passes bash -n syntax check
    const tmpFile = path.join(os.tmpdir(), `retry_check_${Date.now()}.sh`);
    fs.writeFileSync(tmpFile, script);
    try {
      expect(() => execFileSync('bash', ['-n', tmpFile], { stdio: 'pipe' }), `${label}: bash -n failed`).not.toThrow();
    } finally {
      try { fs.unlinkSync(tmpFile); } catch { /* ignore */ }
    }
  }

  it('./messages send/resolve use _post_with_retry (not a bare curl)', () => {
    writeMessagesHelper('t-retry', ws);
    const script = fs.readFileSync(`${ws}/messages`, 'utf-8');
    assertRetryPresent(script, 'messages');
    // Both send and resolve route through the shared retry wrapper.
    expect(script).toContain('_post_with_retry "$API/api/agent/strategic" "messages $OP"');
    // No bare curl with -f (fail-fast) in the write path.
    expect(script).not.toContain('curl -f');
  });

  it('./playbook set uses _post_with_retry (not a bare curl)', () => {
    writePlaybookHelper('t-retry', ws);
    const script = fs.readFileSync(`${ws}/playbook`, 'utf-8');
    assertRetryPresent(script, 'playbook');
    // The set write op routes through the shared retry wrapper.
    expect(script).toContain('_post_with_retry "$API/api/agent/playbook" "playbook set"');
    // Read ops (get/list) remain fail-fast (no retry wrapper).
    expect(script).toContain('curl -f -sS');
  });

  it('./design-doc set/delete use _post_with_retry (not a bare curl)', () => {
    writeDesignDocHelper('t-retry', ws);
    const script = fs.readFileSync(`${ws}/design-doc`, 'utf-8');
    assertRetryPresent(script, 'design-doc');
    // set and delete route through the shared retry wrapper.
    expect(script).toContain('_post_with_retry "$API/api/agent/design-doc" "design-doc set"');
    expect(script).toContain('_post_with_retry "$API/api/agent/design-doc" "design-doc delete"');
    // Read ops (get/list) remain fail-fast via _post_once.
    expect(script).toContain('_post_once()');
  });

  it('./note uses _post_with_retry (not a bare curl)', () => {
    provisionAmbientHelpers('t-retry', ws);
    const script = fs.readFileSync(`${ws}/note`, 'utf-8');
    assertRetryPresent(script, 'note');
    // The annotation POST routes through the shared retry wrapper.
    expect(script).toContain('_post_with_retry "$API/api/agent/annotate" "note $TYPE"');
    // No bare curl with -f in the write path.
    expect(script).not.toContain('curl -f');
  });

  it('429 without Retry-After uses the 5s/15s/30s schedule (not attempt*2)', () => {
    // The previous backoff was attempt*2 (2s, 4s, 6s). The new schedule is 5s/15s/30s.
    // Verify that ALL write helpers use the new schedule and none use attempt*2.
    writeMessagesHelper('t-backoff', ws);
    const messages = fs.readFileSync(`${ws}/messages`, 'utf-8');
    expect(messages).not.toContain('delay=$((attempt * 2))');
    expect(messages).toContain('delay=5');
    expect(messages).toContain('delay=15');
    expect(messages).toContain('delay=30');

    writePlaybookHelper('t-backoff', ws);
    const playbook = fs.readFileSync(`${ws}/playbook`, 'utf-8');
    expect(playbook).not.toContain('delay=$((attempt * 2))');
    expect(playbook).toContain('delay=5');
    expect(playbook).toContain('delay=15');
    expect(playbook).toContain('delay=30');

    writeDesignDocHelper('t-backoff', ws);
    const designDoc = fs.readFileSync(`${ws}/design-doc`, 'utf-8');
    expect(designDoc).not.toContain('delay=$((attempt * 2))');
    expect(designDoc).toContain('delay=5');
    expect(designDoc).toContain('delay=15');
    expect(designDoc).toContain('delay=30');

    provisionAmbientHelpers('t-backoff', ws);
    const note = fs.readFileSync(`${ws}/note`, 'utf-8');
    expect(note).not.toContain('delay=$((attempt * 2))');
    expect(note).toContain('delay=5');
    expect(note).toContain('delay=15');
    expect(note).toContain('delay=30');
  });

  it('429 with Retry-After: the header value takes priority over the default backoff', () => {
    // Every write helper must honor a Retry-After header by reading the dump file
    // and using it as the sleep duration, overriding the default 5s/15s/30s schedule.
    writeMessagesHelper('t-retry-after', ws);
    const script = fs.readFileSync(`${ws}/messages`, 'utf-8');
    // Header is captured to a dump file each attempt.
    expect(script).toContain('dump=$(mktemp)');
    expect(script).toContain('-D "$dump"');
    // Retry-After is extracted from the dump (case-insensitive).
    expect(script).toContain("grep -i '^retry-after:'");
    // If delay is non-empty (from Retry-After), it skips the default schedule.
    expect(script).toContain('if [ -z "$delay" ]');
    // Sleep uses the extracted/computed delay.
    expect(script).toContain('sleep "$delay"');
  });

  it('read-only design-doc workspace does not get the retry function', () => {
    writeDesignDocHelper('t-ro', ws, { readOnly: true });
    const script = fs.readFileSync(`${ws}/design-doc`, 'utf-8');
    // Read-only mode must NOT include the write retry path.
    expect(script).not.toContain('_post_with_retry');
    expect(script).not.toContain('delay=5');
    // But it still has the fail-fast read helper.
    expect(script).toContain('_post_once()');
  });
});
