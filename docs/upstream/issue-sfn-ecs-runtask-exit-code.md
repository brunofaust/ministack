# [bug] `ecs:runTask.sync` never fails on a non-zero essential-container exit

## Affected service

Step Functions ECS integration (`ministack/services/stepfunctions.py`).

## What AWS actually does

`ecs:runTask.sync` is a "Run a Job" service integration: Step Functions waits
for the ECS task to reach `STOPPED`, then fails the state with
`Error: "States.TaskFailed"` when an **essential** container in the task
exited non-zero, or when the task never started at all (per AWS's
`connect-ecs.html` docs, "Key features of Optimized Amazon ECS/Fargate
integration": `RunTask` returning HTTP 200 with a non-empty `failures` list
also fails the state, with `AmazonECS.Unknown`). This is what makes
`Catch(States.ALL)` reachable for an ECS-backed `Task` state at all. A
non-essential sidecar exiting non-zero does **not** fail the task — only
`essential` containers count (AWS default: `essential=true` when the field
is omitted from a container definition).

## What MiniStack does instead

`_poll_ecs_tasks` polls `DescribeTasks` until every task is `STOPPED` and
returns the raw result unconditionally — `exitCode`, `stopCode`, and
`stoppedReason` are never inspected. A container that exits `1` is
indistinguishable from one that exits `0`: `States.TaskFailed` never fires,
and `Catch` is unreachable no matter what actually happened inside the
container.

## Minimal reproduction (stock image, no fork)

```bash
docker run -d --name ministack -p 4566:4566 \
  -v /var/run/docker.sock:/var/run/docker.sock \
  ministackorg/ministack:v1.4.17

aws --endpoint-url=http://localhost:4566 ecs create-cluster --cluster-name c

aws --endpoint-url=http://localhost:4566 ecs register-task-definition \
  --family t --requires-compatibilities FARGATE --network-mode awsvpc \
  --cpu 256 --memory 512 \
  --container-definitions '[{"name":"main","image":"alpine:3.19","essential":true,"command":["sh","-c","exit 7"]}]'

cat > /tmp/sm.json <<'EOF'
{
  "StartAt": "RunTask",
  "States": {
    "RunTask": {
      "Type": "Task",
      "Resource": "arn:aws:states:::ecs:runTask.sync",
      "Parameters": {
        "Cluster": "c", "TaskDefinition": "t", "LaunchType": "FARGATE",
        "NetworkConfiguration": {"AwsvpcConfiguration": {"Subnets": ["subnet-1"]}}
      },
      "Catch": [{"ErrorEquals": ["States.ALL"], "Next": "Failed"}],
      "Next": "Succeeded"
    },
    "Succeeded": {"Type": "Succeed"},
    "Failed": {"Type": "Fail"}
  }
}
EOF

aws --endpoint-url=http://localhost:4566 stepfunctions create-state-machine \
  --name t --definition file:///tmp/sm.json --role-arn arn:aws:iam::000000000000:role/r

aws --endpoint-url=http://localhost:4566 stepfunctions start-execution \
  --state-machine-arn arn:aws:states:us-east-1:000000000000:stateMachine:t --name run1

# wait a few seconds, then:
aws --endpoint-url=http://localhost:4566 stepfunctions describe-execution \
  --execution-arn arn:aws:states:us-east-1:000000000000:execution:t:run1 --query status
```

### Observed

```
"SUCCEEDED"
```
(execution history shows `TaskSucceeded`, no `TaskFailed` event at all,
despite the container exiting `7`).

### Expected

```
"FAILED"
```
with a `TaskFailed` event (`Error: "States.TaskFailed"`) and the `Failed`
state (reached via `Catch`) entered.

## Why it matters

Any state machine that uses `ecs:runTask.sync` with a `Catch` — the standard
pattern for a Fargate batch/worker step whose failure needs orchestrated
handling (retry, alerting, a different downstream branch) — cannot be tested
locally: the failure path is silently unreachable regardless of what the
container actually does.

## Field shape reference

`ecs.py` already models everything this fix needs: `essential` on every
registered container definition (`_register_task_definition`'s
`cdef.setdefault("essential", True)`, AWS's own documented default), and
`stopCode`/`stoppedReason` on the task (`TaskFailedToStart` in `_run_task`,
`EssentialContainerExited` in `_maybe_mark_stopped`, `UserInitiated` in
`_stop_task`). The gap is entirely on the Step Functions side, which never
reads any of it.

## Open question for a maintainer

The exact Cause-document field set for a real `States.TaskFailed` event on
`ecs:runTask.sync` isn't published by AWS as a formal schema. The shape used
in the linked fix (the full stopped-task document, PascalCase-ified) matches
what's been observed in production Step Functions executions, but a
maintainer with a verbatim AWS-captured Cause to diff against should
reconcile the exact field list.

## Labels

`bug`, `Step Functions`, `ECS`
