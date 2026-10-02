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

- Approval boundary, roles and signed decisions: [`docs/APPROVAL_SECURITY.md`](docs/APPROVAL_SECURITY.md)
- Credential placement and network exposure: [`docs/DEPLOYMENT_AND_SECURITY.md`](docs/DEPLOYMENT_AND_SECURITY.md)
- Change history and residual risks: [`SECURITY_CHANGES.md`](SECURITY_CHANGES.md)

In scope: anything that lets a component other than the Commissioner approve or reject a recommendation,
bypasses the LangGraph approval interrupt, reaches EA from outside the MCP proxy, exposes secrets, or
widens network exposure beyond `127.0.0.1`.

Out of scope: findings that require an already-compromised host or a leaked `.env`.
