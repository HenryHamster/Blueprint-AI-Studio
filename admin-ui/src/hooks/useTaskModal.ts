/* eslint-disable react-hooks/refs, react-hooks/set-state-in-effect */
import { useEffect, useState } from 'react';
import type React from 'react';
import { API_BASE_URL, authHeaders, authGet, fetchTaskTypeCatalog, createComputeExperiment, computeExperimentPreflight, listFleetRunners } from '../api';
import { usePersistedState } from './usePersistedState';
import type { ComputeSuccessOperator, ComputeExperimentLaunchRequest, ComputeSuccessCriterion, ComputeExperimentPreflightResponse, FleetRunner } from '../api';
import type { LaunchableTaskTypeOption, UserLaunchableTaskType } from '../taskCapabilities';
import { USER_LAUNCHABLE_TASK_TYPE_IDS, USER_LAUNCHABLE_TASK_TYPE_OPTIONS, resolveTaskCapabilities } from '../taskCapabilities';
import type { TaskCapabilityContract } from '../types/settings';
import type { Task } from '../types/task';

interface RepoOption {
  full_name: string;
  default_branch: string;
}

// The dedicated compute_experiment launch type. Submitting it routes to the
// contract endpoint instead of the generic POST /api/admin/tasks.
export const COMPUTE_EXPERIMENT_TASK_TYPE = 'compute_experiment';

// Code-producing task payload types — mirrors backend payload_schemas
// _CODE_PRODUCING_TASK_TYPES. The backend hard-requires an acceptance_evidence
// declaration on these at authoring time (validate_task_payload with
// require_acceptance_evidence=True), so the operator launcher must collect and
// send one; a code payload submitted without it is rejected with a 422.
export const CODE_PRODUCING_TASK_TYPES: readonly string[] = [
  'create', 'fix', 'refactor', 'qa', 'migrate', 'deps', 'docs',
];

export function isCodeProducingTaskType(taskType: string): boolean {
  return CODE_PRODUCING_TASK_TYPES.includes(taskType);
}

// Acceptance-evidence kind options rendered by the New Task modal, mirroring the
// backend AcceptanceEvidence kind vocabulary (payload_schemas.py).
export const ACCEPTANCE_EVIDENCE_KINDS = [
  { value: 'named_test', label: 'Named test', description: 'A test that exercises the changed seam' },
  { value: 'observable_behavior', label: 'Observable behavior', description: 'A behavior to verify' },
  { value: 'downstream_artifact', label: 'Downstream artifact', description: 'An artifact a later task consumes' },
  { value: 'visual', label: 'Visual', description: 'Before/after screenshot' },
  { value: 'coverage_delta', label: 'Coverage delta', description: 'Coverage percentage improvement' },
] as const;

export type AcceptanceEvidenceKind = typeof ACCEPTANCE_EVIDENCE_KINDS[number]['value'];

// Operator-facing form state for the acceptance_evidence declaration, held as
// strings (raw input) and validated/coerced at submit time — mirroring the
// compute_experiment contract form pattern.
export interface AcceptanceEvidenceForm {
  kind: AcceptanceEvidenceKind;
  testPath: string;
  testId: string;
  description: string;
  artifactId: string;
  component: string;
  viewportWidth: string;
  assertionType: string;
  threshold: string;
}

export function defaultAcceptanceEvidenceForm(): AcceptanceEvidenceForm {
  return {
    kind: 'named_test',
    testPath: '',
    testId: '',
    description: '',
    artifactId: '',
    component: '',
    viewportWidth: '375',
    assertionType: '',
    threshold: '',
  };
}

// Light mirror of the backend test-file heuristic (payload_schemas
// _looks_like_test_file): a path under a tests/testing-style directory OR a
// basename matching the repo's test-file naming, with a real extension. Used to
// guide the operator before round-tripping a doomed declaration.
const TEST_DIR_SEGMENTS = new Set(['tests', 'testing', 'test', '__tests__', '__test__', 'spec']);
const TEST_FILE_BASENAME_RE = /^(?:test_.*\.py|.*_test\.py|.*\.test\.[cm]?[jt]sx?|.*\.spec\.[cm]?[jt]sx?|.*_test\.go|.*_test\.exs?|.*_spec\.rb|test_.*\.rb)$/;

export function looksLikeTestFile(path: string): boolean {
  const candidate = path.trim();
  if (!candidate) return false;
  const segments = candidate.replace(/\\/g, '/').split('/').filter(Boolean);
  if (segments.length === 0) return false;
  const basename = segments[segments.length - 1];
  if (!basename.includes('.')) return false;
  const inTestDir = segments.slice(0, -1).some(seg => TEST_DIR_SEGMENTS.has(seg.toLowerCase()));
  const nameMatches = TEST_FILE_BASENAME_RE.test(basename.toLowerCase());
  return inTestDir || nameMatches;
}

// Client-side mirror of the backend AcceptanceEvidence._check_kind_shape rules.
// Returns null when the declaration is valid, else an actionable message shown
// inline so a code-producing submission is never sent with a partial field.
export function validateAcceptanceEvidenceForm(form: AcceptanceEvidenceForm): string | null {
  switch (form.kind) {
    case 'named_test':
      if (!looksLikeTestFile(form.testPath)) {
        return 'Name a real test file (e.g. tests/test_foo.py or src/foo.test.ts) that exercises the change.';
      }
      return null;
    case 'observable_behavior':
      if (form.description.trim().length < 10) {
        return 'Describe the observable behavior to verify (at least 10 characters).';
      }
      return null;
    case 'downstream_artifact':
      if (!form.artifactId.trim()) {
        return 'Name the downstream artifact a later task consumes (e.g. gs://bucket/path or a job id).';
      }
      return null;
    case 'visual': {
      const width = Number(form.viewportWidth);
      if (!form.component.trim()) {
        return 'Name the UI component to capture.';
      }
      if (!Number.isInteger(width) || width < 1 || width > 4096) {
        return 'Viewport width must be an integer between 1 and 4096.';
      }
      return null;
    }
    case 'coverage_delta': {
      const threshold = Number(form.threshold);
      if (!Number.isFinite(threshold) || threshold < 0 || threshold > 100) {
        return 'Coverage threshold must be a number between 0 and 100.';
      }
      return null;
    }
    default:
      return 'Select an acceptance-evidence kind.';
  }
}

// Build the wire shape from the form; returns null when the declaration is
// incomplete so a code-producing submission is blocked rather than sent with a
// partial field (the backend would reject it with a 422).
export function buildAcceptanceEvidence(form: AcceptanceEvidenceForm): Record<string, unknown> | null {
  if (validateAcceptanceEvidenceForm(form) !== null) return null;
  switch (form.kind) {
    case 'named_test': {
      const evidence: Record<string, unknown> = { kind: 'named_test', test_path: form.testPath.trim() };
      const testId = form.testId.trim();
      if (testId) evidence.test_id = testId;
      return evidence;
    }
    case 'observable_behavior':
      return { kind: 'observable_behavior', description: form.description.trim() };
    case 'downstream_artifact':
      return { kind: 'downstream_artifact', artifact_id: form.artifactId.trim() };
    case 'visual': {
      const evidence: Record<string, unknown> = {
        kind: 'visual',
        component: form.component.trim(),
        viewport_width: Number(form.viewportWidth),
      };
      const assertionType = form.assertionType.trim();
      if (assertionType) evidence.assertion_type = assertionType;
      return evidence;
    }
    case 'coverage_delta':
      return { kind: 'coverage_delta', threshold: Number(form.threshold) };
    default:
      return null;
  }
}

// Rehydrate the form from a stored task payload's acceptance_evidence dict.
export function acceptanceEvidenceFormFromPayload(raw: unknown): AcceptanceEvidenceForm {
  const base = defaultAcceptanceEvidenceForm();
  if (!raw || typeof raw !== 'object') return base;
  const ae = raw as Record<string, unknown>;
  const kind = ae.kind;
  if (
    kind !== 'named_test' && kind !== 'observable_behavior' && kind !== 'downstream_artifact'
    && kind !== 'visual' && kind !== 'coverage_delta'
  ) {
    return base;
  }
  return {
    kind,
    testPath: typeof ae.test_path === 'string' ? ae.test_path : '',
    testId: typeof ae.test_id === 'string' ? ae.test_id : '',
    description: typeof ae.description === 'string' ? ae.description : '',
    artifactId: typeof ae.artifact_id === 'string' ? ae.artifact_id : '',
    component: typeof ae.component === 'string' ? ae.component : '',
    viewportWidth: typeof ae.viewport_width === 'number' ? String(ae.viewport_width) : '375',
    assertionType: typeof ae.assertion_type === 'string' ? ae.assertion_type : '',
    threshold: typeof ae.threshold === 'number' ? String(ae.threshold) : '',
  };
}

// Operator-facing form state for the compute_experiment launch contract. Held
// as strings (raw input) and validated/coerced at submit time, mirroring how
// the PR-number field is kept as a string. The success-criterion sub-fields are
// all-or-nothing: leave them blank to launch without a machine-checkable goal.
export interface ComputeExperimentArm {
  name: string;
  command: string;
}

export interface ComputeExperimentForm {
  useArmsMode: boolean;
  arms: ComputeExperimentArm[];
  declaredMetric: string;
  command: string;
  cwd: string;
  envVars: string;
  accelerator: string;
  machineType: string;
  spendCapUsd: string;
  deadlineSeconds: string;
  experiment: string;
  benchmark: string;
  hypothesis: string;
  metric: string;
  successMetric: string;
  successOperator: ComputeSuccessOperator;
  successThreshold: string;
  requireLiveRelease: boolean;
  requireDurableArtifact: boolean;
  provisioningModel: 'STANDARD' | 'SPOT';
  // Optional container image the experiment runs inside (e.g. nvcr.io/nvidia/...).
  containerImage: string;
  // Optional private-registry credentials. password is write-only — never
  // prefilled from a stored payload (see openDuplicateTaskModal).
  containerRegistryServer: string;
  containerRegistryUsername: string;
  containerRegistryPassword: string;
  // Optional runner UUID or hostname to hard-pin this job to a specific runner.
  // Empty string means auto-select (no forced target).
  forcedRunnerId: string;
  // Optional setup step executed before the main command (bash -lc <value>).
  setupStep: string;
  // Optional no-progress watchdog timeout in seconds (empty = use default).
  noProgressTimeoutSeconds: string;
  // Optional checkpoint resume: prior compute job ID to rehydrate from.
  inputArtifactJobId: string;
}

export const COMPUTE_ACCELERATOR_OPTIONS = [
  { accelerator: 'T4', machineType: 'n1-standard-1' },
  { accelerator: 'L4', machineType: 'g2-standard-4' },
  { accelerator: 'V100', machineType: 'n1-standard-1' },
] as const;

export function canonicalMachineTypeForAccelerator(accelerator: string): string | null {
  const trimmed = accelerator.trim().toUpperCase();
  return COMPUTE_ACCELERATOR_OPTIONS.find(option => option.accelerator === trimmed)?.machineType ?? null;
}

export function defaultComputeExperimentForm(): ComputeExperimentForm {
  return {
    useArmsMode: false,
    arms: [{ name: '', command: '' }, { name: '', command: '' }],
    declaredMetric: '',
    command: '',
    cwd: '',
    envVars: '',
    accelerator: '',
    machineType: '',
    spendCapUsd: '',
    deadlineSeconds: '',
    experiment: '',
    benchmark: '',
    hypothesis: '',
    metric: '',
    successMetric: '',
    successOperator: '>=',
    successThreshold: '',
    requireLiveRelease: false,
    requireDurableArtifact: false,
    provisioningModel: 'STANDARD',
    containerImage: '',
    containerRegistryServer: '',
    containerRegistryUsername: '',
    containerRegistryPassword: '',
    forcedRunnerId: '',
    setupStep: '',
    noProgressTimeoutSeconds: '',
    inputArtifactJobId: '',
  };
}

export function useTaskModal(
  defaultTaskType: UserLaunchableTaskType,
  addToast: (msg: string, type: 'success' | 'error') => void,
  fetchTasksRef: React.MutableRefObject<() => void>,
  defaultTaskBranchRef?: React.MutableRefObject<string | null>,
  selectedProjectRepoRef?: React.MutableRefObject<string | null>,
  taskCapabilityContract?: TaskCapabilityContract | null,
  selectedProjectIdRef?: React.MutableRefObject<string | null>,
) {
  // Launchable set derives from the published contract (falling back to the
  // built-in table when the backend hasn't published it) so the duplicate
  // guard below honors a newly registered launchable type instead of
  // collapsing it to 'create'.
  const launchableTaskTypeIds: readonly string[] = taskCapabilityContract
    ? resolveTaskCapabilities(taskCapabilityContract).userLaunchableOptions.map(o => o.value)
    : USER_LAUNCHABLE_TASK_TYPE_IDS;
  const getDefaultTaskBranch = () => defaultTaskBranchRef?.current ?? null;
  const getSelectedProjectRepo = () => selectedProjectRepoRef?.current || null;
  const defaultTaskBranch = getDefaultTaskBranch();

  // Modal visibility
  const [showNewTaskModal, setShowNewTaskModal] = useState(false);
  // Tracks which opener populated the modal — 'new' persists drafts, 'duplicate' suppresses
  // them so prefilled source-task content doesn't overwrite the user's saved draft.
  const [modalOpener, setModalOpener] = useState<'new' | 'duplicate'>('new');

  // Form fields
  const [newTaskRepo, setNewTaskRepo] = useState('');
  const [newTaskBranch, setNewTaskBranch] = useState(defaultTaskBranch ?? '');
  const [newTaskTitle, setNewTaskTitle] = useState('');
  const [newTaskPrompt, setNewTaskPrompt] = useState('');
  const [newTaskType, setNewTaskType] = useState<string>('create');
  const [newTaskPrNumber, setNewTaskPrNumber] = useState('');
  const [newTaskPrBranch, setNewTaskPrBranch] = useState('');
  const [newTaskModel, setNewTaskModel] = useState('');
  const [newTaskEffort, setNewTaskEffort] = useState('');

  // compute_experiment launch-contract form state. Only consumed when
  // newTaskType === 'compute_experiment'; otherwise inert.
  const [computeExperiment, setComputeExperiment] =
    useState<ComputeExperimentForm>(defaultComputeExperimentForm);

  // acceptance_evidence declaration state. Only consumed when newTaskType is a
  // code-producing type (create / fix / refactor / migrate / ...); otherwise
  // inert. The backend hard-requires this field on those types, so the modal
  // collects it and handleCreateTask refuses to submit without a valid one.
  const [acceptanceEvidence, setAcceptanceEvidence] =
    useState<AcceptanceEvidenceForm>(defaultAcceptanceEvidenceForm);

  // Draft provenance — lifted from NewTaskModal so the hook can include them in
  // the launch request without prop threading through submit.
  const [ideaText, setIdeaText] = usePersistedState('compute-experiment-idea', '');
  const [draftNotes, setDraftNotes] = usePersistedState<string | null>('compute-experiment-draft-notes', null);
  // True when the operator applied an AI-drafted refinement before launching;
  // false for a purely hand-typed contract. Reset on each modal open.
  const [cameFromDraft, setCameFromDraft] = useState(false);

  // Preflight projection state — debounced on hardware/budget inputs.
  const [preflightResult, setPreflightResult] = useState<ComputeExperimentPreflightResponse | null>(null);
  const [preflightLoading, setPreflightLoading] = useState(false);
  const [preflightError, setPreflightError] = useState<string | null>(null);

  // GitHub repo/branch dropdown state
  const [repoOptions, setRepoOptions] = useState<RepoOption[]>([]);
  const [repoLoading, setRepoLoading] = useState(false);
  const [repoError, setRepoError] = useState<string | null>(null);
  const [useManualRepo, setUseManualRepo] = useState(false);
  const [branchOptions, setBranchOptions] = useState<string[]>([]);
  const [branchLoading, setBranchLoading] = useState(false);
  const [branchError, setBranchError] = useState<string | null>(null);
  const [useManualBranch, setUseManualBranch] = useState(false);

  // Fleet runners for the force-target picker — loaded on modal open when
  // selectedProjectIdRef is provided. Empty when unavailable or loading.
  const [fleetRunners, setFleetRunners] = useState<FleetRunner[]>([]);
  const [fleetRunnersLoading, setFleetRunnersLoading] = useState(false);

  // Launchable task-type options, derived from the backend task-type catalog so
  // a newly registered launchable type appears in the launcher with no frontend
  // edit. Seeded with (and falling back to) the static list so the dropdown is
  // never empty while the catalog loads or if the fetch fails.
  const [launchableTaskTypeOptions, setLaunchableTaskTypeOptions] =
    useState<LaunchableTaskTypeOption[]>(() => [...USER_LAUNCHABLE_TASK_TYPE_OPTIONS]);

  const fetchLaunchableTaskTypeOptions = async () => {
    try {
      const catalog = await fetchTaskTypeCatalog();
      // Surface both freely-launchable types and those that expose a dedicated
      // structured launch-contract path (compute_experiment) so the latter
      // appears in the launcher even though user_launchable stays false.
      const launchable = catalog
        .filter(entry => entry.user_launchable || entry.operator_contract_launchable)
        .map(entry => ({ value: entry.id, label: entry.label, description: entry.description }));
      // Only adopt the catalog when it yields options; an empty/garbled
      // response must not blank the dropdown — keep the static fallback.
      if (launchable.length > 0) {
        setLaunchableTaskTypeOptions(launchable);
      }
    } catch {
      // Catalog unreachable — retain the current (static) fallback options.
    }
  };

  useEffect(() => {
    if (!showNewTaskModal) {
      setNewTaskBranch(defaultTaskBranch ?? '');
    }
  }, [defaultTaskBranch, showNewTaskModal]);

  useEffect(() => {
    if (showNewTaskModal && defaultTaskBranch && !newTaskBranch) {
      setNewTaskBranch(defaultTaskBranch);
    }
  }, [defaultTaskBranch, newTaskBranch, showNewTaskModal]);

  // Debounced preflight projection: fires 500ms after hardware/budget inputs settle.
  // Stale in-flight requests are aborted via AbortController. On error the panel
  // degrades silently (preflightError is set) rather than blocking submission.
  useEffect(() => {
    if (newTaskType !== COMPUTE_EXPERIMENT_TASK_TYPE) {
      setPreflightResult(null);
      setPreflightLoading(false);
      setPreflightError(null);
      return;
    }
    const repo = newTaskRepo;
    if (!repo || !repo.includes('/')) {
      setPreflightResult(null);
      return;
    }
    const spendCap = Number(computeExperiment.spendCapUsd);
    const deadline = Number(computeExperiment.deadlineSeconds);
    if (!Number.isFinite(spendCap) || spendCap <= 0) {
      setPreflightResult(null);
      return;
    }
    if (!Number.isInteger(deadline) || deadline <= 0) {
      setPreflightResult(null);
      return;
    }
    const controller = new AbortController();
    const timer = setTimeout(() => {
      setPreflightLoading(true);
      setPreflightError(null);
      computeExperimentPreflight(
        {
          github_repo: repo,
          ...(computeExperiment.accelerator.trim() ? { accelerator: computeExperiment.accelerator.trim() } : {}),
          ...(computeExperiment.machineType.trim() ? { machine_type: computeExperiment.machineType.trim() } : {}),
          spend_cap_usd: spendCap,
          deadline_seconds: deadline,
          ...(computeExperiment.provisioningModel !== 'STANDARD' ? { provisioning_model: computeExperiment.provisioningModel } : {}),
        },
        controller.signal,
      ).then(result => {
        setPreflightResult(result);
        setPreflightLoading(false);
      }).catch(e => {
        if (e instanceof Error && e.name === 'AbortError') return;
        setPreflightError(e instanceof Error ? e.message : 'Preflight check failed');
        setPreflightLoading(false);
      });
    }, 500);
    return () => {
      clearTimeout(timer);
      controller.abort();
    };
  }, [newTaskType, newTaskRepo, computeExperiment.accelerator, computeExperiment.machineType, computeExperiment.spendCapUsd, computeExperiment.deadlineSeconds, computeExperiment.provisioningModel]);

  const getDefaultBranchForRepo = (repo: string) => {
    const repoMeta = repoOptions.find(r => r.full_name === repo);
    const selectedProjectRepo = getSelectedProjectRepo();
    const defaultTaskBranch = getDefaultTaskBranch();
    if (repo && repo === selectedProjectRepo) {
      return defaultTaskBranch ?? '';
    }
    return repoMeta?.default_branch || defaultTaskBranch || '';
  };

  const getDefaultModelForType = (taskType: string) => {
    return JSON.parse(localStorage.getItem(`blueprint-admin:taskTypeModel:${taskType}`) ?? 'null') ??
      JSON.parse(localStorage.getItem('blueprint-admin:defaultModel') ?? 'null') ?? '';
  };

  const getDefaultEffortForType = (taskType: string) => {
    return JSON.parse(localStorage.getItem(`blueprint-admin:taskTypeEffort:${taskType}`) ?? 'null') ??
      JSON.parse(localStorage.getItem('blueprint-admin:defaultEffort') ?? 'null') ?? '';
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

  const fetchFleetRunnersForProject = async () => {
    const projectId = selectedProjectIdRef?.current;
    if (!projectId) return;
    setFleetRunnersLoading(true);
    try {
      const runners = await listFleetRunners(projectId);
      setFleetRunners(runners);
    } catch {
      // Runner list is optional — degrade silently
    } finally {
      setFleetRunnersLoading(false);
    }
  };

  const getDraftStorageKey = (suffix: string) => {
    const projectRepo = getSelectedProjectRepo();
    return projectRepo ? `blueprint-admin:draft:${projectRepo}:${suffix}` : null;
  };

  const restoreDraft = () => {
    const titleKey = getDraftStorageKey('title');
    const promptKey = getDraftStorageKey('prompt');
    if (!titleKey || !promptKey) return;
    const savedTitle = localStorage.getItem(titleKey);
    const savedPrompt = localStorage.getItem(promptKey);
    if (savedTitle) setNewTaskTitle(savedTitle);
    if (savedPrompt) setNewTaskPrompt(savedPrompt);
  };

  const clearDraft = () => {
    const titleKey = getDraftStorageKey('title');
    const promptKey = getDraftStorageKey('prompt');
    if (!titleKey || !promptKey) return;
    localStorage.removeItem(titleKey);
    localStorage.removeItem(promptKey);
  };

  useEffect(() => {
    // Only persist drafts while the new-task modal is the active opener.
    // When opened as a duplicate, the title/prompt are prefilled from the source task,
    // and persisting them would clobber the user's saved draft (issue #6321 review).
    if (!showNewTaskModal || modalOpener !== 'new') return;
    const projectRepo = selectedProjectRepoRef?.current || null;
    if (!projectRepo) return;
    const timer = setTimeout(() => {
      const titleKey = `blueprint-admin:draft:${projectRepo}:title`;
      const promptKey = `blueprint-admin:draft:${projectRepo}:prompt`;
      localStorage.setItem(titleKey, newTaskTitle);
      localStorage.setItem(promptKey, newTaskPrompt);
    }, 500);
    return () => clearTimeout(timer);
  }, [newTaskTitle, newTaskPrompt, showNewTaskModal, modalOpener, selectedProjectRepoRef]);

  const onDraftApplied = () => { setCameFromDraft(true); };

  const openNewTaskModal = () => {
    setModalOpener('new');
    setShowNewTaskModal(true);
    setNewTaskType(defaultTaskType);
    setNewTaskModel(getDefaultModelForType(defaultTaskType));
    setNewTaskEffort(getDefaultEffortForType(defaultTaskType));
    setNewTaskBranch(getDefaultTaskBranch() ?? '');
    setUseManualRepo(false);
    setUseManualBranch(false);
    setBranchOptions([]);
    setBranchError(null);
    setComputeExperiment(defaultComputeExperimentForm());
    setAcceptanceEvidence(defaultAcceptanceEvidenceForm());
    setCameFromDraft(false);
    restoreDraft();
    fetchRepoOptions();
    fetchLaunchableTaskTypeOptions();
    fetchFleetRunnersForProject();
  };

  const handleRepoChange = (value: string) => {
    setNewTaskRepo(value);
    setNewTaskBranch(getDefaultBranchForRepo(value));
    if (value && value.includes('/')) {
      fetchBranchOptions(value);
    } else {
      setBranchOptions([]);
    }
  };

  // Build + validate the compute_experiment launch contract and POST it to the
  // dedicated endpoint. Mirrors the backend launch-contract rules client-side
  // (non-empty command, positive spend cap + deadline) so the operator gets an
  // immediate inline error instead of a round-trip rejection. Returns nothing;
  // surfaces all outcomes through addToast.
  const handleCreateComputeExperiment = async () => {
    // Block submission when a resolved preflight verdict marks the selected
    // hardware as outside the project's allowed set. A fetch error (preflightError
    // set, preflightResult null) degrades gracefully — the server still enforces
    // the policy, so we don't hard-block on a transient preflight failure.
    if (preflightResult && !preflightResult.in_policy) {
      const allowed =
        preflightResult.allowed_accelerator_types?.join(', ') ||
        preflightResult.allowed_machine_types?.join(', ') ||
        'none configured';
      addToast(
        `Hardware is outside this project's policy. Allowed: ${allowed}`,
        'error',
      );
      return;
    }
    const command = computeExperiment.command.trim();
    const usingArms = computeExperiment.useArmsMode;
    if (!usingArms && !command) {
      addToast('Command is required for compute experiments', 'error');
      return;
    }
    if (usingArms) {
      const validArms = computeExperiment.arms.filter(a => a.name.trim() && a.command.trim());
      if (validArms.length < 2) {
        addToast('At least two arms with name and command are required for multi-arm experiments', 'error');
        return;
      }
      if (!computeExperiment.declaredMetric.trim()) {
        addToast('Declared metric is required for multi-arm experiments', 'error');
        return;
      }
    }
    const spendCap = Number(computeExperiment.spendCapUsd);
    if (!Number.isFinite(spendCap) || spendCap <= 0) {
      addToast('Spend cap must be a positive number (USD)', 'error');
      return;
    }
    const deadline = Number(computeExperiment.deadlineSeconds);
    if (!Number.isInteger(deadline) || deadline <= 0) {
      addToast('Deadline must be a positive integer (seconds)', 'error');
      return;
    }
    // success_criterion is optional but all-or-nothing: a metric name without a
    // numeric threshold (or vice-versa) is a misconfiguration, not a partial.
    let successCriterion: ComputeSuccessCriterion | undefined;
    const successMetric = computeExperiment.successMetric.trim();
    const successThresholdRaw = computeExperiment.successThreshold.trim();
    if (successMetric || successThresholdRaw) {
      const threshold = Number(successThresholdRaw);
      if (!successMetric || !successThresholdRaw || !Number.isFinite(threshold)) {
        addToast('Success criterion needs a metric name and a numeric threshold', 'error');
        return;
      }
      successCriterion = {
        metric: successMetric,
        operator: computeExperiment.successOperator,
        threshold,
      };
    }
    const accelerator = computeExperiment.accelerator.trim();
    const machineType = computeExperiment.machineType.trim();
    const canonicalMachineType = accelerator ? canonicalMachineTypeForAccelerator(accelerator) : null;
    if (accelerator && (!canonicalMachineType || (machineType && machineType !== canonicalMachineType))) {
      addToast('Machine type must match the selected accelerator canonical shape', 'error');
      return;
    }
    const experiment = computeExperiment.experiment.trim();
    const benchmark = computeExperiment.benchmark.trim();
    const hypothesis = computeExperiment.hypothesis.trim();
    const metric = computeExperiment.metric.trim();
    const cwd = computeExperiment.cwd.trim();
    // Optional container image + private-registry credentials. The runner runs
    // the command inside container_image (docker run) and, when a password is
    // supplied, performs `docker login` first. Only send container_registry_auth
    // when a password is present; password requires server + username.
    const containerImage = computeExperiment.containerImage.trim();
    const registryServer = computeExperiment.containerRegistryServer.trim();
    const registryUsername = computeExperiment.containerRegistryUsername.trim();
    const registryPassword = computeExperiment.containerRegistryPassword;
    let containerRegistryAuth: { server: string; username: string; password: string } | undefined;
    if (registryPassword) {
      if (!registryServer || !registryUsername) {
        addToast('Registry credentials need a server and username when a password is set', 'error');
        return;
      }
      containerRegistryAuth = {
        server: registryServer,
        username: registryUsername,
        password: registryPassword,
      };
    }
    // Parse KEY=VALUE lines into an env record; skip blank lines and comments.
    const envRecord: Record<string, string> = {};
    for (const line of computeExperiment.envVars.split('\n')) {
      const trimmed = line.trim();
      if (!trimmed || trimmed.startsWith('#')) continue;
      const eqIdx = trimmed.indexOf('=');
      if (eqIdx < 1) continue;
      envRecord[trimmed.slice(0, eqIdx)] = trimmed.slice(eqIdx + 1);
    }
    const hasEnv = Object.keys(envRecord).length > 0;
    const validArms = computeExperiment.arms.filter(a => a.name.trim() && a.command.trim());
    const armsLabel = usingArms ? validArms.map(a => a.name).join(' vs ') : '';
    const title = newTaskTitle.trim() || (usingArms
      ? `Compute sweep: ${armsLabel.slice(0, 60)}`
      : `Compute experiment: ${command.slice(0, 60)}`);
    const prompt = newTaskPrompt.trim() || hypothesis || (usingArms
      ? `Run multi-arm compute experiment: ${armsLabel}`
      : `Run compute experiment: ${command}`);
    const body: ComputeExperimentLaunchRequest = {
      github_repo: newTaskRepo,
      base_branch: newTaskBranch,
      title,
      prompt,
      ...(usingArms
        ? { arms: validArms.map(a => ({ name: a.name.trim(), command: a.command.trim() })), declared_metric: computeExperiment.declaredMetric.trim() }
        : { command }),
      spend_cap_usd: spendCap,
      deadline_seconds: deadline,
      ...(accelerator ? { accelerator } : {}),
      ...(machineType ? { machine_type: machineType } : {}),
      ...(cwd ? { cwd } : {}),
      ...(hasEnv ? { env: envRecord } : {}),
      ...(experiment ? { experiment } : {}),
      ...(benchmark ? { benchmark } : {}),
      ...(hypothesis ? { hypothesis } : {}),
      ...(metric ? { metric } : {}),
      ...(containerImage ? { container_image: containerImage } : {}),
      ...(containerRegistryAuth ? { container_registry_auth: containerRegistryAuth } : {}),
      ...(successCriterion ? { success_criterion: successCriterion } : {}),
      ...(computeExperiment.requireLiveRelease ? { require_live_release: true } : {}),
      ...(computeExperiment.requireDurableArtifact
        ? { require_durable_artifact: true }
        : {}),
      ...(computeExperiment.provisioningModel !== 'STANDARD'
        ? { provisioning_model: computeExperiment.provisioningModel }
        : {}),
      ...(newTaskModel ? { model: newTaskModel } : {}),
      ...(newTaskEffort ? { effort: newTaskEffort } : {}),
      ...(ideaText.trim() ? { idea: ideaText.trim() } : {}),
      ...(draftNotes ? { draft_notes: draftNotes } : {}),
      ...(cameFromDraft ? { drafted_from_refinement: true } : {}),
      ...(computeExperiment.forcedRunnerId.trim() ? { runner_target_id: computeExperiment.forcedRunnerId.trim() } : {}),
      ...(computeExperiment.setupStep.trim() ? { setup_step: computeExperiment.setupStep.trim() } : {}),
      ...(computeExperiment.noProgressTimeoutSeconds.trim() && Number.isInteger(Number(computeExperiment.noProgressTimeoutSeconds)) && Number(computeExperiment.noProgressTimeoutSeconds) > 0
        ? { no_progress_timeout_seconds: Number(computeExperiment.noProgressTimeoutSeconds) }
        : {}),
      ...(computeExperiment.inputArtifactJobId.trim() ? { input_artifact_job_id: computeExperiment.inputArtifactJobId.trim() } : {}),
    };
    try {
      const response = await createComputeExperiment(body);
      setShowNewTaskModal(false);
      setNewTaskTitle('');
      setNewTaskPrompt('');
      setComputeExperiment(defaultComputeExperimentForm());
      clearDraft();
      if (response.requires_approval) {
        addToast('Compute experiment held for approval — it will not run until an operator approves it', 'success');
      } else if (response.is_duplicate) {
        addToast('Reusing an existing matching compute experiment', 'success');
      } else {
        addToast('Compute experiment launched', 'success');
      }
      fetchTasksRef.current();
    } catch (error) {
      const message = error instanceof Error ? error.message : 'unknown error';
      addToast(`Failed to launch compute experiment: ${message}`, 'error');
    }
  };

  const handleCreateTask = async (e: React.FormEvent) => {
    e.preventDefault();
    if (newTaskType === COMPUTE_EXPERIMENT_TASK_TYPE) {
      await handleCreateComputeExperiment();
      return;
    }
    const normalizedPrBranch = newTaskPrBranch.trim();
    if (newTaskType === 'review' || newTaskType === 'fix') {
      const prNum = parseInt(newTaskPrNumber, 10);
      if (!newTaskPrNumber || isNaN(prNum) || prNum < 1) {
        addToast('PR number must be a positive integer', 'error');
        return;
      }
      if (!normalizedPrBranch) {
        const taskLabel = newTaskType === 'fix' ? 'fix' : 'review';
        addToast(`PR branch is required for ${taskLabel} tasks`, 'error');
        return;
      }
    }
    // Code-producing task types hard-require an acceptance_evidence declaration
    // (backend validate_task_payload). Build + validate it client-side so a
    // submission without a valid declaration is blocked with an actionable
    // inline message instead of round-tripping a doomed 422.
    let acceptanceEvidencePayload: Record<string, unknown> | null = null;
    if (isCodeProducingTaskType(newTaskType)) {
      acceptanceEvidencePayload = buildAcceptanceEvidence(acceptanceEvidence);
      if (acceptanceEvidencePayload === null) {
        addToast(
          'Acceptance evidence is required for code-producing tasks — describe what would prove this change worked (e.g. a named test).',
          'error',
        );
        return;
      }
    }
    try {
      const res = await fetch(`${API_BASE_URL}/api/admin/tasks`, {
        method: 'POST',
        headers: await authHeaders(),
        body: JSON.stringify({
          github_repo: newTaskRepo,
          title: newTaskTitle,
          prompt: newTaskPrompt,
          base_branch: newTaskBranch,
          task_type: newTaskType,
          ...(newTaskType === 'review' || newTaskType === 'fix' ? {
            pr_number: parseInt(newTaskPrNumber, 10),
            branch: normalizedPrBranch || undefined,
          } : {}),
          ...(acceptanceEvidencePayload ? { acceptance_evidence: acceptanceEvidencePayload } : {}),
          ...(newTaskModel ? { model: newTaskModel } : {}),
          ...(newTaskEffort ? { effort: newTaskEffort } : {}),
        }),
      });
      if (!res.ok) {
        const errData = await res.json();
        // Propagate the server's actual reason so the NewTaskModal can render it
        // inline (a blanket 'Invalid request' hides the diagnosis).
        throw new Error(errData?.detail || errData?.error?.message || res.statusText);
      }
      setShowNewTaskModal(false);
      setNewTaskTitle('');
      setNewTaskPrompt('');
      setNewTaskPrNumber('');
      setNewTaskPrBranch('');
      clearDraft();
      addToast('Task created successfully', 'success');
      fetchTasksRef.current();
    } catch (error) {
      const message = error instanceof Error ? error.message : 'unknown error';
      console.error('Failed to create task:', error);
      addToast(`Failed to create task: ${message}`, 'error');
      throw error;
    }
  };

  const openPrefilledTaskModal = ({
    taskType,
    title,
    prompt,
    prNumber,
    prBranch,
    baseBranch,
  }: {
    taskType: string;
    title: string;
    prompt: string;
    prNumber?: number | string;
    prBranch?: string;
    baseBranch?: string;
  }) => {
    const repo = getSelectedProjectRepo() || '';
    setModalOpener('duplicate');
    setShowNewTaskModal(true);
    setNewTaskRepo(repo);
    // Prefer the PR's own base branch (so a task launched from a PR
    // targets the right base); fall back to the project default.
    setNewTaskBranch(baseBranch || getDefaultTaskBranch() || '');
    setNewTaskTitle(title);
    setNewTaskPrompt(prompt);
    setNewTaskType(taskType);
    setNewTaskModel(getDefaultModelForType(taskType));
    setNewTaskEffort(getDefaultEffortForType(taskType));
    setNewTaskPrNumber(prNumber === undefined || prNumber === null ? '' : String(prNumber));
    setNewTaskPrBranch(prBranch ?? '');
    setUseManualRepo(false);
    setUseManualBranch(false);
    setBranchOptions([]);
    setBranchError(null);
    setAcceptanceEvidence(defaultAcceptanceEvidenceForm());
    fetchRepoOptions();
    fetchLaunchableTaskTypeOptions();
    if (repo) fetchBranchOptions(repo);
  };

  const openWebResearchTaskModal = (topic?: string) => {
    const trimmedTopic = topic?.trim() ?? '';
    openPrefilledTaskModal({
      taskType: 'web_research',
      title: trimmedTopic || 'Web research',
      prompt: trimmedTopic || 'Research a relevant topic for this project.',
    });
  };

  const openDuplicateTaskModal = (task: Task) => {
    const payload = task.payloads?.[0] ?? {};
    const repo = (payload.github_repo as string) || '';
    const branch = (payload.base_branch as string) || '';
    const model = (payload.model as string) || '';
    const effort = (payload.effort as string) || '';
    // compute_experiment is operator_contract_launchable but not in USER_LAUNCHABLE_TASK_TYPE_IDS,
    // so check for it explicitly. Otherwise use launchableTaskTypeIds.
    const taskType: UserLaunchableTaskType | 'compute_experiment' =
      (task.task_type as string) === COMPUTE_EXPERIMENT_TASK_TYPE
        ? COMPUTE_EXPERIMENT_TASK_TYPE
        : task.task_type && launchableTaskTypeIds.includes(task.task_type)
          ? (task.task_type as UserLaunchableTaskType)
          : 'create';

    setModalOpener('duplicate');
    setShowNewTaskModal(true);
    setNewTaskRepo(repo);
    setNewTaskBranch(branch || getDefaultTaskBranch() || '');
    setNewTaskTitle(task.title);
    setNewTaskPrompt(task.description || '');
    setNewTaskType(taskType);
    setNewTaskModel(model || getDefaultModelForType(taskType));
    setNewTaskEffort(effort || getDefaultEffortForType(taskType));
    setNewTaskPrNumber('');
    setNewTaskPrBranch('');
    setUseManualRepo(false);
    setUseManualBranch(false);
    setBranchOptions([]);
    setBranchError(null);
    setCameFromDraft(false);

    // For compute_experiment tasks, prefill the compute experiment form from the payload.
    if ((task.task_type as string) === COMPUTE_EXPERIMENT_TASK_TYPE) {
      const payloadArms = payload.arms as Array<{ name: string; command: string }> | undefined;
      const hasArms = Array.isArray(payloadArms) && payloadArms.length > 0;
      setComputeExperiment({
        useArmsMode: hasArms,
        arms: hasArms ? payloadArms : [{ name: '', command: '' }, { name: '', command: '' }],
        declaredMetric: (payload.declared_metric as string) || '',
        command: (payload.command as string) || '',
        cwd: (payload.cwd as string) || '',
        envVars: (() => {
          const env = payload.env as Record<string, string> | undefined;
          if (!env) return '';
          return Object.entries(env).map(([k, v]) => `${k}=${v}`).join('\n');
        })(),
        accelerator: (payload.accelerator as string) || '',
        machineType: (payload.machine_type as string) || '',
        spendCapUsd: String(payload.spend_cap_usd ?? ''),
        deadlineSeconds: String(payload.deadline_seconds ?? ''),
        experiment: (payload.experiment as string) || '',
        benchmark: (payload.benchmark as string) || '',
        hypothesis: (payload.hypothesis as string) || '',
        metric: (payload.metric as string) || '',
        successMetric: (() => {
          const sc = payload.success_criterion as { metric: string; operator: string; threshold: number } | undefined;
          return sc?.metric || '';
        })(),
        successOperator: (() => {
          const sc = payload.success_criterion as { metric: string; operator: ComputeSuccessOperator; threshold: number } | undefined;
          return (sc?.operator as ComputeSuccessOperator) || '>=';
        })(),
        successThreshold: (() => {
          const sc = payload.success_criterion as { metric: string; operator: string; threshold: number } | undefined;
          return sc ? String(sc.threshold) : '';
        })(),
        requireLiveRelease: (payload.require_live_release as boolean) || false,
        requireDurableArtifact: (payload.require_durable_artifact as boolean) || false,
        provisioningModel: ((payload.provisioning_model as 'STANDARD' | 'SPOT') || 'STANDARD'),
        containerImage: (payload.container_image as string) || '',
        containerRegistryServer: (() => {
          const auth = payload.container_registry_auth as { server?: string } | undefined;
          return auth?.server || '';
        })(),
        containerRegistryUsername: (() => {
          const auth = payload.container_registry_auth as { username?: string } | undefined;
          return auth?.username || '';
        })(),
        // Never prefill the registry password — it is a write-only secret.
        containerRegistryPassword: '',
        // Forced runner target is not carried over to clones — the operator
        // must consciously choose a target for each new launch.
        forcedRunnerId: '',
        setupStep: (payload.setup_step as string) || '',
        noProgressTimeoutSeconds: payload.no_progress_timeout_seconds != null ? String(payload.no_progress_timeout_seconds) : '',
        inputArtifactJobId: (payload.input_artifact_job_id as string) || '',
      });
    } else {
      setComputeExperiment(defaultComputeExperimentForm());
    }
    // Carry the source task's declared acceptance evidence into the clone so a
    // duplicated code-producing task does not start from a blank declaration.
    setAcceptanceEvidence(acceptanceEvidenceFormFromPayload(payload.acceptance_evidence));

    fetchRepoOptions();
    fetchLaunchableTaskTypeOptions();
    fetchFleetRunnersForProject();
    if (repo) fetchBranchOptions(repo);
  };

  return {
    showNewTaskModal, setShowNewTaskModal,
    newTaskRepo, newTaskBranch, newTaskTitle, newTaskPrompt, newTaskType,
    newTaskPrNumber, newTaskPrBranch, newTaskModel, newTaskEffort,
    setNewTaskRepo, setNewTaskBranch, setNewTaskTitle, setNewTaskPrompt,
    setNewTaskType, setNewTaskPrNumber, setNewTaskPrBranch, setNewTaskModel, setNewTaskEffort,
    computeExperiment, setComputeExperiment,
    acceptanceEvidence, setAcceptanceEvidence,
    ideaText, setIdeaText, draftNotes, setDraftNotes, cameFromDraft, onDraftApplied,
    preflightResult, preflightLoading, preflightError,
    repoOptions, repoLoading, repoError, useManualRepo, setUseManualRepo,
    branchOptions, branchLoading, branchError, useManualBranch, setUseManualBranch, setBranchOptions,
    launchableTaskTypeOptions,
    fleetRunners, fleetRunnersLoading,
    openNewTaskModal, openDuplicateTaskModal, openPrefilledTaskModal, openWebResearchTaskModal, handleRepoChange, handleCreateTask, getDefaultModelForType, getDefaultEffortForType,
  };
}
