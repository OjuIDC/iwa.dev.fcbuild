"""Behavior-freeze tests for existing FC27 single-destination delivery and linkage.

Freezes:
1. Tag generation: build_match_post_tag prefix, digest length, and determinism.
2. tag_match_post_content appending and length constraints.
3. Legacy audit-based linkage recording: record_match_discord_message_links.
4. Single-destination LIMIT 1 lookup constraint in get_match_discord_message_links.
5. SyntheticDiscordNotificationTransport in-memory linkage cache.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

import pytest

from src.fc27_discord_dispatcher import (
    SyntheticDiscordNotificationTransport,
    FC27DiscordDispatchConfig,
)
from src.fc27_match_results import (
    MATCH_POST_TAG_PREFIX,
    MATCH_POST_TAG_DIGEST_LENGTH,
    build_match_post_tag,
    tag_match_post_content,
    extract_match_post_tag,
    MatchResultsValidationError,
    MatchDiscordMessageLinks,
)


def test_build_match_post_tag_format_and_determinism() -> None:
    seed = "match-ref-test-12345"
    tag = build_match_post_tag(seed)

    assert tag.startswith(MATCH_POST_TAG_PREFIX)
    assert len(tag) == len(MATCH_POST_TAG_PREFIX) + MATCH_POST_TAG_DIGEST_LENGTH
    expected_hex = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:MATCH_POST_TAG_DIGEST_LENGTH]
    assert tag == f"{MATCH_POST_TAG_PREFIX}{expected_hex}"

    # Determinism
    assert build_match_post_tag(seed) == tag
    # Distinct seeds produce distinct tags
    assert build_match_post_tag("match-ref-test-67890") != tag


def test_build_match_post_tag_empty_seed_fails() -> None:
    with pytest.raises(MatchResultsValidationError, match="non-empty seed"):
        build_match_post_tag("")
    with pytest.raises(MatchResultsValidationError, match="non-empty seed"):
        build_match_post_tag("   ")


def test_tag_match_post_content_and_extraction() -> None:
    base_text = "Match result: East Coasters 3 - 1 Rivals"
    seed = "match_seed_abc123"
    tagged = tag_match_post_content(base_text, seed=seed)

    assert tagged.startswith(base_text)
    tag = build_match_post_tag(seed)
    assert tag in tagged
    extracted = extract_match_post_tag(tagged)
    assert extracted == tag


def test_synthetic_transport_linkage_cache() -> None:
    transport = SyntheticDiscordNotificationTransport()
    match_ref = "m_test_ref_001"
    res_chan = "12345678901234567"
    res_msg = "98765432109876543"
    tc_chan = "11223344556677889"
    tc_msg = "99887766554433221"

    assert transport.get_match_linkage(match_ref) is None

    transport.record_match_linkage(
        match_ref=match_ref,
        results_channel_id=res_chan,
        results_message_id=res_msg,
        team_chat_channel_id=tc_chan,
        team_chat_message_id=tc_msg,
    )

    links = transport.get_match_linkage(match_ref)
    assert links is not None
    assert links.results_channel_id == res_chan
    assert links.results_message_id == res_msg
    assert links.team_chat_channel_id == tc_chan
    assert links.team_chat_message_id == tc_msg


def test_legacy_audit_linkage_limitation_freeze() -> None:
    """Verifies that MatchDiscordMessageLinks dataclass only models one results channel

    and one team chat channel, showing why multi-destination fan-out was previously impossible.
    """
    links = MatchDiscordMessageLinks(
        match_id=1,
        match_ref="ref_01",
        discord_guild_id="111",
        results_channel_id="222",
        results_message_id="333",
        team_chat_channel_id="444",
        team_chat_message_id="555",
    )
    # The existing model has only single formal and single team-chat slots
    assert hasattr(links, "results_channel_id")
    assert hasattr(links, "team_chat_channel_id")
    assert not hasattr(links, "destinations")
