export type ApplicationState =
  | 'DISCOVERED'
  | 'IMPORTED'
  | 'SCREENING'
  | 'ELIGIBLE'
  | 'SKIPPED'
  | 'QUEUED'
  | 'IN_PROGRESS'
  | 'AUTH_REQUIRED'
  | 'NEEDS_USER_INPUT'
  | 'READY_FOR_REVIEW'
  | 'SUBMISSION_PENDING'
  | 'APPLIED_CONFIRMED'
  | 'APPLICATION_FAILED'
  | 'RETRY_SCHEDULED'
  | 'DUPLICATE'
  | 'CANCELLED';

export const ALL_STATES: ApplicationState[] = [
  'DISCOVERED',
  'IMPORTED',
  'SCREENING',
  'ELIGIBLE',
  'SKIPPED',
  'QUEUED',
  'IN_PROGRESS',
  'AUTH_REQUIRED',
  'NEEDS_USER_INPUT',
  'READY_FOR_REVIEW',
  'SUBMISSION_PENDING',
  'APPLIED_CONFIRMED',
  'APPLICATION_FAILED',
  'RETRY_SCHEDULED',
  'DUPLICATE',
  'CANCELLED',
];

export interface Failure {
  category: string;
  message: string;
  retryable: boolean;
  recommended_action: string;
  details?: Record<string, unknown>;
}

export interface Job {
  id: string;
  company: string | null;
  title: string | null;
  location: string | null;
  remote_type: string | null;
  source_kind: string;
  canonical_url: string;
  apply_url: string | null;
  match_score: number | null;
  match_explanation_json: Record<string, unknown> | null;
  filter_result_json: { passed: boolean; reasons: string[]; notes: string[] } | null;
  posted_at: string | null;
  created_at: string;
  duplicate_of: string | null;
  application_id?: string | null;
  application_state?: ApplicationState | null;
  last_attempt_at?: string | null;
  next_attempt_at?: string | null;
  failure_json?: Failure | null;
  description?: string | null;
  requirements_json?: string[];
}

export interface Application {
  id: string;
  job_id: string;
  state: ApplicationState;
  verification_status: 'confirmed' | 'uncertain' | 'rejected' | 'not_attempted';
  reference_number: string | null;
  application_url: string | null;
  resume_label?: string | null;
  applied_at: string | null;
  attempt_count: number;
  next_attempt_at: string | null;
  last_attempt_at: string | null;
  notes: string | null;
  updated_at: string;
  created_at: string;
  company?: string | null;
  title?: string | null;
  location?: string | null;
  failure_json: Failure | null;
  evidence_json: Record<string, unknown> | null;
  pending_questions_json: PendingQuestion[] | null;
  fill_plan_json: Record<string, unknown> | null;
}

export interface PendingQuestion {
  node?: number;
  label: string;
  status?: string;
  reason?: string;
  legally_significant?: boolean;
  options?: string[] | null;
  field_type?: string;
}

export interface Transition {
  id: number;
  from_state: ApplicationState | null;
  to_state: ApplicationState;
  reason: string | null;
  actor: string;
  created_at: string;
}

export interface Task {
  id: string;
  kind: string;
  status: string;
  instruction: string | null;
  application_id: string | null;
  action_count: number;
  model_calls: number;
  failure_json: Failure | null;
  result_json: Record<string, unknown> | null;
  progress_json: Record<string, unknown> | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
}

export interface Overview {
  jobs_discovered: number;
  jobs_duplicates: number;
  eligible: number;
  queued: number;
  in_progress: number;
  confirmed: number;
  awaiting_review: number;
  failed: number;
  auth_required: number;
  skipped: number;
  by_state: Record<string, number>;
  rates: { success_rate: number | null; failure_rate: number | null; denominator: string; attempted: number };
  recent_activity: AuditEvent[];
  paused: boolean;
  extension_connected: boolean;
  pending_approvals: number;
}

export interface AuditEvent {
  id: number;
  ts: string;
  level: string;
  event: string;
  task_id: string | null;
  application_id: string | null;
  job_id: string | null;
  data_json: Record<string, unknown>;
}

export interface QueueSummary {
  queued: number;
  retry_scheduled: number;
  pending_tasks: number;
  running_queue_tasks: number;
  total: number;
}

export interface ClearQueueResult extends QueueSummary {
  applications_cancelled: number;
  tasks_cancelled: number;
  runs_stopped: number;
}

export interface Paged<T> {
  items: T[];
  total: number;
  limit?: number;
  offset?: number;
}

export interface Profile {
  contact: Record<string, string>;
  summary: string;
  skills: string[];
  experience_years: number | null;
  education: Record<string, string>[];
  employment: Record<string, string | boolean>[];
  projects: Record<string, string>[];
  links: Record<string, string>;
  preferences: Record<string, unknown>;
  legal: Record<string, string>;
  cover_letter_template: string;
  default_resume_id: string | null;
}

export interface Answer {
  id: string;
  category: string;
  question: string;
  answer: string;
  provenance: string;
  confidence: number;
  legally_significant: number | boolean;
}

export interface Resume {
  id: string;
  label: string;
  file_name: string;
  size_bytes: number;
  mime_type: string;
  created_at: string;
}

export interface SchedulerConfig {
  enabled: boolean;
  daily_application_limit: number;
  max_concurrent_tasks: number;
  per_domain_concurrency: number;
  per_domain_delay_seconds: number;
  allowed_domains: string[];
  active_hours_start: number;
  active_hours_end: number;
  active_days: number[];
  max_retries: number;
  retry_base_seconds: number;
  approval_policy: 'always' | 'never' | 'per_site';
  auto_pause_on_resource_pressure: boolean;
  wake_interval_seconds: number;
  max_ram_mb: number;
  cpu_threads: number;
  gpu_enabled: boolean;
  model_roles: Record<string, { provider: string; model: string; temperature: number; max_output_tokens: number }>;
}

export interface SettingsResponse {
  scheduler: SchedulerConfig;
  model_roles: Record<string, { provider: string; model: string; temperature: number; max_output_tokens: number }>;
  runtime: Record<string, unknown>;
  allowed_domains: string[];
  adapters: { name: string; supported_operations: string[]; access_policy: string; verification_method: string; supported: boolean }[];
}

export interface Diagnostics {
  resources: Record<string, number>;
  model_metrics: { roles: { role: string; provider: string; model: string; calls: number; cached: number; errors: number; p50_ms: number | null; p95_ms: number | null }[]; total_calls: number };
  model_gateway: { calls: number; cache_hits: number; failures: number };
  extension: { connected: boolean; connected_at: string | null; active_tab: Record<string, unknown> | null };
  scheduler: { enabled: boolean; running: boolean; last_tick: string | null; last_reason: string; within_active_hours: boolean; paused: boolean };
  running_tasks: string[];
  current_task: Record<string, unknown> | null;
  db: { schema_version: number; path: string };
  errors_24h: AuditEvent[];
}

export interface Approval {
  id: string;
  kind: string;
  status: string;
  payload_json: Record<string, unknown>;
  task_id: string | null;
  application_id: string | null;
  created_at: string;
}
