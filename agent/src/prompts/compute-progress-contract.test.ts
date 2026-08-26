/**
 * Regression guard: the compute-experiment AUTHORING PROMPTS must name the real
 * progress signal.
 *
 * THE DEFECT THIS PINS. The no-progress watchdog polls the MODIFICATION TIME of
 * the file named by `$BLUEPRINT_PROGRESS_FILE` and nothing else. Ground truth,
 * all on the backend/runner side:
 *
 *   - backend/app/compute_backends.py  `PROGRESS_FILE_ENV = "BLUEPRINT_PROGRESS_FILE"`
 *     and the wrapper's watchdog block ("Nothing but the experiment's own command
 *     ever writes $BLUEPRINT_PROGRESS_FILE").
 *   - scripts/local_runner.py          `os.stat(progress_file).st_mtime`, and the
 *     kill that follows with `exit_code=125` / "no-progress watchdog ... exceeded".
 *   - backend/app/_compute_scripts/heartbeat_body.py  reads that file as JSONL,
 *     one `{"metric","value"}` record per line.
 *
 * The prompts used to tell the authoring agent that `declared_metric` was the
 * "PROGRESS signal the watchdog reads". `declared_metric` is a RESULT-FILE key
 * used for scoring and arm comparison; nothing in the watchdog path ever reads
 * it. An agent that faithfully followed the prompt — declaring the metric and
 * never touching `$BLUEPRINT_PROGRESS_FILE` — had its job killed at exactly
 * `no_progress_timeout_seconds`, worst on the long training runs the feature
 * exists to serve.
 *
 * These assertions are deliberately about the PROMPT CORPUS rather than one
 * builder: the same false claim had been copied into four separate prompt sites,
 * so a guard bound to a single call site would not have caught it.
 */
import { describe, it, expect, vi } from 'vitest';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

vi.mock('../index.js', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../index.js')>();
  return {
    ...actual,
    WORKSPACE: '/tmp/workspace',
    sendLog: vi.fn(),
    runClaudeCode: vi.fn(),
    persistAndPostStatus: vi.fn(),
  };
});

import { buildThinkerPrompt, type ThinkerPromptParams } from './thinker.js';

const SRC = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');

/**
 * Every prompt file that documents the compute-experiment launch contract to an
 * authoring agent. Adding a new one WITHOUT the heartbeat rule should fail here.
 */
const AUTHORING_PROMPT_FILES = [
  'prompts/thinker.ts',
  'handlers/research-plan.ts',
  'handlers/strategic.ts',
  'handlers/strategic-ops-registry.ts',
];

const read = (rel: string): string => fs.readFileSync(path.join(SRC, rel), 'utf-8');

/**
 * Phrasings that assert the watchdog reads the METRIC. Matched case-insensitively
 * against collapsed whitespace so a reflow or a line-wrap cannot smuggle the
 * claim back in.
 */
const FALSE_WATCHDOG_CLAIMS = [
  /metric is the progress signal the watchdog reads/i,
  /that metric is the PROGRESS signal/i,
  /declared_metric[^.]{0,80}\bis the (?:progress|liveness) signal/i,
  /no_progress_timeout_seconds, the watchdog's progress signal/i,
];

const collapse = (s: string): string => s.replace(/\s+/g, ' ');

describe('compute-experiment authoring prompts name the real progress signal', () => {
  it.each(AUTHORING_PROMPT_FILES)(
    '%s does not claim declared_metric is the watchdog progress signal',
    (rel) => {
      const text = collapse(read(rel));
      for (const pattern of FALSE_WATCHDOG_CLAIMS) {
        expect(
          text,
          `${rel} still describes declared_metric as the watchdog's progress ` +
            `signal (matched ${pattern}). The watchdog stats the mtime of ` +
            `$BLUEPRINT_PROGRESS_FILE; declared_metric is a $BLUEPRINT_RESULT_FILE ` +
            `key used for scoring/arm comparison only.`,
        ).not.toMatch(pattern);
      }
    },
  );

  it.each(AUTHORING_PROMPT_FILES)(
    '%s names $BLUEPRINT_PROGRESS_FILE wherever it arms no_progress_timeout_seconds',
    (rel) => {
      const text = read(rel);
      // Only files that actually teach the watchdog knob owe the heartbeat rule.
      if (!text.includes('no_progress_timeout_seconds')) return;
      expect(
        text,
        `${rel} documents no_progress_timeout_seconds but never names ` +
          `BLUEPRINT_PROGRESS_FILE, so a compliant agent arms a watchdog it is ` +
          `never told how to feed and the job is killed at the timeout.`,
      ).toContain('BLUEPRINT_PROGRESS_FILE');
    },
  );

  it('teaches the append-JSONL heartbeat idiom, not a rewrite', () => {
    const text = collapse(read('prompts/thinker.ts'));
    expect(text).toMatch(/append/i);
    expect(text).toMatch(/\bJSONL\b/);
    // The mtime — not the content — is what keeps the job alive; say so.
    expect(text).toMatch(/mtime|modification time/i);
    // Name the consequence so the rule reads as load-bearing, not decorative.
    expect(text).toMatch(/125/);
    expect(text).toMatch(/no_progress_timeout/);
  });

  it('teaches the platform Python contract with a container-safe fallback', () => {
    for (const rel of ['prompts/thinker.ts', 'handlers/research-plan.ts']) {
      const text = read(rel);
      expect(text, `${rel} never mentions BLUEPRINT_PYTHON`).toContain('BLUEPRINT_PYTHON');
      expect(text, `${rel} never mentions BLUEPRINT_PIP`).toContain('BLUEPRINT_PIP');
      // BLUEPRINT_PYTHON is bwrap-lane only — it is ABSENT inside a
      // container_image (scripts/runner_isolation.py: "a container image ships
      // its own interpreter", asserted by
      // scripts/test_runner_isolation.py::test_bwrap_plan_without_runtime_is_unchanged).
      // Teaching a bare "$BLUEPRINT_PYTHON" would therefore break the container
      // lane, so the prompts must teach the defaulting form.
      expect(
        text,
        `${rel} must teach the defaulting form BLUEPRINT_PYTHON:-python3 — the ` +
          `var is absent inside a container_image, so the bare form breaks that lane.`,
      ).toContain('BLUEPRINT_PYTHON:-python3');
    }
  });

  it('the built thinker prompt actually carries the heartbeat rule', () => {
    const params: ThinkerPromptParams = {
      contextPrefix: '',
      projectName: 'Test Project',
      clockHeader: 'Clock: 2026-08-25',
      isSelfRepo: false,
      userStateLine: 'User last active: today',
      packerMetaSection: '',
      strategicContext: '',
      problemModelSection: '## problem_model\n_(empty)_',
      problemModelRecovery: 'empty',
      reflectionSection: '',
      selfImprovementBlock: '',
      runningUpdateSection: '',
      designDocJson: '',
      visionJson: '',
      outcomeSection: '',
      previousDecisionOutcomes: '',
      pastRetirementCritiquesSection: '',
      retiredDirectionsSummarySection: '',
      directionPortfolioSection: '',
      userPausedSection: '',
      rejectedProposalsSection: '',
      operatorOverridesSection: '',
      modelHealthSection: '',
      systemMaturitySection: '',
      driftSection: '',
      recentWorkSection: '',
      feedbackSection: '',
      questionSection: '',
      threadSection: '',
      taskId: 'test-task-progress-contract',
      versionPhaseSection: '',
      memoCitationsSection: '',
      efficiencySnapshotSection: '',
    };
    const prompt = buildThinkerPrompt(params);
    expect(prompt).toContain('$BLUEPRINT_PROGRESS_FILE');
    expect(prompt).toContain('BLUEPRINT_PYTHON:-python3');
    for (const pattern of FALSE_WATCHDOG_CLAIMS) {
      expect(collapse(prompt)).not.toMatch(pattern);
    }
  });
});

describe('compute-experiment handler docstring describes the real contract', () => {
  const docstring = (): string => {
    const text = read('handlers/compute-experiment.ts');
    const end = text.indexOf('*/');
    expect(end, 'compute-experiment.ts has no leading block comment').toBeGreaterThan(0);
    return text.slice(0, end);
  };

  it('does not name payload fields that do not exist', () => {
    // None of these are real launch-contract keys; they were invented by the
    // original docstring and never existed in payload_schemas or the handler.
    for (const phantom of ['compute_type', 'resource_config', 'setup_steps', 'run_command']) {
      expect(
        docstring(),
        `docstring names "${phantom}", which is not a real launch-contract field`,
      ).not.toContain(phantom);
    }
  });

  it('does not claim an agent-side poll loop the handler explicitly disclaims', () => {
    const doc = collapse(docstring());
    // The handler body states: "There is also no agent-side poll loop".
    expect(doc).not.toMatch(/polls\s+backend ComputeJob endpoints until terminal/i);
    expect(doc).not.toMatch(/monitors it to a terminal state/i);
    // ...and states the real shape instead.
    expect(doc).toMatch(/park/i);
    expect(doc).toContain('compute_job_terminal');
  });

  it('names the real progress-signal contract', () => {
    const doc = docstring();
    expect(doc).toContain('BLUEPRINT_PROGRESS_FILE');
    expect(doc).toContain('declared_metric');
    expect(collapse(doc)).toMatch(/not a liveness signal/i);
  });
});
