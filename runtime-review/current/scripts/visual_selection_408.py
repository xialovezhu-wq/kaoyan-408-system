"""Version-bound selection metadata for already disclosed 408 figures."""
from __future__ import annotations

import hashlib
import json
import re

SCHEMA = "cs408-visual-selection-v1"
MARKER = re.compile(rb'<script type="application/json" data-cs408-selection>([^<]*)</script>')


def digest(value):
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def selection_binding(identity, source_version, view, hint_level, state_ids, public_payload):
    ids = list(state_ids)
    if (not isinstance(identity, str) or not identity or view not in {"protected", "after_attempt", "direct", "concept"}
            or type(hint_level) is not int or not 0 <= hint_level <= 5
            or source_version is not None and (not isinstance(source_version, str) or not source_version)
            or any(not isinstance(key, str) or not key or len(key) > 100 for key in ids)
            or len(ids) != len(set(ids))):
        raise ValueError("visual_selection_scope_invalid")
    body = {"schema": SCHEMA, "identity": identity, "source_version": source_version, "view": view,
            "hint_level": hint_level, "state_ids": ids, "content_sha256": digest(public_payload)}
    return body | {"binding_sha256": digest(body)}


def marker(binding):
    data = json.dumps(binding, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")
    return '<script type="application/json" data-cs408-selection>' + data + '</script>'


def from_html(raw):
    """Recover scope from archived bytes. HTML cannot prove the viewer's last click."""
    matches = MARKER.findall(raw)
    if len(matches) != 1:
        return None
    data = json.loads(matches[0])
    if not isinstance(data, dict):
        return None
    body = {k: value for k, value in data.items() if k != "binding_sha256"}
    if data.get("schema") != SCHEMA or data.get("binding_sha256") != digest(body):
        return None
    required = {"schema", "identity", "source_version", "view", "hint_level", "state_ids", "content_sha256"}
    if body.keys() != required:
        return None
    return data
