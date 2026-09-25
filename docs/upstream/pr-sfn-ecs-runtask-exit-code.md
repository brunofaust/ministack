# fix(stepfunctions,ecs): propagate ECS task exit code to States.TaskFailed

Closes #`<sfn-ecs-runtask-exit-code issue>`.

## What changed

`ministack/services/stepfunctions.py` (+~95 LOC): `ecs:runTask.sync` now
fails the state with `Error: "States.TaskFailed"` when an essential
container exits non-zero, or when the task never started — making
`Catch(States.ALL)` reachable for ECS-backed `Task` states, matching real
AWS. `ministack/services/ecs.py` (+~20 LOC / -8): a pre-existing, adjacent
bug found while verifying the essential-only check — `_maybe_mark_stopped`
collapsed every container's exit code to a single shared `max()` value —
is fixed alongside it, since without a per-container exit code the new
essential-only distinction above is not reliably correct for multi-container
tasks.

## Approach

**`stepfunctions.py`:**

- `_poll_ecs_tasks` calls the new `_raise_if_ecs_task_failed` once every
  task in the poll reaches `STOPPED`, before returning the (still-available,
  for a Choice state) raw `DescribeTasks` result.
- `_raise_if_ecs_task_failed` raises `States.TaskFailed` when
  `stopCode == "TaskFailedToStart"` (no container ever ran), or when any
  container that is **essential** per its task definition has a non-zero
  `exitCode`. A non-essential sidecar failing is deliberately ignored.
- `_essential_containers_for_task` resolves `{name: essential}` for a task
  via `ecs._describe_task_definition(task["taskDefinitionArn"])` — the same
  internal-call pattern `_poll_ecs_tasks` already uses for
  `ecs._describe_tasks`.
- `_camel_to_pascal` (new — inverse of the file's existing
  `_pascal_to_camel`) builds the Cause document with PascalCase field names,
  matching how real Step Functions normalises an optimized-integration
  document regardless of the service's own (camelCase, for ECS) wire
  format.
- `_invoke_ecs_run_task` additionally raises `AmazonECS.Unknown` on a
  non-empty `failures` list in an HTTP-200 `RunTask` response, per AWS's
  documented behavior for the Run-a-Job/Task-Token patterns. This is
  presently unreachable — `ecs._run_task` never populates `failures` — kept
  so a future `failures`-producing change doesn't silently reopen this gap.

**`ecs.py`:**

- `_maybe_mark_stopped` now tracks one exit code per Docker container
  (`_docker_ids[i]` ↔ `containers[i]`, populated in the same order by
  `_run_task` when every container launches) instead of a single value
  shared across the whole task. Falls back to the previous shared-max
  behavior only when a container's own Docker launch failed (breaking the
  index correspondence) — documented in place, not silently guessed.

## Scope boundaries

- The exact Cause-document field set is a well-informed reconstruction
  (AWS publishes no formal schema for it), not verified against a live
  AWS-captured transcript — flagged as an open question in the linked
  issue.
- The `ecs.py` per-container exit-code fix does not attempt to recover the
  true container↔exit-code mapping when a container's own Docker launch
  raised mid-task; it falls back to the pre-existing (imprecise) behavior
  in that one case, which is strictly no worse than before.
- `AmazonECS.Unknown` wiring is defensive/unreachable today, not exercised
  by any test in this PR.

## Testing status

**Not yet included.** A real PR needs: unit coverage for
`_raise_if_ecs_task_failed` (essential fail / non-essential ignored /
`TaskFailedToStart` / success), `_essential_containers_for_task` (missing
task definition, container not found in it → defaults to essential), and
`ecs.py::_maybe_mark_stopped`'s per-container exit-code tracking
(multi-container task, one essential + one non-essential, mismatched exit
codes) plus its fallback path.

## Verification performed (manual, this session)

Verbatim before/after execution histories in the linked issue and this
fork's `docs/busydone-gaps/sfn-ecs-runtask-exit-code.md`. Three scenarios
driven end-to-end against a throwaway build (`localhost:24566`, isolated
from any other running MiniStack instance):

1. Essential container exits `0` → execution `SUCCEEDED`, unchanged from
   before the fix (non-regression).
2. Essential container exits `7` → `TaskFailed` (`States.TaskFailed`),
   `Catch(States.ALL)` entered (proven by the test state machine's own
   `Fail` state — reachable only via that branch — actually firing),
   execution `FAILED`.
3. Non-essential sidecar exits `9`, essential container exits `0` →
   execution `SUCCEEDED`; `DescribeTasks` confirmed the two containers kept
   independent exit codes (`main: 0`, `sidecar: 9`), proving the `ecs.py`
   fix.

## Checklist (per `CONTRIBUTING.md`)

- [x] Bug fix to an existing service — no issue-first gate required, but
      filed anyway (`#<issue>`) since it documents the reproduction cleanly
      and carries the open Cause-schema question for a maintainer.
- [ ] Tests added and passing
- [ ] Linting passes — not run in this fork, must run before real
      submission
- [ ] Entry added to `CHANGELOG.md`
