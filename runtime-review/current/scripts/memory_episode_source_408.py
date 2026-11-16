"""Cold, read-only enrichment from one hash-bound original episode turn.

This adapter does not repair source records, infer exam years, or replace a
reviewed summary. It asks the package owner to verify the requested immutable
revision, verifies the exact native turn tuple, then returns separate scope
and quote evidence. Quotes are for unbound history search only.
"""
from __future__ import annotations

import copy
import csv
import hashlib
import io
import json
import re
from collections import Counter
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

import conversation_package_408 as packages
from archive_408_conversation_package import ArchiveError

SCHEMA = "cs408-episode-source-proof-v1"
EXAM = re.compile(r"EXAM408-(\d{4})-Q\d{2}(?:-P\d{2})?$")
FORMAL = re.compile(r"(?:DS|CO|OS|CN)_(?:\d{4}|UNK)_\d{3}$")
SHA = re.compile(r"[a-f0-9]{64}$")
FRAGMENT = re.compile(r"turn:([1-9]\d*)(?:#[A-Za-z0-9:_-]+)?$")
LEARNER_ROLES = {"learner_report", "observed_learner_evidence"}
ASSISTANT_ROLES = {"assistant_explanation"}
CANONICAL_STATUSES = {"canonical_new", "linked_existing_wrong_question", "canonical_with_legacy_alias"}


class EpisodeSourceError(ValueError):
    pass


def _digest(raw):
    return hashlib.sha256(raw).hexdigest()


def _guard(path):
    stat = path.stat()
    return {"path": str(path.resolve()), "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns, "ctime_ns": stat.st_ctime_ns}


def _current(guard):
    try:
        observed = _guard(Path(guard["path"]))
        return all(observed[key] == guard[key]
                   for key in ("path", "size", "mtime_ns", "ctime_ns"))
    except (OSError, KeyError):
        return False


class EpisodeSourceResolver:
    """One cold operation's package/registry memo, never a latest-state cache."""
    def __init__(self, repo, *, package_root=None, details_root=None, entities=None):
        self.repo = Path(repo).resolve()
        import knowledge_history_sources_408 as sources
        self._allow_archive = package_root is None and self.repo == sources.CANONICAL_REPO.resolve()
        if package_root is None:
            package_root = (sources.PRIVATE_ROOT if self.repo == sources.CANONICAL_REPO.resolve()
                            else self.repo / ".private/conversation-packages")
        self.package_root = Path(package_root).resolve()
        self.details = Path(details_root).resolve() if details_root is not None else self.repo.with_name(self.repo.name + "-details")
        self.entities = entities or {}
        self._packages = {}
        self._assets = {}
        self._registry = None
        self._registry_guards = []
        self._guards = {}
        self._counts = Counter()

    def _read(self, path, *, under=None):
        path = Path(path)
        if path.is_symlink():
            raise EpisodeSourceError("proof_file_symlink_rejected")
        if not path.is_file():
            raise EpisodeSourceError("proof_file_missing")
        if under is not None:
            try:
                path.resolve().relative_to(Path(under).resolve())
            except ValueError as exc:
                raise EpisodeSourceError("proof_file_escaped_root") from exc
        before = _guard(path)
        raw = path.read_bytes()
        if before != _guard(path):
            raise EpisodeSourceError("proof_file_changed_during_read")
        guard = {**before, "sha256": _digest(raw)}
        if guard["path"] in self._guards and self._guards[guard["path"]] != guard:
            raise EpisodeSourceError("proof_file_changed_within_cold_operation")
        self._guards[guard["path"]] = guard
        return raw, guard

    def _package(self, source_id, canonical_sha):
        key = (source_id, canonical_sha)
        cached = self._packages.get(key)
        if cached and all(_current(guard) for guard in cached["component_guards"]):
            self._counts["package_memo_hits"] += 1
            return cached
        try:
            opened = packages.verify_package_revision_binding(source_id,
                expected_canonical_sha256=canonical_sha, require_sealed=True, private_root=self.package_root)
        except packages.ConversationPackageError as exc:
            unavailable = any(word in str(exc) for word in ("unavailable", "missing"))
            if not self._allow_archive or not unavailable:
                raise
            # Follow the archive owner's exact locator. The top manifest only
            # authenticates the container location; all quote/source bytes
            # still come from the requested immutable revision below it.
            import archive_408_conversation_package as archive
            archive.verify_volume(archive.ARCHIVE_ROOT)
            package_id = source_id.removeprefix(packages.LOCATOR_PREFIX)
            note_path = self.repo / archive.LOCATOR_NOTE_ROOT / (package_id + ".md")
            _, note_guard = self._read(note_path, under=self.repo)
            note = archive._read_locator_note(note_path)
            if note.get("package_id") != package_id:
                raise EpisodeSourceError("archive_locator_identity_conflict")
            relative = archive._relative(note["raw_archive_relpath"])
            if relative.parts[:len(archive.SUBJECT_RELATIVE_ROOT.parts)] != archive.SUBJECT_RELATIVE_ROOT.parts:
                raise EpisodeSourceError("archive_locator_scope_conflict")
            archive_root = archive.ARCHIVE_ROOT / archive.SUBJECT_RELATIVE_ROOT
            container = archive.ARCHIVE_ROOT / relative
            manifest_raw, container_guard = self._read(container / "manifest.json", under=archive_root)
            container_manifest = json.loads(manifest_raw)
            if (container_guard["sha256"] != note.get("raw_archive_manifest_sha256")
                    or container_manifest.get("package_id") != package_id
                    or container_manifest.get("canonical_sha256") != note.get("raw_archive_package_sha256")):
                raise EpisodeSourceError("archive_locator_manifest_conflict")
            opened = packages.verify_package_revision_binding(source_id,
                expected_canonical_sha256=canonical_sha, require_sealed=True, private_root=archive_root)
            if Path(opened["package_dir"]).resolve() != container.resolve():
                raise EpisodeSourceError("archive_revision_location_conflict")
            opened = {**opened, "component_guards": [*opened["component_guards"], note_guard, container_guard]}
        for guard in opened["component_guards"]:
            self._guards[guard["path"]] = guard
        self._packages[key] = opened
        self._counts["package_verifications"] += 1
        return opened

    def _load_registry(self):
        if self._registry is not None and all(_current(g) for g in self._registry_guards):
            return self._registry
        path = self.details / "corpus/408统考真题/indexes/question-index.csv"
        raw, index_guard = self._read(path, under=self.details)
        rows, formals = {}, {}
        for item in csv.DictReader(io.StringIO(raw.decode("utf-8-sig"))):
            identity = item.get("exam_question_id", "")
            if not EXAM.fullmatch(identity) or identity in rows:
                raise EpisodeSourceError("registered_question_index_identity_invalid")
            if item.get("canonical_status") not in CANONICAL_STATUSES or item.get("extraction_status") != "complete":
                continue
            try:
                assets = json.loads(item.get("question_assets") or "[]")
            except ValueError as exc:
                raise EpisodeSourceError("registered_question_assets_invalid") from exc
            if not isinstance(assets, list) or not all(isinstance(ref, str) for ref in assets):
                raise EpisodeSourceError("registered_question_assets_invalid")
            formal_ids = sorted(set(re.findall(r"(?:DS|CO|OS|CN)_(?:\d{4}|UNK)_\d{3}", item.get("formal_node_ids", ""))))
            rows[identity] = {"id": identity, "assets": [{"ref": ref} for ref in assets],
                "formal_ids": formal_ids, "contact_dependency_ids": [], "registry_guard": index_guard,
                "registry_kind": "registered_exam_corpus"}
            for formal in formal_ids:
                formals.setdefault(formal, set()).add(identity)
        guards = [index_guard]
        subquestions = self.repo / "support/syllabus-2027/exam-subquestions.json"
        if subquestions.is_file():
            raw, guard = self._read(subquestions, under=self.repo)
            value = json.loads(raw)
            for item in value.get("items", []):
                identity = item.get("true_item_id", "")
                if (not EXAM.fullmatch(identity) or item.get("review_status") != "reviewed"
                        or item.get("source_authority") not in {"registered_exam_corpus", "user_supplied_third_party_corrected_transcription"}):
                    continue
                assets = item.get("question_asset_refs")
                if not isinstance(assets, list):
                    continue
                declared_parents = {item.get("parent_true_item_id"), item.get("parent_exam_id")}
                if (len(declared_parents) != 1 or None in declared_parents
                        or not re.fullmatch(r"EXAM408-\d{4}-Q\d{2}-P\d{2}", identity)):
                    continue
                parent = next(iter(declared_parents))
                if (parent != identity.rsplit("-P", 1)[0] or parent not in rows
                        or rows[parent]["registry_kind"] != "registered_exam_corpus"):
                    continue
                rows[identity] = {"id": identity, "assets": copy.deepcopy(assets), "formal_ids": [],
                    "contact_dependency_ids": list(item.get("contact_dependency_ids") or []),
                    "registry_guard": guard, "registry_kind": "reviewed_independent_subquestion",
                    "registered_parent_question_id": parent}
            guards.append(guard)
        self._registry = {"questions": rows, "formals": formals}
        self._registry_guards = guards
        self._assets.clear()
        self._counts["registry_reads"] += 1
        return self._registry

    def _registered_assets(self, question_id):
        registry = self._load_registry()
        item = registry["questions"].get(question_id)
        if item is None:
            raise EpisodeSourceError("question_identity_not_registered")
        cached = self._assets.get(question_id)
        if cached and all(_current(g) for g in cached["guards"]):
            return cached
        hashes, guards, references = set(), [item["registry_guard"]], []
        for asset in item["assets"]:
            relative = PurePosixPath(asset.get("ref", ""))
            if relative.is_absolute() or ".." in relative.parts or not relative.parts:
                raise EpisodeSourceError("registered_asset_path_invalid")
            raw, guard = self._read(self.details / relative, under=self.details)
            if asset.get("sha256") and asset["sha256"] != guard["sha256"]:
                raise EpisodeSourceError("registered_asset_hash_conflict")
            if "assets-preprocessed" in relative.parts:
                revision = relative.parts[relative.parts.index("assets-preprocessed") + 1]
                receipt = self.details / "corpus/408统考真题/_derived/preprocessed-image-adoptions" / revision / (question_id + ".json")
                receipt_raw, receipt_guard = self._read(receipt, under=self.details)
                accepted = json.loads(receipt_raw)
                expected = [row for row in (accepted.get("roles", {}).get("question", {}).get("new") or [])
                            if row.get("ref") == relative.as_posix()]
                if (accepted.get("schema") != "preprocessed-image-adoption-receipt-v1"
                        or accepted.get("canonical_question_id") != question_id
                        or accepted.get("revision_id") != revision
                        or accepted.get("root_review", {}).get("decision") != "accept"
                        or len(expected) != 1 or expected[0].get("sha256") != guard["sha256"]):
                    raise EpisodeSourceError("registered_asset_adoption_conflict")
                guards.append(receipt_guard)
            hashes.add(guard["sha256"])
            guards.append(guard)
            references.append({"question_id": question_id, "path": guard["path"], "sha256": guard["sha256"],
                               "registry_kind": item["registry_kind"]})
        result = {"hashes": hashes, "guards": guards, "references": references, "registry": item}
        self._assets[question_id] = result
        return result

    @staticmethod
    def _identity(opened):
        source = opened["source"]
        episode = source.get("source") or {}
        binding = episode.get("display_binding") or {}
        nested = episode.get("source") or {}
        fields = [("source.source.display_binding.true_item_id", binding.get("true_item_id")),
                  ("source.source.display_binding.external_source_id", binding.get("external_source_id")),
                  ("source.source.source.source_id", nested.get("source_id")),
                  ("source.source.source_id", episode.get("source_id")),
                  ("source.source_id", source.get("source_id"))]
        declared = [(path, value) for path, value in fields if isinstance(value, str) and EXAM.fullmatch(value)]
        identities = {value for _, value in declared}
        if not identities:
            raise EpisodeSourceError("episode_question_identity_missing")
        if len(identities) != 1:
            raise EpisodeSourceError("episode_question_identity_conflict")
        return next(iter(identities)), [{"field": key, "question_id": value} for key, value in declared]

    def _display_receipt(self, opened, question_id):
        """Accept only a sealed pointer to an actual shown-display receipt."""
        episode = opened["source"].get("source") or {}
        source = episode.get("source") or {}
        pointers = source.get("actual_visual_receipts") or []
        attachment_by_sha = {row.get("sha256"): row for row in opened["manifest"].get("attachments", [])}
        for pointer in pointers:
            if not isinstance(pointer, dict) or not isinstance(pointer.get("path"), str):
                continue
            sha = Path(pointer["path"]).stem
            attached = attachment_by_sha.get(sha)
            if not SHA.fullmatch(sha) or not attached or attached.get("mime_type") != "application/json":
                continue
            path = Path(opened["package_dir"]) / attached["relative_path"]
            raw, guard = self._read(path, under=opened["package_dir"])
            value = json.loads(raw)
            if (guard["sha256"] == sha and value.get("schema") == "cs408-displayed-visual-evidence-v1"
                    and value.get("question_id") == question_id and value.get("status") == "shown"
                    and value.get("session_id") == opened["source"].get("session_id")):
                return {"kind": "actual_shown_display_receipt", "question_id": question_id,
                        "path": guard["path"], "sha256": sha}, guard
        return None, None

    def _scope(self, opened, record):
        question_id, declarations = self._identity(opened)
        registry = self._load_registry()
        if question_id not in registry["questions"]:
            raise EpisodeSourceError("question_identity_not_registered")
        known = set()
        direct = record.get("source_question_id")
        if isinstance(direct, str) and EXAM.fullmatch(direct):
            known.add(direct)
        for formal in record.get("formal_ids") or []:
            actual = (self.entities.get(formal) or {}).get("canonical_exam_id")
            if isinstance(actual, str) and EXAM.fullmatch(actual):
                known.add(actual)
            registered = registry["formals"].get(formal, set())
            if len(registered) == 1:
                known.update(registered)
        compatible = {question_id}
        registered_parent = registry["questions"][question_id].get("registered_parent_question_id")
        if registered_parent:
            # An explicitly registered child can be associated with its formal
            # parent without claiming that siblings were displayed or studied.
            # Prefix similarity alone is never accepted as this proof.
            compatible.add(registered_parent)
        if known - compatible:
            raise EpisodeSourceError("existing_question_identity_conflict")
        registered = self._registered_assets(question_id)
        captured = {row.get("sha256") for row in opened["manifest"].get("attachments", [])
                    if row.get("role") == "question_image" and SHA.fullmatch(str(row.get("sha256", "")))}
        matches = captured & registered["hashes"]
        guards = list(registered["guards"])
        if matches and captured <= registered["hashes"]:
            asset_proofs = [ref for ref in registered["references"] if ref["sha256"] in matches]
            method = "registered_question_asset_bytes"
        elif captured:
            # A shown label cannot override a conflicting/unverified image.
            raise EpisodeSourceError("episode_question_asset_not_bound")
        else:
            receipt, guard = self._display_receipt(opened, question_id)
            if receipt is None:
                raise EpisodeSourceError("episode_display_proof_missing")
            asset_proofs, method = [receipt], "actual_shown_display_receipt"
            guards.append(guard)
        dependencies = set(registered["registry"].get("contact_dependency_ids", []))
        dependencies.update((self.entities.get(question_id) or {}).get("contact_dependency_ids", []))
        if any(not isinstance(value, str) or not EXAM.fullmatch(value) for value in dependencies):
            raise EpisodeSourceError("episode_question_dependency_unverified")
        source_guard = next(guard for guard in opened["component_guards"]
                            if Path(guard["path"]).name == "source.json")
        return {"schema": SCHEMA, "status": "verified", "authority": "verified_episode_revision",
                "question_ids": [question_id], "contact_dependency_ids": sorted(dependencies),
                "source_id": record["source_id"], "source_sha256": record["source_sha256"],
                "source_json": {"path": source_guard["path"], "sha256": source_guard["sha256"]},
                "identity_fields": declarations, "asset_proofs": asset_proofs, "method": method,
                "registered_parent_association_accepted": bool(registered_parent and registered_parent in known),
                "formal_associations_are_not_inferred_from_year": True}, guards

    def resolve(self, record, *, include_original_turn=True, current_question_id=None,
                subpoint_id=None, excluded_years=()):
        self._counts["records_requested"] += 1
        base = {"schema": SCHEMA, "status": "no_enrichment", "existing_record_unchanged": True,
                "original_turn_usable": False, "formal_write_count": 0, "learner_evidence_write_count": 0}
        if not isinstance(record, Mapping):
            return {**base, "reason": "record_invalid", "conflict": False}
        locator, sha = record.get("source_id"), record.get("source_sha256")
        fragment = FRAGMENT.fullmatch(str(record.get("source_fragment", "")))
        if (not isinstance(locator, str) or not locator.startswith(packages.LOCATOR_PREFIX)
                or not isinstance(sha, str) or not SHA.fullmatch(sha) or fragment is None):
            self._counts["unsupported_tuple"] += 1
            return {**base, "reason": "unsupported_original_turn_tuple", "conflict": False}
        ordinal = int(fragment[1])
        if not isinstance(record.get("original_turn_refs"), list) or not isinstance(record.get("formal_ids", []), list):
            return {**base, "reason": "record_provenance_shape_invalid", "conflict": False}
        refs = [ref for ref in record["original_turn_refs"] if isinstance(ref, dict)
                and type(ref.get("turn")) is int and ref.get("turn") == ordinal]
        if len(refs) != 1 or not SHA.fullmatch(str(refs[0].get("sha256", ""))):
            self._counts["missing_original_turn_ref"] += 1
            return {**base, "reason": "original_turn_ref_missing_or_ambiguous", "conflict": False}
        try:
            opened = self._package(locator, sha)
            turns = opened["conversation"]["turns"]
            if ordinal > len(turns):
                raise EpisodeSourceError("original_turn_ordinal_conflict")
            turn = turns[ordinal - 1]
            from seal_knowledge_observations_408 import _bytes as native_turn_bytes
            turn_sha = _digest(native_turn_bytes(turn))
            if turn.get("ordinal") != ordinal or refs[0]["sha256"] != turn_sha:
                raise EpisodeSourceError("original_turn_hash_conflict")
            if record.get("source_order") is not None and (type(record["source_order"]) is not int or record["source_order"] != ordinal):
                raise EpisodeSourceError("original_turn_order_conflict")
            role = ("user" if record.get("evidence_role") in LEARNER_ROLES else
                    "assistant" if record.get("evidence_role") in ASSISTANT_ROLES else None)
            if role is None:
                raise EpisodeSourceError("original_turn_role_not_supported")
            if turn.get("role") != role:
                raise EpisodeSourceError("original_turn_role_conflict")
            if ((record.get("event_kind") in {"outcome", "repair"} and role != "user")
                    or (record.get("event_kind") in {"hint", "explanation"} and role != "assistant")):
                raise EpisodeSourceError("original_turn_event_role_conflict")
            if not isinstance(turn.get("text"), str) or not turn["text"].strip():
                raise EpisodeSourceError("original_turn_text_missing")
            scope, guards = self._scope(opened, record)
            guards = [*opened["component_guards"], *guards]
            guards = list({guard["path"]: guard for guard in guards}.values())
            if not all(_current(guard) for guard in guards):
                raise EpisodeSourceError("episode_source_changed_during_projection")
            conversation_guard = next(guard for guard in opened["component_guards"]
                                      if Path(guard["path"]).name == "conversation.json")
            provenance = {"source_id": locator, "source_sha256": sha,
                "source_fragment": record["source_fragment"], "ordinal": ordinal, "role": role,
                "turn_sha256": turn_sha, "turn_hash_recipe": "native_compact_sorted_json_without_newline",
                "conversation_path": conversation_guard["path"], "conversation_sha256": conversation_guard["sha256"],
                "source_json_path": scope["source_json"]["path"], "source_json_sha256": scope["source_json"]["sha256"]}
            years = {int(year) for year in excluded_years}
            withheld = ("current_question_or_subpoint_scope" if current_question_id or subpoint_id else
                "protected_year" if any(int(EXAM.fullmatch(q)[1]) in years
                    for q in scope["question_ids"] + scope["contact_dependency_ids"]) else
                "not_requested" if not include_original_turn else None)
            result = {**base, "status": "verified", "conflict": False, "scope_proof": scope,
                "original_turn_provenance": provenance, "dependency_guards": guards,
                "original_turn_usable": withheld is None,
                "source_refs": [{"kind": "verified_original_episode_turn", **provenance}]}
            if withheld is None:
                result["original_turn"] = {"usage": "unbound_history_only", "text": turn["text"],
                    "text_sha256": _digest(turn["text"].encode("utf-8")), **provenance,
                    "is_new_learning_event": False, "independent_mastery_inferred": False}
                self._counts["original_turns_available"] += 1
            else:
                result["original_turn_withheld_reason"] = withheld
                self._counts["original_turns_withheld"] += 1
            self._counts["verified"] += 1
            return result
        except (EpisodeSourceError, packages.ConversationPackageError, ArchiveError, OSError, ValueError, KeyError, TypeError) as exc:
            reason = str(exc) or type(exc).__name__
            conflict = any(word in reason for word in ("conflict", "drift", "changed", "mismatch", "escaped", "symlink"))
            self._counts["conflicts" if conflict else "unavailable"] += 1
            return {**base, "reason": reason, "conflict": conflict}

    def dependency_guards(self):
        guards = [self._guards[key] for key in sorted(self._guards)]
        if not all(_current(guard) for guard in guards):
            raise EpisodeSourceError("episode_source_changed_before_cold_publication")
        return copy.deepcopy(guards)

    def statistics(self):
        return {**dict(self._counts), "distinct_packages": len(self._packages),
                "dependency_file_count": len(self._guards), "formal_write_count": 0,
                "learner_evidence_write_count": 0}
