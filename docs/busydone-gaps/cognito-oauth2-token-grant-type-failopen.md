# Gap: `POST /oauth2/token` mints a valid access token for ANY unrecognized `grant_type`

## What

MiniStack's Cognito `/oauth2/token` endpoint (`_oauth2_token`) implements
three grant types — `authorization_code`, `refresh_token`,
`client_credentials` — and then falls through to a catch-all branch that
synthesizes and returns a **valid access token for any other `grant_type`
value**, including an empty or unresolved `client_id`. No client
authentication is performed on this path at all. Now an unrecognized
`grant_type` is rejected with the OAuth2 RFC 6749 `unsupported_grant_type`
error, matching real Cognito's Hosted UI / token-endpoint behavior.

## 🔴 Step-1 verdict: **(b) deliberately fail-open** — the third one found in this file

The code was never merely *missing* a branch; it was an explicit catch-all
with its own comment, `# ── fallback (legacy behaviour for unrecognised
grant_type) ──`, at the true tail of the grant-type dispatch (unmodified
pre-fix source, `ministack/services/cognito.py:5452-5459`):

```python
# ── fallback (legacy behaviour for unrecognised grant_type) ──
pool_id, pool, client = _find_pool_by_client_id(cid)
access_token = _fake_token(cid or new_uuid(), pool_id or "", cid or "", "access")
return json_response({
    "access_token": access_token,
    "token_type": "Bearer",
    "expires_in": 3600,
})
```

`cid`/`csec` come from `_authenticate_client(headers, form)` a few lines
above, but **`csec` (the client secret) is never checked on this branch, and
`cid` doesn't even need to resolve to a real client** — `cid or new_uuid()`
synthesizes a subject out of thin air when it's empty, and `pool_id or ""`
tolerates an unresolved pool the same way. This is the **most severe** of
the fail-opens found in this file (see the "sibling sweep" in
`cognito-refresh-token-failopen.md`, finding #1): it requires **no valid
credentials whatsoever**, unlike the reset-code, refresh-token, and
signup-confirmation gaps, which all require a valid existing username first.

This is the third deliberate fail-open found in this file, after
`ConfirmForgotPassword`'s accept-any-code
(`busydone/gap-cognito-reset-code`, merged as `busydone/local-gaps` commit
`e76f837`) and `REFRESH_TOKEN_AUTH`'s decode-failure fallback
(`busydone/gap-cognito-refresh-failopen`, commit `f9a9fda`). Both were found
one incident at a time; this one and `ConfirmSignUp`'s (documented
separately, `cognito-signup-confirmation-code-validation.md`) were already
flagged by the refresh-token fix's systematic sweep and are fixed together
in this PR.

**No opt-in escape hatch was added.** Same reasoning as both prior fixes:
real Cognito has no "accept unknown grant_type" knob, nothing in busydone's
tests or MiniStack's own test suite exercises this path expecting a token,
and the three real grant types this endpoint implements are unaffected —
`authorization_code`, `refresh_token`, and `client_credentials` keep their
existing behavior untouched. Flagged explicitly for the maintainer below,
consistent with the two prior fixes' precedent.

## Wire shape — OAuth2 RFC 6749, NOT the AWS JSON-protocol `__type` shape

This is the one point where this gap differs structurally from every other
fix in this file's sweep so far: `/oauth2/token` is Cognito's **Hosted UI /
OAuth2 token endpoint**, not an AWS JSON-protocol API operation. It follows
[RFC 6749 §5.2](https://www.rfc-editor.org/rfc/rfc6749#section-5.2) — a JSON
body with `error` and optionally `error_description`, HTTP 400 — not the
`{"__type": "SomeException", "message": "..."}` shape used by
`ConfirmSignUp`/`ConfirmForgotPassword`/`InitiateAuth`/etc. This endpoint
already gets this right for every other error case in the same function —
`_oauth2_error("invalid_grant", ...)`, `_oauth2_error("invalid_client",
...)`, `_oauth2_error("unauthorized_client", ...)` are all used elsewhere in
`_oauth2_token` and the surrounding Hosted UI code — so the fix reuses the
**exact same existing helper** rather than introducing a new error shape:

```python
def _oauth2_error(error: str, description: str, status: int = 400):
    body = json.dumps({"error": error, "error_description": description}).encode()
    return status, {"Content-Type": "application/json"}, body
```

RFC 6749 §5.2 names `unsupported_grant_type` as the exact error code for
"The authorization grant type is not supported by the authorization
server." — the fix calls `_oauth2_error("unsupported_grant_type",
"Unsupported grant_type.")`, matching both the error code and the response
shape real AWS Cognito returns from its token endpoint for the same
condition.

## Why it matters

Measured on unmodified `v1.4.17` (standalone verification below): any POST
to `/oauth2/token` with a `grant_type` outside the three implemented ones —
whether from a client library bug, a fuzzer, or a typo — silently returns a
working Bearer token instead of the 400 an OAuth2 client expects and would
normally handle as a hard failure. Because no credential is checked at all
on this path, this is also the only gap in the whole sweep reachable with
**zero** prior authentication state — every other fail-open found in this
file (reset code, refresh token, signup code) still required a real,
existing username in the target pool.

## Files/functions changed

`ministack/services/cognito.py` only — the catch-all tail of `_oauth2_token`
(~line 5452-5459 pre-fix). The `authorization_code`, `refresh_token`, and
`client_credentials` branches above it are unchanged. No new state, no new
constant — `_oauth2_error` already existed and was already used by every
other error path in this function.

## Standalone verification

Own branch `busydone/gap-cognito-failopens-2`, own worktree/container — not
yet merged into `busydone/local-gaps`. `ministack-local:dev`, container
`ms-cognito-failopen2`, port `14598` (MiniStack's own test suite not run,
per standing instruction).

**1. Bogus `grant_type` → OAuth2 `unsupported_grant_type` error (was: a
valid access token):**

```
$ curl -sS -i -X POST http://localhost:14598/oauth2/token \
    -H "Content-Type: application/x-www-form-urlencoded" \
    -d "grant_type=totally_bogus_grant&client_id=1Nrxle5HfCSko7w7odJld6JExq"
HTTP/1.1 400
Content-Type: application/json
...
{"error": "unsupported_grant_type", "error_description": "Unsupported grant_type."}
```

**2. A valid grant (`client_credentials`, confidential client) still works —
the critical non-regression check:**

```
$ curl -sS -i -X POST http://localhost:14598/oauth2/token \
    --data-urlencode "grant_type=client_credentials" \
    --data-urlencode "client_id=uRP5rvjRzLQfpnkvgkoPwcgLlo" \
    --data-urlencode "client_secret=<the client's real ClientSecret>"
HTTP/1.1 200
Content-Type: application/json
...
{"access_token": "eyJhbGciOiAi...", "token_type": "Bearer", "expires_in": 3600}
```

`authorization_code` and `refresh_token` branches were not independently
re-verified in this session (they were not touched by this diff and are
outside the catch-all this fix replaces).

## Upstream-PR notes

Existing service modification — no issue required per `CONTRIBUTING.md`,
straight to a PR. **Tests still need writing**: a real PR needs (a) an
unrecognized `grant_type` → 400 `unsupported_grant_type` with no
`client_id`/`client_secret` at all (the zero-credential case that makes
this the most severe finding), (b) the same with a garbage `client_id`,
(c) each of the three real grant types (`authorization_code`,
`refresh_token`, `client_credentials`) still succeeds under valid
conditions — the regression guard — (d) `client_credentials` still rejects
a wrong secret (pre-existing behavior, unchanged by this diff, worth
pinning so a future edit to the catch-all can't silently widen it again).

**🔴 Flag for the maintainer, same as the two prior fixes**: this is a
default-behavior change with no opt-in flag, not a bug fix in the "missing
check" sense — there was no existing strict-mode toggle to flip, and this
is the widest-blast-radius of the three (needs no credentials at all). A
maintainer may prefer this land behind an opt-in strict flag rather than as
an unconditional default change. Consistent with both prior fixes' framing,
which shipped without a flag after checking neither busydone's tests nor
MiniStack's own test suite expect the old permissive behavior.
