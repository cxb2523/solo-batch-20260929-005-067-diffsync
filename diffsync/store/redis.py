"""RedisStore module."""

import copy
import uuid
from pickle import dumps, loads  # nosec
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Set, Type, Union

try:
    from redis import Redis
    from redis.exceptions import ConnectionError as RedisConnectionError
except ImportError as ierr:
    print("Redis is not installed. Have you installed diffsync with redis extra? `pip install diffsync[redis]`")
    raise ierr

from diffsync.exceptions import ObjectAlreadyExists, ObjectNotFound, ObjectStoreException
from diffsync.store import BaseStore
from diffsync.uid import (
    LEGACY_SEPARATOR,
    METRICS,
    decode,
    encode,
    is_new_uid,
    normalize_fields,
)

if TYPE_CHECKING:
    from diffsync import DiffSyncModel

REDIS_DIFFSYNC_ROOT_LABEL = "diffsync"

# Lua script: write the object and register the legacy-uid alias together. Replaying the same
# record (same object key + same alias field) performs no further writes, so it is both atomic and
# idempotent -- a replay can never produce a second degradation/collision record.
_SET_WITH_ALIAS = """
local object_key = KEYS[1]
local alias_key = KEYS[2]
local alias_field = ARGV[1]
local payload = ARGV[2]
local existed = redis.call('HSETNX', alias_key, alias_field, object_key)
redis.call('SET', object_key, payload)
return existed
"""

# Lua script: record a degradation exactly once per (dedupe-hash-field); every replay is a no-op.
_RECORD_DEGRADATION = """
local dedupe_key = KEYS[1]
local dedupe_field = ARGV[1]
local reason = ARGV[2]
return redis.call('HSETNX', dedupe_key, dedupe_field, reason)
"""


class RedisStore(BaseStore):
    """RedisStore class.

    Objects are always written under the new, self-describing key scheme from :mod:`diffsync.uid`.
    Legacy keys (the historical ``diffsync:<store>:<model>:<uid>`` strings) from existing caches and
    historical diffs remain readable verbatim through a Redis hash mapping legacy uids to the new
    object keys. Writes (object payload + alias registration + degradation dedupe) are performed
    atomically server-side and are idempotent under replay.
    """

    def __init__(  # pylint: disable=too-many-arguments
        self,
        *args: Any,
        store_id: Optional[str] = None,
        host: Optional[str] = None,
        port: int = 6379,
        url: Optional[str] = None,
        db: int = 0,
        **kwargs: Any,
    ):
        """Init method for RedisStore."""
        super().__init__(*args, **kwargs)

        if url and host and port:
            raise ValueError("'url' and 'host' arguments can't be specified together.")

        try:
            if url:
                self._store = Redis.from_url(url, db=db)
            elif host:
                self._store = Redis(host=host, port=port, db=db)
            else:
                raise RedisConnectionError("Neither 'host' nor 'url' were specified.")

            if not self._store.ping():
                raise RedisConnectionError()
        except RedisConnectionError:
            raise ObjectStoreException("Redis store is unavailable.") from RedisConnectionError

        self._store_id = store_id if store_id else str(uuid.uuid4())

        self._store_label = f"{REDIS_DIFFSYNC_ROOT_LABEL}:{self._store_id}"
        # Hash mapping "<modelname>\x00<legacy_uid>" -> new object key (legacy read fallback).
        self._alias_key = f"{self._store_label}:uid-aliases"
        # Hash recording one field per distinct degraded/invalid read (replay-safe dedupe).
        self._dedupe_key = f"{self._store_label}:uid-degradations"
        self._set_with_alias = self._store.register_script(_SET_WITH_ALIAS)
        self._record_degradation = self._store.register_script(_RECORD_DEGRADATION)
        self._source = f"redis:{self.name}:{self._store_id}"

    def __str__(self) -> str:
        """Render store name."""
        return f"{self.name} ({self._store_id})"

    def _get_object_from_redis_key(self, key: Union[str, bytes]) -> "DiffSyncModel":
        """Get the object from Redis key."""
        pickled_object = self._store.get(key)
        if pickled_object:
            obj_result = loads(pickled_object)  # noqa: S301
            obj_result.adapter = self.adapter
            return obj_result
        key_text = key.decode() if isinstance(key, bytes) else key
        raise ObjectNotFound(f"{key_text} not present in Cache")

    def get_all_model_names(self) -> Set[str]:
        """Get all the model names stored.

        Return:
            Set of all the model names.
        """
        all_model_names = set()
        for item in self._store.scan_iter(self._scan_pattern(None)):
            key_text = item.decode() if isinstance(item, bytes) else item
            fields, is_new, invalid = decode(self._strip_object_prefix(key_text))
            if invalid or not is_new or not fields:
                continue
            modelname = fields[0]
            all_model_names.add(modelname)
        return all_model_names

    def _object_prefix(self) -> str:
        """Prefix shared by every new-scheme object key in this store."""
        return f"{self._store_label}:v2__"

    def _scan_pattern(self, modelname: Optional[str]) -> str:
        """Build a SCAN pattern for new-scheme object keys, optionally restricted to one model.

        The pattern anchors on the model field plus the following separator, so a model named "de"
        cannot match keys for a model named "device": a single-field uid leaves no trailing
        separator and simply does not match the anchored pattern.
        """
        if modelname is None:
            return f"{self._object_prefix()}*"
        encoded_model = encode([modelname])[len("v2__") :]
        return f"{self._object_prefix()}{encoded_model}{LEGACY_SEPARATOR}*"

    def _strip_object_prefix(self, key: str) -> str:
        """Turn a stored object key back into a decodable new-scheme uid string."""
        prefix = f"{self._store_label}:"
        return key[len(prefix) :] if key.startswith(prefix) else key

    def _object_key(self, modelname: str, uid_fields: List[str]) -> str:
        """Build the unambiguous Redis key for an object.

        The model name is the first length-prefixed field and the uid's own fields follow, so
        model names or uids containing colons, underscores or the old separator cannot collide.
        """
        uid = encode([modelname, *uid_fields])
        return f"{self._store_label}:{uid}"

    def _legacy_key_for_object(self, modelname: str, legacy_uid: str) -> str:
        """Build the historical key layout, used only to read pre-existing cache entries."""
        return f"{self._store_label}:{modelname}:{legacy_uid}"

    @staticmethod
    def _encode_uid_fields(object_class: Union["DiffSyncModel", Type["DiffSyncModel"], None], identifier: Union[str, Dict]) -> List[str]:
        """Encode an identifier into the ordered new-scheme uid fields."""
        if isinstance(identifier, str):
            return normalize_fields([identifier])
        if object_class is None:
            raise ValueError("A model class is required to encode a dict identifier into a uid")
        return normalize_fields(identifier[key] for key in object_class._identifiers)

    @staticmethod
    def _uid_fields_for_obj(obj: "DiffSyncModel") -> List[str]:
        """Extract ordered new-scheme uid fields from a model instance."""
        identifiers = obj.get_identifiers()
        return normalize_fields(identifiers[key] for key in obj._identifiers)

    def _record_read_degradation(self, *, modelname: str, uid: str, reason: str) -> None:
        """Record a distinct legacy/invalid read in Redis, atomically and replay-safe."""
        dedupe_field = f"{modelname}\x00{uid}\x00{reason}"
        self._record_degradation(keys=[self._dedupe_key], args=[dedupe_field, reason])
        METRICS.record_degradation(source=self._source, key=f"{modelname}:{uid}", uid=uid, reason=reason)

    def _resolve_object_key(self, modelname: str, requested_uid: str) -> str:
        """Resolve a read uid (new, legacy or invalid) to the concrete Redis key to fetch."""
        fields, _new, invalid = decode(requested_uid)
        if invalid:
            self._record_read_degradation(modelname=modelname, uid=requested_uid, reason="invalid-new-uid")
            return self._legacy_key_for_object(modelname, requested_uid)
        if is_new_uid(requested_uid):
            return self._object_key(modelname, fields)

        # Legacy uid: prefer the alias registered on write, then fall back to the historical layout.
        alias_field = f"{modelname}\x00{requested_uid}"
        aliased = self._store.hget(self._alias_key, alias_field)
        # A single dedupe reason per (model, uid): replay never double-counts regardless of whether
        # the alias or the historical key layout served the record.
        self._record_read_degradation(modelname=modelname, uid=requested_uid, reason="legacy-read")
        if aliased:
            return aliased.decode() if isinstance(aliased, bytes) else aliased
        return self._legacy_key_for_object(modelname, requested_uid)

    def get(
        self, *, model: Union[str, "DiffSyncModel", Type["DiffSyncModel"]], identifier: Union[str, Dict]
    ) -> "DiffSyncModel":
        """Get one object from the data store based on its unique id.

        Args:
            model: DiffSyncModel class or instance, or modelname string, that defines the type of the object to retrieve
            identifier: Unique ID of the object to retrieve, or dict of unique identifier keys/values

        Raises:
            ValueError: if obj is a str and identifier is a dict (can't convert dict into a uid str without a model class)
            ObjectNotFound: if the requested object is not present
        """
        object_class, modelname = self._get_object_class_and_model(model)

        if isinstance(identifier, str):
            uid = identifier
        elif object_class is not None:
            uid = encode(self._encode_uid_fields(object_class, identifier))
        else:
            raise ValueError(
                f"Invalid args: ({model}, {object_class}, {identifier}): "
                f"either {object_class} should be a class/instance or {identifier} should be a str"
            )

        return self._get_object_from_redis_key(self._resolve_object_key(modelname, uid))

    def get_all(self, *, model: Union[str, "DiffSyncModel", Type["DiffSyncModel"]]) -> List["DiffSyncModel"]:
        """Get all objects of a given type.

        Args:
            model: DiffSyncModel class or instance, or modelname string, that defines the type of the objects to retrieve

        Returns:
            List of Object
        """
        if isinstance(model, str):
            modelname = model
        else:
            modelname = model.get_type()

        results: List["DiffSyncModel"] = []
        for key in self._store.scan_iter(self._scan_pattern(modelname)):
            results.append(self._get_object_from_redis_key(key))
        return results

    def get_by_uids(
        self, *, uids: List[str], model: Union[str, "DiffSyncModel", Type["DiffSyncModel"]]
    ) -> List["DiffSyncModel"]:
        """Get multiple objects from the store by their unique IDs/Keys and type.

        Args:
            uids: List of unique id / key identifying object in the database.
            model: DiffSyncModel class or instance, or modelname string, that defines the type of the objects to retrieve

        Raises:
            ObjectNotFound: if any of the requested UIDs are not found in the store
        """
        if isinstance(model, str):
            modelname = model
        else:
            modelname = model.get_type()

        results = []
        for uid in uids:
            results.append(self._get_object_from_redis_key(self._resolve_object_key(modelname, uid)))
        return results

    def add(self, *, obj: "DiffSyncModel") -> None:
        """Add a DiffSyncModel object to the store.

        Args:
            obj: Object to store

        Raises:
            ObjectAlreadyExists: if a different object with the same uid is already present.
        """
        modelname = obj.get_type()
        uid_fields = self._uid_fields_for_obj(obj)
        legacy_uid = obj.get_unique_id()

        object_key = self._object_key(modelname, uid_fields)
        alias_field = f"{modelname}\x00{legacy_uid}"

        existing_obj_binary = self._store.get(object_key)
        if existing_obj_binary:
            existing_obj = loads(existing_obj_binary)  # noqa: S301
            existing_obj_dict = existing_obj.dict()

            if existing_obj_dict != obj.dict():
                raise ObjectAlreadyExists(f"Object {object_key} already present", obj)

        # Remove the diffsync object before sending to Redis
        obj_copy = copy.copy(obj)
        obj_copy.adapter = None

        payload = dumps(obj_copy)
        # True replay (same record, byte-identical payload): skip the write entirely so a replay
        # can have any side effect at most once.
        if existing_obj_binary == payload:
            return

        # Single round trip: payload + alias are written together; HSETNX makes the alias part a
        # no-op on replay, guaranteeing idempotency even under concurrent duplicate writes.
        self._set_with_alias(
            keys=[object_key, self._alias_key],
            args=[alias_field, payload],
        )

    def update(self, *, obj: "DiffSyncModel") -> None:
        """Update a DiffSyncModel object to the store.

        Args:
            obj: Object to update
        """
        modelname = obj.get_type()
        uid_fields = self._uid_fields_for_obj(obj)
        legacy_uid = obj.get_unique_id()

        object_key = self._object_key(modelname, uid_fields)
        alias_field = f"{modelname}\x00{legacy_uid}"
        obj_copy = copy.copy(obj)
        obj_copy.adapter = None

        self._set_with_alias(
            keys=[object_key, self._alias_key],
            args=[alias_field, dumps(obj_copy)],
        )

    def remove_item(self, modelname: str, uid: str) -> None:
        """Remove one item from store.

        When addressed by a legacy uid, both the resolved new-scheme key and the (possibly present)
        historical-layout key are deleted together so no stale copy is left behind.
        """
        object_key = self._resolve_object_key(modelname, uid)

        if not self._store.exists(object_key):
            raise ObjectNotFound(f"{modelname} {uid} not present in Cache")

        self._store.delete(object_key)
        if not is_new_uid(uid):
            legacy_key = self._legacy_key_for_object(modelname, uid)
            if legacy_key != object_key:
                self._store.delete(legacy_key)
            self._store.hdel(self._alias_key, f"{modelname}\x00{uid}")

    def count(self, *, model: Union[str, "DiffSyncModel", Type["DiffSyncModel"], None] = None) -> int:
        """Returns the number of elements of a specific model, or all elements in the store if unspecified."""
        modelname = None
        if model is not None:
            modelname = model if isinstance(model, str) else model.get_type()

        return len(list(self._store.scan_iter(self._scan_pattern(modelname))))
