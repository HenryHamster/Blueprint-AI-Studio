import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import {
  AGENT_TASK_TYPE_IDS,
  ALL_AGENT_TASK_TYPES,
  __resetTaskCapabilityContractForTesting,
  getReadOnlyTaskTypes,
  getWriteTaskTypes,
  isReadOnlyTaskType,
  loadTaskCapabilityContract,
} from './taskCapabilities.js';
import { buildFixtureCapabilityContract } from './testSetup.js';

describe('agent task-type coverage tuple', () => {
  it('has no duplicate task ids', () => {
    expect(new Set(ALL_AGENT_TASK_TYPES).size).toBe(ALL_AGENT_TASK_TYPES.length);
    expect(new Set(AGENT_TASK_TYPE_IDS).size).toBe(AGENT_TASK_TYPE_IDS.length);
  });

  it('covers the 30 dispatchable task types', () => {
    expect(ALL_AGENT_TASK_TYPES.length).toBe(30);
  });
});

describe('task-capability contract loading', () => {
  afterEach(() => {
    // Restore the canonical fixture seeded by testSetup so later test files /
    // suites that rely on a loaded contract are unaffected.
    __resetTaskCapabilityContractForTesting();
    loadTaskCapabilityContract(buildFixtureCapabilityContract());
  });

  it('derives the read-only/write partition from the contract', () => {
    __resetTaskCapabilityContractForTesting();
    loadTaskCapabilityContract(buildFixtureCapabilityContract());

    const readOnly = new Set(getReadOnlyTaskTypes());
    const write = new Set(getWriteTaskTypes());

    // Every dispatchable type lands in exactly one partition.
    for (const taskType of ALL_AGENT_TASK_TYPES) {
      expect(readOnly.has(taskType) || write.has(taskType)).toBe(true);
      expect(readOnly.has(taskType) && write.has(taskType)).toBe(false);
    }
    // Spot-check both modes against the published contract.
    expect(readOnly.has('self_audit')).toBe(true);
    expect(write.has('create')).toBe(true);
  });

  it('classifies read-only and write types after load', () => {
    __resetTaskCapabilityContractForTesting();
    loadTaskCapabilityContract(buildFixtureCapabilityContract());

    expect(isReadOnlyTaskType('self_audit')).toBe(true);
    expect(isReadOnlyTaskType('review')).toBe(true);
    expect(isReadOnlyTaskType('panel_review')).toBe(true);
    expect(isReadOnlyTaskType('create')).toBe(false);
    expect(isReadOnlyTaskType('fix')).toBe(false);
    // Unknown types are not read-only (preserved fallback; dispatch rejects them first).
    expect(isReadOnlyTaskType('future_capability')).toBe(false);
  });

  it('throws when classifying before the contract is loaded', () => {
    __resetTaskCapabilityContractForTesting();
    expect(() => isReadOnlyTaskType('create')).toThrow(/not loaded/);
    expect(() => getReadOnlyTaskTypes()).toThrow(/not loaded/);
    expect(() => getWriteTaskTypes()).toThrow(/not loaded/);
  });

  it('throws when a known task type is missing from the contract', () => {
    const incomplete = buildFixtureCapabilityContract();
    delete incomplete.migrate;
    expect(() => { loadTaskCapabilityContract(incomplete); }).toThrow(/missing\/incomplete: migrate/);
  });

  it('throws when a known task type carries an invalid agent_mode', () => {
    const invalid = buildFixtureCapabilityContract();
    invalid.review = { type: 'review', agent_mode: 'sideways' };
    expect(() => { loadTaskCapabilityContract(invalid); }).toThrow(/invalid agent_mode: review=sideways/);
  });

  it('throws when the contract is not an object', () => {
    expect(() => { loadTaskCapabilityContract(null); })
      .toThrow(/missing or not an object/);
  });

  it('does not partially apply an invalid contract', () => {
    __resetTaskCapabilityContractForTesting();
    const incomplete = buildFixtureCapabilityContract();
    delete incomplete.docs;
    expect(() => { loadTaskCapabilityContract(incomplete); }).toThrow();
    // Rejected load leaves the cache empty — classification still throws.
    expect(() => isReadOnlyTaskType('create')).toThrow(/not loaded/);
  });
});

describe('partition cache lifecycle', () => {
  beforeEach(() => {
    __resetTaskCapabilityContractForTesting();
    loadTaskCapabilityContract(buildFixtureCapabilityContract());
  });

  it('reflects a reloaded contract', () => {
    // Flip create to read_only in a fresh contract and reload.
    const flipped = buildFixtureCapabilityContract();
    flipped.create = { type: 'create', agent_mode: 'read_only' };
    __resetTaskCapabilityContractForTesting();
    loadTaskCapabilityContract(flipped);
    expect(isReadOnlyTaskType('create')).toBe(true);

    // Restore canonical fixture for subsequent tests.
    __resetTaskCapabilityContractForTesting();
    loadTaskCapabilityContract(buildFixtureCapabilityContract());
    expect(isReadOnlyTaskType('create')).toBe(false);
  });
});
