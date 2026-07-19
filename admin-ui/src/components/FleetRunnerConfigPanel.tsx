import { useMemo, useState } from 'react';
import { Check, Loader2 } from 'lucide-react';
import { updateRunnerCapabilities } from '../api';
import type { FleetRunner } from '../api';
import {
  GPUS_PER_JOB_MIN,
  GPUS_PER_JOB_MAX,
  MAX_CONCURRENT_MIN,
  MAX_CONCURRENT_MAX,
  runnerPinsStrategicConfig,
  maxConcurrentValue,
  maxConcurrentComputeValue,
  gpuAllowlistValue,
  parseGpuIndexList,
  gpusPerJobValue,
  taskTypesValue,
  parseTaskTypeList,
} from './FleetRunnersPanel.utils';

// The values the PATCH /capabilities endpoint accepts. Only fields the operator
// actually changed are sent, so an unchanged field is never rewritten.
interface CapabilityChanges {
  max_concurrent?: number;
  max_concurrent_compute?: number;
  task_types?: 'all' | string[];
  gpu_indices_allowed?: 'all' | number[];
  gpus_per_job?: number;
}

type SaveState =
  | { kind: 'idle' }
  | { kind: 'saving' }
  | { kind: 'saved' }
  | { kind: 'error'; message: string };

function jsonEq(a: unknown, b: unknown): boolean {
  return JSON.stringify(a) === JSON.stringify(b);
}

interface Props {
  projectId: string;
  runner: FleetRunner;
  /** Reload the runner list after a successful save so the header badges and
   *  desired-vs-applied status reflect the change. */
  onSaved: () => void | Promise<void>;
}

/**
 * One coherent per-runner configuration form. Replaces the scattered inline
 * edit-pencils that previously lived in the runner row header. Every control
 * here maps 1:1 to a field the daemon (scripts/local_runner.py) actually
 * consumes via the server-authoritative desired-config the runner converges to
 * on its next heartbeat — the field names and descriptions are written for an
 * operator who has never read the source.
 *
 * Agent concurrency and task-types are shown read-only for strategic runners, matching
 * the backend invariant that pins them (max_concurrent→1, task_types→strategic
 * set); editing them there would silently no-op, so we say so instead.
 */
export function FleetRunnerConfigPanel({ projectId, runner, onSaved }: Props) {
  const strategic = runnerPinsStrategicConfig(runner);
  const hasGpu = runner.capabilities?.has_gpu === true;

  const current = useMemo(
    () => ({
      maxConcurrent: maxConcurrentValue(runner),
      maxConcurrentCompute: maxConcurrentComputeValue(runner),
      taskTypes: taskTypesValue(runner),
      gpuAllowlist: gpuAllowlistValue(runner),
      gpusPerJob: gpusPerJobValue(runner),
    }),
    [runner],
  );

  // Form state, seeded from the runner's current server-authoritative config.
  const [maxConcurrent, setMaxConcurrent] = useState<number>(current.maxConcurrent);
  const [maxConcurrentCompute, setMaxConcurrentCompute] = useState<number>(current.maxConcurrentCompute);
  const [taskTypesAll, setTaskTypesAll] = useState<boolean>(current.taskTypes === 'all');
  const [taskTypesText, setTaskTypesText] = useState<string>(
    current.taskTypes === 'all' ? '' : current.taskTypes.join(', '),
  );
  const [gpuAll, setGpuAll] = useState<boolean>(current.gpuAllowlist === 'all');
  const [gpuText, setGpuText] = useState<string>(
    current.gpuAllowlist === 'all' ? '' : current.gpuAllowlist.join(','),
  );
  const [gpusPerJob, setGpusPerJob] = useState<number>(current.gpusPerJob);
  const [save, setSave] = useState<SaveState>({ kind: 'idle' });

  const changes = useMemo<CapabilityChanges>(() => {
    const out: CapabilityChanges = {};
    if (!strategic) {
      if (maxConcurrent !== current.maxConcurrent) out.max_concurrent = maxConcurrent;
      const nextTaskTypes: 'all' | string[] = taskTypesAll
        ? 'all'
        : (() => {
            const list = parseTaskTypeList(taskTypesText);
            return list.length === 0 ? 'all' : list;
          })();
      if (!jsonEq(nextTaskTypes, current.taskTypes)) out.task_types = nextTaskTypes;
    }
    if (maxConcurrentCompute !== current.maxConcurrentCompute) {
      out.max_concurrent_compute = maxConcurrentCompute;
    }
    if (hasGpu) {
      const nextGpu: 'all' | number[] = gpuAll ? 'all' : parseGpuIndexList(gpuText);
      if (!jsonEq(nextGpu, current.gpuAllowlist)) out.gpu_indices_allowed = nextGpu;
      if (gpusPerJob !== current.gpusPerJob) out.gpus_per_job = gpusPerJob;
    }
    return out;
  }, [strategic, hasGpu, maxConcurrent, maxConcurrentCompute, taskTypesAll, taskTypesText, gpuAll, gpuText, gpusPerJob, current]);

  const dirty = Object.keys(changes).length > 0;

  const handleSave = async () => {
    if (!dirty || save.kind === 'saving') return;
    setSave({ kind: 'saving' });
    try {
      await updateRunnerCapabilities(projectId, runner.id, changes);
      if (changes.max_concurrent !== undefined && changes.max_concurrent_compute === undefined
        && runner.capabilities.max_concurrent_compute == null) {
        setMaxConcurrentCompute(changes.max_concurrent);
      }
      setSave({ kind: 'saved' });
      await onSaved();
    } catch (e) {
      setSave({ kind: 'error', message: e instanceof Error ? e.message : String(e) });
    }
  };

  return (
    <div
      className="space-y-3 border-t border-neutral-800 bg-[#0b0f14] px-3 py-3"
      data-testid="fleet-runner-config-panel"
    >
      <div className="text-ui-md font-semibold uppercase tracking-wide text-neutral-400">
        Runner configuration
      </div>
      <p className="text-ui-md text-neutral-500">
        Changes apply on the runner’s next heartbeat — no restart needed. The status badge
        above flips to <span className="text-emerald-300">config applied</span> once the daemon
        confirms it.
      </p>

      {/* Max concurrent tasks */}
      <ConfigField
        label="Max concurrent tasks"
        description="How many agent tasks this runner works at the same time. Higher uses more of the host's CPU and memory."
      >
        {strategic ? (
          <ReadOnlyValue value="1" note="Pinned to 1 for strategic runners." />
        ) : (
          <input
            type="number"
            min={MAX_CONCURRENT_MIN}
            max={MAX_CONCURRENT_MAX}
            value={maxConcurrent}
            onChange={e =>
              setMaxConcurrent(
                Math.max(
                  MAX_CONCURRENT_MIN,
                  Math.min(MAX_CONCURRENT_MAX, Math.trunc(Number(e.target.value) || MAX_CONCURRENT_MIN)),
                ),
              )
            }
            aria-label="Max concurrent tasks"
            className="w-20 rounded border border-neutral-700 bg-neutral-950 px-2 py-1 text-sm text-neutral-200 outline-none focus:border-neutral-500"
          />
        )}
      </ConfigField>

      <ConfigField
        label="Max concurrent compute jobs"
        description="How many compute jobs this runner works at the same time. This is a separate pool from agent tasks."
      >
        <input
          type="number"
          min={MAX_CONCURRENT_MIN}
          max={MAX_CONCURRENT_MAX}
          value={maxConcurrentCompute}
          onChange={e =>
            setMaxConcurrentCompute(
              Math.max(
                MAX_CONCURRENT_MIN,
                Math.min(MAX_CONCURRENT_MAX, Math.trunc(Number(e.target.value) || MAX_CONCURRENT_MIN)),
              ),
            )
          }
          aria-label="Max concurrent compute jobs"
          className="w-20 rounded border border-neutral-700 bg-neutral-950 px-2 py-1 text-sm text-neutral-200 outline-none focus:border-neutral-500"
        />
      </ConfigField>

      {/* Task types */}
      <ConfigField
        label="Task types"
        description="Which kinds of task this runner accepts. Leave on “All” unless you want to restrict it — e.g. only compute_experiment for a GPU box."
      >
        {strategic ? (
          <ReadOnlyValue
            value={current.taskTypes === 'all' ? 'all' : current.taskTypes.join(', ')}
            note="Pinned to the strategic task set for strategic runners."
          />
        ) : (
          <div className="space-y-1.5">
            <label className="inline-flex items-center gap-1.5 text-sm text-neutral-300">
              <input
                type="checkbox"
                checked={taskTypesAll}
                onChange={e => setTaskTypesAll(e.target.checked)}
                aria-label="Allow all task types"
              />
              All task types
            </label>
            {!taskTypesAll && (
              <input
                type="text"
                value={taskTypesText}
                onChange={e => setTaskTypesText(e.target.value)}
                placeholder="compute_experiment, code_change"
                aria-label="Task types allowed"
                className="w-full max-w-md rounded border border-neutral-700 bg-neutral-950 px-2 py-1 text-sm text-neutral-200 outline-none focus:border-neutral-500"
              />
            )}
          </div>
        )}
      </ConfigField>

      {/* GPU controls — only meaningful on a runner that reported a GPU. */}
      {hasGpu && (
        <>
          <ConfigField
            label="GPU allowlist"
            description="Restrict which GPU indices this runner may use for jobs. “All” lets it use every GPU the host exposes."
          >
            <div className="space-y-1.5">
              <label className="inline-flex items-center gap-1.5 text-sm text-neutral-300">
                <input
                  type="checkbox"
                  checked={gpuAll}
                  onChange={e => setGpuAll(e.target.checked)}
                  aria-label="Allow all GPUs"
                />
                All GPUs
              </label>
              {!gpuAll && (
                <input
                  type="text"
                  inputMode="numeric"
                  value={gpuText}
                  onChange={e => setGpuText(e.target.value)}
                  placeholder="0,1"
                  aria-label="GPU indices allowed"
                  className="w-full max-w-xs rounded border border-neutral-700 bg-neutral-950 px-2 py-1 text-sm text-neutral-200 outline-none focus:border-neutral-500"
                />
              )}
            </div>
          </ConfigField>

          <ConfigField
            label="GPUs per job"
            description={`How many GPUs each compute job reserves on this runner (${GPUS_PER_JOB_MIN}–${GPUS_PER_JOB_MAX}). Set higher only for multi-GPU workloads.`}
          >
            <input
              type="number"
              min={GPUS_PER_JOB_MIN}
              max={GPUS_PER_JOB_MAX}
              value={gpusPerJob}
              onChange={e =>
                setGpusPerJob(
                  Math.max(
                    GPUS_PER_JOB_MIN,
                    Math.min(GPUS_PER_JOB_MAX, Math.trunc(Number(e.target.value) || GPUS_PER_JOB_MIN)),
                  ),
                )
              }
              aria-label="GPUs per job"
              className="w-20 rounded border border-neutral-700 bg-neutral-950 px-2 py-1 text-sm text-neutral-200 outline-none focus:border-neutral-500"
            />
          </ConfigField>
        </>
      )}

      {/* SEAM: the multi-project allow-list section (direction d-95b10147) mounts
          here — a "Projects" ConfigField letting an operator pick which projects
          this runner serves. It ships separately; this panel is the surface it
          slots into. */}

      <div className="flex items-center gap-3 pt-1">
        <button
          type="button"
          onClick={() => void handleSave()}
          disabled={!dirty || save.kind === 'saving'}
          aria-label="Save runner configuration"
          data-testid="fleet-runner-config-save"
          className="inline-flex min-h-11 items-center gap-1.5 rounded-md border border-emerald-500/30 bg-emerald-500/10 px-3 text-sm font-semibold text-emerald-300 transition-colors hover:bg-emerald-500/15 disabled:cursor-not-allowed disabled:opacity-40 md:min-h-9"
        >
          {save.kind === 'saving' ? (
            <Loader2 className="h-3.5 w-3.5 animate-spin" />
          ) : (
            <Check className="h-3.5 w-3.5" />
          )}
          {save.kind === 'saving' ? 'Saving…' : 'Save changes'}
        </button>
        <span className="text-ui-md" data-testid="fleet-runner-config-feedback">
          {save.kind === 'error' ? (
            <span className="text-red-300">Save failed: {save.message}</span>
          ) : save.kind === 'saved' && !dirty ? (
            <span className="text-emerald-300">Saved — applying on next heartbeat</span>
          ) : dirty ? (
            <span className="text-amber-300">Unsaved changes</span>
          ) : (
            <span className="text-neutral-600">No changes</span>
          )}
        </span>
      </div>
    </div>
  );
}

function ConfigField({
  label,
  description,
  children,
}: {
  label: string;
  description: string;
  children: React.ReactNode;
}) {
  return (
    <div className="grid gap-1.5 md:grid-cols-[16rem_1fr] md:items-start md:gap-3">
      <div className="min-w-0">
        <div className="text-sm font-medium text-neutral-200">{label}</div>
        <div className="mt-0.5 text-ui-md text-neutral-500">{description}</div>
      </div>
      <div>{children}</div>
    </div>
  );
}

function ReadOnlyValue({ value, note }: { value: string; note: string }) {
  return (
    <div className="space-y-0.5">
      <div className="rounded border border-neutral-800 bg-neutral-950/60 px-2 py-1 text-sm text-neutral-400">
        {value}
      </div>
      <div className="text-ui-sm text-neutral-600">{note}</div>
    </div>
  );
}
