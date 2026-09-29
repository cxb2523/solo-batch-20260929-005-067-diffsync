"""LocalStore module."""

from collections import defaultdict
from typing import TYPE_CHECKING, Any, Dict, List, Set, Type, Union

from diffsync.exceptions import ObjectAlreadyExists, ObjectNotFound
from diffsync.store import BaseStore
from diffsync.uid import UidIndex, decode, encode, is_new_uid

if TYPE_CHECKING:
    from diffsync import DiffSyncModel


class LocalStore(BaseStore):
    """LocalStore class.

    Records are always keyed by the new, collision-free uid produced by :mod:`diffsync.uid`.
    Legacy uids (the historical ``"__".join(...)`` strings), including uids already present in an
    existing cache or referenced by a historical diff, remain readable verbatim through a per-store
    alias index; a legacy uid is never reinterpreted or split.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Init method for LocalStore."""
        super().__init__(*args, **kwargs)

        self._data: Dict[str, Dict[str, "DiffSyncModel"]] = defaultdict(dict)
        self._uid_index = UidIndex(source=f"local:{self.name}")

    def get_all_model_names(self) -> Set[str]:
        """Get all the model names stored.

        Return:
            Set of all the model names.
        """
        return set(self._data.keys())

    @staticmethod
    def _encode_uid(object_class: Union["DiffSyncModel", Type["DiffSyncModel"], None], identifier: Union[str, Dict]) -> str:
        """Build the new-scheme storage uid from a model class and identifier payload."""
        if object_class is None or not isinstance(identifier, dict):
            raise ValueError("A model class is required to encode a dict identifier into a uid")
        fields = [str(identifier[key]) for key in object_class._identifiers]
        return encode(fields)

    def _resolve_storage_uid(self, modelname: str, uid: str) -> str:
        """Resolve a read uid (new, legacy or invalid) to the actual storage key.

        New uids that parse are used directly. Legacy uids are looked up through the alias index
        registered on writes. Invalid new-scheme uids are flagged via the shared metrics and then
        treated as a literal key, so a single bad value never interrupts the surrounding operation.
        """
        fields, _new, invalid = decode(uid)
        dedupe_key = f"{modelname}:{uid}"
        if invalid:
            self._uid_index.record_read_fallback(uid=uid, key=dedupe_key, reason="invalid-new-uid")
            return uid
        if is_new_uid(uid):
            return uid
        resolved = self._uid_index.resolve(uid)
        if resolved is None:
            # The uid may be a legacy key written before this store instance existed (existing
            # cache/historical diff); read it verbatim and count the fallback once.
            self._uid_index.record_read_fallback(uid=uid, key=dedupe_key, reason="legacy-read")
            return uid
        # Aliased legacy read. One distinct (source, key, uid) record regardless of the alias
        # target, so replaying the same read never double-counts.
        self._uid_index.record_read_fallback(uid=uid, key=dedupe_key, reason="legacy-read")
        return resolved

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
            requested_uid = identifier
        elif object_class is not None:
            requested_uid = self._encode_uid(object_class, identifier)
        else:
            raise ValueError(
                f"Invalid args: ({model}, {object_class}, {identifier}): "
                f"either {object_class} should be a class/instance or {identifier} should be a str"
            )
        storage_uid = self._resolve_storage_uid(modelname, requested_uid)

        if storage_uid not in self._data[modelname]:
            raise ObjectNotFound(f"{modelname} {requested_uid} not present in {str(self)}")
        return self._data[modelname][storage_uid]

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

        return list(self._data[modelname].values())

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
            storage_uid = self._resolve_storage_uid(modelname, uid)
            if storage_uid not in self._data[modelname]:
                raise ObjectNotFound(f"{modelname} {uid} not present in {str(self)}")
            results.append(self._data[modelname][storage_uid])
        return results

    def add(self, *, obj: "DiffSyncModel") -> None:
        """Add a DiffSyncModel object to the store.

        Args:
            obj: Object to store

        Raises:
            ObjectAlreadyExists: if a different object with the same uid is already present.
        """
        modelname = obj.get_type()
        identifiers = obj.get_identifiers()
        new_uid = self._encode_uid(obj, identifiers)
        legacy_uid = obj.get_unique_id()

        existing_obj = self._data[modelname].get(new_uid)
        if existing_obj:
            if existing_obj is not obj:
                raise ObjectAlreadyExists(f"Object {new_uid} already present", obj)
            # Same logical object replayed: keep the alias registration idempotent and return.
            self._uid_index.register(legacy_uid=legacy_uid, new_uid=new_uid, key=f"{modelname}:{new_uid}")
            return

        if not obj.adapter:
            obj.adapter = self.adapter

        self._data[modelname][new_uid] = obj
        self._uid_index.register(legacy_uid=legacy_uid, new_uid=new_uid, key=f"{modelname}:{new_uid}")

    def update(self, *, obj: "DiffSyncModel") -> None:
        """Update a DiffSyncModel object to the store.

        Args:
            obj: Object to update
        """
        modelname = obj.get_type()
        identifiers = obj.get_identifiers()
        new_uid = self._encode_uid(obj, identifiers)
        legacy_uid = obj.get_unique_id()

        existing_obj = self._data[modelname].get(new_uid)
        if existing_obj is obj:
            self._uid_index.register(legacy_uid=legacy_uid, new_uid=new_uid, key=f"{modelname}:{new_uid}")
            return

        self._data[modelname][new_uid] = obj
        self._uid_index.register(legacy_uid=legacy_uid, new_uid=new_uid, key=f"{modelname}:{new_uid}")

    def remove_item(self, modelname: str, uid: str) -> None:
        """Remove one item from store."""
        storage_uid = self._resolve_storage_uid(modelname, uid)
        if storage_uid not in self._data[modelname]:
            raise ObjectNotFound(f"{modelname} {uid} not present in {str(self)}")
        del self._data[modelname][storage_uid]

    def count(self, *, model: Union[str, "DiffSyncModel", Type["DiffSyncModel"], None] = None) -> int:
        """Returns the number of elements of a specific model, or all elements in the store if unspecified."""
        if not model:
            return sum(len(entries) for entries in self._data.values())

        if isinstance(model, str):
            modelname = model
        else:
            modelname = model.get_type()
        return len(self._data[modelname])
