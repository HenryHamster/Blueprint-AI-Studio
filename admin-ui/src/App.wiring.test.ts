import { describe, expect, test } from 'vitest';
import { readFileSync } from 'node:fs';
import path from 'node:path';

const appSource = readFileSync(path.join(process.cwd(), 'src', 'App.tsx'), 'utf8');

describe('App workbench wiring', () => {
  test('keeps chat Workday lifecycle wired to live session state and runner controls', () => {
    const control = appSource.match(/<StrategicSessionControl[\s\S]*?\/>/)?.[0] ?? '';

    expect(control).toContain("currentMode={strategicInteractionMode === 'continuous' ? 'continuous' : 'chat'}");
    expect(control).toContain('sessionActive={strategicSessionActive}');
    expect(control).toContain('sessionStatus={strategicSessionStatus}');
    expect(control).toContain('sessionState={strategicSessionState}');
    expect(control).toContain('sessionRunners={residentStrategicSessionRunners}');
    expect(control).toContain('sessionSelectedRunnerId={strategicSessionSelectedRunnerId}');
    expect(control).toContain('onSelectRunner={handleSelectStrategicRunner}');
    expect(control).toContain('onStartSession={handleStartStrategicSession}');
    expect(control).toContain('onStopSession={handleStopStrategicSession}');
    expect(control).toContain('onModeChange={setStrategicInteractionMode}');
  });

  test('wires the real Strategic MessagesPanel to the context workspace opener', () => {
    expect(appSource).toContain('const openStrategicContextWorkspace = useCallback(');
    expect(appSource).toMatch(/<MessagesPanel[\s\S]*onOpenCommandPalette=\{openCommandPalette\}[\s\S]*onOpenStrategicContextWorkspace=\{openStrategicContextWorkspace\}/);
  });

  test('routes command-palette Strategic targets through overlay-safe wrappers', () => {
    expect(appSource).toMatch(/const openStrategicContextWorkspaceTarget = useCallback\([\s\S]*setStrategicContextWorkspaceOpen\(false\);[\s\S]*openStrategicContextTarget\(target\);/);
    expect(appSource).toMatch(/const openStrategicMessageTargetFromCommandPalette = useCallback\([\s\S]*setStrategicContextWorkspaceOpen\(false\);[\s\S]*strategicMessageTarget\.open\(target\);[\s\S]*\[strategicMessageTarget\.open\]/);
    expect(appSource).toMatch(/openStrategicContextTarget: openStrategicContextWorkspaceTarget,[\s\S]*openStrategicMessageTarget: openStrategicMessageTargetFromCommandPalette,/);
  });

  test('keeps Pipeline context selection separate from the task details drawer', () => {
    expect(appSource).toContain('const [pipelineContextTask, setPipelineContextTask] = useState<Task | null>(null);');
    expect(appSource).toContain('const pipelineContextRequestRef = useRef(0);');
    expect(appSource).toMatch(/const selectPipelineTaskById = useCallback\([\s\S]*const requestId = pipelineContextRequestRef\.current \+ 1;[\s\S]*setPipelineContextTask\(target\);[\s\S]*fetchTask\(taskId\)[\s\S]*pipelineContextRequestRef\.current === requestId[\s\S]*setPipelineContextTask\(task\)/);
    expect(appSource).toMatch(/useStrategicContextNavigation\(\{[\s\S]*selectPipelineTaskById/);
    expect(appSource).toMatch(/const openPipelineTask = useMemo\(\(\) => \{[\s\S]*if \(selectedPipelineContextTask && s\.logTask\?\.id === selectedPipelineContextTask\.id\) return s\.logTask;[\s\S]*if \(selectedPipelineContextTask\) return selectedPipelineContextTask;[\s\S]*if \(!s\.logTask\) return null;/);
    expect(appSource).toContain('onOpenTask: handleOpenTaskDetails,');
    expect(appSource).toMatch(/<RuntimeWorkspace[\s\S]*onViewTask=\{handleOpenTaskDetails\}/);
  });

  test('keeps live task streams owned by Pipeline instead of a global floating panel', () => {
    expect(appSource).not.toContain("import TaskStreamPanel from './components/TaskStreamPanel';");
    expect(appSource).not.toContain('<TaskStreamPanel');
    expect(appSource).toMatch(/const openTrackedTaskStreams = useCallback\(\(\) => \{[\s\S]*setArchiveVisible\(false\);[\s\S]*setTaskViewMode\('pipeline'\);[\s\S]*setPipelineSection\('flow'\);[\s\S]*handleTabChange\('pipeline'\);/);
    expect(appSource).toMatch(/<PipelineWorkspace[\s\S]*stream=\{\{[\s\S]*trackedTaskIds: s\.trackedTaskIds,[\s\S]*trackedLogs: s\.trackedLogs,[\s\S]*onUntrack: s\.untrackTask,[\s\S]*onUntrackAll: s\.untrackAll,/);
    expect(appSource).toMatch(/<PrimaryTabNav[\s\S]*onTabChange=\{\(tab\) => \{[\s\S]*tab === 'pipeline' && s\.trackedTaskIds\.size > 0[\s\S]*openTrackedTaskStreams\(\);/);
    expect(appSource).toContain('Open tracked task streams in Pipeline');
  });

  test('keeps removed task status presets from leaving invisible persisted filters active', () => {
    expect(appSource).toContain('const ACTIVE_TASK_STATUS_FILTER = STATUS_OPTIONS.map(option => option.value);');
    expect(appSource).toMatch(/if \(!sameStringSet\(s\.statusFilter, ACTIVE_TASK_STATUS_FILTER\)\) \{[\s\S]*setStatusFilter\(\[\.\.\.ACTIVE_TASK_STATUS_FILTER\]\);/);
    expect(appSource).toContain('if (s.requiresApproval) setRequiresApproval(false);');
    expect(appSource).toContain('const statusMatch = matchesStatusFilter(t.status, ACTIVE_TASK_STATUS_FILTER);');
    expect(appSource).not.toContain('statusFilter={s.statusFilter}');
    expect(appSource).not.toContain('onStatusFilterChange={s.setStatusFilter}');
    expect(appSource).not.toContain('const approvalMatch = !s.requiresApproval');
    expect(appSource).toContain('const stuckMatch = !s.stuckFilter || stuckTaskIds.has(t.id);');
    expect(appSource).toContain('stuckFilter={s.stuckFilter}');
    expect(appSource).toContain('onStuckFilterChange={s.setStuckFilter}');
  });

  test('keeps bracket section cycling wired through the real Pipeline view state', () => {
    expect(appSource).toContain("const PIPELINE_SECTION_ORDER: readonly PipelineSection[] = ['flow', 'board', 'archive'];");
    expect(appSource).toMatch(/const applyPipelineSection = useCallback\(\(section: PipelineSection\) => \{[\s\S]*setPipelineSection\(section\);[\s\S]*setArchiveVisible\(section === 'archive'\);[\s\S]*if \(section === 'flow'\) setTaskViewMode\('pipeline'\);[\s\S]*if \(section === 'board'\) setTaskViewMode\('kanban'\);/);
    expect(appSource).toMatch(/if \(activeTab === 'pipeline'\) \{[\s\S]*applyPipelineSection\(cycleSection\(PIPELINE_SECTION_ORDER, pipelineSection, direction\)\);[\s\S]*return true;/);
    expect(appSource).toMatch(/searchShortcutBlocked: Boolean\(s\.showNewTaskModal \|\| s\.logTask \|\| transientWorkbenchPanelOpen\)/);
    expect(appSource).toMatch(/transientPanelOpen: transientWorkbenchPanelOpen/);
  });

  test('findings journal feed keeps implementation child outcomes', () => {
    expect(appSource).toContain('`${API_BASE_URL}/api/admin/tasks?task_type=research&archived=false&limit=500&project_id=');
    expect(appSource).toContain('setResearchFeed(all);');
  });

  test('reports each non-chat board navigation after task-fetch completion or paint boundary', () => {
    expect(appSource).toContain('function BoardDataReadyReporter');
    expect(appSource).toContain('<BoardDataReadyReporter activeTab={activeTab} initialLoading={boardInitialLoading} refreshing={boardRefreshing} fetchEpoch={boardDataEpoch} />');
    expect(appSource).toMatch(/if \(targetEpochRef\.current !== null && fetchEpoch >= targetEpochRef\.current\) \{[\s\S]*reportUiLoadTiming\(activeTab, Math\.round\(performance\.now\(\) - navigationStartRef\.current\), 'board_data_ready'\)/);
    expect(appSource).toMatch(/navigationStartRef\.current = performance\.now\(\);[\s\S]*reportedRef\.current = false;[\s\S]*targetEpochRef\.current = fetchEpoch \+ 1;/);
  });
});
