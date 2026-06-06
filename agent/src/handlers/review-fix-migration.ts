// Alembic migration-conflict detection helpers extracted from review-fix.ts.
// These are pure functions with no I/O dependencies.

// Detect migration-file conflicts that go beyond text-level conflict markers:
// when the PR introduces a new alembic migration and the base branch has
// gained additional migrations with equal-or-higher revision numbers since the
// PR branched, the PR's revision id and down_revision are stale and must be
// renumbered even if git produced no conflict markers. Pure-function form so
// it can be unit-tested without touching the filesystem.
export const ALEMBIC_VERSIONS_DIR = 'backend/alembic/versions/';
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
export type ReservedMigrationRevision = {
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
  const downLine = res.provisionalDownRevision
    ? `- Set \`down_revision = "${res.provisionalDownRevision}"\` so the chain stays linear.\n`
    : `- Set \`down_revision\` to the current chain head so the chain stays linear.\n`;
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
    downLine +
    schemaStep +
    `If autogenerate emitted a different id, rename the file and edit the ` +
    `\`revision\` string to "${res.provisionalRevision}". The reserved id is ` +
    `linearized against production at merge time.\n\n`
  );
}

export function buildMigrationConflictGuidance(
  c: MigrationConflict,
  versionsDir: string = ALEMBIC_VERSIONS_DIR,
  schemaRevisionFile: string = 'backend/app/main.py',
  schemaRevisionConst: string = 'EXPECTED_SCHEMA_REVISION',
  reserved: ReservedMigrationRevision | null = null,
): string {
  if (!c.detected) return '';
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
      ? `4. Set \`${schemaRevisionConst} = "${reserved.provisionalRevision}"\` in ${schemaRevisionFile}.\n`
      : '';
    return (
      `## Migration revision conflict (reserved revision)\n` +
      `This PR adds alembic migration file(s):\n${c.prMigrations.map(f => `  - ${f}`).join('\n')}\n` +
      `The base branch gained migration file(s) since this PR branched:\n` +
      `${c.baseNewMigrations.map(f => `  - ${f}`).join('\n')}\n\n` +
      `A revision id was RESERVED for this task — use it VERBATIM, do NOT derive max+1:\n` +
      `1. Rename this PR's migration to ${dir}${reserved.provisionalRevision}_${prDesc}.py and set \`revision = "${reserved.provisionalRevision}"\` inside it.\n` +
      `2. Set \`down_revision = "${reserved.provisionalDownRevision}"\` so the chain is linear.\n` +
      `3. Do NOT edit existing migration files in ${dir}.\n` +
      schemaStep +
      `The reserved id is linearized against production at merge time.\n\n`
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
    `${testStepNum}. Re-run the backend tests (\`bash scripts/test-backend.sh\`) to confirm the chain applies cleanly.\n\n`
  );
}
