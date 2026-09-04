"""The audit entry hash (ADR-0022). Pure function, no database."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta, timezone

import pytest

from app.services.audit import compute_entry_hash

EVENT_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
AT = datetime(2026, 3, 5, 10, 30, tzinfo=UTC)


def entry(**overrides) -> str:
    base = {
        "event_id": EVENT_ID,
        "occurred_at": AT,
        "actor_type": "AGENT",
        "actor_id": "gemini-3.1-flash-lite",
        "action": "agent.explained",
        "entity_type": "discrepancy",
        "entity_id": "abc-123",
        "payload": {"gate_decision": "HUMAN_REVIEW", "delta_minor": -2360},
        "prev_hash": "0" * 64,
    }
    return compute_entry_hash(**{**base, **overrides})


def test_the_hash_is_a_sha256_hex_digest():
    value = entry()
    assert len(value) == 64
    assert set(value) <= set("0123456789abcdef")


def test_the_same_content_always_hashes_the_same():
    assert entry() == entry()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("event_id", uuid.UUID("22222222-2222-2222-2222-222222222222")),
        ("occurred_at", AT + timedelta(seconds=1)),
        ("actor_type", "USER"),
        ("actor_id", "someone-else"),
        ("action", "agent.gated_only"),
        ("entity_type", "match_record"),
        ("entity_id", "def-456"),
        ("payload", {"gate_decision": "BLOCK", "delta_minor": -2360}),
        ("prev_hash", "1" * 64),
    ],
)
def test_changing_any_field_changes_the_hash(field, value):
    """Every hashed field must actually be covered, or tampering with it goes unseen."""
    assert entry(**{field: value}) != entry()


def test_changing_the_predecessor_changes_the_hash():
    """This is what makes it a chain rather than a set of independent checksums."""
    assert entry(prev_hash="a" * 64) != entry(prev_hash="b" * 64)


def test_payload_key_order_does_not_affect_the_hash():
    """Canonical encoding. A hash that moves when nothing did trains people to ignore it."""
    forward = entry(payload={"a": 1, "b": 2, "c": {"x": 1, "y": 2}})
    reversed_keys = entry(payload={"c": {"y": 2, "x": 1}, "b": 2, "a": 1})
    assert forward == reversed_keys


def test_an_equivalent_timestamp_in_another_zone_hashes_the_same():
    """The same instant is the same instant; only the moment is hashed, not its notation."""
    ist = timezone(timedelta(hours=5, minutes=30))
    assert entry(occurred_at=AT.astimezone(ist)) == entry(occurred_at=AT)


def test_the_first_entry_may_have_no_predecessor():
    assert len(entry(prev_hash=None)) == 64


def test_a_genesis_entry_differs_from_one_chained_to_all_zeros():
    assert entry(prev_hash=None) != entry(prev_hash="0" * 64)


def test_an_empty_payload_is_hashable():
    assert len(entry(payload={})) == 64


def test_a_payload_holding_non_json_types_still_hashes():
    """Audit payloads carry Decimals and datetimes; the hash must not crash on them."""
    from decimal import Decimal

    assert len(entry(payload={"score": Decimal("0.9500"), "at": AT})) == 64
