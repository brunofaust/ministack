# Gap: TOTP (`SOFTWARE_TOKEN_MFA`) accepts any code; `SECRET_HASH` is never validated

## What

MiniStack's Cognito emulator (`ministack/services/cognito.py`) had two
unrelated fail-opens, tracked together in Linear ticket BD-428 because they
were filed together, not because they share a fix:

1. **TOTP / `SOFTWARE_TOKEN_MFA`** — three sites accepted any code
   unconditionally, each with a `# Accept any TOTP code in emulator`
   comment documenting it as a deliberate choice:
   - `_admin_respond_to_auth_challenge`'s `SOFTWARE_TOKEN_MFA` branch
     (pre-fix line 3174)
   - `_respond_to_auth_challenge`'s combined `("SOFTWARE_TOKEN_MFA",
     "MFA_SETUP")` branch (pre-fix line 3502)
   - `_verify_software_token` (`VerifySoftwareToken`, pre-fix line 4207) —
     `user_code = data.get("UserCode", "")  # accepted regardless of value
     in emulator`
2. **`SECRET_HASH`** — `grep -n "SECRET_HASH" ministack/services/cognito.py`
   returned zero matches before this fix. The parameter (real name
   `SecretHash`) was never even read, let alone checked against an app
   client's secret, on any of the five operations whose API model carries
   it: `SignUp`, `ConfirmSignUp`, `ForgotPassword`,
   `ConfirmForgotPassword`, `ResendConfirmationCode`.

## 🔴 Step-1 verdict, per item

**(a) TOTP — (b) deliberately fail-open**, same as the four prior Cognito
fixes on this branch: explicit `# Accept any TOTP code` comments document
the choice, not an oversight.

**(b) SECRET_HASH — (a) genuinely missing**, not deliberately skipped: there
is no comment anywhere near a `SecretHash`-shaped read, because there is no
`SecretHash`-shaped read at all. The parameter was never wired into the
request-parsing layer for any of the five operations that carry it.

## Line numbers: corrected during implementation

The ticket's own numbers (3174 / 3502 / 4207) were re-verified against
`busydone/local-gaps` before starting and were exact. They moved again
during this fix as earlier edits (the SECRET_HASH helper, the TOTP
algorithm helpers) were inserted above them in the file — by the time of
the commit the two `RespondToAuthChallenge` sites are at approximately
3274 (`_admin_respond_to_auth_challenge`, `SOFTWARE_TOKEN_MFA` branch) and
3609 (`_respond_to_auth_challenge`, combined branch); `VerifySoftwareToken`
is at approximately 4321. Cite the function names
(`_admin_respond_to_auth_challenge`, `_respond_to_auth_challenge`,
`_verify_software_token`, `_associate_software_token`), not raw line
numbers, in any future reference to this fix — they will drift again.

## Fix shape: real validation, not a fail-closed stub — with one named exception

Both gaps get **real, implemented validation**, not just a rejection stub,
because both are well-defined, bounded algorithms with no ambiguity about
what "correct" means:

- **SECRET_HASH**: `_verify_secret_hash()` computes
  `base64(HMAC-SHA256(client_secret, username + client_id))` — the exact,
  publicly documented algorithm real Cognito uses — and compares it
  (`hmac.compare_digest`) against the caller-supplied `SecretHash`. Only
  enforced when the resolved app client actually has a `ClientSecret`
  (`GenerateSecret=true`); a secret-less client (busydone's own real app
  client) is unaffected, matching real Cognito's behavior of never
  requiring `SecretHash` from a client with no secret.
- **TOTP**: `_totp_code()` / `_totp_code_matches()` implement RFC 6238
  (HOTP over a 30-second time counter, HMAC-SHA1, 6 digits, ±1 step / ±30s
  clock-drift tolerance) — the same algorithm every real authenticator app
  and real Cognito use, no vendor extension. `_associate_software_token`
  now actually **persists** the generated secret against the resolved user
  (keyed by `AccessToken`) instead of generating-and-discarding it, which
  is what made every downstream check accept-anything even if it had
  bothered to compare — there was never a stored value to compare against.

**One named, deliberate fail-closed exception, not full coverage:** real
`AssociateSoftwareToken`/`VerifySoftwareToken` also accept a `Session`
(the unauthenticated MFA_SETUP-challenge enrollment flow, used before an
`AccessToken` exists — e.g. a pool with `MfaConfiguration=ON` forcing
enrollment at first sign-in). That path is **not implemented**: no secret
is ever persisted for a `Session`-only `AssociateSoftwareToken` call, so a
`Session`-only `VerifySoftwareToken` call has nothing to check against and
always falls through to `Status: ERROR` — refusing rather than silently
accepting, but the enrollment flow itself doesn't work end-to-end over
`Session` alone. This is documented in both functions' docstrings with a
🔴 marker. Implementing it would mean adding a second secret-storage path
keyed by the opaque challenge-session token (a new piece of state, a new
`reset()` entry, and session-token continuity across
Associate→Verify→RespondToAuthChallenge) for a flow busydone's own client
(Amplify, `AccessToken`-based "enable MFA from account settings") doesn't
use — exactly the kind of scope growth the task brief asked to avoid.
**Flagged as a known remaining gap, not silently shipped as if complete.**

A second, smaller consequence of the same real-validation choice:
`_admin_respond_to_auth_challenge`'s separate `MFA_SETUP` branch (not
individually named in BD-428, but the sibling of the `MFA_SETUP` half of
the already-named combined branch at the other `RespondToAuthChallenge`
site) now requires `"SOFTWARE_TOKEN_MFA" in user["_mfa_enabled"]` before
finalizing — i.e. a caller can no longer skip straight to
`RespondToAuthChallenge(MFA_SETUP)` without ever completing a successful
`VerifySoftwareToken`. Left unfixed, this would have been a fourth
fail-open of the same shape that the ticket's grep-based line count simply
didn't include (it isn't a `# Accept any TOTP code` comment, it's the
absence of any check at all). Real Cognito's `MFA_SETUP` challenge
response carries no code of its own — the code check happens in
`VerifySoftwareToken` — so this branch checks enrollment state, not a
per-request code, matching that shape.

## Why `VerifySoftwareToken` returns `Status: "ERROR"`, not an exception

Verified against botocore's `cognito-idp` `2016-04-18` API model
(`VerifySoftwareTokenResponseType`): the response has a dedicated
`Status: SUCCESS | ERROR` enum field — this operation is modeled to signal
a bad code via its response body, not (only) via an exception. Using that
field is the more API-accurate choice than guessing an exception name for
an operation whose contract already has a purpose-built field for exactly
this. `RespondToAuthChallenge`/`AdminRespondToAuthChallenge` have no such
field (only `AuthenticationResult` on success), so those two sites use
`CodeMismatchException` — confirmed present in both operations' modeled
`errors` list, and consistent with the exception this fork's three prior
Cognito fixes already use for every other "wrong code" case in this file.
`SECRET_HASH` mismatches use `NotAuthorizedException`
("Unable to verify secret hash for client `<id>`") — the standard,
widely-documented real Cognito message for this exact failure.

## Delivery mechanism / new state — no new persistent store

No new top-level dict was added, so `reset()` (`ministack/services/
cognito.py`) needed no new line to clear. Both fixes store their state on
structures that already exist and are already covered by `reset()`
transitively:

- `SECRET_HASH`: reads `client["ClientSecret"]` — pre-existing field, no
  write at all.
- `TOTP`: writes `user["_totp_secret"]` — a new key on the existing
  per-user dict inside `pool["_users"]` inside `_user_pools`
  (`AccountRegionScopedDict`), the same storage pattern the three prior
  Cognito fixes on this branch already used for `_confirmation_code` /
  `_reset_code`. Isolation and `reset()` clearing are inherited for free.

## Why it matters / test survey (checked before touching anything)

Grepped `frontend/tests/e2e/`, `tests/e2e/`, and `tests/unit/` (busydone
repo, read-only) for MFA / TOTP / SecretHash / SoftwareToken /
GenerateSecret before implementing, per the task brief and this fork's
standing practice:

- `grep -rn "SOFTWARE_TOKEN_MFA\|VerifySoftwareToken\|AssociateSoftwareToken\|TOTP" frontend/tests tests/e2e tests/unit`
  → **zero matches**. busydone's own test suite exercises neither MFA
  challenge flow today.
- `grep -rn "generate_secret\|GenerateSecret\|SecretHash\|SECRET_HASH" frontend/ src/ infra/`
  → the app client is provisioned with `generate_secret = false`
  (Terraform), and no code path reads or sends a `SecretHash`. Confirmed
  by the companion ticket referenced in BD-428 ("Remove dead Cognito
  SECRET_HASH code").

**Conclusion: no existing busydone test exercises either code path, so
none can regress.** Both fixes are pure fidelity improvements matching the
"impact is low today, matters if either is ever enabled" framing in
BD-428 — this doc (and the passing `_verify_secret_hash`/
`_totp_code_matches` helpers) are what makes a *future* MFA or
client-secret e2e test against MiniStack trustworthy, the same "enabling
capability" framing the `ConfirmSignUp` fix used for signup-confirmation
e2e tests.

## Files/functions changed

`ministack/services/cognito.py` only. New constants
`_TOTP_STEP_SECONDS`/`_TOTP_DIGITS`/`_TOTP_WINDOW`. New helpers
`_verify_secret_hash`, `_totp_code`, `_totp_code_matches`,
`_verify_software_token_mfa_code`, `_require_software_token_enrolled`.
Five SECRET_HASH call sites (`_resend_confirmation_code`, `_sign_up`,
`_confirm_sign_up`, `_forgot_password`, `_confirm_forgot_password`) each
gain one early-return check right after client/pool resolution, before any
other work — matching real Cognito's behavior of rejecting an untrusted
caller before revealing anything else (user-exists, code-validity, etc.).
`_associate_software_token` and `_verify_software_token` gain real
secret persistence/comparison. The two `RespondToAuthChallenge` sites'
`SOFTWARE_TOKEN_MFA`/`MFA_SETUP` branches call the two new challenge
helpers instead of unconditionally succeeding.

**Not changed / explicitly out of scope:**
`AuthParameters`/`ChallengeResponses`-embedded `SECRET_HASH` (used by
`InitiateAuth`'s `USER_PASSWORD_AUTH`/`REFRESH_TOKEN_AUTH` and
`RespondToAuthChallenge`'s `PASSWORD_VERIFIER` flows) is a *different*
mechanism from the five modeled `SecretHash` fields fixed here — a
free-form map key, not a typed API parameter — and was not touched. Adding
it would mean threading the same check through `InitiateAuth`'s several
auth-flow branches and `RespondToAuthChallenge`'s several challenge
branches, which is exactly the "don't balloon" scope growth the task
brief warned against, for a mechanism busydone's `generate_secret=false`
client never exercises either way. **Flagged as a known remaining gap.**
The `Session`-based TOTP enrollment path is flagged the same way above.

## Standalone verification

Own branch `busydone/gap-cognito-mfa-secrethash`, own worktree
(`~/Repos/ministack-wt/gap-cognito-mfa-secrethash`), own image tag
`ministack-cognito-mfa-check:dev`, own container `ms-cognito-mfa-check`,
port `14599` — `ministack-local:dev` and every `busydone-local-*`
container were never touched (confirmed via `docker images`/`docker ps`
before and after). Container stopped and removed after verification.
MiniStack's own test suite not run, per standing instruction.

**SECRET_HASH — 1. Missing hash on a secret-bearing client → rejected:**

```
$ aws --endpoint-url http://localhost:14599 cognito-idp sign-up \
    --client-id 2ijXdOJIy0zgYg2vo1Ou29vf7C \
    --username secrethash-tester@example.com --password 'OriginalPass1!' \
    --user-attributes Name=email,Value=secrethash-tester@example.com
An error occurred (NotAuthorizedException) when calling the SignUp operation: Unable to verify secret hash for client 2ijXdOJIy0zgYg2vo1Ou29vf7C
```

**SECRET_HASH — 2. No-secret client (busydone's real config) needs no hash
→ succeeds — the critical non-regression check:**

```
$ aws --endpoint-url http://localhost:14599 cognito-idp sign-up \
    --client-id ijmXIpQ0tpgu2xzCZrs7YRCcRz \
    --username plain-tester@example.com --password 'OriginalPass1!' \
    --user-attributes Name=email,Value=plain-tester@example.com
{
    "UserConfirmed": false,
    "CodeDeliveryDetails": {"Destination": "plain-tester@example.com", "DeliveryMedium": "EMAIL", "AttributeName": "email"},
    "UserSub": "1145bbf1-25c9-4459-a30c-e0eb45f855ff"
}
```

**SECRET_HASH — 3. Correct hash on the secret-bearing client → succeeds:**

```
$ HASH=$(python3 -c "import hmac,hashlib,base64; print(base64.b64encode(hmac.new(b'<client_secret>', b'secrethash-tester@example.com2ijXdOJIy0zgYg2vo1Ou29vf7C', hashlib.sha256).digest()).decode())")
$ aws --endpoint-url http://localhost:14599 cognito-idp sign-up \
    --client-id 2ijXdOJIy0zgYg2vo1Ou29vf7C \
    --username secrethash-tester@example.com --password 'OriginalPass1!' \
    --secret-hash "$HASH" \
    --user-attributes Name=email,Value=secrethash-tester@example.com
{
    "UserConfirmed": false,
    "CodeDeliveryDetails": {"Destination": "secrethash-tester@example.com", "DeliveryMedium": "EMAIL", "AttributeName": "email"},
    "UserSub": "0222dc8e-c8f3-4e13-8e00-f7f6cef21e18"
}
```

**SECRET_HASH — 4. Wrong hash on the secret-bearing client → rejected:**

```
$ aws --endpoint-url http://localhost:14599 cognito-idp sign-up \
    --client-id 2ijXdOJIy0zgYg2vo1Ou29vf7C \
    --username another-user@example.com --password 'OriginalPass1!' \
    --secret-hash "bm90dGhlcmVhbGhhc2g=" \
    --user-attributes Name=email,Value=another-user@example.com
An error occurred (NotAuthorizedException) when calling the SignUp operation: Unable to verify secret hash for client 2ijXdOJIy0zgYg2vo1Ou29vf7C
```

**TOTP — 5. `VerifySoftwareToken` with a wrong code → `Status: ERROR`:**

```
$ aws --endpoint-url http://localhost:14599 cognito-idp associate-software-token --access-token "$ACCESS_TOKEN"
{"SecretCode": "C3C7KBY4X3XM6ZZ3H7WEJ2UCKBNSJYQD", "Session": "..."}

$ aws --endpoint-url http://localhost:14599 cognito-idp verify-software-token --access-token "$ACCESS_TOKEN" --user-code "000000"
{
    "Status": "ERROR"
}
```

**TOTP — 6. `VerifySoftwareToken` with the REAL RFC 6238 code computed
independently from the returned secret → `Status: SUCCESS` — the critical
non-regression check that the happy path still works:**

```
$ CODE=$(python3 -c "<RFC 6238 TOTP over SecretCode at current time>")   # → 877738
$ aws --endpoint-url http://localhost:14599 cognito-idp verify-software-token --access-token "$ACCESS_TOKEN" --user-code "877738" --friendly-device-name "test-device"
{
    "Status": "SUCCESS"
}
```

**TOTP — 7. Sign-in `RespondToAuthChallenge(SOFTWARE_TOKEN_MFA)` with a
wrong code → `CodeMismatchException`** (pool set to
`MfaConfiguration=OPTIONAL`, `SoftwareTokenMfaConfiguration.Enabled=true`,
user already TOTP-enrolled via step 6 above):

```
$ aws --endpoint-url http://localhost:14599 cognito-idp admin-initiate-auth \
    --user-pool-id us-east-1_QnuqciVOF --client-id ijmXIpQ0tpgu2xzCZrs7YRCcRz \
    --auth-flow ADMIN_USER_PASSWORD_AUTH \
    --auth-parameters USERNAME=plain-tester@example.com,PASSWORD='OriginalPass1!'
{"ChallengeName": "SOFTWARE_TOKEN_MFA", "Session": "...", "ChallengeParameters": {...}}

$ aws --endpoint-url http://localhost:14599 cognito-idp admin-respond-to-auth-challenge \
    --user-pool-id us-east-1_QnuqciVOF --client-id ijmXIpQ0tpgu2xzCZrs7YRCcRz \
    --challenge-name SOFTWARE_TOKEN_MFA --session "$SESSION" \
    --challenge-responses USERNAME=plain-tester@example.com,SOFTWARE_TOKEN_MFA_CODE=111111
An error occurred (CodeMismatchException) when calling the AdminRespondToAuthChallenge operation: Invalid code received for verification.
```

**TOTP — 8. Same challenge with the REAL current TOTP code → succeeds
with full `AuthenticationResult` tokens — the critical non-regression
check for the actual sign-in-with-MFA path:**

```
$ CODE=$(python3 -c "<RFC 6238 TOTP over the same secret at current time>")   # → 569832
$ aws --endpoint-url http://localhost:14599 cognito-idp admin-respond-to-auth-challenge \
    --user-pool-id us-east-1_QnuqciVOF --client-id ijmXIpQ0tpgu2xzCZrs7YRCcRz \
    --challenge-name SOFTWARE_TOKEN_MFA --session "$SESSION" \
    --challenge-responses USERNAME=plain-tester@example.com,SOFTWARE_TOKEN_MFA_CODE=569832
→ AuthenticationResult keys: ['AccessToken', 'ExpiresIn', 'TokenType', 'RefreshToken', 'IdToken']
```

## Upstream-PR notes

Existing service modification — no issue required per `CONTRIBUTING.md`,
straight to a PR. **Tests still need writing**: `tests/test_cognito.py`
was read (not run, not modified, per standing instruction) — a real PR
needs, at minimum: SECRET_HASH missing/wrong/correct for each of the five
operations, a no-secret client's non-regression case, TOTP wrong/correct
code for both `VerifySoftwareToken` and both `RespondToAuthChallenge`
sites, the `MFA_SETUP`-without-`VerifySoftwareToken` rejection case, and
clock-drift-window boundary cases (`±1` step accepted, `±2` rejected).

**🔴 Flag for the maintainer, same as the four prior fixes**: SECRET_HASH
enforcement is a default-behavior change with no opt-in flag — any
existing consumer relying on a secret-bearing client that never sent
`SecretHash` (previously silently accepted) will now be rejected. No
existing strict-mode toggle exists to flip; real Cognito has none either.
TOTP enforcement is lower-risk by comparison, since accepting "any code"
was never a *usable* default (a caller couldn't have derived a working
value from anything public) — but flagged for completeness.

**🔴 Flag for the maintainer, new to this fix**: the `Session`-based TOTP
enrollment path (unauthenticated `MFA_SETUP`-challenge
Associate/VerifySoftwareToken) and the `AuthParameters`/
`ChallengeResponses`-embedded `SECRET_HASH` mechanism are both explicitly
NOT implemented — see "Not changed / explicitly out of scope" above. A
real upstream PR should either implement them or carry this same
disclosure so reviewers don't assume full coverage from the headline
"fixes TOTP and SECRET_HASH fail-opens."

**Does this mask a real busydone bug?** No — per the test survey above,
busydone's own test suite exercises neither code path today, and its real
app client is `GenerateSecret=false` with no MFA configured anywhere. Pure
fidelity gap, exactly as BD-428 characterized it.
