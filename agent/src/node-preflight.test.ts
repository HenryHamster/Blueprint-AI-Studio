import { describe, it, expect } from 'vitest';
import { checkNodeVersion, SUPPORTED_NODE_RANGE, RECOMMENDED_NODE_MAJOR } from './node-preflight.js';

describe('checkNodeVersion', () => {
  it('passes for the current process version', () => {
    // The test environment must itself satisfy the supported range
    expect(checkNodeVersion()).toBeNull();
  });

  it('passes for Node 18.x', () => {
    expect(checkNodeVersion('v18.0.0')).toBeNull();
    expect(checkNodeVersion('v18.19.1')).toBeNull();
  });

  it('passes for Node 20.x', () => {
    expect(checkNodeVersion('v20.0.0')).toBeNull();
    expect(checkNodeVersion('v20.11.0')).toBeNull();
  });

  it('passes for Node 22.x', () => {
    expect(checkNodeVersion('v22.0.0')).toBeNull();
  });

  it('rejects Node 16.x with an actionable message', () => {
    const msg = checkNodeVersion('v16.20.0');
    expect(msg).not.toBeNull();
    expect(msg).toContain('v16.20.0');
    expect(msg).toContain(SUPPORTED_NODE_RANGE);
    expect(msg).toContain(`nvm install ${RECOMMENDED_NODE_MAJOR}`);
    expect(msg).toContain(`nvm use ${RECOMMENDED_NODE_MAJOR}`);
    expect(msg).toContain('npm start');
  });

  it('rejects Node 14.x with an actionable message', () => {
    const msg = checkNodeVersion('v14.21.3');
    expect(msg).not.toBeNull();
    expect(msg).toContain('v14.21.3');
    expect(msg).toContain(SUPPORTED_NODE_RANGE);
    expect(msg).toContain('nvm install');
  });

  it('rejects Node 12.x with an actionable message', () => {
    const msg = checkNodeVersion('v12.0.0');
    expect(msg).not.toBeNull();
    expect(msg).toContain('v12.0.0');
    expect(msg).toContain(SUPPORTED_NODE_RANGE);
  });

  it('handles an unparseable version string gracefully', () => {
    const msg = checkNodeVersion('not-a-version');
    expect(msg).not.toBeNull();
    expect(msg).toContain('not-a-version');
    expect(msg).toContain(`v${RECOMMENDED_NODE_MAJOR}.x`);
    expect(msg).toContain('nvm install');
  });

  it('accepts version strings without the leading v prefix', () => {
    expect(checkNodeVersion('20.0.0')).toBeNull();
    expect(checkNodeVersion('18.12.0')).toBeNull();
  });

  it('rejects version without v prefix if too old', () => {
    const msg = checkNodeVersion('16.0.0');
    expect(msg).not.toBeNull();
    expect(msg).toContain('16.0.0');
  });
});
