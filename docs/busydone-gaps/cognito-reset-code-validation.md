# Gap: `ConfirmForgotPassword` accepts ANY verification code

## What

MiniStack's Cognito emulator (`ministack/services/cognito.py`) deliberately
accepted any value in `ConfirmationCode` on `ConfirmForgotPassword` and reset
the password unconditionally. `ForgotPassword`/`AdminResetUserPassword`
already generated a code, stored it on the user, and emailed it via SES —
`ConfirmForgotPassword` simply never read that stored value back. This
change makes `ConfirmForgotPassword` validate the code it was actually
issued, matching real Cognito's `CodeMismatchException`/`ExpiredCodeException`
contract.

## 🔴 Step-1 verdict: **(b) deliberately fail-open** — not (a) unimplemented

The code was never merely *missing* a check; it had an explicit comment
documenting the choice to skip one, at the top of `_confirm_forgot_password`
(unmodified `v1.4.17` / pre-fix source, `ministack/services/cognito.py:3685`):

```python
# Accept any confirmation code in emulation (real AWS validates against issued code)
pw_err = _validate_password(pool, new_password)
```

This is the same class of deliberate fail-open MiniStack already uses
elsewhere (e.g. `MINISTACK_COGNITO_PRETOKEN_STRICT`'s documented "a Lambda
error is logged and the unmodified token is still issued" default) — but
unlike that flag, there was **no config knob** here at all; acceptance was
unconditional. **This changes default behavior, not just adds a strict
mode** — flagged explicitly for upstream in the PR notes below, since a
maintainer may prefer this land behind a flag instead of as the new default.

**Does `ForgotPassword` generate a code today? Yes, already, before this
change.** `_forgot_password` (and the admin-triggered `_admin_reset_user_password`)
already did:

```python
code = "654321"
user["_reset_code"] = code
attrs = _attr_list_to_dict(user.get("Attributes", []))
_send_verification_email(pool, username, attrs, code, attribute_name="password")
```

So generation, per-user storage, and SES delivery were **all already wired**
— only the read-back-and-compare in `ConfirmForgotPassword` was missing (plus
an expiry, which didn't exist at all). This made the fix much narrower than
"build a delivery mechanism": the delivery mechanism already existed and
needed no changes.

## Step 2 — code delivery: existing SES path, no new plumbing (owner directive)

Per explicit owner instruction: deliver the code through MiniStack's existing
SES emulation, exactly as real Cognito does — not a fixed/documented code, not
a bespoke admin endpoint, not a log line.

**This was already the case.** `_send_verification_email` (called by both
`_forgot_password` and `_admin_reset_user_password`, unchanged by this patch)
already routed every issued code through:

```
_send_verification_email → _deliver_cognito_email → ses_mod.send_internal_email → _sent_emails (AccountRegionScopedDict outbox)
```

**Exact email shape that lands in the outbox** (verified below,
`GET /_ministack/ses/messages`):

```json
{
  "MessageId": "<uuid>@email.amazonses.com",
  "Source": "no-reply@verificationemail.com",
  "To": ["gap-tester@example.com"],
  "CC": [], "BCC": [],
  "Subject": "Your verification code",
  "BodyText": "Your verification code is 654321.",
  "BodyHtml": "",
  "Timestamp": 1786817373.575,
  "Type": "CognitoVerificationMessage"
}
```

`Type: "CognitoVerificationMessage"` + `Subject: "Your verification code"`
uniquely identifies this message class in the outbox (signup verification
also uses `CognitoVerificationMessage` but a different `AttributeName` extra
— `email` vs `password` — recorded on the stored record though **not**
surfaced by the `/_ministack/ses/messages` projection, which only returns
`MessageId/Source/To/CC/BCC/Subject/BodyText/BodyHtml/Timestamp/Type`). A
future busydone test can `GET /_ministack/ses/messages`, filter by `To`
contains the target email + `Subject == "Your verification code"`, and
regex the 6-digit code out of `BodyText` (`\d{6}`).

**Busydone's own `/api/emails` route already proxies this endpoint**
(`tests/e2e/CLAUDE.md`), so a positive end-to-end reset flow is testable for
free with no MiniStack-side plumbing left to build.

**What busydone's tests actually need today (checked before choosing):**
grepped `frontend/tests/e2e/` and `tests/e2e/` — there is **no positive
reset-password flow anywhere**. `auth.spec.ts:618`
(`'reset-password with a bogus verification code returns 400'`) is the only
test that reaches real Cognito, and it is purely negative — it never
requests or consumes a real code. The `page.route(...)`-intercepted UI tests
(`auth.spec.ts:136-208`) mock the API entirely and never reach Cognito. So
**validation alone is sufficient to fix the failure cascade**; the SES
delivery path is enabling capability for a *future* positive test, not
something anything consumes on day one — implemented anyway per the owner's
explicit directive and because it was already ~95% built.

## Verbatim evidence of the gap (unmodified `v1.4.17`, pre-fix)

Real-world measured impact (from the task brief, reproduced against
`ministack-local:dev` on a full Playwright run):

- `frontend/tests/e2e/auth.spec.ts:618` posts `code: '000000'` against a real
  user and asserts `400` — MiniStack returned `200` and actually changed
  `bruno.faust+smoke1@gmail.com`'s password.
- ~150 of 169 Playwright failures cascaded from this single mechanism (org 1's
  demo user, used by most specs, got its password silently changed to a
  value nothing else in the suite knew).
- Real AWS Cognito rejects a non-matching code with `CodeMismatchException`
  ("Invalid verification code provided, please try again.").

This is a pure **emulator-fidelity divergence, not a flaky test**: the same
Playwright spec passes against real dev Cognito and fails only against
MiniStack — anyone running the suite locally saw green today and would never
learn local diverges from prod on this exact behavior.

## Wire shapes (source of truth: botocore, read-only)

Read from the busydone repo's installed venv (never modified):
`/Users/bfaust/Repos/busydone/.venv/lib/python3.14/site-packages/botocore/data/cognito-idp/2016-04-18/service-2.json.gz`.

```json
"CodeMismatchException": {
  "type": "structure",
  "members": {"message": {"shape": "MessageType"}},
  "documentation": "<p>This exception is thrown if the provided code doesn't match what the server was expecting.</p>",
  "exception": true
},
"ExpiredCodeException": {
  "type": "structure",
  "members": {"message": {"shape": "MessageType"}},
  "documentation": "<p>This exception is thrown if a code has expired.</p>",
  "exception": true
}
```

Neither shape declares a custom `error.httpStatusCode` — both fall to the
default `400`, matching every other client exception `cognito.py` already
raises via `error_response_json(code, message, status=400)`.

## Files / functions changed

`ministack/services/cognito.py` — **27 insertions, 1 deletion**, one file:

- New module constant `_RESET_CODE_TTL = 3600` (1 hour — AWS documents no
  exact value; reuses `_CHALLENGE_SESSION_TTL`'s own fallback rather than
  inventing a second number), placed next to the file's existing TTL
  constants (`_AUTH_CODE_TTL`, `_NEW_PASSWORD_SESSION_TTL`, `_USER_AUTH_SESSION_TTL`).
- `_forgot_password` / `_admin_reset_user_password`: each now also sets
  `user["_reset_code_expires_at"] = time.time() + _RESET_CODE_TTL` alongside
  the pre-existing `user["_reset_code"] = code` assignment. No change to
  code generation, storage key, or SES delivery — those already existed.
- `_confirm_forgot_password`: reads `data.get("ConfirmationCode", "")`;
  before touching the password, compares it against `user.get("_reset_code")`
  → `CodeMismatchException` on missing/mismatched code (**before** any
  password validation runs, so a rejected attempt never touches
  `user["_password"]` or `UserStatus`); then checks
  `time.time() > user.get("_reset_code_expires_at", 0)` →
  `ExpiredCodeException`. On success, pops both `_reset_code` and
  `_reset_code_expires_at` off the user — the code is single-use, matching
  real Cognito (verified below).

## Storage / multi-tenancy

`_reset_code` / `_reset_code_expires_at` are plain keys on the **user dict
itself** — the same dict `_admin_reset_user_password`/`_forgot_password`
already mutated for `UserStatus`/`UserLastModifiedDate`. That user dict lives
inside `pool["_users"]`, and `pool` lives inside `_user_pools`
(`AccountRegionScopedDict`, unchanged). No new module-level store was
introduced, so:

- the code inherits per-account/per-region pool isolation for free, and
- `reset()`'s existing `_user_pools.clear()` clears every outstanding reset
  code too — no new call needed in `reset()`.

**Measured caveat, not a bug in this patch:** `ForgotPassword` and
`ConfirmForgotPassword` are **unsigned** requests in botocore
(`context.auth_type == "none"` in the `--debug` trace below) — this matches
real AWS, where these two operations are genuinely public/unauthenticated.
Because no `Authorization` header is sent, `AccountRegionScopedDict`'s
account-scoping (which reads the SigV4 credential) cannot distinguish two
different signed AWS "accounts" through *these two operations specifically*
— they always resolve into whatever `get_account_id()` falls back to with no
credential present. This is pre-existing behavior of the whole pool
(`InitiateAuth`/`SignUp`/`ConfirmSignUp` have the same property) and nothing
in this patch changed it. The isolation this patch actually needs — and the
one verified below — is **per-pool/per-user**: a code minted for one user is
never accepted for a different user, even a neighbor in the exact same pool
who currently also has a valid pending code with the identical value.

## Verbatim verification

Built and ran standalone, own container/image, port **14595**:

```
$ docker build --build-arg MINISTACK_VERSION=v1.4.17 -t ministack-local:dev .
[...]
naming to docker.io/library/ministack-local:dev done

$ docker run -d --name ms-cognito-reset -p 14595:4566 -e SERVICES=cognito-idp,ses -e COGNITO_EMAIL_ENABLED=true ministack-local:dev
```

**1. `forgot-password` succeeds and the code is obtainable via the SES outbox:**

```
$ aws --endpoint-url=http://localhost:14595 cognito-idp forgot-password \
    --client-id PjEdztXziKPjQo4HUsdqdlzE7T --username gap-tester@example.com
{
    "CodeDeliveryDetails": {"Destination": "gap-tester@example.com", "DeliveryMedium": "EMAIL", "AttributeName": "email"}
}

$ curl -s http://localhost:14595/_ministack/ses/messages
{
    "messages": {"000000000000": [{
        "MessageId": "67153b78-b1f3-4b7a-9aa4-fa42915187aa@email.amazonses.com",
        "Source": "no-reply@verificationemail.com",
        "To": ["gap-tester@example.com"], "CC": [], "BCC": [],
        "Subject": "Your verification code",
        "BodyText": "Your verification code is 654321.",
        "BodyHtml": "", "Timestamp": 1786817373.575,
        "Type": "CognitoVerificationMessage"
    }]}
}
```

**2. Wrong code → `CodeMismatchException`** (the exact assertion
`auth.spec.ts:618` needs):

```
$ aws --endpoint-url=http://localhost:14595 cognito-idp confirm-forgot-password \
    --client-id PjEdztXziKPjQo4HUsdqdlzE7T --username gap-tester@example.com \
    --confirmation-code 000000 --password DoesNotMatter1!

An error occurred (CodeMismatchException) when calling the ConfirmForgotPassword operation: Invalid verification code provided, please try again.
```

**3. The specific property that caused the 150-failure cascade — a rejected
attempt leaves the password unchanged:**

```
$ aws --endpoint-url=http://localhost:14595 cognito-idp initiate-auth --auth-flow USER_PASSWORD_AUTH \
    --client-id PjEdztXziKPjQo4HUsdqdlzE7T \
    --auth-parameters USERNAME=gap-tester@example.com,PASSWORD=OriginalPass1! \
    --query 'AuthenticationResult.TokenType' --output text
Bearer          # old password still works

$ aws --endpoint-url=http://localhost:14595 cognito-idp initiate-auth --auth-flow USER_PASSWORD_AUTH \
    --client-id PjEdztXziKPjQo4HUsdqdlzE7T \
    --auth-parameters USERNAME=gap-tester@example.com,PASSWORD=DoesNotMatter1! \
    --query 'AuthenticationResult.TokenType' --output text

An error occurred (NotAuthorizedException) when calling the InitiateAuth operation: Incorrect username or password.
```

**4. Correct code succeeds, and the new password actually works:**

```
$ aws --endpoint-url=http://localhost:14595 cognito-idp confirm-forgot-password \
    --client-id PjEdztXziKPjQo4HUsdqdlzE7T --username gap-tester@example.com \
    --confirmation-code 654321 --password NewValidPass1!
(no output = success, HTTP 200)

$ aws --endpoint-url=http://localhost:14595 cognito-idp initiate-auth --auth-flow USER_PASSWORD_AUTH \
    --client-id PjEdztXziKPjQo4HUsdqdlzE7T \
    --auth-parameters USERNAME=gap-tester@example.com,PASSWORD=NewValidPass1! \
    --query 'AuthenticationResult.TokenType' --output text
Bearer
```

Also verified: the code is **single-use** — replaying `654321` after the
successful confirm above returns `CodeMismatchException` (the code was
popped off the user on success).

**5. Tenant isolation** — verified at the meaningful boundary for these two
unsigned operations (per-pool/per-user; see the caveat above for why a
literal cross-"AWS-account" test doesn't exercise anything through these
specific unsigned ops). Two users in the **same** pool: `gap-tester-c` has a
currently-valid pending code (`654321`); `gap-tester-d` (same pool, same
client) never called `forgot-password` at all:

```
$ aws --endpoint-url=http://localhost:14595 cognito-idp confirm-forgot-password \
    --client-id Ozt9V7ABl8zjOz2swPiWfatw3C --username gap-tester-d@example.com \
    --confirmation-code 654321 --password CrossUserPass1!

An error occurred (CodeMismatchException) when calling the ConfirmForgotPassword operation: Invalid verification code provided, please try again.

$ aws --endpoint-url=http://localhost:14595 cognito-idp confirm-forgot-password \
    --client-id Ozt9V7ABl8zjOz2swPiWfatw3C --username gap-tester-c@example.com \
    --confirmation-code 654321 --password NewPassC1!
(no output = success)   # the SAME code value correctly succeeds for the user who actually requested it
```

This proves the check reads `_reset_code` off the **specific resolved user**
object, not a pool-level or module-level flag — cross-user leakage is
structurally impossible regardless of the account-scoping caveat above.

Container `ms-cognito-reset` stopped and removed after verification
(`docker stop ms-cognito-reset && docker rm ms-cognito-reset`); confirmed no
`ms-cognito-reset*` containers remain (`docker ps -a --filter name=ms-cognito-reset`
returns empty).

## Upstream-PR notes

- **🔴 This changes DEFAULT behavior, not just adds a strict mode.**
  Step 1 found this was a *deliberate* fail-open (explicit comment, not a
  missing check) — unlike `MINISTACK_COGNITO_PRETOKEN_STRICT`, there was no
  existing config flag gating it. Flag it explicitly in the PR description:
  a maintainer may want this gated behind an opt-in strict flag (defaulting
  to the new, correct behavior) rather than an unconditional behavior change,
  for backward compatibility with anyone who was relying on the old
  accept-anything shortcut in their own tests.
- Existing service modification (`ministack/services/cognito.py`), not a new
  service — per `CONTRIBUTING.md` this does not require an issue filed
  first and can go straight to a PR.
- **Tests still need writing before upstream submission** — `tests/test_cognito.py`
  was read but not run (per the standing instruction not to run MiniStack's
  own test suite) and was not modified; a real PR needs, at minimum: wrong
  code → `CodeMismatchException`, correct code → success + login with new
  password, replay of a consumed code → `CodeMismatchException`, expired
  code → `ExpiredCodeException` (needs a clock-patch or a configurable TTL
  for the test), and the same-pool/different-user isolation case reproduced
  above.
- The fixed code value (`"654321"`) was **not** changed — that's pre-existing
  MiniStack design (shared with `AdminResetUserPassword`, and with
  `"123456"` for `ResendConfirmationCode`), not something introduced or
  altered by this patch. Randomizing it was out of scope: the gap was
  "no validation happens," not "the code is guessable," and changing
  generation would have widened the diff into an unrelated design decision.
- `_admin_reset_user_password` mutates the same `_reset_code` fields as
  `_forgot_password` — this is pre-existing MiniStack design (not
  changed here) that doesn't fully mirror real AWS (real
  `AdminResetUserPassword` does not require a `ConfirmForgotPassword` code
  at all; it forces `RESET_REQUIRED`/`NEW_PASSWORD_REQUIRED` on next sign-in
  instead). Left as-is — reworking that admin flow is a separate, larger
  design question outside this gap's scope.
