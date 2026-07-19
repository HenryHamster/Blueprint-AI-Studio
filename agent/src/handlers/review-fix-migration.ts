// Alembic migration-conflict detection helpers extracted from review-fix.ts.
// These are pure functions with no I/O dependencies.

// Detect migration-file conflicts that go beyond text-level conflict markers:
// when the PR introduces a new alembic migration and the base branch has
// gained additional migrations with equal-or-higher revision numbers since the
// PR branched, the PR's revision id and down_revision are stale and must be
// renumbered even if git produced no conflict markers. Pure-function form so
// it can be unit-tested without touching the filesystem.
const ALEMBIC_VERSIONS_DIR = 'backend/alembic/versions/';
export type MigrationConflict = {
  detected: boolean;
  prMigrations: string[];
  baseNewMigrations: string[];
};
export function detectMigrationConflict(
  prFiles: string[],
  baseBranchFiles: string[],
  versionsDir: string = ALEMBIC_VERSIONS_DIR,
): MigrationConflict {
  const isMigration = (f: string) =>
    f.startsWith(versionsDir) && f.endsWith('.py') && !f.endsWith('__init__.py');
  const revNum = (f: string): number => {
    const base = f.slice(versionsDir.length);
    const m = base.match(/^(\d+)_/);
    return m ? parseInt(m[1], 10) : -1;
  };
  const prMigrations = prFiles.filter(isMigration);
  if (prMigrations.length === 0) return { detected: false, prMigrations: [], baseNewMigrations: [] };
  const prRevs = prMigrations.map(revNum).filter(n => n >= 0);
  if (prRevs.length === 0) return { detected: false, prMigrations, baseNewMigrations: [] };
  const minPrRev = Math.min(...prRevs);
  const prSet = new Set(prMigrations);
  const baseNewMigrations = baseBranchFiles
    .filter(isMigration)
    .filter(f => !prSet.has(f) && revNum(f) >= minPrRev);
  return { detected: baseNewMigrations.length > 0, prMigrations, baseNewMigrations };
}

// A provisional Alembic revision reserved up-front by the orchestrator so
// migration-producing tasks can be authored in parallel and linearized at merge
// time, rather than serializing the whole pipeline behind one running migration.
type ReservedMigrationRevision = {
  provisionalRevision: string;
  provisionalDownRevision: string;
};

/**
 * Extract a reserved provisional Alembic revision from the agent claim payload,
 * if the orchestrator allocated one for this migration-producing task. Returns
 * null when the claim carries no reservation (e.g. tasks queued before the
 * reservation rollout) so callers fall back to deriving the next revision
 * locally — preserving backward compatibility.
 */
export function extractReservedMigrationRevision(
  task: Record<string, unknown> | undefined | null,
): ReservedMigrationRevision | null {
  const raw = task?.migration_reservation as Record<string, unknown> | undefined;
  if (!raw || typeof raw !== 'object') return null;
  const rev = typeof raw.provisional_revision === 'string' ? raw.provisional_revision.trim() : '';
  const down = typeof raw.provisional_down_revision === 'string' ? raw.provisional_down_revision.trim() : '';
  if (!rev) return null;
  return { provisionalRevision: rev, provisionalDownRevision: down };
}

/**
 * Prompt guidance instructing the agent to use a reserved revision id VERBATIM
 * when authoring a new migration, instead of computing the next number from the
 * local chain (max+1) or trusting whatever id `alembic revision --autogenerate`
 * happens to pick. Emitted only when a reservation rode the claim. Used by both
 * the dedicated migrate handler and create tasks that touch models.py.
 */
export function buildReservedRevisionGuidance(
  res: ReservedMigrationRevision,
  versionsDir: string = ALEMBIC_VERSIONS_DIR,
  schemaRevisionFile: string = 'backend/app/main.py',
  schemaRevisionConst: string = 'EXPECTED_SCHEMA_REVISION',
): string {
  const dir = versionsDir.endsWith('/') ? versionsDir : `${versionsDir}/`;
  const advisoryLine = res.provisionalDownRevision
    ? ` The reservation's advisory parent is \`${res.provisionalDownRevision}\`; use it only if the actual head cannot be determined.\n`
    : '\n';
  const schemaStep = schemaRevisionFile !== ''
    ? `- Set \`${schemaRevisionConst} = "${res.provisionalRevision}"\` in ${schemaRevisionFile} (it MUST match the reserved revision).\n`
    : '';
  return (
    `## Reserved migration revision\n` +
    `A revision id has been RESERVED for this task so migrations can be authored ` +
    `in parallel. Use it VERBATIM — do NOT derive the next number from the ` +
    `existing chain (no max+1), and do NOT rely on \`alembic revision ` +
    `--autogenerate\` to pick the id:\n` +
    `- Name the migration file \`${dir}${res.provisionalRevision}_<short_description>.py\`.\n` +
    `- Set \`revision = "${res.provisionalRevision}"\` inside it.\n` +
    `- Immediately before authoring, derive the actual pre-new-migration chain head from this freshly cloned/rebased checkout (for example, run \`alembic heads\` from the Alembic directory and inspect the migration it reports). Set \`down_revision\` to that actual head — never copy an advisory parent when it disagrees.${advisoryLine}` +
    `- Before writing, fail safely rather than opening a broken PR if the reserved id already exists in the checkout's pre-existing migration chain, or if both ids are numeric and \`${res.provisionalRevision}\` is not greater than the actual head. Report the conflict; do not renumber the reservation or edit an applied migration.\n` +
    schemaStep +
    `If autogenerate emitted a different id, rename the file and edit the ` +
    `\`revision\` string to "${res.provisionalRevision}". The reservation fixes only the new revision id; its parent must be derived from the checkout immediately before authoring.\n\n`
  );
}

export function buildMigrationConflictGuidance(
  c: MigrationConflict,
  versionsDir: string = ALEMBIC_VERSIONS_DIR,
  schemaRevisionFile: string = 'backend/app/main.py',
  schemaRevisionConst: string = 'EXPECTED_SCHEMA_REVISION',
  reserved: ReservedMigrationRevision | null = null,
): string {
  if (!c.detected && (!reserved || c.prMigrations.length === 0)) return '';
  // When a revision id was reserved up-front, consume it verbatim rather than
  // recomputing max+1 against the (now-advanced) base chain. Merge-time
  // finalization linearizes the reserved id against production, so the agent
  // does not need to re-derive a sequential number here.
  if (reserved) {
    const dir = versionsDir.endsWith('/') ? versionsDir : `${versionsDir}/`;
    const prFile = c.prMigrations[0] || '';
    const prBase = prFile.slice(dir.length);
    const prDescMatch = prBase.match(/^\d+_(.+)\.py$/);
    const prDesc = prDescMatch ? prDescMatch[1] : '<description>';
    const schemaStep = schemaRevisionFile !== ''
      ? `- Set \`${schemaRevisionConst} = "${reserved.provisionalRevision}"\` in ${schemaRevisionFile}.\n`
      : '';
    return (
      `## Migration revision repair (reserved revision)\n` +
      `This PR adds alembic migration file(s):\n${c.prMigrations.map(f => `  - ${f}`).join('\n')}\n` +
      (c.baseNewMigrations.length
        ? `The base branch gained migration file(s) since this PR branched:\n${c.baseNewMigrations.map(f => `  - ${f}`).join('\n')}\n\n`
        : `This reserved migration must still be repaired against the current base ref, even though filename conflict detection found no newer numeric file.\n\n`) +
      `A revision id was RESERVED for this task — use it VERBATIM, do NOT derive max+1:\n` +
      `1. Rename this PR's migration to ${dir}${reserved.provisionalRevision}_${prDesc}.py and set \`revision = "${reserved.provisionalRevision}"\` inside it.\n` +
      `2. Derive the actual pre-PR chain head from the current base ref, not this PR's already-branched migration (use a base-ref worktree or inspect the base-ref migration tree). Set \`down_revision\` to that base head. The advisory parent \`${reserved.provisionalDownRevision}\` is fallback-only and MUST NOT win when it disagrees with that head.\n` +
      `3. Fail safely if the reserved id already exists on the base ref, or if both ids are numeric and \`${reserved.provisionalRevision}\` is not greater than the base head. Do not renumber the reservation or edit an applied migration.\n` +
      `- Do NOT edit existing migration files in ${dir}.\n` +
      schemaStep +
      `The reserved id is preserved verbatim; only its stale parent is repaired against the current base ref.\n\n`
    );
  }
  const latestBase = [...c.baseNewMigrations].sort().pop() || '';
  const latestBaseBase = latestBase.slice(versionsDir.length);
  const latestRevMatch = latestBaseBase.match(/^(\d+)_/);
  const latestRev = latestRevMatch ? latestRevMatch[1] : '';
  const latestRevNum = latestRev ? parseInt(latestRev, 10) : NaN;
  const padWidth = Math.max(latestRev.length, 4);
  const nextRev = Number.isFinite(latestRevNum)
    ? String(latestRevNum + 1).padStart(padWidth, '0')
    : '';
  const prFile = c.prMigrations[0] || '';
  const prBase = prFile.slice(versionsDir.length);
  const prDescMatch = prBase.match(/^\d+_(.+)\.py$/);
  const prDesc = prDescMatch ? prDescMatch[1] : '<description>';
  const renamedFile = nextRev
    ? `${versionsDir}${nextRev}_${prDesc}.py`
    : '';
  const concrete = nextRev && latestRev;
  // Schema-revision step (step 4) is emitted only when the project declares a
  // schema-revision gate. A repo with none passes schemaRevisionFile='' and the
  // step — and its number — drop out, so the final test step renumbers 5→4 and
  // no Blueprint file is ever named.
  const hasSchemaRevisionGate = schemaRevisionFile !== '';
  const schemaStep = hasSchemaRevisionGate
    ? (concrete
      ? `4. Set \`${schemaRevisionConst} = "${nextRev}"\` in ${schemaRevisionFile} to match the renumbered revision.\n`
      : `4. Bump \`${schemaRevisionConst}\` in ${schemaRevisionFile} to match the renumbered revision id.\n`)
    : '';
  const testStepNum = hasSchemaRevisionGate ? 5 : 4;
  return (
    `## Migration revision conflict\n` +
    `This PR adds alembic migration file(s):\n${c.prMigrations.map(f => `  - ${f}`).join('\n')}\n` +
    `But the base branch has gained migration file(s) with equal-or-higher revision numbers that this PR does not yet incorporate:\n` +
    `${c.baseNewMigrations.map(f => `  - ${f}`).join('\n')}\n\n` +
    `Resolution steps (migrations are immutable once applied — do NOT edit existing files in ${versionsDir}):\n` +
    (concrete
      ? `1. Latest base-branch migration is ${latestBase} (revision="${latestRev}"). Open it to confirm its \`revision\` string matches "${latestRev}".\n` +
        `2. Rename this PR's migration to ${renamedFile} and set \`revision = "${nextRev}"\` inside it.\n` +
        `3. Set this PR's migration \`down_revision = "${latestRev}"\` so the chain is linear.\n`
      : `1. Read the latest migration on the base branch (${latestBase}) to obtain its revision id.\n` +
        `2. Rename this PR's migration file to the next sequential numeric prefix AFTER the base branch's latest, and update its \`revision\` string to a fresh unique id.\n` +
        `3. Set this PR's migration \`down_revision\` to the base branch's latest revision id so the chain is linear.\n`) +
    schemaStep +
    `${testStepNum}. Re-run the backend tests (using the project's configured backend test command) to confirm the chain applies cleanly.\n\n`
  );
}
