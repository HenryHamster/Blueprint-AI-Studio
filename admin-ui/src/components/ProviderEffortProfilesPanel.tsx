import { useState } from 'react';
import { Plus, X, HelpCircle } from 'lucide-react';
import type { ProviderName } from '../types/settings';

// Must match backend payload_schemas.py::TaskType minus `investigate` (which
// is orphaned — no spawner wires it).
const ALL_TASK_TYPES = [
  'create', 'review', 'fix', 'plan', 'research',
  'strategic_review', 'design_doc_update', 'security_audit',
  'qa', 'deps', 'docs', 'migrate', 'smoketest',
  'refactor', 'debug', 'perf',
] as const;

// Keep this in sync with backend providers/*.py settings_schema. These are
// the KNOBS that actually vary by effort level — everything else stays at
// provider_settings_defaults. The effort concept is opinionated: one effort
// level → one canonical bundle of per-provider settings.
const ANTHROPIC_EFFORT_VALUES = ['low', 'medium', 'high', 'xhigh', 'max'] as const;
const OPENAI_REASONING_VALUES = ['minimal', 'low', 'medium', 'high'] as const;
const OPENAI_VERBOSITY_VALUES = ['low', 'medium', 'high'] as const;

type AnthropicEffort = typeof ANTHROPIC_EFFORT_VALUES[number];
type OpenAIReasoning = typeof OPENAI_REASONING_VALUES[number];
type OpenAIVerbosity = typeof OPENAI_VERBOSITY_VALUES[number];

interface AnthropicProfileBag { effort?: AnthropicEffort }
interface OpenAIProfileBag { reasoning_effort?: OpenAIReasoning; verbosity?: OpenAIVerbosity }
interface GeminiProfileBag { thinking_budget?: number }

// ProfileBag is the on-wire shape: an arbitrary settings bag whose keys are
// validated by the backend. We narrow it to specific effort fields inside
// each card via type assertion rather than tightening the map's type — that
// way the whole state pipeline (useSettings → useAppState → props) can
// speak the same generic Record<string, unknown> dialect.
type ProfileBag = Record<string, unknown>;
type ProfilesMap = Record<string, Partial<Record<ProviderName, ProfileBag>>>;
type TaskMappingMap = Record<string, string>;

interface Props {
  effortProfiles: ProfilesMap;
  onUpdateEffortProfiles: (value: ProfilesMap) => void | Promise<void>;
  taskTypeEffortProfile: TaskMappingMap;
  onUpdateTaskTypeEffortProfile: (value: TaskMappingMap) => void | Promise<void>;
}

export function ProviderEffortProfilesPanel({
  effortProfiles,
  onUpdateEffortProfiles,
  taskTypeEffortProfile,
  onUpdateTaskTypeEffortProfile,
}: Props) {
  const [newProfileName, setNewProfileName] = useState('');

  const profileNames = Object.keys(effortProfiles).filter(n => n !== '_default');

  const omit = <T,>(obj: Record<string, T>, key: string): Record<string, T> =>
    Object.fromEntries(Object.entries(obj).filter(([k]) => k !== key));

  const setProfile = (name: string, bag: Partial<Record<ProviderName, ProfileBag>>) => {
    void onUpdateEffortProfiles({ ...effortProfiles, [name]: bag });
  };

  const deleteProfile = (name: string) => {
    // Also purge any task mappings pointing at the now-deleted profile so
    // the backend validator doesn't reject the next task_type_effort_profile
    // PATCH that comes through.
    const cleanedMapping = Object.fromEntries(
      Object.entries(taskTypeEffortProfile).filter(([, v]) => v !== name),
    );
    if (Object.keys(cleanedMapping).length !== Object.keys(taskTypeEffortProfile).length) {
      void onUpdateTaskTypeEffortProfile(cleanedMapping);
    }
    void onUpdateEffortProfiles(omit(effortProfiles, name));
  };

  const renameProfile = (oldName: string, newName: string) => {
    if (!newName || newName === oldName || newName === '_default') return;
    if (effortProfiles[newName]) return;
    const nextProfiles = Object.fromEntries(
      Object.entries(effortProfiles).map(([k, v]) => [k === oldName ? newName : k, v]),
    );
    void onUpdateEffortProfiles(nextProfiles);
    // Migrate task-type mappings onto the new name too, so nothing dangles.
    const migrated = Object.fromEntries(
      Object.entries(taskTypeEffortProfile).map(([k, v]) => [k, v === oldName ? newName : v]),
    );
    if (JSON.stringify(migrated) !== JSON.stringify(taskTypeEffortProfile)) {
      void onUpdateTaskTypeEffortProfile(migrated);
    }
  };

  const addProfile = () => {
    const name = newProfileName.trim();
    if (!name || name === '_default' || effortProfiles[name]) return;
    setNewProfileName('');
    void onUpdateEffortProfiles({
      ...effortProfiles,
      [name]: {
        anthropic: { effort: 'medium' as AnthropicEffort },
        openai: { reasoning_effort: 'medium' as OpenAIReasoning, verbosity: 'medium' as OpenAIVerbosity },
        gemini: { thinking_budget: 4096 },
      },
    });
  };

  const setTaskProfile = (taskType: string, profileName: string) => {
    if (!profileName) {
      void onUpdateTaskTypeEffortProfile(omit(taskTypeEffortProfile, taskType));
    } else {
      void onUpdateTaskTypeEffortProfile({ ...taskTypeEffortProfile, [taskType]: profileName });
    }
  };

  const defaultProfile = taskTypeEffortProfile._default ?? '';

  return (
    <div className="space-y-5">
      <div className="text-xs text-neutral-400 leading-relaxed">
        Effort profiles let you define <span className="text-neutral-200">low / medium / high / max</span> (or
        your own names) once per provider, then tag each task type with a
        profile. No more copy-pasting effort values across every task type.
        Payload/context can still override per-task. Surgical per-(task,
        provider) overrides live under Provider Settings and win over the
        profile layer.
      </div>

      {/* Profiles */}
      <div>
        <div className="flex items-center justify-between mb-2">
          <div className="flex items-center gap-1.5">
            <h3 className="text-xs font-semibold text-neutral-200">Profiles</h3>
            <HintIcon text="Each profile bundles per-provider effort settings. When a task is dispatched, its profile is resolved (payload → context → task-type mapping → _default) and that provider's bag gets merged into the final settings." />
          </div>
          <div className="flex items-center gap-1">
            <input
              type="text"
              value={newProfileName}
              onChange={e => setNewProfileName(e.target.value)}
              onKeyDown={e => { if (e.key === 'Enter') addProfile(); }}
              placeholder="new profile name"
              className="bg-neutral-900 border border-neutral-700 rounded px-2 py-1 text-xs text-neutral-100 w-40"
            />
            <button
              onClick={addProfile}
              className="inline-flex items-center gap-1 px-2 py-1 rounded border border-neutral-700 text-neutral-300 hover:bg-neutral-800 text-xs"
            >
              <Plus className="w-3 h-3" /> add
            </button>
          </div>
        </div>

        <div className="space-y-3">
          {profileNames.map(name => (
            <ProfileCard
              key={name}
              name={name}
              bag={effortProfiles[name] ?? {}}
              onRename={newName => renameProfile(name, newName)}
              onDelete={() => deleteProfile(name)}
              onChange={bag => setProfile(name, bag)}
            />
          ))}
          {profileNames.length === 0 && (
            <div className="text-[11px] text-neutral-500 italic px-2 py-1">
              No profiles yet. Add one above.
            </div>
          )}
        </div>
      </div>

      {/* Task-type → profile mapping */}
      <div>
        <div className="flex items-center gap-1.5 mb-2">
          <h3 className="text-xs font-semibold text-neutral-200">Task → profile mapping</h3>
          <HintIcon text="Which profile each task type uses. Unmapped task types fall through to _default." />
        </div>
        <div className="border border-neutral-800 rounded bg-neutral-900/30 divide-y divide-neutral-800">
          <MappingRow
            taskType="_default"
            profileName={defaultProfile}
            profileNames={profileNames}
            onChange={v => setTaskProfile('_default', v)}
            emphasized
          />
          {ALL_TASK_TYPES.map(t => (
            <MappingRow
              key={t}
              taskType={t}
              profileName={taskTypeEffortProfile[t] ?? ''}
              profileNames={profileNames}
              onChange={v => setTaskProfile(t, v)}
              placeholderLabel={`(inherits _default${defaultProfile ? `: ${defaultProfile}` : ''})`}
            />
          ))}
        </div>
      </div>
    </div>
  );
}

function HintIcon({ text }: { text: string }) {
  return (
    <span
      className="inline-flex items-center justify-center w-4 h-4 rounded-full border border-neutral-700 text-neutral-500 hover:text-neutral-200 hover:border-neutral-500 cursor-help"
      title={text}
      aria-label={`Help: ${text}`}
    >
      <HelpCircle className="w-3 h-3" />
    </span>
  );
}

interface ProfileCardProps {
  name: string;
  bag: Partial<Record<ProviderName, ProfileBag>>;
  onRename: (newName: string) => void;
  onDelete: () => void;
  onChange: (bag: Partial<Record<ProviderName, ProfileBag>>) => void;
}

function ProfileCard({ name, bag, onRename, onDelete, onChange }: ProfileCardProps) {
  const [editingName, setEditingName] = useState(name);
  const anthropic = (bag.anthropic ?? {}) as AnthropicProfileBag;
  const openai = (bag.openai ?? {}) as OpenAIProfileBag;
  const gemini = (bag.gemini ?? {}) as GeminiProfileBag;

  const setProvider = <K extends ProviderName>(key: K, value: ProfileBag) => {
    onChange({ ...bag, [key]: value });
  };

  return (
    <div className="border border-neutral-800 rounded p-3 bg-neutral-900/30 space-y-3">
      <div className="flex items-center justify-between gap-2">
        <input
          value={editingName}
          onChange={e => setEditingName(e.target.value)}
          onBlur={() => { if (editingName !== name) onRename(editingName); }}
          onKeyDown={e => { if (e.key === 'Enter') (e.target as HTMLInputElement).blur(); }}
          className="bg-transparent border border-transparent hover:border-neutral-800 focus:border-neutral-700 rounded px-1.5 py-0.5 text-xs font-semibold text-neutral-200 font-mono focus:outline-none"
        />
        <button
          onClick={onDelete}
          className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] text-neutral-500 hover:text-red-400 hover:bg-red-950/30"
          title="Delete profile (task types pointing at it revert to _default)"
        >
          <X className="w-3 h-3" /> remove
        </button>
      </div>

      {/* Anthropic */}
      <ProviderRow label="anthropic">
        <EnumField
          label="effort"
          value={anthropic.effort ?? ''}
          options={ANTHROPIC_EFFORT_VALUES}
          onChange={v => setProvider('anthropic', { ...anthropic, effort: v ? (v as AnthropicEffort) : undefined })}
        />
      </ProviderRow>

      {/* OpenAI */}
      <ProviderRow label="openai">
        <EnumField
          label="reasoning_effort"
          value={openai.reasoning_effort ?? ''}
          options={OPENAI_REASONING_VALUES}
          onChange={v => setProvider('openai', { ...openai, reasoning_effort: v ? (v as OpenAIReasoning) : undefined })}
        />
        <EnumField
          label="verbosity"
          value={openai.verbosity ?? ''}
          options={OPENAI_VERBOSITY_VALUES}
          onChange={v => setProvider('openai', { ...openai, verbosity: v ? (v as OpenAIVerbosity) : undefined })}
        />
      </ProviderRow>

      {/* Gemini */}
      <ProviderRow label="gemini">
        <IntField
          label="thinking_budget"
          value={typeof gemini.thinking_budget === 'number' ? gemini.thinking_budget : null}
          min={0}
          max={32768}
          onChange={v => setProvider('gemini', { ...gemini, thinking_budget: v ?? undefined })}
        />
      </ProviderRow>
    </div>
  );
}

function ProviderRow({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="grid grid-cols-[80px_1fr] gap-3 items-center">
      <div className="text-[11px] font-mono text-neutral-500 uppercase tracking-wide">{label}</div>
      <div className="flex items-center gap-3 flex-wrap">{children}</div>
    </div>
  );
}

function EnumField({ label, value, options, onChange }: {
  label: string;
  value: string;
  options: readonly string[];
  onChange: (v: string) => void;
}) {
  return (
    <label className="flex items-center gap-1.5">
      <span className="text-[11px] text-neutral-500 font-mono">{label}:</span>
      <select
        value={value}
        onChange={e => onChange(e.target.value)}
        className="bg-neutral-900 border border-neutral-700 rounded px-1.5 py-0.5 text-[11px] text-neutral-100"
      >
        <option value="">—</option>
        {options.map(o => <option key={o} value={o}>{o}</option>)}
      </select>
    </label>
  );
}

function IntField({ label, value, min, max, onChange }: {
  label: string;
  value: number | null;
  min: number;
  max: number;
  onChange: (v: number | null) => void;
}) {
  return (
    <label className="flex items-center gap-1.5">
      <span className="text-[11px] text-neutral-500 font-mono">{label}:</span>
      <input
        type="number"
        value={value ?? ''}
        min={min}
        max={max}
        step={1}
        onChange={e => {
          const raw = e.target.value;
          if (raw === '') { onChange(null); return; }
          const n = parseInt(raw, 10);
          if (!Number.isNaN(n)) onChange(n);
        }}
        className="bg-neutral-900 border border-neutral-700 rounded px-1.5 py-0.5 text-[11px] text-neutral-100 w-20"
      />
      <span className="text-[10px] text-neutral-600">{min}–{max}</span>
    </label>
  );
}

function MappingRow({
  taskType,
  profileName,
  profileNames,
  onChange,
  emphasized,
  placeholderLabel,
}: {
  taskType: string;
  profileName: string;
  profileNames: string[];
  onChange: (value: string) => void;
  emphasized?: boolean;
  placeholderLabel?: string;
}) {
  return (
    <div className={`px-3 py-2 flex items-center gap-3 ${emphasized ? 'bg-neutral-900/60' : ''}`}>
      <code className={`text-[11px] w-40 shrink-0 font-mono truncate ${emphasized ? 'text-emerald-300' : 'text-neutral-200'}`}>
        {taskType}
      </code>
      <select
        value={profileName}
        onChange={e => onChange(e.target.value)}
        className="bg-neutral-900 border border-neutral-700 rounded px-2 py-1 text-[11px] text-neutral-100 w-48"
      >
        <option value="">{placeholderLabel ?? '(none)'}</option>
        {profileNames.map(p => (
          <option key={p} value={p}>{p}</option>
        ))}
      </select>
    </div>
  );
}
