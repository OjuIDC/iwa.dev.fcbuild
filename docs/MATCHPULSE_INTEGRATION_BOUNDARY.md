# MatchPulse Integration Boundary

## Repository ownership

- MatchPulse product development belongs only in `..\matchpulse-live-public`.
- The embedded or archived MatchPulse prototype in FC Build is historical and non-authoritative.

## FC Build must not

- Import MatchPulse Python source
- Vendor MatchPulse application code
- Use a Git submodule
- Use a Git subtree
- Share a database
- Share Redis
- Share identity tables
- Send private identity tokens
- Call player approval endpoints
- Call assignment endpoints
- Call dispatch endpoints
- Assume MatchPulse can mutate private state

## Allowed boundary

- Deterministic synthetic JSONL
- Approved localhost HTTP calls
- Approved HTTP calls to an explicitly supplied base URL
- Approved WebSocket calls to an explicitly supplied base URL
- Read-only processing of MatchPulse responses

## MatchPulse output is informational

MatchPulse responses cannot authorize player assignment, player approval, invitation, dispatch, delivery, tenant change, or identity mutation.

## Sibling path

`..\matchpulse-live-public`

No third repository may be created.

## Authoritative MatchPulse release

- Source: `335781f928011f99a644083d80bfe4dfc480b1a6`
- Image: `ghcr.io/ofayese/matchpulse-live@sha256:f413c32f671b191a4b3c63c8bed9c7b318ccdc60633bf222d5cba0049feaa226`
- Attestation: `59336499`

Publication, governance, environment policy, and attestation are complete and owned by the public repository.

## Dispatch

DISPATCH_ENABLED=false
DISPATCH_WORKER_ENABLED=false
