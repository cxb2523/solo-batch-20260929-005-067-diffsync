"""Integration tests for uid collision handling across LocalStore and RedisStore."""

from typing import ClassVar, Tuple
from unittest import mock

import fakeredis
import pytest

from diffsync import DiffSyncModel
from diffsync.exceptions import ObjectAlreadyExists, ObjectNotFound
from diffsync.store.local import LocalStore
from diffsync.store.redis import RedisStore
from diffsync.uid import METRICS, encode


class Pair(DiffSyncModel):
    """Two-field model whose identifier tuples can collide under the legacy uid scheme."""

    _modelname = "pair"
    _identifiers = ("a", "b")
    _attributes: ClassVar[Tuple[str, ...]] = ("note",)

    a: str
    b: str
    note: str = ""


PAIR_X = {"a": "a", "b": "_b"}
PAIR_Y = {"a": "a_", "b": "b"}
LEGACY_UID = "a___b"


def _assert_collision_pairs_distinct(store):
    x = Pair(**PAIR_X)
    y = Pair(**PAIR_Y)
    store.add(obj=x)
    store.add(obj=y)
    assert store.count() == 2
    assert store.get(model=Pair, identifier=PAIR_X) is x if isinstance(store, LocalStore) else True
    assert store.get(model=Pair, identifier=PAIR_X) == x
    assert store.get(model=Pair, identifier=PAIR_Y) == y
    return x, y


def test_localstore_zero_new_uid_collision():
    METRICS.reset()
    store = LocalStore(name="t")
    _assert_collision_pairs_distinct(store)
    # both distinct new uids live under the same model bucket
    assert encode(["a", "_b"]) in store._data["pair"]
    assert encode(["a_", "b"]) in store._data["pair"]


def test_localstore_legacy_uid_reads_back_verbatim():
    METRICS.reset()
    store = LocalStore(name="t")
    x, y = _assert_collision_pairs_distinct(store)
    # Seed a raw legacy key to emulate an existing cache written by the old implementation.
    store._data["pair"][LEGACY_UID] = x
    for _ in range(4):
        assert store.get(model=Pair, identifier=LEGACY_UID) is x
    # exactly one degradation despite repeated reads
    reasons = [d for d in METRICS.degradations() if d["uid"] == LEGACY_UID]
    assert len(reasons) == 1, reasons


def test_localstore_invalid_new_uid_is_flagged_without_crash():
    METRICS.reset()
    store = LocalStore(name="t")
    with pytest.raises(ObjectNotFound):
        store.get(model=Pair, identifier="v2__9:zzz")
    with pytest.raises(ObjectNotFound):
        store.get(model=Pair, identifier="v2__9:zzz")
    flagged = [d for d in METRICS.degradations() if d["reason"] == "invalid-new-uid"]
    assert len(flagged) == 1


def test_localstore_replay_add_is_idempotent():
    METRICS.reset()
    store = LocalStore(name="t")
    x = Pair(**PAIR_X)
    for _ in range(3):
        store.add(obj=x)
    assert store.count() == 1
    assert METRICS.degradation_count == 0
    assert METRICS.collision_count == 0


def test_localstore_distinct_object_same_uid_still_conflicts():
    store = LocalStore(name="t")
    store.add(obj=Pair(**PAIR_X))
    with pytest.raises(ObjectAlreadyExists):
        store.add(obj=Pair(**PAIR_X, note="different"))


@pytest.fixture
def redis_store():
    """Provide a RedisStore backed by fakeredis (no external redis-server required)."""
    with mock.patch("diffsync.store.redis.Redis") as redis_cls:
        redis_cls.from_url.side_effect = lambda url, db=0: fakeredis.FakeStrictRedis(decode_responses=False)
        store = RedisStore(name="mystore", store_id="uidtest", url="redis://localhost")
        yield store


def test_redisstore_zero_new_uid_collision(redis_store):
    METRICS.reset()
    _assert_collision_pairs_distinct(redis_store)
    assert redis_store.count(model=Pair) == 2
    assert redis_store.get_all_model_names() == {"pair"}


def test_redisstore_legacy_layout_reads_back(redis_store):
    METRICS.reset()
    x, _y = _assert_collision_pairs_distinct(redis_store)
    # Directly write a record using the old key layout, as an upgrade-time cache would contain.
    from pickle import dumps

    legacy_key = f"{redis_store._store_label}:pair:{LEGACY_UID}"
    redis_store._store.set(legacy_key, dumps(x.__class__(**PAIR_X)))
    for _ in range(4):
        obj = redis_store.get(model=Pair, identifier=LEGACY_UID)
    assert obj.a == "a" and obj.b == "_b"
    reasons = [d for d in METRICS.degradations() if d["uid"] == LEGACY_UID]
    assert len(reasons) == 1, reasons


def test_redisstore_replay_add_counts_one_degradation_max(redis_store):
    METRICS.reset()
    x = Pair(**PAIR_X)
    redis_store.add(obj=x)
    redis_store.add(obj=x)  # identical replay -> no write, no extra metrics
    assert redis_store.count() == 1
    for _ in range(5):
        redis_store.get(model=Pair, identifier=LEGACY_UID)
    legacy_degradations = [d for d in METRICS.degradations() if d["reason"] == "legacy-read"]
    assert len(legacy_degradations) == 1
    # server-side dedupe hash also has exactly one entry
    assert redis_store._store.hlen(redis_store._dedupe_key) == 1


def test_redisstore_invalid_uid_flagged_once(redis_store):
    METRICS.reset()
    for _ in range(3):
        with pytest.raises(ObjectNotFound):
            redis_store.get(model=Pair, identifier="v2__9:zzz")
    flagged = [d for d in METRICS.degradations() if d["reason"] == "invalid-new-uid"]
    assert len(flagged) == 1
    assert redis_store._store.hlen(redis_store._dedupe_key) == 1
