# Gap: `ecs:runTask.sync` never fails the Step Functions state on a non-zero exit code

**Upstream-PR notes**: Bug fix to an existing service (`stepfunctions.py`'s ECS
integration already exists) — no issue required per `CONTRIBUTING.md`, straight
to a PR.

## What

`ministack/services/stepfunctions.py::_poll_ecs_tasks` polls ECS `DescribeTasks`
until every task reaches `STOPPED`, then returns the raw result — it never
looks at `exitCode`, `stopCode`, or `stoppedReason`. A container that exits
non-zero is therefore indistinguishable from one that exited `0`:
`States.TaskFailed` never fires, and a state machine's `Catch(States.ALL)`
branch on an `ecs:runTask.sync` state is unreachable locally, no matter how
the launched container actually behaved.

## Why

Real AWS Step Functions' `ecs:runTask.sync` integration fails the state with
`Error: "States.TaskFailed"` when an **essential** container in the task
exits non-zero, or when the task never started at all — this is what makes
`Catch` usable for ECS-backed steps in the first place. Per AWS's own
integration docs (`connect-ecs.html`, "Key features of Optimized Amazon
ECS/Fargate integration"): the error is always `States.TaskFailed` for a
task-level failure detected after `RunTask`, and `AmazonECS.Unknown` for a
`RunTask` call that itself returns HTTP 200 with a non-empty `failures` list
(a resource/placement failure caught immediately, before the task ever
starts). ECS's own `stopCode` vocabulary (`TaskFailedToStart`,
`EssentialContainerExited`, `UserInitiated`, …) is what a Cause payload is
built from — confirmed against this fork's own `ecs.py`, which already
models all three fields (`_run_task`'s `TaskFailedToStart` path,
`_maybe_mark_stopped`'s `EssentialContainerExited` path, `_stop_task`'s
`UserInitiated` path) and already tracks `essential` on every registered
container definition (`_register_task_definition`'s
`cdef.setdefault("essential", True)` — AWS's own documented default when the
field is omitted).

**Why it matters concretely.** In busydone's pipeline, a coding run that
trips a `LoopNoProgressError` circuit breaker exits non-zero → Step
Functions catches it → `post_results` runs its permanent-error path → the
ticket moves to Jira "Issues" and the Lambda deliberately raises to surface
an alarm. That chain depends entirely on this first link; without it, the
whole failure class is invisible to the local tier.

## Evidence of the gap

Verbatim, captured against a clean `v1.4.17` build in a throwaway worktree
before the fix: a task definition whose only (essential) container ran
`exit 7` was driven through an `ecs:runTask.sync` state with a
`Catch(States.ALL)`. The execution history showed no `TaskFailed` event at
all — `TaskSucceeded` fired regardless of the container's real exit code,
and the execution finished `SUCCEEDED`:

```
ExecutionStarted
TaskStateEntered
TaskScheduled
TaskSucceeded          <-- container exited 7; should have been TaskFailed
TaskStateExited
SucceedStateEntered
SucceedStateExited
ExecutionSucceeded      <-- should have been ExecutionFailed
```

## Files/functions changed

`ministack/services/stepfunctions.py`:

- `_invoke_ecs_run_task` — added a check for `result.get("failures")` on an
  HTTP-200 `RunTask` response, raising `AmazonECS.Unknown` (matches AWS's
  documented behavior for the Run-a-Job/Task-Token patterns). ministack's
  `ecs._run_task` never actually populates `failures` today, so this branch
  is presently unreachable — kept so a future `failures`-producing change
  (e.g. capacity/placement emulation) doesn't silently reopen this exact gap.
- `_poll_ecs_tasks` — once every task is `STOPPED`, calls the new
  `_raise_if_ecs_task_failed` before returning.
- `_raise_if_ecs_task_failed` (new) — raises `States.TaskFailed` when
  `stopCode == "TaskFailedToStart"` (no container ever ran), or when any
  **essential** container's `exitCode` is non-zero. A non-essential
  sidecar exiting non-zero is deliberately ignored, matching real ECS/SFN
  semantics.
- `_essential_containers_for_task` (new) — resolves `{container name:
  essential}` for a task by calling `ecs._describe_task_definition` on the
  task's own `taskDefinitionArn` (the same internal call pattern
  `_poll_ecs_tasks` already uses for `ecs._describe_tasks`).
- `_camel_to_pascal` (new) — inverse of the file's existing
  `_pascal_to_camel`; builds the failed task's Cause document with
  PascalCase field names, matching how real Step Functions normalises an
  optimized-integration document regardless of the underlying service's own
  wire casing (ECS's own JSON-protocol wire format is camelCase, unlike e.g.
  DynamoDB's PascalCase).

`ministack/services/ecs.py`:

- `_maybe_mark_stopped` — **pre-existing, adjacent bug found while verifying
  the essential-container distinction above**: exit codes were tracked as a
  single `exit_code = max(exit_code, ...)` across every Docker container in
  the task, then written back onto **every** container in `containers[]`
  identically. A task with an essential container that exited `0` and a
  non-essential sidecar that exited non-zero would report the essential
  container as having exited non-zero too — silently defeating the new
  essential-only check above for any multi-container task. Fixed to track
  one exit code per Docker container, applied by index (`_docker_ids[i]`
  and `containers[i]` are populated in the same order in `ecs.py::_run_task`
  when every container launches). When a container's own `docker.run()`
  call raised (so it was never appended to `_docker_ids`), the index
  correspondence between `_docker_ids` and `containers` cannot be trusted —
  documented in the code, falls back to the previous (imprecise, shared-max)
  behavior in that one case rather than mis-attributing one container's
  exit code to another's.

## Verification

Rebuilt `ministack-sfn-exit:dev` (a throwaway tag — never `ministack-local:dev`),
ran standalone on `localhost:24566` (never the compose stack's ministack on
`25580`), with `/var/run/docker.sock` mounted and `LAMBDA_EXECUTOR=docker`.

**Non-regression — essential container exits `0`:**

```
$ aws --endpoint-url=http://localhost:24566 stepfunctions describe-execution \
    --execution-arn arn:...:execution:sfn-exit-test:success-run-2 --query status
"SUCCEEDED"
```
History: `ExecutionStarted → TaskStateEntered → TaskScheduled →
TaskSucceeded → TaskStateExited → SucceedStateEntered → SucceedStateExited →
ExecutionSucceeded` — no `TaskFailed`, unchanged from before the fix.

**Failure — essential container exits `7`:**

```
$ aws --endpoint-url=http://localhost:24566 stepfunctions describe-execution \
    --execution-arn arn:...:execution:sfn-exit-test-fail:fail-run-2 --query status
"FAILED"
```
History (verbatim event types, then the `TaskFailed` event's `error` and the
final `ExecutionFailed` detail):
```
ExecutionStarted
TaskStateEntered
TaskScheduled
TaskFailed States.TaskFailed
TaskStateExited
FailStateEntered
ExecutionFailed {'error': 'TaskFailedCaught', 'cause': 'caught by Catch'}
```
`TaskFailedCaught`/`caught by Catch` is the test state machine's own `Fail`
state, reached only via the `Catch(States.ALL)` branch — proving the branch
was entered, not just that the state failed. The `TaskFailed` event's
`cause` (JSON, truncated here) carried the full stopped-task document:
```json
{"TaskArn": "...", "ClusterArn": "...", "StopCode": "EssentialContainerExited",
 "StoppedReason": "Essential container exited",
 "Containers": [{"Name": "main", "ExitCode": 7, "LastStatus": "STOPPED", ...}], ...}
```

**Essential-vs-non-essential distinction — a non-essential sidecar exits `9`,
the essential container exits `0`:**

```
$ aws --endpoint-url=http://localhost:24566 stepfunctions describe-execution \
    --execution-arn arn:...:execution:sfn-exit-test-sidecar:sidecar-run-2 --query status
"SUCCEEDED"
```
`ecs describe-tasks` on the stopped task confirmed per-container exit codes
were tracked independently (the `ecs.py` fix above), not collapsed to a
shared value:
```json
[{"name": "main", "exitCode": 0}, {"name": "sidecar", "exitCode": 9}]
```

## Fidelity notes / what is NOT modeled

- **The Cause document's exact field set is a well-informed reconstruction,
  not a verified-against-a-real-account transcript.** AWS does not publish a
  formal schema for a service integration's failure Cause; the shape used
  here (the full `DescribeTasks`-equivalent task document, PascalCase-ified)
  matches the pattern this repo's own maintainer has observed in production
  Step Functions executions for `ecs:runTask.sync`, and is internally
  consistent with how this file already handles the Lambda integration's
  Cause (the raw error payload, JSON-encoded). If a maintainer has a
  verbatim AWS-captured Cause to diff against, the exact field list here
  should be reconciled to it.
- **`AmazonECS.Unknown` (non-empty `failures` on RunTask) is wired but
  currently unreachable** — `ecs._run_task` never populates `failures`
  today. Documented in the code as deliberate, forward-looking wiring, not
  a claim that this path is exercised.
- **A container whose Docker launch itself fails mid-task** (i.e.
  `docker_client.containers.run()` raises for one container definition but
  not others) breaks the index correspondence between `_docker_ids` and
  `containers[]` that the per-container exit-code fix relies on; this falls
  back to the previous (imprecise) shared-exit-code behavior rather than
  guessing a wrong per-container assignment. This is a pre-existing,
  narrower edge case in `ecs.py`'s task-launch bookkeeping, not something
  this change introduces or claims to fully solve.
- **`.waitForTaskToken` is unaffected** (by design) — it doesn't poll
  `DescribeTasks` at all; task-token completion is driven by the container
  itself calling `SendTaskSuccess`/`SendTaskFailure`, unrelated to this gap.
