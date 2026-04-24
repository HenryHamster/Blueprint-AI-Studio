export type { Task, TaskStatus, TaskType, ResearchFinding } from './task';
export type { Project, Direction } from './project';
export type { DeployState, MaxPlanAccount, OrchestratorHealth, ZoneHealthEntry } from './settings';
export type { Alert } from './alert';
export type { StuckTask, ProjectDiagnostics } from './diagnostics';
export type {
  ProblemModel, Intent, ProjectContext, Capability, Gap, Friction,
  PendingConfirmation, Source, SourceType, GapSize, GapPriority, GapStatus,
  FrictionStatus, PendingConfirmationType, CapabilityStatus,
  SelfReflectionEntry, ProblemModelPatch,
  DirectionsEndpointResponse, GapHistoryEvent, RevisionLogEntry,
} from './problemModel';

/** Budget utilization fields shared between DirectionCard and DirectionProgressSection. */
export interface DirectionBudgetInfo {
  estimated_cost_usd?: number | null;
  cost_budget_usd?: number | null;
  budget_utilization_pct?: number | null;
  budget_exceeded?: boolean;
}
