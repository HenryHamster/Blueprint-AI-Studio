import fs from 'fs';
import os from 'os';
import path from 'path';
import { execFileSync } from 'child_process';
import { afterEach, describe, expect, it } from 'vitest';
import { generateArtifactsScript, writeArtifactsHelper } from './artifacts-helper.js';

const workspaces: string[] = [];

function workspace(): string {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'artifacts-helper-'));
  fs.mkdirSync(path.join(root, '.git', 'info'), { recursive: true });
  workspaces.push(root);
  return root;
}

afterEach(() => {
  for (const root of workspaces.splice(0)) {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

describe('writeArtifactsHelper', () => {
  it('writes an executable helper and local git excludes', () => {
    const root = workspace();
    writeArtifactsHelper(
      root,
      () => path.join(root, '.git', 'info', 'exclude'),
      () => true,
    );
    if (process.platform !== 'win32') {
      expect(fs.statSync(path.join(root, 'artifacts')).mode & 0o111).not.toBe(0);
    }
    const exclude = fs.readFileSync(path.join(root, '.git', 'info', 'exclude'), 'utf-8');
    expect(exclude).toContain('/artifacts');
    expect(exclude).toContain('/.blueprint-inputs/');
  });

  it('uses the rotating token, same-project endpoint, bounded download, and rejects unsafe destinations', () => {
    const script = generateArtifactsScript();
    expect(script).toContain('_load_agent_token');
    expect(script).toContain('/api/agent/compute-jobs/$JOB_ID/artifact/download-url');
    expect(script).toContain('--max-filesize 104857600');
    expect(script).toContain('compute artifact destination contains a symlink');
    expect(script).not.toContain('--dest');
    expect(script).not.toContain('gcloud');
    expect(script).not.toContain('gsutil');
  });

  it.skipIf(process.platform === 'win32')(
    'downloads the exact bundle to the default ignored job directory',
    () => {
    const root = workspace();
    writeArtifactsHelper(
      root,
      () => path.join(root, '.git', 'info', 'exclude'),
      () => true,
    );
    const fakeBin = path.join(root, 'fake-bin');
    fs.mkdirSync(fakeBin);
    const curl = `#!/bin/bash
set -e
OUT=""
WRITE_CODE=0
URL=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    -o) OUT="$2"; shift 2;;
    -w) WRITE_CODE=1; shift 2;;
    -H|--max-time|--connect-timeout|--max-filesize) shift 2;;
    -fLsS|-sS) shift;;
    *) URL="$1"; shift;;
  esac
done
if [[ "$URL" == */artifact/download-url ]]; then
  printf '{"url":"https://signed.example/bundle"}' > "$OUT"
  [ "$WRITE_CODE" -eq 1 ] && printf '200'
else
  printf 'exact-archive-bytes' > "$OUT"
fi
`;
    fs.writeFileSync(path.join(fakeBin, 'curl'), curl, { mode: 0o755 });

    const output = execFileSync('bash', ['./artifacts', 'fetch', 'job-123'], {
      cwd: root,
      encoding: 'utf-8',
      env: {
        ...process.env,
        PATH: `${fakeBin}:${process.env.PATH || ''}`,
        ORCHESTRATOR_URL: 'https://blueprint.example',
        AGENT_API_TOKEN: 'test-token',
        AGENT_TOKEN_FILE: '',
      },
    });
    expect(output).toContain('.blueprint-inputs/compute-jobs/job-123/bundle.tar.gz');
    expect(fs.readFileSync(
      path.join(root, '.blueprint-inputs', 'compute-jobs', 'job-123', 'bundle.tar.gz'),
      'utf-8',
    )).toBe('exact-archive-bytes');
    },
  );

  it.skipIf(process.platform === 'win32')(
    'rejects extra destination arguments before invoking curl',
    () => {
      const root = workspace();
      writeArtifactsHelper(
        root,
        () => path.join(root, '.git', 'info', 'exclude'),
        () => true,
      );
      expect(() => execFileSync('bash', ['./artifacts', 'fetch', 'job-123', '--dest', '../outside'], {
        cwd: root,
        stdio: 'pipe',
        env: {
          ...process.env,
          ORCHESTRATOR_URL: 'https://blueprint.example',
          AGENT_API_TOKEN: 'test-token',
        },
      })).toThrow();
      expect(fs.existsSync(path.join(path.dirname(root), 'outside', 'bundle.tar.gz'))).toBe(false);
    },
  );

  it('uses the resolved worktree git metadata path instead of assuming .git is a directory', () => {
    const root = workspace();
    fs.rmSync(path.join(root, '.git'), { recursive: true, force: true });
    fs.writeFileSync(path.join(root, '.git'), 'gitdir: /tmp/worktrees/example\n');
    const metadataRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'artifacts-helper-gitdir-'));
    workspaces.push(metadataRoot);
    const excludePath = path.join(metadataRoot, 'info', 'exclude');

    writeArtifactsHelper(root, () => excludePath, () => true);

    expect(fs.readFileSync(excludePath, 'utf-8')).toContain('/.blueprint-inputs/');
  });

  it('preserves an existing exclude line and fails closed when ignore verification fails', () => {
    const root = workspace();
    const excludePath = path.join(root, '.git', 'info', 'exclude');
    fs.writeFileSync(excludePath, '/existing-without-final-newline');

    const written = writeArtifactsHelper(root, () => excludePath, () => false);

    expect(written).toBe(false);
    expect(fs.existsSync(path.join(root, 'artifacts'))).toBe(false);
    expect(fs.readFileSync(excludePath, 'utf-8')).toContain(
      '/existing-without-final-newline\n/artifacts\n/.blueprint-inputs/\n',
    );
  });
});
