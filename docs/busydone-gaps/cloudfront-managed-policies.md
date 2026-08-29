# CloudFront: seed AWS-managed cache / origin-request / response-headers policies

## What

MiniStack v1.4.17 implements full CRUD for **custom** CloudFront cache
policies, origin request policies, and response headers policies
(`CreateCachePolicy`, `CreateDistribution`, etc. all work correctly), but it
never seeded AWS's fixed **managed** policy catalog. Real AWS ships these as
global, immutable, well-known policies with stable IDs — identical in every
account and region — and Terraform's
`data "aws_cloudfront_cache_policy" { name = "..." }` (and the
origin-request / response-headers equivalents) resolve by **name** against
that catalog at **plan time**. With nothing seeded, any Terraform plan that
references a managed policy by name aborted before a single resource was
created.

## Why (with verbatim before-error)

busydone's `infra/modules/cloudfront/main.tf` references two managed
policies by name:

```
$ grep -rn "Managed-" /Users/bfaust/Repos/busydone/infra/
infra/modules/cloudfront/main.tf:146:  name = "Managed-CachingDisabled"
infra/modules/cloudfront/main.tf:152:  name = "Managed-AllViewerExceptHostHeader"
```

Running `terraform plan`/`apply` against MiniStack before this fix failed at
the data-source read, before any distribution/cache-policy resource was
even attempted:

```
Error: no matching CloudFront Cache Policy (Managed-CachingDisabled)
Error: no matching CloudFront Origin Request Policy (Managed-AllViewerExceptHostHeader)
```

IDs, TTLs, and forwarding behaviour are transcribed verbatim from the AWS
Managed Policy Reference (fetched 2026-08-15):

- <https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/using-managed-cache-policies.html>
- <https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/using-managed-origin-request-policies.html>
- <https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/using-managed-response-headers-policies.html>

## Precedent

This modifies an **existing** service (`ministack/services/cloudfront.py`),
so per `CONTRIBUTING.md` the "open an issue first" gate applies only to
**new services** and to **infra/Dockerfile/dependency changes** — neither
applies here. This can go straight to a PR.

The pattern itself is not new to this codebase: **PR #609** did exactly
this for IAM managed policies (`arn:aws:iam::aws:policy/*`) — a plain,
non-account-scoped module-level dict (`_aws_managed_policies` in
`services/iam.py`), seeded at import time and re-seeded in `reset()`,
distinct from the per-tenant `AccountScopedDict` stores used for
customer-created resources, with Update/Delete on a managed ARN rejected
with `AccessDenied`. This change mirrors that shape for CloudFront's three
policy families.

## Scope: seeded vs. skipped — a maintainer decision

Per the brief, only the **commonly-referenced subset** was seeded, not
AWS's full managed-policy catalog (~7 cache policies, ~8 origin-request
policies, 5 response-headers policies). The exact set was driven by (a)
what busydone's Terraform actually references and (b) the "obvious
siblings" the brief named explicitly (`CachingOptimized`, `CachingDisabled`,
`AllViewer`, `AllViewerExceptHostHeader`, `CORS-*`).

**Seeded (8 total):**

| Family | Name | Id | Why |
|---|---|---|---|
| Cache | `Managed-CachingDisabled` | `4135ea2d-6df8-44a3-9df3-4b5a84be39ad` | Directly referenced by busydone infra |
| Cache | `Managed-CachingOptimized` | `658327ea-f89d-4fab-a63d-7e88639e58f6` | Named sibling in the brief; the single most common CloudFront cache policy in the wild |
| Origin request | `Managed-AllViewerExceptHostHeader` | `b689b0a8-53d0-40ab-baf2-68738e2966ac` | Directly referenced by busydone infra |
| Origin request | `Managed-AllViewer` | `216adef6-5c7f-47e4-b989-5492eafa07d3` | Named sibling in the brief |
| Response headers | `Managed-SimpleCORS` | `60669652-455b-4ae9-85a4-c4c02393f86c` | "CORS-* as applicable" — no RHP is referenced by busydone yet, seeded for family completeness |
| Response headers | `Managed-CORS-With-Preflight` | `5cc3b908-e619-4b99-88e5-2cf7f45965bd` | Same |
| Response headers | `Managed-CORS-and-SecurityHeadersPolicy` | `e61eb60c-9c35-4d20-a928-2b84e02af89c` | Same |
| Response headers | `Managed-CORS-with-preflight-and-SecurityHeadersPolicy` | `eaab4381-ed33-4a86-88ca-d9558dc6cd63` | Same |

**Explicitly skipped (not seeded):**

- Cache: `Managed-Amplify` (+ 4 Amplify-Hosting-only sub-policies),
  `Managed-CachingOptimizedForUncompressedObjects`,
  `Managed-Elemental-MediaPackage`, `Managed-UseOriginCacheControlHeaders`,
  `Managed-UseOriginCacheControlHeaders-QueryStrings`.
- Origin request: `Managed-AllViewerAndCloudFrontHeaders-2022-06`,
  `Managed-CORS-CustomOrigin`, `Managed-CORS-S3Origin`,
  `Managed-Elemental-MediaTailor-PersonalizedManifests`,
  `Managed-HostHeaderOnly`, `Managed-UserAgentRefererHeaders`.
- Response headers: `Managed-SecurityHeadersPolicy` (standalone; the brief
  said "CORS-* as applicable" and this one has no `CORS` in its name — its
  5-header block is still available for reuse since the two `*-and-
  SecurityHeadersPolicy` combo policies above embed it).

**All of these auto-vivify identically** (same code path, same
`AccessDenied` rejection on mutation) once a real ID/name is added to the
`seeds` list in `_seed_managed_cache_policies` /
`_seed_managed_origin_request_policies` /
`_seed_managed_response_headers_policies` in `ministack/services/cloudfront.py`
— there is no structural reason the rest of the catalog couldn't be added
later. **Flagging explicitly for a maintainer:** is "commonly-referenced
subset, grow on demand" the right default, or should MiniStack seed the
*entire* fixed catalog up front (it's static data, costs nothing at
runtime, and removes any future "why doesn't `Managed-X` exist" surprise)?
The IAM precedent (PR #609) took the same subset-with-autovivify approach
for its ~25 seeded policies against AWS's ~1,000-policy catalog, but
CloudFront's managed-policy catalogs are two orders of magnitude smaller
(~20 total across all three families), which weakens the "too much to seed
up front" argument that justified IAM's choice.

One deliberate asymmetry versus the IAM precedent: IAM has an
`_autovivify_aws_managed_policy` fallback (permissive, opt-in via
`MINISTACK_AUTOCREATE_AWS_MANAGED=1`) for any *unseeded* managed ARN. This
change does **not** add an equivalent auto-vivify fallback for CloudFront —
an unseeded name/ID still 404s (`NoSuchCachePolicy` /
`NoSuchOriginRequestPolicy` / `NoSuchResponseHeadersPolicy`), matching how a
Terraform `data` block genuinely should fail on a policy MiniStack doesn't
know about, rather than silently synthesizing a policy with guessed
settings. Flagging this too, in case a maintainer prefers parity with the
IAM fallback behavior instead.

## Design

Modeled directly on the IAM AWS-managed-policy pattern:

- **Storage:** `_managed_cache_policies`, `_managed_origin_request_policies`,
  `_managed_response_headers_policies` — plain, non-account-scoped
  module-level `dict`s (not `AccountScopedDict`), because managed policies
  have no owning account; every tenant reads the same catalog. This is a
  deliberate divergence from the per-tenant `AccountScopedDict`/
  `AccountRegionScopedDict` containers used elsewhere in this codebase for
  genuinely tenant-owned mutable state — that idiom does not apply here
  because the data being modeled is a global, read-only, non-tenant
  constant, exactly the same reasoning IAM's `_aws_managed_policies` already
  established in this repo.
- **Seeding:** `_seed_managed_cache_policies()` /
  `_seed_managed_origin_request_policies()` /
  `_seed_managed_response_headers_policies()`, called once at import time
  (`_seed_all_managed_policies()` at module scope) and again inside
  `reset()` (clear + re-seed, so `POST /_ministack/reset` restores the
  canonical catalog rather than leaving it empty until process restart).
- **`Type` filtering:** `ListCachePolicies` / `ListOriginRequestPolicies` /
  `ListResponseHeadersPolicies` now accept the optional `Type` query param
  (`managed` | `custom`); omitted returns both (matches real AWS). Managed
  entries are tagged `Type=managed` in the XML summary and never appear
  under a `Type=custom` filter, and vice versa.
- **Lookup:** `GetCachePolicy(Id=...)` / `GetCachePolicyConfig` /
  `ListDistributionsByCachePolicyId` (and the ORP/RHP equivalents) check the
  managed store as a fallback after the customer store, so a managed ID
  resolves correctly whether or not any custom policies exist.
- **Mutation blocked:** `UpdateCachePolicy` / `DeleteCachePolicy` (and the
  shared `_policy_update` / `_policy_delete` used by ORP/RHP) reject a
  managed ID up front with `AccessDenied` (403) — mirroring the exact error
  code IAM's `_delete_policy`/`_delete_policy_version` already use for its
  own AWS-managed policies in this codebase.

## Files changed

- `ministack/services/cloudfront.py` — all of the above. No other file
  touched (no Dockerfile, no `pyproject.toml`, no test-suite changes — the
  brief asked me not to run or edit MiniStack's own test suite).

## Verification (empirical, standalone container, port 14571)

Built and ran the slim image directly (not via docker-compose), container
name `ms-cloudfront`:

```
$ docker build --build-arg MINISTACK_VERSION=v1.4.17 -t ministack-cloudfront:dev .
...
#20 naming to docker.io/library/ministack-cloudfront:dev done

$ docker run -d --name ms-cloudfront -p 14571:4566 ministack-cloudfront:dev
3377330f2350dc158587c5b79cba7284714c7945e986e5e857184f9a98f10734
```

**1. `list-cache-policies --type managed` returns exactly the 2 seeded cache policies:**

```json
{
    "CachePolicyList": {
        "MaxItems": 100,
        "Quantity": 2,
        "Items": [
            {
                "Type": "managed",
                "CachePolicy": {
                    "Id": "4135ea2d-6df8-44a3-9df3-4b5a84be39ad",
                    "LastModifiedTime": "2020-05-20T04:34:00+00:00",
                    "CachePolicyConfig": {
                        "Comment": "Policy with caching disabled",
                        "Name": "Managed-CachingDisabled",
                        "DefaultTTL": 0, "MaxTTL": 0, "MinTTL": 0,
                        "ParametersInCacheKeyAndForwardedToOrigin": {
                            "EnableAcceptEncodingGzip": false,
                            "EnableAcceptEncodingBrotli": false,
                            "HeadersConfig": {"HeaderBehavior": "none", "Headers": {"Quantity": 0}},
                            "CookiesConfig": {"CookieBehavior": "none", "Cookies": {"Quantity": 0}},
                            "QueryStringsConfig": {"QueryStringBehavior": "none", "QueryStrings": {"Quantity": 0}}
                        }
                    }
                }
            },
            {
                "Type": "managed",
                "CachePolicy": {
                    "Id": "658327ea-f89d-4fab-a63d-7e88639e58f6",
                    "CachePolicyConfig": {"Name": "Managed-CachingOptimized", "DefaultTTL": 86400, "MaxTTL": 31536000, "MinTTL": 1, "...": "..."}
                }
            }
        ]
    }
}
```

`list-cache-policies --type custom` on the same fresh container returned
`{"CachePolicyList": {"MaxItems": 100, "Quantity": 0}}` — confirms managed
policies do **not** leak into the `custom` filter. `list-cache-policies`
with no `--type` returned `Quantity: 2` (both, matching real AWS default
behavior).

**2. Name-based lookup (the exact Terraform data-source pattern) resolves the correct ID:**

```
$ aws cloudfront list-cache-policies --type managed --endpoint-url http://localhost:14571 --output json \
    | python3 -c "... find Name == 'Managed-CachingDisabled' ..."
MATCH id= 4135ea2d-6df8-44a3-9df3-4b5a84be39ad
```

And `GetCachePolicy --id 4135ea2d-6df8-44a3-9df3-4b5a84be39ad` returns the
full config with `"Name": "Managed-CachingDisabled"`, ETag
`a46c4a0d-d433-4501-8754-f87fe17edd1e`.

**3. Attempted mutation of a managed policy is rejected (verbatim):**

```
$ aws cloudfront delete-cache-policy --id 4135ea2d-6df8-44a3-9df3-4b5a84be39ad \
    --if-match a46c4a0d-d433-4501-8754-f87fe17edd1e --endpoint-url http://localhost:14571

aws: [ERROR]: An error occurred (AccessDenied) when calling the DeleteCachePolicy operation: The specified cache policy (4135ea2d-6df8-44a3-9df3-4b5a84be39ad) is an AWS-managed CloudFront policy and cannot be modified or deleted.
exit code: 254
```

Confirmed it still exists afterward:

```
$ aws cloudfront get-cache-policy --id 4135ea2d-6df8-44a3-9df3-4b5a84be39ad --endpoint-url http://localhost:14571 --output json
still present, Name= Managed-CachingDisabled
```

Same rejection verified for the origin-request-policy family:

```
$ aws cloudfront delete-origin-request-policy --id b689b0a8-53d0-40ab-baf2-68738e2966ac \
    --if-match dummy --endpoint-url http://localhost:14571

aws: [ERROR]: An error occurred (AccessDenied) when calling the DeleteOriginRequestPolicy operation: The specified origin request policy (b689b0a8-53d0-40ab-baf2-68738e2966ac) is an AWS-managed CloudFront policy and cannot be modified or deleted.
```

**4. Response-headers-policy family also seeds and lists correctly:**

```
$ aws cloudfront list-response-headers-policies --type managed --endpoint-url http://localhost:14571 --output json
Quantity: 4
 - Managed-SimpleCORS 60669652-455b-4ae9-85a4-c4c02393f86c
 - Managed-CORS-With-Preflight 5cc3b908-e619-4b99-88e5-2cf7f45965bd
 - Managed-CORS-and-SecurityHeadersPolicy e61eb60c-9c35-4d20-a928-2b84e02af89c
 - Managed-CORS-with-preflight-and-SecurityHeadersPolicy eaab4381-ed33-4a86-88ca-d9558dc6cd63
```

Container stopped and removed after verification (`docker stop/rm
ms-cloudfront`) — no residual state left running.

## Not run

Per instructions, MiniStack's own lint/test suite was not run in this
session. `python3 -m py_compile` / `ast.parse` was used locally to confirm
`cloudfront.py` has no syntax errors; the empirical AWS-CLI verification
above against the built container is what actually proves the behavior.
