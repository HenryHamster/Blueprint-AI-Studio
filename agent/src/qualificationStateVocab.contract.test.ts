import { describe, it, expect } from 'vitest';
import { existsSync, readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { QUALIFICATION_STATE_VOCAB } from './handlers/research-plan.js';

const FIXTURE_RELATIVE_PATH = 'backend/testing/__fixtures__/qualification_state_vocab.json';

function qualificationStateVocabFixturePath(): string {
  const candidates = [
    import.meta.url.startsWith('file:')
      ? resolve(dirname(fileURLToPath(import.meta.url)), '..', '..', '..', FIXTURE_RELATIVE_PATH)
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

describe('qualification_state_vocab fixture contract (agent)', () => {
  it('fixture has schema_version 1 and a non-empty states array', () => {
    expect(fixture.schema_version).toBe(1);
    expect(Array.isArray(fixture.states)).toBe(true);
    expect(fixture.states.length).toBeGreaterThan(0);
  });

  it('QUALIFICATION_STATE_VOCAB exactly matches the fixture states', () => {
    const vocabStates = [...QUALIFICATION_STATE_VOCAB].sort();
    const fixtureStates = [...fixture.states].sort();
    expect(vocabStates).toEqual(fixtureStates);
  });

  it('all fixture states are present in QUALIFICATION_STATE_VOCAB', () => {
    for (const state of fixture.states) {
      expect(
        QUALIFICATION_STATE_VOCAB.has(state),
        `fixture state '${state}' is missing from agent QUALIFICATION_STATE_VOCAB`,
      ).toBe(true);
    }
  });

  it('AUDIT_IN_PROGRESS_STATES qualification subset is within QUALIFICATION_STATE_VOCAB', () => {
    // 'pending' is the raw backend stamp (not a derived state), so it is NOT
    // in QUALIFICATION_STATE_VOCAB. Only 'qualifying' is the derived state that
    // must be in the vocab.
    const qualificationSubset = ['qualifying'];
    for (const state of qualificationSubset) {
      expect(
        QUALIFICATION_STATE_VOCAB.has(state),
        `AUDIT_IN_PROGRESS_STATES derived state '${state}' is not in QUALIFICATION_STATE_VOCAB`,
      ).toBe(true);
    }
  });
});
