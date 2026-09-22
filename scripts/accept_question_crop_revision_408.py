#!/usr/bin/env python3
"""Fail-closed adoption of root-accepted true-exam question crop revisions.

This utility deliberately has no corpus builder or release-builder dependency.
Without ``--apply`` it only validates the audit evidence and prints a revision
plan.  ``--apply`` copies accepted PNGs into an immutable revision directory
and updates the three external corpus references as one recoverable operation.
"""

from __future__ import annotations

import argparse
import csv
import fcntl
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class AdoptionError(RuntimeError):
    pass


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    if not path.is_file():
        raise AdoptionError(f"missing file: {path}")
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AdoptionError(f"invalid JSON {path}: {exc}") from exc


def expect(condition: bool, message: str) -> None:
    if not condition:
        raise AdoptionError(message)


def item_map(document: dict[str, Any], path: Path) -> dict[str, dict[str, Any]]:
    items = document.get("items")
    expect(isinstance(items, list), f"{path}: items must be a list")
    mapped: dict[str, dict[str, Any]] = {}
    for item in items:
        expect(isinstance(item, dict) and isinstance(item.get("id"), str), f"{path}: invalid item")
        identifier = item["id"]
        expect(identifier not in mapped, f"{path}: duplicate item {identifier}")
        mapped[identifier] = item
    return mapped


def json_field(value: str, field: str, identifier: str) -> list[Any]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise AdoptionError(f"{identifier}: invalid {field}") from exc
    expect(isinstance(parsed, list), f"{identifier}: {field} must be a list")
    return parsed


def same_list(left: Any, right: Any) -> bool:
    return json.dumps(left, sort_keys=True, ensure_ascii=False, separators=(",", ":")) == json.dumps(
        right, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )


def source_from_object(value: Any, identifier: str, label: str) -> Source:
    expect(isinstance(value, dict), f"{identifier}: {label} must be an object")
    raw_path = value.get("pdf_path")
    expect(isinstance(raw_path, str) and raw_path, f"{identifier}: {label}.pdf_path is missing")
    path = Path(raw_path)
    digest = value.get("pdf_sha256")
    pages = value.get("pages")
    expect(isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest) is not None,
           f"{identifier}: invalid {label}.pdf_sha256")
    expect(isinstance(pages, list) and pages and all(isinstance(page, int) and page > 0 for page in pages),
           f"{identifier}: invalid {label}.pages")
    return Source(path, digest, tuple(pages))


def load_source_override(audit_root: Path, identifier: str, inventory_source: Source) -> tuple[dict[str, Any] | None, Path | None, str | None, Source]:
    """Load the single-question replacement authority, if it was supplied.

    The bytes hash is intentionally retained because repair/final documents bind
    the exact reviewed sidecar, rather than merely its parsed JSON meaning.
    """
    path = audit_root / "source-overrides-v1" / f"{identifier}.json"
    if not path.exists():
        return None, None, None, inventory_source
    expect(path.is_file(), f"{identifier}: source override is not a file")
    raw_bytes = path.read_bytes()
    document = load_json(path)
    expect(isinstance(document, dict) and document.get("schema") == "question-source-override-v1",
           f"{identifier}: invalid source override schema")
    expect(document.get("exam_question_id") == identifier, f"{identifier}: source override identity mismatch")
    original = source_from_object(document.get("original_source"), identifier, "original_source")
    expect(original == inventory_source, f"{identifier}: source override original_source does not match inventory")
    replacement = source_from_object(document.get("replacement_source"), identifier, "replacement_source")
    expect(sha256_file(replacement.path) == replacement.sha256,
           f"{identifier}: replacement source PDF hash drift")
    expect(isinstance(document.get("reason"), str) and document["reason"].strip(),
           f"{identifier}: source override reason is missing")
    root_accept = document.get("root_accept")
    expect(isinstance(root_accept, dict), f"{identifier}: source override root_accept is missing")
    expect(root_accept.get("actor") == "root_sol", f"{identifier}: source override root_accept actor is invalid")
    expect(root_accept.get("decision") == "accept_verified_replacement_source",
           f"{identifier}: source override root_accept decision is invalid")
    expect(root_accept.get("verified_question_identity") is True,
           f"{identifier}: source override lacks verified question identity")
    expect(root_accept.get("verified_required_figure_complete") is True,
           f"{identifier}: source override lacks verified required figure completeness")
    expect(root_accept.get("replacement_pdf_sha256") == replacement.sha256,
           f"{identifier}: source override root_accept replacement hash mismatch")
    expect(same_list(root_accept.get("replacement_pages"), list(replacement.pages)),
           f"{identifier}: source override root_accept replacement pages mismatch")
    return document, path, sha256_bytes(raw_bytes), replacement


@dataclass(frozen=True)
class Source:
    path: Path
    sha256: str
    pages: tuple[int, ...]


@dataclass(frozen=True)
class Repair:
    identifier: str
    year: int
    original_assets: tuple[str, ...]
    original_hashes: tuple[str, ...]
    original_source: Source
    effective_source: Source
    source_override: dict[str, Any] | None
    source_override_path: Path | None
    source_override_sha256: str | None
    images: tuple[dict[str, Any], ...]
    pages: tuple[int, ...]
    revision_assets: tuple[str, ...]
    revision_id: str

    @property
    def has_source_override(self) -> bool:
        return self.source_override is not None


@dataclass(frozen=True)
class Plan:
    revision_id: str
    repairs: tuple[Repair, ...]
    audit_root: Path
    corpus_root: Path

    @property
    def sidecar(self) -> Path:
        return self.corpus_root / "_derived" / "crop-adoptions" / f"{self.revision_id}.json"


def validate_image_binding(
    identifier: str,
    manifest: dict[str, Any],
    final: dict[str, Any],
) -> tuple[tuple[dict[str, Any], ...], tuple[int, ...]]:
    images = manifest.get("images")
    final_images = final.get("images")
    regions = manifest.get("source_regions")
    final_regions = final.get("source_regions")
    expect(isinstance(images, list) and images, f"{identifier}: repair has no images")
    expect(isinstance(regions, list) and regions, f"{identifier}: repair has no source regions")
    expect(same_list(regions, final_regions), f"{identifier}: final source regions do not bind manifest")
    expect(isinstance(final_images, list) and len(images) == len(final_images), f"{identifier}: final images do not bind manifest")
    pages: list[int] = []
    result: list[dict[str, Any]] = []
    for ordinal, (image, reviewed) in enumerate(zip(images, final_images), 1):
        expect(isinstance(image, dict) and isinstance(reviewed, dict), f"{identifier}: invalid image binding")
        for field in ("path", "sha256", "region_indices"):
            expect(image.get(field) == reviewed.get(field), f"{identifier}: final image {ordinal} differs in {field}")
        path = Path(str(image.get("path", "")))
        image_hash = str(image.get("sha256", ""))
        expect(len(image_hash) == 64 and sha256_file(path) == image_hash, f"{identifier}: repaired image {ordinal} hash drift")
        indices = image.get("region_indices")
        expect(isinstance(indices, list) and indices, f"{identifier}: image {ordinal} lacks region indices")
        chosen: list[dict[str, Any]] = []
        for index in indices:
            expect(isinstance(index, int) and 0 <= index < len(regions), f"{identifier}: invalid region index {index}")
            region = regions[index]
            expect(isinstance(region, dict) and isinstance(region.get("page"), int), f"{identifier}: region {index} has no integer page")
            chosen.append(region)
        page_set = {region["page"] for region in chosen}
        expect(len(page_set) == 1, f"{identifier}: image {ordinal} spans multiple source PDF pages")
        pages.append(next(iter(page_set)))
        result.append(image)
    return tuple(result), tuple(pages)


def build_plan(audit_root: Path, corpus_root: Path) -> Plan:
    inventory_path = audit_root / "inventory.json"
    inventory = load_json(inventory_path)
    expect(isinstance(inventory, dict), "inventory must be an object")
    inventory_items = item_map(inventory, inventory_path)
    expect(len(inventory_items) == inventory.get("question_count"), "inventory question_count mismatch")
    source_cache: dict[Path, str] = {}
    repairs_unbound: list[dict[str, Any]] = []

    for identifier, entry in sorted(inventory_items.items()):
        year = entry.get("year")
        expect(isinstance(year, int), f"{identifier}: invalid year")
        assets = entry.get("assets")
        expect(isinstance(assets, list) and assets, f"{identifier}: inventory assets missing")
        source = Path(str(entry.get("pdf_path", "")))
        source_hash = str(entry.get("pdf_sha256", ""))
        inventory_pages = entry.get("pdf_pages")
        expect(isinstance(inventory_pages, list), f"{identifier}: invalid inventory PDF pages")
        inventory_source = Source(source, source_hash, tuple(inventory_pages))
        expect(inventory_source.pages and all(isinstance(page, int) and page > 0 for page in inventory_source.pages),
               f"{identifier}: invalid inventory PDF pages")
        actual_source_hash = source_cache.setdefault(source, sha256_file(source))
        expect(actual_source_hash == source_hash, f"{identifier}: source PDF hash drift")
        override, override_path, override_hash, effective_source = load_source_override(audit_root, identifier, inventory_source)
        old_paths: list[str] = []
        old_hashes: list[str] = []
        for asset in assets:
            expect(isinstance(asset, dict), f"{identifier}: invalid inventory asset")
            path = Path(str(asset.get("path", "")))
            asset_hash = str(asset.get("sha256", ""))
            expect(sha256_file(path) == asset_hash, f"{identifier}: old source asset hash drift")
            old_paths.append(str(asset.get("relative_path", "")))
            old_hashes.append(asset_hash)

        confirm_path = audit_root / "sol-confirm" / f"{year}.json"
        confirm = load_json(confirm_path)
        expect(isinstance(confirm, dict) and confirm.get("complete") is True, f"{confirm_path}: incomplete")
        confirms = item_map(confirm, confirm_path)
        # The exact year coverage check is performed below once per file; this direct
        # lookup prevents a duplicate or unrelated confirmation from being accepted.
        decision_item = confirms.get(identifier)
        expect(decision_item is not None, f"{identifier}: missing root confirmation")
        decision = decision_item.get("decision")
        expect(decision in {"keep", "repair"}, f"{identifier}: root confirmation is {decision!r}")
        expect(decision_item.get("source_pdf") == str(source), f"{identifier}: confirmation source path drift")
        expect(decision_item.get("source_sha256") == source_hash, f"{identifier}: confirmation source hash drift")
        viewed = decision_item.get("viewed_assets")
        expect(isinstance(viewed, list) and [x.get("sha256") for x in viewed if isinstance(x, dict)] == old_hashes,
               f"{identifier}: confirmation does not bind current old assets")
        if decision == "keep":
            continue
        repairs_unbound.append(
            {
                "id": identifier,
                "year": year,
                "original_source": inventory_source,
                "effective_source": effective_source,
                "source_override": override,
                "source_override_path": override_path,
                "source_override_sha256": override_hash,
                "paths": tuple(old_paths),
                "hashes": tuple(old_hashes),
            }
        )

    years = {int(item["year"]) for item in inventory_items.values()}
    for year in years:
        expected = {identifier for identifier, entry in inventory_items.items() if entry["year"] == year}
        confirm_path = audit_root / "sol-confirm" / f"{year}.json"
        confirm = load_json(confirm_path)
        expect(set(item_map(confirm, confirm_path)) == expected, f"{confirm_path}: coverage does not match inventory")

    bindings: list[dict[str, Any]] = []
    repair_material: list[tuple[dict[str, Any], tuple[dict[str, Any], ...], tuple[int, ...]]] = []
    for raw in repairs_unbound:
        year = raw["year"]
        manifest_path = audit_root / "repairs" / str(year) / "manifest.json"
        final_path = audit_root / "sol-final" / f"{year}.json"
        manifest = load_json(manifest_path)
        final = load_json(final_path)
        expect(isinstance(manifest, dict) and manifest.get("complete") is True, f"{manifest_path}: incomplete")
        expect(isinstance(final, dict) and final.get("complete") is True, f"{final_path}: incomplete")
        expect(final.get("submission_sha256") == sha256_file(manifest_path), f"{raw['id']}: final does not bind current submission")
        manifests = item_map(manifest, manifest_path)
        finals = item_map(final, final_path)
        identifier = raw["id"]
        expected_repairs = {entry["id"] for entry in repairs_unbound if entry["year"] == year}
        expect(set(manifests) == expected_repairs, f"{manifest_path}: coverage does not match confirmed repairs")
        expect(set(finals) == expected_repairs, f"{final_path}: coverage does not match confirmed repairs")
        submitted, accepted = manifests.get(identifier), finals.get(identifier)
        expect(submitted is not None, f"{identifier}: missing repair submission")
        expect(accepted is not None and accepted.get("decision") == "accept", f"{identifier}: final root decision is not ACCEPT")
        effective_source = raw["effective_source"]
        expect(submitted.get("source_pdf") == str(effective_source.path), f"{identifier}: repair source path drift")
        expect(submitted.get("source_sha256") == effective_source.sha256, f"{identifier}: repair source hash drift")
        expect(tuple(submitted.get("old_asset_sha256", [])) == raw["hashes"], f"{identifier}: repair old asset binding drift")
        expect(accepted.get("source_sha256") == effective_source.sha256, f"{identifier}: final source hash drift")
        if raw["source_override"] is not None:
            expect(submitted.get("source_override_sha256") == raw["source_override_sha256"],
                   f"{identifier}: repair does not bind source override")
            expect(accepted.get("source_pdf") == str(effective_source.path), f"{identifier}: final source path drift")
            expect(accepted.get("source_override_sha256") == raw["source_override_sha256"],
                   f"{identifier}: final does not bind source override")
        images, pages = validate_image_binding(identifier, submitted, accepted)
        if raw["source_override"] is not None:
            expect(all(page in effective_source.pages for page in pages),
                   f"{identifier}: repair ROI page is outside replacement source pages")
        repair_material.append((raw, images, pages))
        bindings.append(
            {"id": identifier, "year": year, "manifest_sha256": sha256_file(manifest_path),
             "original_source": {"pdf_path": str(raw["original_source"].path), "pdf_sha256": raw["original_source"].sha256,
                                 "pages": list(raw["original_source"].pages)},
             "effective_source": {"pdf_path": str(effective_source.path), "pdf_sha256": effective_source.sha256,
                                  "pages": list(effective_source.pages)},
             "source_override_sha256": raw["source_override_sha256"],
             "images": [{"sha256": x["sha256"], "region_indices": x["region_indices"]} for x in images]}
        )

    revision_id = sha256_bytes(json.dumps(bindings, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode())
    repairs: list[Repair] = []
    for raw, images, pages in repair_material:
        relative = tuple(
            f"corpus/408统考真题/assets-revisions/{revision_id}/{raw['year']}/{raw['id']}/question-{index:02d}.png"
            for index in range(1, len(images) + 1)
        )
        repairs.append(Repair(raw["id"], raw["year"], raw["paths"], raw["hashes"], raw["original_source"], raw["effective_source"],
                              raw["source_override"], raw["source_override_path"], raw["source_override_sha256"], images, pages, relative, revision_id))
    return Plan(revision_id, tuple(repairs), audit_root, corpus_root)


def reference_paths(plan: Plan) -> list[Path]:
    years = sorted({repair.year for repair in plan.repairs})
    paths = [plan.corpus_root / "indexes" / "question-index.csv"]
    paths.extend(plan.corpus_root / "_derived" / "extracted" / f"{year}.jsonl" for year in years)
    paths.extend(plan.corpus_root / "cards" / f"{repair.identifier}.md" for repair in plan.repairs)
    return paths


def load_index(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open("r", newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        expect(reader.fieldnames is not None, f"missing index header: {path}")
        rows = list(reader)
    return list(reader.fieldnames), rows


def assert_current_references(plan: Plan) -> None:
    _, index_rows = load_index(plan.corpus_root / "indexes" / "question-index.csv")
    index = {row.get("exam_question_id"): row for row in index_rows}
    for repair in plan.repairs:
        row = index.get(repair.identifier)
        expect(row is not None, f"{repair.identifier}: missing CSV row")
        expect(tuple(json_field(row.get("question_assets", ""), "question_assets", repair.identifier)) == repair.original_assets,
               f"{repair.identifier}: CSV question assets changed since audit")
        if repair.has_source_override:
            expect(row.get("question_source_path") == str(repair.original_source.path),
                   f"{repair.identifier}: CSV question source path changed since audit")
            expect(row.get("question_source_sha256") == repair.original_source.sha256,
                   f"{repair.identifier}: CSV question source hash changed since audit")
        derived_path = plan.corpus_root / "_derived" / "extracted" / f"{repair.year}.jsonl"
        records = [json.loads(line) for line in derived_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        matches = [record for record in records if record.get("exam_question_id") == repair.identifier]
        expect(len(matches) == 1, f"{repair.identifier}: derived record missing or duplicated")
        expect(tuple(matches[0].get("question_assets", [])) == repair.original_assets,
               f"{repair.identifier}: derived question assets changed since audit")
        if repair.has_source_override:
            expect(matches[0].get("question_source_path") == str(repair.original_source.path),
                   f"{repair.identifier}: derived question source path changed since audit")
            expect(matches[0].get("question_source_sha256") == repair.original_source.sha256,
                   f"{repair.identifier}: derived question source hash changed since audit")
        card = (plan.corpus_root / "cards" / f"{repair.identifier}.md").read_text(encoding="utf-8")
        for asset in repair.original_assets:
            expect(f"> ![[{asset}]]" in card, f"{repair.identifier}: card question asset changed since audit")


def assert_live_bindings(plan: Plan) -> None:
    """Recheck all source and submission bytes while holding the writer lock."""
    for repair in plan.repairs:
        expect(sha256_file(repair.original_source.path) == repair.original_source.sha256,
               f"{repair.identifier}: original source PDF changed after preflight")
        expect(sha256_file(repair.effective_source.path) == repair.effective_source.sha256,
               f"{repair.identifier}: effective source PDF changed after preflight")
        if repair.source_override_path is not None:
            expect(sha256_file(repair.source_override_path) == repair.source_override_sha256,
                   f"{repair.identifier}: source override changed after preflight")
        for reference, expected_hash in zip(repair.original_assets, repair.original_hashes):
            old_asset = plan.corpus_root.parents[1] / reference
            expect(sha256_file(old_asset) == expected_hash,
                   f"{repair.identifier}: original asset changed after preflight")
        for image in repair.images:
            expect(sha256_file(Path(str(image["path"]))) == image["sha256"],
                   f"{repair.identifier}: repair submission changed after preflight")


def adopt_row(row: dict[str, str], repair: Repair) -> dict[str, str]:
    updated = dict(row)
    updated["question_assets"] = json.dumps(list(repair.revision_assets), ensure_ascii=False)
    updated["question_pages"] = json.dumps(list(repair.pages), ensure_ascii=False)
    if repair.has_source_override:
        expect("question_source_path" in updated and "question_source_sha256" in updated,
               f"{repair.identifier}: CSV lacks question source fields for override")
        updated["question_source_path"] = str(repair.effective_source.path)
        updated["question_source_sha256"] = repair.effective_source.sha256
    return updated


def update_card(text: str, repair: Repair) -> str:
    source_line = f"- 题卷来源：`{repair.effective_source.path}`（页 {','.join(map(str, repair.pages))}）"
    changed, count = re.subn(r"(?m)^- 题卷来源：.*$", source_line, text, count=1)
    expect(count == 1, f"{repair.identifier}: card source-page aggregate missing")
    replacement = "> [!question] 原题\n" + "\n".join(f"> ![[{asset}]]" for asset in repair.revision_assets)
    changed, count = re.subn(r"> \[!question\] 原题\n(?:> !\[\[[^\n]+\]\]\n?)+", replacement + "\n", changed, count=1)
    expect(count == 1, f"{repair.identifier}: card question embeds missing")
    return changed


def rendered_references(plan: Plan) -> dict[Path, bytes]:
    index_path = plan.corpus_root / "indexes" / "question-index.csv"
    fields, rows = load_index(index_path)
    by_id = {repair.identifier: repair for repair in plan.repairs}
    count = {identifier: 0 for identifier in by_id}
    rendered: dict[Path, bytes] = {}
    output = []
    for row in rows:
        repair = by_id.get(row.get("exam_question_id", ""))
        if repair:
            row = adopt_row(row, repair)
            count[repair.identifier] += 1
        output.append(row)
    expect(all(value == 1 for value in count.values()), "CSV repair row missing or duplicated")
    with tempfile.NamedTemporaryFile("w+", newline="", encoding="utf-8-sig", delete=False) as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader(); writer.writerows(output)
        temporary = Path(handle.name)
    try:
        rendered[index_path] = temporary.read_bytes()
    finally:
        temporary.unlink(missing_ok=True)
    for year in sorted({repair.year for repair in plan.repairs}):
        path = plan.corpus_root / "_derived" / "extracted" / f"{year}.jsonl"
        repairs = {repair.identifier: repair for repair in plan.repairs if repair.year == year}
        result = []
        found = {identifier: 0 for identifier in repairs}
        for line in path.read_text(encoding="utf-8").splitlines():
            record = json.loads(line)
            repair = repairs.get(record.get("exam_question_id"))
            if repair:
                record["question_assets"] = list(repair.revision_assets)
                record["question_pages"] = list(repair.pages)
                if repair.has_source_override:
                    expect("question_source_path" in record and "question_source_sha256" in record,
                           f"{repair.identifier}: derived record lacks question source fields for override")
                    record["question_source_path"] = str(repair.effective_source.path)
                    record["question_source_sha256"] = repair.effective_source.sha256
                found[repair.identifier] += 1
            result.append(json.dumps(record, ensure_ascii=False, sort_keys=True))
        expect(all(value == 1 for value in found.values()), f"{year}: derived repair record missing or duplicated")
        rendered[path] = ("\n".join(result) + "\n").encode()
    for repair in plan.repairs:
        path = plan.corpus_root / "cards" / f"{repair.identifier}.md"
        rendered[path] = update_card(path.read_text(encoding="utf-8"), repair).encode()
    return rendered


def atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush(); os.fsync(handle.fileno())
        os.replace(name, path)
    except BaseException:
        Path(name).unlink(missing_ok=True)
        raise


def sidecar_payload(plan: Plan) -> bytes:
    def source_payload(source: Source) -> dict[str, Any]:
        return {"pdf_path": str(source.path), "pdf_sha256": source.sha256, "pages": list(source.pages)}

    return json.dumps(
        {"schema": "question-crop-adoption-v2", "revision_id": plan.revision_id,
         "repairs": [{"id": r.identifier, "year": r.year, "source_pdf": str(r.effective_source.path),
                      "source_pdf_sha256": r.effective_source.sha256,
                      "source_override": r.source_override,
                      "source_override_sha256": r.source_override_sha256,
                      "original_source": source_payload(r.original_source),
                      "effective_source": source_payload(r.effective_source),
                      "images": [{"ref": ref, "sha256": image["sha256"], "ordinal": index,
                                  "page": r.pages[index - 1], "regions": image["region_indices"],
                                  "source_pdf": str(r.effective_source.path),
                                  "source_pdf_sha256": r.effective_source.sha256,
                                  "source_page": r.pages[index - 1], "region_indices": image["region_indices"]}
                                 for index, (ref, image) in enumerate(zip(r.revision_assets, r.images), 1)]}
                     for r in plan.repairs]}, ensure_ascii=False, sort_keys=True, indent=2
    ).encode() + b"\n"


def already_applied(plan: Plan) -> bool:
    if not plan.sidecar.is_file():
        return False
    try:
        data = load_json(plan.sidecar)
    except AdoptionError:
        return False
    if not isinstance(data, dict) or data.get("revision_id") != plan.revision_id:
        return False
    if plan.sidecar.read_bytes() != sidecar_payload(plan):
        return False
    _, rows = load_index(plan.corpus_root / "indexes" / "question-index.csv")
    index = {row.get("exam_question_id"): row for row in rows}
    for repair in plan.repairs:
        row = index.get(repair.identifier)
        if not row or tuple(json_field(row.get("question_assets", ""), "question_assets", repair.identifier)) != repair.revision_assets:
            return False
        if tuple(json_field(row.get("question_pages", ""), "question_pages", repair.identifier)) != repair.pages:
            return False
        if repair.has_source_override and (
            row.get("question_source_path") != str(repair.effective_source.path)
            or row.get("question_source_sha256") != repair.effective_source.sha256
        ):
            return False
        for reference, image in zip(repair.revision_assets, repair.images):
            path = plan.corpus_root.parents[1] / reference
            if not path.is_file() or sha256_file(path) != image["sha256"]:
                return False
    return True


def apply_plan(plan: Plan) -> dict[str, Any]:
    lock_path = plan.corpus_root / ".crop-adoption.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        if already_applied(plan):
            return {"status": "already_applied", "revision_id": plan.revision_id, "repair_count": len(plan.repairs)}
        assert_live_bindings(plan)
        assert_current_references(plan)
        originals = {path: path.read_bytes() for path in reference_paths(plan)}
        rendered = rendered_references(plan)
        backup_root = plan.corpus_root / "_derived" / "crop-adoption-backups" / plan.revision_id
        try:
            for path, content in originals.items():
                relative = path.relative_to(plan.corpus_root)
                backup = backup_root / relative
                if backup.exists():
                    expect(backup.read_bytes() == content, f"backup collision for {relative}")
                else:
                    atomic_write(backup, content)
            for repair in plan.repairs:
                for index, (reference, image) in enumerate(zip(repair.revision_assets, repair.images), 1):
                    destination = plan.corpus_root.parents[1] / reference
                    if destination.exists():
                        expect(sha256_file(destination) == image["sha256"], f"immutable asset collision: {destination}")
                        continue
                    atomic_write(destination, Path(str(image["path"])).read_bytes())
                    expect(sha256_file(destination) == image["sha256"], f"copied asset hash mismatch: {repair.identifier}/{index}")
            for path, content in rendered.items():
                expect(path.read_bytes() == originals[path], f"CAS failed for {path}")
                atomic_write(path, content)
            atomic_write(plan.sidecar, sidecar_payload(plan))
        except BaseException:
            for path, content in originals.items():
                atomic_write(path, content)
            raise
        return {"status": "applied", "revision_id": plan.revision_id, "repair_count": len(plan.repairs), "receipt": str(plan.sidecar)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audit-root", type=Path, required=True)
    parser.add_argument("--corpus-root", type=Path, required=True)
    parser.add_argument("--apply", action="store_true", help="perform the bounded external corpus adoption")
    parser.add_argument("--revision-id", help="require this exact calculated revision id")
    args = parser.parse_args(argv)
    try:
        plan = build_plan(args.audit_root, args.corpus_root)
        if args.revision_id:
            expect(args.revision_id == plan.revision_id, "requested revision id does not match validated evidence")
        result = apply_plan(plan) if args.apply else {
            "status": "preflight_ok", "revision_id": plan.revision_id, "repair_count": len(plan.repairs),
            "sidecar": str(plan.sidecar),
        }
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except AdoptionError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
