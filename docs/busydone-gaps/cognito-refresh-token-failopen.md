# Gap: `REFRESH_TOKEN_AUTH` accepts ANY refresh token

## What

MiniStack's Cognito emulator accepted **any** value as a `RefreshToken` on
`REFRESH_TOKEN_AUTH` (via `InitiateAuth`, `AdminInitiateAuth`, and
`GetTokensFromRefreshToken`) and authenticated the caller as the **first
user in the pool** whenever the token couldn't be decoded — an explicit
comment documented this as a deliberate choice, not a missing feature. Now
an undecodable or non-matching refresh token is rejected with
`NotAuthorizedException`, matching real Cognito's behavior when a lookup in
its internal token store misses.

## 🔴 Step-1 verdict: **(b) deliberately fail-open** — the second one found in this file

The code was never merely *missing* a check; it had an explicit comment
documenting the choice to fall back, at both call sites (unmodified
pre-fix source, `ministack/services/cognito.py`):

```python
# ~line 2977, _admin_initiate_auth
user = _user_from_token(refresh_token, pool)
if not user:
    # Fall back to first user if token can't be decoded (e.g. externally issued token)
    users = list(pool["_users"].values())
    if not users:
        return error_response_json("NotAuthorizedException", "No users in pool.", 400)
    user = users[0]
```

```python
# ~line 3203-3208, _refresh_auth_result (shared core for InitiateAuth + GetTokensFromRefreshToken)
user = _user_from_token(refresh_token, pool)
if not user:
    users = list(pool["_users"].values())
    if not users:
        return None, error_response_json("NotAuthorizedException", "No users in pool.", 400)
    user = users[0]
```

This is the same class of deliberate fail-open as `ConfirmForgotPassword`'s
"accept any confirmation code" (`busydone/gap-cognito-reset-code`, merged as
`busydone/local-gaps` commit `e76f837`) — **the second one found in this file
by stumbling over it one incident at a time**, which is why this PR also
includes a systematic sweep for the rest (below), instead of waiting for a
third accident.

**No opt-in escape hatch was added.** The task brief asked to consider
whether the "externally issued token" case in the comment is a real
scenario worth preserving behind an explicit flag. Decision: **no** —
grepping busydone's own test suite for exactly this scenario
(`frontend/tests/e2e/auth.spec.ts:567`, see "Why it matters" below) shows
the ONLY existing expectation for an undecodable refresh token is that it
is **rejected**; nothing in busydone's tests or MiniStack's own test suite
sends a garbage token expecting success. Real Cognito has no such knob
either. Consistent with the reset-code fix's precedent (which also shipped
with no flag), this is flagged explicitly for the maintainer in case they'd
rather gate it than change the default outright.

## Why it matters

Measured on unmodified `v1.4.17`, verbatim:

```
$ aws --endpoint-url http://localhost:14597 cognito-idp initiate-auth \
    --auth-flow REFRESH_TOKEN_AUTH --client-id <cid> \
    --auth-parameters REFRESH_TOKEN=this-is-not-a-real-token
→ full AuthenticationResult (valid AccessToken/IdToken) for the first user created in the pool
```

Real Cognito rejects an invalid/unrecognized refresh token with
`NotAuthorizedException`.

**This is not a hypothetical — busydone's own e2e suite already asserts the
opposite of the buggy behavior and was silently passing for the wrong
reason.** `frontend/tests/e2e/auth.spec.ts:567`:

```ts
test('refresh with a bogus refresh_token cookie returns 401', async () => {
  const freshContext = await newGuardedContext({ baseURL: process.env.PLAYWRIGHT_BASE_URL })
  try {
    const res = await freshContext.post('/api/v1/auth/refresh', {
      headers: { Cookie: 'refresh_token=this-is-not-a-real-token' },
    })
    expect(res.status()).toBe(401)
  } finally {
    await freshContext.dispose()
  }
})
```

That is the **exact string** used in this gap's confirmed reproduction.
Before this fix, MiniStack's Cognito would have returned a full
`AuthenticationResult` for that string, busydone's `/api/v1/auth/refresh`
route would have exchanged it for a *real* session as the pool's first
user, and the test's `expect(res.status()).toBe(401)` would have failed —
almost certainly the same failure shape as the reset-code gap (a positive
result where a negative one was asserted), just not yet triggered because
this exact spec/assertion combination hadn't been run against
`ministack-local:dev` yet in this investigation. This is the strongest
signal in the whole sweep that these two Cognito gaps are not academic:
busydone's own test authors already wrote the requirement MiniStack was
silently violating.

## Files/functions changed

`ministack/services/cognito.py` only — **12 insertions, 11 deletions** (a
pure logic change; no new state, no new constant). Both call sites:

- `_admin_initiate_auth`'s `REFRESH_TOKEN_AUTH`/`REFRESH_TOKEN` branch
  (~line 2970-2984): the `if not user:` fallback-to-`users[0]` block is
  replaced with `return error_response_json("NotAuthorizedException",
  "Invalid Refresh Token", 400)`.
- `_refresh_auth_result` (~line 3196-3212), the shared core used by both
  public `InitiateAuth`'s `REFRESH_TOKEN_AUTH` branch and
  `GetTokensFromRefreshToken` — same replacement, so both callers inherit
  the fix from one place. `_initiate_auth`'s own `REFRESH_TOKEN_AUTH` branch
  (~line 3287) and `_get_tokens_from_refresh_token` (~line 3217) both
  delegate to `_refresh_auth_result` and needed no changes of their own.

No new store was added — `_user_from_token`, `pool["_users"]`, and
`_refresh_token_revoked` are all pre-existing, so tenant isolation and
`reset()` clearing are unaffected (nothing new to isolate or clear).

## Standalone verification

Own branch `busydone/gap-cognito-refresh-failopen`, own worktree/container —
not yet merged into `busydone/local-gaps`, no combined-image proof for this
entry. `ministack-local:dev`, container `ms-cognito-failopen`, port `14597`
(MiniStack's own test suite not run, per standing instruction).

**1. Garbage token, `InitiateAuth`:**

```
$ aws --endpoint-url http://localhost:14597 cognito-idp initiate-auth \
    --auth-flow REFRESH_TOKEN_AUTH --client-id Xx8zpCPuf7Nj5cmXGnYaR0cd0O \
    --auth-parameters REFRESH_TOKEN=this-is-not-a-real-token
An error occurred (NotAuthorizedException) when calling the InitiateAuth operation: Invalid Refresh Token
```

**2. Garbage token, `AdminInitiateAuth`:**

```
$ aws --endpoint-url http://localhost:14597 cognito-idp admin-initiate-auth \
    --user-pool-id us-east-1_5HSuePc0S --client-id Xx8zpCPuf7Nj5cmXGnYaR0cd0O \
    --auth-flow REFRESH_TOKEN_AUTH \
    --auth-parameters REFRESH_TOKEN=this-is-not-a-real-token
An error occurred (NotAuthorizedException) when calling the AdminInitiateAuth operation: Invalid Refresh Token
```

**3. Legitimate refresh token (obtained from a real `USER_PASSWORD_AUTH`
login) still works — the critical non-regression check:**

```
$ aws --endpoint-url http://localhost:14597 cognito-idp initiate-auth \
    --auth-flow REFRESH_TOKEN_AUTH --client-id Xx8zpCPuf7Nj5cmXGnYaR0cd0O \
    --auth-parameters REFRESH_TOKEN=<real token from USER_PASSWORD_AUTH> \
    --query 'AuthenticationResult.TokenType' --output text
Bearer

$ aws --endpoint-url http://localhost:14597 cognito-idp admin-initiate-auth \
    --user-pool-id us-east-1_5HSuePc0S --client-id Xx8zpCPuf7Nj5cmXGnYaR0cd0O \
    --auth-flow REFRESH_TOKEN_AUTH \
    --auth-parameters REFRESH_TOKEN=<real token> \
    --query 'AuthenticationResult.TokenType' --output text
Bearer
```

**4. `GetTokensFromRefreshToken` behaves consistently:**

```
$ aws --endpoint-url http://localhost:14597 cognito-idp get-tokens-from-refresh-token \
    --client-id Xx8zpCPuf7Nj5cmXGnYaR0cd0O --refresh-token this-is-not-a-real-token
An error occurred (NotAuthorizedException) when calling the GetTokensFromRefreshToken operation: Invalid Refresh Token

$ aws --endpoint-url http://localhost:14597 cognito-idp get-tokens-from-refresh-token \
    --client-id Xx8zpCPuf7Nj5cmXGnYaR0cd0O --refresh-token <real token> \
    --query 'AuthenticationResult.TokenType' --output text
Bearer
```

**5. Tenant scoping / `reset()`:** no new store was added by this fix (see
above), so isolation is inherited unchanged from the pre-existing
`_user_pools`/`pool["_users"]` containers. `reset()` clears them as before —
verified: `POST /_ministack/reset` → `{"reset": "ok"}`, then
`describe-user-pool` for the pool created in this session returns
`ResourceNotFoundException: User pool ... does not exist.`

## Upstream-PR notes

Existing service modification — no issue required per `CONTRIBUTING.md`,
straight to a PR. **Tests still need writing**: a real PR needs (a) garbage
token → `NotAuthorizedException` for all three of `InitiateAuth`,
`AdminInitiateAuth`, `GetTokensFromRefreshToken`, (b) a real refresh token
still succeeds (the regression guard), (c) a revoked refresh token still
returns "Refresh Token has been revoked" (unchanged, pre-existing
behavior — verify the new code didn't shadow it), (d) cross-pool isolation
(a token minted in pool A rejected against pool B's client).

**🔴 Flag for the maintainer, same as the reset-code fix**: this is a
default-behavior change with no opt-in flag, not a bug fix in the "missing
check" sense — there was no existing strict-mode toggle to flip. A
maintainer may prefer this land behind an opt-in strict flag (mirroring
`MINISTACK_COGNITO_PRETOKEN_STRICT`'s pattern, but inverted — that flag
opts INTO strict rejection with a permissive default, whereas this PR makes
rejection the only behavior) rather than as an unconditional default
change. See the "No opt-in escape hatch was added" section above for why
this PR proceeded without one anyway.

---

## Sibling sweep: other fail-opens found in `cognito.py`

Two deliberate fail-opens (`ConfirmForgotPassword`'s code, this refresh
token) have now been found in this one file by stumbling over them one
incident at a time. Rather than wait for a third accident, the whole file
was grepped for the tell-tale patterns (`fall back`, `accept any`, `in
emulation`, `not validated`, `skip validation`, `for simplicity`, `TODO`,
`real AWS`) and manually triaged. Findings below, ranked by
security-relevant severity in a test context. **None of these are fixed by
this PR** — scope was the refresh-token fail-open only, per the task brief.

### 1. `POST /oauth2/token` mints a valid access token for ANY unrecognized `grant_type` — no client authentication at all

- **Location**: `_oauth2_token`, `ministack/services/cognito.py` ~line
  5452-5459 (the function's docstring itself says it "supports
  authorization_code, refresh_token, client_credentials" — three explicit
  branches above this trailing catch-all).
- **Verbatim**:
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
  above, but **`csec` is never checked in this branch, and `cid` doesn't
  even need to resolve to a real client** — `cid or new_uuid()` synthesizes
  one out of thin air if it's empty.
- **Real AWS**: returns `{"error": "unsupported_grant_type"}` with HTTP 400
  for any `grant_type` outside the ones it implements. It never issues a
  token without validating the grant.
- **Test-context consequence**: the most severe of the group — it requires
  **no valid credentials whatsoever**, not even a real client_id, unlike
  every other finding here (which all require a valid username/existing
  user first). A test (or a bug in a client library that mis-sets
  `grant_type`) gets a working Bearer token silently instead of a 400,
  which could mask an integration bug the same way the refresh-token gap
  masked a real busydone test assertion.
- **Effort**: small — add an explicit `unsupported_grant_type` branch as
  the true fallback, ahead of or replacing this one. Similar shape to the
  refresh-token fix.

### 2. `ConfirmSignUp` accepts ANY confirmation code

- **Location**: `_confirm_sign_up`, ~line 3625-3645.
- **Verbatim**:
  ```python
  user, err = _resolve_user(pool, username)
  if err:
      return err

  # Accept any code in emulation
  user["UserStatus"] = "CONFIRMED"
  user["UserLastModifiedDate"] = _now_epoch()
  return json_response({})
  ```
  `code = data.get("ConfirmationCode", "")` is read at the top of the
  function (line 3628) but **never compared to anything** — `SignUp`
  already stores `user["_confirmation_code"] = "123456"` (line 3606) and
  emails it via the same SES path `ConfirmForgotPassword` used, so the
  fix would be structurally identical to the already-merged reset-code fix.
- **Real AWS**: `CodeMismatchException` on a wrong code,
  `ExpiredCodeException` on an expired one.
- **Test-context consequence**: any test that signs up a user and confirms
  with a wrong/blank code passes when it should fail — the exact sibling of
  the already-fixed `ConfirmForgotPassword` bug, just for account
  verification instead of password reset. **This is the highest-confidence
  "real bug the same way this one was" candidate** — same root pattern,
  same file, same missing read-back-and-compare, code already generated and
  delivered.
- **Effort**: small — same shape as the merged `ConfirmForgotPassword` fix
  (~15-20 line diff), reusing `_confirmation_code`/an expiry field on the
  user dict, no new plumbing (SES path already wired).

### 3. `SOFTWARE_TOKEN_MFA`/`MFA_SETUP` TOTP challenges accept ANY code — in three places, because TOTP is never actually implemented

- **Locations** (one root cause, three call sites — mirrors this PR's own
  "two call sites, one root cause" shape):
  - `_admin_respond_to_auth_challenge`, `SOFTWARE_TOKEN_MFA` branch, ~line
    3165-3173: `# Accept any TOTP code in emulator — no real TOTP validation`
  - `_respond_to_auth_challenge`, `SOFTWARE_TOKEN_MFA`/`MFA_SETUP` branch,
    ~line 3493-3501: `# Accept any TOTP code in emulator`
  - `_verify_software_token` (TOTP enrollment), ~line 4181-4198:
    `"""Accept any TOTP code. Mark the user as TOTP-enrolled..."""` /
    `user_code = data.get("UserCode", "")  # accepted regardless of value in emulator`
- **Real AWS**: validates the 6-digit TOTP code against the shared secret
  issued by `AssociateSoftwareToken` (`SecretCode`) using standard
  RFC 6238 time-step math, at both enrollment (`VerifySoftwareToken`) and
  every subsequent MFA challenge. A wrong code raises
  `EnableSoftwareTokenMFAException` (enrollment) or `CodeMismatchException`
  (challenge).
- **Test-context consequence**: an MFA-enabled account is not actually
  protected by the second factor at all in MiniStack — any test (or
  accidental code path) that reaches a `SOFTWARE_TOKEN_MFA` challenge with a
  garbage/blank code completes login. Unlike the refresh-token and
  confirm-code bugs, this doesn't require impersonating "the first user in
  the pool" — it silently defeats MFA for whichever specific user the
  caller already named, which is arguably a narrower blast radius per call
  but a complete defeat of the feature it claims to implement.
- **Effort**: medium — `_associate_software_token` currently issues a
  random base32 secret that's never persisted on the user or checked
  against later (line 4176: `secret = base64.b32encode(secrets.token_bytes(20)).decode()`,
  discarded after the response). A real fix needs (a) persisting the secret
  on the user at `AssociateSoftwareToken`, (b) implementing RFC 6238 TOTP
  verification (Python stdlib `hmac`/`struct`, no new dependency needed) at
  both `_verify_software_token` and both `RespondToAuthChallenge` sites,
  (c) a small time-skew tolerance window like real AWS. Larger than the
  other findings because it requires actually implementing TOTP math that
  doesn't exist anywhere in the file today, not just wiring up an existing
  stored value.

### 4. `SECRET_HASH` is never validated — structurally absent, not just skipped

- **Location**: file-wide. `grep -n "SECRET_HASH" ministack/services/cognito.py`
  returns **zero matches** in the entire file.
- **What's missing**: real Cognito requires and validates `SECRET_HASH`
  (`Base64(HMAC-SHA256(client_secret, username + client_id))`) on
  `InitiateAuth`/`AdminInitiateAuth` whenever the app client has a secret
  configured, rejecting a missing/wrong hash with `NotAuthorizedException`
  before ever checking the password. MiniStack's `USER_PASSWORD_AUTH`,
  `ADMIN_USER_PASSWORD_AUTH`, and `REFRESH_TOKEN_AUTH` branches accept
  `AuthParameters.SECRET_HASH` as an ignored, uncompared field — the
  parameter is never even read out of `auth_params`.
- **Real AWS**: `NotAuthorizedException` (or, for `SignUp`, a 400) when a
  confidential client's request is missing `SECRET_HASH` or the hash
  doesn't match.
- **Test-context consequence**: this is the "any validation that is
  structurally absent" pattern named in the task brief — a client secret
  configured on a MiniStack app client provides **zero** protection: any
  caller who knows just the username/password (no secret at all) can
  authenticate against a confidential client. busydone's own unit tests
  (`tests/unit/test_api_auth_cognito.py`,
  `test_refresh_token_includes_secret_hash_when_username_and_secret_given`)
  assert that busydone's *client* computes and sends `SECRET_HASH`
  correctly — but nothing in a MiniStack-backed e2e run would ever catch a
  regression that stopped sending it, because the server-side check doesn't
  exist to fail.
- **Effort**: medium — needs the HMAC computation (stdlib `hmac` +
  `base64`, no new dependency) plus a validation branch in each of the
  `USER_PASSWORD_AUTH`/`ADMIN_USER_PASSWORD_AUTH`/`REFRESH_TOKEN_AUTH`
  branches (3-4 call sites) gated on `client.get("ClientSecret")` being
  set — same "only enforce when the client actually has a secret" shape
  already used correctly for the `client_credentials`/`refresh_token`
  OAuth2 branches (~line 5312, 5400, 5422-5424).

### 5. OIDC federated-login `id_token` signature never verified (lower priority — explicitly documented, project-wide non-goal)

- **Location**: `_decode_id_token_unverified`, ~line 4714-4730, consumed by
  `_oauth2_idp_response` (~line 4733) for the external-IdP federated login
  callback.
- **Verbatim** (already self-documented, not a bare comment):
  ```python
  def _decode_id_token_unverified(id_token: str) -> dict:
      """Decode a JWT id_token payload without verifying its signature.

      Matches MiniStack's wider stance on emulator-side crypto checks: we don't
      verify SigV4 on AWS requests and we don't verify SAML response signatures
      on `/saml2/idpresponse`, so we don't verify OIDC id_token signatures here
      either. The threat model for a local emulator is developer testing, not
      token forgery, and adding a JWKS fetch + RS256 verify would be inconsistent
      with the rest of the codebase. Documented gap from real AWS Cognito.
      """
  ```
  `_saml2_idp_response` (~line 4578) is the same category, referenced by
  name in that docstring, and was not separately deep-dived given the time
  budget for this sweep.
- **Real AWS**: fetches the external IdP's JWKS and verifies the
  `id_token`'s signature, issuer, and audience before trusting any claim
  from it, since a forged `id_token` would otherwise let an attacker
  provision/take over a federated-identity Cognito user by claiming any
  `sub`/`email` they like.
- **Test-context consequence**: lower priority than 1-4 above, and
  deliberately ranked last, **because this one is qualitatively
  different** — it is not a hidden/accidental gap discovered by this sweep.
  It is an explicit, reasoned, project-wide architectural stance stated in
  the code itself (shared with SigV4 and SAML signature verification), not
  a special-cased "accept any X" left in one function. Reported here per
  the task brief's "report them whether or not you fix them," but this one
  reads as a **known, accepted trade-off** rather than debt — reversing it
  would be a much larger design decision (add JWKS fetching + RS256
  verification as a new capability the emulator doesn't have anywhere
  today) rather than a bug fix.
- **Effort**: large, and arguably out of scope entirely — would need to
  become a considered feature request (JWKS-fetch + verify), not a
  fail-open reversal, and the codebase's own docstring argues against it
  for the emulator's stated threat model.

### Does any sibling look like it could mask a REAL busydone bug the way the refresh-token one did?

**Yes — #2 (`ConfirmSignUp`) is the strongest candidate.** It's the exact
sibling of the already-fixed `ConfirmForgotPassword` bug (same missing
read-back-and-compare, same SES delivery path already wired, same fix
shape), and busydone almost certainly has a signup-confirmation e2e flow
that would currently pass for the wrong reason, the same way
`auth.spec.ts:567` was silently at risk before this PR. **#4 (`SECRET_HASH`)**
is the second-strongest candidate for a *silent regression* specifically —
not because it fails a test today, but because it means a MiniStack-backed
e2e run can never catch busydone accidentally breaking its own
`SECRET_HASH` computation, since nothing on the server side would reject a
wrong or missing hash. **#1 (`grant_type` fallback)** is the most severe in
isolation (zero credentials needed) but least likely to be silently
exercised by busydone's own flows, since busydone doesn't appear to send
arbitrary/malformed `grant_type` values in normal operation — it's more of
a "any fuzzing or client-library bug becomes invisible" risk than a
day-to-day masked bug.
