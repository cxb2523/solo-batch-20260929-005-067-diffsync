"""Tests for diffsync.uid: new/legacy codec, collisions and idempotent degradation metrics."""

import pytest

from diffsync.uid import (
    InvalidUidError,
    UidIndex,
    UidMetrics,
    decode,
    decode_strict,
    encode,
    find_legacy_collisions,
    is_new_uid,
    legacy_encode,
)

EDGE_CASES = [
    ("simple", ["a", "b"]),
    ("underscore-in-field", ["a_b", "c"]),
    ("separator-in-field", ["a__b", "c"]),
    ("leading-trailing-underscore", ["_a", "b_"]),
    ("backslash", [r"a\b", r"c\d"]),
    ("backslash-underscore", [r"a\_b", "c"]),
    ("empty", ["", ""]),
    ("prefix-lookalike", ["v2__", "x"]),
    ("unicode", ["模型", "主_键"]),
    ("non-string", [1, 2.5, True]),
]


@pytest.mark.parametrize("case", EDGE_CASES)
def test_encode_decode_roundtrip(case):
    fields = case[1]
    uid = encode(fields)
    assert is_new_uid(uid)
    decoded, new, invalid = decode(uid)
    assert (new, invalid) == (True, False)
    assert decoded == [str(f) for f in fields]


def test_new_uid_is_distinguishable_and_human_readable():
    uid = encode(["a_b", "c"])
    assert uid.startswith("v2__")
    # escaped underscore and the length prefix keep it readable
    assert "a\\_b" in uid
    legacy_uid = legacy_encode(["a_b", "c"])
    assert legacy_uid == "a_b__c"
    assert not is_new_uid(legacy_uid)


def test_colliding_legacy_inputs_get_distinct_new_uids():
    pair_a = ["a", "_b"]
    pair_b = ["a_", "b"]
    assert legacy_encode(pair_a) == legacy_encode(pair_b) == "a___b"
    assert encode(pair_a) != encode(pair_b)


def test_decode_legacy_uid_is_untouched():
    legacy = "a_b__c"
    fields, new, invalid = decode(legacy)
    assert fields == [legacy]
    assert (new, invalid) == (False, False)
    # Legacy uids are never split, even if they "look" splittable.
    assert decode_strict  # strict decoder exists for new uids only
    assert decode("anything___here") == (["anything___here"], False, False)


@pytest.mark.parametrize(
    "bad_uid",
    [
        "v2__",
        "v2__abc",
        "v2__3:ab",  # declared length exceeds
        "v2__2:ab__x",  # bad length prefix
        "v2__2:a__1:b",  # declared length exceeds actual field
        "v2__2:ab__",  # trailing separator
        "v2__1:a_1:b",  # missing separator between fields
        "v2__2:a\\",  # trailing escape
        "v2__2:a\\x",  # unsupported escape
    ],
)
def test_invalid_new_uids_are_flagged_not_crashing(bad_uid):
    with pytest.raises(InvalidUidError):
        decode_strict(bad_uid)
    fields, new, invalid = decode(bad_uid)
    assert (new, invalid) == (True, True)
    assert fields == [bad_uid]


def test_find_legacy_collisions():
    samples = [
        (encode(["a", "_b"]), "a___b"),
        (encode(["a_", "b"]), "a___b"),
        (encode(["z"]), "z"),
    ]
    collisions = find_legacy_collisions(samples)
    assert set(collisions) == {"a___b"}
    assert len(collisions["a___b"]) == 2


def test_metrics_are_idempotent_under_replay():
    metrics = UidMetrics()
    assert metrics.record_collision("a___b", [encode(["a", "_b"]), encode(["a_", "b"])])
    assert not metrics.record_collision("a___b", [encode(["a", "_b"]), encode(["a_", "b"])])
    assert metrics.collision_count == 1

    for _ in range(5):
        metrics.record_degradation(source="local:t", key="m:a___b", uid="a___b", reason="legacy-read")
    assert metrics.degradation_count == 1

    # same record, different reason label later: still one count (replay-safe)
    metrics.record_degradation(source="local:t", key="m:a___b", uid="a___b", reason="legacy-alias")
    assert metrics.degradation_count == 1

    metrics.record_degradation(source="local:t", key="m:v2__9:zz", uid="v2__9:zz", reason="invalid-new-uid")
    assert metrics.degradation_count == 2


def test_uid_index_registers_aliases_and_detects_collision_once():
    index = UidIndex(source="local:test", metrics=UidMetrics())
    new_a = encode(["a", "_b"])
    new_b = encode(["a_", "b"])
    index.register(legacy_uid="a___b", new_uid=new_a)
    assert index.resolve("a___b") == new_a
    # Replay of same alias: no collision
    index.register(legacy_uid="a___b", new_uid=new_a)
    assert index.metrics.collision_count == 0
    # Distinct new uid on same legacy uid: collision recorded
    assert index.register(legacy_uid="a___b", new_uid=new_b)
    assert not index.register(legacy_uid="a___b", new_uid=new_b)
    assert index.metrics.collision_count == 1
