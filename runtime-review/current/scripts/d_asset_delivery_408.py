"""Materialize web D image references from one verified publication, without intake.

The web ZIP remains immutable. Only a derived delivery directory is written;
neither asset copying nor a receipt upgrades semantic coverage or starts study.
"""
from __future__ import annotations

import csv
import json
from pathlib import Path
import re
import shutil
import sys
import tempfile

import runtime_paths_408 as _paths

BRIDGE_ROOT = _paths.bridge_root()
if str(BRIDGE_ROOT) not in sys.path:
    sys.path.insert(0, str(BRIDGE_ROOT))
from bridge_archives import check_manifest, file_hash, mac_metadata, require, safe_path, source_file

CONTRACT = "snapshot_refs_v1"
PRESENTATION_POLICY = "verified_snapshot_assets_v1"
NATIVE_CONTRACT = "native_refs_v1"
NATIVE_PRESENTATION = "verified_native_assets_v1"
RECEIPT_PATH = "private/local-asset-assembly.json"
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif"}


def _read(path):
    return json.loads(source_file(path).read_text(encoding="utf-8"))


def _rows(path):
    return [json.loads(line) for line in source_file(path).read_text(encoding="utf-8").splitlines()
            if line.strip()]


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _style_reference_bindings(source_commit, library, items, web_variants, published=None):
    """Resolve private anchor ownership from hash-checked fixed publication metadata."""
    if not web_variants:
        return {}
    require(isinstance(web_variants, list) and all(isinstance(row, dict) for row in web_variants),
            "D_asset_web_variants_required")
    if published is None:
        from study_bridge import Published
        published = Published(BRIDGE_ROOT / "publication")
    files = {row["path"]: row for row in library.get("files", [])}
    entrypoints = library.get("entrypoints", {})
    guide = safe_path(entrypoints.get("variant_style_guide"))
    profile_path = str(Path(guide).with_name("style-profiles.jsonl"))
    index_path = safe_path(entrypoints.get("canonical_exam_index"))
    require(profile_path in files and index_path in files, "D_asset_style_metadata_unavailable")
    with tempfile.TemporaryDirectory(prefix="d-style-reference-") as temporary:
        root = Path(temporary)
        profile_file = published.materialize("408", source_commit, profile_path, root / "profiles.jsonl",
                                             files[profile_path]["sha256"])
        index_file = published.materialize("408", source_commit, index_path, root / "index.csv",
                                           files[index_path]["sha256"])
        profiles = {row["profile_id"]: row for row in _rows(profile_file)}
        with index_file.open(encoding="utf-8-sig", newline="") as handle:
            canonical = {row["exam_question_id"]: row for row in csv.DictReader(handle)}
    result = {}
    for variant in web_variants:
        web_id = variant.get("web_variant_id")
        owners = [item for item in items if item.get("web_variant_id") == web_id
                  and item.get("question_id") == variant.get("question_id")]
        profile = profiles.get(variant.get("style_profile_id"), {})
        point = variant.get("target_concept_id")
        if (len(owners) != 1 or not profile or profile.get("main_knowledge") != point
                or point not in owners[0].get("review_target_concept_ids", [])):
            continue
        for anchor in variant.get("canonical_anchor_commitments") or []:
            cid = anchor.get("canonical_id") if isinstance(anchor, dict) else None
            row = canonical.get(cid, {})
            if cid not in profile.get("canonical_ids", []) or str(row.get("main_knowledge") or "").split(" ", 1)[0] != point:
                continue
            for field in ("question_assets", "solution_assets"):
                for source in json.loads(row.get(field) or "[]"):
                    source = "details/" + safe_path(source)
                    if source in files:
                        result.setdefault(variant["question_id"], set()).add(source)
    return result


def plan_assets(refs, source_commit, library, items, web_variants=None, *, published=None):
    """Check source ownership and public/private roles before copying any bytes."""
    from native_morning_sources_408 import SOURCE_BASIS
    native = library.get("source_basis") == SOURCE_BASIS
    require(bool(re.fullmatch(r"[0-9a-f]{64}" if native else r"[0-9a-f]{40}", str(source_commit))),
            "D_asset_native_source_version_required" if native else "D_asset_snapshot_required")
    require(library.get("subject") == "408", "D_asset_subject_mismatch")
    require(isinstance(refs, list) and len(refs) <= 5000, "D_asset_refs_array_required")
    require(isinstance(items, list) and all(isinstance(item, dict) for item in items),
            "D_asset_items_required")
    files = {row["path"]: row for row in library.get("files", [])}
    question_refs = library.get("question_refs", {})
    canonical = {row["question_id"]: row for row in library.get("original_mappings", [])}
    result, destinations = [], set()
    style_bindings = None
    for ref in refs:
        require(isinstance(ref, dict), "D_asset_ref_object_required")
        fid = ref.get("question_id")
        owners = [item for item in items if item.get("question_id") == fid]
        require(owners and fid in question_refs, "D_asset_question_not_selected", fid)
        source = safe_path(ref.get("source_path"))
        target = safe_path(ref.get("package_path"))
        require(target not in destinations, "D_asset_duplicate_package_path", target)
        destinations.add(target)
        require(Path(source).suffix.lower() in IMAGE_SUFFIXES
                and Path(target).suffix.lower() == Path(source).suffix.lower(),
                "D_asset_image_extension_required", source)
        binding = question_refs[fid]
        questions = set(binding.get("question_images", []))
        solutions = set(binding.get("solution_images", []))
        # A concept-only replacement uses the chosen exam's frozen images;
        # the historical node is not an assertion that they are its original.
        exact_owners = [item for item in owners
                        if re.fullmatch(r"EXAM408-20\d{2}-Q\d{2}(?:-P\d{2})?", str(
                            item.get("preferred_true_item_id") or item.get("true_item_id") or ""))]
        if exact_owners and canonical:
            chosen = {item.get("preferred_true_item_id") or item.get("true_item_id") for item in exact_owners}
            require(chosen <= canonical.keys(), "D_asset_canonical_identity_missing")
            require(ref.get("true_item_id") in (None, *chosen), "D_asset_canonical_identity_mismatch")
            if len(chosen) > 1:
                require(ref.get("true_item_id") in chosen, "D_asset_shared_anchor_requires_exact_identity")
            if ref.get("true_item_id"):
                chosen = {ref["true_item_id"]}
            questions = {p for cid in chosen for p in canonical[cid].get("question_images", [])}
            solutions = {p for cid in chosen for p in canonical[cid].get("solution_images", [])}
        if source not in questions | solutions:
            require(not native and ref.get("role") == "reference" and target.startswith("private/"),
                    "D_asset_not_in_question_publication", source)
            if style_bindings is None:
                style_bindings = _style_reference_bindings(
                    source_commit, library, items, web_variants, published)
            require(source in style_bindings.get(fid, set()), "D_asset_not_in_question_publication", source)
        require(source in files, "D_asset_not_in_question_publication", source)
        row = files[source]
        require(ref.get("source_commit", source_commit) == source_commit, "D_asset_mixed_snapshot")
        require(ref.get("source_sha256") in (None, row["sha256"]), "D_asset_source_hash_mismatch", source)
        require(ref.get("size") in (None, row["bytes"]), "D_asset_source_size_mismatch", source)
        role, visual = ref.get("role"), ref.get("visual_status")
        require(role in {"question", "solution", "reference"}, "D_asset_role_required")
        require(visual in {"viewed", "not_viewed"}, "D_asset_visual_status_required")
        if role == "question":
            require(source in questions and source not in solutions,
                    "D_solution_asset_cannot_be_public", source)
            require(target.startswith(f"questions/{fid}/assets/"), "D_public_asset_path_required")
            displaying = [item for item in owners if target in (item.get("image_paths") or [])]
            require(displaying and all(
                not item.get("web_variant_id") and (
                    not item.get("preferred_true_item_id")
                    or re.fullmatch(r"(?:EXAM408-20\d{2}-Q\d{2}(?:-P\d{2})?|FOS-[0-9A-F]{24})",
                                    str(item["preferred_true_item_id"])))
                for item in displaying), "D_original_image_is_not_variant_surface")
            require(visual == "viewed", "D_required_question_image_not_viewed")
        else:
            require(target.startswith("private/"), "D_private_asset_path_required")
            require(all(target not in (item.get("image_paths") or []) for item in items),
                    "D_private_asset_in_public_image_paths")
            if role == "solution":
                require(source in solutions and visual == "viewed", "D_solution_asset_binding_required")
        hash_origin = ("native_inventory" if native else
                       "published_inventory" if ref.get("source_sha256") is None else "web_reference_matched_publication")
        result.append({"question_id": fid, "source_path": source, "source_sha256": row["sha256"],
                       "size": row["bytes"], "package_path": target, "role": role,
                       "visual_status": visual, "hash_origin": hash_origin})
    for item in items:
        require(all(isinstance(path, str) and not path.startswith("private/")
                    for path in item.get("image_paths", [])), "D_private_asset_in_public_image_paths")
    return result


def validate_presentation_crops(body):
    """Verify preparation-only, pixel-exact header removal from bound public images."""
    from PIL import Image
    crops = body.get("question_presentation_crops", [])
    require(isinstance(crops, list), "D_presentation_crops_array_required")
    root = Path(body["output_root"]).resolve()
    sources = {ref["package_path"]: ref for ref in body.get("asset_refs", [])
               if ref.get("role") == "question"}
    seen, output = set(), []
    for crop in crops:
        source, target = safe_path(crop["source_package_path"]), safe_path(crop["package_path"])
        ref = sources.get(source, {})
        require(source not in seen and target.startswith(f"questions/{ref.get('question_id')}/presentation/")
                and crop.get("source_sha256") == ref.get("source_sha256")
                and crop.get("question_id") == ref.get("question_id")
                and crop.get("true_item_id") == ref.get("true_item_id"),
                "D_presentation_crop_source_binding_invalid")
        seen.add(source)
        source_path, target_path = source_file(root / source, root), source_file(root / target, root)
        require(file_hash(source_path) == crop["source_sha256"]
                and file_hash(target_path) == crop["sha256"], "D_presentation_crop_hash_drift")
        box = crop.get("crop_box")
        with Image.open(source_path) as original, Image.open(target_path) as derived:
            require(isinstance(box, list) and len(box) == 4 and all(type(v) is int for v in box)
                    and box[0] == 0 and 0 < box[1] < original.height
                    and box[2:] == [original.width, original.height],
                    "D_presentation_crop_must_only_remove_header")
            expected, actual = original.crop(box).convert("RGB"), derived.convert("RGB")
            require(expected.size == actual.size and expected.tobytes() == actual.tobytes(),
                    "D_presentation_crop_pixels_changed")
        output.append({"relative_path": target, "sha256": crop["sha256"],
                       "byte_count": target_path.stat().st_size})
    return output


def prepared_question_assets(plan, item):
    """Read an opted-in D plan's verified public images without changing its identity.

    Import already verifies each reference's ownership in the fixed publication.
    The original selection/surface commitment stays intact for scheduling, while
    these exact displayed bytes are also sealed with the learning evidence.
    """
    if plan.get("question_presentation") is None:
        return []
    from native_morning_sources_408 import SOURCE_BASIS
    expected = ((NATIVE_PRESENTATION, NATIVE_CONTRACT) if plan.get("source_basis") == SOURCE_BASIS
                else (PRESENTATION_POLICY, CONTRACT))
    require((plan.get("question_presentation"), plan.get("asset_delivery")) == expected,
            "D_question_presentation_policy_invalid")
    selection = item.get("selection", {})
    require(selection.get("surface_kind") == "canonical_original",
            "D_prepared_presentation_requires_canonical_original")
    fid, identity = item["question_id"], selection["true_item_id"]
    root = Path(plan["output_root"]).resolve()
    files = {row["relative_path"]: row for row in plan["output_files"]}
    refs = [ref for ref in plan.get("asset_refs", [])
            if ref.get("question_id") == fid and ref.get("role") == "question"
            and ref.get("true_item_id") in (None, identity)]
    require(bool(refs), "D_prepared_question_images_missing", identity)
    assets = []
    crops = {row["source_package_path"]: row for row in plan.get("question_presentation_crops", [])}
    for ref in refs:
        relative = safe_path(ref["package_path"])
        require(relative.startswith(f"questions/{fid}/assets/"), "D_public_asset_path_required")
        row = files.get(relative, {})
        require(row.get("sha256") == ref.get("source_sha256")
                and ref.get("source_commit", plan["source_commit"]) == plan["source_commit"],
                "D_prepared_question_binding_drift", relative)
        path = source_file(root / relative, root)
        require(path.stat().st_size == row.get("byte_count") and file_hash(path) == row["sha256"],
                "D_prepared_question_bytes_drift", relative)
        crop = crops.get(relative)
        if crop:
            require(crop.get("source_sha256") == row["sha256"], "D_presentation_crop_source_binding_invalid")
            relative = safe_path(crop["package_path"])
            require(relative.startswith(f"questions/{fid}/presentation/"), "D_public_asset_path_required")
            row = files.get(relative, {})
            path = source_file(root / relative, root)
            require(row.get("sha256") == crop.get("sha256") and file_hash(path) == row["sha256"]
                    and path.stat().st_size == row.get("byte_count"), "D_presentation_crop_hash_drift")
        assets.append({"path": str(path), "ref": relative, "sha256": row["sha256"],
                       "size_bytes": row["byte_count"], "source_path": ref["source_path"],
                       "source_commit": plan["source_commit"],
                       **({"source_sha256": crop["source_sha256"], "crop_box": crop["crop_box"]} if crop else {})})
    return assets


def materialize_assets(plan, source_commit, output_root, published, *, native_sources=None):
    """Copy exact immutable source bytes; duplicate hashes reuse one local read."""
    output_root = output_root.resolve()
    by_hash, output_files = {}, []
    for ref in plan:
        target = output_root / ref["package_path"]
        require(not any(path.is_symlink() for path in [target, *target.parents]), "D_asset_symlink_rejected")
        expected = ref["source_sha256"]
        if target.exists():
            require(file_hash(source_file(target, output_root)) == expected,
                    "D_asset_existing_content_conflict", ref["package_path"])
        elif expected in by_hash:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(by_hash[expected], target)
        elif native_sources is not None:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(native_sources.path(ref["source_path"]), target)
        else:
            published.materialize("408", source_commit, ref["source_path"], target, expected)
        require(target.stat().st_size == ref["size"] and file_hash(target) == expected,
                "D_asset_materialized_bytes_mismatch", ref["package_path"])
        by_hash[expected] = target
        output_files.append({"relative_path": ref["package_path"], "sha256": expected,
                             "byte_count": ref["size"]})
    return {"schema": "cs408-D-asset-assembly-v1", "status": "ASSETS_MATERIALIZED_NOT_IMPORTED",
            "source_commit": source_commit, "assets": plan, "output_files": output_files,
            "asset_reference_count": len(plan), "unique_content_count": len(by_hash),
            "visual_review_claims_preserved_not_verified": True,
            "semantic_coverage_upgraded": False, "learning_started": False,
            "formal_write_count": 0, "learner_evidence_write_count": 0}


def validate_materialized_assets(body, library):
    """Native import must check image bytes and roles before admitting variants."""
    if "asset_delivery" not in body:
        require(not body.get("asset_refs"), "D_asset_delivery_contract_required")
        return []
    from native_morning_sources_408 import SOURCE_BASIS
    expected = NATIVE_CONTRACT if library.get("source_basis") == SOURCE_BASIS else CONTRACT
    require(body["asset_delivery"] == expected, "D_asset_delivery_contract_unsupported")
    items = body.get("selected_items", []) + body.get("supplemental_items", []) + body.get("substitute_items", [])
    plan = plan_assets(body.get("asset_refs"), body["source_commit"], library, items, body.get("web_variants"))
    root = Path(body["output_root"]).resolve()
    result = []
    for ref in plan:
        path = source_file(root / ref["package_path"], root)
        require(path.stat().st_size == ref["size"] and file_hash(path) == ref["source_sha256"],
                "D_asset_materialized_bytes_mismatch", ref["package_path"])
        result.append({"relative_path": ref["package_path"], "sha256": ref["source_sha256"],
                       "byte_count": ref["size"]})
    return result


def assemble_delivery(root, delivery, manifest, *, max_bytes=2 * 1024**3, native_sources=None):
    """Return a checked derived copy; never edit the original ZIP or web-output."""
    if "asset_delivery" not in manifest:
        require(not (root / "asset_refs.jsonl").exists(), "D_asset_delivery_contract_required")
        return root, manifest, None
    expected_contract = NATIVE_CONTRACT if native_sources is not None else CONTRACT
    require(manifest["asset_delivery"] == expected_contract, "D_asset_delivery_contract_unsupported")
    require(manifest.get("project") == "D" and manifest.get("subject") == "408",
            "D_asset_project_required")
    require(not (root / RECEIPT_PATH).exists() and "local_asset_assembly" not in manifest,
            "D_reserved_local_asset_receipt")
    source_commit = manifest["source_commit"]
    require(manifest.get("snapshot_id", source_commit) == source_commit, "D_asset_snapshot_mismatch")
    refs = _rows(root / "asset_refs.jsonl")
    items = _rows(root / "selected_items.jsonl") + _rows(root / "supplemental_items.jsonl")
    if (root / "substitute_items.jsonl").is_file():
        items += _rows(root / "substitute_items.jsonl")
    web_variants = _rows(root / "web_variants.jsonl") if (root / "web_variants.jsonl").is_file() else []
    published = None
    if native_sources is not None:
        require(not web_variants and source_commit == native_sources.version, "D_native_asset_source_version_drift")
        library = native_sources.import_inventory(items, refs)
    else:
        from study_bridge import Published
        published = Published(BRIDGE_ROOT / "publication")
        _, library = published.inspect_version("408", source_commit)
    plan = plan_assets(refs, source_commit, library, items, web_variants, published=published)
    original_bytes = sum(row["size"] for row in manifest["files"]) + (root / "manifest.json").stat().st_size
    missing_bytes = sum(ref["size"] for ref in plan if not (root / ref["package_path"]).exists())
    require(original_bytes + missing_bytes <= max_bytes, "D_assembled_assets_exceed_byte_budget")
    input_hash = file_hash(root / "manifest.json")
    output = delivery / "assembled-output-v1"
    if not output.exists():
        with tempfile.TemporaryDirectory(prefix=".d-assets-", dir=delivery) as work:
            stage = Path(work) / "content"
            shutil.copytree(root, stage)
            receipt = materialize_assets(plan, source_commit, stage, published, native_sources=native_sources)
            receipt["source_manifest_sha256"] = input_hash
            _write(stage / RECEIPT_PATH, receipt)
            roles = {row["path"]: row["role"] for row in manifest["files"]}
            roles.update({ref["package_path"]: "materialized_" + ref["role"] + "_image" for ref in plan})
            roles[RECEIPT_PATH] = "local_asset_assembly_receipt_private"
            assembled = dict(manifest, local_asset_assembly=RECEIPT_PATH)
            assembled["files"] = [{"path": path.relative_to(stage).as_posix(), "sha256": file_hash(path),
                                   "size": path.stat().st_size, "role": roles[path.relative_to(stage).as_posix()]}
                                  for path in sorted(stage.rglob("*"))
                                  if path.is_file() and path != stage / "manifest.json" and not mac_metadata(path)]
            _write(stage / "manifest.json", assembled)
            check_manifest(stage)
            stage.rename(output)
    assembled = check_manifest(output)
    require({key: value for key, value in assembled.items() if key not in {"files", "local_asset_assembly"}}
            == {key: value for key, value in manifest.items() if key != "files"}, "D_assembled_metadata_drift")
    for row in manifest["files"]:
        require(file_hash(source_file(output / row["path"], output)) == row["sha256"],
                "D_assembled_web_content_changed", row["path"])
    receipt = _read(output / RECEIPT_PATH)
    require(receipt.get("source_manifest_sha256") == input_hash and receipt.get("assets") == plan,
            "D_asset_receipt_drift")
    validate_materialized_assets({"asset_delivery": expected_contract, "asset_refs": refs, "source_commit": source_commit,
                                  "selected_items": items, "web_variants": web_variants,
                                  "output_root": str(output)}, library)
    return output, assembled, {"path": str(output / RECEIPT_PATH),
                               "sha256": file_hash(output / RECEIPT_PATH),
                               "asset_reference_count": receipt["asset_reference_count"],
                               "unique_content_count": receipt["unique_content_count"]}
