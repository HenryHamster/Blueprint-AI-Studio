export interface Alert {
  id: string;
  project_id: string;
  severity: 'INFO' | 'WARNING' | 'ERROR';
  title: string;
  message: string;
  task_id: string | null;
  created_at: string;
  dismissed_at: string | null;
}
