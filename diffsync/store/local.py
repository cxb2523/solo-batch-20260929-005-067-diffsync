"""LocalStore module."""

import threading
from collections import defaultdict
from typing import TYPE_CHECKING, Any, Dict, List, Set, Tuple, Type, Union

from diffsync import uid
from diffsync.exceptions import ObjectAlreadyExists, ObjectNotFound
from diffsync.store import BaseStore

if TYPE_CHECKING:
    from diffsync import DiffSyncModel


class LocalStore(BaseStore):
    """LocalStore class.

    Underlying objects are keyed by the *new* unambiguous uid produced by
    :mod:`diffsync.uid`. A secondary ``legacy_uid -> new_uid`` index is maintained per
    model so that uids written by older releases (plain ``__`` joins, including any
    that are ambiguous) keep resolving; ambiguous legacy uids record a collision
    metric exactly once.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Init method for LocalStore."""
        super().__init__(*args, **kwargs)

        self._data: Dict[str, Dict[str, "DiffSyncModel"]] = defaultdict(dict)
        # Per-model mapping of legacy uid -> new uid (first writer wins).
        self._legacy_index: Dict[str, Dict[str, str]] = defaultdict(dict)
        # Per-model set of (legacy_uid, obj.dict()) tuples that exist only in the
        # legacy index and that must be protected against silent overwrite.
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # uid helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _identifier_segments(obj: "DiffSyncModel") -> List[str]:
        """Extract the ordered identifier values of a model object via uid module rules."""
        identifiers = obj.get_identifiers()
        return [str(identifiers[key]) for key in obj._identifiers]  # pylint: disable=protected-access

    def _new_uid(self, modelname: str, obj: "DiffSyncModel") -> str:
        """Build the unambiguous storage uid for an object."""
        return uid.encode_model_uid(modelname, self._identifier_segments(obj))

    def _resolve_storage_uid(self, modelname: str, raw_uid: str) -> Tuple[str, bool]:
        """Resolve a caller-provided uid to the new-format storage key.

        Returns ``(storage_uid, is_new_format)``. Malformed new-format uids are
        reported as degradations but still looked up literally, so a single bad input
        never interrupts the caller.
        """
        if uid.is_new_uid(raw_uid):
            try:
                decoded_model, segments = uid.split_model_uid(raw_uid)
            except uid.IllegalUIDError:
                uid.metrics.report_illegal_uid(raw_uid, location=str(self))
                return raw_uid, True
            if decoded_model != modelname:
                uid.metrics.record_degradation(
                    "model_mismatch", f"{decoded_model}!={modelname}:{raw_uid!r}", location=str(self)
                )
            return raw_uid, True

        # Legacy uid: consult the legacy alias index.
        with self._lock:
            mapped = self._legacy_index[modelname].get(raw_uid)
        if mapped is not None:
            return mapped, False

        # An unmapped legacy uid is still a valid literal key for old data.
        return raw_uid, False

    def get_all_model_names(self) -> Set[str]:
        """Get all the model names stored.

        Return:
            Set of all the model names.
        """
        with self._lock:
            return set(self._data.keys())

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

        raw_uid = self._get_uid(model, object_class, identifier)
        storage_uid, _ = self._resolve_storage_uid(modelname, raw_uid)

        with self._lock:
            if storage_uid not in self._data[modelname]:
                raise ObjectNotFound(f"{modelname} {raw_uid} not present in {str(self)}")
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

        with self._lock:
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
        for raw_uid in uids:
            storage_uid, _ = self._resolve_storage_uid(modelname, raw_uid)
            with self._lock:
                if storage_uid not in self._data[modelname]:
                    raise ObjectNotFound(f"{modelname} {raw_uid} not present in {str(self)}")
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
        legacy = obj.get_unique_id()
        new_key = self._new_uid(modelname, obj)

        with self._lock:
            existing_obj = self._data[modelname].get(new_key)
            if existing_obj:
                if existing_obj is not obj:
                    raise ObjectAlreadyExists(f"Object {new_key} already present", obj)
                # Same object replayed: make sure the alias exists but never double-count.
                self._register_legacy_alias(modelname, legacy, new_key, existing_obj)
                return

            # A different new uid may already own this (possibly ambiguous) legacy uid.
            current_owner = self._legacy_index[modelname].get(legacy)
            if current_owner is not None and current_owner != new_key:
                uid.metrics.record_collision(modelname, legacy, location=str(self))
                # First writer keeps the legacy alias; new object is still stored and
                # reachable by its unambiguous uid.
            else:
                self._legacy_index[modelname][legacy] = new_key

            if not obj.adapter:
                obj.adapter = self.adapter

            self._data[modelname][new_key] = obj

    def _register_legacy_alias(self, modelname: str, legacy: str, new_key: str, existing_obj: "DiffSyncModel") -> None:
        """Idempotently attach a legacy uid alias; count collisions only once."""
        owner = self._legacy_index[modelname].get(legacy)
        if owner is None:
            self._legacy_index[modelname][legacy] = new_key
        elif owner != new_key:
            uid.metrics.record_collision(modelname, legacy, location=str(self))

    def update(self, *, obj: "DiffSyncModel") -> None:
        """Update a DiffSyncModel object to the store.

        Args:
            obj: Object to update
        """
        modelname = obj.get_type()
        legacy = obj.get_unique_id()
        new_key = self._new_uid(modelname, obj)

        with self._lock:
            existing_obj = self._data[modelname].get(new_key)
            if existing_obj is obj:
                self._register_legacy_alias(modelname, legacy, new_key, existing_obj)
                return

            self._data[modelname][new_key] = obj
            self._register_legacy_alias(modelname, legacy, new_key, obj)

    def remove_item(self, modelname: str, uid_raw: str) -> None:
        """Remove one item from store."""
        storage_uid, is_new_format = self._resolve_storage_uid(modelname, uid_raw)
        with self._lock:
            if storage_uid not in self._data[modelname]:
                raise ObjectNotFound(f"{modelname} {uid_raw} not present in {str(self)}")
            del self._data[modelname][storage_uid]
            # Drop only the aliases that point at the removed object. Replaying a delete
            # (idempotency) simply has nothing left to remove/count.
            for alias, target in list(self._legacy_index[modelname].items()):
                if target == storage_uid:
                    del self._legacy_index[modelname][alias]

    def count(self, *, model: Union[str, "DiffSyncModel", Type["DiffSyncModel"], None] = None) -> int:
        """Returns the number of elements of a specific model, or all elements in the store if unspecified."""
        with self._lock:
            if not model:
                return sum(len(entries) for entries in self._data.values())

            if isinstance(model, str):
                modelname = model
            else:
                modelname = model.get_type()
            return len(self._data[modelname])
