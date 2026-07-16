import { describe, it, expect } from 'vitest';
import { resolveBuildConfig } from './build-config.js';

describe('resolveBuildConfig', () => {
  it('returns undefined when project_metadata.skip_build_config is true', () => {
    const task = {
      project_metadata: { skip_build_config: true },
      build_config: { directories: [{ path: 'src', type: 'node' }] },
    };
    expect(resolveBuildConfig(task)).toBeUndefined();
  });

  it('returns undefined when build_config is missing', () => {
    expect(resolveBuildConfig({})).toBeUndefined();
  });

  it('returns undefined when build_config is empty object', () => {
    expect(resolveBuildConfig({ build_config: {} })).toBeUndefined();
  });

  it('returns undefined when directories is missing', () => {
    expect(resolveBuildConfig({ build_config: { language: 'python' } })).toBeUndefined();
  });

  it('returns undefined when directories is an empty array', () => {
    expect(resolveBuildConfig({ build_config: { directories: [] } })).toBeUndefined();
  });

  it('returns undefined when directories is not an array', () => {
    expect(resolveBuildConfig({ build_config: { directories: 'not-an-array' } })).toBeUndefined();
  });

  it('rejects path traversal entries containing ".."', () => {
    const task = {
      build_config: {
        directories: [
          { path: '../escape', type: 'python' },
          { path: 'foo/../bar', type: 'python' },
        ],
      },
    };
    expect(resolveBuildConfig(task)).toBeUndefined();
  });

  it('rejects absolute paths starting with "/"', () => {
    const task = {
      build_config: { directories: [{ path: '/etc/passwd', type: 'python' }] },
    };
    expect(resolveBuildConfig(task)).toBeUndefined();
  });

  it('rejects paths containing null bytes', () => {
    const task = {
      build_config: { directories: [{ path: 'src\0evil', type: 'python' }] },
    };
    expect(resolveBuildConfig(task)).toBeUndefined();
  });

  it('skips entries with missing/empty path but keeps valid siblings', () => {
    const task = {
      build_config: {
        directories: [
          { path: '', type: 'node' },
          { type: 'node' },
          null,
          'not-an-object',
          { path: 'app', type: 'node' },
        ],
      },
    };
    expect(resolveBuildConfig(task)).toEqual({
      directories: [{ path: 'app', type: 'node' }],
    });
  });

  it('extracts a valid directory config with all command fields', () => {
    const task = {
      build_config: {
        directories: [
          {
            path: 'backend',
            type: 'python',
            install_cmd: 'pip install -e .',
            lint_cmd: 'ruff check',
            build_cmd: 'python -m build',
            test_cmd: 'pytest',
          },
        ],
      },
    };
    expect(resolveBuildConfig(task)).toEqual({
      directories: [
        {
          path: 'backend',
          type: 'python',
          install_cmd: 'pip install -e .',
          lint_cmd: 'ruff check',
          build_cmd: 'python -m build',
          test_cmd: 'pytest',
        },
      ],
    });
  });

  it('accepts each valid type (python, node, go, rust)', () => {
    for (const type of ['python', 'node', 'go', 'rust']) {
      const task = { build_config: { directories: [{ path: 'd', type }] } };
      expect(resolveBuildConfig(task)?.directories?.[0].type).toBe(type);
    }
  });

  it('preserves type="other" for extra-family languages (Ruby, Gradle, Maven, etc.)', () => {
    const task = {
      build_config: { directories: [{ path: 'src', type: 'other' }] },
    };
    expect(resolveBuildConfig(task)?.directories?.[0].type).toBe('other');
  });

  it('falls back to "python" for truly unknown type values', () => {
    const task = {
      build_config: { directories: [{ path: 'src', type: 'cobol' }] },
    };
    expect(resolveBuildConfig(task)?.directories?.[0].type).toBe('python');
  });

  it('falls back to "python" when type is missing entirely', () => {
    const task = { build_config: { directories: [{ path: 'src' }] } };
    expect(resolveBuildConfig(task)?.directories?.[0].type).toBe('python');
  });

  it('maps legacy "language" field to type for backwards compat', () => {
    const task = {
      build_config: { directories: [{ path: 'src', language: 'go' }] },
    };
    expect(resolveBuildConfig(task)?.directories?.[0].type).toBe('go');
  });

  it('prefers "type" over "language" when both are present', () => {
    const task = {
      build_config: { directories: [{ path: 'src', type: 'node', language: 'go' }] },
    };
    expect(resolveBuildConfig(task)?.directories?.[0].type).toBe('node');
  });

  it('maps legacy lint_command/build_command/test_command to *_cmd', () => {
    const task = {
      build_config: {
        directories: [
          {
            path: 'backend',
            type: 'python',
            lint_command: 'old-lint',
            build_command: 'old-build',
            test_command: 'old-test',
          },
        ],
      },
    };
    expect(resolveBuildConfig(task)?.directories?.[0]).toEqual({
      path: 'backend',
      type: 'python',
      lint_cmd: 'old-lint',
      build_cmd: 'old-build',
      test_cmd: 'old-test',
    });
  });

  it('prefers new *_cmd fields over legacy *_command fields', () => {
    const task = {
      build_config: {
        directories: [
          {
            path: 'backend',
            type: 'python',
            lint_cmd: 'new-lint',
            lint_command: 'old-lint',
          },
        ],
      },
    };
    expect(resolveBuildConfig(task)?.directories?.[0].lint_cmd).toBe('new-lint');
  });

  it('omits command fields when not strings', () => {
    const task = {
      build_config: {
        directories: [
          {
            path: 'src',
            type: 'node',
            lint_cmd: 123,
            build_cmd: null,
            test_cmd: { bad: true },
          },
        ],
      },
    };
    const entry = resolveBuildConfig(task)?.directories?.[0];
    expect(entry).toEqual({ path: 'src', type: 'node' });
  });

  it('retains only an explicit safe project-verification harness contract', () => {
    const valid = resolveBuildConfig({
      build_config: {
        directories: [{
          path: '.', type: 'python',
          project_verification: { protocol: 'blueprint-v1', harness: 'tools/verify.sh' },
        }],
      },
    });
    expect(valid?.directories?.[0].project_verification).toEqual({
      protocol: 'blueprint-v1', harness: 'tools/verify.sh',
    });

    const canonical = resolveBuildConfig({
      build_config: {
        directories: [{
          path: ' backend/// ', type: 'python',
          project_verification: { protocol: 'blueprint-v1', harness: 'tools/verify.sh' },
        }],
      },
    });
    expect(canonical?.directories?.[0].path).toBe('backend');

    for (const project_verification of [
      { protocol: 'other', harness: 'tools/verify.sh' },
      { protocol: 'blueprint-v1', harness: '../verify.sh' },
      { protocol: 'blueprint-v1', harness: '/tmp/verify.sh' },
      { protocol: 'blueprint-v1', harness: 'tools/verify script.sh' },
    ]) {
      const config = resolveBuildConfig({
        build_config: { directories: [{ path: '.', type: 'python', project_verification }] },
      });
      expect(config?.directories?.[0].project_verification).toBeUndefined();
    }
  });

  it('maps nested git_identity object to git_user_name/email', () => {
    const task = {
      build_config: {
        directories: [{ path: 'src', type: 'node' }],
        git_identity: { name: 'Alice', email: 'alice@example.com' },
      },
    };
    const cfg = resolveBuildConfig(task);
    expect(cfg?.git_user_name).toBe('Alice');
    expect(cfg?.git_user_email).toBe('alice@example.com');
  });

  it('uses flat git_user_name/git_user_email when git_identity is absent', () => {
    const task = {
      build_config: {
        directories: [{ path: 'src', type: 'node' }],
        git_user_name: 'Bob',
        git_user_email: 'bob@example.com',
      },
    };
    const cfg = resolveBuildConfig(task);
    expect(cfg?.git_user_name).toBe('Bob');
    expect(cfg?.git_user_email).toBe('bob@example.com');
  });

  it('prefers nested git_identity over flat git_user_* fields', () => {
    const task = {
      build_config: {
        directories: [{ path: 'src', type: 'node' }],
        git_identity: { name: 'Nested', email: 'nested@example.com' },
        git_user_name: 'Flat',
        git_user_email: 'flat@example.com',
      },
    };
    const cfg = resolveBuildConfig(task);
    expect(cfg?.git_user_name).toBe('Nested');
    expect(cfg?.git_user_email).toBe('nested@example.com');
  });

  it('extracts env_vars when object-shaped', () => {
    const task = {
      build_config: {
        directories: [{ path: 'src', type: 'node' }],
        env_vars: { FOO: 'bar', BAZ: 'qux' },
      },
    };
    expect(resolveBuildConfig(task)?.env_vars).toEqual({ FOO: 'bar', BAZ: 'qux' });
  });

  it('ignores env_vars when it is an array', () => {
    const task = {
      build_config: {
        directories: [{ path: 'src', type: 'node' }],
        env_vars: ['not', 'valid'],
      },
    };
    expect(resolveBuildConfig(task)?.env_vars).toBeUndefined();
  });

  it('propagates top-level language field', () => {
    const task = {
      build_config: {
        directories: [{ path: 'src', type: 'node' }],
        language: 'typescript',
      },
    };
    expect(resolveBuildConfig(task)?.language).toBe('typescript');
  });

  it('returns undefined when all directory entries are invalid', () => {
    const task = {
      build_config: {
        directories: [
          { path: '../bad', type: 'python' },
          { path: '/root', type: 'python' },
          { path: '' },
        ],
      },
    };
    expect(resolveBuildConfig(task)).toBeUndefined();
  });

  it('drops shell-unsafe directory PATHS but retains shell-composed COMMANDS verbatim', () => {
    const task = {
      build_config: {
        directories: [
          // unsafe PATH → whole directory dropped (path is interpolated as a cwd)
          { path: 'svc$(whoami)`rm`;echo|cat&', type: 'node', lint_cmd: 'npm run lint' },
          { path: 'backend', type: 'python', build_cmd: 'python -m build' },
          // unsafe COMMAND no longer drops the dir — the command is retained so
          // the backend's tests_gated decision and the agent's gate agree.
          { path: 'worker', type: 'python', install_cmd: 'pip install -e . && echo bad' },
        ],
      },
    };
    expect(resolveBuildConfig(task)).toEqual({
      directories: [
        { path: 'backend', type: 'python', build_cmd: 'python -m build' },
        { path: 'worker', type: 'python', install_cmd: 'pip install -e . && echo bad' },
      ],
    });
  });

  it('retains quoted and shell-composed test commands so the gate can run them', () => {
    const task = {
      build_config: {
        directories: [
          { path: 'svc', type: 'python', test_cmd: 'pytest -k "not slow"' },
          { path: 'core', type: 'go', test_cmd: 'go test ./... && go vet ./...' },
        ],
      },
    };
    expect(resolveBuildConfig(task)).toEqual({
      directories: [
        { path: 'svc', type: 'python', test_cmd: 'pytest -k "not slow"' },
        { path: 'core', type: 'go', test_cmd: 'go test ./... && go vet ./...' },
      ],
    });
  });

  it('surfaces valid staging_branch_prefixes onto the resolved config', () => {
    const task = {
      build_config: {
        directories: [{ path: 'src', type: 'node' }],
        staging_branch_prefixes: ['stage/', 'integration/'],
      },
    };
    expect(resolveBuildConfig(task)?.staging_branch_prefixes).toEqual(['stage/', 'integration/']);
  });

  it('drops malformed staging_branch_prefixes entries but keeps valid siblings', () => {
    const task = {
      build_config: {
        directories: [{ path: 'src', type: 'node' }],
        staging_branch_prefixes: ['stage/', '', '../escape', '/abs', 'evil$(x)', 42, 'integration/'],
      },
    };
    expect(resolveBuildConfig(task)?.staging_branch_prefixes).toEqual(['stage/', 'integration/']);
  });

  it('leaves staging_branch_prefixes unset when the field is absent', () => {
    const task = { build_config: { directories: [{ path: 'src', type: 'node' }] } };
    expect(resolveBuildConfig(task)?.staging_branch_prefixes).toBeUndefined();
  });

  it('leaves staging_branch_prefixes unset when no entry survives validation', () => {
    const task = {
      build_config: {
        directories: [{ path: 'src', type: 'node' }],
        staging_branch_prefixes: ['', '/abs', '../x'],
      },
    };
    expect(resolveBuildConfig(task)?.staging_branch_prefixes).toBeUndefined();
  });

  it('forwards a valid task_branch_namespace onto the resolved config', () => {
    const task = {
      build_config: {
        directories: [{ path: 'src', type: 'node' }],
        task_branch_namespace: 'stsbot',
      },
    };
    expect(resolveBuildConfig(task)?.task_branch_namespace).toBe('stsbot');
  });

  it('drops an unsafe task_branch_namespace (slash / traversal / case / shell chars)', () => {
    for (const ns of ['evil/x', '../escape', 'has space', 'Up$(x)', '', 'Capitalized']) {
      const task = {
        build_config: {
          directories: [{ path: 'src', type: 'node' }],
          task_branch_namespace: ns,
        },
      };
      expect(resolveBuildConfig(task)?.task_branch_namespace).toBeUndefined();
    }
  });

  it('leaves task_branch_namespace unset when absent', () => {
    const task = { build_config: { directories: [{ path: 'src', type: 'node' }] } };
    expect(resolveBuildConfig(task)?.task_branch_namespace).toBeUndefined();
  });

  it('ignores staging_branch_prefixes when it is not an array', () => {
    const task = {
      build_config: {
        directories: [{ path: 'src', type: 'node' }],
        staging_branch_prefixes: 'stage/',
      },
    };
    expect(resolveBuildConfig(task)?.staging_branch_prefixes).toBeUndefined();
  });

  // ── migration config (de-hardcode the migrate handler's layout) ────────────
  it('parses a full migration config, stripping trailing slashes', () => {
    const task = {
      build_config: {
        directories: [{ path: 'svc', type: 'python' }],
        migration: {
          versions_dir: 'svc/db/migrations/',
          models_dir: 'svc/orm/',
          alembic_cwd: 'svc/',
          schema_revision_file: 'svc/config.py',
          schema_revision_const: 'DB_REVISION',
        },
      },
    };
    expect(resolveBuildConfig(task)?.migration).toEqual({
      versions_dir: 'svc/db/migrations',
      models_dir: 'svc/orm',
      alembic_cwd: 'svc',
      schema_revision_file: 'svc/config.py',
      schema_revision_const: 'DB_REVISION',
    });
  });

  it('preserves an empty schema_revision_file as the explicit "no gate" signal', () => {
    const task = {
      build_config: {
        directories: [{ path: 'svc', type: 'python' }],
        migration: { schema_revision_file: '' },
      },
    };
    expect(resolveBuildConfig(task)?.migration).toEqual({ schema_revision_file: '' });
  });

  it('drops path-traversal / shell-unsafe migration path values', () => {
    const task = {
      build_config: {
        directories: [{ path: 'svc', type: 'python' }],
        migration: {
          versions_dir: '../escape',         // path traversal → dropped
          models_dir: '/abs/models',          // absolute path → dropped
          alembic_cwd: 'svc;rm -rf /',        // shell metachar → dropped
          schema_revision_file: 'a$(id).py',  // shell metachar → dropped
        },
      },
    };
    // Every path-shaped field is rejected → no migration object survives.
    expect(resolveBuildConfig(task)?.migration).toBeUndefined();
  });

  it('rejects a migration object whose only fields are all unsafe', () => {
    const task = {
      build_config: {
        directories: [{ path: 'svc', type: 'python' }],
        migration: { versions_dir: 'a&&b', models_dir: 'x|y' },
      },
    };
    expect(resolveBuildConfig(task)?.migration).toBeUndefined();
  });

  it('leaves migration undefined when the field is absent (Blueprint default path)', () => {
    const task = { build_config: { directories: [{ path: 'backend', type: 'python' }] } };
    expect(resolveBuildConfig(task)?.migration).toBeUndefined();
  });

  // ── confirmed_lint_cmd passthrough ─────────────────────────────────────────
  it('resolveBuildConfig preserves confirmed_lint_cmd', () => {
    const task = {
      build_config: {
        directories: [
          {
            path: 'backend',
            type: 'python',
            lint_cmd: 'ruff check',
            confirmed_lint_cmd: 'ruff check --select ALL',
          },
        ],
      },
    };
    const entry = resolveBuildConfig(task)?.directories?.[0];
    expect(entry?.confirmed_lint_cmd).toBe('ruff check --select ALL');
    expect(entry?.lint_cmd).toBe('ruff check');
  });

  it('carries confirmed_lint_cmd even when commands_stale is true (stale is enforced at read time by effectiveLintCmd)', () => {
    const task = {
      build_config: {
        directories: [
          {
            path: 'backend',
            type: 'python',
            confirmed_lint_cmd: 'ruff check',
            commands_stale: true,
          },
        ],
      },
    };
    const entry = resolveBuildConfig(task)?.directories?.[0];
    expect(entry?.confirmed_lint_cmd).toBe('ruff check');
    expect(entry?.commands_stale).toBe(true);
  });

  it('omits confirmed_lint_cmd when not a string', () => {
    const task = {
      build_config: {
        directories: [
          {
            path: 'backend',
            type: 'python',
            confirmed_lint_cmd: 42,
          },
        ],
      },
    };
    const entry = resolveBuildConfig(task)?.directories?.[0];
    expect(entry).not.toHaveProperty('confirmed_lint_cmd');
  });
});
