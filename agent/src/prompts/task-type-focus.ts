// Task-type-specific self-review focus blocks. Each block overlays the generic
// CODE_CHANGE_HELPER_GUIDANCE with concrete, type-specific scrutiny targeting
// the dominant miss patterns observed per task type in sr_catch_rate_trends.
// Handlers compose these into the prompt via getTaskTypeFocusBlock(taskType).

// Shared scope-drift incidental allowlist used across create/fix/refactor/docs tasks.
// Ensures consistent, comprehensive documentation of files that don't count as scope-drift.
const SCOPE_DRIFT_INCIDENTAL_ALLOWLIST_DESC =
  'lockfiles (`package-lock.json`, `poetry.lock`, `Cargo.lock`, `go.sum`), generated outputs, `.gitignore`, sibling test files (`.test.*`/`.spec.*`)';

// Shared cross-file consistency check instruction used in create and fix task focuses.
// Extracted to a single source of truth so examples and recording format stay consistent
// without drift between the two copies.
const CROSS_FILE_CONSISTENCY_FOCUS =
  '**Cross-file consistency** — when the diff touches multiple files, identify the dominant ' +
  "pattern introduced or modified (e.g., 'added null guard before .map calls', 'switched " +
  "timestamps to UTC-aware', 'added try/except around external HTTP call'). Enumerate every " +
  'touched file and attest that the pattern is applied consistently. If the pattern is ' +
  'inconsistently applied across files, record under `correctness` with `passed: false` ' +
  'and the issue: "pattern X inconsistently applied: present in [files], missing/divergent in [files]".';

// Builds the migrate-task self-review focus block. Defaults reproduce
// Blueprint's own layout byte-for-byte (`backend/app/main.py` /
// `EXPECTED_SCHEMA_REVISION`) so existing content tests stay valid. Callers on
// a non-self repo pass that repo's resolved schema-revision file/constant; a
// repo with NO schema-revision gate passes `schemaRevisionFile: ''`, which
// drops step 3 entirely so no Blueprint file is ever named. Plain string params
// (not the ResolvedMigrationLayout type) keep this prompts module free of a
// task-type-focus → specialized → prompt → task-type-focus import cycle.
export function buildMigrateTaskFocus(opts?: {
  schemaRevisionFile?: string;
  schemaRevisionConst?: string;
}): string {
  const schemaRevisionFile = opts?.schemaRevisionFile ?? 'backend/app/main.py';
  const schemaRevisionConst = opts?.schemaRevisionConst ?? 'EXPECTED_SCHEMA_REVISION';
  const schemaRevisionStep = schemaRevisionFile === ''
    ? ''
    : `3. **${schemaRevisionConst}** — if \`${schemaRevisionFile}\` declares this constant,
   bump it to match the new alembic head or the invariant test will fail.
`;
  return `
## Self-review focus: migrate tasks

Migrate tasks carry the highest regression_risk because schema or data changes
ripple into existing tests. Apply these additional checks before \`./done\`:

1. **Down-revision chain** — verify the new migration's \`down_revision\` matches
   the current alembic head (\`alembic heads\`). A broken chain silently skips
   the migration in CI.
2. **Affected tests** — scan for tests that exercise the changed models or tables.
   Name them explicitly; record them in the \`regression_risk\` issue field even
   when they still pass (e.g. \`"issue": "affected_tests: [test_foo, test_bar] — all still pass"\`).
${schemaRevisionStep}`;
}

export const MIGRATE_TASK_FOCUS = buildMigrateTaskFocus();

export const REFACTOR_TASK_FOCUS = `
## Self-review focus: refactor tasks

Refactor tasks must guarantee NO new behavior is introduced. The dominant miss
is accidental scope-drift or subtle behavioral changes that bypass tests.

1. **Scope-drift (mandatory)** — run BOTH \`git diff --stat HEAD\` AND
   \`git diff --name-only HEAD\`. Both triggers apply: the diff-size threshold
   (Trigger A) and the file-set check against the declared \`related_files\`
   (Trigger B, modulo the incidental allowlist — ${SCOPE_DRIFT_INCIDENTAL_ALLOWLIST_DESC}). If ANY externally-observable behavior changed (return
   values, side effects, error messages, API shape), record it under \`correctness\`
   with explicit before/after: \`"issue": "behavior changed: foo() returned X, now returns Y"\`.
2. **Caller inventory** — grep for every caller of renamed or restructured exports.
   Verify each call site is updated and behavior-preserving.
3. **Test delta** — a pure refactor should need no new tests; if you added tests,
   explain why in \`blind_spots\`.
`;

export const QA_TASK_FOCUS = `
## Self-review focus: qa tasks

QA tasks must produce meaningful coverage — the dominant miss is trivial tests
that don't exercise edge cases or new behaviors.

1. **Behavior enumeration** — list every new behavior or code path under test. In
   \`test-coverage\`, explicitly attest that each has a test: \`"issue": "behaviors
   covered: [X, Y, Z] — each has a dedicated test"\`.
2. **Edge cases** — for each behavior, name at least one non-happy-path scenario
   (empty input, error branch, boundary value) and confirm a test exercises it.
3. **Assertion strength** — tests that only check a function runs without error are
   insufficient. Verify assertions check actual return values or side effects.
`;

export const CREATE_TASK_FOCUS = `
## Self-review focus: create tasks

Create tasks most commonly miss test coverage for new behavior and drift outside
the declared scope. Apply these additional checks before \`./done\`:

1. **Test coverage** — for every new function, class, or behavior you shipped,
   confirm there is at least one test. In \`test-coverage\`, attest explicitly:
   \`"issue": "new behavior X has a test in test_foo.py"\` (or note the gap).
2. **Scope-drift** — run BOTH \`git diff --stat HEAD\` AND \`git diff --name-only HEAD\`.
   Trigger A: diff size against the declared scope threshold. Trigger B: every
   modified file must be in the declared \`related_files\` set OR in the incidental
   allowlist (${SCOPE_DRIFT_INCIDENTAL_ALLOWLIST_DESC}). Any
   modified file outside \`related_files\` and outside the allowlist is off-scope —
   record under \`scope-drift\` with \`passed: false\` and list the off-scope files
   (e.g. "modified files outside declared scope: backend/app/foo.py — declared related_files: [agent/src/baz.ts]").
3. ${CROSS_FILE_CONSISTENCY_FOCUS}
4. **Caller compatibility** — grep for callers of any function or export you
   added or modified; confirm their call sites still compile and behave correctly.
`;

export const FIX_TASK_FOCUS = `
## Self-review focus: fix tasks

Fix tasks most commonly introduce regression risk on the surface that was already
failing, and scope creep into unrelated files. Apply these additional checks:

0. **Reproduced-on-HEAD gate (do this FIRST, before editing)** — a bug-fix task is
   spawned from a finding that asserts a defect at a cited location, but that finding
   can be stale: the bug was already fixed by an intervening merge, the cited code was
   refactored away, or the report was simply wrong. Before changing code, confirm the
   reported defect still reproduces on current HEAD via at least one of: a failing test,
   an observable symptom, or the cited buggy pattern still present at the cited
   code/symbol. Classify the outcome into exactly one of the three canonical
   reproduce-on-HEAD verdicts and act on it:
   - **reproduced** — the defect still reproduces. Take the normal fix path below and
     record the reproduction evidence under \`correctness\`
     (e.g. \`"issue": "reproduced: cited pattern foo() still present at bar.py:42"\`).
     When you ship the fix, close with
     \`./done --reproduced "<what reproduced on HEAD and what you fixed>"\` so the
     terminal payload upgrades the durable reproduce verdict from the seeded
     baseline to \`reproduced\`.
   - **already_resolved / non-reproducing** — the cited code/symbol is absent, or a
     reproduction attempt you wrote down demonstrably no longer fails. Take the
     already-resolved off-ramp \`./done --already-done "<reason naming the cited location
     and what you checked>"\` INSTEAD of producing a speculative change; that path now
     emits the structured \`already_resolved\` reproduce verdict so the outcome is scored
     as a verified no-op rather than a false fix.
   - **indeterminate** — you genuinely cannot tell (ambiguous symptom, a still-present
     cited pattern you cannot exercise). Keep the not-reproducible bar HIGH: ambiguity
     does NOT earn the off-ramp — fall through to the normal fix path below and note the
     uncertainty under \`correctness\`. If you still cannot prove reproduction by the
     time you ship, close with
     \`./done --indeterminate "<why reproduce-on-HEAD remained ambiguous>"\` so the
     terminal payload preserves the neutral outcome explicitly.
1. **Regression surface** — identify the specific code path that was failing before
   your fix. Confirm the fix targets ONLY that path and does not silently change
   adjacent behavior. Record in \`regression_risk\` even when passing:
   \`"issue": "fixed path: foo() — adjacent callers bar(), baz() verified unchanged"\`.
2. **Scope-drift** — run BOTH \`git diff --stat HEAD\` AND \`git diff --name-only HEAD\`.
   Fixes should be surgical, so both triggers apply: diff-size threshold (Trigger A)
   AND the file-set check against \`related_files\` (Trigger B). Any modified file
   outside \`related_files\` (after the incidental allowlist — ${SCOPE_DRIFT_INCIDENTAL_ALLOWLIST_DESC}) is off-scope.
   Record unjustified additions under \`scope-drift\` with \`passed: false\` and list
   the off-scope file paths.
3. **Root-cause vs. symptom** — confirm the fix addresses the root cause, not just
   the observable symptom. If you only masked the symptom, record it under
   \`correctness\` with \`passed: false\`.
4. **Fresh verdict — do not reuse the original create task's SR** — your self-review
   must reflect THIS iteration's diff only. The original task's self-review (propagated
   as \`_self_review_outcome\` in context) is reference material, not a template to copy.
   Write to \`/tmp/self_review_<YOUR_task_id>.json\` using the CURRENT task ID from the
   ORCHESTRATOR env — never a prior task's ID.
5. ${CROSS_FILE_CONSISTENCY_FOCUS}
6. **Push-and-let-CI-verify when local test gate is unavailable** — if your fix is
   complete but the VM cannot run the full DB-backed or Docker-backed test suite
   (e.g. Postgres not running, Docker daemon absent, \`GATE-ENVIRONMENT\` note emitted
   by \`./check test\`), do NOT call \`./blocked\`. Instead:
   (a) Run the cheapest available verification for the stacks you touched: the
       project's linter, the TypeScript type checker (\`npx tsc --noEmit\`), and
       any standalone or non-DB test pass the project supports (e.g. the backend
       standalone smoke that skips Postgres entirely). See the \`./check\`
       description above for the commands specific to this project.
   (b) Run \`./pr open\` to push the completed branch.
   (c) Call \`./done\` and note the runtime limitation in the PR body, naming CI
       as the authoritative gate: "Local DB/Docker unavailable; standalone smoke
       passed; CI runs the full suite on this PR."
   A complete fix sitting unpushed because tests cannot run locally is a stranded
   delivery — push it and let CI verify. \`./blocked\` is only for cases where
   no code edit can fix the problem (e.g. a missing upstream dependency or a
   repo-wide gate failure on files you did not touch).
`;

export const DOCS_TASK_FOCUS = `
## Self-review focus: docs tasks

Docs tasks most commonly ship stale links or descriptions that diverge from
current code behavior. Apply these additional checks before \`./done\`:

1. **Link rot** — for every URL or cross-reference you added or kept, verify it
   resolves. Dead links in published docs are high-visibility failures.
2. **Code-comment vs. behavior** — for every code snippet or function description
   you wrote, re-read the actual implementation and confirm it matches. Divergence
   between docs and code is worse than no docs.
3. **Production-code untouched** — confirm \`git diff --name-only HEAD\` shows only
   documentation files (README, docs/, docstrings). Any production-code change
   that is not in \`related_files\` (and not in the incidental allowlist — ${SCOPE_DRIFT_INCIDENTAL_ALLOWLIST_DESC}) belongs in a separate task; record under \`scope-drift\` with
   \`passed: false\` and list the off-scope files.
`;

export const DEPS_TASK_FOCUS = `
## Self-review focus: deps tasks

Deps tasks most commonly leave lockfile/manifest drift or miss transitive breakage.
Apply these additional checks before \`./done\`:

1. **Lockfile/manifest parity** — confirm every file that declares a dependency
   (package.json, requirements.txt, pyproject.toml, Cargo.toml, go.mod) has a
   corresponding updated lockfile (package-lock.json, poetry.lock, Cargo.lock,
   go.sum). A manifest update without a lockfile update is a broken state.
2. **Transitive impact** — check whether bumped packages pulled in new transitive
   deps or dropped old ones. Run the build to confirm no implicit peer-dep errors.
3. **Major-version guard** — confirm no package crossed a major version boundary.
   Record any that did under \`correctness\` with \`passed: false\`.
`;

// Returns the task-type-specific self-review focus block to append after
// CODE_CHANGE_HELPER_GUIDANCE. Returns '' for task types without a custom block.
// For migrate tasks, an optional `opts` lets callers thread their project's
// resolved schema-revision file/constant through so a non-self repo sees its
// own paths (and a repo with no gate omits the step). Without opts the block
// falls back to Blueprint's defaults.
export function getTaskTypeFocusBlock(
  taskType: string,
  opts?: { schemaRevisionFile?: string; schemaRevisionConst?: string },
): string {
  switch (taskType) {
    case 'migrate': return opts ? buildMigrateTaskFocus(opts) : MIGRATE_TASK_FOCUS;
    case 'refactor': return REFACTOR_TASK_FOCUS;
    case 'qa': return QA_TASK_FOCUS;
    case 'create': return CREATE_TASK_FOCUS;
    case 'fix': return FIX_TASK_FOCUS;
    case 'docs': return DOCS_TASK_FOCUS;
    case 'deps': return DEPS_TASK_FOCUS;
    default: return '';
  }
}
