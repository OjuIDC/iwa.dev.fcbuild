# Security policy

## Reporting a vulnerability

Please **do not open a public issue** for security problems.

- Preferred: use GitHub's private vulnerability reporting on this repository
  (**Security → Report a vulnerability**).
- Include the affected file/endpoint, steps to reproduce, and impact. Do not include real credentials,
  tokens, gamertags of other players, or EA account data.

Expect an acknowledgement within 7 days. Fixes land via a reviewed PR; credit is given on request.

## Supported versions

Only the latest commit on `main` is supported.

## Security model

- Authoritative security boundaries & invariants: [`docs/SECURITY_BOUNDARIES.md`](docs/SECURITY_BOUNDARIES.md)
- Approval boundary, WebAuthn primary authority, and symmetric HMAC shared-secret fallback: [`docs/FC27_APPROVAL_FLOW.md`](docs/FC27_APPROVAL_FLOW.md) and [`docs/APPROVAL_SECURITY.md`](docs/APPROVAL_SECURITY.md)
- Credential placement, least-privilege PostgreSQL roles, and network exposure: [`docs/OPERATIONS.md`](docs/OPERATIONS.md) and [`docs/DEPLOYMENT_AND_SECURITY.md`](docs/DEPLOYMENT_AND_SECURITY.md)
- Feature flags and fail-closed gates: [`docs/FEATURE_FLAGS.md`](docs/FEATURE_FLAGS.md)

In scope: anything that lets a component other than the Commissioner approve or reject a recommendation,
bypasses the LangGraph approval interrupt or 14-field `fc27_decision_evidence` verification, executes unapproved live traffic to EA or any unapproved provider, exposes secrets, or
widens network exposure beyond `127.0.0.1`.

Out of scope: findings that require an already-compromised host or a leaked `.env`.
