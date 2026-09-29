"""Tests for the central diffsync.uid encode/decode module.

Covers the three acceptance boundaries:
1. legacy "__"-joined uids are reproduced verbatim and remain readable;
2. new length-prefixed/escaped uids never collide;
3. malformed new-format uids raise (and are recorded as degradations by callers).
"""

import pytest

from diffsync import uid
from diffsync.uid import IllegalUIDError


def test_legacy_uid_is_verbatim_double_underscore_join():
    assert uid.legacy_uid(["a", "b"]) == "a__b"
    assert uid.legacy_uid(["device1", "eth0"]) == "device1__eth0"
    # Values are stringified exactly like the historical implementation.
    assert uid.legacy_uid([1, 2]) == "1__2"


def test_new_uid_is_distinguishable_and_human_readable():
    encoded = uid.encode(["device", "eth0"])
    assert encoded.startswith(uid.NEW_UID_PREFIX)
    assert "device" in encoded and "eth0" in encoded
    assert uid.is_new_uid(encoded)
    assert not uid.is_new_uid("device1__eth0")


def test_new_uid_roundtrip_preserves_segments():
    cases = [
        [],
        [""],
        ["a"],
        ["a_b", "c"],
        ["a", "b_c"],
        ["a", "b__c"],
        ["a__b", "c"],
        ["model", "v2\u00b6fake"],
        ["m", "has\u00b7middle"],
        ["newline\n", "tab\t", "cr\r"],
        ["back\\slash"],
        ["unicode\u00e9"],
        ["123"],
    ]
    for segments in cases:
        encoded = uid.encode(segments)
        assert uid.decode(encoded) == tuple(str(segment) for segment in segments)


def test_new_uids_have_zero_collision_for_legacy_colliding_pairs():
    # These two pairs collapse to the SAME legacy uid but describe different rows.
    left = ["a", "b__c"]
    right = ["a__b", "c"]
    assert uid.legacy_uid(left) == uid.legacy_uid(right) == "a__b__c"
    encoded_left = uid.encode(left)
    encoded_right = uid.encode(right)
    assert encoded_left != encoded_right
    assert uid.decode(encoded_left) == ("a", "b__c")
    assert uid.decode(encoded_right) == ("a__b", "c")


def test_model_names_with_underscores_do_not_collide():
    assert uid.encode_model_uid("my_model", ["x"]) != uid.encode_model_uid("my", ["model_x"])


@pytest.mark.parametrize(
    "bad",
    [
        "v2\u00b63:ab",  # declared length overruns
        "v2\u00b63:ab\u00b72:x",  # second segment truncated
        "v2\u00b6x:1",  # non-numeric length
        "v2\u00b62:a\\",  # dangling escape
        "v2\u00b63:\\xZZ",  # unknown escape token
        "plain_legacy__uid",
        "",
    ],
)
def test_decode_rejects_illegal_uids(bad):
    with pytest.raises(IllegalUIDError):
        uid.decode(bad)


def test_empty_segments_list_roundtrips():
    assert uid.decode(uid.encode([])) == ()


def test_split_model_uid():
    token = uid.encode_model_uid("interface", ["device1", "eth0"])
    modelname, segments = uid.split_model_uid(token)
    assert modelname == "interface"
    assert segments == ("device1", "eth0")


def test_metrics_are_idempotent_per_record():
    metrics = uid.UIDMetrics()
    assert metrics.record_collision("interface", "a__b__c", location="t") is True
    assert metrics.record_collision("interface", "a__b__c", location="t") is False
    assert metrics.collision_count() == 1

    assert metrics.report_illegal_uid("v2\u00b63:ab", location="t") is True
    assert metrics.report_illegal_uid("v2\u00b63:ab", location="t") is False
    assert metrics.degradation_count() == 1

    snapshot = metrics.snapshot()
    assert snapshot == {"collision_count": 1, "degradation_count": 1}


def test_metrics_separate_locations_count_separately():
    metrics = uid.UIDMetrics()
    metrics.record_collision("m", "u", location="local")
    metrics.record_collision("m", "u", location="redis")
    assert metrics.collision_count() == 2
