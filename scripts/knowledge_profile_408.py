"""Durable, evidence-bound web-authored 408 learning profiles.

This store owns reviewed wording, not question outcomes or FSRS. Derived tutoring
refreshes copy these profiles without rewriting their semantic content.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import copy
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

ROOT_REL = Path(".private/knowledge-profiles-v1")
CURRENT_REL = ROOT_REL / "current.json"
SCHEMA = "cs408-reviewed-knowledge-profile-v1"
UPDATE_SCHEMA = "cs408-knowledge-profile-updates-v1"
INDEX_SCHEMA = "cs408-reviewed-knowledge-profile-index-v1"
SHA = re.compile(r"[0-9a-f]{64}")
CODE = re.compile(r"(?:DS|CO|OS|CN)\d{2}-\d{2}")
FORMAL = re.compile(r"(?:DS|CO|OS|CN)_(?:\d{4}|UNK)_\d{3}")


class ProfileError(ValueError):
    pass


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def subpoint_key(concept_key, title):
    """Stable identity under a canonical point; wording revisions change only content SHA."""
    import unicodedata
    label = re.sub(r"\s+", " ", unicodedata.normalize("NFKC", title)).strip().casefold()
    return "KSP-" + digest([concept_key, label])[:24]


def validate_subpoints(code, rows, bindings):
    require(isinstance(rows, list), "profile_subpoints_array_required")
    seen = set()
    for row in rows:
        require(isinstance(row, dict), "profile_subpoint_invalid")
        title = row.get("title")
        require(isinstance(title, str) and title.strip(), "profile_subpoint_title_required")
        key = subpoint_key(code, title)
        require(row.get("subpoint_id") == key and key not in seen,
                "profile_subpoint_identity_invalid")
        seen.add(key)
        for field in ("short_summary", "next_check"):
            require(isinstance(row.get(field), str) and row[field].strip(),
                    "profile_subpoint_text_required:" + field)
        require(len(row["short_summary"].encode()) <= 2400, "profile_subpoint_summary_too_large")
        require(isinstance(row.get("keywords"), list) and row["keywords"]
                and all(isinstance(v, str) and v.strip() for v in row["keywords"]),
                "profile_subpoint_keywords_required")
        require(isinstance(row.get("evidence"), list) and row["evidence"],
                "profile_subpoint_evidence_required")
        _source_refs(row.get("source_refs"), bindings)


def require(ok, reason):
    if not ok:
        raise ProfileError(reason)


def read(path):
    require(path.is_file() and not path.is_symlink(), "profile_file_missing_or_unsafe:" + str(path))
    return json.loads(path.read_text(encoding="utf-8"))


def atomic(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    require(not any(p.is_symlink() for p in (path, *path.parents)), "profile_destination_symlink")
    raw = encoded(value)
    fd, temporary = tempfile.mkstemp(prefix=".profile-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return hashlib.sha256(raw).hexdigest()


def content_write(root, value):
    sha = digest(value)
    path = root / "objects" / (sha + ".json")
    if path.exists():
        require(path.read_bytes() == encoded(value), "profile_content_address_conflict")
    else:
        atomic(path, value)
    return {"sha256": sha, "path": "objects/" + sha + ".json"}


def current_index(repo):
    path = Path(repo) / CURRENT_REL
    if not path.exists():
        return {"schema": INDEX_SCHEMA, "profiles": {}, "operations": {}, "generation": None}
    value = read(path)
    require(value.get("schema") == INDEX_SCHEMA and isinstance(value.get("profiles"), dict)
            and isinstance(value.get("operations"), dict), "profile_index_invalid")
    require(value.get("generation") == digest({k: v for k, v in value.items() if k != "generation"}),
            "profile_index_generation_mismatch")
    return value


def load_profiles(repo):
    root = Path(repo) / ROOT_REL
    index = current_index(repo)
    values = {}
    for code, ref in index["profiles"].items():
        require(CODE.fullmatch(code) and SHA.fullmatch(str(ref.get("sha256", ""))), "profile_reference_invalid")
        expected = "objects/" + ref["sha256"] + ".json"
        require(ref.get("path") == expected, "profile_reference_path_invalid")
        value = read(root / expected)
        require(digest(value) == ref["sha256"] and value.get("schema") == SCHEMA
                and value.get("concept_key") == code, "profile_object_binding_mismatch")
        values[code] = {**value, "version": ref["sha256"]}
    return {"generation": index["generation"], "profiles": values}


def _taxonomy(repo):
    result = {}
    for line in (Path(repo) / "知识点标签表.md").read_text(encoding="utf-8").splitlines():
        match = re.fullmatch(r"\s*-\s+((?:DS|CO|OS|CN)\d{2}-\d{2})\s+(.+?)\s*", line)
        if match:
            result[match[1]] = match[2]
    require(result, "profile_canonical_knowledge_table_missing")
    return result


def _updates(value):
    if isinstance(value, dict):
        require(value.get("schema") == UPDATE_SCHEMA, "profile_update_schema_invalid")
        value = value.get("updates")
    require(isinstance(value, list) and all(isinstance(row, dict) for row in value), "profile_updates_array_required")
    return value


def _source_refs(refs, bindings):
    require(isinstance(refs, list) and refs, "profile_source_refs_required")
    require(isinstance(bindings, dict), "profile_verified_evidence_bindings_required")
    for ref in refs:
        require(isinstance(ref, dict) and isinstance(ref.get("path"), str)
                and SHA.fullmatch(str(ref.get("sha256", ""))), "profile_source_ref_invalid")
        bound = bindings.get(ref["path"])
        expected = bound.get("sha256") if isinstance(bound, dict) else bound
        require(expected == ref["sha256"], "profile_source_ref_not_verified:" + ref["path"])
    return copy.deepcopy(refs)


def _question_refs(repo, refs, formal_bindings):
    require(isinstance(refs, list), "profile_question_refs_array_required")
    result = []
    for raw in refs:
        require(isinstance(raw, dict), "profile_question_ref_invalid")
        row = copy.deepcopy(raw)
        fid = row.get("formal_id")
        logical = row.get("logical_question_id")
        if logical and formal_bindings is not None:
            resolved = formal_bindings.get(logical)
            if isinstance(resolved, dict):
                resolved = resolved.get("formal_id")
            require(resolved and (not fid or fid == resolved), "profile_logical_question_binding_missing:" + str(logical))
            fid = row["formal_id"] = resolved
        require(fid or logical, "profile_question_identity_required")
        if not fid:
            # prepare can validate a future local ID; apply must bind it.
            require(formal_bindings is None, "profile_unresolved_logical_question")
            row.pop("obsidian_uri", None)
            row.pop("card_path", None)
            result.append(row)
            continue
        require(isinstance(fid, str) and FORMAL.fullmatch(fid), "profile_formal_id_invalid")
        card = Path(repo) / (fid + ".md")
        require(card.is_file() and not card.is_symlink(), "profile_formal_card_missing:" + fid)
        actual_sha = hashlib.sha256(card.read_bytes()).hexdigest()
        row["card_path"] = str(card.resolve())
        row["card_relpath"] = card.name
        row["card_sha256"] = actual_sha
        # URI points to a real safe card. A formal ID is not evidence of a true
        # exam year; supplied canonical identity remains source metadata only.
        from question_visual_links_408 import visual_link
        row.update(visual_link(Path(repo), fid))
        result.append(row)
    return result


def validate_updates(repo, updates, expected_points=None, evidence_bindings=None,
                     formal_bindings=None, check_versions=True):
    repo = Path(repo).resolve()
    taxonomy = _taxonomy(repo)
    index = current_index(repo)
    result, seen = [], set()
    for raw in _updates(updates):
        code = raw.get("concept_key")
        require(code in taxonomy and code not in seen, "profile_unknown_or_duplicate_concept:" + str(code))
        seen.add(code)
        require("expected_previous_version" in raw, "profile_expected_previous_version_required")
        previous = raw["expected_previous_version"]
        require(previous is None or (isinstance(previous, str) and SHA.fullmatch(previous)), "profile_previous_version_invalid")
        if check_versions:
            require(previous == index["profiles"].get(code, {}).get("sha256"), "profile_version_conflict:" + code)
        for field in ("overview", "short_summary", "next_check"):
            require(isinstance(raw.get(field), str) and raw[field].strip(), "profile_text_required:" + field)
        require(len(raw["short_summary"].encode("utf-8")) <= 2400, "profile_short_summary_exceeds_2400_bytes")
        require(len(encoded(raw)) <= 512 * 1024, "profile_update_too_large")
        for field in ("timeline", "progress"):
            require(isinstance(raw.get(field), list), "profile_list_required:" + field)
        aliases = raw.get("aliases", [])
        require(isinstance(aliases, list) and all(isinstance(a, str) and a.strip() for a in aliases), "profile_aliases_invalid")
        for alias in aliases:
            require(not CODE.fullmatch(alias) or alias == code, "profile_alias_cannot_renumber_concept")
        value = copy.deepcopy(raw)
        require(value.get("authorship_origin", "web_final_semantic_content") in {
            "web_final_semantic_content", "local_explicitly_authorized_semantic_content"
        }, "profile_authorship_origin_invalid")
        validate_subpoints(code, raw.get("subpoints", []), evidence_bindings)
        value.update(source_refs=_source_refs(raw.get("source_refs"), evidence_bindings),
                     question_refs=_question_refs(repo, raw.get("question_refs", []), formal_bindings),
                     aliases=sorted(set(aliases)))
        result.append(value)
    if expected_points is not None:
        require(seen == set(expected_points), "profile_updates_must_cover_all_affected_points")
    alias_owner = {}
    for code, label in taxonomy.items():
        alias_owner.setdefault(label.strip().casefold(), set()).add(code)
    stored = load_profiles(repo)["profiles"]
    for code, profile in stored.items():
        if code not in seen:
            for alias in profile.get("aliases", []):
                alias_owner.setdefault(alias.strip().casefold(), set()).add(code)
    for value in result:
        code = value["concept_key"]
        for alias in value["aliases"]:
            other = alias_owner.setdefault(alias.strip().casefold(), set())
            require(not other or other == {code}, "profile_alias_ambiguous:" + alias)
            other.add(code)
    return result


@contextmanager
def _lock(root):
    root.mkdir(parents=True, exist_ok=True)
    require(not any(p.is_symlink() for p in (root, *root.parents)), "profile_root_symlink")
    with (root / ".lock").open("a+b") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield


def apply_updates(repo, updates, operation_id, formal_bindings=None,
                  evidence_bindings=None, expected_points=None):
    repo = Path(repo).resolve()
    require(isinstance(operation_id, str) and operation_id, "profile_operation_identity_required")
    raw_updates = _updates(updates)
    request_sha = digest({"updates": raw_updates, "formal_bindings": formal_bindings or {},
                          "expected_points": sorted(expected_points) if expected_points is not None else None})
    op_key = hashlib.sha256(operation_id.encode()).hexdigest()
    root = repo / ROOT_REL
    receipt_path = root / "receipts" / (op_key + ".json")
    journal_path = root / "operations" / (op_key + ".json")
    with _lock(root):
        if receipt_path.exists():
            receipt = read(receipt_path)
            require(receipt.get("request_sha256") == request_sha, "profile_operation_content_conflict")
            for ref in receipt["profiles"].values():
                require(digest(read(root / ref["path"])) == ref["sha256"], "profile_committed_object_drift")
            return {**receipt, "status": "already_applied", "receipt_path": str(receipt_path)}
        index = current_index(repo)
        prior_op = index["operations"].get(op_key)
        if prior_op:
            require(prior_op["request_sha256"] == request_sha, "profile_committed_operation_conflict")
            journal = read(journal_path)
            require(journal["request_sha256"] == request_sha, "profile_operation_journal_conflict")
            profile_refs = journal["profiles"]
            generation = journal["published_generation"]
        else:
            normalized = validate_updates(repo, raw_updates, expected_points=expected_points,
                                          evidence_bindings=evidence_bindings,
                                          formal_bindings=formal_bindings or {}, check_versions=True)
            # Cold boundary only: bind each authored profile to the actual
            # post-formal point evidence. Later unrelated points can change
            # without making this profile stale; changed same-point evidence
            # must never silently reuse old reviewed wording as current.
            from exam_tutoring_context_408 import _compute_snapshot
            snapshot = _compute_snapshot(repo, dt.datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat())
            point_records = snapshot["point_artifacts"]["index"]["point_records"]
            profile_refs = {}
            timestamp = dt.datetime.now(dt.timezone.utc).isoformat()
            for value in normalized:
                code = value["concept_key"]
                profile = {**value, "schema": SCHEMA,
                           "origin": value.get("authorship_origin", "web_final_semantic_content"),
                           "operation_id": operation_id, "accepted_at": timestamp,
                           "evidence_fingerprint": point_records.get(code, {}).get("sha256"),
                           "independent_mastery_inferred": False}
                require(profile["evidence_fingerprint"], "profile_point_has_no_materialized_evidence:" + code)
                point_entry = snapshot["point_artifacts"]["index"]["points"][code]
                point_summary = snapshot["point_artifacts"]["objects"][point_entry["ref"]]
                require(point_summary.get("record_count", 0) > 0,
                        "profile_point_has_no_materialized_evidence:" + code)
                profile_refs[code] = content_write(root, profile)
            generation = index["generation"]
            journal = {"schema": "cs408-knowledge-profile-operation-v1", "operation_id": operation_id,
                       "request_sha256": request_sha, "profiles": profile_refs,
                       "previous_generation": generation}
            next_index = copy.deepcopy(index)
            next_index["profiles"].update(profile_refs)
            next_index["operations"][op_key] = {"request_sha256": request_sha,
                                              "generation_before_publication": generation}
            next_index.pop("generation", None)
            next_index["generation"] = digest(next_index)
            journal["published_generation"] = next_index["generation"]
            atomic(journal_path, journal)
            atomic(repo / CURRENT_REL, next_index)
            generation = next_index["generation"]
        receipt = {"schema": "cs408-knowledge-profile-commit-v1", "status": "applied",
                   "operation_id": operation_id, "request_sha256": request_sha,
                   "profiles": profile_refs, "profile_count": len(profile_refs),
                   "generation": generation, "formal_question_write_count": 0,
                   "fsrs_write_count": 0, "model_call_count": 0}
        atomic(receipt_path, receipt)
    return {**receipt, "receipt_path": str(receipt_path)}


def status(repo):
    value = load_profiles(repo)
    return {"status": "ready", "schema": INDEX_SCHEMA, "generation": value["generation"],
            "profile_count": len(value["profiles"]),
            "versions": {code: row["version"] for code, row in value["profiles"].items()},
            "write_count": 0}


def operation_applied(repo, operation_id):
    """Read-only recovery gate, including pointer-published/receipt-missing commits.

    The caller still invokes apply_updates with its original payload to verify
    request identity and recover the exact durable receipt.
    """
    key = hashlib.sha256(operation_id.encode()).hexdigest()
    root = Path(repo) / ROOT_REL
    receipt = root / "receipts" / (key + ".json")
    if receipt.is_file():
        value = read(receipt)
        require(value.get("operation_id") == operation_id, "profile_operation_identity_drift")
        return True
    return key in current_index(repo)["operations"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("status", "get"))
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--code")
    args = parser.parse_args()
    result = status(args.repo) if args.command == "status" else load_profiles(args.repo)["profiles"].get(args.code)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
