import { describe, expect, it } from 'vitest';
import { requireClaimAgentToken, taskRequiresGithubCredentials } from './index.js';

describe('Resident credential boundary', () => {
  it('bypasses GitHub only for bounded Resident turns', () => {
    expect(taskRequiresGithubCredentials('create')).toBe(true);
    expect(taskRequiresGithubCredentials('strategic_resident_turn')).toBe(false);
  });

  it('requires the server-rotated token for a Resident claim', () => {
    expect(requireClaimAgentToken('create', '')).toBe('');
    expect(requireClaimAgentToken('strategic_resident_turn', 'rotated')).toBe('rotated');
    expect(() => requireClaimAgentToken('strategic_resident_turn', '')).toThrow(
      'resident claim token rotation failed: missing agent_token',
    );
  });
});
