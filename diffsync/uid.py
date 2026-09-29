"""Centralized encode/decode logic for DiffSync unique identifiers (uids).

Historically a uid was simply the identifier values joined with ``"__"``. That scheme is ambiguous
when a model name or a primary-key value itself contains underscores: the tuples ``("a_b", "c")``
and ``("a", "b_c")`` both produce the legacy uid ``"a_b__c"``. In a store keyed by such strings
(local cache, Redis cache, historical diff data) the two records collide and one silently
overwrites/shadows the other.

This module provides two coexisting schemes:

* **Legacy** uids are produced by :func:`legacy_encode` and are byte-for-byte identical to the old
  ``"__".join(...)`` output, so every uid previously written into a cache or a historical diff can
  still be read back exactly as-is.
* **New** uids are produced by :func:`encode`. Every field is character-escaped (so the separator
  never appears unescaped inside a field) *and* wrapped in an explicit length prefix (so a parser
  never has to guess where a field ends). Every new uid starts with :data:`NEW_UID_PREFIX`, which no
  legacy uid can start with, making the two schemes trivially distinguishable. New uids remain
  human-readable, e.g. ``("a_b", "c")`` -> ``"v2__3:a_b_3:c"``.

Reads go through :func:`decode`: a new uid is parsed back to the exact fields it was built from;
a legacy uid is returned untouched (it is fundamentally ambiguous and can never be split
unambiguously). Anything that carries the new-uid marker but fails strict parsing is reported as
an *invalid* uid so callers can flag it without aborting the surrounding operation.

The module also tracks two process-wide, idempotency-aware counters used by the stores and by the
debugging web board:

* **collisions**: distinct legacy uids that are shared by more than one distinct new uid.
* **degradations**: each distinct record (``namespace`` + key context + uid) that could only be
  served via the legacy/ambiguous path (legacy read fallback or invalid new uid). Replaying the
  same record any number of times increments the counter exactly once.
"""

from __future__ import annotations

import threading
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

NEW_UID_PREFIX = "v2__"
"""Marker prefixing every new-scheme uid. A legacy uid cannot start with it."""

LEGACY_SEPARATOR = "__"
"""Separator used by the historical, ambiguous uid scheme."""

# Escaping is character-based: backslash first (so the escape character itself cannot collide with
# an underscore that it precedes), then underscore. Both characters therefore round-trip exactly,
# and the literal separator never occurs *unescaped* inside an encoded field.
_ESCAPE = "\\"
_ESCAPED_ESCAPE = "\\\\"
_ESCAPED_UNDERSCORE = "\\_"

_UidFields = Sequence[str]


class InvalidUidError(ValueError):
    """Raised when a uid carries the new-scheme marker but fails strict decoding."""

    def __init__(self, uid: str, reason: str) -> None:
        """Store the offending uid and a human-readable reason."""
        self.uid = uid
        self.reason = reason
        super().__init__(f"Invalid new-scheme uid {uid!r}: {reason}")


def _escape_field(value: str) -> str:
    return value.replace(_ESCAPE, _ESCAPED_ESCAPE).replace("_", _ESCAPED_UNDERSCORE)


def _unescape_field(value: str, uid: str) -> str:
    result: List[str] = []
    index = 0
    while index < len(value):
        char = value[index]
        if char != _ESCAPE:
            result.append(char)
            index += 1
            continue
        if index + 1 >= len(value):
            raise InvalidUidError(uid, "trailing escape character inside field")
        next_char = value[index + 1]
        if next_char == _ESCAPE:
            result.append(_ESCAPE)
        elif next_char == "_":
            result.append("_")
        else:
            raise InvalidUidError(uid, f"unsupported escape sequence {next_char!r}")
        index += 2
    return "".join(result)


def normalize_fields(fields: Iterable[Any]) -> List[str]:
    """Convert identifier values to strings, exactly like the legacy scheme did.

    Args:
        fields: Ordered identifier values for a record (model primary keys, etc.).

    Returns:
        New list of the values coerced to ``str``.
    """
    return [str(field) for field in fields]


def legacy_encode(fields: Iterable[Any]) -> str:
    """Build a legacy uid, byte-for-byte identical to the historical ``"__".join(...)`` output.

    >>> legacy_encode(("a_b", "c"))
    'a_b__c'
    """
    return LEGACY_SEPARATOR.join(str(field) for field in fields)


def encode(fields: Iterable[Any]) -> str:
    r"""Build a collision-free, self-describing new-scheme uid.

    Each field is escaped and wrapped in a ``<length>:`` prefix; fields are joined with the legacy
    separator. The leading :data:`NEW_UID_PREFIX` marks it as a new-scheme uid.

    >>> print(encode(("a_b", "c")))  # doctest: +NORMALIZE_WHITESPACE
    v2__4:a\\_b__1:c
    >>> decode(encode(("a_b", "c")))
    (['a_b', 'c'], True, False)
    """
    encoded_fields = [f"{len(escaped)}:{escaped}" for escaped in (_escape_field(f) for f in normalize_fields(fields))]
    return NEW_UID_PREFIX + LEGACY_SEPARATOR.join(encoded_fields)


def is_new_uid(uid: Any) -> bool:
    """Return whether ``uid`` is marked as a new-scheme uid."""
    return isinstance(uid, str) and uid.startswith(NEW_UID_PREFIX)


def decode(uid: str) -> Tuple[List[str], bool, bool]:
    """Decode a uid read from storage.

    Args:
        uid: uid string as stored.

    Returns:
        ``(fields, is_new, is_invalid)`` tuple:

        * new-scheme uid that parses cleanly: ``(fields, True, False)``
        * legacy uid: ``([uid], False, False)`` -- returned untouched, never split
        * new-scheme uid that fails parsing: ``([uid], True, True)``

    Raises:
        InvalidUidError: only by :func:`decode_strict`; this function reports invalidity instead.
    """
    if not is_new_uid(uid):
        return [uid], False, False
    try:
        return decode_strict(uid), True, False
    except InvalidUidError:
        return [uid], True, True


def decode_strict(uid: str) -> List[str]:
    """Decode a new-scheme uid, raising :class:`InvalidUidError` if anything is malformed.

    Parsing is driven entirely by the length prefixes: the separator is stripped out while walking
    the string and never used to guess field boundaries. Every length declaration is validated
    against the actual number of remaining characters.
    """
    if not isinstance(uid, str):
        raise InvalidUidError(str(uid), "uid must be a string")
    if not uid.startswith(NEW_UID_PREFIX):
        raise InvalidUidError(uid, "missing new-scheme prefix")

    remainder = uid[len(NEW_UID_PREFIX) :]
    fields: List[str] = []
    position = 0
    while True:
        colon = remainder.find(":", position)
        if colon == -1:
            raise InvalidUidError(uid, "missing length prefix")
        length_text = remainder[position:colon]
        if not length_text.isdigit():
            raise InvalidUidError(uid, f"length prefix must be digits, got {length_text!r}")
        field_length = int(length_text)
        escaped_start = colon + 1
        escaped_end = escaped_start + field_length
        if escaped_end > len(remainder):
            raise InvalidUidError(uid, f"declared length {field_length} exceeds remaining input")
        escaped_field = remainder[escaped_start:escaped_end]
        fields.append(_unescape_field(escaped_field, uid))
        position = escaped_end
        if position == len(remainder):
            break
        if not remainder.startswith(LEGACY_SEPARATOR, position):
            raise InvalidUidError(uid, "expected field separator after length-prefixed field")
        position += len(LEGACY_SEPARATOR)
        if position == len(remainder):
            raise InvalidUidError(uid, "trailing separator with no following field")
    if not fields:
        raise InvalidUidError(uid, "uid contains no fields")
    return fields


def encode_store_key(namespace: str, *parts: Any) -> str:
    r"""Build a self-describing storage key for a fixed namespace and ordered key parts.

    Used by backends (such as Redis) that must flatten ``namespace`` plus uid into a single string
    and need the result to be unambiguous regardless of embedded separators or colons.

    >>> print(encode_store_key("diffsync:123", "device", ("eth_0",)))
    v2__12:diffsync:123__6:device__6:eth\\_0
    """
    # The namespace is a single opaque field; remaining parts are themselves field sequences.
    fields: List[str] = [namespace]
    for part in parts:
        if isinstance(part, (list, tuple)):
            fields.extend(normalize_fields(part))
        else:
            fields.append(str(part))
    return encode(fields)


def find_legacy_collisions(samples: Iterable[Tuple[str, str]]) -> Dict[str, List[str]]:
    """Given ``(new_uid, legacy_uid)`` pairs, return legacy uids that map to >1 distinct new uid.

    Args:
        samples: Iterable of ``(new_uid, legacy_uid)`` pairs (order of the pair matches the
            encoding direction: the unambiguous uid first, the ambiguous uid second).

    Returns:
        Mapping of each colliding legacy uid to the sorted list of distinct new uids that share it.
    """
    mapping: Dict[str, set] = {}
    for new_uid, legacy_uid in samples:
        mapping.setdefault(legacy_uid, set()).add(new_uid)
    return {legacy_uid: sorted(new_uids) for legacy_uid, new_uids in mapping.items() if len(new_uids) > 1}


class UidMetrics:
    """Thread-safe, idempotency-aware registry of legacy collisions and read degradations.

    A *collision* is recorded once per distinct colliding ``legacy_uid``.
    A *degradation* is recorded once per distinct ``(source, key, uid)`` record: replaying the same
    record (for example, re-adding/re-reading the same cached object) never double-counts it.
    """

    def __init__(self) -> None:
        """Initialize empty counters and dedupe sets."""
        self._lock = threading.RLock()
        self._collisions: Dict[str, Tuple[str, ...]] = {}
        self._degradations: Dict[Tuple[str, str, str], str] = {}

    def record_collision(self, legacy_uid: str, new_uids: Iterable[str]) -> bool:
        """Record one distinct ambiguous legacy uid and the distinct new uids colliding on it.

        Returns:
            True if this is the first time ``legacy_uid`` was recorded, False on a replay.
        """
        with self._lock:
            if legacy_uid in self._collisions:
                return False
            distinct = tuple(sorted(set(new_uids)))
            if len(distinct) < 2:
                return False
            self._collisions[legacy_uid] = distinct
            return True

    def record_degradation(self, *, source: str, key: str, uid: str, reason: str) -> bool:
        """Record one distinct legacy/invalid read, deduplicating replays of the same record.

        Returns:
            True if counted, False if this exact record was already recorded.
        """
        dedupe_key = (source, key, uid)
        with self._lock:
            if dedupe_key in self._degradations:
                # Same record replayed under a (possibly new) fallback label: idempotent, no count.
                return False
            self._degradations[dedupe_key] = reason
            return True

    @property
    def collision_count(self) -> int:
        """Number of distinct legacy uids known to collide."""
        with self._lock:
            return len(self._collisions)

    @property
    def degradation_count(self) -> int:
        """Number of distinct records that required a legacy/invalid fallback."""
        with self._lock:
            return len(self._degradations)

    def collisions(self) -> Dict[str, List[str]]:
        """Return a copy of the recorded collisions mapping."""
        with self._lock:
            return {legacy_uid: list(new_uids) for legacy_uid, new_uids in self._collisions.items()}

    def degradations(self) -> List[Dict[str, str]]:
        """Return a sorted copy of the recorded degradation records."""
        with self._lock:
            return [
                {"source": source, "key": key, "uid": uid, "reason": reason}
                for (source, key, uid), reason in sorted(self._degradations.items())
            ]

    def snapshot(self) -> Dict[str, Any]:
        """Return a JSON-serializable snapshot for dashboards/APIs."""
        with self._lock:
            return {
                "collision_count": len(self._collisions),
                "degradation_count": len(self._degradations),
                "collisions": self.collisions(),
                "degradations": self.degradations(),
            }

    def reset(self) -> None:
        """Clear all recorded metrics (mainly useful for tests and dashboards)."""
        with self._lock:
            self._collisions.clear()
            self._degradations.clear()


# Process-wide default registry; stores and the web board share this instance.
METRICS = UidMetrics()


class UidIndex:
    """Per-store alias index mapping ambiguous legacy uids to unambiguous new uids.

    Writes always land under the new uid. Reads by legacy uid (existing caches/historical diffs)
    are served through this alias map. The first time an ambiguous legacy uid is seen to resolve to
    more than one distinct new uid, a collision is recorded (once) in :data:`METRICS`.
    """

    def __init__(self, source: str, metrics: Optional[UidMetrics] = None) -> None:
        """Initialize an index for a given store source label."""
        self.source = source
        self.metrics = metrics if metrics is not None else METRICS
        self._lock = threading.RLock()
        self._aliases: Dict[str, str] = {}
        self._collision_members: Dict[str, set] = {}

    def register(self, *, legacy_uid: str, new_uid: str, key: str = "") -> bool:
        """Idempotently register the alias ``legacy_uid -> new_uid`` and detect collisions.

        Returns:
            True if this registration revealed a previously unknown collision, else False.
        """
        with self._lock:
            members = self._collision_members.setdefault(legacy_uid, set())
            members.add(new_uid)
            existing = self._aliases.get(legacy_uid)
            if existing is None:
                self._aliases[legacy_uid] = new_uid
                return False
            if existing == new_uid:
                return False
            # A second distinct new uid maps to the same ambiguous legacy uid: real collision.
            return self.metrics.record_collision(legacy_uid, members)

    def resolve(self, legacy_uid: str) -> Optional[str]:
        """Return the registered new uid for a legacy uid, if known."""
        with self._lock:
            return self._aliases.get(legacy_uid)

    def aliases(self) -> Dict[str, str]:
        """Return a copy of the current alias mapping."""
        with self._lock:
            return dict(self._aliases)

    def record_read_fallback(self, *, uid: str, key: str, reason: str) -> bool:
        """Record a legacy/invalid read against the shared metrics, idempotently."""
        return self.metrics.record_degradation(source=self.source, key=key, uid=uid, reason=reason)
