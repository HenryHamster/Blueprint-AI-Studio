import { describe, expect, it } from 'vitest';
import {
  FILTER_TASK_TYPE_IDS,
  matchesTaskTypeFilter,
  PROVIDER_ROUTABLE_TASK_TYPE_IDS,
  TASK_CAPABILITIES,
  USER_LAUNCHABLE_TASK_TYPE_IDS,
  getTaskTypeLabel,
  resolveTaskCapabilities,
} from './taskCapabilities';
import type { TaskCapabilityContract } from './types/settings';

describe('task capability registry', () => {
  it('has no duplicate task ids', () => {
    const ids = TASK_CAPABILITIES.map(capability => capability.id);
    expect(new Set(ids).size).toBe(ids.length);
  });

  it('keeps user-launchable and provider-routable subsets inside known capabilities', () => {
    const ids = new Set(TASK_CAPABILITIES.map(capability => capability.id));
    expect(USER_LAUNCHABLE_TASK_TYPE_IDS.every(id => ids.has(id))).toBe(true);
    expect(PROVIDER_ROUTABLE_TASK_TYPE_IDS.every(id => ids.has(id))).toBe(true);
    expect(FILTER_TASK_TYPE_IDS.every(id => ids.has(id))).toBe(true);
  });

  it('keeps rollup as a UI-only synthetic type', () => {
    expect(FILTER_TASK_TYPE_IDS).toContain('rollup');
    expect(PROVIDER_ROUTABLE_TASK_TYPE_IDS).not.toContain('rollup');
    expect(USER_LAUNCHABLE_TASK_TYPE_IDS).not.toContain('rollup');
  });

  it('includes backend task capabilities that were previously hidden from filters', () => {
    expect(FILTER_TASK_TYPE_IDS).toContain('self_audit');
    expect(PROVIDER_ROUTABLE_TASK_TYPE_IDS).toContain('investigate');
  });

  it('does not invent labels for unknown future task types', () => {
    expect(getTaskTypeLabel('future_capability')).toBe('future_capability');
  });
});

describe('resolveTaskCapabilities (contract-driven)', () => {
  it('falls back to the built-in table when no contract is published', () => {
    const resolved = resolveTaskCapabilities();
    const launchable = resolved.userLaunchableOptions.map(o => o.value);
    expect(launchable).toEqual([...USER_LAUNCHABLE_TASK_TYPE_IDS]);
    // rollup is a UI-only synthetic type: visible in filters, never routable.
    expect(resolved.filterOptions.map(o => o.value)).toContain('rollup');
    expect(resolved.providerRoutableIds).not.toContain('rollup');
    // Fallback reservable set mirrors the backend defaults.
    expect(resolved.reservableIds).toEqual(['create', 'plan', 'research']);
  });

  it('falls back when the contract is an empty object', () => {
    const resolved = resolveTaskCapabilities({});
    expect(resolved.userLaunchableOptions.map(o => o.value)).toEqual([...USER_LAUNCHABLE_TASK_TYPE_IDS]);
  });

  it('drives launchable, routable, reservable and labels from the published contract', () => {
    const contract: TaskCapabilityContract = {
      web_research: {
        type: 'web_research',
        label: 'Web Research',
        badge: 'Web',
        description: 'Web-first research producing knowledge documents.',
        user_launchable: true,
        provider_configurable: true,
        filter_visible: true,
        reservable: true,
      },
      hidden_audit: {
        type: 'hidden_audit',
        label: 'Hidden Audit',
        user_launchable: false,
        provider_configurable: true,
        filter_visible: false,
        reservable: false,
      },
    };
    const resolved = resolveTaskCapabilities(contract);

    // A newly registered launchable type surfaces automatically.
    expect(resolved.userLaunchableOptions.map(o => o.value)).toEqual(['web_research']);
    // provider-configurable types are routable regardless of launchability.
    expect(resolved.providerRoutableIds).toEqual(expect.arrayContaining(['web_research', 'hidden_audit']));
    // reservable flag flows through to the reserved-slot allowlist.
    expect(resolved.reservableIds).toEqual(['web_research']);
    // a non-filter-visible type stays out of the filter list.
    expect(resolved.filterOptions.map(o => o.value)).not.toContain('hidden_audit');
    expect(resolved.filterIds).toEqual(expect.arrayContaining(['web_research']));
    // labels come from the contract, not a hardcoded table.
    expect(resolved.getLabel('web_research')).toBe('Web Research');
    expect(resolved.getLabel('unknown_type')).toBe('unknown_type');
  });

  it('overlays UI-only synthetic types the backend never publishes (rollup)', () => {
    const contract: TaskCapabilityContract = {
      create: { type: 'create', label: 'Create', user_launchable: true, provider_configurable: true, filter_visible: true },
    };
    const resolved = resolveTaskCapabilities(contract);
    expect(resolved.byId.rollup?.synthetic).toBe(true);
    expect(resolved.filterOptions.map(o => o.value)).toContain('rollup');
    expect(resolved.providerRoutableIds).not.toContain('rollup');
    expect(resolved.userLaunchableOptions.map(o => o.value)).not.toContain('rollup');
  });

  it('keeps board filtering contract-driven for newly published filter-visible types', () => {
    const contract: TaskCapabilityContract = {
      create: { type: 'create', label: 'Create', user_launchable: true, provider_configurable: true, filter_visible: true },
      release_audit: { type: 'release_audit', label: 'Release Audit', provider_configurable: true, filter_visible: true },
    };
    const resolved = resolveTaskCapabilities(contract);

    expect(resolved.filterIds).toContain('release_audit');
    expect(matchesTaskTypeFilter('release_audit', ['release_audit'], resolved.filterIds)).toBe(true);
    expect(matchesTaskTypeFilter('release_audit', ['create'], resolved.filterIds)).toBe(false);
  });
});
