"""Diff and DiffElement classes for DiffSync.

Copyright (c) 2020-2021 Network To Code, LLC <info@networktocode.com>

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

  http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""

from functools import total_ordering
from typing import Any, Dict, Iterable, Iterator, List, Optional, Type

from .enum import DiffSyncActions
from .exceptions import ObjectAlreadyExists, ObjectNotFound
from .uid import decode, encode, is_new_uid
from .utils import OrderedDefaultDict, intersection

# This workaround is used because we are defining a method called `str` in our class definition, which therefore renders
# the builtin `str` type unusable.
StrType = str


class Diff:
    """Diff Object, designed to store multiple DiffElement object and organize them in a group."""

    def __init__(self) -> None:
        """Initialize a new, empty Diff object."""
        self.children = OrderedDefaultDict[StrType, Dict[StrType, DiffElement]](dict)
        """DefaultDict for storing DiffElement objects.

        In the common (non-colliding) case each group maps ``element.name`` -> element, preserving
        the historical format exactly. Entries that collide on a name are promoted to their
        unambiguous uid keys; :attr:`uid_index` maps every uid to its element regardless.
        """
        self.models_processed = 0
        # Secondary index: group -> {new_uid: element} for exact uid lookups on name-keyed entries.
        self.uid_index: Dict[StrType, Dict[StrType, DiffElement]] = OrderedDefaultDict(dict)

    def __len__(self) -> int:
        """Total number of DiffElements stored herein."""
        total = 0
        for child in self.get_children():
            total += len(child)
        return total

    def complete(self) -> None:
        """Method to call when this Diff has been fully populated with data and is "complete".

        The default implementation does nothing, but a subclass could use this, for example, to save
        the completed Diff to a file or database record.
        """

    @staticmethod
    def _element_storage_uid(element: "DiffElement") -> str:
        """Build the collision-free uid for an element's primary-key values.

        All uid construction in the diff layer goes through :mod:`diffsync.uid`; the uid is derived
        from ``element.keys`` (the ordered identifiers) rather than ``element.name`` (the
        shortname), because the shortname is explicitly not guaranteed to be globally unique.
        """
        return encode(str(element.keys[key]) for key in element.keys)

    @staticmethod
    def _legacy_storage_uid(element: "DiffElement") -> str:
        """Build the historical, ambiguous uid for an element (used for legacy reads only)."""
        return "__".join(str(element.keys[key]) for key in element.keys)

    def add(self, element: "DiffElement") -> None:
        """Add a new DiffElement to the changeset of this Diff.

        Elements are keyed by their human-readable ``name`` as in the historical format, which keeps
        ``self.children`` and :meth:`dict` backward compatible. If two elements of the same type
        share a name but have different primary keys (the exact case the legacy ``"__"`` uid could
        not distinguish), the colliding entries are transparently re-keyed by their unambiguous
        uids. An exact uid duplicate still raises, as before.

        Raises:
            ObjectAlreadyExists: if an element with the same unique id is already stored.
        """
        group = self.children[element.type]
        storage_uid = self._element_storage_uid(element)

        # Exact-uid dedupe first: re-adding the same logical element must always fail.
        for existing in group.values():
            if self._element_storage_uid(existing) == storage_uid:
                raise ObjectAlreadyExists(f"Already storing a {element.type} named {element.name}", element)

        self.uid_index[element.type][storage_uid] = element

        name_key = element.name
        incumbent = group.get(name_key)
        if incumbent is None and not is_new_uid(name_key):
            # Common, collision-free case: keep the historical name key verbatim.
            group[name_key] = element
            return

        # Collision on the name key (or an unusual name that mimics a new uid): promote both the
        # incumbent and the newcomer to their unambiguous uid keys.
        if incumbent is not None:
            del group[name_key]
            group[self._element_storage_uid(incumbent)] = incumbent
        group[storage_uid] = element

    def _find_element(self, model_type: StrType, uid: StrType) -> Optional["DiffElement"]:
        """Find a stored element by type and uid (new-scheme or legacy), without raising.

        A legacy uid is matched verbatim against the legacy uids of stored elements -- it is never
        split or reinterpreted. A malformed new-scheme uid never matches, so callers can flag it
        without interrupting anything.
        """
        group = self.children.get(model_type)
        if not group:
            return None
        if is_new_uid(uid):
            stored = group.get(uid)
            if stored is None:
                stored = self.uid_index.get(model_type, {}).get(uid)
            if stored is not None:
                return stored
            _fields, _new, invalid = decode(uid)
            if invalid:
                return None
            # Well-formed but absent: no element.
            return None
        # Legacy uid: exact historical-uid comparison against every stored element.
        for element in group.values():
            if self._legacy_storage_uid(element) == uid:
                return element
        return None

    def get_element(self, model_type: StrType, uid: StrType) -> "DiffElement":
        """Retrieve a stored element by type and uid.

        Args:
            model_type: Model type/group name of the element.
            uid: New-scheme uid (encoded identifiers) or a legacy uid read from a historical diff.

        Raises:
            ObjectNotFound: if no matching element exists.
        """
        element = self._find_element(model_type, uid)
        if element is None:
            raise ObjectNotFound(f"No {model_type} element with uid {uid} present in diff")
        return element

    def groups(self) -> List[StrType]:
        """Get the list of all group keys in self.children."""
        return list(self.children.keys())

    def has_diffs(self) -> bool:
        """Indicate if at least one of the child elements contains some diff.

        Returns:
            True if at least one child element contains some diff
        """
        for group in self.groups():
            for child in self.children[group].values():
                if child.has_diffs(include_children=True):
                    return True

        return False

    def get_children(self) -> Iterator["DiffElement"]:
        """Iterate over all child elements in all groups in self.children.

        For each group of children, check if an order method is defined,
        Otherwise use the default method.
        """
        order_default = "order_children_default"

        for group in self.groups():
            order_method_name = f"order_children_{group}"
            if hasattr(self, order_method_name):
                order_method = getattr(self, order_method_name)
            else:
                order_method = getattr(self, order_default)

            yield from order_method(self.children[group])

    @classmethod
    def order_children_default(cls, children: Dict[StrType, "DiffElement"]) -> Iterator["DiffElement"]:
        """Default method to an Iterator for children.

        Since children is already an OrderedDefaultDict, this method is not doing anything special.
        """
        yield from children.values()

    def summary(self) -> Dict[StrType, int]:
        """Build a dict summary of this Diff and its child DiffElements."""
        summary = {
            DiffSyncActions.CREATE: 0,
            DiffSyncActions.UPDATE: 0,
            DiffSyncActions.DELETE: 0,
            "no-change": 0,
        }
        for child in self.get_children():
            child_summary = child.summary()
            for key in summary:
                summary[key] += child_summary[key]
        summary[DiffSyncActions.SKIP] = (
            self.models_processed
            - summary[DiffSyncActions.CREATE]
            # Updated elements are doubly accumulated in models_processed as they exist in SCR and DST.
            - 2 * summary[DiffSyncActions.UPDATE]
            - summary[DiffSyncActions.DELETE]
            # 'no-change' elements are doubly accumulated in models_processed as they exist in SCR and DST.
            - 2 * summary["no-change"]
        )
        return summary

    def str(self, indent: int = 0) -> StrType:
        """Build a detailed string representation of this Diff and its child DiffElements."""
        margin = " " * indent
        output = []
        for group in self.groups():
            group_heading_added = False
            for child in self.children[group].values():
                if child.has_diffs(include_children=True):
                    if not group_heading_added:
                        output.append(f"{margin}{group}")
                        group_heading_added = True
                    output.append(child.str(indent + 2))
        result = "\n".join(output)
        if not result:
            result = "(no diffs)"
        return result

    def dict(self) -> Dict[StrType, Dict[StrType, Dict]]:
        """Build a dictionary representation of this Diff.

        The public shape is unchanged from the historical format (elements keyed by their
        ``name``/shortname); the unambiguous uid is only used internally for storage.
        """
        result = OrderedDefaultDict[str, Dict](dict)
        for child in self.get_children():
            if child.has_diffs(include_children=True):
                result[child.type][child.name] = child.dict()
        return dict(result)


@total_ordering
class DiffElement:  # pylint: disable=too-many-instance-attributes
    """DiffElement object, designed to represent a single item/object that may or may not have any diffs."""

    def __init__(  # pylint: disable=too-many-positional-arguments
        self,
        obj_type: StrType,
        name: StrType,
        keys: Dict,
        source_name: StrType = "source",
        dest_name: StrType = "dest",
        diff_class: Type[Diff] = Diff,
    ):  # pylint: disable=too-many-arguments
        """Instantiate a DiffElement.

        Args:
            obj_type: Name of the object type being described, as in DiffSyncModel.get_type().
            name: Human-readable name of the object being described, as in DiffSyncModel.get_shortname().
                This name must be unique within the context of the Diff that is the direct parent of this DiffElement.
            keys: Primary keys and values uniquely describing this object, as in DiffSyncModel.get_identifiers().
            source_name: Name of the source DiffSync object
            dest_name: Name of the destination DiffSync object
            diff_class: Diff or subclass thereof to use to calculate the diffs to use for synchronization
        """
        if not isinstance(obj_type, StrType):
            raise ValueError(f"obj_type must be a string (not {type(obj_type)})")

        if not isinstance(name, StrType):
            raise ValueError(f"name must be a string (not {type(name)})")

        self.type = obj_type
        self.name = name
        self.keys = keys
        self.source_name = source_name
        self.dest_name = dest_name
        # Note: *_attrs == None if no target object exists; it'll be an empty dict if it exists but has no _attributes
        self.source_attrs: Optional[Dict] = None
        self.dest_attrs: Optional[Dict] = None
        self.child_diff = diff_class()

    def __lt__(self, other: "DiffElement") -> bool:
        """Logical ordering of DiffElements.

        Other comparison methods (__gt__, __le__, __ge__, etc.) are created by our use of the @total_ordering decorator.
        """
        return (self.type, self.name) < (other.type, other.name)

    def __eq__(self, other: object) -> bool:
        """Logical equality of DiffElements.

        Other comparison methods (__gt__, __le__, __ge__, etc.) are created by our use of the @total_ordering decorator.
        """
        if not isinstance(other, DiffElement):
            return NotImplemented
        return (
            self.type == other.type
            and self.name == other.name
            and self.keys == other.keys
            and self.source_attrs == other.source_attrs
            and self.dest_attrs == other.dest_attrs
            # TODO also check that self.child_diff == other.child_diff, needs Diff to implement __eq__().
        )

    def __str__(self) -> StrType:
        """Basic string representation of a DiffElement."""
        return (
            f'{self.type} "{self.name}" : {self.keys} : '
            f"{self.source_name} → {self.dest_name} : {self.get_attrs_diffs()}"
        )

    def __len__(self) -> int:
        """Total number of DiffElements in this one, including itself."""
        total = 1  # self
        for child in self.get_children():
            total += len(child)
        return total

    @property
    def action(self) -> Optional[StrType]:
        """Action, if any, that should be taken to remediate the diffs described by this element.

        Returns:
            "create", "update", "delete", or None)
        """
        if self.source_attrs is not None and self.dest_attrs is None:
            return DiffSyncActions.CREATE
        if self.source_attrs is None and self.dest_attrs is not None:
            return DiffSyncActions.DELETE
        if (
            self.source_attrs is not None
            and self.dest_attrs is not None
            and any(self.source_attrs[attr_key] != self.dest_attrs[attr_key] for attr_key in self.get_attrs_keys())
        ):
            return DiffSyncActions.UPDATE

        return None

    # TODO: separate into set_source_attrs() and set_dest_attrs() methods, or just use direct property access instead?
    def add_attrs(self, source: Optional[Dict] = None, dest: Optional[Dict] = None) -> None:
        """Set additional attributes of a source and/or destination item that may result in diffs."""
        # TODO: should source_attrs and dest_attrs be "write-once" properties, or is it OK to overwrite them once set?
        if source is not None:
            self.source_attrs = source

        if dest is not None:
            self.dest_attrs = dest

    def get_attrs_keys(self) -> Iterable[StrType]:
        """Get the list of shared attrs between source and dest, or the attrs of source or dest if only one is present.

        - If source_attrs is not set, return the keys of dest_attrs
        - If dest_attrs is not set, return the keys of source_attrs
        - If both are defined, return the intersection of both keys
        """
        if self.source_attrs is not None and self.dest_attrs is not None:
            return intersection(list(self.dest_attrs.keys()), list(self.source_attrs.keys()))
        if self.source_attrs is None and self.dest_attrs is not None:
            return self.dest_attrs.keys()
        if self.source_attrs is not None and self.dest_attrs is None:
            return self.source_attrs.keys()
        return []

    def get_attrs_diffs(self) -> Dict[StrType, Dict[StrType, Any]]:
        """Get the dict of actual attribute diffs between source_attrs and dest_attrs.

        Returns:
            Dictionary of the form `{"-": {key1: <value>, key2: ...}, "+": {key1: <value>, key2: ...}}`,
            where the `"-"` or `"+"` dicts may be absent.
        """
        if self.source_attrs is not None and self.dest_attrs is not None:
            return {
                "-": {
                    key: self.dest_attrs[key]
                    for key in self.get_attrs_keys()
                    if self.source_attrs[key] != self.dest_attrs[key]
                },
                "+": {
                    key: self.source_attrs[key]
                    for key in self.get_attrs_keys()
                    if self.source_attrs[key] != self.dest_attrs[key]
                },
            }
        if self.source_attrs is None and self.dest_attrs is not None:
            return {"-": {key: self.dest_attrs[key] for key in self.get_attrs_keys()}}
        if self.source_attrs is not None and self.dest_attrs is None:
            return {"+": {key: self.source_attrs[key] for key in self.get_attrs_keys()}}
        return {}

    def add_child(self, element: "DiffElement") -> None:
        """Attach a child object of type DiffElement.

        Childs are saved in a Diff object and are organized by type and name.
        """
        self.child_diff.add(element)

    def get_children(self) -> Iterator["DiffElement"]:
        """Iterate over all child DiffElements of this one."""
        yield from self.child_diff.get_children()

    def has_diffs(self, include_children: bool = True) -> bool:
        """Check whether this element (or optionally any of its children) has some diffs.

        Args:
          include_children: If True, recursively check children for diffs as well.
        """
        if (self.source_attrs is not None and self.dest_attrs is None) or (
            self.source_attrs is None and self.dest_attrs is not None
        ):
            return True
        if self.source_attrs is not None and self.dest_attrs is not None:
            for attr_key in self.get_attrs_keys():
                if self.source_attrs.get(attr_key) != self.dest_attrs.get(attr_key):
                    return True

        if include_children:
            if self.child_diff.has_diffs():
                return True

        return False

    def summary(self) -> Dict[StrType, int]:
        """Build a summary of this DiffElement and its children."""
        summary = {
            DiffSyncActions.CREATE: 0,
            DiffSyncActions.UPDATE: 0,
            DiffSyncActions.DELETE: 0,
            "no-change": 0,
        }
        if self.action:
            summary[self.action] += 1
        else:
            summary["no-change"] += 1
        child_summary = self.child_diff.summary()
        for key in summary:
            summary[key] += child_summary[key]
        return summary

    def str(self, indent: int = 0) -> StrType:
        """Build a detailed string representation of this DiffElement and its children."""
        margin = " " * indent
        result = f"{margin}{self.type}: {self.name}"
        if self.source_attrs is not None and self.dest_attrs is not None:
            # Only print attrs that have meaning in both source and dest
            attrs_diffs = self.get_attrs_diffs()
            for attr in attrs_diffs["+"]:
                result += (
                    f"\n{margin}  {attr}"
                    f"    {self.source_name}({attrs_diffs['+'][attr]})"
                    f"    {self.dest_name}({attrs_diffs['-'][attr]})"
                )
        elif self.dest_attrs is not None:
            result += f" MISSING in {self.source_name}"
        elif self.source_attrs is not None:
            result += f" MISSING in {self.dest_name}"

        if self.child_diff.has_diffs():
            result += "\n" + self.child_diff.str(indent + 2)
        elif self.source_attrs is None and self.dest_attrs is None:
            result += " (no diffs)"
        return result

    def dict(self) -> Dict[StrType, Dict[StrType, Any]]:
        """Build a dictionary representation of this DiffElement and its children."""
        attrs_diffs = self.get_attrs_diffs()
        result = {}
        if "-" in attrs_diffs:
            result["-"] = attrs_diffs["-"]
        if "+" in attrs_diffs:
            result["+"] = attrs_diffs["+"]
        if self.child_diff.has_diffs():
            result.update(self.child_diff.dict())
        return result
