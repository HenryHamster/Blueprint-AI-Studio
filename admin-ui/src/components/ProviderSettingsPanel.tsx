import { useMemo, useState } from 'react';
import type {
  EffortLevel,
  MaxPlanAccount,
  ProviderCatalogEntry,
  ProviderName,
  ProviderSettingsBag,
  ProviderSettingsByEffort,
  ProviderSettingsDefaults,
  TaskTypeProviderOverrides,
} from '../types/settings';
import { EFFORT_LEVELS } from '../types/settings';
import { useTaskCapabilities } from '../taskCapabilities';

// Keep in sync with backend/app/providers/gemini.py::_GEMINI_PREVIEW_MODELS
// and _PREVIEW_TO_GA_FALLBACK. When the backend adds a preview model (or
// promotes one to GA), update both halves of this mapping.
const GEMINI_PREVIEW_FALLBACK: Record<string, string> = {
  'gemini-3-pro-preview': 'gemini-2.5-pro',
  'gemini-3-flash-preview': 'gemini-2.5-flash',
  'gemini-3.1-pro-preview': 'gemini-2.5-pro',
  'gemini-3.1-flash-preview': 'gemini-2.5-flash',
};

// Mirrors backend/app/providers/openai.py::_FLEX_SUPPORTED_MODELS. Kept in
// sync by convention — when the backend gains/loses flex support for a
// model, update both sides. Unknown model → fail closed (no flex) so an
// operator doesn't stamp a guaranteed-400 config.
const OPENAI_FLEX_SUPPORTED: ReadonlySet<string> = new Set([
  'gpt-5.6-sol',
  'gpt-5.6-terra',
  'gpt-5.6-luna',
  'gpt-5.5',
  'gpt-5.4',
  'gpt-5.4-mini',
]);

function openaiModelSupportsFlex(model: unknown): boolean {
  if (typeof model !== 'string') return false;
  const n = model.trim().toLowerCase();
  if (!n) return false;
  return OPENAI_FLEX_SUPPORTED.has(n);
}

// JSON-Schema subset we render — mirrors backend providers/base.py:_validate_against_schema.
interface SchemaNode {
  type?: 'object' | 'string' | 'integer' | 'number' | 'boolean' | 'array';
  enum?: readonly (string | number)[];
  properties?: Record<string, SchemaNode>;
  additionalProperties?: boolean;
  items?: SchemaNode;
  default?: unknown;
  // Extensions used by our settings_schema to drive UX: inline hint copy and
  // suggested-but-not-required values for free-text fields (rendered as a
  // <datalist> so the user gets autocomplete without losing custom input).
  description?: string;
  examples?: readonly string[];
}

type UpdateDefaults = (value: ProviderSettingsDefaults) => void | Promise<void>;
type UpdateOverrides = (value: TaskTypeProviderOverrides) => void | Promise<void>;

interface Props {
  providers: Record<string, ProviderCatalogEntry>;
  providerSettingsDefaults: ProviderSettingsDefaults;
  taskTypeProviderOverrides: TaskTypeProviderOverrides;
  providerSettingsByEffort: ProviderSettingsByEffort;
  maxPlanAccounts: MaxPlanAccount[];
  onUpdateDefaults: UpdateDefaults;
  onUpdateOverrides: UpdateOverrides;
  onUpdateProviderSettingsByEffort: (value: ProviderSettingsByEffort) => void | Promise<void>;
}

export function ProviderSettingsPanel({
  providers,
  providerSettingsDefaults,
  taskTypeProviderOverrides,
  providerSettingsByEffort,
  maxPlanAccounts,
  onUpdateDefaults,
  onUpdateOverrides,
  onUpdateProviderSettingsByEffort,
}: Props) {
  const { providerRoutableOptions } = useTaskCapabilities();
  const [scope, setScope] = useState<'matrix' | 'defaults' | 'overrides'>('matrix');
  const [selectedProvider, setSelectedProvider] = useState<ProviderName>('anthropic');
  const [selectedTaskType, setSelectedTaskType] = useState<string>('review');
  const [matrixLevel, setMatrixLevel] = useState<EffortLevel>('medium');

  const providerNames = Object.keys(providers) as ProviderName[];
  const currentSchema = providers[selectedProvider]?.settings_schema;

  const effectiveBag: ProviderSettingsBag = useMemo(() => {
    if (scope === 'matrix') {
      return (providerSettingsByEffort[selectedProvider]?.[matrixLevel] ?? {}) as ProviderSettingsBag;
    }
    if (scope === 'defaults') {
      return providerSettingsDefaults[selectedProvider] ?? {};
    }
    return taskTypeProviderOverrides[selectedTaskType]?.[selectedProvider] ?? {};
  }, [scope, selectedProvider, selectedTaskType, matrixLevel, providerSettingsDefaults, taskTypeProviderOverrides, providerSettingsByEffort]);

  const handleBagChange = (newBag: ProviderSettingsBag) => {
    if (scope === 'matrix') {
      const existingForProvider = providerSettingsByEffort[selectedProvider] ?? {};
      const nextForProvider = { ...existingForProvider, [matrixLevel]: newBag };
      const next = {
        ...providerSettingsByEffort,
        [selectedProvider]: nextForProvider,
      } as ProviderSettingsByEffort;
      void onUpdateProviderSettingsByEffort(next);
      return;
    }
    if (scope === 'defaults') {
      void onUpdateDefaults({ ...providerSettingsDefaults, [selectedProvider]: newBag });
      return;
    }
    // overrides: prune empty (provider, task) bags so defaults/matrix show through.
    const existingForType = taskTypeProviderOverrides[selectedTaskType] ?? {};
    const prunedForType = Object.fromEntries(
      Object.entries(existingForType).filter(([prov]) => prov !== selectedProvider),
    ) as Partial<Record<ProviderName, ProviderSettingsBag>>;
    if (Object.keys(newBag).length > 0) {
      prunedForType[selectedProvider] = newBag;
    }
    const prunedOverrides = Object.fromEntries(
      Object.entries(taskTypeProviderOverrides).filter(([tt]) => tt !== selectedTaskType),
    ) as TaskTypeProviderOverrides;
    if (Object.keys(prunedForType).length > 0) {
      prunedOverrides[selectedTaskType] = prunedForType;
    }
    void onUpdateOverrides(prunedOverrides);
  };

  if (providerNames.length === 0) {
    return (
      <div className="text-xs text-neutral-500">
        Loading provider catalog…
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <div className="text-xs text-neutral-400 leading-relaxed">
        Per-provider dispatch settings. <b>Effort matrix</b> is the main knob:
        each (provider, effort level) cell defines the model + settings used
        when a task routes there. <b>Defaults</b> is the effort-independent
        base (rarely needed). <b>Overrides</b> pin per-(task, provider) tweaks
        that win over the matrix.
      </div>

      {/* Scope tabs: Matrix | Defaults | Overrides */}
      <div className="flex items-center gap-2 border-b border-neutral-800 pb-2">
        <ScopeTab active={scope === 'matrix'} onClick={() => setScope('matrix')}>
          Effort matrix
        </ScopeTab>
        <ScopeTab active={scope === 'defaults'} onClick={() => setScope('defaults')}>
          Defaults
        </ScopeTab>
        <ScopeTab active={scope === 'overrides'} onClick={() => setScope('overrides')}>
          Task-type overrides
        </ScopeTab>
      </div>

      {scope === 'matrix' && (
        <div className="flex items-center gap-2">
          <label className="text-xs text-neutral-400 w-20 shrink-0">Effort level</label>
          <div className="flex items-center gap-1">
            {EFFORT_LEVELS.map(level => (
              // reason: effort level tab in compact matrix toolbar; py-1 is operator-density convention
              <button
                data-viewport-allow-small-target
                key={level}
                onClick={() => setMatrixLevel(level)}
                className={`px-2.5 py-1 rounded text-xs transition-colors ${
                  matrixLevel === level
                    ? 'bg-emerald-900/40 text-emerald-300 border border-emerald-800'
                    : 'text-neutral-400 hover:text-neutral-200 border border-transparent'
                }`}
              >
                {level}
              </button>
            ))}
          </div>
        </div>
      )}

      {scope === 'overrides' && (
        <div className="flex items-center gap-2">
          <label className="text-xs text-neutral-400 w-20 shrink-0">Task type</label>
          <select
            value={selectedTaskType}
            onChange={e => setSelectedTaskType(e.target.value)}
            className="flex-1 bg-workbench-base border border-neutral-700 rounded px-2 py-1.5 text-xs text-neutral-100"
          >
            {providerRoutableOptions.map(t => (
              <option key={t.value} value={t.value}>{t.label}</option>
            ))}
          </select>
        </div>
      )}

      {/* Provider tabs */}
      <div className="flex items-center gap-1">
        {providerNames.map(name => (
          // reason: provider tab in compact settings toolbar; py-1.5 text-xs is operator-density convention
          <button
            data-viewport-allow-small-target
            key={name}
            onClick={() => setSelectedProvider(name)}
            className={`px-3 py-1.5 rounded text-xs transition-colors ${
              selectedProvider === name
                ? 'bg-workbench-panel text-neutral-100 border border-neutral-700'
                : 'text-neutral-400 hover:text-neutral-200 hover:bg-workbench-hover/50 border border-transparent'
            }`}
          >
            {name}
          </button>
        ))}
      </div>

      {/* Schema-driven form */}
      {currentSchema ? (
        <SchemaForm
          schema={currentSchema as SchemaNode}
          value={effectiveBag}
          onChange={handleBagChange}
          allowInherit={scope === 'overrides'}
        />
      ) : (
        <div className="text-xs text-neutral-500">
          Provider {selectedProvider} has no declared settings schema.
        </div>
      )}

      {/* Preview-model fallback warning. Only relevant for Gemini today —
          OpenAI's preview gating doesn't have a clean GA fallback map. */}
      {selectedProvider === 'gemini' && (
        <PreviewFallbackBanner
          selectedModel={typeof effectiveBag.model === 'string' ? effectiveBag.model : null}
          accounts={maxPlanAccounts}
        />
      )}

      {scope === 'overrides' && Object.keys(effectiveBag).length === 0 && (
        <div className="text-ui-md text-neutral-500 italic">
          No overrides for {selectedTaskType} / {selectedProvider}. Fields below
          will create an override when you set them.
        </div>
      )}
    </div>
  );
}

function ScopeTab({ active, onClick, children }: { active: boolean; onClick: () => void; children: React.ReactNode }) {
  return (
    // reason: scope tab in compact provider settings toolbar; py-1.5 text-xs is operator-density convention
    <button
      data-viewport-allow-small-target
      onClick={onClick}
      className={`px-3 py-1.5 rounded text-xs transition-colors ${
        active
          ? 'bg-workbench-panel text-neutral-100'
          : 'text-neutral-400 hover:text-neutral-200'
      }`}
    >
      {children}
    </button>
  );
}

// ─── Schema renderer ────────────────────────────────────────────────────────
// Walks an object schema's properties, rendering each field by type/enum.
// The top-level schema must be `type: object`; nested objects recurse.
// `allowInherit=true` (overrides pane) gives each field an "override" checkbox;
// unchecked fields are absent from the bag (inherit defaults at dispatch).

interface SchemaFormProps {
  schema: SchemaNode;
  value: ProviderSettingsBag;
  onChange: (next: ProviderSettingsBag) => void;
  allowInherit: boolean;
}

// Cross-field constraints: some fields only make sense in combination with
// specific values of other fields. Returning a non-null string disables the
// field in the UI and shows the reason; the backend will also ignore it, but
// catching it at the UI layer keeps the operator from setting a dead knob.
function crossFieldLock(key: string, bag: ProviderSettingsBag): string | null {
  // Anthropic: advisor only makes sense with a non-advisor-class executor,
  // because an opus/fable executor + opus advisor is silently ignored by
  // settings_to_cli.
  if (
    key === 'advisor' &&
    (bag.model === 'claude-fable-5' || bag.model === 'claude-opus-4-8' || bag.model === 'claude-opus-4-7')
  ) {
    return 'Advisor only applies when the executor model is sonnet or haiku.';
  }
  return null;
}

// Per-enum-value gating: when some enum options are only valid in combination
// with another field, return { [enumValue]: reason } so the renderer can
// disable just those options rather than locking the whole field. Backend
// drops incompatible values at dispatch; this just keeps the admin from
// picking something we know will be dropped.
function crossFieldEnumDisable(key: string, bag: ProviderSettingsBag): Record<string, string> {
  // OpenAI: service_tier=flex only works on flex-capable models (see
  // OPENAI_FLEX_SUPPORTED). Sending flex to anything else returns a 400
  // `Unsupported service_tier: flex` — mirror the backend's
  // _model_supports_flex gate so the picker never offers flex for an
  // incompatible model.
  if (key === 'service_tier' && !openaiModelSupportsFlex(bag.model)) {
    const modelLabel = typeof bag.model === 'string' && bag.model ? `\`${bag.model}\`` : 'the selected model';
    return { flex: `${modelLabel} does not accept service_tier=flex.` };
  }
  return {};
}

function SchemaForm({ schema, value, onChange, allowInherit }: SchemaFormProps) {
  if (schema.type !== 'object' || !schema.properties) {
    return null;
  }
  const properties = schema.properties;
  const keys = Object.keys(properties);
  return (
    <div className="space-y-2">
      {keys.map(key => {
        const propSchema = properties[key];
        const present = key in value;
        const currentVal = value[key];
        const lockReason = crossFieldLock(key, value);
        const disabledEnumValues = crossFieldEnumDisable(key, value);
        return (
          <FieldRow
            key={key}
            label={humanizeKey(key)}
            schema={propSchema}
            value={currentVal}
            present={present}
            allowInherit={allowInherit}
            lockReason={lockReason}
            disabledEnumValues={disabledEnumValues}
            onChange={(next, removed) => {
              if (removed) {
                const pruned = Object.fromEntries(
                  Object.entries(value).filter(([k]) => k !== key),
                ) as ProviderSettingsBag;
                onChange(pruned);
              } else {
                onChange({ ...value, [key]: next });
              }
            }}
          />
        );
      })}
    </div>
  );
}

interface FieldRowProps {
  label: string;
  schema: SchemaNode;
  value: unknown;
  present: boolean;
  allowInherit: boolean;
  lockReason?: string | null;
  disabledEnumValues?: Record<string, string>;
  onChange: (value: unknown, removed?: boolean) => void;
}

function FieldRow({ label, schema, value, present, allowInherit, lockReason, disabledEnumValues, onChange }: FieldRowProps) {
  const overridden = present;
  const locked = Boolean(lockReason);
  const effectiveDisabled = locked || (allowInherit && !overridden);
  const currentValueDisabledReason =
    typeof value === 'string' && disabledEnumValues ? disabledEnumValues[value] : undefined;

  const toggleOverride = () => {
    if (overridden) {
      onChange(undefined, true);
    } else {
      // Seeding default depending on shape so the control is usable immediately.
      let seed: unknown = '';
      if (schema.type === 'boolean') seed = false;
      else if (schema.type === 'integer' || schema.type === 'number') seed = 0;
      else if (schema.enum && schema.enum.length > 0) seed = schema.enum[0];
      else if (schema.type === 'array') seed = [];
      else if (schema.type === 'object') seed = {};
      onChange(seed);
    }
  };

  return (
    <div className="grid grid-cols-[160px_1fr_auto] gap-3 items-center py-1.5">
      <div className="flex items-center gap-1.5 pt-1.5">
        <label className="text-xs text-neutral-400 font-mono truncate" title={label}>
          {label}
        </label>
        {schema.description && (
          <span
            className="inline-flex items-center justify-center w-4 h-4 rounded-full border border-neutral-700 text-ui-xs text-neutral-500 hover:text-neutral-200 hover:border-neutral-500 cursor-help select-none"
            title={schema.description}
            aria-label={`Help: ${schema.description}`}
          >
            ?
          </span>
        )}
      </div>
      <div className={`${effectiveDisabled ? 'opacity-40 pointer-events-none' : ''}`}>
        <Field schema={schema} value={value} onChange={onChange} disabledEnumValues={disabledEnumValues} />
        {lockReason && (
          <div className="mt-1 text-ui-md text-amber-400 leading-snug italic">
            {lockReason}
          </div>
        )}
        {!lockReason && currentValueDisabledReason && (
          <div className="mt-1 text-ui-md text-amber-400 leading-snug italic">
            {currentValueDisabledReason} This value will be dropped at dispatch.
          </div>
        )}
      </div>
      {allowInherit && !locked && (
        // reason: inline inherit/override toggle in compact schema form row; text-ui-sm py-1 matches field row density
        <button
          data-viewport-allow-small-target
          onClick={toggleOverride}
          className={`text-ui-sm px-2 py-1 rounded border ${
            overridden
              ? 'border-emerald-700 text-emerald-300 bg-emerald-950/30'
              : 'border-neutral-700 text-neutral-500 hover:text-neutral-300'
          }`}
          title={overridden ? 'Remove override (inherit default)' : 'Create override'}
        >
          {overridden ? 'override' : 'inherit'}
        </button>
      )}
    </div>
  );
}

// Renders the appropriate control for one schema node. Nested objects render
// recursively inside a bordered box.
function Field({ schema, value, onChange, disabledEnumValues }: { schema: SchemaNode; value: unknown; onChange: (v: unknown) => void; disabledEnumValues?: Record<string, string> }) {
  // String with enum -> select
  if (schema.enum && schema.enum.length > 0 && (schema.type === 'string' || schema.type === undefined)) {
    return (
      <select
        value={(value as string | number) ?? ''}
        onChange={e => onChange(e.target.value)}
        className="w-full bg-workbench-base border border-neutral-700 rounded px-2 py-1 text-xs text-neutral-100"
      >
        {schema.enum.map(opt => {
          const reason = disabledEnumValues?.[String(opt)];
          return (
            <option
              key={String(opt)}
              value={opt}
              disabled={Boolean(reason)}
              title={reason}
            >
              {String(opt)}{reason ? ' (unsupported)' : ''}
            </option>
          );
        })}
      </select>
    );
  }

  if (schema.type === 'boolean') {
    return (
      // reason: boolean schema toggle switch; h-5 w-9 is standard toggle knob, not a tap target
      <button
        data-viewport-allow-small-target
        onClick={() => onChange(!value)}
        className={`relative inline-flex h-5 w-9 items-center rounded-full transition-colors ${value ? 'bg-emerald-600' : 'bg-workbench-layer'}`}
      >
        <span className={`inline-block h-3.5 w-3.5 rounded-full bg-white transition-transform ${value ? 'translate-x-[18px]' : 'translate-x-[3px]'}`} />
      </button>
    );
  }

  if (schema.type === 'integer' || schema.type === 'number') {
    return (
      <input
        type="number"
        value={(value as number) ?? ''}
        step={schema.type === 'integer' ? 1 : 'any'}
        onChange={e => {
          const raw = e.target.value;
          if (raw === '') { onChange(undefined); return; }
          const n = schema.type === 'integer' ? parseInt(raw, 10) : parseFloat(raw);
          if (!Number.isNaN(n)) onChange(n);
        }}
        className="w-full bg-workbench-base border border-neutral-700 rounded px-2 py-1 text-xs text-neutral-100"
      />
    );
  }

  if (schema.type === 'array' && schema.items?.type === 'string') {
    // Comma-separated editor. Arrays of scalars are the only shape we expose
    // so far (Anthropic.betas). Expand to richer editors when we surface more.
    const arr = Array.isArray(value) ? (value as string[]) : [];
    return (
      <input
        type="text"
        value={arr.join(', ')}
        onChange={e => {
          const parts = e.target.value.split(',').map(s => s.trim()).filter(Boolean);
          onChange(parts);
        }}
        placeholder="comma-separated"
        className="w-full bg-workbench-base border border-neutral-700 rounded px-2 py-1 text-xs text-neutral-100 font-mono"
      />
    );
  }

  if (schema.type === 'object' && schema.properties) {
    const asBag = (typeof value === 'object' && value !== null) ? value as ProviderSettingsBag : {};
    return (
      <div className="border border-neutral-800 rounded p-2 space-y-1.5 bg-workbench-base/30">
        <SchemaForm
          schema={schema}
          value={asBag}
          onChange={next => onChange(next)}
          allowInherit={false}
        />
      </div>
    );
  }

  // Fallback: free-text string. When the schema declares `examples`, wire a
  // <datalist> so the user gets autocomplete suggestions without losing the
  // ability to type a custom value (useful for preview-model names, etc).
  const examples = schema.examples ?? [];
  const hasExamples = examples.length > 0;
  const listId = hasExamples
    ? `datalist-${examples.slice(0, 3).join('-').replace(/[^a-zA-Z0-9-]/g, '')}`
    : undefined;
  return (
    <>
      <input
        type="text"
        value={(value as string) ?? ''}
        onChange={e => onChange(e.target.value || undefined)}
        list={listId}
        className="w-full bg-workbench-base border border-neutral-700 rounded px-2 py-1 text-xs text-neutral-100"
      />
      {hasExamples && (
        <datalist id={listId}>
          {examples.map(ex => <option key={ex} value={ex} />)}
        </datalist>
      )}
    </>
  );
}

function humanizeKey(key: string): string {
  return key.replace(/_/g, ' ');
}


// Preview-model fallback banner.
//
// When the operator picks a Gemini preview model, we partition their Gemini
// accounts into three buckets by looking at the cached `available_models`:
//   - will-run:       model is listed → task runs with the preview model
//   - will-fallback:  model is NOT listed → dispatcher swaps to GA counterpart
//                     (gemini-3.1-pro-preview → gemini-2.5-pro, etc)
//   - unknown:        account hasn't been probed yet → may or may not work
//                     (fail-open at dispatch time)
//
// The banner only renders when there's something actionable to surface:
// either at least one account will fall back, or we have no idea what any
// of the accounts can see. A fully-green "all accounts have access" state
// is silent — no banner clutter for the happy path.
function PreviewFallbackBanner({
  selectedModel,
  accounts,
}: {
  selectedModel: string | null;
  accounts: MaxPlanAccount[];
}) {
  if (!selectedModel || !(selectedModel in GEMINI_PREVIEW_FALLBACK)) {
    return null;
  }
  const ga = GEMINI_PREVIEW_FALLBACK[selectedModel];

  const gemini = accounts.filter(
    a => a.provider === 'gemini' && a.auth_mode === 'api_key' && a.enabled,
  );
  if (gemini.length === 0) {
    return null;
  }

  const willRun: MaxPlanAccount[] = [];
  const willFallback: MaxPlanAccount[] = [];
  const unknown: MaxPlanAccount[] = [];
  for (const a of gemini) {
    const md = a.provider_metadata || {};
    const models = Array.isArray(md.available_models) ? md.available_models : null;
    if (models === null) {
      unknown.push(a);
    } else if (models.includes(selectedModel)) {
      willRun.push(a);
    } else {
      willFallback.push(a);
    }
  }

  // Happy path: every enabled Gemini key has been probed and every one of them
  // has access to the preview model. Don't render anything.
  if (willFallback.length === 0 && unknown.length === 0) {
    return null;
  }

  const nameList = (accts: MaxPlanAccount[]) => accts.map(a => a.name).join(', ');

  return (
    <div className="border border-amber-900/60 bg-amber-950/20 rounded p-3 text-ui-md space-y-1.5">
      <div className="text-amber-300 font-medium">
        {selectedModel} is a preview model — dispatcher will fall back to{' '}
        <span className="font-mono">{ga}</span> for accounts without preview access.
      </div>
      {willFallback.length > 0 && (
        <div className="text-amber-200/80">
          <span className="text-amber-400">{willFallback.length}/{gemini.length}</span> account{willFallback.length === 1 ? '' : 's'} will fall back:{' '}
          <span className="font-mono text-neutral-300 break-all">{nameList(willFallback)}</span>
        </div>
      )}
      {willRun.length > 0 && (
        <div className="text-neutral-400">
          <span className="text-emerald-400">{willRun.length}/{gemini.length}</span> will run the preview model:{' '}
          <span className="font-mono break-all">{nameList(willRun)}</span>
        </div>
      )}
      {unknown.length > 0 && (
        <div className="text-neutral-500">
          <span className="text-neutral-400">{unknown.length}/{gemini.length}</span> not yet probed (click refresh on the account row to check):{' '}
          <span className="font-mono break-all">{nameList(unknown)}</span>
        </div>
      )}
    </div>
  );
}
