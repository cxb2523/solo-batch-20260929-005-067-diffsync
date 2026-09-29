"""Diff-level uid disambiguation tests."""

from diffsync import uid
from diffsync.diff import Diff, DiffElement


def test_diff_element_uids_old_and_new():
    element = DiffElement("interface", "eth0", {"device_name": "a", "name": "b__c"})
    assert element.get_legacy_uid() == "a__b__c"
    assert element.get_uid() == uid.encode_model_uid("interface", ["a", "b__c"])
    assert uid.is_new_uid(element.get_uid())


def test_diff_lookup_by_new_and_legacy_uid():
    diff = Diff()
    element = DiffElement("interface", "eth0", {"device_name": "device1", "name": "eth0"})
    diff.add(element)

    assert diff.get_by_uid("interface", "device1__eth0") is element
    new_key = uid.encode_model_uid("interface", ["device1", "eth0"])
    assert diff.get_by_uid("interface", new_key) is element


def test_diff_colliding_shortnames_record_one_collision():
    diff = Diff()
    first = DiffElement("interface", "eth0", {"device_name": "a", "name": "b__c"})
    second = DiffElement("interface", "eth0", {"device_name": "a__b", "name": "c"})

    # Same shortname but distinct unambiguous identity: both retained, collision counted.
    diff.add(first)
    diff.add(second)
    assert uid.metrics.collision_count() == 1

    # Both reachable by their own uid.
    assert diff.get_by_uid("interface", uid.encode_model_uid("interface", ["a", "b__c"])) is first
    assert diff.get_by_uid("interface", uid.encode_model_uid("interface", ["a__b", "c"])) is second


def test_diff_malformed_new_uid_degrades_without_raising():
    diff = Diff()
    assert diff.get_by_uid("interface", "v2\u00b63:bad") is None
    assert uid.metrics.degradation_count() == 1
