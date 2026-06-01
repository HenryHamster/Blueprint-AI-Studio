import { describe, it, expect } from 'vitest';
import { detectMigrationConflict, buildMigrationConflictGuidance } from './review-fix-migration.js';

describe('detectMigrationConflict', () => {
  it('returns detected=false when PR has no migration files', () => {
    const result = detectMigrationConflict(
      ['backend/app/main.py', 'agent/src/handlers/review-fix.ts'],
      ['backend/alembic/versions/0050_x.py'],
    );
    expect(result.detected).toBe(false);
    expect(result.prMigrations).toEqual([]);
  });

  it('returns detected=false when PR migration is the newest (no conflicting base migrations)', () => {
    const result = detectMigrationConflict(
      ['backend/alembic/versions/0050_add_foo.py'],
      ['backend/alembic/versions/0049_prior.py', 'backend/alembic/versions/0050_add_foo.py'],
    );
    expect(result.detected).toBe(false);
    expect(result.prMigrations).toEqual(['backend/alembic/versions/0050_add_foo.py']);
  });

  it('detects silent conflict when base has a newer migration with equal-or-higher revision', () => {
    const result = detectMigrationConflict(
      ['backend/alembic/versions/0050_add_foo.py'],
      [
        'backend/alembic/versions/0049_prior.py',
        'backend/alembic/versions/0050_merged_first.py',
        'backend/alembic/versions/0051_also_merged.py',
      ],
    );
    expect(result.detected).toBe(true);
    expect(result.baseNewMigrations).toEqual([
      'backend/alembic/versions/0050_merged_first.py',
      'backend/alembic/versions/0051_also_merged.py',
    ]);
  });

  it('ignores non-migration files and __init__.py', () => {
    const result = detectMigrationConflict(
      ['backend/alembic/versions/0050_add_foo.py', 'backend/alembic/versions/__init__.py'],
      ['backend/alembic/versions/__init__.py', 'backend/alembic/versions/0050_other.py'],
    );
    expect(result.detected).toBe(true);
    expect(result.prMigrations).toEqual(['backend/alembic/versions/0050_add_foo.py']);
  });

  it('detects a conflict under a non-Blueprint versions directory', () => {
    const versionsDir = 'db/migrations/versions/';
    const result = detectMigrationConflict(
      ['db/migrations/versions/0050_add_foo.py'],
      [
        'db/migrations/versions/0049_prior.py',
        'db/migrations/versions/0050_merged_first.py',
        'db/migrations/versions/0051_also_merged.py',
      ],
      versionsDir,
    );
    expect(result.detected).toBe(true);
    expect(result.prMigrations).toEqual(['db/migrations/versions/0050_add_foo.py']);
    expect(result.baseNewMigrations).toEqual([
      'db/migrations/versions/0050_merged_first.py',
      'db/migrations/versions/0051_also_merged.py',
    ]);
  });

  it('ignores Blueprint-dir files when a custom versions directory is supplied', () => {
    const result = detectMigrationConflict(
      ['db/migrations/versions/0050_add_foo.py', 'backend/alembic/versions/0050_unrelated.py'],
      ['backend/alembic/versions/0051_merged.py'],
      'db/migrations/versions/',
    );
    // The PR migration is detected under the custom dir, but the base file in
    // the Blueprint dir must NOT count as a conflicting base migration.
    expect(result.prMigrations).toEqual(['db/migrations/versions/0050_add_foo.py']);
    expect(result.detected).toBe(false);
    expect(result.baseNewMigrations).toEqual([]);
  });
});

describe('buildMigrationConflictGuidance', () => {
  it('returns empty string when no conflict detected', () => {
    expect(buildMigrationConflictGuidance({ detected: false, prMigrations: [], baseNewMigrations: [] })).toBe('');
  });

  it('mentions renumbering, down_revision chaining, and EXPECTED_SCHEMA_REVISION bump', () => {
    const out = buildMigrationConflictGuidance({
      detected: true,
      prMigrations: ['backend/alembic/versions/0050_add_foo.py'],
      baseNewMigrations: ['backend/alembic/versions/0051_merged.py'],
    });
    expect(out).toMatch(/Migration revision conflict/);
    expect(out).toMatch(/down_revision/);
    expect(out).toMatch(/EXPECTED_SCHEMA_REVISION/);
    expect(out).toMatch(/0051_merged\.py/);
  });

  it('includes computed next revision number, renamed file, and EXPECTED_SCHEMA_REVISION value', () => {
    const out = buildMigrationConflictGuidance({
      detected: true,
      prMigrations: ['backend/alembic/versions/0050_add_foo.py'],
      baseNewMigrations: ['backend/alembic/versions/0051_merged.py'],
    });
    expect(out).toMatch(/revision="0051"/);
    expect(out).toMatch(/backend\/alembic\/versions\/0052_add_foo\.py/);
    expect(out).toMatch(/revision = "0052"/);
    expect(out).toMatch(/down_revision = "0051"/);
    expect(out).toMatch(/EXPECTED_SCHEMA_REVISION = "0052"/);
  });

  it('emits the renamed migration path under a custom versions directory', () => {
    const versionsDir = 'db/migrations/versions/';
    const out = buildMigrationConflictGuidance({
      detected: true,
      prMigrations: ['db/migrations/versions/0050_add_foo.py'],
      baseNewMigrations: ['db/migrations/versions/0051_merged.py'],
    }, versionsDir);
    expect(out).toMatch(/db\/migrations\/versions\/0052_add_foo\.py/);
    expect(out).toMatch(/do NOT edit existing files in db\/migrations\/versions\//);
    expect(out).toMatch(/down_revision = "0051"/);
    // The Blueprint default dir must not leak into custom-dir guidance.
    expect(out).not.toMatch(/backend\/alembic\/versions\/0052/);
  });

  it('regression: default (no versionsDir) guidance is byte-for-byte unchanged', () => {
    const conflict = {
      detected: true,
      prMigrations: ['backend/alembic/versions/0050_add_foo.py'],
      baseNewMigrations: ['backend/alembic/versions/0051_merged.py'],
    };
    expect(buildMigrationConflictGuidance(conflict)).toBe(
      buildMigrationConflictGuidance(conflict, 'backend/alembic/versions/'),
    );
  });
});
