export interface Alert {
  id: string;
  project_id: string;
  severity: 'INFO' | 'WARNING' | 'ERROR';
  title: string;
  message: string;
  domain?: string | null;
  event_type?: string | null;
  source?: string | null;
  dedupe_key?: string | null;
  occurrence_count?: number | null;
  last_seen_at?: string | null;
  task_id: string | null;
  created_at: string;
  dismissed_at: string | null;
  acknowledged_at: string | null;
}

export interface AlertFilters {
  projectId?: string | null;
  domain?: string | null;
  eventType?: string | null;
  source?: string | null;
}

export interface ProjectAlertDomain {
  domain: string;
  label: string;
  description: string;
  enabled: boolean;
  configured: boolean;
  default_enabled: boolean;
  core: boolean;
}

export interface ProjectAlertDomainsResponse {
  project_id: string;
  domains: ProjectAlertDomain[];
}
