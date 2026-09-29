"""Flask mini-site: live side-by-side board for diffsync uid encoding.

Run with ``python app.py`` and open http://127.0.0.1:5000 .

The page shows two columns -- the legacy "__"-joined uid and the new
length-prefixed/escaped uid -- for the model name / primary key entered, plus the
process-wide collision and degradation counters. Editing a field refreshes both
columns immediately; an illegal uid only marks its column red and never interrupts.

A LocalStore-backed demo (with a pre-seeded legacy cache record and two underscore
collision rows) is exercised through JSON endpoints so the counters are real, not
mocked.
"""

from typing import Any, Dict, List

from flask import Flask, jsonify, render_template, request

from diffsync import DiffSyncModel, uid
from diffsync.exceptions import ObjectAlreadyExists
from diffsync.store.local import LocalStore

app = Flask(__name__)


# ---------------------------------------------------------------------------
# Demo model + store
# ---------------------------------------------------------------------------
class DemoInterface(DiffSyncModel):
    """Two-identifier model that reproduces the underscore collision boundary."""

    _modelname = "interface"
    _identifiers = ("device_name", "name")

    device_name: str
    name: str


def _build_demo_store() -> LocalStore:
    """Build a fresh demo store, including a legacy-only record and collision rows."""
    store = LocalStore(name="demo-local")

    # 1) A record living only under a legacy uid key (as inherited from an old cache).
    legacy_only = DemoInterface(device_name="legacy_dev", name="eth0")
    # Inject it under the plain legacy uid to prove old caches still read back.
    legacy_key = legacy_only.get_unique_id()
    if not legacy_only.adapter:
        legacy_only.adapter = store.adapter
    store._data["interface"][legacy_key] = legacy_only  # pylint: disable=protected-access

    # 2) Two distinct rows whose legacy uids collide ("a__b__c").
    store.add(obj=DemoInterface(device_name="a", name="b__c"))
    store.add(obj=DemoInterface(device_name="a__b", name="c"))

    return store


DEMO_STORE = _build_demo_store()


# ---------------------------------------------------------------------------
# Pure board calculations (shared by the page and tests)
# ---------------------------------------------------------------------------
def build_columns(modelname: str, identifier_values: List[str]) -> Dict[str, Any]:
    """Compute both uid columns and their validity/collision metadata."""
    segments = list(identifier_values)
    legacy_value = uid.legacy_uid(segments)
    new_value = uid.encode([modelname, *segments])

    # Whether this exact (model, identifier tuple) shares its legacy uid with the
    # other demo collision row.
    legacy_collides = _legacy_is_ambiguous(modelname, segments)

    return {
        "model": modelname,
        "identifiers": segments,
        "legacy": {"value": legacy_value, "valid": True, "collides": legacy_collides},
        "new": {"value": new_value, "valid": True, "collides": False},
    }


def _legacy_is_ambiguous(modelname: str, segments: List[str]) -> bool:
    """Return True if the legacy uid for these segments is shared by another stored row.

    The check compares every stored row's *actual* identifier tuple against the legacy
    uid that these segments would produce -- this is true regardless of which row won
    the first-writer alias race.
    """
    if modelname not in DEMO_STORE.get_all_model_names():
        return False
    legacy_value = uid.legacy_uid(segments)
    collisions = 0
    for obj in DEMO_STORE.get_all(model=modelname):
        obj_segments = [str(obj.get_identifiers()[key]) for key in obj._identifiers]  # pylint: disable=protected-access
        if uid.legacy_uid(obj_segments) == legacy_value:
            collisions += 1
    return collisions > 1


def inspect_uid(raw_uid: str, expected_model: str) -> Dict[str, Any]:
    """Decode a user-pasted uid for a column; flag malformed input without raising."""
    result: Dict[str, Any] = {"input": raw_uid, "kind": "unknown", "valid": False}
    if uid.is_new_uid(raw_uid):
        result["kind"] = "new"
        try:
            modelname, segments = uid.split_model_uid(raw_uid)
            matches = modelname == expected_model
            result.update(
                valid=True,
                model=modelname,
                identifiers=list(segments),
                model_matches=matches,
            )
            if not matches:
                # Syntactically valid but aimed at another model: graceful degradation.
                uid.metrics.record_degradation(
                    "model_mismatch", f"{modelname}!={expected_model}:{raw_uid!r}", location="board"
                )
                result["error"] = f"model mismatch: {modelname} != {expected_model}"
        except uid.IllegalUIDError:
            result["valid"] = False
            result["error"] = "malformed new-format uid"
            uid.metrics.report_illegal_uid(raw_uid, location="board")
    else:
        result["kind"] = "legacy"
        result["valid"] = True
        result["model"] = expected_model
        result["identifiers"] = raw_uid.split(uid.LEGACY_SEPARATOR)
    return result


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.route("/")
def index() -> str:
    """Render the two-column board."""
    return render_template("uid_board.html")


@app.route("/api/compute")
def api_compute():
    """Return legacy + new uids for a model name and list of identifier values."""
    modelname = request.args.get("model", "interface")
    raw_ids = request.args.get("identifiers", "device1,eth0")
    identifiers = [piece for piece in raw_ids.split(",")]
    return jsonify(build_columns(modelname, identifiers))


@app.route("/api/decode")
def api_decode():
    """Decode a pasted uid; illegal uids are marked invalid and counted once."""
    raw_uid = request.args.get("uid", "")
    expected_model = request.args.get("model", "interface")
    return jsonify(inspect_uid(raw_uid, expected_model))


@app.route("/api/metrics")
def api_metrics():
    """Expose collision/degradation counters plus the live demo store contents."""
    payload = uid.metrics.snapshot()
    payload["stored"] = sorted(f"{obj.device_name}/{obj.name}" for obj in DEMO_STORE.get_all(model="interface"))
    return jsonify(payload)


@app.route("/api/replay", methods=["POST"])
def api_replay():
    """Replay a record write into the demo store; counters stay idempotent."""
    body = request.get_json(silent=True) or {}
    device_name = body.get("device_name", "a")
    name = body.get("name", "b__c")
    try:
        DEMO_STORE.add(obj=DemoInterface(device_name=device_name, name=name))
    except ObjectAlreadyExists:
        # Same logical record arriving twice is an idempotent no-op: keep the existing
        # row and never emit a second collision/degradation entry.
        pass
    return jsonify(uid.metrics.snapshot())


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
