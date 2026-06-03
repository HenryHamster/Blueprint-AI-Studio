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

export function buildMigrationConflictGuidance(
  c: MigrationConflict,
  versionsDir: string = ALEMBIC_VERSIONS_DIR,
  schemaRevisionFile: string = 'backend/app/main.py',
  schemaRevisionConst: string = 'EXPECTED_SCHEMA_REVISION',
): string {
  if (!c.detected) return '';
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
