"""Store-level uid behavior for LocalStore and RedisStore (fakeredis-backed)."""

import pickle

import pytest

from diffsync import DiffSyncModel, uid
from diffsync.exceptions import ObjectNotFound
from diffsync.store.local import LocalStore


class Interface(DiffSyncModel):
    """Two-identifier model used to exercise the underscore collision boundary."""

    _modelname = "interface"
    _identifiers = ("device_name", "name")

    device_name: str
    name: str


def _first_colliding():
    # legacy uid "a__b__c"
    return Interface(device_name="a", name="b__c")


def _second_colliding():
    # legacy uid "a__b__c" as well, but a different row
    return Interface(device_name="a__b", name="c")


# --------------------------------------------------------------------------
# LocalStore
# --------------------------------------------------------------------------
@pytest.fixture
def local_store():
    return LocalStore()


def test_local_legacy_uid_reads_back_verbatim(local_store):
    intf = Interface(device_name="device1", name="eth0")
    local_store.add(obj=intf)
    assert local_store.get(model="interface", identifier="device1__eth0") is intf


def test_local_new_uid_reads_back(local_store):
    intf = Interface(device_name="device1", name="eth0")
    local_store.add(obj=intf)
    new_key = uid.encode_model_uid("interface", ["device1", "eth0"])
    assert local_store.get(model="interface", identifier=new_key) is intf


def test_local_legacy_colliding_rows_both_stored_and_counted_once(local_store):
    first = _first_colliding()
    second = _second_colliding()
    local_store.add(obj=first)
    local_store.add(obj=second)

    assert local_store.count(model="interface") == 2
    assert uid.metrics.collision_count() == 1

    # Replaying both records must not add a second collision.
    local_store.add(obj=first)
    local_store.add(obj=second)
    assert uid.metrics.collision_count() == 1

    # The ambiguous legacy uid deterministically resolves to the first writer.
    assert local_store.get(model="interface", identifier="a__b__c") is first

    # Each row is reachable by its own collision-free uid.
    first_key = uid.encode_model_uid("interface", ["a", "b__c"])
    second_key = uid.encode_model_uid("interface", ["a__b", "c"])
    assert first_key != second_key
    assert local_store.get(model="interface", identifier=first_key) is first
    assert local_store.get(model="interface", identifier=second_key) is second


def test_local_malformed_new_uid_records_degradation_and_does_not_crash(local_store):
    local_store.add(obj=Interface(device_name="a", name="b"))
    before = uid.metrics.degradation_count()
    with pytest.raises(ObjectNotFound):
        local_store.get(model="interface", identifier="v2\u00b63:truncated")
    assert uid.metrics.degradation_count() == before + 1


def test_local_remove_by_new_and_legacy_uid(local_store):
    intf = Interface(device_name="device1", name="eth0")
    local_store.add(obj=intf)
    new_key = uid.encode_model_uid("interface", ["device1", "eth0"])
    local_store.remove_item("interface", new_key)
    assert local_store.count(model="interface") == 0


# --------------------------------------------------------------------------
# RedisStore (fakeredis)
# --------------------------------------------------------------------------
@pytest.fixture
def redis_store(redisdb):
    from diffsync.store.redis import RedisStore

    return RedisStore(name="mystore", store_id="uid-test", url="unix://ignored")


def test_redis_legacy_literal_key_from_old_release_reads_back(redis_store):
    # Simulate a key written by an older release: diffsync:<id>:<model>:<legacy uid>
    legacy_key = f"{redis_store._store_label}:interface:device1__eth0"
    redis_store._store.set(legacy_key, pickle.dumps(Interface(device_name="device1", name="eth0")))

    fetched = redis_store.get(model="interface", identifier="device1__eth0")
    assert fetched.device_name == "device1"
    assert fetched.name == "eth0"


def test_redis_new_uid_reads_back(redis_store):
    intf = Interface(device_name="device1", name="eth0")
    redis_store.add(obj=intf)
    new_key = uid.encode_model_uid("interface", ["device1", "eth0"])
    fetched = redis_store.get(model="interface", identifier=new_key)
    assert fetched == intf


def test_redis_colliding_rows_both_stored_collision_counted_once(redis_store):
    first = _first_colliding()
    second = _second_colliding()
    redis_store.add(obj=first)
    redis_store.add(obj=second)

    assert redis_store.count(model="interface") == 2
    assert redis_store._store.scard(redis_store._collision_set_key()) == 1

    redis_store.add(obj=first)
    redis_store.add(obj=second)
    assert redis_store._store.scard(redis_store._collision_set_key()) == 1

    first_key = uid.encode_model_uid("interface", ["a", "b__c"])
    second_key = uid.encode_model_uid("interface", ["a__b", "c"])
    assert redis_store.get(model="interface", identifier=first_key) == first
    assert redis_store.get(model="interface", identifier=second_key) == second
    assert sorted(obj.name for obj in redis_store.get_all(model="interface")) == ["b__c", "c"]


def test_redis_malformed_new_uid_records_degradation(redis_store):
    redis_store.add(obj=Interface(device_name="a", name="b"))
    with pytest.raises(ObjectNotFound):
        redis_store.get(model="interface", identifier="v2\u00b63:truncated")
    assert redis_store._store.scard(redis_store._degradation_set_key()) == 1

    # Repeating the same bad lookup stays idempotent.
    with pytest.raises(ObjectNotFound):
        redis_store.get(model="interface", identifier="v2\u00b63:truncated")
    assert redis_store._store.scard(redis_store._degradation_set_key()) == 1


def test_redis_remove_and_replay_is_idempotent(redis_store):
    intf = Interface(device_name="device1", name="eth0")
    redis_store.add(obj=intf)
    redis_store.add(obj=intf)  # replay must be a no-op
    assert redis_store.count(model="interface") == 1

    new_key = uid.encode_model_uid("interface", ["device1", "eth0"])
    redis_store.remove_item("interface", new_key)
    assert redis_store.count(model="interface") == 0
    with pytest.raises(ObjectNotFound):
        redis_store.remove_item("interface", new_key)
