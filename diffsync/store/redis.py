"""RedisStore module."""

import copy
import uuid
from pickle import dumps, loads  # nosec
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Set, Tuple, Type, Union

try:
    from redis import Redis
    from redis.exceptions import ConnectionError as RedisConnectionError
except ImportError as ierr:
    print("Redis is not installed. Have you installed diffsync with redis extra? `pip install diffsync[redis]`")
    raise ierr

from diffsync import uid
from diffsync.exceptions import ObjectAlreadyExists, ObjectNotFound, ObjectStoreException
from diffsync.store import BaseStore

if TYPE_CHECKING:
    from diffsync import DiffSyncModel

REDIS_DIFFSYNC_ROOT_LABEL = "diffsync"

#: Hash field prefix used by the per-model legacy-uid alias maps.
LEGACY_ALIAS_PREFIX = "legacy"
#: Redis set holding idempotent collision dedup keys.
COLLISION_SET_SUFFIX = "meta:collisions"
#: Redis set holding idempotent degradation dedup keys.
DEGRADATION_SET_SUFFIX = "meta:degradations"

# Atomic, idempotent add:
#  KEYS[1] = object data key (new-format uid)
#  KEYS[2] = legacy alias hash (model)
#  KEYS[3] = collision dedup set
#  ARGV[1] = legacy uid (hash field)
#  ARGV[2] = pickled object payload
# Returns (status, collision_flag): status is 1 stored, 0 replay no-op, 2 existing
# different payload; collision_flag is 1 when a legacy-uid collision was recorded.
_ADD_SCRIPT = """
local existing = redis.call('GET', KEYS[1])
local collided = 0
if existing then
    if existing == ARGV[2] then
        return {0, 0}
    end
    return {2, 0}
end
local owner = redis.call('HGET', KEYS[2], ARGV[1])
if owner and owner ~= KEYS[1] then
    if redis.call('SADD', KEYS[3], 'collision\x1f' .. ARGV[1]) == 1 then
        collided = 1
    end
    -- Do not steal the ambiguous legacy alias; store under the unambiguous key only.
else
    redis.call('HSET', KEYS[2], ARGV[1], KEYS[1])
end
redis.call('SET', KEYS[1], ARGV[2])
return {1, collided}
"""

# Atomic, idempotent migration of a legacy literal key onto the new-format key.
#  KEYS[1] = legacy data key, KEYS[2] = new data key
#  KEYS[3] = legacy alias hash, KEYS[4] = collision dedup set
#  ARGV[1] = legacy uid (hash field)
_MIGRATE_SCRIPT = """
local legacy_value = redis.call('GET', KEYS[1])
if not legacy_value then
    return 0
end
local existing_new = redis.call('GET', KEYS[2])
if existing_new then
    if existing_new == legacy_value then
        redis.call('DEL', KEYS[1])
        local owner = redis.call('HGET', KEYS[3], ARGV[1])
        if not owner then
            redis.call('HSET', KEYS[3], ARGV[1], KEYS[2])
        elseif owner ~= KEYS[2] then
            redis.call('SADD', KEYS[4], 'collision\x1f' .. ARGV[1])
        end
        return 0
    end
    return 2
end
redis.call('SET', KEYS[2], legacy_value)
redis.call('DEL', KEYS[1])
redis.call('HSET', KEYS[3], ARGV[1], KEYS[2])
return 1
"""


class RedisStore(BaseStore):
    """RedisStore class.

    Object data is keyed by the unambiguous uid produced by :mod:`diffsync.uid`.
    Per-model Redis hashes map legacy (``__``-joined) uids onto those keys, so caches
    written by older releases keep reading back verbatim; writes use server-side Lua
    scripts so collision/degradation bookkeeping is atomic and idempotent.
    """

    _add_script_sha: Optional[str] = None
    _migrate_script_sha: Optional[str] = None

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

        # Register server-side Lua scripts once (client caches the EVALSHA hash).
        self._add_script = self._store.register_script(_ADD_SCRIPT)
        self._migrate_script = self._store.register_script(_MIGRATE_SCRIPT)

    def __str__(self) -> str:
        """Render store name."""
        return f"{self.name} ({self._store_id})"

    # ------------------------------------------------------------------
    # key construction
    # ------------------------------------------------------------------
    def _object_key(self, modelname: str, new_uid: str) -> str:
        """Data key for an unambiguous uid: ``diffsync:<id>:<v2...>``.

        The encoded uid never contains a literal ":" (all structural characters are
        escaped, and the model name is embedded *inside* the uid), so model filtering
        is a simple prefix glob.
        """
        return f"{self._store_label}:{new_uid}"

    def _legacy_object_key(self, modelname: str, legacy: str) -> str:
        """Historical data key layout: ``diffsync:<id>:<model>:<legacy uid>``."""
        return f"{self._store_label}:{modelname}:{legacy}"

    def _alias_hash_key(self, modelname: str) -> str:
        """Per-model hash mapping legacy uids (fields) to new-format data keys."""
        return f"{self._store_label}:{modelname}:{LEGACY_ALIAS_PREFIX}"

    def _collision_set_key(self) -> str:
        """Set of distinct collision dedup keys for the whole store."""
        return f"{self._store_label}:{COLLISION_SET_SUFFIX}"

    def _degradation_set_key(self) -> str:
        """Set of distinct degradation dedup keys for the whole store."""
        return f"{self._store_label}:{DEGRADATION_SET_SUFFIX}"

    @staticmethod
    def _identifier_segments(obj: "DiffSyncModel") -> List[str]:
        """Extract ordered identifier values through the uid module."""
        identifiers = obj.get_identifiers()
        return [str(identifiers[key]) for key in obj._identifiers]  # pylint: disable=protected-access

    def _resolve_object_key(self, modelname: str, raw_uid: str) -> Tuple[str, bool]:
        """Resolve a caller uid to ``(data_key, is_new_format)``.

        New-format uids map directly; legacy uids go through the alias hash, falling
        back to the historical literal key. Malformed new uids record a degradation
        and are looked up literally, so a bad value never interrupts the caller.
        """
        if uid.is_new_uid(raw_uid):
            try:
                decoded_model, _ = uid.split_model_uid(raw_uid)
            except uid.IllegalUIDError:
                self._record_degradation("illegal_uid", repr(raw_uid))
                return self._object_key(modelname, raw_uid), True
            if decoded_model != modelname:
                self._record_degradation("model_mismatch", f"{decoded_model}!={modelname}:{raw_uid!r}")
            return self._object_key(modelname, raw_uid), True

        owner = self._store.hget(self._alias_hash_key(modelname), raw_uid)
        if owner:
            return owner.decode(), False
        # Unmapped legacy uid: historical literal key.
        return self._legacy_object_key(modelname, raw_uid), False

    def _record_degradation(self, reason: str, detail: str) -> bool:
        """Idempotently record a degradation; SADD gives set-level dedup atomically."""
        return bool(self._store.sadd(self._degradation_set_key(), f"{reason}\x1f{detail}"))

    def _record_collision(self, legacy: str) -> bool:
        """Idempotently record a legacy-uid collision."""
        return bool(self._store.sadd(self._collision_set_key(), f"collision\x1f{legacy}"))

    def _get_object_from_redis_key(self, key: str) -> "DiffSyncModel":
        """Get the object from Redis key."""
        pickled_object = self._store.get(key)
        if pickled_object:
            obj_result = loads(pickled_object)  # noqa: S301
            obj_result.adapter = self.adapter
            return obj_result
        raise ObjectNotFound(f"{key} not present in Cache")

    def get_all_model_names(self) -> Set[str]:
        """Get all the model names stored.

        Return:
            Set of all the model names.
        """
        all_model_names = set()
        for item in self._store.scan_iter(f"{self._store_label}:*"):
            key = item.decode()
            if not key.startswith(f"{self._store_label}:"):
                continue
            remainder = key[len(self._store_label) + 1 :]
            # Legacy keys:   <model>:<uid>  and  <model>:legacy
            # New keys:      v2... (model embedded in the encoded uid)
            if uid.is_new_uid(remainder):
                try:
                    model_name, _ = uid.split_model_uid(remainder)
                    all_model_names.add(model_name)
                except uid.IllegalUIDError:
                    self._record_degradation("illegal_uid", key)
                continue
            if remainder.endswith(f":{LEGACY_ALIAS_PREFIX}") or remainder.startswith("meta:"):
                continue
            model_name = remainder.split(":", 1)[0]
            all_model_names.add(model_name)
        return all_model_names

    def get(
        self, *, model: Union[str, "DiffSyncModel", Type["DiffSyncModel"]], identifier: Union[str, Dict]
    ) -> "DiffSyncModel":
        """Get one object from the data store based on its unique id.

        Raises:
            ValueError: if obj is a str and identifier is a dict (can't convert dict into a uid str without a model class)
            ObjectNotFound: if the requested object is not present
        """
        object_class, modelname = self._get_object_class_and_model(model)

        raw_uid = self._get_uid(model, object_class, identifier)
        object_key, _ = self._resolve_object_key(modelname, raw_uid)

        return self._get_object_from_redis_key(object_key)

    def _iter_object_keys(self, modelname: Optional[str]) -> List[bytes]:
        """List object data keys, excluding alias hashes and meta keys; optionally per model."""
        keys: List[bytes] = []
        pattern = f"{self._store_label}:*" if modelname is None else f"{self._store_label}:*"
        for item in self._store.scan_iter(pattern):
            key = item.decode()
            remainder = key[len(self._store_label) + 1 :]
            if remainder.startswith("meta:"):
                continue
            if uid.is_new_uid(remainder):
                if modelname is None:
                    keys.append(item)
                    continue
                try:
                    decoded_model, _ = uid.split_model_uid(remainder)
                except uid.IllegalUIDError:
                    self._record_degradation("illegal_uid", key)
                    continue
                if decoded_model == modelname:
                    keys.append(item)
                continue
            # Legacy layout: "<model>:<legacy uid>"; alias hashes end with ":legacy".
            legacy_model, _, legacy_rest = remainder.partition(":")
            if legacy_model == "meta" or not legacy_rest:
                continue
            if legacy_rest == LEGACY_ALIAS_PREFIX:
                continue
            if modelname is None or legacy_model == modelname:
                keys.append(item)
        return keys

    def get_all(self, *, model: Union[str, "DiffSyncModel", Type["DiffSyncModel"]]) -> List["DiffSyncModel"]:
        """Get all objects of a given type.

        Returns:
            List of Object
        """
        if isinstance(model, str):
            modelname = model
        else:
            modelname = model.get_type()

        return [self._get_object_from_redis_key(key) for key in self._iter_object_keys(modelname)]  # type: ignore[arg-type]

    def get_by_uids(
        self, *, uids: List[str], model: Union[str, "DiffSyncModel", Type["DiffSyncModel"]]
    ) -> List["DiffSyncModel"]:
        """Get multiple objects from the store by their unique IDs/Keys and type.

        Raises:
            ObjectNotFound: if any of the requested UIDs are not found in the store
        """
        if isinstance(model, str):
            modelname = model
        else:
            modelname = model.get_type()

        results = []
        for raw_uid in uids:
            object_key, _ = self._resolve_object_key(modelname, raw_uid)
            results.append(self._get_object_from_redis_key(object_key))
        return results

    def add(self, *, obj: "DiffSyncModel") -> None:
        """Add a DiffSyncModel object to the store.

        The write, legacy-alias registration and collision bookkeeping run in one
        Lua script, so it is atomic; replaying an identical record performs no writes
        and contributes at most one collision entry.

        Raises:
            ObjectAlreadyExists: if a different object with the same uid is already present.
        """
        modelname = obj.get_type()
        legacy = obj.get_unique_id()
        new_uid = uid.encode_model_uid(modelname, self._identifier_segments(obj))
        object_key = self._object_key(modelname, new_uid)
        alias_hash = self._alias_hash_key(modelname)

        # Remove the diffsync object before sending to Redis
        obj_copy = copy.copy(obj)
        obj_copy.adapter = None
        payload = dumps(obj_copy)

        # First migrate any legacy-literal key written by an older release (no-op if none).
        self._migrate_script(
            keys=[
                self._legacy_object_key(modelname, legacy),
                object_key,
                alias_hash,
                self._collision_set_key(),
            ],
            args=[legacy],
        )

        result = self._add_script(
            keys=[object_key, alias_hash, self._collision_set_key()],
            args=[legacy, payload],
        )
        status = result[0] if isinstance(result, (list, tuple)) else result

        if status == 2:
            existing_obj_binary = self._store.get(object_key)
            existing_obj = loads(existing_obj_binary)  # noqa: S301
            if existing_obj.dict() != obj.dict():
                raise ObjectAlreadyExists(f"Object {new_uid} already present", obj)
            # Same content, different pickle: a logical replay -- nothing to change.

    def update(self, *, obj: "DiffSyncModel") -> None:
        """Update a DiffSyncModel object to the store, keeping the legacy alias consistent."""
        modelname = obj.get_type()
        legacy = obj.get_unique_id()
        new_uid = uid.encode_model_uid(modelname, self._identifier_segments(obj))
        object_key = self._object_key(modelname, new_uid)
        alias_hash = self._alias_hash_key(modelname)

        obj_copy = copy.copy(obj)
        obj_copy.adapter = None
        payload = dumps(obj_copy)

        # If only an old literal key exists, migrate it first so the update is not lost.
        self._migrate_script(
            keys=[
                self._legacy_object_key(modelname, legacy),
                object_key,
                alias_hash,
                self._collision_set_key(),
            ],
            args=[legacy],
        )

        self._store.set(object_key, payload)
        owner = self._store.hget(alias_hash, legacy)
        if not owner:
            self._store.hsetnx(alias_hash, legacy, object_key)
        elif owner.decode() != object_key:
            self._record_collision(legacy)

    def remove_item(self, modelname: str, raw_uid: str) -> None:
        """Remove one item from store, resolving either a new or a legacy uid."""
        object_key, is_new_format = self._resolve_object_key(modelname, raw_uid)

        if not self._store.exists(object_key):
            raise ObjectNotFound(f"{modelname} {raw_uid} not present in Cache")

        self._store.delete(object_key)
        alias_hash = self._alias_hash_key(modelname)
        # Remove only alias fields that point at the deleted key (idempotent replay).
        for field, value in self._store.hgetall(alias_hash).items():
            if value.decode() == object_key:
                self._store.hdel(alias_hash, field)

    def count(self, *, model: Union[str, "DiffSyncModel", Type["DiffSyncModel"], None] = None) -> int:
        """Returns the number of elements of a specific model, or all elements in the store if unspecified."""
        if isinstance(model, str):
            modelname: Optional[str] = model
        elif model is None:
            modelname = None
        else:
            modelname = model.get_type()
        return len(self._iter_object_keys(modelname))
