// Response shapes of the Cavman API (src/cavman/views.py and api.py).

export type RunState =
  | "starting"
  | "planning"
  | "running"
  | "revision_required"
  | "recovering"
  | "stopping"
  | "approval_needed"
  | "waiting"
  | "blocked"
  | "paused"
  | "budget_reached"
  | "failed"
  | "cancelled"
  | "complete";

export type ModelMode = "automatic" | "budget" | "balanced" | "quality";

export type Stage = "understand" | "plan" | "build" | "review" | "deliver";

export type TaskState =
  | "Waiting"
  | "Ready"
  | "Running"
  | "Validating"
  | "Reviewing"
  | "Revision Needed"
  | "Needs Approval"
  | "Blocked"
  | "Failed"
  | "Accepted"
  | "Replaced"
  | "Cancelled";

export type CheckStatus = "passed" | "failed" | "not_run";

export interface RunSummary {
  id: string;
  project_id: string;
  project_name: string;
  prompt: string;
  executor: "provider" | "scripted";
  status: "active" | "completed" | "abandoned";
  state: RunState;
  label: string;
  explanation: string;
  stage: Stage;
  task_counts: Partial<Record<TaskState, number>>;
  tasks_total: number;
  tasks_accepted: number;
  pending_approvals: number;
  failures: number;
  cost_usd: number;
  cost_complete: boolean;
  model_calls: number;
  created_at: string;
  updated_at: string;
}

export interface RequiredCheck {
  check: string;
  label: string;
  status: CheckStatus;
}

export interface TaskView {
  id: string;
  title: string;
  specialist: string;
  deliverable: string;
  acceptance_criteria: string[];
  status: string;
  state: TaskState;
  capability: string;
  capability_label: string;
  dependencies: string[];
  attempt: number;
  max_attempts: number;
  revisions: number;
  blocker: string | null;
  required_checks: RequiredCheck[];
  review_required: boolean;
  artifact_ids: string[];
  latest_artifact_id: string | null;
  failure_count: number;
  latest_event: { title: string; created_at: string } | null;
  usage: { calls: number; models: string[]; cost_usd: number } | null;
  updated_at: string;
}

export interface ValidationView {
  id: string;
  check: string;
  label: string;
  status: "passed" | "failed";
  trusted: boolean;
  executor: string;
  command: string | null;
  returncode: number | null;
  output: string;
  content_digest: string;
  created_at: string;
}

export interface ReviewView {
  id: string;
  status: "passed" | "changes_requested";
  reviewer: string;
  reason: string;
  evidence: string[];
  content_digest: string;
  created_at: string;
}

export interface ArtifactView {
  id: string;
  task_id: string;
  task_title: string;
  specialist: string | null;
  kind: "code_change" | "document";
  status: "candidate" | "accepted" | "rejected" | "superseded";
  version: number;
  predecessor_id: string | null;
  input_artifact_ids: string[];
  content_digest: string;
  workspace_fingerprint: string | null;
  workspace_id: string | null;
  integrated_commit: string | null;
  changed_files: string[];
  validation_state: "passed" | "failed" | "partial" | "not_run";
  review_state: "passed" | "changes_requested" | "not_reviewed";
  validations: ValidationView[];
  reviews: ReviewView[];
  summary: string;
  content_chars: number;
  created_at: string;
  content?: string;
  diff?: string | null;
}

export interface ApprovalView {
  id: string;
  action: string;
  category: string;
  title: string;
  what: string;
  why: string;
  changes: string[];
  risk: string;
  target: string;
  scope: Record<string, unknown>;
  scope_digest: string;
  artifact_refs: string[];
  required: boolean;
  status: "pending" | "approved" | "rejected" | "superseded";
  created_at: string;
  decided_at: string | null;
  decision: { approved: boolean; reason: string; decided_by: string } | null;
}

export interface FailureView {
  id: string;
  task_id: string;
  task_title: string;
  classification: string;
  explanation: string;
  evidence: string;
  created_at: string;
  recovery: { action: string; label: string; reason: string; created_at: string } | null;
}

export interface TimelineEvent {
  sequence: number;
  kind: string;
  title: string;
  detail: string | null;
  level: "info" | "success" | "warning" | "error" | "attention";
  task_id: string | null;
  debug: boolean;
  created_at: string;
}

export interface UsageView {
  calls: number;
  cost_usd: number;
  cost_complete: boolean;
  calls_without_cost: number;
  tokens: { input_tokens: number; output_tokens: number; cached_tokens: number; total_tokens: number };
  by_model: Record<string, { calls: number; total_tokens: number; cost_usd: number; calls_without_cost: number }>;
  by_role: Record<string, { calls: number; total_tokens: number; cost_usd: number; calls_without_cost: number }>;
  budget: {
    limit_usd: number;
    spent_usd: number;
    remaining_usd: number;
    max_model_calls: number;
    model_calls: number;
    warning: boolean;
    exceeded: boolean;
  } | null;
}

export interface JobView {
  id: string;
  kind: "start" | "continue" | "recover";
  status: "queued" | "running" | "succeeded" | "failed" | "cancelled";
  outcome: string | null;
  outcome_text: string | null;
  message: string | null;
  cancel_requested: boolean;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
}

export interface DeliveryView {
  status: "ready" | "failed";
  created_at: string;
  error: string | null;
  files: string[];
  documents: string[];
  deleted: string[];
  total_files: number | null;
  commit: string | null;
  report: string | null;
  downloadable: boolean;
}

export interface RunDetail extends RunSummary {
  objective: string;
  constraints: string[];
  criteria: string[];
  tasks: TaskView[];
  artifacts: ArtifactView[];
  accepted_artifact_ids: string[];
  approvals: ApprovalView[];
  capability_requests: {
    id: string;
    task_id: string;
    requested_capability: string;
    reason: string;
    risk: string;
    status: string;
    created_at: string;
  }[];
  failure_details: FailureView[];
  recovery: {
    revision: number;
    replans_remaining: number;
    proposals: {
      id: string;
      trigger: string;
      base_revision: number;
      adds: number;
      removes: number;
      reopens: number;
      requires_approval: boolean;
      status: string;
    }[];
  };
  unresolved_issues: string[];
  usage: UsageView;
  jobs: JobView[];
  event_cursor: number;
  timeline: TimelineEvent[];
  final_result: string | null;
  delivery: DeliveryView | null;
  publication?: { repository: string; url: string; commit: string; private: boolean; created_at: string } | null;
}

export interface ProjectSource {
  url: string;
  commit: string;
  branch: string;
  files: number;
  dropped: string[];
}

export interface ProjectView {
  id: string;
  name: string;
  description: string;
  settings: Record<string, unknown> & { source?: ProjectSource };
  created_at: string;
  updated_at: string;
  run_count: number;
  latest_run: RunSummary | null;
  runs?: RunSummary[];
  next?: string | null;
}

export interface SystemView {
  version: string;
  executor: "provider" | "scripted";
  model_modes: { mode: ModelMode; available: boolean; manager_model: string | null; worker_model: string | null }[];
  provider: { configured: boolean; provider?: string; manager_model?: string; worker_model?: string; problem?: string };
  sandbox: { available: boolean; bubblewrap: boolean; prlimit: boolean };
  budget: { default_usd: number; max_usd: number; default_max_model_calls: number; warning_ratio: number };
  capabilities: { github_publish: boolean; previews: boolean; sandbox_toolchains: string[] };
}

export interface AccountSpending {
  period_start: string;
  spent_usd: number;
  limit_usd: number;
  remaining_usd: number;
  model_calls: number;
  max_model_calls: number;
  remaining_calls: number;
  calls_without_cost: number;
  cost_complete: boolean;
  exhausted: boolean;
  warning: boolean;
}

export interface ModeCostHistory {
  builds: number;
  median_usd: number | null;
  low_usd: number | null;
  high_usd: number | null;
}

export interface EstimateView {
  window_days: number;
  min_builds: number;
  modes: Record<string, ModeCostHistory>;
  default_budget_usd: number;
  max_budget_usd: number;
  default_max_model_calls: number;
  account: {
    remaining_usd: number;
    limit_usd: number;
    remaining_calls: number;
    max_model_calls: number;
    cost_complete: boolean;
    calls_without_cost: number;
    exhausted: boolean;
  };
}
