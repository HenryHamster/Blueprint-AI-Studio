import { useMemo, useState } from 'react';
import { Plus, X, HelpCircle } from 'lucide-react';
import type { ProviderCatalogEntry, ProviderName } from '../types/settings';

// Task-type list must match backend payload_schemas.py::TaskType. Unassigned
// types show up in the "Uncategorized" strip so nothing silently loses its
// allow-list. `investigate` is intentionally omitted — it exists in the enum
// but no spawner wires it, so surfacing it here would just create a
// permanently-uncategorized entry.
const ALL_TASK_TYPES = [
  'create', 'review', 'fix', 'plan', 'research',
  'strategic_review', 'design_doc_update', 'security_audit',
  'qa', 'deps', 'docs', 'migrate', 'smoketest',
  'refactor', 'debug', 'perf',
] as const;

type SelectionMode = 'balanced' | 'preferred' | 'exclusive';

const MODE_DESCRIPTIONS: Record<SelectionMode, string> = {
  balanced: 'Pool accounts across all listed providers; pick globally by health + current load. Adding an account immediately puts it into rotation.',
  preferred: 'Try providers in list order; fall through to the next only when the earlier one is exhausted.',
  exclusive: 'Use the first listed provider only; never fall back cross-provider.',
};

const DEFAULT_CATEGORY_KEY = '_default';

interface Props {
  providers: Record<string, ProviderCatalogEntry>;
  selectionMode: SelectionMode;
  onUpdateSelectionMode: (mode: SelectionMode) => void | Promise<void>;
  taskTypeCategories: Record<string, string[]>;
  onUpdateTaskTypeCategories: (value: Record<string, string[]>) => void | Promise<void>;
  providerPreferenceByCategory: Record<string, ProviderName[]>;
  onUpdateProviderPreferenceByCategory: (value: Record<string, ProviderName[]>) => void | Promise<void>;
  taskTypeProviderPreference: Record<string, ProviderName[]>;
  onUpdateTaskTypeProviderPreferences: (value: Record<string, ProviderName[]>) => void | Promise<void>;
}

export function ProviderCategoriesPanel({
  providers,
  selectionMode,
  onUpdateSelectionMode,
  taskTypeCategories,
  onUpdateTaskTypeCategories,
  providerPreferenceByCategory,
  onUpdateProviderPreferenceByCategory,
  taskTypeProviderPreference,
  onUpdateTaskTypeProviderPreferences,
}: Props) {
  const providerNames = Object.keys(providers) as ProviderName[];
  const [newCategoryName, setNewCategoryName] = useState('');

  // Which task types aren't claimed by any category? They still resolve via
  // the _default preference, but calling them out keeps the config honest.
  const uncategorized = useMemo(() => {
    const claimed = new Set<string>();
    Object.values(taskTypeCategories).forEach(members => {
      if (Array.isArray(members)) members.forEach(m => claimed.add(m));
    });
    return ALL_TASK_TYPES.filter(t => !claimed.has(t));
  }, [taskTypeCategories]);

  const defaultProviders: ProviderName[] = providerPreferenceByCategory[DEFAULT_CATEGORY_KEY] ?? providerNames;

  const setCategoryMembers = (category: string, members: string[]) => {
    const next = { ...taskTypeCategories, [category]: members };
    void onUpdateTaskTypeCategories(next);
  };

  const omit = <T,>(obj: Record<string, T>, key: string): Record<string, T> =>
    Object.fromEntries(Object.entries(obj).filter(([k]) => k !== key));

  const renameCategory = (oldName: string, newName: string) => {
    if (!newName || newName === oldName || newName === DEFAULT_CATEGORY_KEY) return;
    if (taskTypeCategories[newName]) return;
    const nextCategories = Object.fromEntries(
      Object.entries(taskTypeCategories).map(([k, v]) => [k === oldName ? newName : k, v]),
    );
    void onUpdateTaskTypeCategories(nextCategories);
    if (providerPreferenceByCategory[oldName]) {
      const nextPrefs = { ...omit(providerPreferenceByCategory, oldName), [newName]: providerPreferenceByCategory[oldName] };
      void onUpdateProviderPreferenceByCategory(nextPrefs);
    }
  };

  const deleteCategory = (category: string) => {
    void onUpdateTaskTypeCategories(omit(taskTypeCategories, category));
    if (providerPreferenceByCategory[category]) {
      void onUpdateProviderPreferenceByCategory(omit(providerPreferenceByCategory, category));
    }
  };

  const addCategory = () => {
    const name = newCategoryName.trim();
    if (!name || name === DEFAULT_CATEGORY_KEY || taskTypeCategories[name]) return;
    setNewCategoryName('');
    void onUpdateTaskTypeCategories({ ...taskTypeCategories, [name]: [] });
  };

  const moveTaskType = (taskType: string, toCategory: string | null) => {
    // Remove from whichever category currently owns it; add to the destination
    // when toCategory is non-null (null = make it uncategorized).
    const next: Record<string, string[]> = {};
    Object.entries(taskTypeCategories).forEach(([cat, members]) => {
      next[cat] = (members || []).filter(m => m !== taskType);
    });
    if (toCategory !== null) {
      const existing = next[toCategory] ?? [];
      if (!existing.includes(taskType)) next[toCategory] = [...existing, taskType];
    }
    void onUpdateTaskTypeCategories(next);
  };

  const setCategoryProviders = (category: string, providers: ProviderName[]) => {
    const next = { ...providerPreferenceByCategory, [category]: providers };
    void onUpdateProviderPreferenceByCategory(next);
  };

  const clearCategoryProviders = (category: string) => {
    void onUpdateProviderPreferenceByCategory(omit(providerPreferenceByCategory, category));
  };

  const setTaskTypeOverride = (taskType: string, providers: ProviderName[] | null) => {
    if (providers === null || providers.length === 0) {
      void onUpdateTaskTypeProviderPreferences(omit(taskTypeProviderPreference, taskType));
    } else {
      void onUpdateTaskTypeProviderPreferences({ ...taskTypeProviderPreference, [taskType]: providers });
    }
  };

  return (
    <div className="space-y-5">
      {/* Selection mode */}
      <div>
        <div className="flex items-center gap-1.5 mb-2">
          <h3 className="text-xs font-semibold text-neutral-200">Selection mode</h3>
          <HintIcon text="Controls how the pool picks among accounts listed for a task. Per-task overrides below still win." />
        </div>
        <div className="flex items-center gap-1 border border-neutral-800 rounded p-1 w-fit">
          {(['balanced', 'preferred', 'exclusive'] as SelectionMode[]).map(mode => (
            <button
              key={mode}
              onClick={() => void onUpdateSelectionMode(mode)}
              className={`px-3 py-1.5 rounded text-xs transition-colors ${
                selectionMode === mode
                  ? 'bg-emerald-900/40 text-emerald-300 border border-emerald-800'
                  : 'text-neutral-400 hover:text-neutral-200 border border-transparent'
              }`}
              title={MODE_DESCRIPTIONS[mode]}
            >
              {mode}
            </button>
          ))}
        </div>
        <div className="text-[11px] text-neutral-500 mt-1.5 italic">
          {MODE_DESCRIPTIONS[selectionMode]}
        </div>
      </div>

      {/* Default allow-list */}
      <div className="border border-neutral-800 rounded p-3 bg-neutral-900/30">
        <div className="flex items-center gap-1.5 mb-2">
          <span className="text-xs font-semibold text-neutral-200 font-mono">_default</span>
          <HintIcon text="Fallback allow-list used by any task type not assigned to a category, or a category without its own preference. Uncheck a provider to exclude it globally." />
        </div>
        <ProviderChecklist
          providers={providerNames}
          selected={defaultProviders}
          onChange={values => setCategoryProviders(DEFAULT_CATEGORY_KEY, values)}
        />
      </div>

      {/* Categories */}
      <div>
        <div className="flex items-center justify-between mb-2">
          <div className="flex items-center gap-1.5">
            <h3 className="text-xs font-semibold text-neutral-200">Categories</h3>
            <HintIcon text="Group task types so a batch can share the same provider allow-list. Types can live in only one category; unassigned types fall back to _default." />
          </div>
          <div className="flex items-center gap-1">
            <input
              type="text"
              value={newCategoryName}
              onChange={e => setNewCategoryName(e.target.value)}
              onKeyDown={e => { if (e.key === 'Enter') addCategory(); }}
              placeholder="new category name"
              className="bg-neutral-900 border border-neutral-700 rounded px-2 py-1 text-xs text-neutral-100 w-40"
            />
            <button
              onClick={addCategory}
              className="inline-flex items-center gap-1 px-2 py-1 rounded border border-neutral-700 text-neutral-300 hover:bg-neutral-800 text-xs"
              title="Add category"
            >
              <Plus className="w-3 h-3" /> add
            </button>
          </div>
        </div>

        <div className="space-y-3">
          {Object.entries(taskTypeCategories).map(([category, members]) => (
            <CategoryCard
              key={category}
              name={category}
              members={members || []}
              allTaskTypes={ALL_TASK_TYPES as unknown as string[]}
              taskTypeCategories={taskTypeCategories}
              providerNames={providerNames}
              preference={providerPreferenceByCategory[category] ?? null}
              defaultProviders={defaultProviders}
              onRename={newName => renameCategory(category, newName)}
              onDelete={() => deleteCategory(category)}
              onAddMember={taskType => moveTaskType(taskType, category)}
              onRemoveMember={taskType => setCategoryMembers(category, members.filter(m => m !== taskType))}
              onPreferenceChange={values => setCategoryProviders(category, values)}
              onPreferenceClear={() => clearCategoryProviders(category)}
            />
          ))}
        </div>

        {uncategorized.length > 0 && (
          <div className="mt-3 p-2 border border-amber-900/50 rounded bg-amber-950/20 text-[11px]">
            <span className="text-amber-300">Uncategorized:</span>{' '}
            <span className="text-neutral-300">{uncategorized.join(', ')}</span>
            <span className="text-neutral-500"> — these resolve via _default.</span>
          </div>
        )}
      </div>

      {/* Per-task overrides */}
      <div>
        <div className="flex items-center gap-1.5 mb-2">
          <h3 className="text-xs font-semibold text-neutral-200">Per-task overrides</h3>
          <HintIcon text="Wins over category + _default for a specific task type. Leave empty to inherit from the category." />
        </div>
        <TaskOverrideTable
          providerNames={providerNames}
          overrides={taskTypeProviderPreference}
          onSet={setTaskTypeOverride}
        />
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

function ProviderChecklist({
  providers,
  selected,
  onChange,
}: {
  providers: ProviderName[];
  selected: ProviderName[];
  onChange: (values: ProviderName[]) => void;
}) {
  const toggle = (name: ProviderName) => {
    if (selected.includes(name)) {
      onChange(selected.filter(p => p !== name));
    } else {
      onChange([...selected, name]);
    }
  };
  return (
    <div className="flex items-center gap-2 flex-wrap">
      {providers.map(name => {
        const active = selected.includes(name);
        return (
          <button
            key={name}
            onClick={() => toggle(name)}
            className={`px-2 py-1 rounded text-xs border transition-colors ${
              active
                ? 'bg-emerald-900/40 text-emerald-200 border-emerald-800'
                : 'bg-neutral-900 text-neutral-500 border-neutral-800 hover:text-neutral-300'
            }`}
          >
            {name}
          </button>
        );
      })}
    </div>
  );
}

interface CategoryCardProps {
  name: string;
  members: string[];
  allTaskTypes: string[];
  taskTypeCategories: Record<string, string[]>;
  providerNames: ProviderName[];
  preference: ProviderName[] | null;
  defaultProviders: ProviderName[];
  onRename: (newName: string) => void;
  onDelete: () => void;
  onAddMember: (taskType: string) => void;
  onRemoveMember: (taskType: string) => void;
  onPreferenceChange: (values: ProviderName[]) => void;
  onPreferenceClear: () => void;
}

function CategoryCard({
  name, members, allTaskTypes, taskTypeCategories,
  providerNames, preference, defaultProviders,
  onRename, onDelete, onAddMember, onRemoveMember,
  onPreferenceChange, onPreferenceClear,
}: CategoryCardProps) {
  const [editingName, setEditingName] = useState(name);
  // Task types available to add: not already members of THIS category. Adding
  // from another category is allowed (moveTaskType handles the steal).
  const addable = allTaskTypes.filter(t => !members.includes(t));
  const hasOwnPreference = preference !== null;
  const effectivePreference = preference ?? defaultProviders;

  const currentOwner = (taskType: string): string | null => {
    for (const [cat, list] of Object.entries(taskTypeCategories)) {
      if (Array.isArray(list) && list.includes(taskType) && cat !== name) return cat;
    }
    return null;
  };

  return (
    <div className="border border-neutral-800 rounded p-3 bg-neutral-900/30 space-y-2">
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
          title="Delete category (task types fall back to _default)"
        >
          <X className="w-3 h-3" /> remove
        </button>
      </div>

      <div className="flex items-center gap-1.5 flex-wrap">
        {members.map(m => (
          <span
            key={m}
            className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded border border-neutral-700 bg-neutral-800/60 text-[11px] text-neutral-200 font-mono"
          >
            {m}
            <button
              onClick={() => onRemoveMember(m)}
              className="text-neutral-500 hover:text-red-400"
              title="Remove from category (→ uncategorized)"
            >
              <X className="w-3 h-3" />
            </button>
          </span>
        ))}
        {addable.length > 0 && (
          <select
            value=""
            onChange={e => { if (e.target.value) onAddMember(e.target.value); }}
            className="bg-neutral-900 border border-neutral-700 rounded px-1.5 py-0.5 text-[11px] text-neutral-300"
          >
            <option value="">+ add task type</option>
            {addable.map(t => {
              const owner = currentOwner(t);
              return (
                <option key={t} value={t}>
                  {t}{owner ? `  (moves from ${owner})` : ''}
                </option>
              );
            })}
          </select>
        )}
      </div>

      <div className="flex items-center gap-2 pt-1 border-t border-neutral-800/70">
        <div className="text-[10px] text-neutral-500 shrink-0">providers:</div>
        <ProviderChecklist
          providers={providerNames}
          selected={effectivePreference}
          onChange={onPreferenceChange}
        />
        {hasOwnPreference ? (
          <button
            onClick={onPreferenceClear}
            className="text-[10px] text-neutral-500 hover:text-neutral-200 ml-auto shrink-0"
            title="Remove this category's preference (inherit _default)"
          >
            reset to _default
          </button>
        ) : (
          <span className="text-[10px] text-neutral-600 ml-auto shrink-0 italic">inheriting _default</span>
        )}
      </div>
    </div>
  );
}

function TaskOverrideTable({
  providerNames,
  overrides,
  onSet,
}: {
  providerNames: ProviderName[];
  overrides: Record<string, ProviderName[]>;
  onSet: (taskType: string, providers: ProviderName[] | null) => void;
}) {
  const [newType, setNewType] = useState<string>('');
  const entries = Object.entries(overrides).filter(([k]) => k !== '_default');
  const eligibleToAdd = ALL_TASK_TYPES.filter(t => !(t in overrides));

  return (
    <div className="border border-neutral-800 rounded bg-neutral-900/30 divide-y divide-neutral-800">
      {entries.length === 0 && (
        <div className="px-3 py-2 text-[11px] text-neutral-500 italic">
          No overrides. Add one below to pin a specific task type.
        </div>
      )}
      {entries.map(([taskType, providers]) => (
        <div key={taskType} className="px-3 py-2 flex items-center gap-3">
          <code className="text-[11px] text-neutral-200 w-32 shrink-0 font-mono truncate">{taskType}</code>
          <ProviderChecklist
            providers={providerNames}
            selected={providers}
            onChange={values => onSet(taskType, values)}
          />
          <button
            onClick={() => onSet(taskType, null)}
            className="ml-auto text-[10px] text-neutral-500 hover:text-red-400 shrink-0"
            title="Remove override (inherit category)"
          >
            remove
          </button>
        </div>
      ))}
      {eligibleToAdd.length > 0 && (
        <div className="px-3 py-2 flex items-center gap-2">
          <select
            value={newType}
            onChange={e => setNewType(e.target.value)}
            className="bg-neutral-900 border border-neutral-700 rounded px-1.5 py-1 text-[11px] text-neutral-200"
          >
            <option value="">+ add override for…</option>
            {eligibleToAdd.map(t => <option key={t} value={t}>{t}</option>)}
          </select>
          <button
            onClick={() => { if (newType) { onSet(newType, providerNames); setNewType(''); } }}
            disabled={!newType}
            className="inline-flex items-center gap-1 px-2 py-1 rounded border border-neutral-700 text-neutral-300 hover:bg-neutral-800 text-[11px] disabled:opacity-40"
          >
            <Plus className="w-3 h-3" /> add
          </button>
        </div>
      )}
    </div>
  );
}
