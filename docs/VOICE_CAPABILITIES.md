# Optional voice capabilities

Voice is not too much for this architecture if it remains a separate, optional profile.

## Recommended components

- One local speech-to-text service for all agents.
- One local text-to-speech service with multiple voice profiles.
- A voice gateway that authenticates calls to the Graph API.
- Push-to-talk for the initial release.
- WebSocket or WebRTC streaming only after the request/response version is stable.

## Security boundary

Voice must never directly approve a recommendation. Speech recognition can prepare a proposed decision, but the Commissioner must confirm the exact candidate and action through an authenticated visual or hardware-backed control. Record a transcript hash, not raw audio, unless recording is explicitly enabled and governed by a retention policy.

## Suggested events

```text
fc27:voice:transcript
fc27:voice:reply
fc27:voice:confirmation_required
fc27:voice:error
```

Do not broadcast raw audio through Redis pub/sub. Store short-lived audio in memory or an encrypted temporary volume and delete it immediately after transcription.

## Rollout order

1. Text-to-speech for approved rundowns.
2. Push-to-talk questions with local speech-to-text.
3. Read-only voice queries about matches and players.
4. Voice-assisted approval drafting with mandatory visual confirmation.
5. Optional distinct voice profiles for ScoutAlpha, AnalystUnit, CoachPrime, TacticsCore, and DirectorPrime.
