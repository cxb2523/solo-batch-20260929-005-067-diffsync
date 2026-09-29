"""Flask debugging board for diffsync uid encoding.

Run with ``python app.py`` and open the board URL printed on startup (default http://127.0.0.1:5050/).

The page shows a live, side-by-side comparison of:

* the **legacy** uid (historical ``"__".join(...)`` output), still read back verbatim from
  existing caches and historical diffs;
* the **new** uid (escaped fields + length prefixes, ``v2__`` marker), which is collision-free;
* process-wide collision and degradation counters from :mod:`diffsync.uid`.

Everything interactive is served as JSON by :http:get:`/api/analyze`; invalid uids are reported
per-pane and highlighted in red in the browser without interrupting anything.
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

from flask import Flask, jsonify, render_template, request

from diffsync.uid import (
    METRICS,
    UidIndex,
    decode,
    encode,
    find_legacy_collisions,
    legacy_encode,
)

app = Flask(__name__)

# A single in-memory index lets the board demonstrate alias/collision behavior for the rows that
# the user has explored in this process, mirroring what a store does internally.
_BOARD_INDEX = UidIndex(source="board")


def _split_fields(raw: str) -> List[str]:
    """Parse the comma/newline separated field input, keeping empty fields (they are legal)."""
    if raw == "":
        return []
    return [part.strip() for part in raw.replace("\n", ",").split(",")]


def _analyze_row(*, model: str, fields_raw: str) -> Dict[str, Any]:
    """Compute one comparison row for the left (input) pane."""
    fields = _split_fields(fields_raw)
    # The store key also incorporates the model name; include it in the legacy/new comparison so a
    # model name containing underscores is exercised too.
    key_fields = [model, *fields] if model else list(fields)
    legacy_uid = legacy_encode(key_fields)
    new_uid = encode(key_fields)
    decoded, new_flag, invalid = decode(new_uid)
    result: Dict[str, Any] = {
        "model": model,
        "fields": fields,
        "legacy_uid": legacy_uid,
        "new_uid": new_uid,
        "new_decodes": new_flag and not invalid and decoded == key_fields,
        "decoded_fields": decoded,
    }
    # Register the alias so a second row with the same legacy uid shows up as a collision.
    _BOARD_INDEX.register(legacy_uid=legacy_uid, new_uid=new_uid, key=f"{model}:{new_uid}")
    return result


def _analyze_pasted_uid(uid: str) -> Dict[str, Any]:
    """Analyze an arbitrary uid pasted in the right pane; never raises on bad input."""
    fields, new_flag, invalid = decode(uid)
    legacy_literal = None if new_flag else fields[0]
    aliased = None if not new_flag else _BOARD_INDEX.resolve(legacy_encode(fields))
    return {
        "uid": uid,
        "scheme": "invalid" if invalid else ("new" if new_flag else "legacy"),
        "fields": fields,
        "invalid": invalid,
        "legacy_literal": legacy_literal,
        "alias_target": aliased,
        "readable": (not invalid) and (new_flag or True),
    }


@app.route("/")
def index() -> str:
    """Render the two-pane uid board."""
    return render_template("uid_board.html")


@app.route("/api/analyze", methods=["GET", "POST"])
def api_analyze() -> Any:
    """Compare left (model + fields) and right (raw uid) inputs and return JSON."""
    payload: Dict[str, Any] = request.get_json(silent=True) or request.args
    model = str(payload.get("model", ""))
    fields_raw = str(payload.get("fields", ""))
    pasted_uids = payload.get("uids", "")
    if isinstance(pasted_uids, str):
        uid_lines = [line.strip() for line in pasted_uids.splitlines() if line.strip()]
    else:
        uid_lines = [str(item) for item in pasted_uids]

    left = _analyze_row(model=model, fields_raw=fields_raw)
    right = [_analyze_pasted_uid(uid) for uid in uid_lines]

    # Collisions among rows seen in this board session (legacy uid shared by distinct new uids).
    samples: List[Tuple[str, str]] = []
    aliases = _BOARD_INDEX.aliases()
    collision_members = _BOARD_INDEX._collision_members  # noqa: SLF001 - board-only introspection
    for legacy_uid, new_uids in collision_members.items():
        for new_uid in new_uids:
            samples.append((new_uid, legacy_uid))
    collisions = find_legacy_collisions(samples)

    return jsonify(
        {
            "left": left,
            "right": right,
            "board_collisions": collisions,
            "metrics": METRICS.snapshot(),
            "aliases": aliases,
        }
    )


@app.route("/api/metrics")
def api_metrics() -> Any:
    """Return the current process-wide collision/degradation counters."""
    return jsonify(METRICS.snapshot())


@app.route("/api/reset", methods=["POST"])
def api_reset() -> Any:
    """Clear process metrics and the board's demonstration index."""
    METRICS.reset()
    _BOARD_INDEX._aliases.clear()  # noqa: SLF001
    _BOARD_INDEX._collision_members.clear()  # noqa: SLF001
    return jsonify({"ok": True})


if __name__ == "__main__":
    import os

    port = int(os.environ.get("UID_BOARD_PORT", "5050"))
    app.run(host="127.0.0.1", port=port, debug=False)
