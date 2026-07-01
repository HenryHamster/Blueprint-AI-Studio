# Operator Workbench Full Usability Roadmap

Last updated: 2026-07-01

This is the execution roadmap for making the operator workbench fully usable across backend and frontend. It turns the operating model in `docs/operator-experience-operating-model.md` and the UI mapping in `admin-ui/mockups/operator-ui-mapping.md` into implementation milestones.

The rule for this file: after every major batch, update the status ledger, milestone checklists, evidence links, and next-batch focus before pushing to `automain`.

## Status Ledger

Overall implementation estimate: 85%.

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
| M2. Persistent local Strategic workday mode | Done | Workbench state, 12-hour continuation markers, explicit marker ownership/lease diagnostics, duplicate-session protection, Strategic local runner requirement, local runner selection, attention for holds, stale Strategic hold timeout, and clearer Devices UI. | Only revisit explicit takeover or heartbeat fallback renewal if real multi-operator contention appears. |
| M3. Messages as the active-work interface | Done | Shared `ProjectMessage` creation/projection, bounded workbench message context, minimal Strategic-session Chat, backend/frontend arming fixtures. | Keep regression coverage while M4/M5 add richer command and context surfaces. |
| M4. Ctrl+K and hotkey operating layer | Partial | Command palette, shortcut hooks, server command descriptors, allowlisted command execution, shared target/refetch vocabulary for command routing. | Finish missing hotkeys and shortcut-help behavior. |
| M5. Strategic context and canvas/artifact surface | Done | Context package, selected refs, canvas descriptors, `StrategicContextWorkspace`, map/resource rows, Chat artifact slot, descriptor schema, renderer tests, golden fixtures. | Keep v1 read-only; revisit writable canvas/layout persistence only with a concrete requirement. |
| M6. Pipeline monitoring and task sideboard | Done | Pipeline workspace, task tree, task rail, stale attention routing, task sideboard context, task-sideboard contract, Chat task-target drawer routing, waiting-vs-blocked health labels. | Further tracked-stream affordance polish can continue without reopening the sideboard contract. |
| M7. Runtime, devices, accounts, settings, project management | Done | Canonical Project/Runtime settings homes, workspace-hosted settings router semantics, command/context routing to Project Manage and Runtime Settings/Accounts, reachability and viewport tests. | Keep regression coverage while remaining project/runtime policy work lands. |
| M8. Operator attention and work-session read model | Partial | Derived attention state, registry-backed curated metric metadata, actions, events, jobs, command receipts, and command receipts excluded from check-in deltas. | Finish lifecycle clearing semantics and performance review for larger event/metric volumes. |
| M9. Unified event and message contracts | Partial | Workbench event v1 envelope exists, project message creation/projection are centralized, backend/frontend target/refetch helpers now share the same vocabulary. | Inventory legacy emitters and converge backend/frontend fixtures. |
| M10. Visual system and UI polish | Done | Workbench theme tokens, shell/header/workspace token migration, raw-shell-color guard, Pipeline duplicate task-open cleanup, focused viewport assertions and capture review. | Extend token coverage opportunistically to lower-level panels without changing IA. |
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

Status: Done.

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
- Stale Strategic local runner holds now share the compute local-runner bounded timeout path and terminally fail with an operator alert instead of waiting forever.
- Runtime Devices and Strategic session controls now distinguish fresh, stale, and unknown continuation state; preferred runners; eligible live Strategic runners; connected runners; and Strategic vs compute local-runner holds.
- Continuous mode now uses a 12-hour workday continuation TTL instead of the generic 4-hour UI idle TTL.
- Continuation markers expose explicit owner/lease diagnostics in the backend read model and preserve fresh markers owned by another operator.
- Newer same-user workday leases are protected from stale duplicate tabs that try to clear or overwrite the marker.
- The Devices control warns when a fresh Workday marker is owned by another operator without showing raw owner ids.

Future considerations:
- Keep the marker project-level unless real multi-operator contention proves explicit takeover or per-operator markers are worth the added system surface.
- Consider heartbeat fallback renewal only if preferred-runner loss causes real workday interruption; current behavior remains conservative.

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

Status: Done.

Purpose: Make Chat the clean place to talk to Strategic, discuss plans/results, and let Strategic pull relevant context or artifacts into view.

Existing:
- `MessagesPanel` is a real page, not only a drawer.
- `MessagesComposer` supports minimal chat mode and Strategic session context.
- Strategic session message route exists.
- Message arming distinguishes steering, passive notes, routine events, not armed, and duplicates.
- Conversation filtering keeps user/operator/Strategic/high-signal messages visible.
- Message context can include session snapshots and selected refs.

Completed:
- `ProjectMessage` remains the durable source; shared creation/projection helpers own the canonical message shape while legacy aliases remain isolated to legacy readers.
- Operator workbench context keys are explicitly bounded before storage; raw `event_type` is namespaced to `user_event_type`, and raw `strategic_session`/internal receipt-key injection is dropped in favor of trusted snapshots and receipts.
- Message create/resolve/delete responses now carry stable status/id/message contracts where the route can do so without breaking legacy callers.
- Primary Chat uses the Strategic-session message route and minimal composer/list chrome; search, saved views, resolved visibility, batch actions, noise preferences, delete/resolve, convert-to-direction, and investigation controls stay in the secondary triage surface.
- Strategic context is attached by bounded `strategic_session` snapshots with selected refs, selected-ref descriptors, and context package descriptors instead of copying artifacts inline.
- Routine events stay out of the active conversation and arming/unread copy distinguishes steering, passive notes, routine events, failed arming, and idempotent duplicates.

Evidence:
- Backend tests cover operator context caps/reserved keys, arming outcomes, strategic-session snapshots, idempotent duplicates, and delete response shape.
- Frontend tests consume backend-like arming fixtures for all five outcomes and keep primary Chat in minimal Strategic-session mode.

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

Completed in latest batch:
- Added shared backend target/refetch vocabulary in `backend/app/workbench_targets.py`.
- Reused that contract from work-session commands, command execution receipts/jobs, action targets, and event-feed target/refetch projection.
- Added shared frontend target/refetch normalization in `admin-ui/src/types/workbenchTargets.ts`.
- Routed command descriptors, attention targets, context navigation, and websocket refetch handling through the frontend helper without changing the public command-palette shape.
- Preserved legacy `runtime/runners` target links by aliasing them to the current Runtime Devices section.

Remaining work:
- Complete the command registry/contract beyond target/refetch normalization:
  - command id
  - label
  - group
  - priority
  - target
  - refetch surfaces
  - executable vs navigation-only
  - mutation allowlist
- Continue removing duplicated command semantics between backend and frontend where the command id and mutation allowlists still exist in both places.
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
- `backend/app/workbench_targets.py`
- `admin-ui/src/components/CommandPalette.tsx`
- `admin-ui/src/components/CommandPaletteHost.tsx`
- `admin-ui/src/hooks/useWorkbenchCommands.ts`
- `admin-ui/src/hooks/useOperatorKeyboardShortcuts.ts`
- `admin-ui/src/hooks/useWorkbenchShortcutRouter.ts`
- `admin-ui/src/hooks/useCommandPaletteShortcut.ts`
- `admin-ui/src/types/workbenchTargets.ts`

Acceptance gates:
- Backend tests cover every server command id and malformed/disabled/stale command rejection.
- Frontend tests cover keyboard routing and all canonical targets.
- Command palette viewport tests pass without overflow.
- Review confirms every visible command either navigates somewhere real or executes an allowlisted mutation.

## M5. Strategic Context and Canvas/Artifact Surface

Status: Done.

Purpose: Let Strategic pull up actual numbers, diagrams, plots, resources, and task context while the main chat stays clean.

Existing:
- `backend/app/strategic_context_package.py` builds a bounded read-only context package.
- Package includes project, running update, problem model, directions, messages, tasks, attention, knowledge docs, artifacts, cycles, active task, selected refs, resources, and canvas descriptors.
- Canvas descriptor has nodes/edges and an open command.
- Frontend has `StrategicContextWorkspace`, `StrategicContextMap`, context rail, descriptor chips, and selected-ref handling.

Completed in this milestone:
- Added stable v1 renderable descriptor schema in `admin-ui/src/strategicStateApi.ts` for context map, metric cards, table, markdown/text, image, chart/plot, task/direction/message/resource detail, and unsupported states.
- Added backend descriptor generation in `backend/app/strategic_context_package.py` from the existing bounded context package. The contract stays read-only and does not expose artifact logs, storage URIs, or knowledge bodies.
- Added `StrategicContextArtifactSlot` and rendered it inside `StrategicContextRail`, giving Chat a first-class desktop canvas/artifact slot while preserving existing selected-ref controls and the mobile context drawer path.
- Added backend golden fixture `backend/testing/__fixtures__/operator_strategic_context_package_v1.json` and frontend/backend tests that render/validate the exact v1 descriptor set.
- Added empty/unsupported/stale-ready rendering states in the artifact slot. Writable canvas and layout persistence remain explicit non-goals.

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

Status: Done.

Purpose: Preserve passive monitoring of task flow while making task-level detail more useful and less cluttered.

Existing:
- `PipelineWorkspace` owns flow, board, archive, task status strip, task context rail, and tracked task stream.
- Existing `PipelineView` preserves parent/child groups, sidecars, off-page parent/dependency chips, filters, expand/collapse, active focus, group archive, clear wave deps, unarchive, PR/iteration/blocking/heartbeat badges, and task detail open.
- `TaskDetailDrawerSideboardContext` renders bounded context.
- Recent milestone added stale queued/provisioning/runner attention into Pipeline/sideboard flows.

Completed in this milestone:
- Extended `backend/app/admin.py` task sideboard context with a bounded `task` block for task state, current blocker, runner, cost, and next action, alongside existing project/strategic/pipeline/artifact/message/link groups.
- Updated `TaskDetailDrawerSideboardContext` and `TaskSideboardContext` types to render the backend task block without exposing raw project model or task payload internals.
- Routed Chat/Strategic context `surface: task` targets through the task drawer callback while preserving Pipeline task selection for context.
- Split queued dependency waits from true blocked work in Pipeline sideboard health and Now labels. Waiting tasks no longer inflate blocked/attention counts.
- Added backend golden fixture `backend/testing/__fixtures__/operator_task_sideboard_context_v1.json` and frontend/backend tests for the backend-to-frontend sideboard contract.
- Added Pipeline/Strategic rail viewport coverage for the changed sideboard and artifact-slot surfaces.

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

Status: Done.

Purpose: Make cost, accounts, devices, deployment health, and policy settings available when needed without cramming them into Chat or Pipeline.

Existing:
- Runtime workspace sections include overview, devices, compute, accounts, settings, analytics/health.
- Devices reuse fleet runners, warm VM, and capacity panels.
- Accounts reuse account/provider health panels.
- Project workspace rehosts project settings and project management.
- Settings section router exists and many settings forms have tests.

Future considerations:
- Keep Runtime Devices aligned with M2 local-runner follow-up without editing the M2-owned runner panels in this batch.
- Continue moving any future project policy controls through the canonical Project Manage home.

Batch completed 2026-07-01:
- Added canonical `SETTINGS_SECTION_HOMES`, `PROJECT_POLICY_SECTIONS`, and `RUNTIME_POLICY_SECTIONS` so each settings section has one declared Project or Runtime home.
- Made `SettingsSectionRouter` host semantics explicit with `hostKind="workspace"` vs modal close behavior.
- Routed command palette and Strategic context targets through canonical homes for Project Manage, Runtime Accounts, and Runtime Settings sections.
- Added reachability tests for every project policy and runtime policy home plus workspace/modal router behavior.
- Added viewport coverage for Runtime Accounts desktop/mobile and Project Manage mobile reachability/touch targets.

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
- Curated work-session metrics are now registry-backed with source path, unit, window, denominator, target, and stale behavior metadata while preserving the existing UI-facing IDs/labels/values.
- Check-in deltas now ignore operator command receipts, so internal command acknowledgements do not inflate "since last check-in" activity counts.

Remaining work:
- Define lifecycle semantics for attention:
  - what clears automatically
  - what can be acknowledged
  - what must be dismissed on the underlying object
  - what is only watch-level
- Consider a lightweight acknowledgement model only if indirect clearing is not enough.
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

Completed in latest batch:
- Added backend-owned target/refetch constants for workbench targets and invalidation surfaces.
- Event envelopes now filter/dedupe refetch surfaces against the canonical list and fall back to `strategic_session` when needed.
- Event-feed items now normalize targets before projection, so unsupported target surfaces are dropped instead of being handed to the UI.
- Operator command receipts are normalized consistently on persistence and projection.
- Frontend websocket helpers now filter refetch surfaces through the same known surface list.
- Frontend target routing now rejects unknown target surfaces and normalizes constrained sections before changing tabs.

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
- Expand canonical target fixture coverage from helper tests into backend-generated golden fixtures.

Authoritative files:
- `backend/app/workbench_events.py`
- `backend/app/workbench_targets.py`
- `backend/app/project_message_projection.py`
- `backend/app/project_message_creation.py`
- `backend/app/work_session_commands.py`
- `backend/app/work_session_command_execution.py`
- `admin-ui/src/strategicStateApi.ts`
- `admin-ui/src/types/workbenchTargets.ts`
- `admin-ui/src/types/workbenchEvents.ts`
- `admin-ui/src/hooks/useStrategicContextNavigation.ts`
- `admin-ui/src/hooks/useWorkbenchCommands.ts`

Acceptance gates:
- Backend contract tests generate or validate fixtures.
- Frontend contract tests consume those fixtures.
- Search/inventory test prevents new untracked websocket emitters.
- Review confirms no hand-built fixture is being treated as canonical.

## M10. Visual System and UI Polish

Status: Done.

Purpose: Make the app feel like a polished developer tool: dark, dense where needed, calm, and visually consistent.

Existing:
- Dark developer-tool direction is in place.
- Chat has reduced chrome compared with older versions.
- Pipeline remains denser for monitoring.
- Many viewport tests exist.

Future considerations:
- Extend token migration deeper into low-level panels when those panels are otherwise touched.
- Keep screenshot capture review in later completion-audit batches.

Batch completed 2026-07-01:
- Added Workbench dark-theme tokens for base/shell/surface/rail/panel/layer/card/control colors, borders, focus rings, compact spacing, and text hierarchy.
- Migrated `WorkbenchLayout`, `HeaderBar`, `OperatorWorkspaces`, `RuntimeAccountsPanel`, `MessagesPanel`, and `PipelineWorkspace` primary shell surfaces to semantic workbench tokens.
- Added `workbench-theme-tokens.test.ts` to prevent primary shell files from reintroducing raw dark `bg-[#...]` / hex color literals.
- Tightened focus rings to render inside scroll containers and restored 44px mobile touch targets on Project header actions.
- Removed the duplicate Pipeline Now-row open button while keeping the row itself as the accessible open target.
- Captured and reviewed representative mobile/desktop PNGs for Header, Messages, Runtime Accounts, Project Manage, Runtime Devices/Settings/Compute, and Pipeline.

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

Recommended next implementation batches: continue M4/M9 and M5/M6, then run M12 audit loops.

Scope:
1. M4/M9: harden Ctrl+K commands, context targets, and backend/frontend fixtures.
2. M5/M6: add first-class Chat canvas/artifact rendering and task sideboard fixture coverage.
3. Keep M7/M10 token and settings-home regression coverage current while lower-level panels change.
4. Run M8 lifecycle/performance audit checks while contract fixtures expand.
5. Start M12 completion audit passes once M5/M6/M9 have their fixture coverage.

Reason: the highest-risk M2 reliability gap is closed, M3 has landed, and M7/M10 cleanup has landed; the next material progress is making command/contracts, canvas/sideboard, and read-model audit hardening move in parallel.

## Update Log

| Date | Commit | Change |
| --- | --- | --- |
| 2026-07-01 | M8 read-model metrics | Added registry-backed work-session metric metadata and excluded operator command receipts from check-in delta counts. |
| 2026-07-01 | M2 local Strategic reliability | Added bounded stale Strategic local runner hold timeout/recovery, frontend Devices/session state clarity, and focused tests. |
| 2026-07-01 | roadmap creation | Created full usability roadmap and milestone ledger. |
| 2026-07-01 | `33dd23e38` | Pushed stale operator attention paths: stale runner pins, provisioning, queued dependencies, Strategic local runner hold counts, frontend routing/sideboard integration, and regression tests. |
| 2026-07-01 | M7/M10 runtime and visual polish | Implemented M7/M10 cleanup: canonical settings homes, workspace router host semantics, workbench theme tokens, primary shell token migration, Pipeline duplicate-open cleanup, reachability tests, focused typecheck/unit/viewport/capture evidence. |
| 2026-07-01 | M2 continuation ownership/TTL | Added 12-hour workday continuation TTL, owner/lease read-model fields, foreign-owner and duplicate-session marker protection, Devices ownership warning, and focused backend/frontend tests. |
