# Gap: Glue never seeds a `default` database

**Upstream-PR notes**: Bug fix to an existing service (`glue.py` already
exists) — no issue required per `CONTRIBUTING.md`, straight to a PR.

## What

`ministack`'s Glue emulator never seeds a `default` database —
`GetDatabase`/`CreateTable` against `"default"` 400s with
`EntityNotFoundException` on a stack that has made zero Glue API calls.

## Why

Real AWS auto-provisions a Data Catalog database named `default` in every
account/region — it exists before the account ever calls `CreateDatabase`,
exactly like the `default` VPC in EC2. Terraform's `aws_glue_catalog_table`
(and any raw `CreateTable`/`GetTable` call) that targets
`database_name = "default"` assumes this pre-existing row. Field set
verified against botocore's own Glue model:
`botocore/data/glue/2017-03-31/service-2.json` → `shapes.Database.members`
(`Name`, `Description`, `LocationUri`, `Parameters`, `CreateTime`,
`CatalogId`, plus advanced-only `CreateTableDefaultPermissions`,
`TargetDatabase`, `FederatedDatabase` that a plain database never sets —
matches what `ministack`'s own `_create_database()` already emits).

## Evidence of the gap

Verbatim, captured against a clean `v1.4.17` build in a throwaway worktree
before the fix, `aws-cli` v1.45.63:

```
$ aws --endpoint-url=http://localhost:14567 glue get-database --name default

aws: [ERROR]: An error occurred (EntityNotFoundException) when calling the
GetDatabase operation: Database default not found
```

## Files/functions changed

`ministack/services/glue.py` (+21 LOC / 0 removed):

- Added `_DEFAULT_DATABASE_NAME = "default"` and
  `_ensure_default_database()` — idempotent seed, same idiom as
  `_ensure_default_workgroup()` / `_ensure_default_data_catalog()` in
  `ministack/services/athena.py` and `_ensure_defaults_initialized()` /
  `_init_defaults()` in `ministack/services/ec2.py`: an `if name not in
  _databases:` guard writing the same field shape `_create_database()`
  already produces.
- Called at **module load** (covers the default account/region scope
  immediately, same as `ec2.py`'s `_ensure_defaults_initialized()` call
  right after its definition).
- Called at the **top of `handle_request()`** (covers every other
  account/region a multi-tenant caller might use, lazily, on first
  request — same as `athena.py`'s pattern; required because `_databases`
  is an `AccountRegionScopedDict` keyed off the *request's* SigV4
  credentials, which aren't known at module-load time).
- Called again at the **end of `reset()`** — `/_ministack/reset` runs
  inside a request context (account/region contextvars are already set
  from that call's Authorization header), so re-seeding there immediately
  restores the invariant instead of waiting for the next Glue call, same
  as `ec2.py`'s `reset()` calling `_init_defaults()` directly rather than
  only relying on the lazy `handle_request` guard.

## Verification

Rebuilt `ministack-local:dev`, ran standalone on `localhost:14566`:

```
$ aws --endpoint-url=http://localhost:14566 glue get-database --name default
{
    "Database": {
        "Name": "default",
        "Description": "",
        "Parameters": {},
        "CreateTime": "2026-08-15T02:59:17+02:00",
        "CatalogId": "000000000000"
    }
}
```

Also verified the database survives `/_ministack/reset` (POST reset, then
re-ran `get-database` — same success response, new `CreateTime`), and that
the real downstream use case works end-to-end
(`CreateTable`/`GetTable` against `database_name=default`):

```
$ aws --endpoint-url=http://localhost:14566 glue create-table \
    --database-name default --table-input '{"Name":"probe_table", ...}'
{}
$ aws --endpoint-url=http://localhost:14566 glue get-table \
    --database-name default --name probe_table
{
    "Table": {
        "Name": "probe_table",
        "DatabaseName": "default",
        ...
    }
}
```

## Open question for a maintainer

The `Description` field is seeded as `""` (empty string) — this matches
what `_create_database()` defaults to when no description is given, but
wasn't independently confirmed against a real AWS account (no live AWS
access during this investigation); if a maintainer has verified real-AWS
text for the auto-created `default` database's description, it should
replace the empty-string guess.
