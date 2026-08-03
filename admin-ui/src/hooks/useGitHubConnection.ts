import { useCallback, useEffect, useState } from 'react';
import { API_BASE_URL, authGet } from '../api';
import { apiErrorMessage } from '../api/core';

export interface GitHubConnectionStatus {
  identity_linked: boolean;
  credential_kind: 'oauth' | 'pat' | null;
  account_name: string | null;
  app_configured: boolean;
  app_installation_linked: boolean;
  app_installation_reachable: boolean;
  installation_count: number;
}

const UNKNOWN_STATUS: GitHubConnectionStatus = {
  identity_linked: false,
  credential_kind: null,
  account_name: null,
  app_configured: false,
  app_installation_linked: false,
  app_installation_reachable: false,
  installation_count: 0,
};

export function useGitHubConnection(enabled = true) {
  const [status, setStatus] = useState<GitHubConnectionStatus | null>(null);
  const [loading, setLoading] = useState(enabled);
  const [error, setError] = useState<string | null>(null);
  const [connecting, setConnecting] = useState(false);

  useEffect(() => {
    let active = true;
    if (!enabled) {
      return () => { active = false; };
    }
    void (async () => {
      try {
        const response = await authGet(`${API_BASE_URL}/api/github/status`);
        if (!response.ok) {
          const data = await response.json().catch(() => ({}));
          throw new Error(apiErrorMessage(data, 'Could not check your GitHub connection'));
        }
        const data = await response.json();
        if (active) {
          setStatus({
            identity_linked: data?.identity_linked === true,
            credential_kind: data?.credential_kind === 'oauth' || data?.credential_kind === 'pat' ? data.credential_kind : null,
            account_name: typeof data?.account_name === 'string' ? data.account_name : null,
            app_configured: data?.app_configured === true,
            app_installation_linked: data?.app_installation_linked === true,
            app_installation_reachable: data?.app_installation_reachable === true,
            installation_count: Number.isInteger(data?.installation_count) ? data.installation_count : 0,
          });
        }
      } catch (reason) {
        if (active) {
          setStatus(UNKNOWN_STATUS);
          setError(reason instanceof Error ? reason.message : 'Could not check your GitHub connection');
        }
      } finally {
        if (active) setLoading(false);
      }
    })();
    return () => { active = false; };
  }, [enabled]);

  const connect = useCallback(async (beforeRedirect?: () => void) => {
    setConnecting(true);
    setError(null);
    try {
      const params = new URLSearchParams({ redirect_uri: window.location.origin });
      const response = await authGet(`${API_BASE_URL}/api/github/oauth/authorize?${params.toString()}`);
      if (!response.ok) {
        const data = await response.json().catch(() => ({}));
        throw new Error(apiErrorMessage(data, 'Failed to start GitHub connection'));
      }
      const data = await response.json();
      if (typeof data?.url !== 'string' || !data.url) throw new Error('GitHub connection URL was missing');
      beforeRedirect?.();
      window.location.assign(data.url);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Failed to start GitHub connection');
      setConnecting(false);
    }
  }, []);

  return { status, loading, error, connecting, connect };
}
