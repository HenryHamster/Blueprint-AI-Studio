import { useMemo, useState } from 'react';
import { Plus, X, HelpCircle } from 'lucide-react';
import type {
  CategoryRoutingEntry,
  EffortLevel,
  ProviderCatalogEntry,
  ProviderName,
  TaskTypeRoutingEntry,
} from '../types/settings';
import { EFFORT_LEVELS } from '../types/settings';
import { useTaskCapabilities } from '../taskCapabilities';

const DEFAULT_CATEGORY_KEY = '_default';
type SelectionMode = 'balanced' | 'preferred' | 'exclusive';

const MODE_DESCRIPTIONS: Record<SelectionMode, string> = {
  balanced: 'Pool accounts across listed providers; pick by health + current load. Adding an account immediately puts it into rotation.',
  preferred: 'Try providers in list order; fall through to the next only when the earlier is exhausted.',
  exclusive: 'Use the first listed provider only; never fall back.',
};

interface Props {
  providers: Record<string, ProviderCatalogEntry>;
  selectionMode: SelectionMode;
  onUpdateSelectionMode: (mode: SelectionMode) => void | Promise<void>;
  taskTypeCategories: Record<string, string[]>;
  onUpdateTaskTypeCategories: (value: Record<string, string[]>) => void | Promise<void>;
  categoryRouting: Record<string, CategoryRoutingEntry>;
  onUpdateCategoryRouting: (value: Record<string, CategoryRoutingEntry>) => void | Promise<void>;
  taskTypeRouting: Record<string, TaskTypeRoutingEntry>;
  onUpdateTaskTypeRouting: (value: Record<string, TaskTypeRoutingEntry>) => void | Promise<void>;
}

export function ProviderCategoriesPanel({
  providers,
  selectionMode,
  onUpdateSelectionMode,
  taskTypeCategories,
  onUpdateTaskTypeCategories,
  categoryRouting,
  onUpdateCategoryRouting,
  taskTypeRouting,
  onUpdateTaskTypeRouting,
}: Props) {
  const { providerRoutableIds } = useTaskCapabilities();
  const allTaskTypes = providerRoutableIds;
  const providerNames = Object.keys(providers) as ProviderName[];
  const [newCategoryName, setNewCategoryName] = useState('');

  const omit = <T,>(obj: Record<string, T>, key: string): Record<string, T> =>
    Object.fromEntries(Object.entries(obj).filter(([k]) => k !== key));

  const uncategorized = useMemo(() => {
    const claimed = new Set<string>();
    Object.values(taskTypeCategories).forEach(members => {
      if (Array.isArray(members)) members.forEach(m => claimed.add(m));
    });
    return allTaskTypes.filter(t => !claimed.has(t));
  }, [taskTypeCategories, allTaskTypes]);

  const defaultEntry: CategoryRoutingEntry = categoryRouting[DEFAULT_CATEGORY_KEY] ?? {
    providers: providerNames,
    effort: 'medium',
  };

  const setCategoryEntry = (category: string, entry: CategoryRoutingEntry) => {
    void onUpdateCategoryRouting({ ...categoryRouting, [category]: entry });
  };

  const setCategoryMembers = (category: string, members: string[]) => {
    void onUpdateTaskTypeCategories({ ...taskTypeCategories, [category]: members });
  };

  const renameCategory = (oldName: string, newName: string) => {
    if (!newName || newName === oldName || newName === DEFAULT_CATEGORY_KEY) return;
    if (taskTypeCategories[newName] || categoryRouting[newName]) return;
    const renamedCats = Object.fromEntries(
      Object.entries(taskTypeCategories).map(([k, v]) => [k === oldName ? newName : k, v]),
    );
    void onUpdateTaskTypeCategories(renamedCats);
    if (categoryRouting[oldName]) {
      const renamedRouting = {
        ...omit(categoryRouting, oldName),
        [newName]: categoryRouting[oldName],
      };
      void onUpdateCategoryRouting(renamedRouting);
    }
  };

  const deleteCategory = (category: string) => {
    void onUpdateTaskTypeCategories(omit(taskTypeCategories, category));
    if (categoryRouting[category]) {
      void onUpdateCategoryRouting(omit(categoryRouting, category));
    }
  };

  const addCategory = () => {
    const name = newCategoryName.trim();
    if (!name || name === DEFAULT_CATEGORY_KEY || taskTypeCategories[name]) return;
    setNewCategoryName('');
    void onUpdateTaskTypeCategories({ ...taskTypeCategories, [name]: [] });
    void onUpdateCategoryRouting({
      ...categoryRouting,
      [name]: { providers: defaultEntry.providers, effort: defaultEntry.effort },
    });
  };

  const moveTaskType = (taskType: string, toCategory: string | null) => {
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

  const setTaskOverride = (taskType: string, entry: TaskTypeRoutingEntry | null) => {
    if (!entry || (entry.providers === undefined && entry.effort === undefined)) {
      void onUpdateTaskTypeRouting(omit(taskTypeRouting, taskType));
    } else {
      void onUpdateTaskTypeRouting({ ...taskTypeRouting, [taskType]: entry });
    }
  };

  return (
    <div className="space-y-5">
      {/* Selection mode */}
      <div>
        <div className="flex items-center gap-1.5 mb-2">
          <h3 className="text-xs font-semibold text-neutral-200">Selection mode</h3>
          <HintIcon text="Controls how the pool picks accounts across providers listed for a task." />
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
        <div className="text-ui-md text-neutral-500 mt-1.5 italic">
          {MODE_DESCRIPTIONS[selectionMode]}
        </div>
      </div>

      {/* Default category */}
      <CategoryCard
        name={DEFAULT_CATEGORY_KEY}
        isDefault
        members={[]}
        allTaskTypes={allTaskTypes}
        taskTypeCategories={taskTypeCategories}
        providerNames={providerNames}
        entry={defaultEntry}
        onRename={() => { /* default can't be renamed */ }}
        onDelete={() => { /* default can't be deleted */ }}
        onAddMember={() => { /* default has no explicit members */ }}
        onRemoveMember={() => { /* n/a */ }}
        onEntryChange={entry => setCategoryEntry(DEFAULT_CATEGORY_KEY, entry)}
      />

      {/* User-defined categories */}
      <div>
        <div className="flex items-center justify-between mb-2">
          <div className="flex items-center gap-1.5">
            <h3 className="text-xs font-semibold text-neutral-200">Categories</h3>
            <HintIcon text="Group task types so one entry controls providers + effort for the whole group. Types can live in only one category; unassigned types resolve via _default above." />
          </div>
          <div className="flex items-center gap-1">
            <input
              type="text"
              value={newCategoryName}
              onChange={e => setNewCategoryName(e.target.value)}
              onKeyDown={e => { if (e.key === 'Enter') addCategory(); }}
              placeholder="new category name"
              className="bg-workbench-base border border-neutral-700 rounded px-2 py-1 text-xs text-neutral-100 w-40"
            />
            {/* eslint-disable-next-line viewport/min-touch-target -- pre-existing, dense UI; audit per rule introduction */}
            <button
              onClick={addCategory}
              className="inline-flex items-center gap-1 px-2 py-1 rounded border border-neutral-700 text-neutral-300 hover:bg-workbench-hover text-xs"
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
              allTaskTypes={allTaskTypes}
              taskTypeCategories={taskTypeCategories}
              providerNames={providerNames}
              entry={categoryRouting[category] ?? { providers: defaultEntry.providers, effort: defaultEntry.effort }}
              onRename={newName => renameCategory(category, newName)}
              onDelete={() => deleteCategory(category)}
              onAddMember={taskType => moveTaskType(taskType, category)}
              onRemoveMember={taskType => setCategoryMembers(category, members.filter(m => m !== taskType))}
              onEntryChange={entry => setCategoryEntry(category, entry)}
            />
          ))}
        </div>

        {uncategorized.length > 0 && (
          <div className="mt-3 p-2 border border-amber-900/50 rounded bg-amber-950/20 text-ui-md">
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
          <HintIcon text="Surgical per-task-type overrides. Either field optional: override just the providers, just the effort, or both. Inherits whatever isn't overridden from the task's category." />
        </div>
        <TaskOverrideTable
          providerNames={providerNames}
          allTaskTypes={allTaskTypes}
          overrides={taskTypeRouting}
          onSet={setTaskOverride}
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
    onChange(selected.includes(name) ? selected.filter(p => p !== name) : [...selected, name]);
  };
  return (
    <div className="flex items-center gap-2 flex-wrap">
      {providers.map(name => {
        const active = selected.includes(name);
        return (
          // eslint-disable-next-line viewport/min-touch-target -- pre-existing, dense UI; audit per rule introduction
          <button
            key={name}
            onClick={() => toggle(name)}
            className={`px-2 py-1 rounded text-xs border transition-colors ${
              active
                ? 'bg-emerald-900/40 text-emerald-200 border-emerald-800'
                : 'bg-workbench-base text-neutral-500 border-neutral-800 hover:text-neutral-300'
            }`}
          >
            {name}
          </button>
        );
      })}
    </div>
  );
}

function EffortSelect({
  value,
  onChange,
  allowEmpty,
}: {
  value: EffortLevel | '';
  onChange: (value: EffortLevel | '') => void;
  allowEmpty?: boolean;
}) {
  return (
    <select
      value={value}
      onChange={e => onChange(e.target.value as EffortLevel | '')}
      className="bg-workbench-base border border-neutral-700 rounded px-2 py-1 text-xs text-neutral-100"
    >
      {allowEmpty && <option value="">(inherit)</option>}
      {EFFORT_LEVELS.map(l => <option key={l} value={l}>{l}</option>)}
    </select>
  );
}

interface CategoryCardProps {
  name: string;
  isDefault?: boolean;
  members: string[];
  allTaskTypes: string[];
  taskTypeCategories: Record<string, string[]>;
  providerNames: ProviderName[];
  entry: CategoryRoutingEntry;
  onRename: (newName: string) => void;
  onDelete: () => void;
  onAddMember: (taskType: string) => void;
  onRemoveMember: (taskType: string) => void;
  onEntryChange: (entry: CategoryRoutingEntry) => void;
}

function CategoryCard({
  name, isDefault, members, allTaskTypes, taskTypeCategories,
  providerNames, entry,
  onRename, onDelete, onAddMember, onRemoveMember, onEntryChange,
}: CategoryCardProps) {
  const [editingName, setEditingName] = useState(name);
  const addable = allTaskTypes.filter(t => !members.includes(t));

  const currentOwner = (taskType: string): string | null => {
    for (const [cat, list] of Object.entries(taskTypeCategories)) {
      if (Array.isArray(list) && list.includes(taskType) && cat !== name) return cat;
    }
    return null;
  };

  return (
    <div className="border border-neutral-800 rounded p-3 bg-workbench-base/30 space-y-2">
      <div className="flex items-center justify-between gap-2">
        {isDefault ? (
          <span className="text-xs font-semibold text-neutral-200 font-mono px-1.5 py-0.5">{name}</span>
        ) : (
          <input
            value={editingName}
            onChange={e => setEditingName(e.target.value)}
            onBlur={() => { if (editingName !== name) onRename(editingName); }}
            onKeyDown={e => { if (e.key === 'Enter') (e.target as HTMLInputElement).blur(); }}
            className="bg-transparent border border-transparent hover:border-neutral-800 rounded px-1.5 py-0.5 text-xs font-semibold text-neutral-200 font-mono workbench-focus"
          />
        )}
        {!isDefault && (
          // eslint-disable-next-line viewport/min-touch-target -- pre-existing, dense UI; audit per rule introduction
          <button
            onClick={onDelete}
            className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-ui-sm text-neutral-500 hover:text-red-400 hover:bg-red-950/30"
            title="Delete category (task types fall back to _default)"
          >
            <X className="w-3 h-3" /> remove
          </button>
        )}
      </div>

      {!isDefault && (
        <div className="flex items-center gap-1.5 flex-wrap">
          {members.map(m => (
            <span
              key={m}
              className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded border border-neutral-700 bg-workbench-panel/60 text-ui-md text-neutral-200 font-mono"
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
              className="bg-workbench-base border border-neutral-700 rounded px-1.5 py-0.5 text-ui-md text-neutral-300"
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
      )}

      <div className="flex items-center gap-3 pt-1 border-t border-neutral-800/70">
        <div className="flex items-center gap-2">
          <span className="text-ui-sm text-neutral-500 shrink-0">providers:</span>
          <ProviderChecklist
            providers={providerNames}
            selected={entry.providers}
            onChange={values => onEntryChange({ ...entry, providers: values })}
          />
        </div>
        <div className="flex items-center gap-2 ml-auto">
          <span className="text-ui-sm text-neutral-500 shrink-0">effort:</span>
          <EffortSelect
            value={entry.effort}
            onChange={value => value && onEntryChange({ ...entry, effort: value })}
          />
        </div>
      </div>
    </div>
  );
}

function TaskOverrideTable({
  providerNames,
  allTaskTypes,
  overrides,
  onSet,
}: {
  providerNames: ProviderName[];
  allTaskTypes: string[];
  overrides: Record<string, TaskTypeRoutingEntry>;
  onSet: (taskType: string, entry: TaskTypeRoutingEntry | null) => void;
}) {
  const [newType, setNewType] = useState<string>('');
  const entries = Object.entries(overrides);
  const eligibleToAdd = allTaskTypes.filter(t => !(t in overrides));

  return (
    <div className="border border-neutral-800 rounded bg-workbench-base/30 divide-y divide-neutral-800">
      {entries.length === 0 && (
        <div className="px-3 py-2 text-ui-md text-neutral-500 italic">
          No overrides. Add one below to pin a specific task type.
        </div>
      )}
      {entries.map(([taskType, entry]) => (
        <div key={taskType} className="px-3 py-2 flex flex-wrap items-start gap-3">
          <code className="text-ui-md text-neutral-200 w-28 shrink-0 font-mono truncate">{taskType}</code>
          <div className="flex items-center gap-1.5">
            <span className="text-ui-sm text-neutral-500">providers:</span>
            <ProviderChecklist
              providers={providerNames}
              selected={entry.providers ?? []}
              onChange={values => onSet(taskType, { ...entry, providers: values.length ? values : undefined })}
            />
          </div>
          <div className="flex items-center gap-1.5 ml-auto">
            <span className="text-ui-sm text-neutral-500">effort:</span>
            <EffortSelect
              value={entry.effort ?? ''}
              onChange={value => onSet(taskType, { ...entry, effort: value || undefined })}
              allowEmpty
            />
          </div>
          <button
            onClick={() => onSet(taskType, null)}
            className="text-ui-sm text-neutral-500 hover:text-red-400 shrink-0"
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
            className="bg-workbench-base border border-neutral-700 rounded px-1.5 py-1 text-ui-md text-neutral-200"
          >
            <option value="">+ add override for…</option>
            {eligibleToAdd.map(t => <option key={t} value={t}>{t}</option>)}
          </select>
          {/* eslint-disable-next-line viewport/min-touch-target -- pre-existing, dense UI; audit per rule introduction */}
          <button
            onClick={() => { if (newType) { onSet(newType, { providers: providerNames }); setNewType(''); } }}
            disabled={!newType}
            className="inline-flex items-center gap-1 px-2 py-1 rounded border border-neutral-700 text-neutral-300 hover:bg-workbench-hover text-ui-md disabled:opacity-40"
          >
            <Plus className="w-3 h-3" /> add
          </button>
        </div>
      )}
    </div>
  );
}
