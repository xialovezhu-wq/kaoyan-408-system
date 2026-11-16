"""Bind portable web teaching proposals to verified native conversation evidence.

No semantic inference, profile writes, or learning events happen here. The
returned updates are a deterministic derivative; the submitted bytes stay intact.
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import knowledge_profile_408 as profiles
import sol_curation_decision_408 as decisions

SCHEMA = "cs408-portable-visual-teaching-plans-v1"
FILENAME = "portable_visual_teaching_plans.json"
ADAPTER_SCHEMA = "cs408-portable-visual-teaching-adaptation-v1"


class PortableVisualError(ValueError):
    pass


def require(ok, reason):
    if not ok:
        raise PortableVisualError(reason)


def digest(value):
    return hashlib.sha256(profiles.encoded(value)).hexdigest()


def reopen_evidence(repo, locator, expected_sha, *, archive_only=False):
    """Reopen one named original; archive fallback uses its exact locator note."""
    if not archive_only:
        try:
            return decisions.reopen_direct_evidence(locator, expected_sha)
        except (OSError, ValueError, RuntimeError):
            if not str(locator).startswith("cs408-conversation-package://"):
                raise
    import archive_408_conversation_package as archive
    import conversation_package_408 as packages
    require(str(locator).startswith(packages.LOCATOR_PREFIX), "portable_native_package_required")
    package_id = locator.removeprefix(packages.LOCATOR_PREFIX)
    proof = archive.reopen_archived_package(repo=repo, package_id=package_id,
        requested_relative_paths=["conversation.json", "source.json"])
    require(proof["package_sha256"] == expected_sha, "portable_archive_package_sha_drift")
    note = archive._read_locator_note(Path(repo) / archive.LOCATOR_NOTE_ROOT / (package_id + ".md"))
    folder = Path(archive.ARCHIVE_ROOT) / note["raw_archive_relpath"]
    manifest_raw = (folder / "manifest.json").read_bytes()
    require(hashlib.sha256(manifest_raw).hexdigest() == proof["manifest_sha256"], "portable_archive_manifest_drift")
    manifest = json.loads(manifest_raw)
    require(manifest["canonical_sha256"] == expected_sha and manifest["package_id"] == package_id,
            "portable_archive_package_drift")
    objects = {row["relative_path"]: row["json"] for row in proof["requested_objects"]}
    return {"kind": "cs408_conversation_package_v1", "sha256": expected_sha,
            "missing_fields": manifest.get("missing_fields", []), "package": {
                "package_dir": str(folder), "manifest": manifest,
                "conversation": objects["conversation.json"], "source": objects["source.json"],
                "archive_proof": proof}}


def _previous_ref(repo, ref):
    """An explicit revision may relocate the same old original, never its text."""
    result = copy.deepcopy(ref)
    path = Path(ref["path"])
    if path.is_file() and not any(p.is_symlink() for p in (path, *path.parents)):
        return result
    require(path.name == "conversation.json", "portable_previous_source_requires_rebind")
    import archive_408_conversation_package as archive
    package_id = path.parent.name
    proof = archive.reopen_archived_package(repo=repo, package_id=package_id,
        requested_relative_paths=["conversation.json"])
    selected = proof["requested_objects"]
    require(len(selected) == 1 and selected[0]["sha256"] == ref["sha256"], "portable_previous_archive_sha_drift")
    note = archive._read_locator_note(Path(repo) / archive.LOCATOR_NOTE_ROOT / (package_id + ".md"))
    target = Path(archive.ARCHIVE_ROOT) / note["raw_archive_relpath"] / "conversation.json"
    require(target.is_file() and not any(p.is_symlink() for p in (target, *target.parents))
            and hashlib.sha256(target.read_bytes()).hexdigest() == ref["sha256"], "portable_previous_archive_source_drift")
    result["path"] = str(target)
    return result


def _prior(repo, update):
    version = update.get("expected_previous_version")
    return profiles.load_profile_version(repo, update["concept_key"], version) if version else None


def _target(update, subpoint_id):
    if subpoint_id is None:
        return update
    matches = [row for row in update.get("subpoints", []) if row.get("subpoint_id") == subpoint_id]
    require(len(matches) == 1, "portable_subpoint_not_in_update")
    return matches[0]


def _bind_old(plan, bindings):
    for ref in plan.get("source_refs", []):
        path = Path(ref.get("path", ""))
        require(path.is_absolute() and not any(p.is_symlink() for p in (path, *path.parents)),
                "portable_previous_source_unsafe")
        require(bindings.get(str(path), ref["sha256"]) == ref["sha256"], "portable_binding_conflict")
        bindings[str(path)] = ref["sha256"]
    profiles.validate_teaching_plan(plan, bindings)


def _retain_declared_focus(repo, updates, bindings):
    """Reopen only an explicitly selected immutable predecessor's focus.

    This is a transport for retaining reviewed content, not a trust path for
    arbitrary web-supplied local filenames or a semantic review of new claims.
    """
    result = copy.deepcopy(updates)
    audit = []
    for update in result.get("updates", []):
        targets = [update, *update.get("subpoints", [])]
        selected = [target for target in targets
                    if isinstance(target.get("teaching_focus_update"), dict)
                    and target["teaching_focus_update"].get("action") == "retain"]
        if not selected:
            continue
        require(update.get("expected_previous_version"), "portable_focus_previous_version_required")
        prior = _prior(repo, update)
        old_targets = {row.get("subpoint_id"): row for row in [prior, *prior.get("subpoints", [])]}
        for target in selected:
            sid = target.get("subpoint_id")
            previous = old_targets.get(sid, {})
            focus = previous.get("teaching_focus")
            require(focus is not None, "portable_previous_focus_required")
            require("teaching_focus" not in target or target["teaching_focus"] == focus,
                    "portable_focus_retain_no_replacement")
            # Only the source refs read from the hash-bound native predecessor
            # may add bindings. Quotes and hashes are revalidated below.
            verified = dict(bindings)
            for ref in focus.get("source_refs", []):
                path = Path(ref.get("path", ""))
                require(path.is_absolute() and not any(p.is_symlink() for p in (path, *path.parents)),
                        "portable_previous_focus_source_unsafe")
                require(verified.get(str(path), ref["sha256"]) == ref["sha256"], "portable_binding_conflict")
                verified[str(path)] = ref["sha256"]
            profiles.validate_teaching_focus(focus, verified)
            target["teaching_focus"] = copy.deepcopy(focus)
            profiles._validate_teaching_focus_continuity(target, previous, sid or update["concept_key"])
            bindings.update(verified)
            audit.append({"concept_key": update["concept_key"], "subpoint_id": sid, "action": "retain",
                "previous_profile_version": update["expected_previous_version"],
                "retained_focus_sha256": digest(focus), "source_bindings": copy.deepcopy(focus["source_refs"])})
    return result, audit


def _native_ref(repo, ref, captures, enhancement_items, reopened, bindings, *, archive_only):
    require(isinstance(ref, dict) and set(ref) == {
        "id", "capture_id", "conversation_locator", "input_member_path", "conversation_sha256", "turns"
    }, "portable_source_fields_invalid")
    cid = ref["capture_id"]
    require(isinstance(cid, str) and cid in captures, "portable_capture_out_of_scope")
    require(enhancement_items[cid].get("action") != "needs_user", "portable_unreviewed_capture")
    capture = captures[cid]
    require(ref["conversation_locator"] == capture["evidence_locator"], "portable_conversation_locator_drift")
    if cid not in reopened:
        opened = reopen_evidence(repo, capture["evidence_locator"], capture["evidence_sha256"], archive_only=archive_only)
        require(opened.get("kind") == "cs408_conversation_package_v1", "portable_native_package_required")
        reopened[cid] = opened["package"]
    package = reopened[cid]
    package_id = package["manifest"]["package_id"]
    expected_member = f"captures/{cid}/packages/{package_id}/conversation.json"
    require(ref["input_member_path"] == expected_member, "portable_input_member_drift")
    path = Path(package["package_dir"]) / "conversation.json"
    require(path.is_absolute() and path.is_file()
            and not any(p.is_symlink() for p in (path, *path.parents)), "portable_conversation_path_unsafe")
    rows = [row for row in package["manifest"]["files"] if row["relative_path"] == "conversation.json"]
    require(len(rows) == 1, "portable_conversation_manifest_missing")
    raw = path.read_bytes()
    actual = hashlib.sha256(raw).hexdigest()
    require(actual == rows[0]["sha256"] == ref["conversation_sha256"], "portable_conversation_sha_drift")
    require(len(raw) == rows[0]["byte_count"], "portable_conversation_size_drift")
    require(bindings.get(str(path), actual) == actual, "portable_binding_conflict")
    bindings[str(path)] = actual
    return {"id": ref["id"], "path": str(path), "sha256": actual,
            # This is only a proposed write until enhancement.apply finishes all
            # formal items; prepare never writes a reviewed profile.
            "review_state": "reviewed", "turns": copy.deepcopy(ref["turns"])}


def _bind_retained_claim_sources(target, predecessor, bindings):
    """Revalidate native source bytes for an exactly retained historical claim."""
    prior_claims = {claim["claim_id"]: claim for claim in predecessor.get("claims", [])}
    for claim in target.get("claims", []):
        if claim != prior_claims.get(claim["claim_id"]):
            continue
        verified = dict(bindings)
        for ref in prior_claims[claim["claim_id"]]["source_refs"]:
            path = Path(ref["path"])
            require(path.is_absolute() and not any(parent.is_symlink() for parent in (path, *path.parents)),
                    "portable_retained_claim_source_unsafe")
            require(verified.get(str(path), ref["sha256"]) == ref["sha256"], "portable_binding_conflict")
            verified[str(path)] = ref["sha256"]
        profiles._teaching_sources(claim, bindings=verified)
        bindings.update(verified)


def adapt(*, repo, package_root, manifest, enhancement, updates, evidence_bindings, archive_only=False):
    """Return (derived updates, verified bindings, receipt or None)."""
    # An unchanged native plan may already be included in a legacy v1 update.
    # Its predecessor object, never a caller-supplied path, authorizes binding.
    bindings = dict(evidence_bindings)
    submitted_updates = updates
    updates, retained_focus = _retain_declared_focus(repo, updates, bindings)
    for update in updates.get("updates", []):
        if not update.get("expected_previous_version"):
            continue
        targets = [update, *update.get("subpoints", [])]
        if not any(target.get("teaching_plan") or target.get("claims") for target in targets):
            continue
        prior = _prior(repo, update)
        old_targets = {row.get("subpoint_id"): row for row in [prior, *prior.get("subpoints", [])]}
        for target in targets:
            old = old_targets.get(target.get("subpoint_id"), {})
            plan = target.get("teaching_plan")
            if plan and plan == old.get("teaching_plan"):
                _bind_old(plan, bindings)
            _bind_retained_claim_sources(target, old, bindings)
    path = Path(package_root) / FILENAME
    if not path.exists():
        receipt = None
        if retained_focus:
            receipt = {"schema": ADAPTER_SCHEMA, "source_file": "knowledge_updates.json",
                "source_sha256": digest(submitted_updates), "source_hash_scope": "canonical_submitted_updates",
                "derived_identity_sha256": digest(updates), "items": [],
                "retained_teaching_focus": retained_focus,
                "write_policy": "profile_only_after_formal_items_complete"}
        return updates, bindings, receipt
    file_rows = [row for row in manifest["files"] if row["path"] == FILENAME]
    require(len(file_rows) == 1 and file_rows[0]["role"] == "supplemental", "portable_file_role_invalid")
    require(path.is_file() and not path.is_symlink(), "portable_file_unsafe")
    raw = path.read_bytes()
    require(len(raw) <= 16 * 1024 * 1024 and hashlib.sha256(raw).hexdigest() == file_rows[0]["sha256"],
            "portable_file_binding_drift")
    proposal = json.loads(raw)
    require(isinstance(proposal, dict) and set(proposal) == {"schema", "source_commit", "study_date", "part_id", "items"},
            "portable_root_fields_invalid")
    require(proposal["schema"] == SCHEMA, "portable_schema_invalid")
    require(proposal["source_commit"] == manifest["source_commit"]
            and proposal["study_date"] == manifest["study_date"], "portable_version_or_date_drift")
    # Partition returns are merged before the native prepare entry is allowed.
    require(proposal["part_id"] is None, "portable_partition_requires_local_merge")
    require(isinstance(proposal["items"], list) and len(proposal["items"]) <= 512, "portable_items_invalid")
    result = copy.deepcopy(updates)
    by_point = {row["concept_key"]: row for row in result["updates"]}
    captures = {row["capture_id"]: row for row in manifest["captures"]}
    enhancement_items = {row["capture_id"]: row for row in enhancement["items"]}
    reopened, seen, audit, source_identities = {}, set(), [], {}
    for item in proposal["items"]:
        required = {"concept_key", "subpoint_id", "subpoint_title", "disposition", "reason", "summary_target", "plan", "source_refs"}
        require(isinstance(item, dict) and required <= set(item) <= required | {"previous_plan_refs"},
                "portable_item_fields_invalid")
        code, sid = item["concept_key"], item["subpoint_id"]
        require(isinstance(code, str) and code in by_point and code in manifest["affected_points"],
                "portable_point_out_of_scope")
        require(sid is None or isinstance(sid, str), "portable_subpoint_invalid")
        key = (code, sid)
        require(key not in seen, "portable_duplicate_target")
        seen.add(key)
        require(item["summary_target"] == ("parent" if sid is None else "subpoint"), "portable_summary_target_invalid")
        target = _target(by_point[code], sid)
        require(item["subpoint_title"] is None if sid is None else item["subpoint_title"] == target.get("title"),
                "portable_subpoint_title_drift")
        action = item["disposition"]
        require(action in {"retain", "revise", "retire"}, "portable_disposition_invalid")
        require(isinstance(item["reason"], str) and 0 < len(item["reason"].strip()) <= 400, "portable_reason_required")
        require(isinstance(item["source_refs"], list), "portable_sources_invalid")
        old_profile = _prior(repo, by_point[code])
        old_targets = [old_profile] if sid is None else [row for row in (old_profile or {}).get("subpoints", []) if row["subpoint_id"] == sid]
        old_plan = old_targets[0].get("teaching_plan") if old_targets and old_targets[0] else None
        previous_refs = item.get("previous_plan_refs", [])
        require(isinstance(previous_refs, list), "portable_previous_refs_invalid")
        if action in {"retain", "retire"}:
            require(old_plan is not None, "portable_previous_plan_required")
            require(item["plan"] is None and not item["source_refs"] and not previous_refs,
                    "portable_retain_retire_no_replacement")
            plan = copy.deepcopy(old_plan) if action == "retain" else None
            if plan:
                _bind_old(plan, bindings)
        else:
            require(isinstance(item["plan"], dict) and not item["plan"].get("source_refs"),
                    "portable_plan_native_refs_forbidden")
            plan = copy.deepcopy(item["plan"])
            refs = [_native_ref(repo, ref, captures, enhancement_items, reopened, bindings, archive_only=archive_only)
                    for ref in item["source_refs"]]
            identities = {ref["id"]: {"capture_id": ref["capture_id"], "conversation_locator": ref["conversation_locator"],
                "package_sha256": captures[ref["capture_id"]]["evidence_sha256"],
                "input_member_path": ref["input_member_path"]} for ref in item["source_refs"]}
            if previous_refs:
                require(old_plan is not None, "portable_previous_plan_required")
                old_sources = {ref["id"]: ref for ref in old_plan["source_refs"]}
                for ref in previous_refs:
                    require(isinstance(ref, dict) and set(ref) == {"id", "previous_source_id", "profile_version"},
                            "portable_previous_ref_fields_invalid")
                    require(ref["profile_version"] == by_point[code]["expected_previous_version"]
                            and ref["previous_source_id"] in old_sources, "portable_previous_ref_drift")
                    selected = _previous_ref(repo, old_sources[ref["previous_source_id"]])
                    refs.append({**selected, "id": ref["id"]})
                    require(bindings.get(selected["path"], selected["sha256"]) == selected["sha256"], "portable_binding_conflict")
                    bindings[selected["path"]] = selected["sha256"]
                    identities[ref["id"]] = {"profile_version": ref["profile_version"],
                        "concept_key": code, "subpoint_id": sid, "previous_source_id": ref["previous_source_id"]}
            plan["source_refs"] = refs
            profiles.validate_teaching_plan(plan, bindings)
            source_identities[key] = identities
        require("teaching_plan" not in target or target["teaching_plan"] == plan, "portable_inline_plan_conflict")
        update = {"action": action, "reason": item["reason"]}
        require("teaching_plan_update" not in target or target["teaching_plan_update"] == update,
                "portable_inline_disposition_conflict")
        target["teaching_plan_update"] = update
        if plan is None:
            target.pop("teaching_plan", None)
        else:
            target["teaching_plan"] = plan
        audited_sources = copy.deepcopy((plan or {}).get("source_refs", []))
        for ref in audited_sources:
            if ref["id"] in source_identities.get(key, {}):
                ref["verified_origin"] = source_identities[key][ref["id"]]
                ref.pop("path")
        audit.append({"concept_key": code, "subpoint_id": sid, "action": action,
                      "capture_ids": sorted({ref["capture_id"] for ref in item["source_refs"]}),
                      "previous_profile_version": by_point[code].get("expected_previous_version"),
                      "source_bindings": audited_sources})
    neutral = copy.deepcopy(result)
    for update in neutral["updates"]:
        for target in [update, *update.get("subpoints", [])]:
            key = (update["concept_key"], target.get("subpoint_id"))
            for ref in (target.get("teaching_plan") or {}).get("source_refs", []):
                if ref["id"] in source_identities.get(key, {}):
                    ref["verified_origin"] = source_identities[key][ref["id"]]
                    ref.pop("path")
    receipt = {"schema": ADAPTER_SCHEMA, "source_file": FILENAME, "source_sha256": hashlib.sha256(raw).hexdigest(),
               "derived_identity_sha256": digest(neutral), "items": audit,
               "write_policy": "profile_only_after_formal_items_complete"}
    if retained_focus:
        receipt["retained_teaching_focus"] = retained_focus
    return result, bindings, receipt
