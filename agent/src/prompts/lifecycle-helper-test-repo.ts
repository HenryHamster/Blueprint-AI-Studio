import { execFileSync } from 'node:child_process';

/** Make a copied lifecycle helper look exactly like its production placement:
 * committed at a clean repository HEAD with the wrapper's exact base marker. */
export function prepareLifecycleHelperTestRepo(dir: string, taskId: string): void {
  const git = process.platform === 'win32' ? 'C:/Program Files/Git/cmd/git.exe' : 'git';
  const bash = process.platform === 'win32' ? 'C:/Program Files/Git/bin/bash.exe' : 'bash';
  const runGit = (args: string[]): string => execFileSync(
    git, args, { cwd: dir, encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] },
  ).trim();
  runGit(['init']);
  runGit(['config', 'user.name', 'Lifecycle Helper Test']);
  runGit(['config', 'user.email', 'lifecycle-helper@example.test']);
  runGit(['add', '-A']);
  runGit(['commit', '-m', 'clean helper fixture']);
  const baseCommit = runGit(['rev-parse', 'HEAD']);
  const marker = `/tmp/base_commit_${taskId}`;
  execFileSync(
    bash, ['-c', 'printf %s "$1" > "$2"', 'helper-fixture', baseCommit, marker],
    { stdio: 'pipe' },
  );
}
