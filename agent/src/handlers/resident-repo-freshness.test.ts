import { execFileSync } from 'child_process';
import fs from 'fs';
import os from 'os';
import path from 'path';
import { afterEach, describe, expect, it } from 'vitest';
import {
  formatResidentRepoSnapshot,
  syncResidentRepository,
} from './resident-repo-freshness.js';

const tempDirs: string[] = [];

function git(cwd: string, ...args: string[]): string {
  return execFileSync('git', ['-C', cwd, ...args], { encoding: 'utf-8' }).trim();
}

function makeRepository(): { remote: string; seed: string; checkout: string } {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'resident-repo-freshness-'));
  tempDirs.push(root);
  const remote = path.join(root, 'remote.git');
  const seed = path.join(root, 'seed');
  const checkout = path.join(root, 'checkout');
  execFileSync('git', ['init', '--bare', remote]);
  execFileSync('git', ['init', seed]);
  git(seed, 'config', 'user.email', 'test@example.com');
  git(seed, 'config', 'user.name', 'Blueprint Test');
  fs.writeFileSync(path.join(seed, 'README.md'), 'one\n');
  git(seed, 'add', 'README.md');
  git(seed, 'commit', '-m', 'initial');
  git(seed, 'branch', '-M', 'main');
  git(seed, 'remote', 'add', 'origin', remote);
  git(seed, 'push', '-u', 'origin', 'main');
  execFileSync('git', ['clone', '--branch', 'main', remote, checkout]);
  return { remote, seed, checkout };
}

afterEach(() => {
  for (const dir of tempDirs.splice(0)) fs.rmSync(dir, { recursive: true, force: true });
});

describe('syncResidentRepository', () => {
  it('moves the checkout to origin and reports a bounded delta without creating a wake', () => {
    const { seed, checkout } = makeRepository();
    const previousHead = git(checkout, 'rev-parse', 'HEAD');
    fs.writeFileSync(path.join(seed, 'next.txt'), 'two\n');
    git(seed, 'add', 'next.txt');
    git(seed, 'commit', '-m', 'add next');
    git(seed, 'push', 'origin', 'main');

    const snapshot = syncResidentRepository(checkout, 'main', previousHead);

    expect(snapshot.status).toBe('CURRENT');
    expect(snapshot.head).toBe(git(seed, 'rev-parse', 'HEAD'));
    expect(git(checkout, 'rev-parse', 'HEAD')).toBe(snapshot.head);
    expect(snapshot.commits).toContainEqual(expect.stringContaining('add next'));
    expect(snapshot.changedFiles).toEqual(['next.txt']);
    expect(snapshot.deltaAvailable).toBe(true);
    expect(formatResidentRepoSnapshot(snapshot)).toContain('Status: CURRENT');
  });

  it('keeps a synchronized checkout CURRENT when no prior baseline exists', () => {
    const { checkout } = makeRepository();

    const snapshot = syncResidentRepository(checkout, 'main', null);

    expect(snapshot.status).toBe('CURRENT');
    expect(snapshot.deltaAvailable).toBe(false);
    expect(formatResidentRepoSnapshot(snapshot)).toContain(
      'Delta: unavailable; baseline reset to the current HEAD.',
    );
  });

  it('keeps a synchronized checkout CURRENT when the prior baseline is unavailable', () => {
    const { checkout } = makeRepository();
    const unavailablePrior = 'a'.repeat(40);

    const snapshot = syncResidentRepository(checkout, 'main', unavailablePrior);

    expect(snapshot.status).toBe('CURRENT');
    expect(snapshot.head).toBe(git(checkout, 'rev-parse', 'HEAD'));
    expect(snapshot.deltaAvailable).toBe(false);
    expect(snapshot.reason).toBeNull();
  });

  it('returns UNKNOWN with the observed local HEAD when refresh fails', () => {
    const { checkout } = makeRepository();
    git(checkout, 'remote', 'set-url', 'origin', path.join(checkout, 'missing.git'));

    const snapshot = syncResidentRepository(checkout, 'main', null);

    expect(snapshot.status).toBe('UNKNOWN');
    expect(snapshot.head).toMatch(/^[0-9a-f]{40}$/);
    expect(formatResidentRepoSnapshot(snapshot)).toContain(
      'Operator conversation remains available',
    );
  });
});
