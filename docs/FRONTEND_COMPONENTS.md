# Frontend Components Reference

Guide to React + TypeScript components in `admin-ui/src/components/`.

---

## Component Organization

Components are organized by feature area:

- **Layout & Shell** — HeaderBar, ErrorBoundary, main layout
- **Task Management** — TaskCard, TaskDetailDrawer, NewTaskModal
- **Dashboards** — DashboardHome, MetricsDashboard, KanbanBoard
- **Autonomous Mode** — DirectionsPanel, DirectionCard, AutonomousStatus
- **Analytics** — ProviderCostPanel, SrTrendBreakdown, PipelineRegressionsPanel
- **Settings** — SettingsSectionRouter, runtime/project settings section components
- **Utilities** — Pagination, ColumnsPicker, ConfirmDialog

---

## Core Layout Components

### HeaderBar

Top navigation bar with project selector, alerts, and settings.

**Props:**
```typescript
{
  currentProjectId?: string;
  onProjectChange?: (projectId: string) => void;
  alertCount?: number;
}
```

**Features:**
- Real-time alert badge
- Breadcrumb navigation
- GitHub status indicator
- Settings entry point into the routed Runtime Settings workspace

---

### ErrorBoundary

Error fallback UI (React error boundary).

**Catches:**
- Rendering errors
- Lifecycle errors
- Event handler errors

**Displays:** Error message + "Reload" button.

---

## Task Management

### NewTaskModal

Form to launch a new task.

**Form fields:**
- Repository (owner/repo)
- Base branch
- Task type (select dropdown)
- Prompt/description
- Priority (low/medium/high)

**Validation:**
- GitHub owner/repo pattern matching
- Task type from enum
- Prompt length limits

---

### TaskCard

Compact task status card displayed in lists/kanban.

**Props:**
```typescript
{
  task: TaskSummary;
  onClick?: (taskId: string) => void;
  status?: TaskStatus;
}
```

**Displays:**
- Task type badge
- Status indicator (color-coded)
- Timestamp
- PR/commit link (if available)

**Interactive:**
- Click to open detail drawer
- Hover shows full title

---

### TaskDetailDrawer

Expanded task view with full details, logs, artifacts.

**Tabs:**
1. **Overview** — Task metadata, status, timeline
2. **Logs** — Real-time streaming logs (WebSocket)
3. **Artifacts** — Generated outputs (PR, commit, reports)
4. **Self-Review** — Agent's code quality assessment
5. **Verdict** — Structured fix-loop telemetry

**Real-time features:**
- Log tail streaming (WebSocket)
- Status changes
- Artifact links (clickable)

---

## Dashboards

### DashboardHome

Main admin dashboard (default view on login).

**Sections:**
1. **Alerts** — Active issues & warnings
2. **Quick Stats** — VMs running, queue depth, success rate
3. **Recent Tasks** — Table of latest tasks by status
4. **Metrics Cards** — Cost/token/latency summaries

**Data source:** `/api/admin/analytics` (polled every 30s)

---

### MetricsDashboard

Detailed analytics (cost breakdown, success rates, latency).

**Charts:**
- Task success rate (line chart, weekly)
- Token usage by model (stacked bar)
- Cost by provider (pie chart: on-demand vs. spot)
- Latency percentiles (p50, p95, p99)

**Filters:**
- Date range (last 7/30/90 days)
- Task type
- Status

---

### KanbanBoard

Kanban-style board showing tasks by status column with full drag-and-drop support.

**Columns:**
- QUEUED
- PROVISIONING
- IN_PROGRESS
- COMPLETED
- FAILED

**Features:**
- **Drag-drop** to reorder tasks within columns (uses dnd-kit library)
- Card count per column
- Color coding by priority
- Click card to expand detail drawer
- Sort tasks by: newest, oldest, type, title, duration, attempt count
- Archive completed/failed/cancelled tasks
- Customize visible columns
- Collapse/expand columns to save space

**Drag & Drop:**
- Built with `@dnd-kit/core` and `@dnd-kit/sortable`
- Reorders task position within columns (UI state only; backend ordering may differ)
- Handlers: `handleDragStart`, `handleDragEnd`
- Persists column collapse state to localStorage

---

## Autonomous Mode Components

### DirectionsPanel

List of strategic directions (autonomous research modes).

**Displays:**
- Active/paused/completed status
- Priority indicator
- Task count spawned from direction
- Last update timestamp

**Actions:**
- Pause/resume direction
- Create new direction
- View direction details

---

### DirectionCard

Detailed view of a single direction.

**Sections:**
1. **Metadata** — Title, description, priority, mode
2. **Findings** — Collected research findings
3. **Recommendations** — Next steps
4. **Spawned Tasks** — Links to tasks created from this direction

**Edit mode:**
- Text input for title/description
- Priority slider
- Mode selector (active/paused)

---

### AutonomousStatus

Status summary of autonomous research mode.

**Displays:**
- Mode (enabled/disabled)
- Active directions count
- Tasks spawned in last 24h
- Average spawn rate
- Next scheduled cycle (countdown)

**Actions:**
- Enable/disable autonomous mode
- Force run cycle now
- Configure settings

---

### DirectionAutoPauseWidget

Visual indicator of auto-pause status for a direction.

**Shows:**
- Whether direction is paused
- Pause reason (health check, manual, etc.)
- Auto-pause metrics (failure rate, stale age)
- Resume button

---

## Analytics Panels

### ProviderCostPanel

Cost breakdown by provider (Anthropic, OpenAI, Gemini).

**Metrics:**
- Per-provider spend (line chart)
- Cost vs. task count (scatter)
- Token usage by model
- Cost per task (histogram)

---

### SrTrendBreakdown

Self-review catch-rate trends over time.

**Shows:**
- Weekly catch rates (line chart)
- Breakdown by check category (stacked bar)
  - Correctness
  - Security
  - Test coverage
  - etc.
- Trend arrow (↑ improving, ↓ declining)

---

### PipelineRegressionsPanel

Regression analysis (checks that fail frequently).

**Identifies:**
- Which checks regress together (co-failure)
- Blind spots (categories with low catch rate)
- High-friction code paths (fail frequently)

**Suggests:**
- Areas for focused investigation
- Training data to improve agent accuracy

---

### FailureStageWidget

Which stage of pipeline most tasks fail at?

**Stages:**
- Clone
- Build
- Lint
- Tests
- PR creation
- Post-merge

**Display:**
- Failure count per stage (bar chart)
- Trend over time

---

### MediumScopeWidget

Metrics on "medium scope" changes (100-500 line diffs).

**Tracks:**
- Success rate for small vs. large tasks
- Fix-loop complexity by scope
- Recommendations on scope tuning

---

## Settings Components

Settings are routed through workspace-hosted sections. The HeaderBar gear opens
Runtime Settings, and Project Manage hosts project-scoped settings.

`settingsSections.ts` defines the canonical `SectionId` union. Runtime IDs are
`general`, `provider-routing`, `provider-settings`, `orchestrator`, `deploy`,
`accounts`, `notifications`, and `advanced`; project IDs are `build-config`,
`branches`, `auto-review`, `resource-envelope`, `auto-remediation`,
`issue-implementation`, `alert-domains`, and `access`.

`OperatorWorkspaceSections.ts` maps those IDs into `PROJECT_MANAGE_SECTIONS`,
`RUNTIME_SECTIONS`, `RUNTIME_SETTINGS_SECTIONS`, and `SETTINGS_SECTION_HOMES`
for command and strategic-context routing.

`SettingsSectionRouter.tsx` renders the active `SectionId` body. Runtime bodies
include `SettingsGeneralSection`, `SettingsOrchestratorSection`, `DeploySection`,
`ProviderCategoriesPanel`, `ProviderSettingsPanel`, `SettingsAccountsSection`,
`SettingsNotificationsSection`, and `SettingsAdvancedSection`; project bodies
include `ProjectBuildConfigSection`, `ProjectBranchesSection`,
`ProjectAutoReviewSection`, `ProjectResourceEnvelopeSection`,
`ProjectAutoRemediationSection`, `ProjectIssueImplementationSection`,
`ProjectAlertDomainsSection`, and router-rendered `access` actions.

Viewport coverage lives in `SettingsSectionRouter.viewport.test.tsx` plus
focused viewport tests for representative section bodies such as
`SettingsGeneralSection`, `SettingsAdvancedSection`, `SettingsDeploySection`,
`ProjectBuildConfigSection`, `ProjectBranchesSection`, and
`ProjectAutoReviewSection`.

---

### AutonomousConfigSection

Configure autonomous research mode.

**Settings:**
- Enable/disable
- Max concurrent research tasks
- Direction priority weights
- Health check thresholds
- Starvation fallback duration

---

## Utility Components

### Pagination

Reusable pagination controls.

**Props:**
```typescript
{
  currentPage: number;
  pageSize: number;
  totalItems: number;
  onPageChange: (page: number) => void;
}
```

---

### ColumnsPicker

Table column visibility selector (checkboxes).

**Use case:**
- TaskList showing task ID, status, type, time, PR link
- Allow user to toggle which columns are visible

---

### ConfirmDialog

Confirmation modal for destructive actions.

**Props:**
```typescript
{
  title: string;
  message: string;
  confirmText?: string;
  cancelText?: string;
  onConfirm: () => void | Promise<void>;
  onCancel: () => void;
  isDestructive?: boolean;  // Shows red button
}
```

---

### MarkdownContent

Render markdown with syntax highlighting.

**Features:**
- Code block highlighting (Prism.js)
- Link detection & target="_blank"
- Safe HTML sanitization (DOMPurify)

---

### ActivityFeed

Timeline of project events.

**Events:**
- Task launched
- Task completed/failed
- PR created/merged
- Direction paused/resumed
- Settings changed

**Display:**
- Chronological list
- Icons for event type
- Timestamp
- Expandable details

---

## Data Hooks

### useAppState

Global app state management (Redux alternative).

**Provides:**
```typescript
{
  projects: Project[];
  currentProject: Project | null;
  tasks: Task[];
  alerts: Alert[];
  autonomous: AutonomousConfig;
}
```

**Methods:**
- `setCurrentProject(id)`
- `refreshTasks(projectId)`
- `updateTask(taskId, updates)`
- `dismissAlert(alertId)`

---

### useWebSocket

Real-time log streaming from orchestrator.

**Usage:**
```typescript
const { logs, connected, error } = useWebSocket(
  projectId,
  taskId,
  onNewLog
);
```

**Returns:**
- `logs: string[]` — Accumulated log lines
- `connected: boolean` — WS status
- `error: Error | null` — Connection error

---

## Styling

All components use **Tailwind CSS** with no CSS-in-JS.

**Color scheme:**
- Primary: Blue-600
- Success: Green-600
- Danger: Red-600
- Warning: Amber-500
- Neutral: Slate-500

**Typography:**
- Headings: Inter (sans-serif)
- Body: Systole (sans-serif)
- Code: Roboto Mono (monospace)

**Responsive design:**
- Breakpoints: sm (640px), md (768px), lg (1024px), xl (1280px)
- Mobile-first (min-width: Tailwind utility prefix)

---

## Testing

### Viewport Regression Tests

All components have corresponding `.viewport.test.tsx` files.

**Purpose:** Ensure components don't overflow horizontally at 375px (mobile width).

**Naming convention:**
- Component: `FooBar.tsx`
- Viewport test: `FooBar.viewport.test.tsx`

**Example:**
```typescript
// FooBar.viewport.test.tsx
import { render } from '@playwright/experimental-ct-react';
import FooBar from './FooBar';

test('FooBar does not overflow at 375px', async ({ mount }) => {
  const component = await mount(<FooBar />);
  const { width } = await component.boundingBox();
  expect(width).toBeLessThanOrEqual(375);
});
```

Run with `npm run test:viewport`.

---

### Unit Tests

Functional tests using Vitest/jsdom.

**Naming convention:**
- Component: `FooBar.tsx`
- Unit test: `FooBar.test.tsx`

**Coverage:** Interaction, state changes, error states.

Run with `npm run test:run`.

---

## Component Import Paths

All components are under `admin-ui/src/components/`:

```typescript
import HeaderBar from '@/components/HeaderBar';
import TaskCard from '@/components/TaskCard';
import { ConfirmDialog } from '@/components/ConfirmDialog';
```

Note: `@/` is configured as path alias for `src/` (see `tsconfig.json`).

---

## Adding New Components

1. Create `ComponentName.tsx` in `admin-ui/src/components/`
2. Export as default export
3. Add JSDoc comment at top
4. Create `ComponentName.test.tsx` for unit tests
5. Create `ComponentName.viewport.test.tsx` for viewport test
6. Import in parent component or route
7. Run `npm run build` to check for TS errors
8. Run `npm run test:viewport` to verify mobile layout

---

## Common Patterns

### Async Data Loading

```typescript
const [data, setData] = useState<T | null>(null);
const [loading, setLoading] = useState(false);
const [error, setError] = useState<Error | null>(null);

useEffect(() => {
  (async () => {
    setLoading(true);
    try {
      const result = await fetch('/api/...');
      setData(result);
    } catch (e) {
      setError(e as Error);
    } finally {
      setLoading(false);
    }
  })();
}, []);
```

### Optimistic Updates

Update UI immediately, then sync with server:

```typescript
setTasks(prev => prev.map(t => 
  t.id === taskId ? { ...t, status: 'COMPLETED' } : t
));
await api.updateTaskStatus(taskId, 'COMPLETED');
```

### Loading States

Show spinner during async work:

```typescript
{loading && <Spinner />}
{!loading && error && <ErrorMessage error={error} />}
{!loading && !error && <Content data={data} />}
```

---

## Accessibility

- All interactive elements have `role` attributes
- Form labels properly associated with inputs
- ARIA live regions for real-time updates
- Keyboard navigation (Tab, Enter, Escape)
- Color contrast >= 4.5:1 (WCAG AA)

Use `npm run build` to catch accessibility issues in the ESLint output.
