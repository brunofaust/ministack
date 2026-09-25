# Gap: no `s3vectors` service (control plane + data plane)

## What

MiniStack had **no S3 Vectors support at all** before this change. This adds a
new `ministack/services/s3vectors.py` service module implementing the full
control plane and data plane MiniStack needs to emulate `busydone`'s vector
index:

**Control plane:** `CreateVectorBucket`, `GetVectorBucket`, `DeleteVectorBucket`,
`ListVectorBuckets`, `CreateIndex`, `GetIndex`, `DeleteIndex`, `ListIndexes`,
`PutVectorBucketPolicy`, `GetVectorBucketPolicy`, `DeleteVectorBucketPolicy`.

**Data plane:** `PutVectors`, `GetVectors`, `DeleteVectors`, `ListVectors`,
`QueryVectors` (brute-force cosine/euclidean top-k, stdlib `math` only — no
numpy dependency added).

## Why

`busydone`'s RAG/vector index (its own `CLAUDE.md`: *"The S3 Vectors index is
the core of the product"*) depends on S3 Vectors for both Terraform
provisioning (`aws_s3vectors_vector_bucket`, `aws_s3vectors_index`,
`aws_s3vectors_vector_bucket_policy`) and the runtime pipeline
(`PutVectors`/`QueryVectors`/etc. via `S3VectorStore`). Without this service,
every S3 Vectors call against MiniStack returned `405 MethodNotAllowed`.

## Source of the wire shapes

`botocore`'s `s3vectors` service model, apiVersion `2025-07-15`
(`site-packages/botocore/data/s3vectors/2025-07-15/service-2.json`, read-only,
not modified):

- `protocol: rest-json`, `signingName: s3vectors`, `endpointPrefix: s3vectors`
- Every operation is `POST /<OperationName>` (fixed path, no ARN-in-path
  scheme) — e.g. `{"http": {"method": "POST", "requestUri": "/CreateVectorBucket"}}`
- Bucket ARN pattern: `arn:aws[-a-z0-9]*:s3vectors:[a-z0-9-]+:[0-9]{12}:bucket/[a-z0-9][a-z0-9-.]{1,61}[a-z0-9]`
- Index ARN pattern: same + `/index/[a-z0-9][a-z0-9-.]{1,61}[a-z0-9]`
- `DataType` enum: `["float32"]`; `DistanceMetric` enum: `["euclidean", "cosine"]`
- `QueryOutputVector` carries a single `distance` field (not both
  `distance`/`score` as the e2e reference mock emits) — this emulator matches
  the **real botocore shape**, `distance` only.

## Verbatim evidence of the gap before this change

Confirmed via the task brief: no `s3vectors.py` among the ~84 files in
`ministack/services/`, and every real S3 Vectors call against a stock
MiniStack returns `405 MethodNotAllowed` — there was no route, no module, no
`SERVICE_REGISTRY`/`SERVICE_PATTERNS` entry.

## Files and functions changed

- **`ministack/services/s3vectors.py`** (new, 631 LOC) — full control +
  data plane. Structure copied from `ministack/services/s3tables.py`'s
  idioms verbatim: `AccountRegionScopedDict` module state, `get_state()` /
  `restore_state()` / `reset()` + the `PERSIST_STATE` module-load restore
  triple, ARN-canonicalization helpers (`_bucket_arn`, `_canonical_bucket_arn`,
  `_bucket_name_from_arn`), `error_response_json`/`json_response` from
  `core/responses.py`, `parse_arn`/`ArnParseError` from `core/arn.py`.
  Unlike S3 Tables, S3 Vectors has no ARN-in-path REST scheme to parse — every
  op is `POST /<OperationName>` with the resource identity in the JSON body —
  so routing is a flat `_OPERATIONS` dict keyed by path, not the multi-branch
  ARN-splitting router `s3tables.handle_request` needs.
  - Data-plane helpers `_extract_floats` (dual `{"float32": [...]}` /
    `Float32` / bare-list unwrapping) and `_metadata_matches`
    (`$eq`/`$ne`/`$in`/`$nin`/`$and`/`$or` filter DSL) are ported **nearly
    verbatim** from `tests/e2e/mocks/s3vectors/server.py` (the busydone repo's
    hand-rolled reference mock, read-only, not modified) — same logic, only
    the storage container changed (a per-tenant `AccountRegionScopedDict`
    instead of a single global `dict`).
  - `QueryVectors` diverges from that reference mock: real S3 Vectors emits
    only `distance` (not the reference mock's extra `score` field), and
    distance semantics differ by metric — `_cosine_distance` = `1 -
    cosine_similarity` (0 = identical), `_euclidean_distance` = plain L2 norm
    — both **ascending sort = most similar first**, matching real AWS.
  - `CreateIndex`/`PutVectors`/`QueryVectors` validate `dataType`,
    `dimension` (1-4096, matches vectors against the index's configured
    dimension), and `distanceMetric` — real AWS validates all three; the
    reference mock did not.
- **`ministack/app.py`** — two one-line registry additions:
  `SERVICE_REGISTRY["s3vectors"] = {"module": "s3vectors"}` (routes requests
  to the module) and `_state_map["s3vectors"] = "s3vectors"` (so
  `save_all`/`_reset_all_state` at shutdown/`/_ministack/reset` pick up the
  module once it's loaded — verified empirically below).
- **`ministack/core/router.py`** — one new `SERVICE_PATTERNS["s3vectors"]`
  entry: `host_patterns: [r"s3vectors\."]`, `credential_scope: "s3vectors"`,
  and `path_prefixes` listing the 16 literal fixed op paths (documentation —
  `detect_service()` never actually reads `path_prefixes`, verified by
  reading the whole function; the functional match for a signed client is
  `credential_scope`, matched at step 2 before host/path patterns are ever
  consulted).

## Tenant-scoped state — verified, not assumed

All four module-level state containers (`_vector_buckets`,
`_bucket_policies`, `_indexes`, `_vectors`) are `AccountRegionScopedDict`,
which namespaces every key by `(get_account_id(), get_region())` read from
request-time SigV4 contextvars — the same idiom `s3tables.py` already uses.
There is no "default vector bucket" in real AWS S3 Vectors (unlike Athena's
default workgroup, EC2's default VPC, or Glue's default database) — every
bucket/index is explicitly user-created — so the `_ensure_default_*()`
seed-at-load triple pattern those services use does not apply here; there is
nothing to seed.

Multi-tenancy was verified empirically, not assumed from the container type:

```
=== Account A (111111111111) creates a bucket ===
{
    "vectorBucketArn": "arn:aws:s3vectors:us-east-1:111111111111:bucket/tenant-a-bucket"
}
=== Account B (222222222222) lists buckets -- must NOT see tenant-a-bucket ===
{
    "vectorBuckets": []
}
=== Account A lists buckets -- must see tenant-a-bucket ===
{
    "vectorBuckets": [
        {
            "vectorBucketName": "tenant-a-bucket",
            "vectorBucketArn": "arn:aws:s3vectors:us-east-1:111111111111:bucket/tenant-a-bucket",
            ...
        }
    ]
}
=== Different region (eu-west-1) for account A -- must NOT see us-east-1 bucket ===
{
    "vectorBuckets": []
}
```

And state survives `POST /_ministack/reset` correctly (cleared, not
orphaned — proving `_state_map`'s `"s3vectors"` entry is wired into
`_reset_all_state()`'s sweep):

```
=== POST /_ministack/reset ===
{"reset": "ok"}

{
    "vectorBuckets": []
}
```

## Verification (empirical — full round trip)

Built the image and ran it standalone, per the task brief:

```
$ docker build --build-arg MINISTACK_VERSION=v1.4.17 -t ministack-local:dev .
...
#20 naming to docker.io/library/ministack-local:dev done

$ docker run -d --name ms-s3vectors -p 14570:4566 -e SERVICES=s3vectors -e DEBUG=1 ministack-local:dev
$ curl -fsS http://localhost:14570/_localstack/health   # polled until ready
```

Full round trip via the AWS CLI (`AWS_ACCESS_KEY_ID=test`, region
`us-east-1`, `--endpoint-url=http://localhost:14570`):

```
=== CreateVectorBucket ===
{"vectorBucketArn": "arn:aws:s3vectors:us-east-1:000000000000:bucket/busydone-test-bucket"}

=== CreateIndex (float32, dim 4, cosine) ===
{"indexArn": "arn:aws:s3vectors:us-east-1:000000000000:bucket/busydone-test-bucket/index/busydone-idx"}

=== PutVectorBucketPolicy / GetVectorBucketPolicy ===
{"policy": "{\"Version\":\"2012-10-17\",...}"}

=== PutVectors (4 vectors: near-a [1,0,0,0], near-a2 [0.9,0.1,0,0], far-b [0,1,0,0], orthogonal-c [0,0,1,0]) ===
(200 OK, empty body)

=== QueryVectors (query [1.0, 0.05, 0.0, 0.0], topK=4, cosine, returnDistance) ===
{
    "vectors": [
        {"distance": 0.0012476611221553524, "key": "near-a",        "metadata": {"src": "a"}},
        {"distance": 0.0018416081744208057, "key": "near-a2",       "metadata": {"src": "a"}},
        {"distance": 0.9500623830561078,    "key": "far-b",         "metadata": {"src": "b"}},
        {"distance": 1.0,                    "key": "orthogonal-c", "metadata": {"src": "c"}}
    ],
    "distanceMetric": "cosine"
}
```

**Ordering is correct and sensible**: the two vectors nearest the query
direction (`near-a`, `near-a2`) rank first with near-zero distance; the
orthogonal query-independent axis (`orthogonal-c`) ranks last at distance
`1.0` (cosine distance of exactly-orthogonal vectors); `far-b` (opposite-ish
axis) sits between them at ~0.95. Metadata filtering also verified —
`--filter '{"src":{"$eq":"b"}}'` on the same query correctly narrowed the
result to only `far-b`.

Cleanup path verified too: `DeleteVectors` (removed `orthogonal-c`,
confirmed via `ListVectors`), `GetIndex`/`GetVectorBucketPolicy` on a
now-deleted resource both correctly raised `NotFoundException` (mapped by
botocore to a real `ClientError`, not a raw 400), `DeleteVectorBucketPolicy`,
`DeleteIndex`, and `DeleteVectorBucket` (cascade-deletes its indexes/vectors,
verified empty afterward) — full lifecycle round-trip, no manual state
cleanup needed.

Container stopped and removed after verification
(`docker stop ms-s3vectors && docker rm ms-s3vectors`) — nothing left
running.

## Dependencies

**None added.** `QueryVectors` uses stdlib `math` only (`sqrt`, `sum`,
`zip`) for cosine/euclidean distance — no numpy, matching every other
MiniStack service.

## Upstream PR notes

- **This IS a new service** — per `CONTRIBUTING.md`, brand-new services need
  **an issue opened before a PR**. Do not open a PR directly.
- This change touches neither the `Dockerfile`/`Dockerfile.full` nor
  `pyproject.toml`/`requirements.txt` (no new dependency), so the
  infra/dependency issue-first gate does not additionally apply — only the
  new-service gate does.
- **Tests are not yet written.** Before any upstream submission, this needs
  unit/integration tests exercising: control-plane CRUD for buckets/indexes/
  policies (including the ARN vs. name dual calling convention), the
  `PutVectors`→`QueryVectors` round trip with both distance metrics, the
  metadata filter DSL (`$and`/`$or`/`$in`/`$nin`), dimension-mismatch
  validation, `NotFoundException`/`ConflictException` error paths, cascade
  delete, and multi-tenant (account/region) isolation — mirroring the
  existing `tests/test_s3tables*.py` style, per this repo's own test
  conventions.
- Not implemented: `TagResource`/`UntagResource`/`ListTagsForResource` (not
  in the task's required operation list — `tags` on `CreateVectorBucket`/
  `CreateIndex` is accepted and silently ignored, not persisted or
  queryable). `CreateVectorBucket`'s `encryptionConfiguration.kmsKeyArn` is
  stored but not validated against a real KMS key (MiniStack's `kms` service
  is a separate, unintegrated emulator). Pagination
  (`maxResults`/`nextToken`) is not implemented for `ListVectorBuckets`/
  `ListIndexes`/`ListVectors`/`QueryVectors` — every list operation returns
  its full result set in one page, which is correct for emulator-scale test
  fixtures but diverges from real AWS's paginated contract.
