#!/usr/bin/env python3
"""Freeze a Shanghai day's raw Capture packages without grading or formal intake."""
from __future__ import annotations

import argparse
from contextlib import nullcontext
import datetime as dt
import fcntl
import hashlib
import json
from pathlib import Path
import re
import sys
import tempfile
import zipfile
from zoneinfo import ZoneInfo

import runtime_paths_408 as _paths

sys.dont_write_bytecode = True
sys.path.insert(0, str(_paths.bridge_root()))
from bridge_archives import safe_path, source_file, write_zip, file_hash
import conversation_package_408 as packages
import intake_fact_capture_408 as captures

SCHEMA = "cs408-raw-capture-day-v1"
ALIGNMENT_SCHEMA = "cs408-teaching-alignment-requirements-v1"
ALIGNMENT_FILE = "TEACHING_ALIGNMENT_REQUIREMENTS.json"
ALIGNMENT_REGISTRY = Path(".private/study-bridge/teaching-alignment-inputs")
SYLLABUS_TEXT_FILE = "contracts/2027-syllabus-source.txt"
SYLLABUS_PROOF_FILE = "contracts/2027-syllabus-source-proof.json"


def capture_set_sha256(ids):
    return hashlib.sha256(json.dumps(sorted(ids), ensure_ascii=False, sort_keys=True,
                                   separators=(",", ":")).encode()).hexdigest()


def register_teaching_input(repo, archive_path, requirements):
    """Record the verified archive identity, never a learning/formal event."""
    from split_web_input_408 import read_archive
    archive_path = Path(archive_path).resolve()
    data, manifest = read_archive(archive_path)
    raw = data[ALIGNMENT_FILE]
    require(json.loads(raw) == requirements, "alignment_registry_requirements_drift")
    ids = sorted(row["capture_id"] for row in manifest["captures"])
    require(ids == requirements["capture_ids"] and capture_set_sha256(ids) == requirements["capture_set_sha256"],
            "alignment_registry_capture_scope_drift")
    require(manifest.get("source_commit", manifest.get("baseline_snapshot_id")) == requirements["source_commit"],
            "alignment_registry_snapshot_drift")
    require(re.fullmatch(r"[A-Za-z0-9_.-]{1,160}", requirements["input_id"]), "alignment_input_id_invalid")
    for row in requirements["source_files"]:
        require(row["path"] in data and hashlib.sha256(data[row["path"]]).hexdigest() == row["sha256"],
                "alignment_registry_source_drift:" + row["path"])
    value = {"requirements": requirements, "requirements_sha256": hashlib.sha256(raw).hexdigest(),
             "input_zip_path": str(archive_path), "input_zip_sha256": file_hash(archive_path)}
    path = Path(repo) / ALIGNMENT_REGISTRY / (requirements["input_id"] + ".json")
    if path.exists():
        require(json.loads(path.read_bytes()) == value, "alignment_registry_conflict")
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Exclusive creation protects an already-registered input identity.
        with path.open("xb") as stream:
            stream.write(encoded(value))
    require(json.loads(path.read_bytes()) == value, "alignment_registry_readback_failed")
    return {"path": str(path), "sha256": file_hash(path), "input_id": requirements["input_id"]}


def verify_teaching_input_registration(repo, archive_path, requirements, requirements_raw):
    """A split derives authority from the already-registered native parent."""
    input_id = requirements["input_id"]
    require(re.fullmatch(r"[A-Za-z0-9_.-]{1,160}", input_id), "alignment_input_id_invalid")
    path = Path(repo) / ALIGNMENT_REGISTRY / (input_id + ".json")
    require(path.is_file(), "alignment_parent_registration_required")
    value = json.loads(path.read_bytes())
    require(value.get("requirements") == requirements
            and value.get("requirements_sha256") == hashlib.sha256(requirements_raw).hexdigest()
            and value.get("input_zip_path") == str(Path(archive_path).resolve())
            and value.get("input_zip_sha256") == file_hash(archive_path), "alignment_parent_registration_drift")
    return value


def _verified_capture_question(capture, versions):
    """Only native display bindings establish an exact question, never hints."""
    identities = set()
    from concept_review_408 import digest as receipt_digest
    for package in versions:
        outer = package["source"].get("source") or {}
        source = outer.get("source") or {}
        binding = outer.get("display_binding") or {}
        if binding.get("schema") == "cs408-D-supplement-display-v1":
            material = {key: value for key, value in binding.items() if key != "receipt_sha256"}
            require(binding.get("receipt_sha256") == receipt_digest(material), "alignment_display_binding_hash_drift")
            if binding.get("surface_kind") == "canonical_original":
                require(source.get("source_id") == binding.get("true_item_id"), "alignment_exact_question_binding_drift")
                identities.add(binding["true_item_id"])
        elif source.get("origin_lane") == "campaign_original" and binding.get("display_receipt_sha256"):
            # Campaign source freezes a reference, while the original display
            # is a canonical package attachment. Verify those exact bytes.
            expected = binding["display_receipt_sha256"]
            for row in package["manifest"]["files"]:
                if row.get("sha256") != expected:
                    continue
                raw = source_file(Path(package["package_dir"]) / safe_path(row["relative_path"])).read_bytes()
                require(hashlib.sha256(raw).hexdigest() == expected, "alignment_campaign_display_hash_drift")
                display = json.loads(raw)
                require(all(display.get(key) == binding.get(key) for key in
                            ("run_id", "item_id", "campaign_item_commitment_sha256")),
                        "alignment_campaign_display_scope_drift")
                require(source.get("source_id") == display.get("true_item_id"), "alignment_exact_question_binding_drift")
                if display.get("surface_kind") == "canonical_original":
                    identities.add(display["true_item_id"])
                elif display.get("surface_kind") == "formal_original":
                    formal = display.get("formal_node_id")
                    require(isinstance(formal, str) and re.fullmatch(r"(?:DS|CO|OS|CN)_(?:\d{4}|UNK)_\d{3}", formal)
                            and source.get("formal_node_id") == formal, "alignment_formal_original_binding_drift")
                    identities.add(formal)
    require(len(identities) <= 1, "alignment_capture_multiple_exact_questions:" + capture["capture_id"])
    return next(iter(identities), None)


def _read_large_alignment_page(snap, failed_page, *, identity, points, cursor):
    """Recover an oversized cold export from the same published object graph.

    MCP still uses its 64 KiB limit. The local snapshot reader already supports
    1 MiB pages; its inputs are only the named, hash-verified publication members,
    never the live teaching stores. Preserve the real MCP failure as provenance.
    """
    import study_personalization_408 as snapshot_reader
    failed = failed_page.get("personal_context") or {}
    require(failed.get("status") == "budget_too_small"
            and failed_page.get("source_commit") == snap.commit,
            "alignment_object_recovery_requires_same_snapshot_budget_failure")
    manifest_path = snap.path(snap.library["entrypoints"]["personalization"])
    options = {"personal_scope": "full", "materials": "all"}
    if identity:
        options["current_question_id"] = identity
    plan = snapshot_reader.plan_snapshot(manifest_path, points or [], [], view="direct", **options)
    paths = plan.get("files")
    require(isinstance(paths, list) and all(isinstance(path, str) for path in paths),
            "alignment_object_recovery_plan_invalid")
    snap.materialize_paths(paths)
    objects = []
    for path in sorted(set(paths)):
        # Snapshot.path checks the published allowlist and exact byte SHA.
        raw = snap.path(path).read_bytes()
        objects.append({"published_path": path, "sha256": hashlib.sha256(raw).hexdigest(), "size_bytes": len(raw)})
    value = snapshot_reader.query_snapshot(manifest_path, points or [], [], view="direct",
        max_bytes=1024 * 1024, cursor=cursor, **options)
    require(value.get("status") == "ready", "alignment_object_recovery_still_unavailable:" + str(identity or points))
    require(all(value.get(key) == failed.get(key) for key in ("version", "formal_version")),
            "alignment_object_recovery_version_drift")
    return {**failed_page, "personal_context": value, "source_mappings": snap.source_mappings(value),
            "personal_context_origin": "verified_fixed_snapshot_objects",
            "object_read_recovery": {"method": "published_refs_with_native_snapshot_reader",
                "source_commit": snap.commit, "mcp_response": failed_page,
                "mcp_max_bytes": 65536, "local_native_max_bytes": 1024 * 1024,
                "materialized_objects": objects, "live_sources_used": False}}


def _local_standalone_candidates(snap, selected, opened):
    """Find teacher context for a sealed standalone problem without promoting a hint to a binding."""
    hints = {}
    for capture in selected:
        facts = (capture.get("capture") or {}).get("source_facts") or {}
        qid = facts.get("source_id", "")
        if facts.get("execution_kind") == "standalone_question" and re.fullmatch(r"EXAM408-\d{4}-Q\d{2}(?:-P\d{2})?", qid):
            hints[capture["capture_id"]] = qid
    if not hints:
        return {}
    mappings = {r["question_id"]: r for r in snap.library.get("original_mappings", [])}
    wanted = set(hints.values()) - mappings.keys()
    for shard in snap.library.get("original_mapping_inventory", []):
        if not wanted:
            break
        raw = snap.path(shard["path"]).read_bytes()
        require(hashlib.sha256(raw).hexdigest() == shard["sha256"], "standalone_original_mapping_hash_drift")
        for line in raw.splitlines():
            row = json.loads(line)
            if row["question_id"] in wanted:
                mappings[row["question_id"]] = row
                wanted.discard(row["question_id"])
    result = {}
    for cid, qid in hints.items():
        images = (mappings.get(qid) or {}).get("question_images") or []
        attached = {a.get("sha256") for package in opened[cid] for a in package["manifest"].get("attachments", [])
                    if a.get("role") == "question_image"}
        if images and attached and all(hashlib.sha256(snap.path(path).read_bytes()).hexdigest() in attached
                                       for path in images):
            result[cid] = qid
    return result


def _alignment_context(selected, opened, requested_commit, cache_parent, *, local_author=False, repo=None):
    """Freeze exact teaching inputs from the native reader, or an explicitly pinned replica."""
    from export_morning_input_408 import LibraryStore, BRIDGE_ROOT
    native_source = None
    if local_author and requested_commit is None and repo is not None:
        import exam_tutoring_context_408 as tutoring
        from study_personalization_408 import current_identity
        native_identity = current_identity(repo)
        require(native_identity.get("status") == "ready", "alignment_native_context_unavailable")
        native_source = native_identity["source_version"]
        native_root = (repo / tutoring.DEFAULT_STATE_REL).resolve()
    reading = _visual_reading_index(selected, opened, requested_commit)
    by_capture = {row["capture_id"]: row for row in reading["captures"]}
    questions = []
    for capture in selected:
        cid = capture["capture_id"]
        questions.append({"capture_ids": [cid], "question_identity": _verified_capture_question(capture, opened[cid]),
                          "knowledge_points": by_capture[cid]["candidate_points"],
                          "expected_previous_sha256": None, "expected_source_plan_sha256": None})
    grouped = {}
    for target in questions:
        key = target["question_identity"] or ("capture:" + target["capture_ids"][0])
        if key not in grouped:
            grouped[key] = target
        else:
            grouped[key]["capture_ids"] = sorted(set(grouped[key]["capture_ids"] + target["capture_ids"]))
            grouped[key]["knowledge_points"] = sorted(set(grouped[key]["knowledge_points"] + target["knowledge_points"]))
    questions = list(grouped.values())
    blobs, routes, context_rows = {}, {}, {}
    store = LibraryStore(BRIDGE_ROOT, cache_parent=cache_parent)
    try:
        snap = store.snapshot("408", requested_commit or "")
        commit = snap.commit
        require(re.fullmatch(r"[0-9a-f]{40}", commit) and (not requested_commit or requested_commit == commit),
                "alignment_snapshot_version_drift")
        prefix = Path(snap.library["entrypoints"]["personalization"]).parent.as_posix() + "/"

        def object_bytes(path, expected):
            if native_source and path.startswith(prefix):
                local = source_file(native_root / path.removeprefix(prefix), native_root)
                raw = local.read_bytes()
            else:
                raw = snap.path(path).read_bytes()
            require(hashlib.sha256(raw).hexdigest() == expected, "alignment_source_object_hash_drift:" + path)
            return raw

        def freeze_refs(page):
            refs = {}

            def visit(value):
                if isinstance(value, dict):
                    if (isinstance(value.get("ref"), str) and isinstance(value.get("sha256"), str)
                            and value["ref"].startswith(("objects/", "manifests/", "receipts/", prefix))):
                        relative = safe_path(value["ref"])
                        path = relative if relative.startswith(prefix) else prefix + relative
                        refs[path] = value["sha256"]
                    for child in value.values():
                        visit(child)
                elif isinstance(value, list):
                    for child in value:
                        visit(child)
            visit(page["personal_context"])
            for row in page.get("source_mappings", []):
                if row.get("status") == "available" and row.get("published_path"):
                    refs[row["published_path"]] = row["sha256"]
            result = []
            for path, expected in sorted(refs.items()):
                raw = object_bytes(path, expected)
                name = "teaching_sources/" + expected + Path(path).suffix
                require(name not in blobs or blobs[name] == raw, "alignment_source_collision")
                blobs[name] = raw
                result.append({"published_path": path, "package_path": name, "sha256": expected})
            return result

        def query(identity=None, points=None):
            if native_source:
                request = {"schema": tutoring.QUERY_INPUT_SCHEMA, "stage": "post_answer", "max_profiles": 0,
                           "knowledge_points": points or []}
                if identity:
                    request["current_question_id"] = identity
                value = tutoring.query_context(repo, request, teaching_view="direct", personal_scope="full", materials="all")
                require(value.get("status") == "available" and value.get("source_version") == native_source,
                        "alignment_native_context_drift:" + str(identity or points))
                value = dict(value, status="ready", complete=True)
                value["knowledge_summaries"] = value.pop("records", [])
                page = {"source_commit": commit, "personal_context": value}
                return {"source_commit": commit, "native_source_version": native_source, "pages": [page],
                        "source_files": freeze_refs(page)}
            pages, seen, cursor = [], set(), ""
            while True:
                # Freeze complete teacher material and use the same cursor scope
                # when a large page falls back to the native snapshot reader.
                kwargs = {"personal_scope": "full", "materials": "all"}
                if identity:
                    kwargs["current_question_id"] = identity
                page = store.personal_context("408", points or [], source_commit=commit, view="direct",
                    max_bytes=65536, cursor=cursor, allow_historical=True, **kwargs)
                require(page.get("source_commit") == commit, "alignment_context_version_drift")
                value = page.get("personal_context") or {}
                if value.get("status") == "budget_too_small":
                    page = _read_large_alignment_page(snap, page, identity=identity, points=points, cursor=cursor)
                    value = page["personal_context"]
                require(value.get("status") == "ready",
                        "alignment_context_unavailable:" + str(identity or points) + ":" + str(value.get("status")))
                pages.append(page)
                if value.get("complete") is not False:
                    break
                next_cursor = value.get("next_cursor")
                require(next_cursor and next_cursor not in seen, "alignment_context_pagination_stalled")
                seen.add(next_cursor)
                cursor = next_cursor
            refs = {row["package_path"]: row for page in pages for row in freeze_refs(page)}
            return {"source_commit": commit, "pages": pages, "source_files": list(refs.values())}

        def native_version(value, key, label):
            require(key in value, "alignment_snapshot_version_field_missing:" + label + ":" + key)
            version = value[key]
            require(version is None or re.fullmatch(r"[0-9a-f]{64}", version), "alignment_native_version_invalid:" + label)
            return version

        local_candidates = _local_standalone_candidates(snap, selected, opened) if local_author else {}
        for target in questions:
            bound_identity = target["question_identity"]
            identity = bound_identity or local_candidates.get(target["capture_ids"][0])
            if identity is None:
                continue
            if identity not in context_rows:
                context_rows[identity] = query(identity=identity)
                blobs["question_context/" + identity + "/context.json"] = encoded(context_rows[identity])
            found = [p["personal_context"].get("question_visual_template") for p in context_rows[identity]["pages"]]
            template = next((v for v in found if isinstance(v, dict)), None)
            require(template and template.get("question_identity") == identity, "alignment_question_template_unavailable:" + identity)
            if not bound_identity:
                cid = target["capture_ids"][0]
                by_capture[cid]["local_teacher_context_candidate"] = {
                    "question_id": identity, "basis": "sealed_question_images_match_fixed_original",
                    "status": "teacher_context_only_local_identity_review_required",
                }
            if bound_identity and template.get("status") != "missing":
                target["expected_previous_sha256"] = native_version(template, "template_native_version", identity)
                require(target["expected_previous_sha256"] is not None, "alignment_template_native_version_missing:" + identity)
                target["expected_source_plan_sha256"] = native_version(template, "source_visual_plan_version", identity)
            target["knowledge_points"] = sorted(set(target["knowledge_points"]) | set(template.get("knowledge_points", [])))

        point_ids = sorted({p for target in questions for p in target["knowledge_points"]})
        pending = [(point, None) for point in point_ids]
        while pending:
            identity, parent = pending.pop(0)
            if identity in routes:
                continue
            context = query(points=[identity])
            records = [row for page in context["pages"] for row in page["personal_context"].get("knowledge_summaries", [])]
            record = next((row for row in records if (row.get("subpoint_id") or row.get("knowledge_point")) == identity), None)
            require(record is not None, "alignment_point_context_missing:" + identity)
            route = record.get("linear_teaching_route") or {}
            version = None
            if route.get("status") != "current_question_compilation_required":
                version = native_version(route, "native_route_version", identity)
                require(version is not None, "alignment_route_native_version_missing:" + identity)
            routes[identity] = {"identity": identity, "parent": parent, "expected_previous_sha256": version}
            blobs["personal_context/" + identity + "/context.json"] = encoded(context)
            profile_ref = ((record.get("personal_source") or {}).get("profile_object") if native_source else
                           (record.get("learning_profile") or {}).get("profile_object"))
            if parent is None and isinstance(profile_ref, dict):
                relative = safe_path(profile_ref["ref"])
                path = relative if relative.startswith(prefix) else prefix + relative
                profile = json.loads(object_bytes(path, profile_ref["sha256"]))
                for subpoint in profile.get("subpoints", []):
                    sid = subpoint.get("subpoint_id")
                    require(isinstance(sid, str) and re.fullmatch(r"KSP-[0-9a-f]+", sid), "alignment_subpoint_identity_invalid")
                    pending.append((sid, identity))
        blobs["personal_context/SNAPSHOT.json"] = encoded({"source_commit": commit,
            "publication_identity": snap.identity() if hasattr(snap, "identity") else {"source_commit": commit},
            "freshness": {"status": "historical_reference"} if native_source else store.freshness(snap),
            "native_source_version": native_source, "scope": "batch_only", "live_sources_used": bool(native_source)})
        reading["source_commit"] = commit
        reading["status"] = "frozen_native_context" if native_source else "frozen_same_snapshot_context"
        if native_source:
            require(current_identity(repo) == native_identity, "alignment_native_context_advanced")
            reading["native_source_version"] = native_source
        reading["lookup_contract"]["personal_context"]["source_commit"] = commit
        for row in reading["captures"]:
            row["candidate_points"] = next(t["knowledge_points"] for t in questions if row["capture_id"] in t["capture_ids"])
            row["lookup_status"] = reading["status"]
        return commit, questions, list(routes.values()), point_ids, blobs, reading
    finally:
        store.close()


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _syllabus_source_files(repo, source_commit, context_files):
    """Copy local syllabus bytes only when the frozen snapshot proves their SHA.

    The source text is not necessarily a published MCP member. A current local
    path is therefore only a candidate, never authority for a historical input.
    Keep a gap receipt when it cannot be bound; do not break unrelated Capture
    exports or silently substitute another syllabus version.
    """
    bindings, gaps = [], []
    for name, raw in sorted(context_files.items()):
        if not name.startswith(("personal_context/", "question_context/")) or not name.endswith("/context.json"):
            continue
        context = json.loads(raw)
        if context.get("source_commit") != source_commit:
            gaps.append({"reason": "frozen_context_version_drift", "context_path": name})
            continue

        def bind(expected, pointer, object_ref=None):
            binding = {"context_path": name, "context_sha256": hashlib.sha256(raw).hexdigest(),
                       "json_pointer": pointer, "syllabus_source_sha256": expected}
            if object_ref:
                binding["syllabus_object"] = object_ref
            bindings.append(binding)
            if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
                gaps.append({"reason": "frozen_syllabus_sha256_invalid", "context_path": name})

        def visit(value, pointer=""):
            if isinstance(value, dict):
                syllabus = value.get("syllabus_context")
                if isinstance(syllabus, dict):
                    if "syllabus_source_sha256" in syllabus:
                        bind(syllabus["syllabus_source_sha256"], pointer + "/syllabus_context/syllabus_source_sha256")
                    reference = syllabus.get("context_object")
                    if isinstance(reference, dict):
                        # The small MCP view normally carries a content-addressed
                        # reference. Follow only its already-frozen source mapping.
                        ref, sha = reference.get("ref"), reference.get("sha256")
                        matches = [row for row in context.get("source_files", [])
                                   if isinstance(ref, str) and row.get("sha256") == sha
                                   and (row.get("published_path") == ref
                                        or str(row.get("published_path", "")).endswith("/" + ref))]
                        try:
                            require(isinstance(sha, str) and re.fullmatch(r"[0-9a-f]{64}", sha)
                                    and len(matches) == 1, "frozen_syllabus_object_mapping_missing")
                            mapping = matches[0]
                            object_raw = context_files[mapping["package_path"]]
                            require(hashlib.sha256(object_raw).hexdigest() == sha, "frozen_syllabus_object_hash_drift")
                            obj = json.loads(object_raw)
                            require(obj.get("schema") == "cs408-syllabus-context-v1"
                                    and "syllabus_source_sha256" in obj, "frozen_syllabus_object_invalid")
                            bind(obj["syllabus_source_sha256"], pointer + "/syllabus_context/context_object",
                                 {**mapping, "json_pointer": "/syllabus_source_sha256"})
                        except (ValueError, KeyError, AttributeError) as exc:
                            gaps.append({"reason": "frozen_syllabus_object_unavailable", "context_path": name,
                                         "detail": str(exc)})
                for key, child in value.items():
                    visit(child, pointer + "/" + str(key).replace("~", "~0").replace("/", "~1"))
            elif isinstance(value, list):
                for index, child in enumerate(value):
                    visit(child, pointer + "/" + str(index))
        visit(context)
    expected_hashes = {row["syllabus_source_sha256"] for row in bindings
                       if isinstance(row["syllabus_source_sha256"], str)}
    if not bindings:
        gaps.append({"reason": "fixed_snapshot_syllabus_hash_missing"})
    elif len(expected_hashes) != 1:
        gaps.append({"reason": "fixed_snapshot_syllabus_hash_conflict"})
    expected = next(iter(expected_hashes)) if len(expected_hashes) == 1 else None
    candidate, catalog_provenance, text_raw = {}, {}, None
    try:
        text_raw = source_file(Path(repo) / "support/syllabus-2027/source.txt", Path(repo)).read_bytes()
        candidate = {"path": "support/syllabus-2027/source.txt", "sha256": hashlib.sha256(text_raw).hexdigest(),
                     "size_bytes": len(text_raw), "role": "current_local_candidate_only"}
        if candidate["sha256"] != expected:
            gaps.append({"reason": "local_text_does_not_match_fixed_snapshot", "actual_sha256": candidate["sha256"]})
    except (OSError, ValueError) as exc:
        gaps.append({"reason": "local_syllabus_text_unavailable", "detail": str(exc)})
    try:
        catalog_raw = source_file(Path(repo) / "support/syllabus-2027/catalog.json", Path(repo)).read_bytes()
        catalog = json.loads(catalog_raw)
        catalog_source = catalog.get("source") or {}
        catalog_provenance = {"path": "support/syllabus-2027/catalog.json",
                              "sha256": hashlib.sha256(catalog_raw).hexdigest(), "source": catalog_source,
                              "role": "current_local_provenance_only_not_snapshot_catalog"}
        if not isinstance(catalog_source, dict) or catalog_source.get("text_sha256") != expected:
            gaps.append({"reason": "local_catalog_does_not_match_fixed_snapshot"})
    except (OSError, ValueError, AttributeError) as exc:
        gaps.append({"reason": "local_syllabus_catalog_unavailable", "detail": str(exc)})
    available = not gaps and text_raw is not None
    proof = {"schema": "cs408-fixed-syllabus-source-proof-v1", "source_commit": source_commit,
             "status": "available" if available else "source_limited",
             "text_path": SYLLABUS_TEXT_FILE if available else None, "expected_text_sha256": expected,
             "frozen_context_bindings": bindings, "local_text_candidate": candidate,
             "local_catalog_provenance": catalog_provenance, "gaps": gaps,
             "version_boundary": "Text bytes match the fixed snapshot syllabus SHA, not a claim that the local source path or catalog is published at that snapshot. Bindings refer to frozen parent-input contexts; each split retains its own related contexts."}
    result = {SYLLABUS_PROOF_FILE: encoded(proof)}
    if available:
        result[SYLLABUS_TEXT_FILE] = text_raw
    return result


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def require(condition, message):
    if not condition:
        raise ValueError(message)


def instant(text):
    value = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    require(value.tzinfo is not None, "export_cutoff_timezone_required")
    return value.astimezone(dt.timezone.utc)


def _open_package(repo, locator, package_root, expected_sha):
    try:
        return packages.verify_package_binding(locator, private_root=package_root,
                    expected_canonical_sha256=expected_sha, require_sealed=True)
    except packages.ConversationPackageError:
        # Exact archived package only; no archive scan or invented replacement.
        import archive_408_conversation_package as archive
        package_id = locator.removeprefix(packages.LOCATOR_PREFIX)
        proof = archive.reopen_archived_package(repo=repo, package_id=package_id,
                    requested_relative_paths=["conversation.json", "source.json"])
        note = archive._read_locator_note(Path(repo) / archive.LOCATOR_NOTE_ROOT / (package_id + ".md"))
        folder = Path(archive.ARCHIVE_ROOT) / note["raw_archive_relpath"]
        manifest = json.loads((folder / "manifest.json").read_text())
        require(manifest["canonical_sha256"] == proof["package_sha256"], "export_archive_binding_drift")
        require(proof["package_sha256"] == expected_sha, "export_archive_capture_binding_drift")
        objects = {row["relative_path"]: row["json"] for row in proof["requested_objects"]}
        return {"package_dir": str(folder), "manifest": manifest,
                "conversation": objects["conversation.json"], "source": objects["source.json"],
                "archive_proof": proof}


def _visual_reading_index(selected, opened, source_commit):
    """Bounded lookup instructions from this batch only; no live profile scan."""
    require(source_commit is None or re.fullmatch(r"[0-9a-f]{40}", source_commit),
            "export_source_commit_invalid")
    records = []
    point_keys = {"concept_key", "knowledge_point", "knowledge_points", "main_knowledge",
                  "candidate_points", "concept_id", "concept_ids"}
    identity_keys = {"question_identity", "current_question_id", "exam_question_id", "true_item_id"}
    for capture in selected:
        points, identities = set(), []

        def collect(value, location):
            if isinstance(value, dict):
                for key, child in value.items():
                    here = location + "/" + key
                    values = child if isinstance(child, list) else [child]
                    if key in point_keys:
                        points.update(v for v in values if isinstance(v, str) and re.fullmatch(r"(?:DS|CO|OS|CN)\d{2}-\d{2}", v))
                    if key in identity_keys and isinstance(child, str) and child.strip():
                        identities.append({"value": child, "input_locator": here,
                                           "status": "verify_current_question_binding_before_lookup"})
                    collect(child, here)
            elif isinstance(value, list):
                for i, child in enumerate(value):
                    collect(child, location + "/" + str(i))

        cid = capture["capture_id"]
        collect(capture["capture"], f"captures/{cid}/capture.json#")
        for package in opened[cid]:
            package_id = package["manifest"]["package_id"]
            collect(package["source"], f"captures/{cid}/packages/{package_id}/source.json#")
        records.append({"capture_id": cid, "candidate_points": sorted(points),
                        "question_identity_candidates": identities,
                        "lookup_status": "lookup_required",
                        "scope": "current_capture_only_not_mastery_or_confirmed_question_identity"})
    return {"schema": "cs408-enhancement-reading-index-v1", "source_commit": source_commit,
            "status": "lookup_required" if source_commit else "requires_fixed_source_commit",
            "captures": records,
            "lookup_contract": {
                "version_rule": "Fix one verified library source_commit before reading; preserve it for every lookup and partition. Never mix live current with this baseline.",
                "personal_context": {"subject": "408", "view": "direct", "source_commit": source_commit,
                    "knowledge_points": "only this Capture's verified candidate_points; follow returned source mappings to the full old profile, teaching_plan and subpoints"},
                "question_templates": "Verify the current exact question identity from this Capture source first; then use the same-snapshot personalization question route/current_question_id when supported, or its published exact route. Missing or unsupported means unavailable, never substitute a related original or sibling.",
                "point_templates": "Read visual_template from the same-snapshot point context and its source mappings; missing means unavailable.",
                "evidence_boundary": "Current candidate templates and their version are proposals, not evidence that a visual was displayed. Actual displayed visuals, user preference and effect require original conversation turns and original attachments.",
            }}


def _closed_display_receipts(repo, study_date, paths, cutoff, bound):
    """Accept only an explicit native closure bound to this export's saved work."""
    import d_concept_supplement_408 as native
    captures_by_id = {row["capture_id"]: row for row in bound}
    proofs = {}
    for path in paths or []:
        path = Path(path)
        value = json.loads(path.read_bytes())
        session_id = value.get("session_id")
        require(path.resolve() == native.session_closure_path(repo, session_id).resolve(),
                "export_native_session_closure_required")
        closure = native.read_session_closure(repo, session_id)
        require(closure == value and closure.get("reason") == "user_requested_stop"
                and closure.get("current_exposure_preserved") is True
                and closure.get("remaining_learning_status") == "not_completed"
                and closure.get("last_display_date") == study_date
                and instant(closure["closed_at"]) <= cutoff,
                "export_session_closure_scope_drift")
        display = native.read_receipt(Path(closure["current_display_path"]))
        require(display["receipt_sha256"] == closure["current_display_sha256"]
                and display["review_date"] == study_date
                and closure.get("remaining_request_ids", [])[:1] == [display["request_id"]],
                "export_closed_display_binding_drift")
        completed = closure.get("completed_episodes", [])
        require(len(completed) == closure.get("saved_count")
                and len({row["capture_id"] for row in completed}) == len(completed),
                "export_closure_saved_count_drift")
        for row in completed:
            saved = captures_by_id.get(row["capture_id"], {})
            require(all(saved.get(key) == row.get(key)
                        for key in ("capture_id", "capture_payload_sha256", "episode_id")),
                    "export_closure_saved_capture_drift")
        proofs[display["receipt_sha256"]] = closure
    return proofs


def _export_day(repo, study_date, *, output=None, cutoff=None, package_root=None,
               baseline_snapshot_id=None, details_root=None, session_closures=None, capture_ids=None,
               web_handoff=False, evening_source_version=None):
    repo = Path(repo).resolve()
    require(dt.date.fromisoformat(study_date).isoformat() == study_date, "export_study_date_invalid")
    evening_only = evening_source_version is not None
    if evening_only:
        require(output is not None and not web_handoff and baseline_snapshot_id is None
                and re.fullmatch(r"[0-9a-f]{64}", evening_source_version), "evening_native_source_invalid")
        baseline_snapshot_id = evening_source_version
    end = instant(cutoff) if cutoff else dt.datetime.now(dt.timezone.utc)
    root = captures.capture_root(repo)
    lock_path = root / ".lock"
    with (lock_path.open("rb") if lock_path.exists() else nullcontext()) as lock:
        if lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_SH)
        events = captures._load_events(root)
        event_lines = (root / "events.jsonl").read_bytes().splitlines(keepends=True) if (root / "events.jsonl").is_file() else []
        eligible = [event for event in events if instant(event["created_at"]) <= end]
        state = captures.replay(eligible)
    selected = [row for _, row in sorted(state.get("captures", {}).items()) if row["study_date"] == study_date]
    if capture_ids is not None:
        require(capture_ids and len(capture_ids) == len(set(capture_ids)), "export_exact_capture_set_invalid")
        available = {row['capture_id'] for row in selected}
        require(set(capture_ids) <= available, "export_exact_capture_missing_or_other_date")
        selected = [row for row in selected if row['capture_id'] in set(capture_ids)]
    require(selected, "export_no_captures_for_date")
    bound, opened, compatibility, missing = [], {}, [], []
    marker_checks = {}
    for capture in selected:
        cid = capture["capture_id"]
        locators = [loc for loc in captures._capture_evidence_locators(capture) if loc.startswith(packages.LOCATOR_PREFIX)]
        locators.extend(ref['locator'] for ref in capture.get('supplement_history', [])
                        if str(ref.get('locator', '')).startswith(packages.LOCATOR_PREFIX))
        try:
            require(locators, "canonical_package_locator_missing")
            # A supplement can retain its parent refs. Its manifest proves the
            # same Capture episode; retain every version as raw evidence while
            # selecting the latest cumulative version as the enhancement input.
            references = [ref for ref in capture["capture"].get("stable_evidence_refs", [])
                          if isinstance(ref, dict) and str(ref.get("locator", "")).startswith(packages.LOCATOR_PREFIX)]
            history = capture.get("supplement_history", [])
            bindings = {ref["locator"]: ref["sha256"] for ref in references + history}
            require(all(loc in bindings for loc in locators), "export_package_hash_binding_missing")
            versions = [_open_package(repo, loc, package_root, bindings[loc]) for loc in dict.fromkeys(locators)]
            from mark_study_episode_408 import verify_opened
            marker_checks[cid] = [{"package_id": value["manifest"]["package_id"],
                                   **verify_opened(value)} for value in versions]
            primary = history[-1] if history else references[0]
            package = next(value for value in versions
                           if packages.LOCATOR_PREFIX + value["manifest"]["package_id"] == primary["locator"])
            opened[cid] = versions
            inner = package["source"].get("source") or {}
            facts = capture["capture"].get("source_facts") or {}
            kind = inner.get("execution_kind") or facts.get("execution_kind")
            episode = inner.get("episode_id") or facts.get("episode_id")
            if not kind or not episode:
                compatibility.append({"capture_id": cid, "original_episode_id": episode,
                                      "original_execution_kind": kind,
                                      "note": "Legacy Capture identity is the compatibility key, not a fabricated learning event."})
            row = {"capture_id": cid, "capture_payload_sha256": capture["payload_sha256"],
                   "episode_id": episode or cid,
                   "execution_kind": kind or "standalone_question",
                   "evidence_locator": packages.LOCATOR_PREFIX + package["manifest"]["package_id"],
                   "evidence_sha256": package["manifest"]["canonical_sha256"]}
            require(row["execution_kind"] in {"original_review", "concept_supplement", "standalone_question"}, "unknown_execution_kind")
            bound.append(row)
        except (ValueError, OSError, KeyError) as exc:
            missing.append({"capture_id": cid, "reason": str(exc)})
    # Never make an incomplete day look like an all-Capture learning package.
    if missing:
        return {"status": "BLOCKED", "study_date": study_date, "expected_capture_count": len(selected),
                "unresolved": missing, "formal_write_count": 0, "learning_write_count": 0}
    from start_morning_coverage_campaign_408 import pending_for_date
    pending = pending_for_date(repo, study_date=study_date)["episodes"]
    from d_concept_supplement_408 import receipt_root, read_receipt
    bound_displays = {
        ((package["source"].get("source") or {}).get("display_binding") or {}).get("receipt_sha256")
        for versions in opened.values() for package in versions
    }
    closed_displays = _closed_display_receipts(repo, study_date, session_closures, end, bound)
    for path in receipt_root(repo).glob("*.json"):
        display = read_receipt(path)
        if display.get("execution_kind") != "concept_supplement":
            continue
        displayed = instant(display["displayed_at"])
        if (displayed <= end and displayed.astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat() == study_date
                and display["receipt_sha256"] not in bound_displays
                and display["receipt_sha256"] not in closed_displays):
            pending.append({"request_id": display["request_id"], "episode_id": None,
                            "display_receipt_path": str(path), "status": "displayed_unsealed"})
    if pending and capture_ids is None:
        return {"status": "BLOCKED", "reason": "unsealed_episodes_remain", "study_date": study_date,
                "sealed_capture_count": len(bound), "unsealed_episodes": pending,
                "formal_write_count": 0, "learning_write_count": 0}
    if pending:
        sealed_episodes = {row['episode_id'] for row in bound}
        require(not any(row.get('episode_id') in sealed_episodes for row in pending),
                "export_selected_episode_still_unsealed")
    identity = digest(bound)
    details = Path(details_root or str(_paths.details_root())).resolve()
    if output:
        target = Path(output).expanduser().resolve()
    else:
        # 2026-09-27: the end-of-day web enhancement input of the production root goes to T9.
        from web_transfer_root_408 import base_for
        root, _ = base_for("capture-day", details, "learning-deliveries")
        if not web_handoff:
            root = root / "本地正式编纂"
        version = "_" + baseline_snapshot_id[:12] if baseline_snapshot_id and not web_handoff else ""
        target = root / study_date / ("408_learning_" + study_date + "_" + identity[:12] + version + ".zip")
    from web_transfer_root_408 import validate_cold_output
    target = validate_cold_output(target, details=details)
    require(not target.is_relative_to(repo) and not target.is_relative_to(Path(package_root or packages.DEFAULT_ROOT).expanduser().resolve()),
            "export_destination_must_be_outside_source_store")
    if target.exists():
        with zipfile.ZipFile(source_file(target)) as old:
            manifest = json.loads(old.read("manifest.json"))
            requirements = json.loads(old.read(ALIGNMENT_FILE)) if ALIGNMENT_FILE in old.namelist() else None
            for row in manifest.get("files", []):
                raw = old.read(row["path"])
                require(len(raw) == row["size_bytes"] and hashlib.sha256(raw).hexdigest() == row["sha256"],
                        "export_existing_archive_hash_drift")
        require(manifest.get("input_set_sha256") == identity and manifest.get("schema") == SCHEMA, "export_output_conflict")
        frozen_mode = manifest.get("authoring_mode", "legacy_frozen_input")
        if evening_only:
            require(frozen_mode == "evening_evidence" and manifest.get("source_commit") == evening_source_version,
                    "export_existing_evening_source_conflict")
        else:
            require(frozen_mode in {"legacy_frozen_input", "web_handoff" if web_handoff else "local"},
                    "export_existing_input_authoring_mode_conflict")
        registration = register_teaching_input(repo, target, requirements) if requirements else None
        return {"status": "ALREADY_EXPORTED", "path": str(target), "sha256": file_hash(target),
                "authoring_mode": frozen_mode, "source_commit": manifest.get("source_commit"),
                "requested_source_commit": baseline_snapshot_id,
                "teaching_alignment_registration": registration,
                "capture_count": len(bound), "formal_write_count": 0, "learning_write_count": 0}
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".408-day-", dir=target.parent) as temporary:
        stage = Path(temporary)
        files, roles, index = {}, {}, []

        def add_bytes(relative, raw, role):
            relative = safe_path(relative)
            dest = stage / relative
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(raw)
            files[relative], roles[relative] = dest, role

        if capture_ids is not None:
            add_bytes('EXPORT_SCOPE.json', encoded({
                'kind': 'exact_sealed_capture_set', 'capture_ids': sorted(capture_ids),
                'unsealed_outside_scope': pending, 'unsealed_marked_completed': False,
                'active_or_paused_session_changed': False}), 'export_scope')

        if not evening_only:
            baseline_snapshot_id, question_targets, route_targets, affected_points, context_files, reading = \
                _alignment_context(selected, opened, baseline_snapshot_id, stage, local_author=not web_handoff, repo=repo)
            for name, raw in context_files.items():
                add_bytes(name, raw, "fixed_snapshot_teaching_context")
            for name, raw in _syllabus_source_files(repo, baseline_snapshot_id, context_files).items():
                add_bytes(name, raw, "fixed_snapshot_syllabus_source")
            add_bytes("READING_INDEX.json", encoded(reading), "bounded_lookup_index")

        for capture in selected:
            cid = capture["capture_id"]
            add_bytes("captures/" + cid + "/capture.json", encoded(capture["capture"]), "raw_capture")
            add_bytes("captures/" + cid + "/message_boundary_check.json",
                      encoded({"checks": marker_checks[cid],
                               "semantic_review": "核对本题起止、题图/解析归属及是否混入下一题；技术校验不证明语义边界正确。有歧义报告，不改写原包。"}),
                      "message_boundary_check")
            add_bytes("captures/" + cid + "/local_status.json", encoded({
                "capture_id": cid, "study_date": capture["study_date"],
                "capture_payload_sha256": capture["payload_sha256"],
                "quality_status": capture.get("quality_status"),
                "current_batch_id": capture.get("current_batch_id"),
                "formal_id_hint": capture.get("formal_id"),
                "recorded_at": capture.get("recorded_at"),
                "scope": "frozen canonical Capture state; not proof that an unbound formal transaction never committed",
            }), "frozen_local_status")
            for package in opened[cid]:
                manifest = package["manifest"]
                folder = Path(package["package_dir"])
                prefix = "captures/" + cid + "/packages/" + manifest["package_id"] + "/"
                for row in manifest["files"]:
                    relative = safe_path(row["relative_path"])
                    path = source_file(folder / relative, folder)
                    raw = path.read_bytes()
                    require(len(raw) == row["byte_count"] and hashlib.sha256(raw).hexdigest() == row["sha256"], "export_package_changed_during_copy")
                    add_bytes(prefix + relative, raw, row.get("role") or "raw_evidence")
                add_bytes(prefix + "manifest.json", (folder / "manifest.json").read_bytes(), "raw_manifest")
                receipt = folder / "receipt.json"
                if receipt.is_file() and prefix + "receipt.json" not in files:
                    add_bytes(prefix + "receipt.json", receipt.read_bytes(), "raw_receipt")
                if package.get("archive_proof"):
                    add_bytes(prefix + "archive-reopen-proof.json", encoded(package["archive_proof"]), "archive_proof")
                index.append({"capture_id": cid, "package_id": manifest["package_id"],
                              "canonical_sha256": manifest["canonical_sha256"], "path": prefix,
                              "missing_fields": manifest.get("missing_fields", [])})
        add_bytes("capture_index.json", encoded({"captures": bound, "packages": index, "legacy_identity_notes": compatibility}), "index")
        selected_ids = {row["capture_id"] for row in selected}
        event_ids = {event["event_id"] for event in eligible if event.get("capture_id") in selected_ids}
        raw_events = [line for line in event_lines if line.strip() and json.loads(line).get("event_id") in event_ids]
        add_bytes("capture_events.jsonl", b"".join(raw_events), "raw_capture_events")
        for display_sha, closure in closed_displays.items():
            add_bytes("administrative_closures/" + display_sha + ".json", encoded(closure),
                      "administrative_session_closure_not_learning")

        def finish(authoring_mode, requirements=None):
            manifest = {"schema": SCHEMA, "schema_version": 1, "subject": "408", "study_date": study_date,
                        "authoring_mode": authoring_mode,
                        "timezone": "Asia/Shanghai", "run_id": "408-DAY-" + study_date.replace("-", "") + "-" + identity[:16],
                        "baseline_snapshot_id": baseline_snapshot_id, "source_commit": baseline_snapshot_id,
                        "cutoff": end.isoformat(), "input_set_sha256": identity, "capture_count": len(bound),
                        "captures": bound, "capture_set_sha256": capture_set_sha256(selected_ids),
                        "formal_write_count": 0, "learning_write_count": 0,
                        "files": [{"path": name, "sha256": file_hash(file), "size_bytes": file.stat().st_size,
                                   "role": roles[name]} for name, file in sorted(files.items())]}
            add_bytes("manifest.json", encoded(manifest), "manifest")
            write_zip(files, target)
            registration = register_teaching_input(repo, target, requirements) if requirements else None
            return {"status": "EXPORTED", "path": str(target), "sha256": file_hash(target),
                    "capture_count": len(bound), "package_count": len(index), "input_set_sha256": identity,
                    "teaching_alignment_registration": registration,
                    "formal_write_count": 0, "learning_write_count": 0}

        if evening_only:
            add_bytes("START_HERE.md", (
                "408 晚间复盘原始证据\n\n"
                "按 capture_index.json 查看当天完整对话、附件和封存版本。"
                "结合晚间输入外层的本机摘要和正式完成凭据编写复盘。"
            ).encode(), "instructions")
            return finish("evening_evidence")
        for filename, target_name in (
            ("408-formal-enhancement-v1.md", "formal-enhancement-contract.md"),
            ("408-knowledge-profile-v1.md", "knowledge-profile-contract.md"),
            ("408-portable-visual-teaching-v1.md", "408-portable-visual-teaching-v1.md"),
            ("408-portable-question-teaching-v1.md", "408-portable-question-teaching-v1.md"),
            ("question-visual-408-v1.md", "question-visual-408-v1.md"),
            ("source-visual-408-v1.md", "source-visual-408-v1.md"),
            ("web-execution-report-408-v1.md", "web-execution-report-contract.md"),
            ("evening-review-408-v1.md", "evening-review-contract.md"),
            # 2026-10-01: knowledge-profile-contract.md cites this file by name; the web looked for it.
            ("408-syllabus-context-v1.md", "408-syllabus-context-v1.md"),
        ):
            if not web_handoff and filename in {"web-execution-report-408-v1.md", "evening-review-408-v1.md"}:
                continue
            contract = repo / "schema" / filename
            if filename == "408-portable-question-teaching-v1.md":
                require(contract.is_file(), "alignment_portable_contract_missing")
            if contract.is_file():
                add_bytes("contracts/" + target_name, contract.read_bytes(), "contract")
        authoring = repo / "schema/408-web-enhancement-authoring.md"
        if web_handoff and authoring.is_file():
            add_bytes("WEB_AUTHORING_GUIDE.md", authoring.read_bytes(), "instructions")
        template_run = ("WEB-408-" if web_handoff else "LOCAL-408-") + study_date.replace("-", "") + "-" + identity[:16]
        # A later published baseline is a distinct cold input for the same
        # Capture set. Existing registered archives retain their original ID.
        input_prefix = "408-DAY-" if web_handoff else "408-LOCAL-DAY-"
        if not web_handoff:
            # Evening prep and formal curation can freeze distinct files for the same evidence.
            input_prefix += hashlib.sha256(str(target).encode()).hexdigest()[:12] + "-"
        input_id = input_prefix + study_date.replace("-", "") + "-" + identity[:16] + "-" + baseline_snapshot_id
        add_bytes("return-template/enhancement.json", encoded({
            "schema": "cs408-formal-enhancement-v1", "run_id": template_run,
            "source_commit": baseline_snapshot_id or "", "items": [],
        }), "unfilled_template")
        add_bytes("return-template/knowledge_updates.json", encoded({
            "schema": "cs408-knowledge-profile-updates-v1", "updates": [],
        }), "unfilled_template")
        add_bytes("return-template/portable_visual_teaching_plans.json", encoded({
            "schema": "cs408-portable-visual-teaching-plans-v1", "source_commit": baseline_snapshot_id or "",
            "study_date": study_date, "part_id": None, "items": [],
        }), "unfilled_template")
        add_bytes("return-template/portable_question_teaching_updates.json", encoded({
            "schema": "cs408-portable-question-teaching-v1", "source_commit": baseline_snapshot_id,
            "study_date": study_date, "input_binding": {"input_id": input_id, "requirements_sha256": ""},
            "questions": [], "routes": [],
        }), "unfilled_template")
        if web_handoff:
            add_bytes("START_HERE.md", (
                "# 408 原始学习总包\n\n本包是指定日期的完整Capture证据，不是正式入库决定。"
                "正确题、提示后完成、未解决和点级补充保持原始身份；Capture不代表错误。\n\n"
                "先读各Capture的message_boundary_check.json并核对完整对话与附件归属。"
                "技术核验不证明题目语义边界正确；错标或歧义须报告，不能静默裁剪、改写或补造。\n\n"
                "请交给408正式入库增强业务，完整读取本批原件后，结合MCP同一固定快照的正式历史，"
                "按 support/d408/formal-enhancement-contract.md 与 knowledge-profile-contract.md "
                "输出最终 enhancement.json 和 knowledge_updates.json。"
                "manifest.captures可直接沿用，snapshot/source_commit必须沿用本包已核固定source_commit；"
                "它是增强前读取基线，不是本地正式更新后的新版本，禁止另换current。\n\n"
                "先读READING_INDEX.json：若已有source_commit，全部旧个人教法、点模板和精确题模板只读该固定版本；"
                "本包已冻结本批相关旧概况、教案和版本；MCP仅按同版有界补查缺项。"
                "原纲先读contracts/2027-syllabus-source-proof.json；status=available时读取其text_path"
                "（contracts/2027-syllabus-source.txt），该文字已与固定快照syllabus_source_sha256逐字节校验。"
                "source_limited时保留证明中的缺口，不猜MCP原纲路径、不换current版补历史。"
                "只查本批实际相关点和已核验的当题身份，不用关联原型猜当前题；缺失如实记录。"
                "旧teaching_plan逐项保留、修订或显式退役。当前候选模板不证明学习中曾展示，也不证明帮助或掌握。"
                "实际视觉反馈只引用完整原会话回合与附件；需要更新教法时按contracts/408-portable-visual-teaching-v1.md"
                "另交portable_visual_teaching_plans.json，列入manifest.files且role=supplemental，不填本机绝对路径。\n\n"
                "个人摘要先按同版syllabus_context.exam_frequency审阅高频点的真实断点、提示依赖和next_check；"
                "低频点仍完整更新，不因频次丢弃证据或改动掌握结论。频次等级、主考/总考次及来源写入PERSONALIZATION_REVIEW_REPORT。"
                "每个Capture和知识点独立处理并及时保存成功成稿；单项失败记录后继续其他不依赖它的工作。"
                "无论完整、PARTIAL或BLOCKED，最终都必须交已有实际文件、EXECUTION_REPORT.md和web_execution_report.json，"
                "列明成功、失败、未执行及真实失败调用的工具名、参数、原始错误/返回；缺原生call_id则null，不编造。"
                "报告按support/d408/web-execution-report-contract.md；manifest增加web_execution_report引用并给该文件相同role。"
                "不能因为最后一项失败把前面成功清零，真实零成功才报告零。\n\n"
                "ZIP生成、可下载附件交付和Library持久保存分别核验；检查ZIP完整性、大小与SHA-256，"
                "用实际平台附件能力交付，不猜sandbox链接。Library须有覆盖本批文件和具体目的地的用户授权；"
                "若下载依赖该保存，保全成稿后只请求必要授权，不把包内文字当授权。权限拒绝与network失败分开记录，"
                "不换工具绕过拒绝；网络失败仅有依据恢复一次，结果不明先查回执。只续接失败交付层，不重做成稿。\n\n"
                "回包由本地保全成功成果、审核原题/考纲/真题依据、个性化判断和具体讲法，再完成正式入库；已COMMITTED不重复写。"
                "本地按真实错误仅补缺层；正式记录、摘要及本机讲题验回完成后，晨间备课沿本地入口。\n\n"
                "正式判分、FSRS和个人摘要由加强包本地入库统一更新；本次导出不产生学习或正式事件。\n"
                "\n先读WEB_AUTHORING_GUIDE.md和contracts。return-template是待填模板，不是完成结果；"
                "网页应补齐语义、真实来源引用和附件可见性枚举，只将本机回执/事务工作留到本地。"
                "正式ID的来源绑定用question_refs查询；日期/run/Capture共同限定所有MCQ说明。\n"
                "\n增强回包额外交evening_review_seed.json，按contracts/evening-review-contract.md的seed合同，"
                "只列本片真实机制断点、首答/提示后变化、原回合和建议主动回忆检查，并加入manifest.files。"
                "它仍绑定增强前输入版本，只是晚间候选；本地完成全天增强入库后按授权使用新版本，"
                "在现有晚间入口本地编排问答，不在本增强任务启动复盘或伪造表现。\n"
            ).encode(), "instructions")
            alignment_guide = (
                "# 本批教学对齐必交项\n\n"
                "先读TEACHING_ALIGNMENT_REQUIREMENTS.json及contracts/408-portable-question-teaching-v1.md。"
                "教学优先级以新portable合同和已核exam_visual_binding为准；"
                "question-visual/source-visual合同提供字段说明，旧热点示例不能覆盖原图结构与本问步骤绑定。"
                "requirements.required=true是本批完整交付要求，不能省略文件或改字段绕过。"
                "source_commit绑定一个实际发布快照；网页MCP只读该版本，不直接读取或写入实时本地数据库。\n\n"
                "原纲来源证明见contracts/2027-syllabus-source-proof.json；只有status=available才使用"
                "contracts/2027-syllabus-source.txt，并核对固定context中的syllabus_source_sha256。"
                "未匹配时包内仅保留source_limited证明，项目其他版本不得静默替代。\n\n"
                "结合项目已上传且版本吻合的2027原纲、真题与教材、当前题完整原件，以及同版MCP真实个人历史，"
                "交付knowledge_updates.json中的完整个人摘要，以及portable_visual_teaching_plans.json中的父点/小点计划；同一目标不重复交内联计划。"
                "以及portable_question_teaching_updates.json中的精确题template、source_plan与父点/小点route。"
                "精确题/路线显式retain/revise/source_limited，个人计划按原合同retain/revise/retire。保留原题图的字段、对象、布局、方向及初态；"
                "文字讲清主线，图解绑定具体步骤，适度拓展必须有真实题设差异或考纲依据。"
                "附件存在或OCR文字不证明原图已查看；未看到或冲突时记录source_limited，不能猜图。\n\n"
                "question_targets仅记录Capture已核的精确题身份；null不授权改用原型、关联旧题或兄弟小问。"
                "未知题可用同Capture enhancement.new.logical_question_id；实际为已有真题时须由本地审核并登记canonical绑定，否则保留source_limited；"
                "affected_points是候选范围，真实更新点集从本批观察核定，不能把候选当作个人弱点。"
                "旧模板/源图方案/route版本分别使用登记的native SHA；缺版本不能用当前live数据补历史快照。\n\n"
                "return-template为空模板。input_binding.requirements_sha256填写本包requirements原始字节SHA-256；"
                "把成稿portable_question_teaching_updates.json按role=supplemental列入回包manifest.files。"
                "原始会话和附件保持完整；只读备稿不产生正式学习。"
                "本地核证据与教法后正式更新，重建并验回原生讲题查询。只有用户明确要求远程访问或外发才发布。已提交只补缺层。\n"
            ).encode()
            add_bytes("TEACHING_ALIGNMENT_GUIDE.md", alignment_guide, "instructions")
            add_bytes("START_HERE.md", "先读 TEACHING_ALIGNMENT_GUIDE.md，本批正式成稿必须同步交付教学更新。\n\n".encode() + files["START_HERE.md"].read_bytes(), "instructions")
        else:
            add_bytes("START_HERE.md", (
                "本地正式增强编纂\n\n"
                "本包使用 kaoyan-408-enhancement-intake 的本机作者合同和原生唯一写者。\n"
                "在当前已授权的本地会话中核对本包 Capture、原始消息/附件、固定快照及旧概况。按 TEACHING_ALIGNMENT_REQUIREMENTS.json 确认精确范围与原生版本。"
                "逐题以真实首答、机制断点、提示后变化和未验证边界成稿。学情证据与考纲、真题的客观考法分开。\n"
                "从 return-template 取字段，在经校验的本地工作目录生成正式成稿。确定性模板不能替代实际语义审核。"
                "同点先合并完整历史、父点与KSP，再由 enhancement_intake_408.py prepare --source-dir <成稿目录> 校验，"
                "apply --prepared <回执> 串行提交，随后 status 与同版原生讲题入口验回摘要、版本和答案权限。"
                "中断用原回执续办。只有用户明确要求远程访问或外发才发布。"
                "仅请求准备材料时停在冻结输入，不触发写者。"
            ).encode(), "instructions")
            add_bytes("TEACHING_ALIGNMENT_GUIDE.md", (
                "用冻结的本机来源版本核对当前题级教案、父点和 KSP。原题图及历史出处沿本包已核的固定来源身份回读。"
                "按 contracts/408-portable-question-teaching-v1.md 组织题模板、源图及路线；按 408-portable-visual-teaching-v1.md 衔接个人讲法。"
                "保留真实题图字段、编号、方向与初始状态，给出文字主线和可点击图解。扩展需有考纲和已接纳真题双重依据。"
                "学生展示共同遵守年度与阶段隔离；完整教师材料不直接进入提示视图。"
                "目录标记须另起一行写“细查小点：”，逐项包含现行标题和至少一个准确关键词。"
                "候选题号及考点只用来找同版教师资料。当前题身份、真正薄弱点及原生版本必须另核。"
                "缺来源、材料冲突或等待证实的边界保留原状，逐项保存已有成稿。"
            ).encode(), "instructions")
        requirements = {"schema": ALIGNMENT_SCHEMA, "required": True, "input_id": input_id,
            "source_commit": baseline_snapshot_id, "study_date": study_date,
            "capture_ids": sorted(selected_ids), "capture_set_sha256": capture_set_sha256(selected_ids),
            "affected_points": affected_points, "question_targets": question_targets, "route_targets": route_targets,
            "source_files": [{"path": name, "sha256": file_hash(path)} for name, path in sorted(files.items())]}
        add_bytes(ALIGNMENT_FILE, encoded(requirements), "required_teaching_alignment")
        return finish("web_handoff" if web_handoff else "local", requirements)


def export_day(repo, study_date, *, output=None, cutoff=None, package_root=None,
               baseline_snapshot_id=None, details_root=None, session_closures=None, capture_ids=None,
               web_handoff=False):
    result = _export_day(repo, study_date, output=output, cutoff=cutoff, package_root=package_root,
                         baseline_snapshot_id=baseline_snapshot_id, details_root=details_root,
                         session_closures=session_closures, capture_ids=capture_ids, web_handoff=web_handoff)
    if web_handoff and result.get("capture_count", 0) > 40 and result.get("path"):
        try:
            from split_web_input_408 import split_captures
            result.update(split_captures(result["path"], repo=repo))
        except Exception as exc:
            result.update(partition_status="PENDING", partition_error=str(exc))
    if result.get("path"):
        from web_transfer_root_408 import register_output, PRODUCTION_DETAILS
        details = Path(details_root) if details_root else PRODUCTION_DETAILS
        roots = [Path(result["path"])]
        if result.get("partition_set_path"):
            roots.append(Path(result["partition_set_path"]).parent)
        result["storage_registrations"] = [register_output(path, details=details,
            purpose="capture-day-web-input" if web_handoff else "capture-day-local-author-input") for path in roots]
        result.setdefault("authoring_mode", "web_handoff" if web_handoff else "local")
        result["next_action"] = "explicit_web_handoff" if web_handoff else "continue_local_semantic_authoring"
        result["next_step_zh"] = ("本次已明确选择手动外发，请使用本批兼容作者输入。"
                                  if web_handoff else "本地证据已就绪。已授权正式入库时，当前会话继续审核成稿并通过原生写者提交。")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--date", required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--cutoff")
    parser.add_argument("--package-root", type=Path)
    parser.add_argument("--details-root", type=Path)
    parser.add_argument("--web-handoff", action="store_true", help="Explicit external delivery with web instructions and optional partitions")
    parser.add_argument("--source-commit", help="Pin the original evidence and teaching context to this verified local snapshot")
    parser.add_argument('--capture-id', action='append', help='Exact sealed Capture scope; repeat per item, leaving other active frontiers untouched')
    parser.add_argument("--session-closure", type=Path, action="append", default=[],
                        help="Native user-stop receipt for an exposed, unanswered D frontier")
    args = parser.parse_args()
    try:
        result = export_day(args.repo, args.date, output=args.output, cutoff=args.cutoff,
                            package_root=args.package_root, details_root=args.details_root,
                            baseline_snapshot_id=args.source_commit, session_closures=args.session_closure,
                            capture_ids=args.capture_id, web_handoff=args.web_handoff)
    except Exception as exc:
        result = {"status": "BLOCKED", "reason": str(exc), "formal_write_count": 0, "learning_write_count": 0}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] in {"EXPORTED", "ALREADY_EXPORTED"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
