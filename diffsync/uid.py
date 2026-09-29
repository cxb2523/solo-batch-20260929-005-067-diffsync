"""Central unique-id (uid) encode/decode logic for DiffSync.

Historically a uid was the plain ``"__".join(identifier_values)`` string. That format
collides whenever a model name or a primary-key value itself contains underscores --
for example ``("a_b", "c")`` and ``("a", "b_c")`` both render as ``a_b__c`` -- so the
local store, the Redis store and historical diffs could silently get "cross-wired".

This module is the single place that knows how uids are written and read:

* :func:`legacy_uid` reproduces the original, ambiguous, format byte-for-byte so that
  existing caches and historical diffs keep working.
* :func:`encode` / :func:`decode` implement the new, unambiguous, format. Each segment
  is *character-escaped* (so no segment can contain a structural separator) **and**
  wrapped in an explicit *length prefix* (double insurance: even a segment containing
  underscores can be sliced out without guessing). A leading :data:`NEW_UID_PREFIX`
  marker makes new uids trivially distinguishable from legacy ones while remaining
  human readable (``v2¶device¶7:device1`` still shows the raw values verbatim).

Collisions and malformed/legacy uids encountered on read are reported through
:data:`metrics` exactly once per logical record, which is what powers the uid board's
collision/degradation counters and guarantees that replaying one record never produces
two degradation entries.
"""

import threading
from typing import Any, Dict, List, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# Format constants
# ---------------------------------------------------------------------------

#: Separator of the *legacy* uid format; intentionally ambiguous.
LEGACY_SEPARATOR = "__"

#: First byte(s) of every uid produced by :func:`encode`; chosen so a legacy uid --
#: which is a free-form user string -- cannot legally start with it (legacy uid is a
#: raw user value, so a user *could* type this; such a value is simply treated as a
#: malformed uid on decode, which is reported as a degradation rather than crashing).
NEW_UID_PREFIX = "v2\u00b6"  # v2 + PILCROW SIGN

#: Structural separator between length-prefixed segments of a new uid.
SEGMENT_SEPARATOR = "\u00b7"  # MIDDLE DOT

#: Escaped escape.
ESCAPE = "\\"

#: Escaping map: backslash first, then every structural character, plus the newline
#: family so a uid can never span multiple lines.
_ESCAPE_MAP = {
    ESCAPE: "\\x5c",
    SEGMENT_SEPARATOR: "\\xM",
    NEW_UID_PREFIX[-1:]: "\\xP",
    "\r": "\\xR",
    "\n": "\\xN",
    "\t": "\\xT",
}

_UNESCAPE_MAP = {token: char for char, token in _ESCAPE_MAP.items()}


class IllegalUIDError(ValueError):
    """Raised when a string that claims to be a new-format uid fails strict parsing.

    A plain legacy uid does *not* raise this; only strings that start with
    :data:`NEW_UID_PREFIX` and then fail the grammar do.
    """


# ---------------------------------------------------------------------------
# Core (de)serialization
# ---------------------------------------------------------------------------


def _escape_segment(value: str) -> str:
    """Escape every structural character in a single segment."""
    for char, token in _ESCAPE_MAP.items():
        value = value.replace(char, token)
    return value


def _unescape_segment(value: str) -> str:
    """Reverse :func:`_escape_segment`; raise on an unknown escape sequence."""
    result: List[str] = []
    index = 0
    while index < len(value):
        char = value[index]
        if char == ESCAPE:
            token = value[index : index + 4]
            if token in _UNESCAPE_MAP:
                result.append(_UNESCAPE_MAP[token])
                index += 4
            else:
                token = value[index : index + 3]
                if token not in _UNESCAPE_MAP:
                    raise IllegalUIDError(f"Illegal escape sequence {token!r} in uid segment {value!r}")
                result.append(_UNESCAPE_MAP[token])
                index += 3
        else:
            result.append(char)
            index += 1
    return "".join(result)


def encode(segments: Sequence[Any]) -> str:
    r"""Build a new, unambiguous uid from ordered identifier segments.

    Each segment is stringified, character-escaped and wrapped in an explicit
    ``<length>:`` prefix; segments are joined with :data:`SEGMENT_SEPARATOR` and the
    whole uid starts with :data:`NEW_UID_PREFIX`.

    >>> encode(["device", "eth_0"])
    'v2¶6:device·5:eth_0'
    """
    parts = []
    for segment in segments:
        escaped = _escape_segment(str(segment))
        parts.append(f"{len(escaped)}:{escaped}")
    return NEW_UID_PREFIX + SEGMENT_SEPARATOR.join(parts)


def decode(uid: str) -> Tuple[str, ...]:
    """Strictly parse a uid produced by :func:`encode` back into its raw segments.

    Raises:
        IllegalUIDError: if ``uid`` is not a syntactically valid new-format uid.
    """
    if not isinstance(uid, str) or not uid.startswith(NEW_UID_PREFIX):
        raise IllegalUIDError(f"Not a new-format uid: {uid!r}")

    body = uid[len(NEW_UID_PREFIX) :]
    if not body:
        return ()

    segments: List[str] = []
    cursor = 0
    while cursor < len(body):
        colon = body.find(":", cursor)
        if colon == -1:
            raise IllegalUIDError(f"Missing length prefix in uid {uid!r}")
        length_text = body[cursor:colon]
        if not length_text.isdigit():
            raise IllegalUIDError(f"Non-numeric length prefix {length_text!r} in uid {uid!r}")
        length = int(length_text)
        start = colon + 1
        end = start + length
        if end > len(body):
            raise IllegalUIDError(f"Declared length {length} overruns uid {uid!r}")
        raw_segment = body[start:end]
        segments.append(_unescape_segment(raw_segment))
        cursor = end
        if cursor == len(body):
            break
        if body[cursor] != SEGMENT_SEPARATOR:
            raise IllegalUIDError(f"Expected segment separator at position {cursor} of uid {uid!r}")
        cursor += 1
    return tuple(segments)


def is_new_uid(uid: str) -> bool:
    """Return True if ``uid`` carries the new-format marker."""
    return isinstance(uid, str) and uid.startswith(NEW_UID_PREFIX)


def legacy_uid(segments: Sequence[Any]) -> str:
    """Reproduce the original ``__``-joined uid, byte for byte."""
    return LEGACY_SEPARATOR.join(str(segment) for segment in segments)


# ---------------------------------------------------------------------------
# Convenience wrappers used by the stores and the diff engine
# ---------------------------------------------------------------------------


def encode_model_uid(modelname: str, identifier_segments: Sequence[Any]) -> str:
    """Encode a store key that is globally unique for ``(modelname, identifiers)``.

    The model name is included as the first segment so that model names containing
    underscores (or colliding with another model's key space) cannot clash either.
    """
    return encode([modelname, *identifier_segments])


def split_model_uid(uid: str) -> Tuple[str, Tuple[str, ...]]:
    """Decode a model uid into ``(modelname, identifier_segments)``.

    Raises:
        IllegalUIDError: on a malformed new-format uid.
    """
    segments = decode(uid)
    if len(segments) < 1:
        raise IllegalUIDError(f"Model uid is missing its model name: {uid!r}")
    return segments[0], segments[1:]


def encode_diff_token(obj_type: str, keys: Dict[str, Any], identifier_order: Sequence[str]) -> str:
    """Build the unambiguous token used to index a :class:`~diffsync.diff.DiffElement`."""
    return encode([obj_type, *(keys[name] for name in identifier_order)])


# ---------------------------------------------------------------------------
# Collision / degradation metrics, idempotent per logical record
# ---------------------------------------------------------------------------


class UIDMetrics:
    """Process-wide registry of collision and degradation events.

    Both counters are backed by *sets of dedup keys* under a lock, so recording the
    same logical record twice (a replayed cache write, a retried read...) only ever
    contributes one entry.
    """

    def __init__(self) -> None:
        """Initialize empty event sets and a guarding lock."""
        self._lock = threading.Lock()
        self._collisions: set = set()
        self._degradations: set = set()

    @staticmethod
    def _normalize_location(location: Optional[str]) -> str:
        return location or "unknown"

    def record_collision(self, modelname: str, legacy_uid_value: str, location: Optional[str] = None) -> bool:
        """Record that a legacy uid resolves ambiguously for ``modelname``.

        Returns:
            True if this is the first time the tuple was seen, False on replay.
        """
        dedup_key = ("collision", self._normalize_location(location), modelname, legacy_uid_value)
        with self._lock:
            if dedup_key in self._collisions:
                return False
            self._collisions.add(dedup_key)
            return True

    def record_degradation(self, reason: str, detail: str, location: Optional[str] = None) -> bool:
        """Record a graceful degradation (malformed uid, legacy fallback, ...).

        Returns:
            True if this is the first time the tuple was seen, False on replay.
        """
        dedup_key = ("degradation", self._normalize_location(location), reason, detail)
        with self._lock:
            if dedup_key in self._degradations:
                return False
            self._degradations.add(dedup_key)
            return True

    def report_illegal_uid(self, uid: Any, location: Optional[str] = None) -> bool:
        """Convenience wrapper for malformed new-format uids encountered on read."""
        return self.record_degradation("illegal_uid", repr(uid), location=location)

    def collision_count(self) -> int:
        """Number of distinct legacy-uid collisions recorded."""
        with self._lock:
            return len(self._collisions)

    def degradation_count(self) -> int:
        """Number of distinct degradation events recorded."""
        with self._lock:
            return len(self._degradations)

    def snapshot(self) -> Dict[str, int]:
        """Serializable counters for dashboards/tests."""
        with self._lock:
            return {
                "collision_count": len(self._collisions),
                "degradation_count": len(self._degradations),
            }

    def reset(self) -> None:
        """Clear all recorded events (mainly used by tests and the demo board)."""
        with self._lock:
            self._collisions.clear()
            self._degradations.clear()


#: Shared, process-wide metric registry used by every store and by the Flask board.
metrics = UIDMetrics()
