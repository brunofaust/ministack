# Gap: no way to discover an RDS instance/cluster's Docker host-mapped port

**Upstream-PR notes**: New admin endpoint on an existing service (`rds.py`
already exists) — no issue required per `CONTRIBUTING.md` (the issue-first
gate is scoped to new services and infra changes, neither of which this is).
Upstream already has a closely related, **closed** issue/PR — see
"Relationship to upstream #795 / #797" below; the PR draft should reference
it and explain why RDS still needs its own endpoint.

## What

`ministack/services/rds.py` assigns each emulated RDS instance/cluster a
random Docker host-mapped port (`_next_port()` off `RDS_BASE_PORT`, stored
in the private `_HostPort`/`_shared_host_port` fields — never a
public/documented field). A caller outside the emulator that needs that
port — a test harness on the Docker host, a human debugging a stuck
container — has no supported way to ask for it. Today's only option is
resolving the emulated container **by name** (reconstructing this module's
private naming scheme: `_rds_docker_name`/`_rds_cluster_docker_name`, both
salted with a `sha1(account:region)[:12]` scope hash) and inspecting it via
the Docker API directly.

This is exactly what busydone's local e2e harness does today
(`tests/e2e/provision.py::discover_rds_host_port` /
`resolve_rds_container_name`, in the `busydone` repo, not this one) — and it
is fragile precisely because it's re-deriving a private implementation
detail instead of asking MiniStack for it.

The endpoint follows the active request account and region, like the RDS
service APIs themselves. It never returns identifiers or reachability data
from another tenant scope.

## Why — and why `DescribeDBInstances.Endpoint.Port` isn't a substitute

The obvious question is "doesn't `DescribeDBInstances` already return a
reachable `Endpoint`?" — and upstream closed a directly related proposal
(**#795**, "Add /_ministack/ports endpoint for Docker-backed service port
discovery", covering RDS *and* ElastiCache) on exactly that reasoning: PR
**#797** ("Replace all hard coded localhost with environment variable",
merged 2026-06-03) made every service's `Endpoint.Address` respect
`MINISTACK_HOST` instead of a hardcoded `"localhost"`, and a maintainer
(`Nahuel990`) argued the dedicated endpoint was then redundant — "Since we
already support describe-db-instances with the hostname... remote clients
get a reachable address without exposing the internal address of the host
in an endpoint."

Verified against this fork's `rds.py` (built on the same `MINISTACK_HOST`
mechanism #797 shipped) that this holds **only on a fresh
`CreateDBInstance`**, and stops holding the moment MiniStack restarts with
persisted state — which is the normal shape of a long-lived local e2e stack
(`load_state()` → `to_respawn` → `_start_rds_container_for_instance`,
`ministack/services/rds.py` around line 1213):

1. **Fresh create** — `Endpoint.Port` in the `CreateDBInstance`/
   `DescribeDBInstances` response *does* carry the real host-mapped port
   today (confirmed live below: created an instance, port 15432 came back
   in both `Endpoint.Port` and the new admin endpoint).
2. **Warm restart / reattach** — `_start_rds_container_for_instance`
   (triggered by `load_state()` on process boot when persisted RDS state
   exists) deliberately **overwrites** `Endpoint.Port` back to the engine's
   native container port (5432/3306) to match real AWS's wire shape — see
   the comment at `rds.py` ~line 1234: *"Host port must come from
   `_HostPort`... NOT from `Endpoint.Port` — the latter is overwritten to
   `container_port`... to match real AWS."* After this point,
   `DescribeDBInstances` no longer exposes the host-mapped port **at all**;
   only the private `_HostPort`/`_shared_host_port` fields still know it.

So #795/#797 closed the gap for the one-shot "just created it" case, but not
for the persisted-and-restarted case — which is the case a long-running
local dev/e2e stack actually hits every time it restarts. RDS also needs the
**in-network** address (the container's IP + native port on the ministack
Docker network) alongside the host-mapped port, which `Endpoint` never
carries at all (by design — it's an AWS-parity field, not a debugging one).
Conflating host-mapped and in-network addresses is exactly the bug class
that once made spawned Lambda containers unreachable in this stack.

## Design

Followed the existing `GET /_ministack/transfer/sftp-ports` precedent
(`ministack/app.py` ~line 998) — the closest prior art for "an admin
endpoint that surfaces a Docker port mapping boto3's response shape can't
carry" — same dispatch tier (Tier 1, pre-body), same
try/except-then-200-JSON shape, same registration point in
`_handle_pre_body_request`.

### `GET /_ministack/rds/endpoints`

Returns `{"instances": [...], "clusters": [...]}`, spanning **every**
account/region present on the server (same convention as the
`/_ministack/ses/messages` and `/_ministack/sqs/messages` admin endpoints —
a caller polling during startup has no other account/region context to
scope by). Each entry:

```json
{
  "db_instance_identifier": "mydb",
  "db_cluster_identifier": null,
  "account_id": "000000000000",
  "region": "us-east-1",
  "container_name": "ministack-rds-281afe4a44d7-instance-mydb",
  "status": "available",
  "host_mapped": {"host": "localhost", "port": 15432},
  "in_network": {"host": "172.19.0.5", "port": 5432}
}
```

`host_mapped`/`in_network` are `null` (never a fabricated port) when Docker
hasn't started the container yet or wasn't available at all — never guessed.
An Aurora cluster member reports the **cluster's** shared container (it
doesn't own one of its own): `container_name` and both address fields come
from the parent cluster via `_shared_cluster_id`.

Empty account/region → `{"instances": [], "clusters": []}` with a plain
`200` — never a 404/500 — so a caller polling during startup can tell
"nothing yet" apart from "broken".

## Files/functions changed

- `ministack/services/rds.py` (+78 LOC): new `rds_endpoints_summary()`,
  placed next to `_sync_cluster_endpoints()`. Reads directly from the
  existing `_instances`/`_clusters` `AccountRegionScopedDict` stores (no
  Docker calls from the HTTP layer — the host/in-network fields are already
  maintained on those dicts by the create/respawn/attach code paths this
  endpoint only reads).
- `ministack/app.py` (+26 LOC): new `_handle_rds_endpoints_request()`
  mirroring `_handle_transfer_sftp_ports_request()`, registered in
  `_handle_pre_body_request` right after the sftp-ports call.
- `tests/test_rds.py` (+3 tests): two deterministic unit tests against
  `rds_endpoints_summary()` directly (empty/populated, and the
  never-guess-a-missing-port case), one black-box HTTP test creating a real
  instance and asserting on the live JSON.

## Verification

Full method: `uv sync --extra dev`, ran `python -m ministack` as a bare host
process on `localhost:4566` (no Docker-in-Docker), confirmed port 4566 was
free first (`lsof -i :4566 -sTCP:LISTEN`), ran the suite, killed the process
after.

**Empty case** (before creating anything):

```
$ curl -s http://localhost:4566/_ministack/rds/endpoints
{"instances": [], "clusters": []}
$ curl -s -o /dev/null -w "%{http_code}\n" http://localhost:4566/_ministack/rds/endpoints
200
```

**Populated case** (after `create_db_instance(DBInstanceIdentifier="gap-verify-db", Engine="postgres", ...)`):

```
{"instances": [{"db_instance_identifier": "gap-verify-db", "db_cluster_identifier": null,
"account_id": "000000000000", "region": "us-east-1",
"container_name": "ministack-rds-281afe4a44d7-instance-gap-verify-db", "status": "available",
"host_mapped": {"host": "localhost", "port": 15432}, "in_network": null}], "clusters": []}
```

Cross-checked against Docker directly:

```
$ docker ps --filter "name=ministack-rds" --format '{{.Names}}\t{{.Ports}}'
ministack-rds-281afe4a44d7-instance-gap-verify-db	0.0.0.0:15432->5432/tcp, [::]:15432->5432/tcp
```

`container_name` and `host_mapped.port` match the real container exactly.
`in_network` was `null` here because this bare host-process run has no
`ministack` Docker network for the spawned container to join (expected —
see `rds.py`'s own `if ms_network:` guard; a Docker-Compose deployment with
the network configured would populate it).

**Endpoint.Port divergence claim** (supporting the "Why" section above),
same live server, a second instance:

```python
resp = c.describe_db_instances(DBInstanceIdentifier="gap-verify-port-claim")
# -> Endpoint: {'Address': 'localhost', 'Port': 15432, 'HostedZoneId': 'Z2R2ITUGPM61AM'}
```

Confirms the fresh-create case matches upstream's #797 fix. The
warm-restart divergence (`Endpoint.Port` reverting to the container's
native port) is evidenced by the code path itself (`_start_rds_container_for_instance`
+ the `rds.py` ~line 1234 comment cited above) rather than re-triggered
live, since reproducing it needs a full MiniStack process restart with
`PERSIST_STATE=1` against on-disk state — out of scope for a single-session
verification pass, but the code path is unambiguous and self-documenting.

**Test suite** — touched-area tests, sequential (not `-n5`, to avoid
unrelated cross-test flakiness under a shared live server — see below):

```
$ pytest tests/test_rds.py tests/test_rds_data.py -q
274 passed, 7 skipped in 113.78s (0:01:53)
```

New tests in isolation:

```
$ pytest tests/test_rds.py -k rds_endpoints -q
3 passed, 214 deselected
```

Full suite (`pytest tests/ -q -n 5`): `5685 passed, 22 failed, 54 skipped`.
All 22 failures are in files this change never touches (cognito, glue,
cloudfront, ecs, firehose, scheduler, transfer, cloudwatch, kinesis, eks,
dsql) and reproduce standalone, outside `-n5`, on a clean checkout — e.g.
`tests/test_kinesis.py::test_kinesis_cbor_put_record` fails with
`ModuleNotFoundError: No module named 'cbor2'` (a missing optional test
dependency in this worktree's fresh venv, unrelated to RDS). Two of my own
new tests initially failed under `-n5` for a real reason worth recording:
they first asserted the **whole** `_instances`/`_clusters` store was empty,
which is inherently flaky against a live server shared with concurrent
xdist workers — fixed by scoping the assertion to the test's own
account/region (see `_rds_endpoints_summary_scoped` in the test file),
matching how `test_sqs.py`'s admin-endpoint tests already filter by their
own generated queue URL rather than asserting global emptiness.

No other endpoint's behavior changed — `/_ministack/health`,
`/_ministack/transfer/sftp-ports` reconfirmed working after the change; a
`POST` to the new path itself falls through to the existing generic 405,
same as any other GET-only admin route.

## Relationship to upstream #795 / #797

- **#795** (closed, not merged): proposed a combined RDS+ElastiCache ports
  endpoint. Closed in favor of #797.
- **#797** (merged 2026-06-03): fixed the underlying `Endpoint.Address`
  hardcoded-`localhost` bug this fork also carries the fix for
  (`_MINISTACK_HOST`). Genuinely fixes the fresh-create case.
- **This gap**: #797 doesn't reach the warm-restart case (`Endpoint.Port`
  is deliberately reset to the container's native port there, by design, to
  match real AWS), and neither PR addressed the in-network address at all.
  The PR draft for this fix should link both, credit #797 for the part it
  already solved, and scope the ask narrowly: host-mapped port after a
  restart, plus the in-network address — not a re-litigation of #795's
  broader (and rejected) proposal.

## Open question for a maintainer

Whether `GET /_ministack/rds/endpoints` should also cover ElastiCache
(closer to #795's original combined scope) is left to the maintainers' call
— this fix is scoped to RDS only, matching the concrete gap busydone hit.
