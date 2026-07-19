import { describe, it, expect } from 'vitest';
import {
  detectMigrationConflict,
  buildMigrationConflictGuidance,
  extractReservedMigrationRevision,
  buildReservedRevisionGuidance,
} from './review-fix-migration.js';

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

  it('uses a custom schema-revision file/constant and does not leak Blueprint paths', () => {
    const out = buildMigrationConflictGuidance(
      {
        detected: true,
        prMigrations: ['svc/db/versions/0050_add_foo.py'],
        baseNewMigrations: ['svc/db/versions/0051_merged.py'],
      },
      'svc/db/versions/',
      'svc/app/config.py',
      'SCHEMA_REV',
    );
    expect(out).toMatch(/SCHEMA_REV = "0052"/);
    expect(out).toMatch(/svc\/app\/config\.py/);
    expect(out).not.toMatch(/EXPECTED_SCHEMA_REVISION/);
    expect(out).not.toMatch(/backend\/app\/main\.py/);
  });

  it('omits the schema-revision step and renumbers the test step to 4 when no gate is declared', () => {
    const out = buildMigrationConflictGuidance(
      {
        detected: true,
        prMigrations: ['svc/db/versions/0050_add_foo.py'],
        baseNewMigrations: ['svc/db/versions/0051_merged.py'],
      },
      'svc/db/versions/',
      '',
    );
    // No schema-revision step and no Blueprint file named.
    expect(out).not.toMatch(/EXPECTED_SCHEMA_REVISION/);
    expect(out).not.toMatch(/backend\/app\/main\.py/);
    expect(out).not.toMatch(/Bump/);
    // The final test step renumbers from 5 to 4.
    expect(out).toMatch(/4\. Re-run the backend tests/);
    expect(out).not.toMatch(/5\. Re-run the backend tests/);
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

  it('uses checkout head when reservation anchor is stale', () => {
    const out = buildMigrationConflictGuidance(
      {
        detected: true,
        prMigrations: ['backend/alembic/versions/0050_add_foo.py'],
        baseNewMigrations: ['backend/alembic/versions/0051_merged.py'],
      },
      'backend/alembic/versions/',
      'backend/app/main.py',
      'EXPECTED_SCHEMA_REVISION',
      { provisionalRevision: '0607', provisionalDownRevision: '0530' },
    );
    expect(out).toMatch(/reserved revision/i);
    // Reserved id is used verbatim — NOT the max+1 ("0052") of the base chain.
    expect(out).toMatch(/0607_add_foo\.py/);
    expect(out).toMatch(/revision = "0607"/);
    expect(out).toMatch(/current base ref/i);
    expect(out).toMatch(/down_revision.*base head/i);
    expect(out).toMatch(/advisory parent `0530`.*MUST NOT win/i);
    expect(out).toMatch(/EXPECTED_SCHEMA_REVISION = "0607"/);
    expect(out).toMatch(/already exists on the base ref/i);
    expect(out).toMatch(/not greater than the base head/i);
    expect(out).not.toMatch(/down_revision = "0530"/);
    expect(out).not.toMatch(/0052/);
  });
});

describe('extractReservedMigrationRevision', () => {
  it('returns null when no reservation rides the claim', () => {
    expect(extractReservedMigrationRevision({})).toBeNull();
    expect(extractReservedMigrationRevision(undefined)).toBeNull();
    expect(extractReservedMigrationRevision({ migration_reservation: null })).toBeNull();
  });

  it('parses provisional_revision and provisional_down_revision from the claim', () => {
    const res = extractReservedMigrationRevision({
      migration_reservation: {
        provisional_revision: '0153',
        provisional_down_revision: '0151',
      },
    });
    expect(res).toEqual({ provisionalRevision: '0153', provisionalDownRevision: '0151' });
  });

  it('returns null when the revision id is blank', () => {
    expect(
      extractReservedMigrationRevision({
        migration_reservation: { provisional_revision: '   ', provisional_down_revision: '0151' },
      }),
    ).toBeNull();
  });
});

describe('buildReservedRevisionGuidance', () => {
  it('uses the freshly cloned checkout head over a stale advisory parent', () => {
    const out = buildReservedRevisionGuidance({
      provisionalRevision: '0607',
      provisionalDownRevision: '0530',
    });
    expect(out).toMatch(/Reserved migration revision/);
    expect(out).toMatch(/backend\/alembic\/versions\/0607_<short_description>\.py/);
    expect(out).toMatch(/revision = "0607"/);
    expect(out).toMatch(/actual pre-new-migration chain head/i);
    expect(out).toMatch(/freshly cloned\/rebased checkout/i);
    expect(out).toMatch(/Set `down_revision` to that actual head/i);
    expect(out).toMatch(/advisory parent is `0530`; use it only if the actual head cannot be determined/i);
    expect(out).toMatch(/EXPECTED_SCHEMA_REVISION = "0607"/);
    expect(out).toMatch(/already exists in the checkout's pre-existing migration chain/i);
    expect(out).toMatch(/not greater than the actual head/i);
    expect(out).not.toMatch(/down_revision = "0530"/);
    expect(out).toMatch(/no max\+1/);
  });

  it('honors a custom versions dir and schema-revision file/const', () => {
    const out = buildReservedRevisionGuidance(
      { provisionalRevision: '0153', provisionalDownRevision: '0151' },
      'svc/db/versions/',
      'svc/app/config.py',
      'SCHEMA_REV',
    );
    expect(out).toMatch(/svc\/db\/versions\/0153_<short_description>\.py/);
    expect(out).toMatch(/SCHEMA_REV = "0153"/);
    expect(out).not.toMatch(/EXPECTED_SCHEMA_REVISION/);
    expect(out).not.toMatch(/backend\/app\/main\.py/);
  });

  it('omits the schema-revision step when no gate is declared', () => {
    const out = buildReservedRevisionGuidance(
      { provisionalRevision: '0153', provisionalDownRevision: '0151' },
      'svc/db/versions/',
      '',
    );
    expect(out).not.toMatch(/SCHEMA_REV/);
    expect(out).not.toMatch(/EXPECTED_SCHEMA_REVISION/);
  });
});
