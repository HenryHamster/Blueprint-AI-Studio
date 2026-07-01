# Operator Workbench Full Usability Roadmap

Last updated: 2026-07-01

This is the execution roadmap for making the operator workbench fully usable across backend and frontend. It turns the operating model in `docs/operator-experience-operating-model.md` and the UI mapping in `admin-ui/mockups/operator-ui-mapping.md` into implementation milestones.

The rule for this file: after every major batch, update the status ledger, milestone checklists, evidence links, and next-batch focus before pushing to `automain`.

## Status Ledger

Overall implementation estimate: 68%.

Legend:
- Done: implemented, reviewed, tested, and pushed.
- Partial: usable pieces exist, but the milestone is not complete.
- Planned: not started or only exploratory.
- Later: useful, but not required before the workbench is usable.
- Defer: deliberately not building unless a later requirement proves it necessary.

| Milestone | Status | Current evidence | Next action |
| --- | --- | --- | --- |
| M0. Workbench IA foundation | Done | Four-mode shell, Chat/Pipeline/Project/Runtime homes, task archive moved under Pipeline, runtime settings/devices/accounts rehosted. | Keep regression coverage while later cleanup lands. |
| M1. Roadmap and tracking discipline | In progress | This document. | Keep this file updated after every pushed batch. |
| M2. Persistent local Strategic workday mode | Partial | Workbench state, continuous mode markers, Strategic local runner requirement, local runner selection, attention for holds. | Add bounded recovery/timeout and make persistence/ownership explicit. |
| M3. Messages as the active-work interface | Partial | `MessagesPanel` page, clean composer mode, strategic session messages, context descriptors. | Finish message contract cleanup and remove remaining always-visible advanced chrome from primary chat. |
| M4. Ctrl+K and hotkey operating layer | Partial | Command palette, shortcut hooks, server command descriptors, allowlisted command execution. | Finish canonical command/target registry and missing hotkeys. |
| M5. Strategic context and canvas/artifact surface | Partial | Context package, selected refs, canvas descriptors, `StrategicContextWorkspace`, map/resource rows. | Add first-class Chat canvas slot and contract fixtures before any writable canvas system. |
| M6. Pipeline monitoring and task sideboard | Partial | Pipeline workspace, task tree, task rail, stale attention routing, task sideboard context. | Tighten sideboard integration, status flow, tracked streams, and fixture-based contract tests. |
| M7. Runtime, devices, accounts, settings, project management | Partial | Runtime workspace, fleet/devices, accounts, settings router, project manage sections. | Remove modal-era assumptions and split project policy vs runtime settings cleanly. |
| M8. Operator attention and work-session read model | Partial | Derived attention state, curated metrics, actions, events, jobs, command receipts. | Harden lifecycle semantics, targets, metrics registry, and stale/local safety. |
| M9. Unified event and message contracts | Partial | Workbench event v1 envelope exists, project message creation/projection are centralized. | Inventory legacy emitters and converge backend/frontend fixtures. |
| M10. Visual system and UI polish | Partial | Dark developer-tool direction, reduced chrome, viewport tests. | Tokenize theme, reduce hardcoded colors/panels, align spacing/header/margins/fonts. |
| M11. Background operator jobs | Planned | Existing compute jobs, runner jobs, orchestrator loops, command receipts. | Only build a real operator-job table if a concrete async operator command needs it. |
| M12. Completion audit and hardening | Planned | Focused tests exist across major components. | Run full backend/frontend audit loops, viewport screenshots, and contract gates. |

## Operating Principles

1. Reuse existing systems first: runners, messages, events, artifacts, metrics, Strategic cycles, task detail, and settings sections.
2. Avoid parallel systems unless a concrete requirement cannot be met by extending the existing one.
3. The main UI should stay simple: Chat for active Strategic work, Pipeline for passive task flow, Project for durable strategy, Runtime for devices/accounts/health/settings.
4. Advanced actions belong in command/context surfaces, not as permanent buttons in the primary chat.
5. Every milestone must include focused tests, review, fixes, and a push to `automain`.
6. Before each implementation batch, sync from `origin/automain`.
7. After each batch, update this roadmap with the real status and evidence.

## M0. Workbench IA Foundation

Status: Done.

Purpose: Establish the main operator surfaces without dropping existing capabilities.

Completed:
- Four top-level workbench modes: Chat, Pipeline, Project, Runtime.
- Chat uses `MessagesPanel` as the active Strategic conversation surface.
- Pipeline uses `PipelineWorkspace` with flow, board, archive, task sideboard, and tracked streams.
- Project rehosts update, directions, problem model, design doc, and project management.
- Runtime rehosts overview, devices, compute, accounts, analytics, and settings.
- Task drawer, alerts, activity feed, research browser, knowledge browser, project modals, and task streams remain global overlays.

Authoritative files:
- `admin-ui/src/App.tsx`
- `admin-ui/src/hooks/useUIState.ts`
- `admin-ui/src/components/OperatorWorkspaces.tsx`
- `admin-ui/src/components/PipelineWorkspace.tsx`
- `admin-ui/src/components/WorkbenchLayout.tsx`

Regression gates:
- `admin-ui/src/operatorWorkbenchContracts.test.ts`
- `admin-ui/src/App.wiring.test.ts`
- `admin-ui/src/components/WorkbenchComposition.viewport.test.tsx`
- `admin-ui/src/components/OperatorWorkspaces.viewport.test.tsx`
- `admin-ui/src/components/PipelineWorkspace.viewport.test.tsx`

## M1. Roadmap and Tracking Discipline

Status: In progress.

Purpose: Prevent open-ended work from turning into invisible drift.

Deliverables:
- A single tracked roadmap file with all known backend/frontend/operator milestones.
- Status table with current completion state and next action.
- Per-milestone acceptance gates and authoritative files.
- After every pushed batch, update status, evidence, percentage, and next focus.

Acceptance:
- This file exists under `docs/plans/`.
- Future milestone commits update this file when status changes.

## M2. Persistent Local Strategic Workday Mode

Status: Partial.

Purpose: Let the operator work continuously with Strategic while local hardware is available, without creating a separate Strategic system.

Existing:
- `backend/app/workbench_state.py` stores per-operator workbench state in `MessagesNoisePrefs.prefs["_strategic_workbench"]`.
- Workbench modes include `chat`, `check_in`, `monitor`, and `continuous`.
- Continuous mode writes a project-level `_strategic_local_continuation` marker in `Project.vision`.
- `backend/app/strategic_spawning.py` consumes fresh continuation markers and stamps Strategic task context with:
  - `strategic_local_runner_required`
  - `strategic_local_runner_reason`
  - `preferred_runner_target_id`
- Local runners exist through `LocalRunner`, admin registration/list/delete, runner heartbeat/polling, and `scripts/local_runner.py`.
- Strategic runner preset restricts task types and concurrency.
- Orchestrator can select eligible local runners and hold local-required Strategic tasks instead of falling back to cloud.
- Attention now surfaces Strategic local runner holds.

Remaining work:
- Add bounded timeout/recovery for stale Strategic local holds, comparable to compute local-runner hold safety.
- Make continuation-marker ownership explicit for multi-operator cases. Current project-level marker is intentionally lossy.
- Decide whether the current 4-hour workbench TTL is enough for a workday or whether continuous mode needs a longer durable marker.
- Make UI copy/state precise: local Strategic is persistent while the operator device is connected, not a separate Strategic backend.
- Add clearer Runtime Devices affordances for registering/selecting/prefering a local Strategic runner.
- Add tests for stale Strategic local hold timeout, recovery, preferred-runner loss, and duplicate operator sessions.

Authoritative files:
- `backend/app/workbench_state.py`
- `backend/app/strategic_spawning.py`
- `backend/app/strategic_runtime_work.py`
- `backend/app/orchestrator.py`
- `backend/app/routers/admin_runners.py`
- `backend/app/routers/runner.py`
- `scripts/local_runner.py`
- `admin-ui/src/components/StrategicSessionControl.tsx`
- `admin-ui/src/components/FleetRunnersPanel.tsx`

Acceptance gates:
- Backend tests prove local Strategic tasks do not cloud-fallback when local is required.
- Backend tests prove stale local Strategic holds become actionable or terminal instead of waiting forever.
- Frontend tests prove continuous mode and preferred runner state are visible and route to Runtime Devices.
- Review confirms no new parallel Strategic system was introduced.

## M3. Messages as the Active-Work Interface

Status: Partial.

Purpose: Make Chat the clean place to talk to Strategic, discuss plans/results, and let Strategic pull relevant context or artifacts into view.

Existing:
- `MessagesPanel` is a real page, not only a drawer.
- `MessagesComposer` supports minimal chat mode and Strategic session context.
- Strategic session message route exists.
- Message arming distinguishes steering, passive notes, routine events, not armed, and duplicates.
- Conversation filtering keeps user/operator/Strategic/high-signal messages visible.
- Message context can include session snapshots and selected refs.

Remaining work:
- Finalize the backend message contract:
  - Keep `ProjectMessage` as the durable source.
  - Reduce legacy projection drift across `admin_message_dict`, `legacy_direction_message_dict`, `agent_thread_message_dict`, and `agent_fetch_message_dict`.
  - Bound and document free-form `context` keys used by operator workbench.
  - Align message create/update/delete/resolve responses with frontend types.
- Move low-frequency message controls out of the primary chat surface:
  - Search, saved views, resolved visibility, batch actions, event noise preferences, delete/resolve, convert-to-direction, investigation launch.
  - Keep them accessible through command palette, context menu, or secondary surfaces.
- Keep the composer plain:
  - No fake action chips such as "render chart" or "pull spend" unless backed by real commands.
  - The primary action is sending a message to Strategic.
- Add a clean mechanism for Strategic to attach or reference a context artifact/canvas without stuffing it inline into every answer.
- Tighten unread/high-signal counting so routine alerts do not crowd active chat.

Authoritative files:
- `backend/app/project_message_creation.py`
- `backend/app/project_message_projection.py`
- `backend/app/message_convergence.py`
- `backend/app/routers/admin_projects.py`
- `admin-ui/src/components/MessagesPanel.tsx`
- `admin-ui/src/components/MessagesListView.tsx`
- `admin-ui/src/components/MessagesComposer.tsx`
- `admin-ui/src/components/MessagesPanelHeader.tsx`
- `admin-ui/src/components/messageArming.ts`
- `admin-ui/src/types/messages.ts`

Acceptance gates:
- Backend tests cover all arming outcomes and message context caps.
- Frontend tests consume backend-like fixtures for every arming outcome.
- Chat viewport tests pass at 375px without hidden composer or overflowing message rows.
- Review confirms the primary chat surface has no unexplained or nonfunctional buttons.

## M4. Ctrl+K and Hotkey Operating Layer

Status: Partial.

Purpose: Let the operator avoid typing polished essays and avoid hunting through UI chrome. Commands should expose real actions, navigation, and context selection.

Existing:
- Command palette exists.
- One-key workbench mode shortcuts exist.
- Backend emits work-session command descriptors.
- Backend executes a narrow allowlist:
  - mode changes
  - clear selected refs
  - prefer/clear runner
  - mark messages seen
  - dismiss alert
- Frontend maps server commands plus local navigation/action commands.

Remaining work:
- Create a canonical command registry/contract:
  - command id
  - label
  - group
  - priority
  - target
  - refetch surfaces
  - executable vs navigation-only
  - mutation allowlist
- Remove duplicated target/command semantics between backend and frontend.
- Add missing hotkeys from the IA target:
  - `[` and `]` for previous/next subtab within the current mode
  - `/` to focus current mode search/filter where available
  - `?` for shortcuts overlay
  - unified `Esc` behavior for topmost overlay, selection, and mode defaults
- Ensure hotkeys are disabled inside inputs, textareas, selects, and rich editors.
- Add command entries for the hidden message actions moved out of chat chrome.
- Keep command execution scoped. Do not add arbitrary backend mutation execution without a real permission and validation layer.

Authoritative files:
- `backend/app/work_session_commands.py`
- `backend/app/work_session_command_execution.py`
- `admin-ui/src/components/CommandPalette.tsx`
- `admin-ui/src/components/CommandPaletteHost.tsx`
- `admin-ui/src/hooks/useWorkbenchCommands.ts`
- `admin-ui/src/hooks/useOperatorKeyboardShortcuts.ts`
- `admin-ui/src/hooks/useWorkbenchShortcutRouter.ts`
- `admin-ui/src/hooks/useCommandPaletteShortcut.ts`

Acceptance gates:
- Backend tests cover every server command id and malformed/disabled/stale command rejection.
- Frontend tests cover keyboard routing and all canonical targets.
- Command palette viewport tests pass without overflow.
- Review confirms every visible command either navigates somewhere real or executes an allowlisted mutation.

## M5. Strategic Context and Canvas/Artifact Surface

Status: Partial.

Purpose: Let Strategic pull up actual numbers, diagrams, plots, resources, and task context while the main chat stays clean.

Existing:
- `backend/app/strategic_context_package.py` builds a bounded read-only context package.
- Package includes project, running update, problem model, directions, messages, tasks, attention, knowledge docs, artifacts, cycles, active task, selected refs, resources, and canvas descriptors.
- Canvas descriptor has nodes/edges and an open command.
- Frontend has `StrategicContextWorkspace`, `StrategicContextMap`, context rail, descriptor chips, and selected-ref handling.

Remaining work:
- Make canvas/artifact a first-class Chat workspace slot on desktop and an accessible surface on mobile.
- Decide the v1 rendering set:
  - context map
  - metric cards
  - table
  - markdown/text artifact
  - image
  - chart/plot descriptor
  - task/direction/message/resource detail
- Add stable descriptor schemas for artifact/canvas resources that frontend can render without guessing.
- Add backend-generated golden fixtures for context package/canvas descriptors.
- Keep v1 read-only unless a concrete writable canvas requirement appears.
- Do not create canvas layout persistence until versioning/storage is justified.
- Add loading, stale, empty, and unsupported-artifact states.

Authoritative files:
- `backend/app/strategic_context_package.py`
- `backend/app/work_session_commands.py`
- `admin-ui/src/components/StrategicContextWorkspace.tsx`
- `admin-ui/src/components/StrategicContextMap.tsx`
- `admin-ui/src/components/StrategicContextRail.tsx`
- `admin-ui/src/components/StrategicContextDescriptor.tsx`
- `admin-ui/src/utils/strategicContextDescriptor.ts`

Acceptance gates:
- Backend fixture validates context package shape and bounded body/log behavior.
- Frontend renders each v1 canvas/artifact descriptor type.
- Mobile viewport tests prove the chat composer remains reachable without long scrolling.
- Review confirms no permanent dashboard clutter was added to chat.

## M6. Pipeline Monitoring and Task Sideboard

Status: Partial.

Purpose: Preserve passive monitoring of task flow while making task-level detail more useful and less cluttered.

Existing:
- `PipelineWorkspace` owns flow, board, archive, task status strip, task context rail, and tracked task stream.
- Existing `PipelineView` preserves parent/child groups, sidecars, off-page parent/dependency chips, filters, expand/collapse, active focus, group archive, clear wave deps, unarchive, PR/iteration/blocking/heartbeat badges, and task detail open.
- `TaskDetailDrawerSideboardContext` renders bounded context.
- Recent milestone added stale queued/provisioning/runner attention into Pipeline/sideboard flows.

Remaining work:
- Clean up Pipeline view spacing and visual hierarchy without losing information.
- Make task sideboard fully integrated:
  - open from Pipeline and Chat context
  - show task state, current blocker, artifact/PR, direction, parent wave, runner, cost, messages, and next action
  - avoid duplicating task rows or status labels
- Add backend-to-frontend fixture for task detail sideboard context.
- Ensure failed/blocked/parked/attention states are deduped and labeled correctly.
- Improve tracked stream affordances for passive monitoring:
  - what is running
  - what changed recently
  - what needs intervention
  - logs only when expanded or tracked
- Keep task-by-task management available but not the primary workflow.

Authoritative files:
- `backend/app/operator_attention.py`
- `backend/app/admin.py`
- `backend/app/admin_task_mutations.py`
- `admin-ui/src/components/PipelineWorkspace.tsx`
- `admin-ui/src/components/PipelineView.tsx`
- `admin-ui/src/components/TaskContextRail.tsx`
- `admin-ui/src/components/TaskDetailDrawer.tsx`
- `admin-ui/src/components/TaskDetailDrawerSideboardContext.tsx`
- `admin-ui/src/components/TaskStreamPanel.tsx`

Acceptance gates:
- Backend tests cover stale/blocked/failed/parked/local runner precedence.
- Frontend tests prove Now/task health summary labels are not masked by unrelated alerts.
- Task drawer fixture test proves sideboard context renders exact backend payload safely.
- Viewport tests prove Pipeline and sideboard do not overflow at mobile width.

## M7. Runtime, Devices, Accounts, Settings, and Project Management

Status: Partial.

Purpose: Make cost, accounts, devices, deployment health, and policy settings available when needed without cramming them into Chat or Pipeline.

Existing:
- Runtime workspace sections include overview, devices, compute, accounts, settings, analytics/health.
- Devices reuse fleet runners, warm VM, and capacity panels.
- Accounts reuse account/provider health panels.
- Project workspace rehosts project settings and project management.
- Settings section router exists and many settings forms have tests.

Remaining work:
- Remove modal-era assumptions from `SettingsSectionRouter` and section bodies.
- Split policy homes cleanly:
  - project policy: branches, build config, resource envelope, auto-review, auto-remediation, issue implementation, members
  - runtime policy: accounts, providers, deploy/orchestrator settings, notifications, advanced runtime config
- Ensure project management is not only a modal. Keep modal if useful, but provide navigable Project Manage state.
- Make Runtime Devices the obvious home for local runner registration, connection health, and preferred Strategic runner.
- Make Accounts/Usage show cost-relevant state with denominator/time-window context.
- Keep settings fields single-authority. Do not expose the same policy from conflicting places.

Authoritative files:
- `admin-ui/src/components/OperatorWorkspaces.tsx`
- `admin-ui/src/components/OperatorWorkspaceSections.ts`
- `admin-ui/src/components/SettingsSectionRouter.tsx`
- `admin-ui/src/components/RuntimeAccountsPanel.tsx`
- `admin-ui/src/components/FleetRunnersPanel.tsx`
- `admin-ui/src/components/WarmVmPanel.tsx`
- `admin-ui/src/components/ProjectManagementModal.tsx`
- `admin-ui/src/components/ProjectMembers.tsx`
- `admin-ui/src/hooks/useSettings.ts`

Acceptance gates:
- Tests prove every settings section remains reachable from its canonical home.
- Viewport tests pass for Runtime Devices, Accounts, Settings, and Project Manage.
- Review confirms no duplicated policy authority.

## M8. Operator Attention and Work-Session Read Model

Status: Partial.

Purpose: Give the operator a reliable "what needs me" and "what is happening now" model without forcing manual task management.

Existing:
- `operator_attention.py` derives high-signal message, question/blocker, approval, local runner hold, stale runner pin, stale provisioning, stale queued dependency, failed, blocked, parked, alert, and manual-intervention direction items.
- Attention items now include canonical targets for task/runtime routing.
- Work-session projections include actions, commands, curated metrics, events, jobs/receipts, selected refs, context package, and runtime work.
- Work-session command execution emits receipts.

Remaining work:
- Define lifecycle semantics for attention:
  - what clears automatically
  - what can be acknowledged
  - what must be dismissed on the underlying object
  - what is only watch-level
- Consider a lightweight acknowledgement model only if indirect clearing is not enough.
- Add metric registry:
  - id
  - source/provenance path
  - unit
  - time window
  - denominator
  - target
  - stale behavior
- Ensure read-model performance does not degrade as more metrics/events are added.
- Make stale Strategic local runner holds bounded, as covered in M2.
- Keep attention item count bounded while preserving high-priority ordering.

Authoritative files:
- `backend/app/operator_attention.py`
- `backend/app/strategic_runtime_work.py`
- `backend/app/work_session_actions.py`
- `backend/app/work_session_commands.py`
- `backend/app/work_session_command_execution.py`
- `backend/app/routers/admin_operator_attention.py`
- `backend/app/routers/admin_projects.py`
- `admin-ui/src/strategicStateApi.ts`
- `admin-ui/src/components/StrategicContextRail.tsx`

Acceptance gates:
- Backend tests cover every attention reason, target, count, and precedence rule.
- Frontend tests cover attention routing for task, messages, project, runtime/devices, and alert targets.
- Performance review confirms strategic-state remains acceptable or has caching/materialization.

## M9. Unified Event and Message Contracts

Status: Partial.

Purpose: Stop backend/frontend drift by making read models, events, messages, commands, and targets contract-driven.

Existing:
- `workbench_events.py` defines v1 envelope:
  - `schema_version`
  - `project_id`
  - `resource`
  - `action`
  - `refetch.surfaces`
- Event feed projects messages, tasks, alerts, Strategic cycles, local runners, compute jobs, and command receipts.
- Frontend consumes event envelopes as invalidation hints.
- Message creation/projection code is centralized enough to build from.

Remaining work:
- Inventory all websocket emitters:
  - mark as workbench v1 envelope
  - mark as legacy compatibility
  - migrate or document why legacy stays
- Add backend-owned golden fixtures or JSON schemas for:
  - `StrategicStateResponse`
  - `StrategicSessionResponse`
  - workbench event payload
  - command descriptor
  - context target
  - message arming outcome
  - task detail sideboard context
- Add frontend tests that consume generated backend fixtures.
- Create canonical target vocabulary constants:
  - surfaces: chat/messages, pipeline, project, runtime, task, knowledge
  - sections per surface
  - ids allowed per target type
- Normalize or reject unknown backend target sections before they reach UI.

Authoritative files:
- `backend/app/workbench_events.py`
- `backend/app/project_message_projection.py`
- `backend/app/project_message_creation.py`
- `backend/app/work_session_commands.py`
- `backend/app/work_session_command_execution.py`
- `admin-ui/src/strategicStateApi.ts`
- `admin-ui/src/types/workbenchEvents.ts`
- `admin-ui/src/hooks/useStrategicContextNavigation.ts`
- `admin-ui/src/hooks/useWorkbenchCommands.ts`

Acceptance gates:
- Backend contract tests generate or validate fixtures.
- Frontend contract tests consume those fixtures.
- Search/inventory test prevents new untracked websocket emitters.
- Review confirms no hand-built fixture is being treated as canonical.

## M10. Visual System and UI Polish

Status: Partial.

Purpose: Make the app feel like a polished developer tool: dark, dense where needed, calm, and visually consistent.

Existing:
- Dark developer-tool direction is in place.
- Chat has reduced chrome compared with older versions.
- Pipeline remains denser for monitoring.
- Many viewport tests exist.

Remaining work:
- Tokenize the dark theme:
  - base/background/surface/layer colors
  - borders
  - text hierarchy
  - focus rings
  - status colors
  - spacing scale
  - panel/header/button sizing
- Reduce hardcoded color literals like `#020407`, `#10151c`, and ad hoc bordered panels.
- Align header:
  - smaller logo
  - less bulky margins
  - no duplicate "message strategic" placement
  - command palette access near the composer where appropriate
- Align Chat layout to target:
  - simple thread
  - reachable composer
  - optional canvas slot
  - minimal project/session context
- Align Pipeline layout:
  - monitoring restored and easy to scan
  - no duplicate buttons
  - clear task sideboard
- Clean old style/state:
  - remove stale route names and old docs references
  - keep orphan component guard current
  - keep allowlisted orphan components only when justified

Authoritative files:
- `admin-ui/src/index.css`
- `admin-ui/tailwind.config.*`
- `admin-ui/src/components/HeaderBar.tsx`
- `admin-ui/src/components/WorkbenchLayout.tsx`
- `admin-ui/src/components/MessagesPanel.tsx`
- `admin-ui/src/components/PipelineWorkspace.tsx`
- `admin-ui/src/components/OperatorWorkspaces.tsx`
- `admin-ui/src/test/viewport-coverage.test.ts`
- `admin-ui/src/test/orphan-component-guard.test.ts`

Acceptance gates:
- Viewport screenshot review at desktop and mobile.
- Automated viewport tests for Chat, Pipeline, Project, Runtime, command palette, task drawer, and context workspace.
- Review confirms no one-note palette, oversized margins, hidden composer, duplicate buttons, or placeholder-looking panels.

## M11. Background Operator Jobs

Status: Planned.

Purpose: Support operator-triggered async work only where synchronous command receipts are not enough.

Existing:
- Compute jobs, runner subprocess jobs, parked wakes, and orchestrator loops already handle actual runtime work.
- Work-session jobs currently mean persisted command receipts in `ProjectMessage` context.
- Command execution is synchronous and idempotent.

Decision:
- Do not build a general `OperatorJob` system yet.
- Build only if a concrete operator action requires async progress, cancellation, retry, and history beyond existing compute/task/job models.

Possible triggers that might justify it later:
- Long-running local runner registration validation.
- Bulk policy migration across projects.
- Large artifact/canvas generation.
- Backfill or contract fixture generation from production data.

If justified, required deliverables:
- `OperatorJob` table or extension of an existing durable job model.
- Status model: queued, running, succeeded, failed, cancelled.
- Progress events through workbench envelope.
- Idempotency, retry, cancellation, and permission checks.
- UI surface in Runtime or command receipts, not primary Chat.

Acceptance gates:
- Design review proving existing Task/ComputeJob/ProjectMessage receipts cannot handle the case.
- Backend lifecycle tests.
- Frontend progress/cancel/retry tests.

## M12. Completion Audit and Hardening

Status: Planned.

Purpose: Prove the full operator workbench is usable, not just locally green.

Required audit checklist:
- Active work:
  - operator can chat with Strategic
  - Strategic can refer to context, numbers, artifacts, diagrams, or canvas descriptors
  - composer is always reachable on mobile and desktop
  - no unexplained fake controls are visible
- Passive monitoring:
  - operator can watch task flow in Pipeline
  - task statuses, blockers, stale states, runner holds, and recent changes are visible
  - task sideboard opens from relevant surfaces
- Runtime:
  - devices/local runners are visible and manageable
  - accounts/usage/cost-related state is reachable
  - deploy/orchestrator health is reachable
- Project:
  - running update, directions, problem model, design doc, project settings, and project management are reachable
  - project archive/task archive meanings are not confused
- Contracts:
  - backend fixtures drive frontend tests
  - event envelope inventory is complete
  - command/target vocabulary is canonical
- Visual:
  - desktop and mobile screenshots look professional
  - spacing/header/margins/font/color are consistent
  - old placeholder UI is removed or rehosted
- Safety:
  - dangerous actions show blast radius
  - stale data is visible
  - metrics have denominator/time-window context
  - settings that alter cost/risk are single-authority policy changes

Final verification gates:
- Backend focused suites:
  - `backend/testing/test_strategic_state.py`
  - `backend/testing/test_workbench_events.py`
  - `backend/testing/test_work_session_commands.py`
  - `backend/testing/test_work_session_actions.py`
  - `backend/testing/test_work_session_command_execution.py`
- Frontend focused suites:
  - `admin-ui/src/strategicStateApi.test.ts`
  - `admin-ui/src/operatorWorkbenchContracts.test.ts`
  - `admin-ui/src/types/workbenchEvents.test.ts`
  - `admin-ui/src/hooks/useWorkbenchCommands.test.tsx`
  - `admin-ui/src/hooks/useStrategicContextNavigation.test.tsx`
  - `admin-ui/src/components/MessagesPanel.test.tsx`
  - `admin-ui/src/components/PipelineWorkspace.test.tsx`
  - `admin-ui/src/components/TaskContextRail.test.tsx`
  - viewport tests for all primary surfaces
- Typecheck:
  - `npm run typecheck`
- Review:
  - backend subagent review
  - frontend subagent review
  - visual/UX review
  - integration-contract review

## Next Batch Recommendation

Recommended next implementation batch: M2 plus the contract parts that protect it.

Scope:
1. Add stale Strategic local hold timeout/recovery.
2. Add tests for Strategic local runner hold lifecycle and preferred-runner loss.
3. Tighten continuous-mode UI state in Runtime Devices and Strategic session controls.
4. Add/update contract tests for the affected attention/runtime fields.
5. Update this roadmap after push.

Reason: persistent local Strategic is one of the few remaining backend features that can make the workday mode genuinely reliable. It also directly affects the active-work Chat workflow and the Runtime Devices surface.

Parallelizable follow-up batch:
1. Chat canvas/artifact slot in frontend.
2. Command/target registry hardening.
3. Message contract fixture cleanup.
4. Visual token cleanup for shell/header/Chat/Pipeline.

## Update Log

| Date | Commit | Change |
| --- | --- | --- |
| 2026-07-01 | roadmap creation | Created full usability roadmap and milestone ledger. |
| 2026-07-01 | `33dd23e38` | Pushed stale operator attention paths: stale runner pins, provisioning, queued dependencies, Strategic local runner hold counts, frontend routing/sideboard integration, and regression tests. |
