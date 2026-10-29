> Archived document
>
> This file is retained for historical context and is not an authoritative
> operational guide for the current repository state.
> Status: Non-authoritative historical record.
> Operational status: DO NOT USE
> Original path: PHASE2_WEBAUTHN_POLICY.md
> Archived on: 2026-10-29
> Superseded by: docs/SECURITY_BOUNDARIES.md
> Reason: Consolidated into current security boundaries and approval flow documentation
> Warning: Do not execute commands in this archived document.

# Phase 2 WebAuthn & User Verification Policy Specification

**Implementation**: `src/identity/webauthn.py`, `src/identity/policy.py`
**Standard**: W3C WebAuthn Level 3 / FIDO2
**Pinned Library**: `webauthn` (Python)

---

## 1. WebAuthn User Verification (UV) Standard (Finding R1)

In compliance with Finding R1, all privileged Commissioner operations require authentic WebAuthn User Verification (biometrics or device PIN verified locally on the authenticator):

### Challenge Generation Policy:
- Options are generated using:
  ```python
  options = _webauthn_generate_auth_options(
      rp_id=self.config.rp_id,
      challenge=raw_challenge_bytes,
      allow_credentials=allow_descriptors,
      user_verification=UserVerificationRequirement.REQUIRED,
      timeout=60000,
  )
  ```
- `UserVerificationRequirement.REQUIRED` is strictly mandated. `PREFERRED` or `DISCOURAGED` are prohibited for decision challenges.

### Assertion Verification Policy:
- Assertion responses are verified using:
  ```python
  verified = _webauthn_verify_auth_response(
      credential=response_payload,
      expected_challenge=expected_challenge_bytes,
      expected_rp_id=self.config.rp_id,
      expected_origin=list(self.config.allowed_origins),
      credential_public_key=pub_key,
      credential_current_sign_count=current_counter,
      require_user_verification=True,
  )
  ```
- Verification explicitly checks:
  ```python
  if verified.user_verified is not True:
      raise InvalidAuthenticationResponse(
          "step-up assertion requires user verification (UV flag not set)"
      )
  ```

---

## 2. Protected Action Taxonomy

The following actions strictly require `AuthLevel.STEP_UP_PASSKEY`:

1. `ActionType.PACKAGE_APPROVE`: Authorizing recommendation package dispatch.
2. `ActionType.PACKAGE_REJECT`: Terminally rejecting recommendation package.
3. `ActionType.PACKAGE_REQUEST_REVISION`: Requesting coach revision on recommendation package.
4. `ActionType.DELEGATION_CREATE`: Granting decision authority to a delegate.
5. `ActionType.USER_ROLE_ASSIGN`: Elevating a user's role.
6. `ActionType.USER_ROLE_REVOKE`: Revoking a user's role.

---

## 3. Ceremony Separation

Security ceremonies are cleanly partitioned to prevent cross-ceremony privilege escalation:

| Ceremony Type | Challenge Scope | Auth Level Required | Output Outcome |
|---|---|---|---|
| **Passkey Registration** | `passkey_registration` | `bootstrap_enrollment` or `authenticated` | New registered passkey in `fc27_webauthn_credentials`. |
| **Passkey Login** | `login` | None (Unauthenticated) | New session with `auth_level='authenticated'`. |
| **Decision Step-Up** | `step_up_decision` | `authenticated` | Persisted `fc27_decision_evidence` bound to payload hash. |

*Registration is never treated as a decision step-up.*

---

## 4. Authenticator Sign-Counter Lifecycle & Clone Detection (R14)

Authenticators maintain signature counters to defend against authenticator cloning:
1. **Zero-Counter Authenticators**: Modern synced passkeys (e.g. iCloud Keychain, Google Password Manager) typically report `sign_count = 0`. The verification policy permits `0 -> 0` transitions.
2. **Initial Sync**: Permitted transition from `0 -> N` when an authenticator begins tracking counters.
3. **Monotonic Progression**: For hardware tokens reporting `counter > 0`, the new counter must be strictly greater than the stored counter (`new_counter > current_counter`).
4. **Clone / Replay Detection**:
   ```python
   if current_counter > 0 and new_counter <= current_counter:
       raise IdentityStoreError(
           401,
           "authenticator sign counter replay or clone detected: counter did not increase"
       )
   ```
