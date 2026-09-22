#!/usr/bin/env python3
"""Adopt root-reviewed preprocessed exam images without rendering any PDF."""
from __future__ import annotations

import argparse
import csv
import fcntl
import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image


class AdoptionError(RuntimeError):
    pass


def expect(condition: bool, message: str) -> None:
    if not condition:
        raise AdoptionError(message)


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    expect(path.is_file(), f"missing file: {path}")
    return sha256_bytes(path.read_bytes())


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AdoptionError(f"invalid JSON {path}: {exc}") from exc


def same_json(left: Any, right: Any) -> bool:
    return json.dumps(left, ensure_ascii=False, sort_keys=True, separators=(",", ":")) == json.dumps(
        right, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def under(root: Path, candidate: str, label: str) -> Path:
    path = (root / candidate).resolve() if not Path(candidate).is_absolute() else Path(candidate).resolve()
    try:
        path.relative_to(root.resolve())
    except ValueError as exc:
        raise AdoptionError(f"{label} escapes its declared root") from exc
    return path


def refs(value: Any, identifier: str, label: str) -> tuple[tuple[str, str], ...]:
    expect(isinstance(value, list), f"{identifier}: {label} must be a list")
    output: list[tuple[str, str]] = []
    for raw in value:
        expect(isinstance(raw, dict) and isinstance(raw.get("ref"), str) and isinstance(raw.get("sha256"), str),
               f"{identifier}: invalid {label} binding")
        expect(re.fullmatch(r"[0-9a-f]{64}", raw["sha256"]) is not None, f"{identifier}: invalid {label} hash")
        output.append((raw["ref"], raw["sha256"]))
    return tuple(output)


def pixels_png(source: Path) -> tuple[bytes, str, str]:
    """Return immutable PNG bytes, source bytes hash, and decoded pixel hash."""
    source_bytes = source.read_bytes()
    source_hash = sha256_bytes(source_bytes)
    try:
        with Image.open(source) as image:
            image.load()
            mode = "RGBA" if "A" in image.getbands() else "RGB"
            decoded = image.convert(mode)
            pixel_hash = sha256_bytes(mode.encode() + b"\0" + str(decoded.size).encode() + b"\0" + decoded.tobytes())
            if source.suffix.lower() == ".png":
                return source_bytes, source_hash, pixel_hash
            expect(source.suffix.lower() == ".webp", f"unsupported source image type: {source.name}")
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as handle:
                converted = Path(handle.name)
            try:
                decoded.save(converted, format="PNG", optimize=False)
                with Image.open(converted) as proof:
                    proof.load()
                    proof_pixels = proof.convert(mode)
                    proof_hash = sha256_bytes(mode.encode() + b"\0" + str(proof_pixels.size).encode() + b"\0" + proof_pixels.tobytes())
                expect(proof_hash == pixel_hash, f"lossy WebP conversion: {source}")
                return converted.read_bytes(), source_hash, pixel_hash
            finally:
                converted.unlink(missing_ok=True)
    except OSError as exc:
        raise AdoptionError(f"invalid image {source}: {exc}") from exc


@dataclass(frozen=True)
class Material:
    path: Path
    output: bytes
    source_sha256: str
    pixel_sha256: str
    provenance: dict[str, Any]


@dataclass(frozen=True)
class Item:
    identifier: str
    package_id: str | None
    original_question: tuple[tuple[str, str], ...]
    original_solution: tuple[tuple[str, str], ...]
    question: tuple[Material, ...] | None
    solution: tuple[Material, ...] | None
    root_review: dict[str, Any]
    revision_id: str
    plan_sha256: str
    package: dict[str, Any]
    corpus_root: Path

    @property
    def year(self) -> str:
        return self.identifier.split("-")[1]

    @property
    def receipt(self) -> Path:
        return self.corpus_root / "_derived" / "preprocessed-image-adoptions" / self.revision_id / f"{self.identifier}.json"


def validate_id(identifier: Any) -> str:
    expect(isinstance(identifier, str) and re.fullmatch(r"EXAM408-(?:19|20)\d{2}-Q\d{2}", identifier) is not None,
           f"invalid canonical_question_id: {identifier!r}")
    return identifier


def manifest_records(package_root: Path, package: dict[str, Any]) -> tuple[Path, dict[int, tuple[dict[str, Any], str]]]:
    manifest_name = package.get("manifest_path")
    expect(isinstance(manifest_name, str), "package manifest_path is missing")
    path = under(package_root, manifest_name, "package manifest")
    expect(sha256_file(path) == package.get("manifest_sha256"), "package manifest hash drift")
    records: dict[int, tuple[dict[str, Any], str]] = {}
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if line.strip():
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise AdoptionError(f"invalid package manifest line {number}") from exc
            expect(isinstance(record, dict), f"package manifest line {number} is not an object")
            records[number] = (record, sha256_bytes(line.encode()))
    return path, records


def package_material(raw: dict[str, Any], identifier: str, package_root: Path, records: dict[int, tuple[dict[str, Any], str]]) -> Material:
    provenance = raw.get("provenance")
    expect(isinstance(provenance, dict), f"{identifier}: package image lacks provenance")
    expect(provenance.get("kind") == "preprocessed_package", f"{identifier}: package image provenance kind is invalid")
    expect(isinstance(provenance.get("source_pages"), list) and all(isinstance(page, int) and page > 0 for page in provenance["source_pages"]),
           f"{identifier}: package source_pages is invalid")
    line = provenance.get("manifest_line")
    expect(isinstance(line, int) and line in records, f"{identifier}: package manifest line is invalid")
    record, line_hash = records[line]
    expect(provenance.get("raw_line_sha256") == line_hash, f"{identifier}: package raw-line hash drift")
    field = provenance.get("original_image_field")
    expect(isinstance(field, str) and field in record, f"{identifier}: package original image field is invalid")
    package_id = raw.get("package_id")
    expect(package_id is None or record.get("id") == package_id, f"{identifier}: package ID does not bind manifest")
    raw_path = raw.get("path")
    expect(isinstance(raw_path, str), f"{identifier}: image path is missing")
    field_value = record[field]
    expect(field_value == raw_path or (isinstance(field_value, list) and raw_path in field_value),
           f"{identifier}: package image path does not bind manifest field")
    expected = raw.get("sha256")
    expect(isinstance(expected, str) and provenance.get("file_sha256") == expected, f"{identifier}: package file hash binding is missing")
    path = under(package_root, raw_path, f"{identifier}: package image")
    expect(sha256_file(path) == expected, f"{identifier}: package image hash drift")
    output, source_hash, pixel_hash = pixels_png(path)
    copy = dict(provenance); copy["source_path"] = str(path); copy["manifest_record"] = record
    copy["source_bytes_sha256"] = source_hash; copy["decoded_pixel_sha256"] = pixel_hash
    return Material(path, output, source_hash, pixel_hash, copy)


def local_material(raw: dict[str, Any], identifier: str, corpus_root: Path, package_root: Path, records: dict[int, tuple[dict[str, Any], str]]) -> Material:
    provenance = raw.get("provenance")
    expect(isinstance(provenance, dict), f"{identifier}: image lacks provenance")
    kind = provenance.get("kind")
    expect(kind in {"existing_local", "existing_local_revision", "user_supplement", "assembled_preprocessed"},
           f"{identifier}: unsupported non-package provenance")
    expect(kind not in {"pdf_render", "pdf_crop"}, f"{identifier}: PDF rendering provenance is forbidden")
    expect(isinstance(provenance.get("source_pages"), list) and all(isinstance(page, int) and page > 0 for page in provenance["source_pages"]),
           f"{identifier}: local source_pages is invalid")
    raw_path = raw.get("path")
    expect(isinstance(raw_path, str) and isinstance(raw.get("sha256"), str), f"{identifier}: image path/hash is missing")
    path = under(corpus_root.parents[1], raw_path, f"{identifier}: local image") if kind.startswith("existing_local") else Path(raw_path).resolve()
    expect(path.is_file() and sha256_file(path) == raw["sha256"], f"{identifier}: local image hash drift")
    if kind == "assembled_preprocessed":
        components = provenance.get("components")
        expect(isinstance(components, list) and components, f"{identifier}: assembled image lacks components")
        component_bindings: list[dict[str, Any]] = []
        for ordinal, component in enumerate(components, 1):
            expect(isinstance(component, dict) and isinstance(component.get("path"), str) and isinstance(component.get("sha256"), str),
                   f"{identifier}: invalid assembled component")
            component_kind = component.get("kind")
            expect(component_kind in {"preprocessed_package", "user_supplement", "existing_local", "existing_local_revision"},
                   f"{identifier}: assembled component kind is invalid")
            expect(component_kind not in {"pdf_render", "pdf_crop"}, f"{identifier}: PDF rendering component is forbidden")
            component_path = under(package_root, component["path"], f"{identifier}: assembled component") if component_kind == "preprocessed_package" else Path(component["path"]).resolve()
            expect(sha256_file(component_path) == component["sha256"], f"{identifier}: assembled component hash drift")
            if component_kind == "preprocessed_package":
                line = component["manifest_line"]
                expect(isinstance(line, int) and line in records, f"{identifier}: assembled component manifest line is invalid")
                record, line_hash = records[line]
                expect(component.get("raw_line_sha256") == line_hash, f"{identifier}: assembled component raw-line hash drift")
                field = component.get("original_image_field")
                expect(isinstance(field, str) and field in record, f"{identifier}: assembled component field is invalid")
                expect(component.get("file_sha256") == component["sha256"], f"{identifier}: assembled component file hash binding is missing")
                value = record[field]
                expect(value == component["path"] or (isinstance(value, list) and component["path"] in value),
                       f"{identifier}: assembled component does not bind manifest field")
                expect(component.get("role") in {None, "shared_context", "question", "solution"},
                       f"{identifier}: assembled component role is invalid")
                component_bindings.append({"ordinal": ordinal, "role": component.get("role"), "manifest_line": line,
                                           "raw_line_sha256": line_hash, "original_image_field": field, "record": record})
    output, source_hash, pixel_hash = pixels_png(path)
    copy = dict(provenance); copy["source_path"] = str(path); copy["source_bytes_sha256"] = source_hash; copy["decoded_pixel_sha256"] = pixel_hash
    if kind == "assembled_preprocessed": copy["component_manifest_bindings"] = component_bindings
    return Material(path, output, source_hash, pixel_hash, copy)


def build_items(plan_path: Path, corpus_root: Path) -> tuple[str, str, tuple[Item, ...]]:
    plan_bytes = plan_path.read_bytes(); plan_sha = sha256_bytes(plan_bytes); plan = load_json(plan_path)
    expect(isinstance(plan, dict) and plan.get("schema") == "preprocessed-corpus-image-adoption-plan-v1", "invalid plan schema")
    package = plan.get("package"); expect(isinstance(package, dict), "invalid package")
    root_name = package.get("root"); expect(isinstance(root_name, str), "package root is missing")
    package_root = Path(root_name).resolve(); manifest_path, records = manifest_records(package_root, package)
    expect(isinstance(package.get("archive_sha256"), str) and re.fullmatch(r"[0-9a-f]{64}", package["archive_sha256"]) is not None,
           "invalid package archive_sha256")
    package_copy = dict(package); package_copy["manifest_path"] = str(manifest_path)
    raw_items = plan.get("items"); expect(isinstance(raw_items, list) and raw_items, "plan items are missing")
    revision = sha256_bytes(b"preprocessed-image-adoption-v1\0" + plan_bytes)
    output: list[Item] = []; seen: set[str] = set()
    for raw in raw_items:
        expect(isinstance(raw, dict), "invalid plan item")
        identifier = validate_id(raw.get("canonical_question_id")); expect(identifier not in seen, f"duplicate plan item: {identifier}"); seen.add(identifier)
        review = raw.get("root_review"); expect(isinstance(review, dict) and review.get("actor") == "root_sol" and review.get("decision") == "accept",
                                                   f"{identifier}: root review is not ACCEPT")
        package_id = raw.get("package_id"); expect(package_id is None or isinstance(package_id, str), f"{identifier}: invalid package_id")
        expect(package_id is None or (package_id != identifier and re.fullmatch(r"EXAM408-(?:19|20)\d{2}-Q\d{2}", package_id) is None),
               f"{identifier}: package_id cannot impersonate canonical question identity")
        year = int(identifier.split("-")[1])
        if year == 2021:
            expect(package_id is None, f"{identifier}: 2021 cannot use a package candidate")
        roles: dict[str, tuple[Material, ...] | None] = {}
        for role in ("question", "solution"):
            field = f"{role}_images"
            supplied = raw.get(field)
            if supplied is None:
                roles[role] = None; continue
            expect(isinstance(supplied, list) and supplied, f"{identifier}: {field} must be non-empty when supplied")
            materials: list[Material] = []
            for image in supplied:
                expect(isinstance(image, dict), f"{identifier}: invalid {field} image")
                kind = image.get("provenance", {}).get("kind") if isinstance(image.get("provenance"), dict) else None
                expect(kind != "pdf_render", f"{identifier}: PDF rendering provenance is forbidden")
                if kind == "preprocessed_package":
                    expect(year != 2021, f"{identifier}: 2021 cannot use a package candidate")
                    expect(package_id is not None, f"{identifier}: package image lacks package_id")
                    bound = dict(image); bound["package_id"] = package_id
                    materials.append(package_material(bound, identifier, package_root, records))
                else:
                    materials.append(local_material(image, identifier, corpus_root, package_root, records))
            roles[role] = tuple(materials)
        original_question = refs(raw.get("original_question_assets"), identifier, "original_question_assets")
        original_solution = refs(raw.get("original_solution_assets"), identifier, "original_solution_assets")
        for reference, expected_hash in original_question + original_solution:
            expect(sha256_file(under(corpus_root.parents[1], reference, f"{identifier}: original asset")) == expected_hash,
                   f"{identifier}: original asset hash drift")
        output.append(Item(identifier, package_id, original_question, original_solution, roles["question"], roles["solution"],
                           review, revision, plan_sha, package_copy, corpus_root))
    return plan_sha, revision, tuple(output)


def load_index(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle); expect(reader.fieldnames is not None, "missing index header")
        return list(reader.fieldnames), list(reader)


def ref_values(row: dict[str, str], field: str, identifier: str) -> tuple[str, ...]:
    try: value = json.loads(row.get(field, ""))
    except json.JSONDecodeError as exc: raise AdoptionError(f"{identifier}: invalid {field}") from exc
    expect(isinstance(value, list) and all(isinstance(x, str) for x in value), f"{identifier}: invalid {field}")
    return tuple(value)


def current_check(item: Item) -> tuple[dict[str, str], str]:
    index_path = item.corpus_root / "indexes" / "question-index.csv"; _, rows = load_index(index_path)
    matches = [row for row in rows if row.get("exam_question_id") == item.identifier]; expect(len(matches) == 1, f"{item.identifier}: CSV row missing or duplicated")
    row = matches[0]
    for field, expected in (("question_assets", item.original_question), ("solution_assets", item.original_solution)):
        expect(ref_values(row, field, item.identifier) == tuple(ref for ref, _ in expected), f"{item.identifier}: {field} changed since plan")
        for ref, digest in expected: expect(sha256_file(under(item.corpus_root.parents[1], ref, f"{item.identifier}: original asset")) == digest, f"{item.identifier}: original asset hash drift")
    card = item.corpus_root / "cards" / f"{item.identifier}.md"; expect(card.is_file(), f"{item.identifier}: card missing")
    return row, card.read_text(encoding="utf-8")


def derived_path(item: Item) -> Path:
    return item.corpus_root / "_derived" / "extracted" / f"{item.year}.jsonl"


def render_derived(item: Item, new: dict[str, tuple[tuple[str, Material], ...]], allowed: dict[str, tuple[tuple[str, str], ...]]) -> tuple[Path, bytes, bool]:
    path = derived_path(item); expect(path.is_file(), f"{item.identifier}: derived record file missing")
    lines = path.read_text(encoding="utf-8").splitlines(); found = 0; changed = False; output: list[str] = []
    for line in lines:
        record = json.loads(line)
        if record.get("exam_question_id") != item.identifier:
            output.append(line); continue
        found += 1
        for role in ("question", "solution"):
            supplied = getattr(item, role)
            if supplied is None: continue
            field = f"{role}_assets"; current = tuple(record.get(field, [])); old_refs = tuple(ref for ref, _ in allowed[role]); new_refs = tuple(ref for ref, _ in new[role])
            expect(current in {old_refs, new_refs}, f"{item.identifier}: derived {field} has unknown reference drift")
            if current != new_refs:
                record[field] = list(new_refs); changed = True
        output.append(json.dumps(record, ensure_ascii=False, sort_keys=True) if changed and record.get("exam_question_id") == item.identifier else line)
    expect(found == 1, f"{item.identifier}: derived record missing or duplicated")
    return path, ("\n".join(output) + "\n").encode(), changed


def verify_applied_receipt_state(item: Item, new: dict[str, tuple[tuple[str, Material], ...]], receipt: bytes) -> None:
    expect(item.receipt.is_file() and item.receipt.read_bytes() == receipt, f"{item.identifier}: verified receipt is missing or drifted")
    receipt_data = load_json(item.receipt)
    expect(isinstance(receipt_data, dict) and receipt_data.get("root_plan_sha256") == item.plan_sha256 and receipt_data.get("root_review") == item.root_review,
           f"{item.identifier}: receipt does not bind this root-approved plan")
    _, rows = load_index(item.corpus_root / "indexes" / "question-index.csv"); matches = [row for row in rows if row.get("exam_question_id") == item.identifier]
    expect(len(matches) == 1, f"{item.identifier}: CSV row missing or duplicated")
    card = (item.corpus_root / "cards" / f"{item.identifier}.md").read_text(encoding="utf-8")
    for role in ("question", "solution"):
        if getattr(item, role) is None: continue
        refs_new = tuple(ref for ref, _ in new[role])
        expect(ref_values(matches[0], f"{role}_assets", item.identifier) == refs_new, f"{item.identifier}: CSV {role} refs are not applied")
        for ref, material in new[role]:
            expect(sha256_file(under(item.corpus_root.parents[1], ref, f"{item.identifier}: new asset")) == sha256_bytes(material.output),
                   f"{item.identifier}: applied {role} asset hash drift")
            expect(f"![[{ref}]]" in card, f"{item.identifier}: card {role} refs are not applied")


def recover_derived_only(item: Item, new: dict[str, tuple[tuple[str, Material], ...]], receipt: bytes) -> dict[str, Any]:
    verify_applied_receipt_state(item, new, receipt)
    path, rendered, changed = render_derived(item, new, {"question": item.original_question, "solution": item.original_solution})
    if not changed: return {"status": "already_applied", "id": item.identifier, "receipt": str(item.receipt)}
    original = path.read_bytes(); backup = item.corpus_root / "_derived" / "preprocessed-image-adoption-backups" / item.revision_id / item.identifier / path.relative_to(item.corpus_root)
    if not backup.exists(): atomic_write(backup, original)
    expect(path.read_bytes() == original, f"CAS failed for {path}")
    try: atomic_write(path, rendered)
    except BaseException:
        if path.exists() and path.read_bytes() == rendered: atomic_write(path, original)
        raise
    return {"status": "derived_recovered", "id": item.identifier, "receipt": str(item.receipt)}


def asset_ref(item: Item, role: str, ordinal: int) -> str:
    return f"corpus/408统考真题/assets-preprocessed/{item.revision_id}/{item.year}/{item.identifier}/{role}-{ordinal:02d}.png"


def replace_embeds(text: str, role: str, refs_new: list[str], identifier: str) -> str:
    if role == "question":
        pattern = r"(> \[!question\] 原题\n)(?:> !\[\[[^\n]+\]\]\n?)+"
        replacement = r"\1" + "".join(f"> ![[{ref}]]\n" for ref in refs_new)
    else:
        pattern = r"(> \[!answer\]- 答案与解析\n)(?:> !\[\[[^\n]+\]\]\n?)+"
        replacement = r"\1" + "".join(f"> ![[{ref}]]\n" for ref in refs_new)
    output, count = re.subn(pattern, replacement, text, count=1)
    expect(count == 1, f"{identifier}: card {role} embeds missing")
    return output


def update_source_note(text: str, item: Item) -> str:
    note = f"- 预处理图像回执：`_derived/preprocessed-image-adoptions/{item.revision_id}/{item.identifier}.json`"
    output, count = re.subn(r"(?m)^- 预处理图像回执：.*$", note, text, count=1)
    return output if count else note + "\n" + text


def atomic_write(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True); fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle: handle.write(value); handle.flush(); os.fsync(handle.fileno())
        os.replace(name, path)
    except BaseException:
        Path(name).unlink(missing_ok=True); raise


def receipt_payload(item: Item, old: dict[str, tuple[tuple[str, str], ...]], new: dict[str, tuple[tuple[str, Material], ...]]) -> bytes:
    roles: dict[str, Any] = {}
    bindings: list[dict[str, Any]] = []; pages: dict[str, list[int]] = {}
    for role in ("question", "solution"):
        supplied = getattr(item, role)
        entries = [] if supplied is None else [{"ref": ref, "sha256": sha256_bytes(material.output), "input": material.provenance,
                                                 "source_bytes_sha256": material.source_sha256, "decoded_pixel_sha256": material.pixel_sha256}
                                                for ref, material in new[role]]
        roles[role] = {"old": [{"ref": ref, "sha256": digest} for ref, digest in old[role]], "new": entries}
        pages[role] = [page for material in (supplied or ()) for page in material.provenance.get("source_pages", []) if isinstance(page, int)]
        for material in supplied or ():
            provenance = material.provenance
            if "manifest_line" in provenance:
                bindings.append({"role": role, "manifest_line": provenance["manifest_line"], "raw_line_sha256": provenance["raw_line_sha256"],
                                 "original_image_field": provenance["original_image_field"], "record": provenance["manifest_record"]})
            for component in provenance.get("component_manifest_bindings", []):
                bindings.append({"role": role, "component_ordinal": component["ordinal"], "component_role": component["role"],
                                 "manifest_line": component["manifest_line"], "raw_line_sha256": component["raw_line_sha256"],
                                 "original_image_field": component["original_image_field"], "record": component["record"]})
    payload = {"schema": "preprocessed-image-adoption-receipt-v1", "revision_id": item.revision_id, "root_plan_sha256": item.plan_sha256,
               "canonical_question_id": item.identifier, "package_id": item.package_id, "package": item.package, "root_review": item.root_review,
               "manifest_bindings": bindings, "original_source_pages": pages, "roles": roles}
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2).encode() + b"\n"


def apply_item(item: Item) -> dict[str, Any]:
    lock_path = item.corpus_root / ".preprocessed-image-adoption.lock"; lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        old = {"question": item.original_question, "solution": item.original_solution}
        new = {role: tuple((asset_ref(item, role, index), material) for index, material in enumerate(getattr(item, role) or (), 1)) for role in ("question", "solution")}
        receipt = receipt_payload(item, old, new)
        if item.receipt.is_file() and item.receipt.read_bytes() == receipt:
            return recover_derived_only(item, new, receipt)
        row, card_text = current_check(item)
        index_path = item.corpus_root / "indexes" / "question-index.csv"; fields, rows = load_index(index_path); card_path = item.corpus_root / "cards" / f"{item.identifier}.md"
        derived, rendered_derived, _ = render_derived(item, new, old)
        originals = {index_path: index_path.read_bytes(), card_path: card_path.read_bytes(), derived: derived.read_bytes()}; rendered_rows = []
        for candidate in rows:
            if candidate.get("exam_question_id") == item.identifier:
                for role in ("question", "solution"):
                    if getattr(item, role) is not None: candidate[f"{role}_assets"] = json.dumps([ref for ref, _ in new[role]], ensure_ascii=False)
            rendered_rows.append(candidate)
        with tempfile.NamedTemporaryFile("w+", encoding="utf-8-sig", newline="", delete=False) as handle:
            writer = csv.DictWriter(handle, fieldnames=fields); writer.writeheader(); writer.writerows(rendered_rows); rendered_index = Path(handle.name)
        try: rendered = {index_path: rendered_index.read_bytes()}
        finally: rendered_index.unlink(missing_ok=True)
        changed_card = card_text
        for role in ("question", "solution"):
            if getattr(item, role) is not None: changed_card = replace_embeds(changed_card, role, [ref for ref, _ in new[role]], item.identifier)
        if item.question is not None or item.solution is not None:
            changed_card = update_source_note(changed_card, item)
        rendered[card_path] = changed_card.encode()
        rendered[derived] = rendered_derived
        written_assets: list[tuple[Path, bytes]] = []
        backup_root = item.corpus_root / "_derived" / "preprocessed-image-adoption-backups" / item.revision_id / item.identifier
        try:
            for path, value in originals.items():
                backup = backup_root / path.relative_to(item.corpus_root)
                if backup.exists():
                    expect(backup.read_bytes() == value, f"backup collision for {backup}")
                else:
                    atomic_write(backup, value)
            for role in ("question", "solution"):
                for ref, material in new[role]:
                    destination = under(item.corpus_root.parents[1], ref, f"{item.identifier}: destination")
                    if destination.exists(): expect(destination.read_bytes() == material.output, f"immutable asset collision: {destination}")
                    else: atomic_write(destination, material.output); written_assets.append((destination, material.output))
            for path, value in rendered.items(): expect(path.read_bytes() == originals[path], f"CAS failed for {path}"); atomic_write(path, value)
            atomic_write(item.receipt, receipt)
        except BaseException:
            for path, value in originals.items():
                if path.exists() and path.read_bytes() == rendered.get(path): atomic_write(path, value)
            for path, value in written_assets:
                if path.exists() and path.read_bytes() == value: path.unlink()
            if item.receipt.exists() and item.receipt.read_bytes() == receipt: item.receipt.unlink()
            raise
        return {"status": "applied", "id": item.identifier, "receipt": str(item.receipt)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__); parser.add_argument("--plan", type=Path, required=True); parser.add_argument("--corpus-root", type=Path, required=True)
    parser.add_argument("--apply", action="store_true"); parser.add_argument("--recover-derived-only", action="store_true"); parser.add_argument("--plan-sha256"); parser.add_argument("--revision-id")
    args = parser.parse_args(argv)
    try:
        plan_sha, revision, items = build_items(args.plan, args.corpus_root)
        if args.apply or args.recover_derived_only:
            expect(not (args.apply and args.recover_derived_only), "choose either --apply or --recover-derived-only")
            expect(args.plan_sha256 == plan_sha, "write operation requires exact --plan-sha256")
            expect(args.revision_id == revision, "write operation requires exact --revision-id")
        if args.apply:
            result = [apply_item(item) for item in items]
            print(json.dumps({"status": "applied", "plan_sha256": plan_sha, "revision_id": revision, "items": result}, ensure_ascii=False))
        elif args.recover_derived_only:
            result = []
            for item in items:
                old = {"question": item.original_question, "solution": item.original_solution}
                new = {role: tuple((asset_ref(item, role, index), material) for index, material in enumerate(getattr(item, role) or (), 1)) for role in ("question", "solution")}
                lock_path = item.corpus_root / ".preprocessed-image-adoption.lock"; lock_path.parent.mkdir(parents=True, exist_ok=True)
                with lock_path.open("a+") as lock:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                    result.append(recover_derived_only(item, new, receipt_payload(item, old, new)))
            print(json.dumps({"status": "derived_recovery_complete", "plan_sha256": plan_sha, "revision_id": revision, "items": result}, ensure_ascii=False))
        else: print(json.dumps({"status": "preflight_ok", "plan_sha256": plan_sha, "revision_id": revision, "item_count": len(items)}, ensure_ascii=False))
        return 0
    except AdoptionError as exc:
        print(f"REFUSED: {exc}", file=os.sys.stderr); return 2


if __name__ == "__main__": raise SystemExit(main())
