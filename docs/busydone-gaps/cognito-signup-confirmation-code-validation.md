# Gap: `ConfirmSignUp` accepts ANY confirmation code

## What

MiniStack's Cognito emulator (`ministack/services/cognito.py`) deliberately
accepted any value in `ConfirmationCode` on `ConfirmSignUp` and confirmed
the account unconditionally — a `# Accept any code in emulation` comment
documented this as a deliberate choice, not a missing feature. `SignUp`
already generated and stored a confirmation code on the user
(`_confirmation_code`) and emailed it via the existing SES emulation path —
`ConfirmSignUp` simply never read that stored value back. This is the exact
sibling of the already-fixed `ConfirmForgotPassword` bug
(`cognito-reset-code-validation.md`, `busydone/local-gaps` commit
`e76f837`): same missing read-back-and-compare, same SES delivery path
already wired, same fix shape. Now `ConfirmSignUp` validates the stored
code: mismatch/missing → `CodeMismatchException`, expired → new
`_SIGNUP_CODE_TTL` (24h). A rejected attempt never touches `UserStatus`.

## 🔴 Step-1 verdict: **(b) deliberately fail-open** — the exact sibling of an already-fixed gap

The code was never merely *missing* a check; it had an explicit comment
documenting the choice to skip one, at the top of `_confirm_sign_up`
(unmodified pre-fix source, `ministack/services/cognito.py:3642`):

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
function (unchanged, pre-existing line) but was **never compared to
anything** before this fix.

This was already flagged as the strongest candidate for a real masked
busydone bug by the refresh-token fix's systematic sweep
(`cognito-refresh-token-failopen.md`, finding #2) — "the exact sibling of
the already-fixed `ConfirmForgotPassword` bug, same missing
read-back-and-compare, same SES delivery path already wired". This PR fixes
that finding.

**Does `SignUp` already generate a code? Yes, already, before this
change.** `_sign_up` (~line 3562-3622) already did:

```python
user = {
    ...
    "_confirmation_code": "123456",
}
pool["_users"][username] = user
...
if "email" in attr_dict:
    resp["CodeDeliveryDetails"] = {...}
    _send_verification_email(pool, username, attr_dict, user["_confirmation_code"])
```

So generation, per-user storage, and SES delivery were **all already
wired** — only the read-back-and-compare in `ConfirmSignUp` was missing,
plus an expiry (which didn't exist at all for this code, unlike the
reset-code flow's `_RESET_CODE_TTL`). `ResendConfirmationCode`
(~line 2776-2795) reuses the same stored code (or falls back to `"123456"`
if unset) and was already wired to the same SES path too — this fix also
refreshes the expiry on resend, matching real Cognito re-issuing the code's
validity window.

**No opt-in escape hatch was added.** Same reasoning and precedent as the
two prior Cognito fail-open fixes in this file — no existing strict-mode
toggle to flip, real Cognito has no such knob, and (per the test survey
below) nothing in busydone's own test suite relies on the old
accept-any-code behavior for a *real* backend call.

## Delivery mechanism — existing SES path, no new plumbing needed

Exactly as with the reset-code fix, the code is delivered through
MiniStack's existing SES emulation. `_send_verification_email` (called by
`_sign_up` and `_resend_confirmation_code`, unchanged by this patch)
already routes every issued code through `_deliver_cognito_email` →
`ses_mod.send_internal_email` → the `_sent_emails` outbox
(`Type: "CognitoVerificationMessage"`, `Subject: "Your verification code"`),
readable via the existing `GET /_ministack/ses/messages` endpoint — which
busydone's own `/api/emails` route already proxies. Verified below: the
outbox entry for a fresh signup carries `"BodyText": "Your verification
code is 123456."` — the pre-existing fixed code value from `_sign_up`,
unchanged by this fix (the gap was "no validation," not "guessable code,"
same as the reset-code fix's note on this point).

## Why it matters / test survey (checked before choosing the fix shape)

Grepped `frontend/tests/e2e/` and `tests/e2e/` (busydone repo, read-only)
for every signup/confirm-related test before touching anything, per the
task brief:

- **`frontend/tests/e2e/local-org-lifecycle.spec.ts`** and
  **`frontend/tests/e2e/signup-all-plans.spec.ts`** both use
  `MOCK_CONFIRM_CODE = '123456'` and submit it through the UI — but both
  mock `**/api/v1/auth/confirm-signup` at the **browser network layer**
  (`page.route(...)`) before the click, so neither test ever reaches
  busydone's backend or MiniStack's Cognito. **Not affected by this fix**
  — coincidentally, even if they did reach the real backend, `'123456'` is
  the exact value MiniStack's `_sign_up` actually issues, so these would
  still pass for the right reason now, not just the wrong one.
- **`frontend/tests/e2e/signup-all-plans.spec.ts`**'s "Signup API — live
  validation & plans" suite (~line 556) *does* hit the real
  `/auth/confirm-signup` endpoint with no mocking, but only for a
  **non-existent email** (`POST /auth/confirm-signup with a bogus code for
  a non-existent email returns 400`, ~line 656) — that request short-
  circuits inside `_resolve_user`'s `UserNotFoundException` path
  (pre-existing, unchanged), before reaching the new mismatch check this
  fix adds. **Not affected.**
- **`frontend/tests/e2e/gap-pages.spec.ts`**'s `ConfirmSignupPage` suite
  exercises the confirm-code input field's client-side digit-stripping and
  disabled-button states, but **never submits** a completed form to any
  backend. **Not affected.**
- **`frontend/tests/e2e/endpoint-coverage.spec.ts`** posts an **empty
  body** to `/auth/confirm-signup` for a reachability sweep — Pydantic
  validation rejects it (422) before any Cognito call. **Not affected.**
- No `tests/e2e/*.py` (the Python/MiniStack-backed tier) test calls
  `ConfirmSignUp` at all (`grep -rn "confirm_sign_up\|ConfirmSignUp"
  tests/e2e/*.py` → zero matches).
- `tests/unit/test_api_auth_cognito.py`,
  `tests/unit/test_api_routes_auth.py`, etc. mock the boto3 `cognito-idp`
  client directly and never reach MiniStack, so they are unaffected by
  construction.

**Conclusion: no existing busydone test breaks.** The frontend UI specs
that reference a hardcoded `'123456'` never reach MiniStack's Cognito at
all (mocked at the network layer), and the two specs that do reach it for
real (`endpoint-coverage.spec.ts`'s empty body, `signup-all-plans.spec.ts`'s
non-existent-email 400) both short-circuit before this fix's new
comparison logic runs. **There is currently no positive signup-confirmation
e2e flow against a real MiniStack backend anywhere in busydone's test
suite** — the same gap already noted for `ForgotPassword` in the reset-code
fix's doc. This fix is enabling capability for such a test, same as that
one.

## Files/functions changed

`ministack/services/cognito.py` only. New `_SIGNUP_CODE_TTL = 86400`
constant (24h — see the comment in-source for why this differs from
`_RESET_CODE_TTL`'s 1h: AWS doesn't publish an exact value for either, 24h
is the widely cited default for the SignUp verification code specifically,
a separate and longer-lived code than the password-reset flow's).
`_sign_up` gains one line setting the expiry alongside the pre-existing
code generation; `_resend_confirmation_code` gains one line refreshing the
expiry (matching real Cognito re-issuing the code's validity window);
`_confirm_sign_up` gains the mismatch/expiry checks (before any
`UserStatus` mutation) plus single-use cleanup on success — structurally
identical to `_confirm_forgot_password`'s existing fix. Code/expiry are
stored as plain keys on the existing per-user dict inside `pool["_users"]`
inside `_user_pools` (`AccountRegionScopedDict`) — no new store, so
isolation and `reset()` clearing are inherited for free, same as the
reset-code fix.

`_admin_confirm_sign_up` (AdminConfirmSignUp, ~line 2708) was checked and
deliberately left unchanged — real AWS's admin-triggered confirm needs no
confirmation code at all, and MiniStack's implementation already matches
that (no code read, no code check) — same pre-existing, correct asymmetry
the reset-code fix's doc noted for `AdminResetUserPassword` reusing the
same field.

## Standalone verification

Own branch `busydone/gap-cognito-failopens-2`, own worktree/container — not
yet merged into `busydone/local-gaps`. `ministack-local:dev`, container
`ms-cognito-failopen2`, port `14598` (MiniStack's own test suite not run,
per standing instruction).

**1. Sign up a test user, then wrong code → `CodeMismatchException`:**

```
$ aws --endpoint-url http://localhost:14598 cognito-idp sign-up \
    --client-id 1Nrxle5HfCSko7w7odJld6JExq \
    --username gap2-tester@example.com --password 'OriginalPass1!' \
    --user-attributes Name=email,Value=gap2-tester@example.com
{"UserConfirmed": false, "CodeDeliveryDetails": {...}, "UserSub": "3b0fdd52-..."}

$ aws --endpoint-url http://localhost:14598 cognito-idp confirm-sign-up \
    --client-id 1Nrxle5HfCSko7w7odJld6JExq \
    --username gap2-tester@example.com --confirmation-code 000000
An error occurred (CodeMismatchException) when calling the ConfirmSignUp operation: Invalid verification code provided, please try again.
```

**2. User remains UNCONFIRMED after the rejected attempt:**

```
$ aws --endpoint-url http://localhost:14598 cognito-idp admin-get-user \
    --user-pool-id us-east-1_ZlIrOoMBr --username gap2-tester@example.com \
    --query 'UserStatus' --output text
UNCONFIRMED
```

**3. Real code, read from the SES outbox, then correct code → success, user
CONFIRMED — the critical non-regression check:**

```
$ curl -s http://localhost:14598/_ministack/ses/messages
{"messages": {"000000000000": [{
  "MessageId": "71af8b4c-...@email.amazonses.com",
  "Source": "no-reply@verificationemail.com",
  "To": ["gap2-tester@example.com"],
  "Subject": "Your verification code",
  "BodyText": "Your verification code is 123456.",
  "Type": "CognitoVerificationMessage",
  ...
}]}}

$ aws --endpoint-url http://localhost:14598 cognito-idp confirm-sign-up \
    --client-id 1Nrxle5HfCSko7w7odJld6JExq \
    --username gap2-tester@example.com --confirmation-code 123456
(200, empty body — success)

$ aws --endpoint-url http://localhost:14598 cognito-idp admin-get-user \
    --user-pool-id us-east-1_ZlIrOoMBr --username gap2-tester@example.com \
    --query 'UserStatus' --output text
CONFIRMED
```

**4. Replay of the consumed code → rejected:**

```
$ aws --endpoint-url http://localhost:14598 cognito-idp confirm-sign-up \
    --client-id 1Nrxle5HfCSko7w7odJld6JExq \
    --username gap2-tester@example.com --confirmation-code 123456
An error occurred (CodeMismatchException) when calling the ConfirmSignUp operation: Invalid verification code provided, please try again.
```

## Upstream-PR notes

Existing service modification — no issue required per `CONTRIBUTING.md`,
straight to a PR. **Tests still need writing**: `tests/test_cognito.py` was
read (not run, not modified, per standing instruction) — a real PR needs
wrong-code, correct-code + `UserStatus` transition, replay-rejection,
expiry, `ResendConfirmationCode` refreshing the expiry, and
same-pool/different-user isolation cases — same shape as the reset-code
fix's own test list. The fixed code value (`"123456"`) is pre-existing
MiniStack design, not changed here.

**🔴 Flag for the maintainer, same as the two prior fixes**: this is a
default-behavior change with no opt-in flag, not a bug fix in the "missing
check" sense — there was no existing strict-mode toggle to flip. A
maintainer may prefer this land behind an opt-in strict flag rather than as
an unconditional default change. See the "No opt-in escape hatch was
added" section above.

**Does this mask a real busydone bug?** Per the test survey above: not
today, because nothing in busydone's suite reaches this path for real with
a non-trivial code. But it is the same *class* of risk the reset-code fix's
doc flagged for `ForgotPassword` — a future signup-confirmation e2e test
written against MiniStack (now enabled by this fix, since the SES delivery
path was already there) would previously have passed for the wrong reason
against any code, silently.
