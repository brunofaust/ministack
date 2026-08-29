# S3 Tables: maintenance-configuration surface

## What

`ministack/services/s3tables.py` never implemented the S3 Tables maintenance-
configuration API. The GET side was a bare stub (`json_response({})` for any
`/buckets/{arn}/maintenance`-style sub-resource path), and the PUT side
(`/buckets/{arn}/maintenance/{type}`, `/tables/{arn}/{ns}/{name}/maintenance/{type}`)
wasn't routed at all — it fell through to `UnknownOperationException`. Newly
created buckets/tables never had a maintenance configuration, so
`GetTableBucketMaintenanceConfiguration`/`GetTableMaintenanceConfiguration`
always returned an empty `settings` object.

This closes the gap for the four operations:

- `GetTableBucketMaintenanceConfiguration` / `PutTableBucketMaintenanceConfiguration`
- `GetTableMaintenanceConfiguration` / `PutTableMaintenanceConfiguration`

and seeds AWS's real default configuration at creation time for both table
buckets and tables, so a freshly created resource returns a **populated**
configuration with no prior `Put` call — matching real AWS, which
auto-provisions these defaults on every bucket/table.

`GetTableMaintenanceJobStatus` is out of scope (not part of the reported gap;
no caller needs it yet).

## Why

The `hashicorp/terraform-provider-aws` `aws_s3tables_table_bucket` resource
calls `GetTableBucketMaintenanceConfiguration` as part of `Read` right after
`Create`, and its generated schema requires the `settings` object to be
populated (a non-optional nested attribute). Against MiniStack's stub, the
provider failed to decode the response and the retry then hit a `409` because
the bucket already existed — the apply could never converge:

```
Value Conversion Error: expected tftypes.Object["settings":Object["non_current_days":Number,"unreferenced_days":Number]]
got tftypes.Object["settings":Object[]]
```

followed on retry by:

```
409 ConflictException: already exists
```

This is why `infra/e2e`'s root Terraform (in the consuming `busydone` repo)
currently omits the `s3_tables` module.

## Wire shape (verified against botocore, not memory)

Source: `botocore/data/s3tables/2018-05-10/service-2.json.gz`, read read-only
from the `busydone` repo's venv
(`busydone/.venv/lib/python3.14/site-packages/botocore/data/s3tables/`).

Operations + REST bindings (`operations.<Name>.http`):

| Operation | Method | Path |
|---|---|---|
| `GetTableBucketMaintenanceConfiguration` | GET | `/buckets/{tableBucketARN}/maintenance` |
| `PutTableBucketMaintenanceConfiguration` | PUT (204) | `/buckets/{tableBucketARN}/maintenance/{type}` |
| `GetTableMaintenanceConfiguration` | GET | `/tables/{tableBucketARN}/{namespace}/{name}/maintenance` |
| `PutTableMaintenanceConfiguration` | PUT (204) | `/tables/{tableBucketARN}/{namespace}/{name}/maintenance/{type}` |

Enums (`shapes.<Name>.enum`):

- `TableBucketMaintenanceType`: `icebergUnreferencedFileRemoval`
- `TableMaintenanceType`: `icebergCompaction`, `icebergSnapshotManagement`
- `MaintenanceStatus`: `enabled`, `disabled`

**The `settings` member is itself a one-of structure keyed by the maintenance
type again** — not the settings fields directly:

```
TableBucketMaintenanceConfigurationValue
  status: MaintenanceStatus
  settings: TableBucketMaintenanceSettings
    icebergUnreferencedFileRemoval: IcebergUnreferencedFileRemovalSettings
      unreferencedDays: PositiveInteger
      nonCurrentDays: PositiveInteger

TableMaintenanceConfigurationValue
  status: MaintenanceStatus
  settings: TableMaintenanceSettings
    icebergCompaction: IcebergCompactionSettings
      targetFileSizeMB: PositiveInteger
      strategy: IcebergCompactionStrategy (enum: auto | binpack | sort | z-order)
    icebergSnapshotManagement: IcebergSnapshotManagementSettings
      minSnapshotsToKeep: PositiveInteger
      maxSnapshotAgeHours: PositiveInteger
```

This was caught empirically, not by re-reading the model carefully enough the
first time: an initial flat implementation (`settings: {unreferencedDays: 3,
nonCurrentDays: 10}`) produced

```
aws: [ERROR]: Invalid service response: TableBucketMaintenanceSettings must have one and only one member set.
```

from botocore's own response parser — confirming the AWS CLI docs' example
payloads (`--value='{"status":"enabled","settings":{"icebergCompaction":{"targetFileSizeMB":256}}}'`)
are the literal wire shape, not shorthand.

**The `PUT` request body wraps the payload in a `"value"` key.** The `value`
member of `Put*Request` carries no `payload` trait, so botocore's JSON
protocol serializes it like any other body member — body is
`{"value": {"status": ..., "settings": {...}}}`, not the value object
flattened at the top level. Also caught empirically: an unwrapped read of
the request body produced a `PUT` that silently stored an empty `settings`
dict (200/204 response, no error — the bug was invisible until the next
`GET`).

## Defaults seeded at creation (the key unblock)

Confirmed against AWS's own docs, not guessed:
[Maintenance for tables](https://docs.aws.amazon.com/AmazonS3/latest/userguide/s3-tables-maintenance.html)
and [Maintenance for table buckets](https://docs.aws.amazon.com/AmazonS3/latest/userguide/s3-table-buckets-maintenance.html).

| Type | Level | Default `status` | Default `settings` |
|---|---|---|---|
| `icebergUnreferencedFileRemoval` | bucket | `enabled` | `unreferencedDays: 3`, `nonCurrentDays: 10` |
| `icebergCompaction` | table | `enabled` | `targetFileSizeMB: 512`, `strategy: "auto"` |
| `icebergSnapshotManagement` | table | `enabled` | `minSnapshotsToKeep: 1`, `maxSnapshotAgeHours: 120` |

Seeded at:

- `_create_table_bucket()` — writes `_bucket_maintenance[arn]` right after the
  bucket row is created.
- `_create_table()` (control-plane `CreateTable`) — writes
  `_table_maintenance[key]` right after the table row is created.
- `_iceberg_create_table()` (Iceberg REST catalog `POST .../tables`) — same
  seed via `_set_bucket_region_value`, since a table can be created through
  either route and both are read back through the same control-plane
  `Get/PutTableMaintenanceConfiguration` operations.

`Get*MaintenanceConfiguration` also falls back to the same default builder if
the store somehow has no entry (defensive read, not the primary seed path —
the primary path is the write-at-creation above). `Delete*` cleans up the
corresponding maintenance entry so state doesn't leak past a bucket/table's
lifetime.

## State container

New module-level stores, same `AccountRegionScopedDict` type as the
existing `_table_buckets`/`_namespaces`/`_tables` (keyed by bucket ARN or the
existing `_table_key(bucket_arn, namespace, table)` composite, respectively)
— **not** a plain dict, so multi-tenant isolation holds automatically via the
request-scoped SigV4 account/region contextvars:

- `_bucket_maintenance`: `bucket_arn -> {type: {status, settings}}`
- `_table_maintenance`: `"bucket_arn\x00namespace\x00table" -> {type: {status, settings}}`

Both are included in `get_state()`/`restore_state()`/`reset()` alongside the
existing stores, so persistence and the test-isolation `reset()` gate cover
them the same way.

This is deliberately **not** the `_ensure_default_*()` module-load +
`handle_request()` + `reset()` triple used by `athena.py`'s
`_ensure_default_workgroup()` or the Glue `_ensure_default_database()` fix on
this branch — those seed a **global singleton** that must exist even before
any create call (AWS auto-provisions a `default` Glue database and Athena
workgroup with no prior create). S3 Tables maintenance configuration has no
such singleton: it only ever needs to exist once the bucket/table it belongs
to exists, so seeding it at the point of that resource's own creation is the
correct (and simpler) analog — there is nothing to lazily ensure before any
bucket/table has been created.

## Files / functions changed

`ministack/services/s3tables.py` (+~150 LOC / -8 removed):

- New stores: `_bucket_maintenance`, `_table_maintenance` (both
  `AccountRegionScopedDict`).
- `get_state()` / `restore_state()` / `reset()`: extended to cover the two
  new stores.
- New: `_default_bucket_maintenance_configuration()`,
  `_default_table_maintenance_configuration()`,
  `_get_table_bucket_maintenance_configuration()`,
  `_put_table_bucket_maintenance_configuration()`,
  `_get_table_maintenance_configuration()`,
  `_put_table_maintenance_configuration()`.
- `_create_table_bucket()`, `_create_table()`, `_iceberg_create_table()`:
  seed the default configuration.
- `_delete_table_bucket()`, `_delete_table()`: clean up the corresponding
  maintenance entry.
- `handle_request()`: bucket-route rewritten to split ARN from suffix with
  the existing `_split_arn_and_suffix()` helper (previously it naively
  joined all path segments into `arn`, which happened to work only because
  every prior sub-resource case short-circuited to a stub before `arn` was
  used) and dispatch `maintenance` / `maintenance/{type}`; table-route
  extended with `maintenance` (3-part suffix) / `maintenance/{type}` (4-part
  suffix) cases alongside the existing `metadata-location` handling.

## Verification (verbatim)

Built with the **slim** `Dockerfile` (`docker build --build-arg
MINISTACK_VERSION=v1.4.17 -t ministack-s3tables-slim:dev .`) — the
maintenance-configuration surface is pure control-plane JSON state, no
Iceberg REST/DuckDB catalog access involved, so the slim image was
sufficient; no `Dockerfile.full` rebuild was needed. Ran standalone,
container `ms-s3tables`, port `14572:4566`, `SERVICES=s3,s3tables`.

Bucket-level, create then get (default, populated):

```
$ aws --endpoint-url=http://localhost:14572 s3tables create-table-bucket --name busydone-local-verify3
{
    "arn": "arn:aws:s3tables:us-east-1:000000000000:bucket/busydone-local-verify3"
}

$ aws --endpoint-url=http://localhost:14572 s3tables get-table-bucket-maintenance-configuration --table-bucket-arn "$ARN"
{
    "tableBucketARN": "arn:aws:s3tables:us-east-1:000000000000:bucket/busydone-local-verify3",
    "configuration": {
        "icebergUnreferencedFileRemoval": {
            "status": "enabled",
            "settings": {
                "icebergUnreferencedFileRemoval": {
                    "unreferencedDays": 3,
                    "nonCurrentDays": 10
                }
            }
        }
    }
}
```

Bucket-level PUT round-trip:

```
$ aws --endpoint-url=http://localhost:14572 s3tables put-table-bucket-maintenance-configuration \
    --table-bucket-arn "$ARN" --type icebergUnreferencedFileRemoval \
    --value '{"status":"enabled","settings":{"icebergUnreferencedFileRemoval":{"unreferencedDays":7,"nonCurrentDays":30}}}'
put exit=0

$ aws --endpoint-url=http://localhost:14572 s3tables get-table-bucket-maintenance-configuration --table-bucket-arn "$ARN"
{
    "tableBucketARN": "...",
    "configuration": {
        "icebergUnreferencedFileRemoval": {
            "status": "enabled",
            "settings": {
                "icebergUnreferencedFileRemoval": {
                    "unreferencedDays": 7,
                    "nonCurrentDays": 30
                }
            }
        }
    }
}
```

Table-level, create then get (default, populated — both types):

```
$ aws --endpoint-url=http://localhost:14572 s3tables get-table-maintenance-configuration --table-bucket-arn "$ARN" --namespace myns --name mytable
{
    "tableARN": "arn:aws:s3tables:us-east-1:000000000000:bucket/busydone-local-verify3/table/myns/mytable",
    "configuration": {
        "icebergCompaction": {
            "status": "enabled",
            "settings": {"icebergCompaction": {"targetFileSizeMB": 512, "strategy": "auto"}}
        },
        "icebergSnapshotManagement": {
            "status": "enabled",
            "settings": {"icebergSnapshotManagement": {"minSnapshotsToKeep": 1, "maxSnapshotAgeHours": 120}}
        }
    }
}
```

Table-level PUT round-trip (only the targeted type changes; the other type is
untouched):

```
$ aws --endpoint-url=http://localhost:14572 s3tables put-table-maintenance-configuration \
    --table-bucket-arn "$ARN" --namespace myns --name mytable --type icebergCompaction \
    --value '{"status":"enabled","settings":{"icebergCompaction":{"targetFileSizeMB":256,"strategy":"sort"}}}'
put exit=0

$ aws --endpoint-url=http://localhost:14572 s3tables get-table-maintenance-configuration --table-bucket-arn "$ARN" --namespace myns --name mytable
{
    "tableARN": "...",
    "configuration": {
        "icebergCompaction": {
            "status": "enabled",
            "settings": {"icebergCompaction": {"targetFileSizeMB": 256, "strategy": "sort"}}
        },
        "icebergSnapshotManagement": {
            "status": "enabled",
            "settings": {"icebergSnapshotManagement": {"minSnapshotsToKeep": 1, "maxSnapshotAgeHours": 120}}
        }
    }
}
```

Regression check on pre-existing routes sharing the rewritten bucket-route
dispatch (`get-table-bucket`, the `encryption` stub, `delete-table`,
`delete-table-bucket`), plus 404 on a deleted table's maintenance config:

```
$ aws --endpoint-url=http://localhost:14572 s3tables get-table-bucket --table-bucket-arn "$ARN"
{ "arn": "...", "name": "busydone-local-verify3", "ownerAccountId": "000000000000", "createdAt": "..." }

$ aws --endpoint-url=http://localhost:14572 s3tables get-table-bucket-encryption --table-bucket-arn "$ARN"
{}

$ aws --endpoint-url=http://localhost:14572 s3tables delete-table --table-bucket-arn "$ARN" --namespace myns --name mytable
delete-table exit=0

$ aws --endpoint-url=http://localhost:14572 s3tables get-table-maintenance-configuration --table-bucket-arn "$ARN" --namespace myns --name mytable
aws: [ERROR]: An error occurred (NotFoundException) when calling the GetTableMaintenanceConfiguration operation: Table mytable not found

$ aws --endpoint-url=http://localhost:14572 s3tables delete-table-bucket --table-bucket-arn "$ARN"
delete-table-bucket exit=0
```

Container stopped and removed after verification
(`docker rm -f ms-s3tables`).

### Terraform provider — read on whether it's now unblocked

Not run against `busydone`'s actual `infra/e2e` Terraform (out of scope for
this worktree — another agent owns that repo). But the specific failure
reported — `Value Conversion Error: expected tftypes.Object["settings":
Object["non_current_days":Number,"unreferenced_days":Number]] got
tftypes.Object["settings":Object[]]"` — is a decode of exactly the response
shape now fixed: `GetTableBucketMaintenanceConfiguration` returns a
populated `icebergUnreferencedFileRemoval.settings.icebergUnreferencedFileRemoval`
object (verified above) instead of an empty `{}`. The provider's own
generated Go SDK types match the same nested botocore/Smithy model verified
here, so decoding a populated response of this exact shape should no longer
error. The prior `409 ConflictException: already exists` on retry was a
downstream symptom of the same decode failure (Terraform retried `Create`
after the `Read`-time decode failed) and should not recur once decode
succeeds on the first pass. **Read: likely unblocked**, but this is inference
from the fixed response shape, not an observed `terraform apply` run —
confirming that is the next step for whoever re-enables the `s3_tables`
module in `infra/e2e`.

## Upstream-PR notes

Bug fix / feature-completion to an existing service (`s3tables.py` already
exists) — no issue required per `CONTRIBUTING.md`; straight to a PR. No
Dockerfile/infra changes were made (the slim image was sufficient), so the
separate infra-changes issue-first gate doesn't apply either.

**Tests still need writing before this goes upstream** — this change has
manual CLI verification (above) but no automated test in
`tests/test_s3tables.py`. At minimum, upstream should add:

1. A default-configuration test: create a bucket/table, assert
   `Get*MaintenanceConfiguration` returns the populated defaults with no
   prior `Put`.
2. A round-trip test: `Put*MaintenanceConfiguration` then `Get*` reflects the
   new values, and an untouched type/field is unaffected by a partial `Put`.
3. A tenant-isolation test in the same style as
   `test_s3tables_buckets_are_account_scoped` /
   `test_s3tables_buckets_are_region_scoped`, since this is exactly the
   class of bug a plain `dict` (instead of `AccountRegionScopedDict`) would
   introduce silently.
4. A `NotFoundException` test for `Get*`/`Put*MaintenanceConfiguration`
   against a bucket/table that doesn't exist, and for a table maintenance
   call after the table's been deleted (spot-checked manually above, not
   yet automated).
5. An invalid-`type` test (`ValidationException` for a `type` not in the
   enum) for both bucket- and table-level `Put`.

`GetTableMaintenanceJobStatus` remains unimplemented — flagged as a
follow-up, not bundled into this change (it wasn't part of the reported gap
and no current caller needs it).
