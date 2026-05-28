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
});
