import type { Mood } from "@/components/brand/cavman";
import type { CheckStatus, RunState, Stage, TaskState } from "./types";

export type Tone = "ember" | "glacier" | "ok" | "bad" | "warn" | "review" | "neutral";

export const RUN_TONE: Record<RunState, Tone> = {
  starting: "glacier",
  planning: "glacier",
  running: "glacier",
  revision_required: "review",
  recovering: "warn",
  stopping: "warn",
  approval_needed: "ember",
  input_needed: "ember",
  waiting: "warn",
  blocked: "warn",
  paused: "neutral",
  budget_reached: "warn",
  failed: "bad",
  cancelled: "neutral",
  complete: "ok",
};

/** What the pixel Cavman on a run is doing: digging while it builds, hiding when it needs you, asleep when paused. */
export const RUN_MOOD: Record<RunState, Mood> = {
  starting: "dig",
  planning: "dig",
  running: "dig",
  revision_required: "dig",
  recovering: "dig",
  stopping: "idle",
  approval_needed: "hide",
  input_needed: "hide",
  waiting: "hide",
  blocked: "hide",
  paused: "sleep",
  budget_reached: "sleep",
  failed: "hide",
  cancelled: "sleep",
  complete: "cheer",
};

/** States in which the backend is actively executing; the UI must not offer "continue". */
export const EXECUTING: ReadonlySet<RunState> = new Set(["starting", "planning", "running", "revision_required", "recovering", "stopping"]);

export function isExecuting(state: RunState): boolean {
  return EXECUTING.has(state);
}

export function canContinue(state: RunState, status: string): boolean {
  return status === "active" && !isExecuting(state) && state !== "approval_needed";
}

export const TASK_TONE: Record<TaskState, Tone> = {
  Waiting: "neutral",
  Ready: "glacier",
  Running: "glacier",
  Validating: "review",
  Reviewing: "review",
  "Revision Needed": "warn",
  "Needs Approval": "ember",
  Blocked: "warn",
  Failed: "bad",
  Accepted: "ok",
  Replaced: "neutral",
  Cancelled: "neutral",
};

export const CHECK_TONE: Record<CheckStatus | "running", Tone> = {
  passed: "ok",
  failed: "bad",
  not_run: "neutral",
  running: "glacier",
};

export const CHECK_LABEL: Record<CheckStatus | "running", string> = {
  passed: "Passed",
  failed: "Failed",
  not_run: "Not run",
  running: "Running",
};

export const STAGES: { id: Stage; label: string }[] = [
  { id: "understand", label: "Understand" },
  { id: "plan", label: "Plan" },
  { id: "build", label: "Build" },
  { id: "review", label: "Review" },
  { id: "deliver", label: "Deliver" },
];

export function stageIndex(stage: Stage): number {
  return STAGES.findIndex((s) => s.id === stage);
}

/**
 * A check with no trusted record is "not run" — unless the task is currently in
 * validation, in which case it is honestly "running". Nothing is ever assumed passed.
 */
export function checkDisplayStatus(status: CheckStatus, taskState: TaskState, executing: boolean): CheckStatus | "running" {
  if (status === "not_run" && executing && taskState === "Validating") return "running";
  return status;
}

export const TONE_CLASSES: Record<Tone, { text: string; bg: string; border: string; dot: string }> = {
  ember: { text: "text-fg", bg: "bg-ember/35", border: "border-ember-deep/50", dot: "bg-ember-deep" },
  glacier: { text: "text-glacier", bg: "bg-glacier/10", border: "border-glacier/30", dot: "bg-glacier" },
  ok: { text: "text-ok", bg: "bg-ok/10", border: "border-ok/30", dot: "bg-ok" },
  bad: { text: "text-bad", bg: "bg-bad/10", border: "border-bad/30", dot: "bg-bad" },
  warn: { text: "text-warn", bg: "bg-warn/10", border: "border-warn/30", dot: "bg-warn" },
  review: { text: "text-review", bg: "bg-review/10", border: "border-review/30", dot: "bg-review" },
  neutral: { text: "text-fg-soft", bg: "bg-surface-2", border: "border-line-strong", dot: "bg-muted" },
};
