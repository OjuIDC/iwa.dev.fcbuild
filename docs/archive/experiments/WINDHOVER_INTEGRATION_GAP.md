> Archived document
> **Archived: historical reference only.**
> This document is non-authoritative and must not be used as current operational
> guidance.
> Replacement: `docs/FC27_COMPONENT_STATUS.md`.
> Archive reason: Historical 2026-10-01 unimplemented Windhover contract gap note; not part of the supported FC27 community-provider architecture.
> Operational status: DO NOT USE
> Original path: docs/WINDHOVER_INTEGRATION_GAP.md
> Archived on: 2026-10-31
> Superseded by: docs/FC27_COMPONENT_STATUS.md
> Reason: Historical 2026-10-01 unimplemented Windhover contract gap note; not part of the supported FC27 community-provider architecture.
> Warning: Do not execute commands in this archived document.

# Windhover integration: unresolved contract

**Status: UNRESOLVED. Nothing is implemented for Windhover.** The integration needs an interface the
project owner has not yet supplied. This document records what exists and what has to be decided.
Nothing in the code base imitates or assumes how Windhover behaves.

## References found (searched 2026-10-01)

| Location | Content |
|---|---|
| Project charter (claude.ai project description) | "optional mobile observability through Windhover connected to Langfuse traces"; "Enable optional Windhover live graph view for mobile monitoring"; "optional Windhover config snippet for mobile live view" |
| `src/live_ws_server.py` docstring | Lists "Windhover" as one example WebSocket client: "browser, mobile, Windhover" |
| `README.md`, `docs/`, `config/`, Compose files, tests, requirements | No references |
| Historical ZIPs / `code-structure/*.txt` | Not searched for implementation. They are historical snapshots and are not authoritative. |

The repository has no Windhover dependency, API schema, SDK, URL format, authentication method,
ADR or acceptance criteria.

## What is missing from the interface definition

1. **What Windhover is technically.** It could be a native app, a PWA, a hosted service or a Langfuse plugin.
2. **Transport.** Options include reading Langfuse's public API, subscribing to the FC27 WebSocket bridge, or polling the Graph API.
3. **Data contract.** Which events and fields it consumes, for example graph node progression, interrupt status or lead packages, and in what schema.
4. **Authentication.** Whether it holds a Langfuse key, a WebSocket token (`WS_AUTH_TOKEN`) or a new read-only credential.
5. **Network path.** Every FC27 port binds to `127.0.0.1`. A phone needs a VPN or an authenticated TLS reverse proxy.
6. **Write capability.** Whether it is strictly read-only. It must never approve or reject anything.

## Decisions required from the owner

- Choose the transport and supply its schema or documentation.
- Choose the credential model. It must be a credential separate from the service and commissioner tokens.
- Decide whether trace content (prompts and gamertags) may leave the host. The current default is `LANGFUSE_CAPTURE_CONTENT=false`.
- Choose the remote-access mechanism for mobile, for example Tailscale or WireGuard, or a TLS reverse proxy with SSO.

## Proposed integration seams (no code written)

- **Read-only via Langfuse.** Windhover reads traces through the Langfuse public API with a Langfuse
  project key. Every trace is already keyed by `thread_id` and tagged with `fc_version` and `platform`
  (see `src/tracing.py`). This needs no FC27 code change.
- **Live view via the WebSocket bridge.** Windhover connects to `ws-server` using the documented subprotocol
  authentication (`fc27.v1`, `fc27.bearer.<token>`) and an allow-listed Origin. It would require a
  dedicated read-only token and channel filtering. Neither exists yet.

## Security considerations

- Windhover must have no path to `/threads/{id}/decision-challenge` or `/decision`, and must never hold
  the commissioner token, the signing secret, or database or Redis credentials.
- Mobile clients are a higher-risk location for credentials, so use short-lived, revocable, read-only tokens.
- Do not expose Langfuse, the WebSocket bridge or the Graph API on a public interface without TLS and authentication.

## Acceptance criteria (require owner approval before any implementation)

1. A written interface specification covering transport, schema and authentication, approved by the owner.
2. A read-only credential that is distinct from all existing credentials, with a test proving it cannot reach decision routes.
3. An integration test against a recorded or mock Windhover client built from that specification.
4. Documentation of the remote-access path, with no `0.0.0.0` bindings added to Compose.
