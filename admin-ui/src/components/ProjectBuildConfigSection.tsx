import { useState, useEffect } from 'react';
import { Plus, Trash2, RefreshCw } from 'lucide-react';
import type { BuildConfig, BuildConfigDirectory } from '../types/project';

interface ProjectBuildConfigSectionProps {
  githubOwner: string | null;
  githubRepo: string | null;
  buildConfig: BuildConfig | null;
  onSave: (config: BuildConfig) => Promise<void>;
  onDetect: () => Promise<void>;
  detectAvailable: boolean;
}

const EMPTY_DIR: BuildConfigDirectory = {
  path: '',
  type: '',
  lint_cmd: '',
  test_cmd: '',
  build_cmd: '',
};

function newDir(): BuildConfigDirectory {
  return { ...EMPTY_DIR };
}

export function ProjectBuildConfigSection({
  githubOwner,
  githubRepo,
  buildConfig,
  onSave,
  onDetect,
  detectAvailable,
}: ProjectBuildConfigSectionProps) {
  const hasConfig = buildConfig && (
    (buildConfig.directories?.length ?? 0) > 0 ||
    buildConfig.git_user_email ||
    buildConfig.git_user_name
  );

  const [dirs, setDirs] = useState<BuildConfigDirectory[]>(() =>
    buildConfig?.directories?.length ? buildConfig.directories : []
  );
  const [gitEmail, setGitEmail] = useState(
    buildConfig?.git_user_email ?? 'blueprint-agent@automated.dev'
  );
  const [gitName, setGitName] = useState(
    buildConfig?.git_user_name ?? 'Blueprint Agent'
  );
  const [enableFreshReviewer, setEnableFreshReviewer] = useState(
    buildConfig?.enable_fresh_reviewer ?? false
  );
  const [saving, setSaving] = useState(false);
  const [detecting, setDetecting] = useState(false);

  // Sync state when buildConfig prop changes (e.g. after detect or project switch)
  useEffect(() => {
    setDirs(buildConfig?.directories?.length ? buildConfig.directories : []);
    setGitEmail(buildConfig?.git_user_email ?? 'blueprint-agent@automated.dev');
    setGitName(buildConfig?.git_user_name ?? 'Blueprint Agent');
    setEnableFreshReviewer(buildConfig?.enable_fresh_reviewer ?? false);
  }, [buildConfig]);

  const handleSave = async () => {
    setSaving(true);
    try {
      await onSave({
        ...buildConfig,
        directories: dirs,
        git_user_email: gitEmail,
        git_user_name: gitName,
        enable_fresh_reviewer: enableFreshReviewer,
      });
    } finally {
      setSaving(false);
    }
  };

  const handleDetect = async () => {
    setDetecting(true);
    try {
      await onDetect();
    } finally {
      setDetecting(false);
    }
  };

  const updateDir = (index: number, field: keyof BuildConfigDirectory, value: string) => {
    setDirs(prev => prev.map((d, i) => i === index ? { ...d, [field]: value } : d));
  };

  const removeDir = (index: number) => {
    setDirs(prev => prev.filter((_, i) => i !== index));
  };

  const addDir = () => {
    setDirs(prev => [...prev, newDir()]);
  };

  return (
    <div className="space-y-6">
      {/* Repository info */}
      <div>
        <h3 className="text-xs font-semibold text-neutral-300 uppercase tracking-wider mb-3">Repository</h3>
        <div className="bg-neutral-900 border border-neutral-800 rounded-lg px-4 py-3 flex items-center gap-2">
          <span className="text-xs text-neutral-400">Repository</span>
          <span className="text-xs text-neutral-100 font-mono ml-auto">
            {githubOwner && githubRepo ? `${githubOwner}/${githubRepo}` : '—'}
          </span>
        </div>
        {buildConfig?.language ? (
          <div className="mt-2 flex flex-wrap gap-1.5">
            <span className="text-[11px] bg-neutral-800 border border-neutral-700 text-neutral-300 px-2 py-0.5 rounded-full">
              {buildConfig.language}
            </span>
          </div>
        ) : null}
      </div>

      {/* Empty state / detect prompt */}
      {!hasConfig && (
        <div className="bg-neutral-900 border border-neutral-800 rounded-lg px-5 py-6 text-center space-y-3">
          <p className="text-xs text-neutral-400">
            No build configuration detected yet. Run auto-detection to populate lint, test, and build commands from the repository structure.
          </p>
          {detectAvailable ? (
            <button
              onClick={handleDetect}
              disabled={detecting}
              className="inline-flex items-center gap-2 bg-neutral-800 hover:bg-neutral-700 text-neutral-200 rounded-lg px-4 py-2 text-xs transition-colors disabled:opacity-50"
            >
              {detecting ? (
                <RefreshCw className="w-3.5 h-3.5 animate-spin" />
              ) : (
                <RefreshCw className="w-3.5 h-3.5" />
              )}
              Detect Build Config
            </button>
          ) : (
            <button
              onClick={addDir}
              className="inline-flex items-center gap-2 bg-neutral-800 hover:bg-neutral-700 text-neutral-200 rounded-lg px-4 py-2 text-xs transition-colors"
            >
              <Plus className="w-3.5 h-3.5" />
              Add manually
            </button>
          )}
        </div>
      )}

      {/* Directories table */}
      {hasConfig && (
        <div>
          <div className="flex items-center justify-between mb-2">
            <h3 className="text-xs font-semibold text-neutral-300 uppercase tracking-wider">Directories</h3>
            <div className="flex items-center gap-2">
              {detectAvailable && (
                <button
                  onClick={handleDetect}
                  disabled={detecting}
                  title="Re-detect from repository"
                  className="text-neutral-500 hover:text-neutral-300 transition-colors disabled:opacity-50"
                >
                  <RefreshCw className={`w-3.5 h-3.5 ${detecting ? 'animate-spin' : ''}`} />
                </button>
              )}
            </div>
          </div>
          <p className="text-[11px] text-neutral-600 mb-3">
            Configure lint, test, and build commands per directory. The agent runs these in CI validation.
          </p>
          <div className="space-y-3">
            {dirs.map((dir, i) => (
              <div key={i} className="bg-neutral-900 border border-neutral-800 rounded-lg p-3 space-y-2">
                <div className="flex items-center gap-2 mb-1">
                  <div className="flex-1">
                    <label className="block text-[10px] text-neutral-500 mb-0.5 uppercase">Path</label>
                    <input
                      type="text"
                      value={dir.path}
                      onChange={e => updateDir(i, 'path', e.target.value)}
                      placeholder="backend/"
                      className="w-full bg-neutral-950 border border-neutral-700 text-neutral-100 rounded px-2 py-1 text-xs font-mono focus:outline-none focus:ring-1 focus:ring-neutral-600"
                    />
                  </div>
                  <div className="flex-1">
                    <label className="block text-[10px] text-neutral-500 mb-0.5 uppercase">Type</label>
                    <input
                      type="text"
                      value={dir.type}
                      onChange={e => updateDir(i, 'type', e.target.value)}
                      placeholder="python"
                      className="w-full bg-neutral-950 border border-neutral-700 text-neutral-100 rounded px-2 py-1 text-xs font-mono focus:outline-none focus:ring-1 focus:ring-neutral-600"
                    />
                  </div>
                  <button
                    onClick={() => removeDir(i)}
                    className="mt-4 text-neutral-600 hover:text-red-400 transition-colors shrink-0"
                    title="Remove directory"
                  >
                    <Trash2 className="w-3.5 h-3.5" />
                  </button>
                </div>
                <div className="grid grid-cols-3 gap-2">
                  <div>
                    <label className="block text-[10px] text-neutral-500 mb-0.5 uppercase">Lint</label>
                    <input
                      type="text"
                      value={dir.lint_cmd ?? ''}
                      onChange={e => updateDir(i, 'lint_cmd', e.target.value)}
                      placeholder="ruff check ."
                      className="w-full bg-neutral-950 border border-neutral-700 text-neutral-100 rounded px-2 py-1 text-xs font-mono focus:outline-none focus:ring-1 focus:ring-neutral-600"
                    />
                  </div>
                  <div>
                    <label className="block text-[10px] text-neutral-500 mb-0.5 uppercase">Test</label>
                    <input
                      type="text"
                      value={dir.test_cmd ?? ''}
                      onChange={e => updateDir(i, 'test_cmd', e.target.value)}
                      placeholder="pytest"
                      className="w-full bg-neutral-950 border border-neutral-700 text-neutral-100 rounded px-2 py-1 text-xs font-mono focus:outline-none focus:ring-1 focus:ring-neutral-600"
                    />
                  </div>
                  <div>
                    <label className="block text-[10px] text-neutral-500 mb-0.5 uppercase">Build</label>
                    <input
                      type="text"
                      value={dir.build_cmd ?? ''}
                      onChange={e => updateDir(i, 'build_cmd', e.target.value)}
                      placeholder="npm run build"
                      className="w-full bg-neutral-950 border border-neutral-700 text-neutral-100 rounded px-2 py-1 text-xs font-mono focus:outline-none focus:ring-1 focus:ring-neutral-600"
                    />
                  </div>
                </div>
              </div>
            ))}
          </div>
          <button
            onClick={addDir}
            className="mt-3 flex items-center gap-1.5 text-xs text-neutral-500 hover:text-neutral-300 transition-colors"
          >
            <Plus className="w-3.5 h-3.5" />
            Add directory
          </button>
        </div>
      )}

      {/* Git identity */}
      {hasConfig && (
        <div>
          <h3 className="text-xs font-semibold text-neutral-300 uppercase tracking-wider mb-3">Git Identity</h3>
          <p className="text-[11px] text-neutral-600 mb-3">
            Used for commits authored by the Blueprint agent.
          </p>
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="block text-xs text-neutral-400 mb-1">Commit Email</label>
              <input
                type="email"
                value={gitEmail}
                onChange={e => setGitEmail(e.target.value)}
                placeholder="blueprint-agent@automated.dev"
                className="w-full bg-neutral-950 border border-neutral-700 text-neutral-100 rounded-lg px-3 py-2 text-xs font-mono focus:outline-none focus:ring-1 focus:ring-neutral-600"
              />
            </div>
            <div>
              <label className="block text-xs text-neutral-400 mb-1">Commit Name</label>
              <input
                type="text"
                value={gitName}
                onChange={e => setGitName(e.target.value)}
                placeholder="Blueprint Agent"
                className="w-full bg-neutral-950 border border-neutral-700 text-neutral-100 rounded-lg px-3 py-2 text-xs font-mono focus:outline-none focus:ring-1 focus:ring-neutral-600"
              />
            </div>
          </div>
        </div>
      )}

      {/* Agent Behavior */}
      {hasConfig && (
        <div>
          <h3 className="text-xs font-semibold text-neutral-300 uppercase tracking-wider mb-3">Agent Behavior</h3>
          <label className="flex items-start gap-3 cursor-pointer group">
            <div className="mt-0.5 shrink-0">
              <input
                type="checkbox"
                checked={enableFreshReviewer}
                onChange={e => setEnableFreshReviewer(e.target.checked)}
                className="sr-only peer"
                id="fresh-reviewer-toggle"
              />
              <div
                onClick={() => setEnableFreshReviewer(v => !v)}
                className={`w-8 h-4 rounded-full transition-colors cursor-pointer ${enableFreshReviewer ? 'bg-blue-600' : 'bg-neutral-700'} relative`}
              >
                <div className={`absolute top-0.5 left-0.5 w-3 h-3 rounded-full bg-white transition-transform ${enableFreshReviewer ? 'translate-x-4' : 'translate-x-0'}`} />
              </div>
            </div>
            <div>
              <span className="text-xs text-neutral-200 font-medium">Fresh Reviewer</span>
              <p className="text-[11px] text-neutral-500 mt-0.5">
                Second-pass critique by an independent Claude session before <code className="font-mono text-neutral-400">./done</code>.
                Catches issues the primary agent may miss. Adds a small additional cost per code-change task.
              </p>
            </div>
          </label>
        </div>
      )}

      {/* Save button */}
      {hasConfig && (
        <div className="flex justify-end pt-2">
          <button
            onClick={handleSave}
            disabled={saving}
            className="bg-neutral-700 hover:bg-neutral-600 text-neutral-100 rounded-lg px-5 py-2 text-xs font-medium transition-colors disabled:opacity-50"
          >
            {saving ? 'Saving...' : 'Save Build Config'}
          </button>
        </div>
      )}
    </div>
  );
}
