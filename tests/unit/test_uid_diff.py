"""Tests for uid-aware Diff storage: collisions, legacy reads and invalid uid tolerance."""

import pytest

from diffsync.diff import Diff, DiffElement
from diffsync.exceptions import ObjectAlreadyExists, ObjectNotFound
from diffsync.uid import encode


def test_diff_common_case_remains_name_keyed():
    diff = Diff()
    element = DiffElement("device", "device1", {"name": "device1"})
    diff.add(element)
    assert diff.children["device"] == {"device1": element}


def test_diff_same_name_different_keys_no_longer_collide():
    diff = Diff()
    # Two elements sharing a shortname but distinguished by another primary key.
    first = DiffElement("interface", "eth0", {"device_name": "a", "name": "eth0"})
    second = DiffElement("interface", "eth0", {"device_name": "b", "name": "eth0"})
    diff.add(first)
    diff.add(second)
    assert len(diff.children["interface"]) == 2
    values = list(diff.children["interface"].values())
    assert len(values) == 2
    assert all(el in values for el in (first, second))
    # both keys are unambiguous new uids
    assert encode(["a", "eth0"]) in diff.children["interface"]
    assert encode(["b", "eth0"]) in diff.children["interface"]


def test_diff_exact_uid_duplicate_still_raises():
    diff = Diff()
    element = DiffElement("interface", "eth0", {"device_name": "a", "name": "eth0"})
    diff.add(element)
    with pytest.raises(ObjectAlreadyExists):
        diff.add(DiffElement("interface", "othername", {"device_name": "a", "name": "eth0"}))


def test_diff_get_element_by_new_and_legacy_uid():
    diff = Diff()
    element = DiffElement("interface", "eth0", {"device_name": "a", "name": "eth0"})
    diff.add(element)
    new_uid = encode(["a", "eth0"])
    assert diff.get_element("interface", new_uid) is element
    # legacy uid built from the same keys reads the element back verbatim
    assert diff.get_element("interface", "a__eth0") is element
    with pytest.raises(ObjectNotFound):
        diff.get_element("interface", "no__such")
    with pytest.raises(ObjectNotFound):
        diff.get_element("interface", "v2__9:zzz")


def test_diff_invalid_uid_never_raises_outside_explicit_lookup():
    diff = Diff()
    assert diff._find_element("interface", "v2__9:zzz") is None
    assert diff.groups() == []


def test_diff_dict_shape_unchanged_with_collision():
    diff = Diff()
    first = DiffElement("interface", "eth0", {"device_name": "a", "name": "eth0"})
    first.add_attrs(source={"description": "x"})
    second = DiffElement("interface", "eth0", {"device_name": "b", "name": "eth0"})
    second.add_attrs(source={"description": "y"})
    diff.add(first)
    diff.add(second)
    rendered = diff.dict()
    # Historical public shape: keyed by name; one slot survives as in the old ambiguous format.
    assert set(rendered["interface"]) == {"eth0"}
