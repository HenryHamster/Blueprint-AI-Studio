export type MessageType = 'QUESTION' | 'FEEDBACK' | 'BUG_REPORT' | 'BLOCKER' | 'NOTE' | string;
export type MessageFromType = 'user' | 'agent' | 'strategic' | 'system' | string;
export type MessageStatus = 'open' | 'linked' | 'resolved' | string;

export type CitationState = 'absent' | 'present' | 'indeterminate';

export interface ProvenanceWarnings {
  citation_state?: CitationState;
  broken_refs?: string[];
  ungrounded_refs?: string[];
}

export interface ProjectMessageContextTarget {
  surface?: string | null;
  section?: string | null;
  project_id?: string | null;
  task_id?: string | null;
  message_id?: string | null;
  direction_id?: string | null;
  alert_id?: string | null;
  action?: string | null;
  document_id?: string | null;
  artifact_id?: string | null;
  runner_id?: string | null;
  cycle_id?: string | null;
}

export interface ProjectMessageSelectedRef {
  kind: string;
  id: string;
  label?: string;
}

export interface ProjectMessageSelectedRefDescriptor {
  kind: string;
  id: string;
  label?: string;
  summary?: string;
  status?: string;
  target?: ProjectMessageContextTarget;
}

export interface ProjectMessageStrategicContextDescriptor {
  id: string;
  kind?: string;
  label?: string;
  summary?: string;
  status?: string;
  target?: ProjectMessageContextTarget;
}

export interface ProjectMessageStrategicSessionContext {
  version?: 1 | number;
  interaction_mode?: string | null;
  active_surface?: string | null;
  session_started_at?: string | null;
  last_interaction_at?: string | null;
  captured_at?: string | null;
  context_package_id?: string | null;
  selected_refs?: ProjectMessageSelectedRef[];
  selected_ref_descriptors?: ProjectMessageSelectedRefDescriptor[];
  context_package_descriptor?: ProjectMessageStrategicContextDescriptor;
}

export interface ProjectMessageOperatorCommandContext extends Record<string, unknown> {
  version?: 1 | number;
  command_id?: string | null;
  label?: string | null;
  status?: string | null;
  target?: ProjectMessageContextTarget | null;
  refetch_surfaces?: string[];
  result?: Record<string, unknown> | null;
}

export interface ProjectMessageCheckInContext extends Record<string, unknown> {
  version?: 1 | number;
  kind?: string | null;
  operator_user_id?: string | null;
  captured_at?: string | null;
}

export interface MessageAttachment {
  type: string;
  mime_type: string;
  data?: string;
  name?: string;
  size_bytes?: number;
  url?: string;
  thumbnail_url?: string;
  /** Internal stable key for React reconciliation; never serialized. */
  _attachmentKey?: string;
}

export type ProjectMessageContext = Record<string, unknown> & {
  event_type?: string;
  user_event_type?: string;
  reply_to?: string;
  reply_preview?: string;
  answer?: string;
  resolution?: string;
  direction_id?: string;
  task_id?: string;
  source_task_id?: string;
  source_message_id?: string;
  dedupe_key?: string;
  operator_command?: ProjectMessageOperatorCommandContext | string;
  check_in?: ProjectMessageCheckInContext | string;
  strategic_session?: ProjectMessageStrategicSessionContext;
  _provenance_warnings?: ProvenanceWarnings;
  attachments?: MessageAttachment[];
};

export interface ProjectMessage {
  id: string;
  project_id: string;
  message_type: MessageType;
  from_type: MessageFromType;
  from_task_id: string | null;
  content: string;
  context: ProjectMessageContext;
  status: MessageStatus;
  resolution: string | null;
  resolved_at: string | null;
  created_at: string | null;
}

export type ProjectMessagePreview = ProjectMessage;
