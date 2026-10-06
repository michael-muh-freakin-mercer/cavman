"use client";

import { LoaderCircle, MessageCircleQuestion } from "lucide-react";
import { useId, useState } from "react";
import { Button } from "@/components/ui/button";
import { Panel } from "@/components/ui/panel";
import { useHydrated } from "@/lib/use-hydrated";

/** The planner's questions before it plans; the answer continues the build. */
export function QuestionsCard({ questions, onAnswer }: { questions: string[]; onAnswer: (answer: string) => Promise<void> }) {
  const hydrated = useHydrated();
  const [answer, setAnswer] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const id = useId();
  return (
    <Panel
      id="questions"
      title={<span className="flex items-center gap-2"><MessageCircleQuestion className="h-4 w-4 text-ember-deep" aria-hidden="true" /> Cavman needs your input</span>}
      description="A few questions before Cavman plans this build. Nothing has been spent on building yet."
    >
      <ol className="list-decimal space-y-1 pl-5 text-sm text-fg">
        {questions.map((question) => <li key={question}>{question}</li>)}
      </ol>
      <form
        method="post"
        className="mt-4 space-y-3"
        onSubmit={async (event) => {
          event.preventDefault();
          setBusy(true);
          setError(null);
          try {
            await onAnswer(answer.trim());
          } catch (reason) {
            setError(reason instanceof Error ? reason.message : "That did not work.");
            setBusy(false);
          }
        }}
      >
        <label htmlFor={id} className="block text-xs text-muted">Your answers</label>
        <textarea id={id} required value={answer} onChange={(event) => setAnswer(event.target.value)} rows={4} maxLength={4000}
          className="w-full rounded-md border-2 border-line-strong bg-surface px-3 py-2 text-sm focus:border-ink focus:outline-none" />
        {error ? <p role="alert" className="text-sm text-bad">{error}</p> : null}
        <Button type="submit" disabled={busy || !hydrated || !answer.trim()}>
          {busy ? <LoaderCircle className="h-4 w-4 animate-spin" aria-hidden="true" /> : null}
          Answer and continue
        </Button>
      </form>
    </Panel>
  );
}
