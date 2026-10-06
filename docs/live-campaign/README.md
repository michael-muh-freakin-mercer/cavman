# Live-model campaigns

Real OpenRouter models driving real Cavman builds (`scripts/live_campaign.py`).
A small, budget-capped version runs on demand in CI: Actions → **Live smoke
(real models, spends credits)** → Run workflow.
Each report lists every request with its final state, tasks, failure classes,
model calls, provider-reported cost and time.

## 2026-10-02: what the stricter review costs, and which models fit which mode

Live smokes from CI on the two standard requests that cost most and least before (ISO 8601 durations,
and a JSON todo CLI), with send-back reasons recorded (#47).

| Model | Request | Result | Calls | Cost | Time |
|---|---|---|---|---|---|
| deepseek-v4-pro (default) | ISO 8601 | budget reached | n/a | $0.65 | 62 min |
| deepseek-v4-pro | ISO 8601 | complete | 22 | $0.03 | 7 min |
| deepseek-v4-pro | todo CLI | complete | n/a | $0.05 | 9 min |
| deepseek-v4-flash | ISO 8601 | budget reached (150-call cap) | 150 | $0.26 | 49 min |
| deepseek-v4-flash | todo CLI | complete after a failed test round | 108 | $0.12 | 29 min |
| claude-sonnet-5.5 | ISO 8601 | blocked after 3 candidates | 86 | $1.63 | 8 min |
| claude-sonnet-5.5 | todo CLI | complete | 20 | $0.45 | 2 min |

**Why some builds cost 20 times more than others.** The same ISO 8601 request cost $0.03 in one run
and $0.65 in another. In the Sonnet run, both reviewers ruled every plan item met, and the work was
still sent back. Trusted code fails a review whose reviewer opened no file (it judged from the diff in
its input), but it said so only in the evidence, not in the reason the specialist is given. The
specialist was told "review requested changes" with a reason saying everything was fine, and spent 79
calls redoing working code. Fixed: a reviewer that reads nothing is asked once more to open the
files, and if it still does not, the reason says the verdict was discarded because the reviewer read
nothing.

**Modes.** deepseek-v4-flash is too weak: it hit the call cap and needed extra rounds, so it saves
nothing. claude-sonnet-5.5 is fast and was clean where review behaved, at about 10 times the
per-build cost of deepseek-v4-pro. Suggested configuration: Budget and Balanced on deepseek-v4-pro,
Maximum Quality on claude-sonnet-5.5. Repeat the Sonnet ISO build after the reviewer fix before
quoting quality numbers.

Live spend on 2026-10-02: $3.18 provider-reported in the runs above, plus the unfinished last builds of two smokes cut off by the job timeout (no report survived; each was capped at $1), and one smoke started by another session.

## 2026-09-30: live smoke with plan-item review

**Result: 3 of 3 builds completed** ([report](20260930T124540Z.md), [data](20260930T124540Z.json)),
run from CI ([run 36716799520](https://github.com/michael-muh-freakin-mercer/caveman/actions/runs/36716799520))
with reviewers ruling on every plan item.

- Models: `deepseek/deepseek-v4-pro`. Limits: $1 per build, $3 total.
- Cost: $1.00 total; $0.14, $0.19 and $0.67 per build. Every call reported its cost.
- Time: 7.4, 8.0 and 32.8 minutes. Model calls: 26, 35 and 76.
- Two builds hit a `BAD_OUTPUT` failure and completed after a revision.

Review is dearer than on 2026-09-28, when the median build cost $0.09 and took
22 calls. The ISO 8601 build used 18 reviewer calls and 57 specialist calls
and came within a third of its $1 ceiling. Three builds are too few to say
how much of that is the stricter review and how much is ordinary variation;
the report does not record why each revision was requested.

An earlier attempt the same day
([run 36715069184](https://github.com/michael-muh-freakin-mercer/caveman/actions/runs/36715069184), $0.17)
completed one build and then stopped with "Event loop is closed", a fault in
the campaign script rather than in a build.

## 2026-09-28: first campaign

**Result: 10 of 10 builds completed** ([report](20260928T180959Z.md), [data](20260928T180959Z.json)).

- Models: `deepseek/deepseek-v4-pro` for planner and specialists (OpenRouter).
- Limits: $2 per build, $20 total, 150 model calls per build.
- Cost: $0.87 total; median $0.09 per build (range $0.04–$0.15).
- Time: median 5.5 minutes per build (range 1.8–11.3); median 22 model calls.
- Mix: 6 Python, 3 TypeScript (`node_test`), 1 specification plus code.
- Recoveries: two builds hit a `BAD_OUTPUT` failure once and completed after a
  kernel-routed revision.
- **Cost reporting confirmed.** OpenRouter's own usage counter rose by the same
  amount Cavman recorded, to within half a cent; every call reported its cost.
  USD budgets therefore enforce real spend.

Every accepted change passed trusted sandbox checks (compile, pytest or node:test)
and an independent model review before the kernel accepted it. The generated
code has not been reviewed by a person.

### What the smoke builds found before the campaign

Four single-prompt smoke builds ($0.95 in total) exposed problems that scripted
tests could not:

1. **Specialists could not run their own checks.** Every `run_check` call was
   refused by the sandbox's strict command templates, which the model was never
   told. It rewrote files blind until it ran out of steps. Specialists now name
   a check (`pytest`, `compile`, `node_test`, `tsc`) and the tool builds the
   template; output is trimmed to its tail.
2. **Most plans were rejected.** Planners wrote free text into `required_inputs`
   and put `pytest_regression` where nothing could run. The driver now
   normalizes both; planning retries three times and reports why it failed.
3. **Success on the last step was thrown away.** A specialist reached green
   checks on its final (24th) step. Specialists get 40 steps. A developer
   workspace with changes is submitted for validation and review when steps
   run out, labelled as a platform submission.

An operator-only trace (`WALTER_TOOL_TRACE=<file>`) of tool names and sizes made
the first problem visible.

### Not yet measured

- Larger multi-task builds (web apps with several components).
- Other models and the Budget / Balanced / Maximum Quality modes.
- Builds that need approvals or capability requests.
