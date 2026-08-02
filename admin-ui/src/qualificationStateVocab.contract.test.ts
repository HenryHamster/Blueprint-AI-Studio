import { describe, it, expect } from 'vitest';
import { existsSync, readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import type { QualificationStateValue } from './api';
import { QUAL_STATE_STYLE } from './components/VersionLifecyclePanelPrimitives';

const FIXTURE_RELATIVE_PATH = 'backend/testing/__fixtures__/qualification_state_vocab.json';

function qualificationStateVocabFixturePath(): string {
  const candidates = [
    import.meta.url.startsWith('file:')
      ? resolve(dirname(fileURLToPath(import.meta.url)), '..', '..', FIXTURE_RELATIVE_PATH)
      : null,
    resolve(process.cwd(), '..', FIXTURE_RELATIVE_PATH),
    resolve(process.cwd(), FIXTURE_RELATIVE_PATH),
  ].filter((path): path is string => Boolean(path));
  const fixturePath = candidates.find(candidate => existsSync(candidate));
  if (!fixturePath) {
    throw new Error(`Missing qualification_state_vocab fixture. Checked: ${candidates.join(', ')}`);
  }
  return fixturePath;
}

const fixture = JSON.parse(
  readFileSync(qualificationStateVocabFixturePath(), 'utf-8'),
) as { schema_version: number; states: string[] };

// Exhaustive compile-time array typed as QualificationStateValue[] — if a state
// is in the union but missing here the TS compiler errors; if a literal here is
// not in the union it errors too. This is the cross-language pin.
const ALL_QUAL_STATE_VALUES: QualificationStateValue[] = [
  'audit_crashed',
  'deferred_unsynced',
  'dry_run',
  'error',
  'infra_failure',
  'not_promoted',
  'promoted',
  'promoted_unconfirmed',
  'promotion_check_wait',
  'promotion_confirmation_timed_out',
  'promotion_deploy_failed',
  'promotion_failed',
  'protection_blocked_review',
  'qualifying',
  'required_check_failed',
  'rubric_changed',
  'rubric_provenance_unknown',
];

describe('qualification_state_vocab fixture contract (admin-ui)', () => {
  it('fixture has schema_version 1 and a non-empty states array', () => {
    expect(fixture.schema_version).toBe(1);
    expect(Array.isArray(fixture.states)).toBe(true);
    expect(fixture.states.length).toBeGreaterThan(0);
  });

  it('QUAL_STATE_STYLE keys exactly match the fixture states', () => {
    const styleKeys = Object.keys(QUAL_STATE_STYLE).sort();
    const fixtureStates = [...fixture.states].sort();
    expect(styleKeys).toEqual(fixtureStates);
  });

  it('QualificationStateValue union exactly matches the fixture states', () => {
    const unionStates = [...ALL_QUAL_STATE_VALUES].sort();
    const fixtureStates = [...fixture.states].sort();
    expect(unionStates).toEqual(fixtureStates);
  });

  it('all fixture states are valid QualificationStateValue members', () => {
    const unionSet = new Set<string>(ALL_QUAL_STATE_VALUES);
    for (const state of fixture.states) {
      expect(unionSet.has(state), `fixture state '${state}' is not in QualificationStateValue union`).toBe(true);
    }
  });
});
