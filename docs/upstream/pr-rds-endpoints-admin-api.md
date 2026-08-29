# feat(rds): add `GET /_ministack/rds/endpoints` for host-mapped port discovery

Related to #795 (closed) and #797 (merged) — see "Relationship to #795/#797"
below. No issue opened first: this is a new admin endpoint on an existing
service, not a new service or an infrastructure change, so it doesn't hit
`CONTRIBUTING.md`'s issue-first gate.

## What changed

New `GET /_ministack/rds/endpoints` admin endpoint, returning
`{"instances": [...], "clusters": [...]}` for the active request account
and region. Each entry reports the instance/cluster identifier, its Docker
container name, the **host-mapped** port (what a process on the Docker host
uses to connect), and the **in-network** address (what a sibling container
on the ministack Docker network uses) — both explicitly, never conflated.
Either address is `null` when Docker hasn't started the container yet, or
wasn't available at all — never a guessed value. Empty account/region
returns `200` with empty collections, not an error, so a caller polling
during startup can tell "nothing yet" apart from "broken".

- `ministack/services/rds.py`: new `rds_endpoints_summary()`, reading
  directly from the existing `_instances`/`_clusters`
  `AccountRegionScopedDict` stores (no new Docker calls — the host/
  in-network fields are already maintained there by the existing create/
  respawn/attach code paths).
- `ministack/app.py`: new `_handle_rds_endpoints_request()`, modeled 1:1 on
  the existing `_handle_transfer_sftp_ports_request()` (`GET
  /_ministack/transfer/sftp-ports`) — same dispatch tier, same
  try/except-then-200-JSON shape, registered in `_handle_pre_body_request`.
- `tests/test_rds.py`: two deterministic unit tests directly against
  `rds_endpoints_summary()` (empty/populated, and the never-guess-a-missing-
  port case) plus one black-box HTTP test creating a real instance and
  asserting on the live response.

## Relationship to #795 / #797

#795 proposed a combined RDS+ElastiCache ports endpoint and was closed in
favor of #797, which fixed `Endpoint.Address`'s hardcoded `localhost` (now
`MINISTACK_HOST`) — genuinely solving the **fresh-create** case: right
after `CreateDBInstance`, `Endpoint.Port` does carry the real host-mapped
port today (verified live against this fork, built on the same fix).

It does not reach two cases this PR addresses:

1. **Warm restart / reattach.** `_start_rds_container_for_instance`
   (`rds.py`, triggered by `load_state()` on process boot when persisted RDS
   state exists) deliberately overwrites `Endpoint.Port` back to the
   engine's native container port (5432/3306) to match real AWS's wire
   shape — see the existing comment at that call site. After a restart,
   `DescribeDBInstances` no longer exposes the host-mapped port at all; only
   the private `_HostPort`/`_shared_host_port` fields still know it. This is
   the normal shape of a long-lived local dev/e2e stack, not an edge case.
2. **The in-network address.** `Endpoint` never carries it (by design — it's
   an AWS-parity field), but a caller running as a sibling container needs
   it, and conflating it with the host-mapped port is a real bug class (it's
   what once made spawned Lambda containers unreachable in this stack).

Scoped narrowly to RDS, matching the concrete gap encountered — not a
re-litigation of #795's broader (and already-rejected) combined-service
proposal. Whether to extend the same endpoint to ElastiCache is left to
maintainer judgment.

## Verification

```
$ curl -s http://localhost:4566/_ministack/rds/endpoints
{"instances": [], "clusters": []}          # before creating anything, still 200

$ curl -s http://localhost:4566/_ministack/rds/endpoints   # after CreateDBInstance
{"instances": [{"db_instance_identifier": "gap-verify-db", ...,
  "container_name": "ministack-rds-281afe4a44d7-instance-gap-verify-db",
  "host_mapped": {"host": "localhost", "port": 15432}, "in_network": null}], "clusters": []}

$ docker ps --filter "name=ministack-rds" --format '{{.Names}}\t{{.Ports}}'
ministack-rds-281afe4a44d7-instance-gap-verify-db   0.0.0.0:15432->5432/tcp, [::]:15432->5432/tcp
```

`container_name` and `host_mapped.port` match the real container exactly.

```
$ pytest tests/test_rds.py tests/test_rds_data.py -q
274 passed, 7 skipped in 113.78s (0:01:53)

$ pytest tests/test_rds.py -k rds_endpoints -q
3 passed, 214 deselected
```

No other endpoint's behavior changed.

## Scope boundaries

- ElastiCache is out of scope (see "Relationship to #795/#797" above).
- Aurora cluster members report the cluster's shared container (they never
  own one of their own) — `container_name`/addresses come from the parent
  cluster via the existing `_shared_cluster_id` link, not duplicated per
  member.
