"""Testing of RedisStore."""

from unittest import mock

import fakeredis
import pytest

from diffsync.exceptions import ObjectStoreException
from diffsync.store.redis import RedisStore


@pytest.fixture
def redisdb():
    """Provide a fakeredis-backed client when no real redis-server is available."""
    return fakeredis.FakeStrictRedis(decode_responses=False)


def _get_path_from_redisdb(redisdb_instance):
    # fakeredis has no unix socket path; RedisStore is constructed directly from this client in
    # tests via the patch below, so this URL is only a placeholder.
    return "redis://localhost"


@pytest.fixture
def redis_store_factory(monkeypatch):
    """Build RedisStore instances backed by a shared fakeredis client."""
    client = fakeredis.FakeStrictRedis(decode_responses=False)

    def _factory(**kwargs):
        with mock.patch("diffsync.store.redis.Redis") as redis_cls:
            redis_cls.from_url.side_effect = lambda url, db=0: client
            return RedisStore(url="redis://localhost", **kwargs)

    return _factory



def _make_store(_client):
    """Construct a RedisStore around the fakeredis client."""
    with mock.patch("diffsync.store.redis.Redis") as redis_cls:
        redis_cls.from_url.side_effect = lambda url, db=0: _client
        return RedisStore(name="mystore", store_id="123", url="redis://localhost")


# Keep import used for the wrong-host test (real connection attempt expected to fail).
_ = ObjectStoreException

def test_redisstore_init(redisdb):
    store = _make_store(redisdb)
    assert str(store) == "mystore (123)"


def test_redisstore_init_wrong():
    with pytest.raises(ObjectStoreException):
        RedisStore(name="mystore", store_id="123", url="redis://wrong")


def test_redisstore_add_obj(redisdb, make_site):
    store = _make_store(redisdb)
    site = make_site()
    store.add(obj=site)
    assert store.count() == 1


def test_redisstore_add_obj_twice(redisdb, make_site):
    store = _make_store(redisdb)
    site = make_site()
    store.add(obj=site)
    store.add(obj=site)
    assert store.count() == 1


def test_redisstore_get_all_obj(redisdb, make_site):
    store = _make_store(redisdb)
    site = make_site()
    store.add(obj=site)
    assert store.get_all(model=site.__class__)[0] == site


def test_redisstore_get_obj(redisdb, make_site):
    store = _make_store(redisdb)
    site = make_site()
    store.add(obj=site)
    assert store.get(model=site.__class__, identifier=site.name) == site


def test_redisstore_remove_obj(redisdb, make_site):
    store = _make_store(redisdb)
    site = make_site()
    store.add(obj=site)
    assert store.count(model=site.__class__) == store.count() == 1
    store.remove(obj=site)
    assert store.count(model=site.__class__) == store.count() == 0


def test_redisstore_get_all_model_names(redisdb, make_site, make_device):
    store = _make_store(redisdb)
    site = make_site()
    store.add(obj=site)
    device = make_device()
    store.add(obj=device)
    assert site.get_type() in store.get_all_model_names()
    assert device.get_type() in store.get_all_model_names()
