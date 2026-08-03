import { beforeEach, describe, expect, test } from 'vitest';
import {
  clearGitHubOAuthCallbackQuery,
  NEW_PROJECT_OAUTH_RESUME_KEY,
  readNewProjectOAuthResume,
  saveNewProjectOAuthResume,
  shouldResumeNewProjectAfterGitHubOAuth,
  type NewProjectOAuthResumeState,
} from './newProjectOAuthResume';

const resumeState: NewProjectOAuthResumeState = {
  version: 1,
  repoInput: 'acme/platform',
  projectName: 'Platform',
  goal: 'Ship safely',
  projectType: 'product',
  users: 'internal teams',
  team: 'three engineers',
  deployCadence: 'weekly',
  notes: 'No Friday deploys',
  constraints: ['Keep CI green'],
  promotionTargetInput: 'main',
  useManualRepo: true,
  repoFilter: '',
  previewDefaultBranch: 'main',
};

beforeEach(() => {
  window.sessionStorage.clear();
  window.history.replaceState({ retained: true }, '', '/project?keep=1#details');
});

describe('new-project GitHub OAuth resume state', () => {
  test('serializes only the explicit non-credential schema', () => {
    saveNewProjectOAuthResume({
      ...resumeState,
      githubOAuthToken: 'must-not-persist',
    } as NewProjectOAuthResumeState & { githubOAuthToken: string });

    const stored = JSON.parse(window.sessionStorage.getItem(NEW_PROJECT_OAUTH_RESUME_KEY) ?? '{}');
    expect(stored).toEqual(resumeState);
    expect(stored).not.toHaveProperty('githubOAuthToken');
  });

  test('resumes only a valid snapshot on a successful callback', () => {
    saveNewProjectOAuthResume(resumeState);
    window.history.replaceState({}, '', '/?github_connected=octocat');

    expect(shouldResumeNewProjectAfterGitHubOAuth()).toBe(true);

    window.history.replaceState({}, '', '/?github_error=failed');
    expect(shouldResumeNewProjectAfterGitHubOAuth()).toBe(false);
  });

  test('clears invalid snapshots instead of leaving stale state', () => {
    window.sessionStorage.setItem(NEW_PROJECT_OAUTH_RESUME_KEY, '{invalid-json');

    expect(readNewProjectOAuthResume()).toBeNull();
    expect(window.sessionStorage.getItem(NEW_PROJECT_OAUTH_RESUME_KEY)).toBeNull();
  });

  test('ignores unexpected fields in a previously stored snapshot', () => {
    window.sessionStorage.setItem(NEW_PROJECT_OAUTH_RESUME_KEY, JSON.stringify({
      ...resumeState,
      githubOAuthToken: 'must-not-surface',
    }));

    expect(readNewProjectOAuthResume()).toEqual(resumeState);
    expect(readNewProjectOAuthResume()).not.toHaveProperty('githubOAuthToken');
  });

  test('removes only GitHub callback params and preserves other navigation state', () => {
    window.history.replaceState(
      { retained: true },
      '',
      '/project?keep=1&github_connected=octocat&github_error=failed#details',
    );

    clearGitHubOAuthCallbackQuery();

    expect(`${window.location.pathname}${window.location.search}${window.location.hash}`).toBe('/project?keep=1#details');
    expect(window.history.state).toEqual({ retained: true });
  });
});
