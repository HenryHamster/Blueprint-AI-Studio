export type MessageType = 'QUESTION' | 'FEEDBACK' | 'BUG_REPORT' | 'BLOCKER' | 'NOTE' | string;
export type MessageFromType = 'user' | 'agent' | 'strategic' | 'system' | string;
export type MessageStatus = 'open' | 'linked' | 'resolved' | string;

export type CitationState = 'absent' | 'present' | 'indeterminate';

export interface ProvenanceWarnings {
  citation_state?: CitationState;
  broken_refs?: string[];
  ungrounded_refs?: string[];
}

export type ProjectMessageContext = Record<string, unknown> & {
  _provenance_warnings?: ProvenanceWarnings;
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
