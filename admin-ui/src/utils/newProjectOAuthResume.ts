export const NEW_PROJECT_OAUTH_RESUME_KEY = 'blueprint:new-project:github-oauth-resume:v1';

export interface NewProjectOAuthResumeState {
  version: 1;
  repoInput: string;
  projectName: string;
  goal: string;
  projectType: string;
  users: string;
  team: string;
  deployCadence: string;
  notes: string;
  constraints: string[];
  promotionTargetInput: string;
  useManualRepo: boolean;
  repoFilter: string;
  previewDefaultBranch: string;
}

export function saveNewProjectOAuthResume(state: NewProjectOAuthResumeState): void {
  try {
    // Serialize an explicit allowlist so a future caller cannot accidentally
    // persist a credential or token by passing a wider runtime object.
    const snapshot: NewProjectOAuthResumeState = {
      version: 1,
      repoInput: state.repoInput,
      projectName: state.projectName,
      goal: state.goal,
      projectType: state.projectType,
      users: state.users,
      team: state.team,
      deployCadence: state.deployCadence,
      notes: state.notes,
      constraints: [...state.constraints],
      promotionTargetInput: state.promotionTargetInput,
      useManualRepo: state.useManualRepo,
      repoFilter: state.repoFilter,
      previewDefaultBranch: state.previewDefaultBranch,
    };
    window.sessionStorage.setItem(NEW_PROJECT_OAUTH_RESUME_KEY, JSON.stringify(snapshot));
  } catch {
    // OAuth still works when session storage is unavailable; only form resume is lost.
  }
}

export function readNewProjectOAuthResume(): NewProjectOAuthResumeState | null {
  try {
    const raw = window.sessionStorage.getItem(NEW_PROJECT_OAUTH_RESUME_KEY);
    if (!raw) return null;
    const value = JSON.parse(raw) as Partial<NewProjectOAuthResumeState>;
    if (value.version !== 1 || !Array.isArray(value.constraints)) {
      clearNewProjectOAuthResume();
      return null;
    }
    const stringFields = [
      'repoInput',
      'projectName',
      'goal',
      'projectType',
      'users',
      'team',
      'deployCadence',
      'notes',
      'promotionTargetInput',
      'repoFilter',
      'previewDefaultBranch',
    ] as const;
    if (stringFields.some(field => typeof value[field] !== 'string')) {
      clearNewProjectOAuthResume();
      return null;
    }
    if (!value.constraints.every(item => typeof item === 'string')) {
      clearNewProjectOAuthResume();
      return null;
    }
    if (typeof value.useManualRepo !== 'boolean') {
      clearNewProjectOAuthResume();
      return null;
    }
    return {
      version: 1,
      repoInput: value.repoInput as string,
      projectName: value.projectName as string,
      goal: value.goal as string,
      projectType: value.projectType as string,
      users: value.users as string,
      team: value.team as string,
      deployCadence: value.deployCadence as string,
      notes: value.notes as string,
      constraints: [...value.constraints],
      promotionTargetInput: value.promotionTargetInput as string,
      useManualRepo: value.useManualRepo,
      repoFilter: value.repoFilter as string,
      previewDefaultBranch: value.previewDefaultBranch as string,
    };
  } catch {
    clearNewProjectOAuthResume();
    return null;
  }
}

export function clearNewProjectOAuthResume(): void {
  try {
    window.sessionStorage.removeItem(NEW_PROJECT_OAUTH_RESUME_KEY);
  } catch {
    // Best effort only.
  }
}

export function shouldResumeNewProjectAfterGitHubOAuth(): boolean {
  const params = new URLSearchParams(window.location.search);
  return Boolean(params.get('github_connected') && readNewProjectOAuthResume());
}

export function clearGitHubOAuthCallbackQuery(): void {
  const url = new URL(window.location.href);
  url.searchParams.delete('github_connected');
  url.searchParams.delete('github_error');
  const query = url.searchParams.toString();
  const nextUrl = `${url.pathname}${query ? `?${query}` : ''}${url.hash}`;
  window.history.replaceState(window.history.state, '', nextUrl);
}
