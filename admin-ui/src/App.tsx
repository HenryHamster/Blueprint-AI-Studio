import { useState, useEffect, useRef, useCallback } from 'react';
import { Plus, Activity, X, AlertCircle, AlertTriangle, CheckCircle, Terminal, GitBranch, Settings, LogOut } from 'lucide-react';
import { TaskCard } from './components/TaskCard';
import type { Task, TaskStatus } from './components/TaskCard';
import { auth, signInWithGoogle, getIdToken, onAuthChange, signOut } from './firebase';
import type { User } from 'firebase/auth';

const API_BASE_URL = import.meta.env.VITE_API_URL || '';

async function authHeaders(): Promise<Record<string, string>> {
  const token = await getIdToken();
  return { 'Authorization': `Bearer ${token}`, 'Content-Type': 'application/json' };
}

async function authGet(url: string) {
  const token = await getIdToken();
  return fetch(url, { headers: { 'Authorization': `Bearer ${token}` } });
}

export default function App() {
  const [user, setUser] = useState<User | null>(null);
  const [authLoading, setAuthLoading] = useState(true);
  const [tasks, setTasks] = useState<Task[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [page, setPage] = useState(1);
  const limit = 50;

  // New Task modal state
  const [showNewTaskModal, setShowNewTaskModal] = useState(false);
  const [newTaskRepo, setNewTaskRepo] = useState('');
  const [newTaskBranch, setNewTaskBranch] = useState('main');
  const [newTaskTitle, setNewTaskTitle] = useState('');
  const [newTaskPrompt, setNewTaskPrompt] = useState('');

  // GitHub repo/branch dropdown state
  interface RepoOption { full_name: string; default_branch: string; }
  const [repoOptions, setRepoOptions] = useState<RepoOption[]>([]);
  const [repoLoading, setRepoLoading] = useState(false);
  const [repoError, setRepoError] = useState<string | null>(null);
  const [useManualRepo, setUseManualRepo] = useState(false);
  const [branchOptions, setBranchOptions] = useState<string[]>([]);
  const [branchLoading, setBranchLoading] = useState(false);
  const [branchError, setBranchError] = useState<string | null>(null);
  const [useManualBranch, setUseManualBranch] = useState(false);

  // Log viewer state
  const [logTask, setLogTask] = useState<Task | null>(null);
  const [logLines, setLogLines] = useState<string[]>([]);
  const logEndRef = useRef<HTMLDivElement>(null);
  const wsRef = useRef<WebSocket | null>(null);

  // Settings state
  const [showSettings, setShowSettings] = useState(false);
  const [maxConcurrentVMs, setMaxConcurrentVMs] = useState(5);
  const [maxRetries, setMaxRetries] = useState(3);
  const [usingMaxPlan, setUsingMaxPlan] = useState(false);
  const settingsRef = useRef<HTMLDivElement>(null);

  // Max plan pool state
  interface MaxPlanAccount { id: string; name: string; enabled: boolean; active_tasks: number; total_tasks: number; }
  const [maxPlanAccounts, setMaxPlanAccounts] = useState<MaxPlanAccount[]>([]);
  const [newAccountName, setNewAccountName] = useState('');
  const [newAccountCreds, setNewAccountCreds] = useState('');

  // GitHub connection state
  const [githubAccount, setGithubAccount] = useState<string | null>(null);

  // Firebase auth state
  useEffect(() => {
    const unsub = onAuthChange((u) => { setUser(u); setAuthLoading(false); });
    return unsub;
  }, []);

  // Handle OAuth redirect query params
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const connected = params.get('github_connected');
    const ghError = params.get('github_error');
    if (connected) {
      setError(null);
      setGithubAccount(connected);
      window.history.replaceState({}, '', window.location.pathname);
    } else if (ghError) {
      const messages: Record<string, string> = {
        state_expired: 'GitHub OAuth expired — try again',
        invalid_state: 'GitHub OAuth failed — invalid state',
        no_installations: 'No GitHub App installations found — install the app on your repos first',
        failed: 'GitHub connection failed — try again',
      };
      setError(messages[ghError] || `GitHub error: ${ghError}`);
      window.history.replaceState({}, '', window.location.pathname);
    }
  }, []);

  useEffect(() => {
    if (!user) return;
    fetchTasks();
    fetchSettings();
    const interval = setInterval(fetchTasks, 5000);
    return () => clearInterval(interval);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [page, user]);

  // Auto-scroll log viewer
  useEffect(() => {
    logEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [logLines]);

  // Clean up WebSocket on close
  useEffect(() => {
    return () => {
      if (wsRef.current) {
        wsRef.current.close();
        wsRef.current = null;
      }
    };
  }, []);

  // Close settings dropdown on outside click
  useEffect(() => {
    if (!showSettings) return;
    const handleClick = (e: MouseEvent) => {
      if (settingsRef.current && !settingsRef.current.contains(e.target as Node)) {
        setShowSettings(false);
      }
    };
    document.addEventListener('mousedown', handleClick);
    return () => document.removeEventListener('mousedown', handleClick);
  }, [showSettings]);

  const fetchTasks = async () => {
    try {
      const offset = (page - 1) * limit;
      const res = await authGet(`${API_BASE_URL}/api/admin/tasks?limit=${limit}&offset=${offset}`);
      if (res.ok) {
        const data = await res.json();
        setTasks(data || []);
        setError(null);
      } else {
        setError(`Failed to fetch tasks: ${res.statusText}`);
      }
    } catch (err) {
      console.error('Failed to fetch tasks:', err);
      setError('Failed to connect to backend server.');
    } finally {
      setLoading(false);
    }
  };

  const handleApprove = async (taskId: string) => {
    try {
      await fetch(`${API_BASE_URL}/api/admin/tasks/${taskId}`, {
        method: 'PATCH',
        headers: await authHeaders(),
        body: JSON.stringify({ requires_approval: false, status: 'QUEUED' }),
      });
      fetchTasks();
    } catch (error) {
      console.error('Failed to approve task:', error);
    }
  };

  const handleCancel = async (taskId: string) => {
    try {
      await fetch(`${API_BASE_URL}/api/admin/tasks/${taskId}`, {
        method: 'PATCH',
        headers: await authHeaders(),
        body: JSON.stringify({ status: 'FAILED' }),
      });
      fetchTasks();
    } catch (error) {
      console.error('Failed to cancel task:', error);
    }
  };

  const handleRetry = async (taskId: string) => {
    try {
      await fetch(`${API_BASE_URL}/api/admin/tasks/${taskId}`, {
        method: 'PATCH',
        headers: await authHeaders(),
        body: JSON.stringify({ status: 'QUEUED' }),
      });
      fetchTasks();
    } catch (error) {
      console.error('Failed to retry task:', error);
    }
  };

  const handleEditPrompt = async (taskId: string, newPrompt: string) => {
    try {
      await fetch(`${API_BASE_URL}/api/admin/tasks/${taskId}`, {
        method: 'PATCH',
        headers: await authHeaders(),
        body: JSON.stringify({ payloads: [{ prompt: newPrompt }] }),
      });
      fetchTasks();
    } catch (error) {
      console.error('Failed to edit task:', error);
    }
  };

  const handleDismiss = async (taskId: string) => {
    try {
      await fetch(`${API_BASE_URL}/api/admin/tasks/${taskId}`, {
        method: 'PATCH',
        headers: await authHeaders(),
        body: JSON.stringify({ status: 'CANCELLED' }),
      });
      fetchTasks();
    } catch (error) {
      console.error('Failed to dismiss task:', error);
    }
  };

  const fetchSettings = async () => {
    try {
      const res = await authGet(`${API_BASE_URL}/api/admin/settings`);
      if (res.ok) {
        const data = await res.json();
        setMaxConcurrentVMs(data.max_concurrent_vms);
        setMaxRetries(data.max_retries ?? 3);
        setUsingMaxPlan(data.using_max_plan ?? false);
        setMaxPlanAccounts(data.max_plan_accounts || []);
        setGithubAccount(data.github_account || null);
      }
    } catch (err) {
      console.error('Failed to fetch settings:', err);
    }
  };

  const addMaxPlanAccount = async () => {
    if (!newAccountName || !newAccountCreds) return;
    try {
      const res = await fetch(`${API_BASE_URL}/api/admin/max-plan-accounts`, {
        method: 'POST', headers: await authHeaders(),
        body: JSON.stringify({ name: newAccountName, credentials_json: newAccountCreds }),
      });
      if (res.ok) {
        setNewAccountName('');
        setNewAccountCreds('');
        fetchSettings();
      }
    } catch (err) { console.error('Failed to add account:', err); }
  };

  const removeMaxPlanAccount = async (id: string) => {
    try {
      await fetch(`${API_BASE_URL}/api/admin/max-plan-accounts/${id}`, {
        method: 'DELETE', headers: await authHeaders(),
      });
      fetchSettings();
    } catch (err) { console.error('Failed to remove account:', err); }
  };

  const toggleMaxPlanAccount = async (id: string, enabled: boolean) => {
    try {
      await fetch(`${API_BASE_URL}/api/admin/max-plan-accounts/${id}`, {
        method: 'PATCH', headers: await authHeaders(),
        body: JSON.stringify({ enabled }),
      });
      fetchSettings();
    } catch (err) { console.error('Failed to toggle account:', err); }
  };

  const connectGitHub = async () => {
    try {
      const res = await authGet(`${API_BASE_URL}/api/github/oauth/authorize`);
      if (res.ok) {
        const { url } = await res.json();
        window.location.href = url;
      }
    } catch (err) { console.error('Failed to start GitHub OAuth:', err); }
  };

  const updateMaxVMs = async (value: number) => {
    const clamped = Math.max(1, Math.min(30, value));
    setMaxConcurrentVMs(clamped);
    try {
      await fetch(`${API_BASE_URL}/api/admin/settings`, {
        method: 'PATCH',
        headers: await authHeaders(),
        body: JSON.stringify({ max_concurrent_vms: clamped }),
      });
    } catch (err) {
      console.error('Failed to update settings:', err);
    }
  };

  const fetchRepoOptions = async () => {
    setRepoLoading(true);
    setRepoError(null);
    try {
      const res = await authGet(`${API_BASE_URL}/api/admin/github/repos`);
      if (!res.ok) {
        const errData = await res.json().catch(() => ({}));
        setRepoError(errData?.detail || 'Failed to load repositories');
        setUseManualRepo(true);
        return;
      }
      const data = await res.json();
      setRepoOptions(data.repositories || []);
      if (data.repositories?.length === 0) {
        setRepoError('No repositories found for this GitHub installation');
        setUseManualRepo(true);
      }
    } catch {
      setRepoError('Failed to connect to server');
      setUseManualRepo(true);
    } finally {
      setRepoLoading(false);
    }
  };

  const fetchBranchOptions = async (repo: string) => {
    setBranchOptions([]);
    setBranchError(null);
    setUseManualBranch(false);
    if (!repo) return;
    setBranchLoading(true);
    try {
      const res = await authGet(`${API_BASE_URL}/api/admin/github/branches?repo=${encodeURIComponent(repo)}`);
      if (!res.ok) {
        const errData = await res.json().catch(() => ({}));
        setBranchError(errData?.detail || 'Failed to load branches');
        setUseManualBranch(true);
        return;
      }
      const data = await res.json();
      setBranchOptions(data.branches || []);
      if (data.branches?.length === 0) {
        setBranchError('No branches found');
        setUseManualBranch(true);
      }
    } catch {
      setBranchError('Failed to connect to server');
      setUseManualBranch(true);
    } finally {
      setBranchLoading(false);
    }
  };

  const openNewTaskModal = () => {
    setShowNewTaskModal(true);
    setUseManualRepo(false);
    setUseManualBranch(false);
    setBranchOptions([]);
    setBranchError(null);
    fetchRepoOptions();
  };

  const handleRepoChange = (value: string) => {
    setNewTaskRepo(value);
    // Auto-set default branch from repo metadata if available
    const repoMeta = repoOptions.find(r => r.full_name === value);
    if (repoMeta) {
      setNewTaskBranch(repoMeta.default_branch || 'main');
    } else {
      setNewTaskBranch('main');
    }
    // Fetch branches for selected repo
    if (value && value.includes('/')) {
      fetchBranchOptions(value);
    } else {
      setBranchOptions([]);
    }
  };

  const handleCreateTask = async (e: React.FormEvent) => {
    e.preventDefault();
    try {
      const res = await fetch(`${API_BASE_URL}/api/admin/tasks`, {
        method: 'POST',
        headers: await authHeaders(),
        body: JSON.stringify({
          github_repo: newTaskRepo,
          title: newTaskTitle,
          prompt: newTaskPrompt,
          base_branch: newTaskBranch,
        }),
      });
      if (!res.ok) {
        const errData = await res.json();
        alert(`Failed to create task: ${errData?.detail || errData?.error?.message || res.statusText}`);
        return;
      }
      setShowNewTaskModal(false);
      setNewTaskTitle('');
      setNewTaskPrompt('');
      // Keep repo and branch for rapid re-use
      fetchTasks();
    } catch (error) {
      console.error('Failed to create task:', error);
    }
  };

  const handleViewLogs = useCallback((task: Task) => {
    // Close existing WS
    if (wsRef.current) {
      wsRef.current.close();
      wsRef.current = null;
    }
    setLogTask(task);
    setLogLines(task.error_logs ? [`[Error Log]\n${task.error_logs}`] : []);

    // Only connect WebSocket for active tasks
    if (task.status !== 'IN_PROGRESS' && task.status !== 'PROVISIONING') return;
    if (!task.project_id) return;

    // Get WS ticket then connect
    (async () => {
      try {
        const ticketRes = await fetch(`${API_BASE_URL}/api/projects/${task.project_id}/ws-ticket`, {
          method: 'POST',
          headers: await authHeaders(),
        });
        if (!ticketRes.ok) {
          setLogLines(prev => [...prev, '[Failed to get WebSocket ticket]']);
          return;
        }
        const { ticket } = await ticketRes.json();

        const wsUrl = (API_BASE_URL || window.location.origin).replace(/^http/, 'ws');
        const ws = new WebSocket(`${wsUrl}/api/projects/${task.project_id}/ws`);
        wsRef.current = ws;

        ws.onopen = () => {
          ws.send(JSON.stringify({ ticket }));
          setLogLines(prev => [...prev, '[Connected to live log stream]']);
        };

        ws.onmessage = (event) => {
          try {
            const msg = JSON.parse(event.data);
            if (msg.event === 'agent_log' && msg.data?.task_id === task.id) {
              setLogLines(prev => [...prev, msg.data.log]);
            } else if (msg.event === 'task_updated' && msg.data?.task_id === task.id) {
              setLogLines(prev => [...prev, `[Status: ${msg.data.status}]`]);
            }
          } catch {
            // ignore parse errors
          }
        };

        ws.onerror = () => {
          setLogLines(prev => [...prev, '[WebSocket error]']);
        };

        ws.onclose = () => {
          setLogLines(prev => [...prev, '[Stream disconnected]']);
        };
      } catch (err) {
        setLogLines(prev => [...prev, `[Connection error: ${err}]`]);
      }
    })();
  }, []);

  const closeLogViewer = () => {
    if (wsRef.current) {
      wsRef.current.close();
      wsRef.current = null;
    }
    setLogTask(null);
    setLogLines([]);
  };

  const columns: { id: TaskStatus; label: string }[] = [
    { id: 'QUEUED', label: 'Queued' },
    { id: 'IN_PROGRESS', label: 'In Progress' },
    { id: 'FAILED', label: 'Failed' },
    { id: 'COMPLETED', label: 'Completed' },
  ];

  // Auth gate — show login screen if not authenticated
  if (authLoading) {
    return (
      <div className="h-screen flex items-center justify-center bg-neutral-950 text-neutral-400">
        <Activity className="w-5 h-5 animate-spin" />
      </div>
    );
  }

  if (!user) {
    return (
      <div className="h-screen flex items-center justify-center bg-neutral-950">
        <div className="bg-neutral-900 border border-neutral-800 rounded-xl p-8 text-center max-w-sm">
          <h1 className="text-lg font-semibold text-neutral-100 mb-2">Blueprint Admin</h1>
          <p className="text-xs text-neutral-500 mb-6">Sign in with your Google account to continue</p>
          <button
            onClick={() => signInWithGoogle().catch(e => setError(String(e)))}
            className="bg-white hover:bg-neutral-200 text-neutral-950 px-6 py-2 rounded text-sm font-semibold transition-colors w-full"
          >
            Sign in with Google
          </button>
          {error && <p className="text-red-400 text-xs mt-4">{error}</p>}
        </div>
      </div>
    );
  }

  const actionRequiredTasks = tasks.filter(t => t.requires_approval || t.status === 'FAILED');
  const runningTasks = tasks.filter(t => t.status === 'IN_PROGRESS' || t.status === 'PROVISIONING');
  const queuedTasks = tasks.filter(t => t.status === 'QUEUED');
  const oneDayAgo = new Date(Date.now() - 24 * 60 * 60 * 1000);
  const completedRecently = tasks.filter(t => t.status === 'COMPLETED' && new Date(t.created_at) > oneDayAgo);

  return (
    <div className="h-screen overflow-hidden flex flex-col bg-neutral-950 text-neutral-200 font-sans p-6">
      {/* Header */}
      <header className="mb-6 flex justify-between items-center shrink-0">
        <div>
          <h1 className="text-xl font-semibold tracking-tight text-neutral-100 flex items-center gap-2">
            <Activity className="w-5 h-5 text-neutral-400" />
            Blueprint Launcher
          </h1>
          <p className="text-xs text-neutral-500 mt-1 font-mono uppercase tracking-wider">Claude Code PVM Orchestrator</p>
        </div>

        <div className="flex items-center gap-8 px-6 py-2 bg-neutral-900/30 rounded-lg border border-neutral-800/50">
          <div className="flex flex-col items-center">
            <span className="text-[10px] text-neutral-500 font-semibold uppercase tracking-wider mb-1">Active VMs</span>
            <span className="text-xl font-bold text-emerald-400 font-mono leading-none flex items-center gap-1.5">
              {runningTasks.length > 0 && <span className="w-2 h-2 rounded-full bg-emerald-500 animate-pulse"></span>}
              {runningTasks.length}<span className="text-xs text-neutral-600 font-sans">/{maxConcurrentVMs}</span>
            </span>
          </div>
          <div className="w-px h-8 bg-neutral-800"></div>
          <div className="flex flex-col items-center">
            <span className="text-[10px] text-neutral-500 font-semibold uppercase tracking-wider mb-1">Est. Cost</span>
            <span className="text-xl font-bold text-neutral-200 font-mono leading-none">
              ${(runningTasks.length * (usingMaxPlan ? 0.07 : 1.07)).toFixed(2)}<span className="text-xs text-neutral-500 font-sans">/hr</span>
            </span>
            <span className="text-[9px] text-neutral-600 mt-0.5">{usingMaxPlan ? 'VM only (Max plan)' : 'VM + API tokens'}</span>
          </div>
          <div className="w-px h-8 bg-neutral-800"></div>
          <div className="flex flex-col items-center">
            <span className="text-[10px] text-neutral-500 font-semibold uppercase tracking-wider mb-1">Queue</span>
            <span className="text-xl font-bold text-neutral-200 font-mono leading-none">{queuedTasks.length}</span>
          </div>
        </div>

        <div className="flex items-center gap-2">
          <div className="relative" ref={settingsRef}>
            <button
              onClick={() => { setShowSettings(!showSettings); if (!showSettings) fetchSettings(); }}
              className="bg-neutral-800 hover:bg-neutral-700 text-neutral-300 p-2 rounded text-xs transition-colors border border-neutral-700"
              title="Settings"
            >
              <Settings className="w-4 h-4" />
            </button>
            {showSettings && (
              <div className="absolute right-0 top-full mt-2 w-80 bg-neutral-900 border border-neutral-700 rounded-lg shadow-2xl p-4 z-50 max-h-[70vh] overflow-y-auto custom-scrollbar">
                <h3 className="text-[11px] font-semibold text-neutral-400 uppercase tracking-wider mb-3">Settings</h3>
                <div className="mb-4">
                  <label className="block text-[11px] font-medium text-neutral-400 mb-1.5">Max Concurrent VMs (1-30)</label>
                  <input
                    type="number"
                    min={1}
                    max={30}
                    value={maxConcurrentVMs}
                    onChange={e => updateMaxVMs(parseInt(e.target.value) || 1)}
                    className="w-full bg-neutral-950 border border-neutral-700 text-neutral-100 rounded px-3 py-1.5 text-sm font-mono focus:outline-none focus:ring-1 focus:ring-neutral-600"
                  />
                </div>
                <div className="mb-4">
                  <label className="block text-[11px] font-medium text-neutral-400 mb-1.5">Max Retries (0-10)</label>
                  <input
                    type="number"
                    min={0}
                    max={10}
                    value={maxRetries}
                    onChange={e => {
                      const v = Math.max(0, Math.min(10, parseInt(e.target.value) || 0));
                      setMaxRetries(v);
                      authHeaders().then(h => fetch(`${API_BASE_URL}/api/admin/settings`, {
                        method: 'PATCH', headers: h,
                        body: JSON.stringify({ max_retries: v }),
                      })).catch(() => {});
                    }}
                    className="w-full bg-neutral-950 border border-neutral-700 text-neutral-100 rounded px-3 py-1.5 text-sm font-mono focus:outline-none focus:ring-1 focus:ring-neutral-600"
                  />
                </div>

                <div className="border-t border-neutral-800 pt-3 mb-4">
                  <h4 className="text-[11px] font-semibold text-neutral-400 uppercase tracking-wider mb-2">GitHub</h4>
                  {githubAccount ? (
                    <div className="flex items-center gap-2 bg-neutral-950 rounded p-2 border border-neutral-800">
                      <span className="w-2 h-2 rounded-full bg-emerald-500 shrink-0" />
                      <span className="text-xs text-neutral-200">Connected: <span className="font-mono">{githubAccount}</span></span>
                    </div>
                  ) : (
                    <button onClick={connectGitHub}
                      className="w-full bg-neutral-800 hover:bg-neutral-700 text-neutral-300 text-xs py-1.5 rounded transition-colors border border-neutral-700 flex items-center justify-center gap-1.5">
                      <GitBranch className="w-3 h-3" /> Connect GitHub
                    </button>
                  )}
                </div>

                <div className="border-t border-neutral-800 pt-3">
                  <h4 className="text-[11px] font-semibold text-neutral-400 uppercase tracking-wider mb-2">Max Plan Accounts</h4>
                  {maxPlanAccounts.length === 0 && (
                    <p className="text-[11px] text-neutral-600 mb-2">No accounts — using API key fallback</p>
                  )}
                  {maxPlanAccounts.map(a => (
                    <div key={a.id} className="flex items-center gap-2 mb-2 bg-neutral-950 rounded p-2 border border-neutral-800">
                      <button onClick={() => toggleMaxPlanAccount(a.id, !a.enabled)}
                        className={`w-2 h-2 rounded-full shrink-0 ${a.enabled ? 'bg-emerald-500' : 'bg-neutral-600'}`}
                        title={a.enabled ? 'Enabled (click to disable)' : 'Disabled (click to enable)'} />
                      <div className="flex-1 min-w-0">
                        <div className="text-xs text-neutral-200 truncate">{a.name}</div>
                        <div className="text-[10px] text-neutral-500 font-mono">{a.active_tasks} active / {a.total_tasks} total</div>
                      </div>
                      <button onClick={() => removeMaxPlanAccount(a.id)}
                        className="text-neutral-600 hover:text-red-400 text-xs transition-colors shrink-0">&times;</button>
                    </div>
                  ))}
                  <div className="mt-2 space-y-1.5">
                    <input value={newAccountName} onChange={e => setNewAccountName(e.target.value)}
                      placeholder="Account name" className="w-full bg-neutral-950 border border-neutral-700 text-neutral-100 rounded px-2 py-1 text-xs focus:outline-none focus:ring-1 focus:ring-neutral-600" />
                    <textarea value={newAccountCreds} onChange={e => setNewAccountCreds(e.target.value)}
                      placeholder="Paste credentials.json contents" rows={2}
                      className="w-full bg-neutral-950 border border-neutral-700 text-neutral-100 rounded px-2 py-1 text-xs resize-none focus:outline-none focus:ring-1 focus:ring-neutral-600 font-mono" />
                    <button onClick={addMaxPlanAccount} disabled={!newAccountName || !newAccountCreds}
                      className="w-full bg-neutral-800 hover:bg-neutral-700 disabled:opacity-30 text-neutral-300 text-xs py-1 rounded transition-colors border border-neutral-700">
                      Add Account
                    </button>
                  </div>
                </div>
              </div>
            )}
          </div>
          <button
            onClick={openNewTaskModal}
            className="bg-white hover:bg-neutral-200 text-neutral-950 px-4 py-2 rounded text-xs font-semibold transition-colors shadow-sm flex items-center gap-2 h-fit"
          >
            <Plus className="w-3.5 h-3.5" />
            Launch Task
          </button>
          <button
            onClick={() => signOut(auth)}
            className="bg-neutral-800 hover:bg-neutral-700 text-neutral-400 p-2 rounded text-xs transition-colors border border-neutral-700"
            title={`Sign out (${user.email})`}
          >
            <LogOut className="w-4 h-4" />
          </button>
        </div>
      </header>

      {/* Summary Panels */}
      <div className="grid grid-cols-3 gap-4 mb-6 shrink-0">
        <div className="bg-neutral-900/50 border border-neutral-800/50 rounded-xl p-4 flex flex-col gap-3 h-[120px]">
          <div className="flex items-center justify-between">
            <h3 className="text-[11px] font-bold text-neutral-400 uppercase tracking-widest flex items-center gap-2">
              <AlertTriangle className="w-3.5 h-3.5 text-amber-500" /> Action Required
            </h3>
            <span className="text-[10px] font-medium bg-neutral-800 px-2 py-0.5 rounded-full border border-neutral-700">{actionRequiredTasks.length}</span>
          </div>
          <div className="flex-1 flex flex-col gap-2 overflow-y-auto custom-scrollbar">
            {actionRequiredTasks.map(t => (
              <div key={t.id} className="text-xs bg-neutral-950 border border-neutral-800/80 px-2 py-1.5 rounded flex justify-between items-center">
                <span className="truncate pr-2 text-neutral-300">{t.title}</span>
                <span className={`shrink-0 text-[9px] uppercase tracking-wider font-semibold px-1.5 py-0.5 rounded ${t.requires_approval ? 'bg-amber-500/10 text-amber-500' : 'bg-red-500/10 text-red-500'}`}>
                  {t.requires_approval ? 'Approval' : 'Failed'}
                </span>
              </div>
            ))}
            {actionRequiredTasks.length === 0 && <div className="text-xs text-neutral-600 m-auto flex items-center gap-1.5"><CheckCircle className="w-3.5 h-3.5" /> All clear</div>}
          </div>
        </div>

        <div className="bg-neutral-900/50 border border-neutral-800/50 rounded-xl p-4 flex flex-col gap-3 h-[120px]">
          <div className="flex items-center justify-between">
            <h3 className="text-[11px] font-bold text-neutral-400 uppercase tracking-widest flex items-center gap-2">
              <Activity className="w-3.5 h-3.5 text-blue-500" /> Active & Queued
            </h3>
            <span className="text-[10px] font-medium bg-neutral-800 px-2 py-0.5 rounded-full border border-neutral-700">{runningTasks.length} active / {queuedTasks.length} queued</span>
          </div>
          <div className="flex-1 flex flex-col gap-2 overflow-y-auto custom-scrollbar">
            {runningTasks.map(t => (
              <div key={t.id} className="text-xs bg-neutral-950 border border-neutral-800/80 px-2 py-1.5 rounded flex justify-between items-center">
                <span className="truncate pr-2 text-neutral-300">{t.title}</span>
                <span className="shrink-0 text-[9px] uppercase tracking-wider font-semibold px-1.5 py-0.5 rounded bg-blue-500/10 text-blue-500">
                  {t.status === 'PROVISIONING' ? 'Prov' : 'Running'}
                </span>
              </div>
            ))}
            {runningTasks.length === 0 && <div className="text-xs text-neutral-600 m-auto">No active tasks</div>}
          </div>
        </div>

        <div className="bg-neutral-900/50 border border-neutral-800/50 rounded-xl p-4 flex flex-col gap-3 h-[120px]">
          <div className="flex items-center justify-between">
            <h3 className="text-[11px] font-bold text-neutral-400 uppercase tracking-widest flex items-center gap-2">
              <CheckCircle className="w-3.5 h-3.5 text-emerald-500" /> Completed (24h)
            </h3>
            <span className="text-[10px] font-medium bg-neutral-800 px-2 py-0.5 rounded-full border border-neutral-700">{completedRecently.length}</span>
          </div>
          <div className="flex-1 flex flex-col gap-2 overflow-y-auto custom-scrollbar">
            {completedRecently.map(t => (
              <div key={t.id} className="text-xs bg-neutral-950 border border-neutral-800/80 px-2 py-1.5 rounded flex justify-between items-center">
                <span className="truncate pr-2 text-neutral-400">{t.title}</span>
                {t.pr_url ? (
                  <a href={t.pr_url} target="_blank" rel="noopener noreferrer" className="shrink-0 text-[9px] text-emerald-400 hover:text-emerald-300 font-medium">PR</a>
                ) : (
                  <span className="shrink-0 text-[10px] text-neutral-500 font-mono">
                    {new Date(t.created_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}
                  </span>
                )}
              </div>
            ))}
            {completedRecently.length === 0 && <div className="text-xs text-neutral-600 m-auto">No recent completions</div>}
          </div>
        </div>
      </div>

      {error && (
        <div className="mb-6 bg-red-950/50 text-red-400 px-4 py-3 rounded-lg text-sm border border-red-900/50 flex items-center gap-2 shrink-0">
          <AlertCircle className="w-4 h-4" />
          <p>{error}</p>
        </div>
      )}

      {/* Kanban Board */}
      <main className="flex gap-4 overflow-x-auto overflow-y-hidden pb-4 flex-1 custom-scrollbar">
        {columns.map(col => (
          <div key={col.id} className="flex-1 min-w-[300px] flex flex-col bg-neutral-900/50 rounded-xl border border-neutral-800/50 p-3">
            <div className="flex items-center justify-between mb-3 px-1">
              <h2 className="text-[11px] font-bold text-neutral-400 uppercase tracking-widest">{col.label}</h2>
              <span className="text-[10px] bg-neutral-800 text-neutral-300 px-2 py-0.5 rounded-full font-medium border border-neutral-700">
                {tasks.filter(t => t.status === col.id).length}
              </span>
            </div>

            <div className="flex-1 overflow-y-auto space-y-3 pr-1 custom-scrollbar">
              {/* Also show PROVISIONING tasks in the IN_PROGRESS column */}
              {tasks
                .filter(t => t.status === col.id || (col.id === 'IN_PROGRESS' && t.status === 'PROVISIONING'))
                .map(task => (
                  <TaskCard
                    key={task.id}
                    task={task}
                    onApprove={handleApprove}
                    onCancel={handleCancel}
                    onRetry={handleRetry}
                    onEditPrompt={handleEditPrompt}
                    onDismiss={handleDismiss}
                    onViewLogs={handleViewLogs}
                  />
                ))}
              {tasks.filter(t => t.status === col.id || (col.id === 'IN_PROGRESS' && t.status === 'PROVISIONING')).length === 0 && !loading && (
                <div className="text-center text-neutral-600 text-xs py-8 border border-dashed border-neutral-800 rounded-xl bg-neutral-900/20">
                  No tasks
                </div>
              )}
            </div>
          </div>
        ))}

      </main>

      {/* Pagination — only show when needed */}
      {(page > 1 || tasks.length >= limit) && <div className="flex items-center justify-between px-6 py-2 border-t border-neutral-800 shrink-0">
        <div className="text-xs text-neutral-500">
          {tasks.length > 0
            ? `Showing ${(page - 1) * limit + 1}–${(page - 1) * limit + tasks.length}`
            : 'No tasks'}
        </div>
        <div className="flex gap-2">
          <button
            onClick={() => setPage(p => Math.max(1, p - 1))}
            disabled={page === 1}
            className="px-3 py-1 text-xs font-medium rounded-md bg-neutral-800 text-neutral-300 hover:bg-neutral-700 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
          >
            Previous
          </button>
          <button
            onClick={() => setPage(p => p + 1)}
            disabled={tasks.length < limit}
            className="px-3 py-1 text-xs font-medium rounded-md bg-neutral-800 text-neutral-300 hover:bg-neutral-700 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
          >
            Next
          </button>
        </div>
      </div>}

      {/* New Task Modal */}
      {showNewTaskModal && (
        <div className="fixed inset-0 bg-black/60 backdrop-blur-sm flex items-center justify-center p-4 z-50">
          <div className="bg-neutral-900 rounded-xl shadow-2xl w-full max-w-lg overflow-hidden border border-neutral-800">
            <div className="flex justify-between items-center p-5 border-b border-neutral-800">
              <h2 className="text-sm font-semibold text-neutral-100 flex items-center gap-2">
                <GitBranch className="w-4 h-4 text-neutral-400" />
                Launch Claude Code Task
              </h2>
              <button onClick={() => setShowNewTaskModal(false)} className="text-neutral-500 hover:text-neutral-300 transition-colors">
                <X className="w-4 h-4" />
              </button>
            </div>
            <form onSubmit={handleCreateTask} className="p-5">
              <div className="grid grid-cols-2 gap-4 mb-4">
                <div>
                  <div className="flex items-center justify-between mb-2">
                    <label className="block text-[11px] font-semibold text-neutral-400 uppercase tracking-wider">Repository</label>
                    {!repoLoading && (
                      <button
                        type="button"
                        onClick={() => { setUseManualRepo(m => !m); if (!useManualRepo) { setNewTaskRepo(''); setBranchOptions([]); } }}
                        className="text-[10px] text-neutral-500 hover:text-neutral-300 transition-colors"
                      >
                        {useManualRepo ? 'Use dropdown' : 'Type manually'}
                      </button>
                    )}
                  </div>
                  {repoLoading ? (
                    <div className="w-full bg-neutral-950 border border-neutral-800 rounded-lg px-3 py-2 text-sm text-neutral-500 font-mono">
                      Loading repos...
                    </div>
                  ) : useManualRepo ? (
                    <>
                      <input
                        type="text"
                        required
                        value={newTaskRepo}
                        onChange={e => handleRepoChange(e.target.value)}
                        className="w-full bg-neutral-950 border border-neutral-800 text-neutral-100 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-neutral-700 focus:border-neutral-600 transition-all placeholder:text-neutral-600 font-mono"
                        placeholder="owner/repo"
                      />
                      {repoError && <p className="text-[10px] text-amber-500 mt-1">{repoError}</p>}
                    </>
                  ) : (
                    <select
                      required
                      value={newTaskRepo}
                      onChange={e => handleRepoChange(e.target.value)}
                      className="w-full bg-neutral-950 border border-neutral-800 text-neutral-100 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-neutral-700 focus:border-neutral-600 transition-all font-mono appearance-none cursor-pointer"
                    >
                      <option value="" disabled>Select a repository</option>
                      {repoOptions.map(r => (
                        <option key={r.full_name} value={r.full_name}>{r.full_name}</option>
                      ))}
                    </select>
                  )}
                </div>
                <div>
                  <div className="flex items-center justify-between mb-2">
                    <label className="block text-[11px] font-semibold text-neutral-400 uppercase tracking-wider">Base Branch</label>
                    {!branchLoading && branchOptions.length > 0 && (
                      <button
                        type="button"
                        onClick={() => setUseManualBranch(m => !m)}
                        className="text-[10px] text-neutral-500 hover:text-neutral-300 transition-colors"
                      >
                        {useManualBranch ? 'Use dropdown' : 'Type manually'}
                      </button>
                    )}
                  </div>
                  {branchLoading ? (
                    <div className="w-full bg-neutral-950 border border-neutral-800 rounded-lg px-3 py-2 text-sm text-neutral-500 font-mono">
                      Loading branches...
                    </div>
                  ) : useManualBranch || branchOptions.length === 0 ? (
                    <>
                      <input
                        type="text"
                        value={newTaskBranch}
                        onChange={e => setNewTaskBranch(e.target.value)}
                        className="w-full bg-neutral-950 border border-neutral-800 text-neutral-100 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-neutral-700 focus:border-neutral-600 transition-all placeholder:text-neutral-600 font-mono"
                        placeholder="main"
                      />
                      {branchError && <p className="text-[10px] text-amber-500 mt-1">{branchError}</p>}
                    </>
                  ) : (
                    <select
                      value={newTaskBranch}
                      onChange={e => setNewTaskBranch(e.target.value)}
                      className="w-full bg-neutral-950 border border-neutral-800 text-neutral-100 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-neutral-700 focus:border-neutral-600 transition-all font-mono appearance-none cursor-pointer"
                    >
                      {branchOptions.map(b => (
                        <option key={b} value={b}>{b}</option>
                      ))}
                    </select>
                  )}
                </div>
              </div>
              <div className="mb-4">
                <label className="block text-[11px] font-semibold text-neutral-400 uppercase tracking-wider mb-2">Task Title</label>
                <input
                  type="text"
                  required
                  value={newTaskTitle}
                  onChange={e => setNewTaskTitle(e.target.value)}
                  className="w-full bg-neutral-950 border border-neutral-800 text-neutral-100 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-neutral-700 focus:border-neutral-600 transition-all placeholder:text-neutral-600"
                  placeholder="e.g., Add user authentication"
                />
              </div>
              <div className="mb-6">
                <label className="block text-[11px] font-semibold text-neutral-400 uppercase tracking-wider mb-2">Prompt / Instructions</label>
                <textarea
                  required
                  value={newTaskPrompt}
                  onChange={e => setNewTaskPrompt(e.target.value)}
                  className="w-full bg-neutral-950 border border-neutral-800 text-neutral-100 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-neutral-700 focus:border-neutral-600 h-40 resize-none transition-all placeholder:text-neutral-600"
                  placeholder="Describe what Claude Code should do in this repository. Be specific about files, patterns, and expected outcomes..."
                />
              </div>
              <div className="flex justify-end gap-2">
                <button
                  type="button"
                  onClick={() => setShowNewTaskModal(false)}
                  className="px-4 py-1.5 text-xs text-neutral-400 font-medium hover:bg-neutral-800 rounded transition-colors"
                >
                  Cancel
                </button>
                <button
                  type="submit"
                  className="px-4 py-1.5 bg-white text-neutral-950 text-xs font-semibold hover:bg-neutral-200 rounded transition-colors"
                >
                  Launch
                </button>
              </div>
            </form>
          </div>
        </div>
      )}

      {/* Log Viewer Slide-out */}
      {logTask && (
        <div className="fixed inset-0 z-50 flex">
          <div className="flex-1 bg-black/40" onClick={closeLogViewer} />
          <div className="w-[600px] bg-neutral-950 border-l border-neutral-800 flex flex-col shadow-2xl">
            <div className="flex justify-between items-center p-4 border-b border-neutral-800 shrink-0">
              <div className="overflow-hidden">
                <h3 className="text-sm font-semibold text-neutral-100 flex items-center gap-2 truncate">
                  <Terminal className="w-4 h-4 text-neutral-400 shrink-0" />
                  {logTask.title}
                </h3>
                <p className="text-[10px] text-neutral-500 font-mono mt-1 truncate">{logTask.id}</p>
              </div>
              <button onClick={closeLogViewer} className="text-neutral-500 hover:text-neutral-300 transition-colors shrink-0 ml-2">
                <X className="w-4 h-4" />
              </button>
            </div>
            <div className="flex-1 overflow-y-auto p-4 font-mono text-xs text-neutral-300 leading-relaxed custom-scrollbar">
              {logLines.length === 0 && (
                <div className="text-neutral-600 text-center py-8">
                  {logTask.status === 'IN_PROGRESS' || logTask.status === 'PROVISIONING'
                    ? 'Waiting for logs...'
                    : 'No logs available for this task.'}
                </div>
              )}
              {logLines.map((line, i) => (
                <div key={i} className="whitespace-pre-wrap break-all mb-0.5">
                  {line.startsWith('[error]') || line.startsWith('[stderr]')
                    ? <span className="text-red-400">{line}</span>
                    : line.startsWith('[Status:')
                      ? <span className="text-blue-400">{line}</span>
                      : line.startsWith('[')
                        ? <span className="text-neutral-500">{line}</span>
                        : line}
                </div>
              ))}
              <div ref={logEndRef} />
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
