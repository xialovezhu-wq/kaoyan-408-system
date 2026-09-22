#!/usr/bin/env python3
"""Immutable OOXML source-document evidence for one 408 formal problem scope.

The invocation path is used only to read one already-authorized DOCX.  Stored
objects are content addressed and never contain the host path.  A supported
document has exactly two independent scopes; callers select one scope and
cannot supply titles, ranges, hashes, Capture IDs, or deployment authority.
"""

from __future__ import annotations

import copy
import hashlib
import io
import json
import os
import posixpath
import re
import stat
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, Mapping
from xml.etree import ElementTree as ET

import current_question_evidence_408 as evidence
from image_integrity_408 import ImageIntegrityError, validate_image_bytes


BUNDLE_SCHEMA_V4 = "current-question-evidence-bundle-v4"
CAPTURE_KIND = "formal_problem_document"
QUESTION_MODE = "formal_problem_document"
DIALOGUE_SCHEMA = "formal-problem-document-dialogue-v1"
RECEIPT_SCHEMA = "formal-problem-document-capture-receipt-v1"
RECEIPT_LOCATOR_PREFIX = "formal-problem-document-capture-receipt://sha256/"
LEGACY_BACKGROUND_HANDOFF_LOCATOR_PREFIX = (
    "current-question-background-handoff://sha256/"
)
SOURCE_OBJECT_REF_PREFIX = "current-question-attachment://sha256/"
DOCX_MEDIA_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)
SOURCE_LEDGER_RELATIVE_PATH = (
    "wiki/study_vaults/408-full/state/intake-curation/events.jsonl"
)
SOURCE_HOT_RECEIPT_RELATIVE_ROOT = (
    "wiki/study_vaults/408-full/state/intake-curation/"
    ".capture-hot-writer-v1/capture-receipts"
)
MAX_DOCX_BYTES = 32 * 1024 * 1024
MAX_XML_BYTES = 8 * 1024 * 1024
MAX_ZIP_ENTRY_BYTES = 32 * 1024 * 1024
MAX_ZIP_TOTAL_BYTES = 96 * 1024 * 1024
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
CAPTURE_ID_RE = re.compile(r"^CAP-\d{8}-[0-9a-f]{12}$")
EVENT_ID_RE = re.compile(r"^CE-[0-9a-f]{20}$")
SPEAKER_RE = re.compile(r"^(用户|助手).*(?:第\s*\d+\s*轮)[）)]$")
NS = {
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "pr": "http://schemas.openxmlformats.org/package/2006/relationships",
}


FORMAL_PROBLEM_DOCUMENT_CONTRACT: dict[str, Any] = {
    "schema_version": "formal-problem-document-contract-v1",
    "source_document_sha256": (
        "0f75958fc5dcf98adc76d838fa5408f3423880b10e848eb1290306feecf95997"
    ),
    "document_title": "数据结构两道单链表真题 对话与错误整理",
    "study_date": "2026-08-16",
    "scopes": {
        "part1": {
            "scope_id": "part1",
            "scope_title": "一、2015 年统考真题：按绝对值去除单链表重复结点",
            "next_boundary_title": "二、2019 年统考真题：单链表首尾交错重排",
            "render_page_start": 2,
            "render_page_end": 8,
            "block_start": 6,
            "block_end": 110,
            "first_result": "partial",
            "images": [
                {
                    "caption": "图 1-1 2015 年统考真题题图",
                    "role": "question_image",
                    "sha256": "7d4dfca377d8c590e0bcd809271c941a282fe7bc762c9df2ea17bf06cdbac0ed",
                },
                {
                    "caption": "图 1-2 用户最初代码截图",
                    "role": "learner_code_image",
                    "sha256": "4a2d7a15ff0e38b865da5c7d4c138a3127f73f295b4398b866c05468a683a3e9",
                },
                {
                    "caption": "图 1-3 题目参考解析图片",
                    "role": "solution_image",
                    "sha256": "2b7cc135cb7a4fd1cc6b2266a420a06fc03668ced88dc39dacc6d4c377e83d93",
                },
            ],
        },
        "part2": {
            "scope_id": "part2",
            "scope_title": "二、2019 年统考真题：单链表首尾交错重排",
            "next_boundary_title": "三、资料依据与图片清单",
            "render_page_start": 9,
            "render_page_end": 18,
            "block_start": 111,
            "block_end": 279,
            "first_result": "wrong",
            "images": [
                {
                    "caption": "图 2-1 2019 年统考真题题图",
                    "role": "question_image",
                    "sha256": "2bd465ac72036f0d843a9222f84ab1d01b82e1b772e50ee76a4697b617ef7aa9",
                },
                {
                    "caption": "图 2-2 题目参考解析图片",
                    "role": "solution_image",
                    "sha256": "dcc79dd66e712ae2bbe581fd0cc694a87168cc557cc8d81b88a2daadf1cc88e0",
                },
                {
                    "caption": (
                        "图 2-3 第一次生成的原地逆置图解（因含重复数值，用户指出辨识不清；"
                        "仅作为对话过程记录）"
                    ),
                    "role": "interaction_diagram",
                    "sha256": "eee046a3313ec19b7a1b8f60fc8b7593388fbe534499923cb6b8b3ab14338a27",
                },
                {
                    "caption": "图 2-4 使用 10、20、30、40、50、60 演示的后半段原地逆置图",
                    "role": "interaction_diagram_corrected",
                    "sha256": "1578ba0e89cb31c658271136fe0dfc6536379849fd5e27ca26d666ce62b9ba5b",
                },
                {
                    "caption": "图 2-5 用户针对 2019 年真题编写的代码截图",
                    "role": "learner_code_image",
                    "sha256": "c211df3a80421caacdeb005dd268352c5433525ba3e03578b87be62b96299f6d",
                },
            ],
        },
    },
}


class FormalProblemDocumentError(RuntimeError):
    """The source document, one scope, or its private binding failed closed."""


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _pretty_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def _sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _sha256_value(value: Any) -> str:
    return _sha256_bytes(_canonical_bytes(value))


def _read_regular_source(path_value: str | Path) -> tuple[Path, bytes]:
    path = Path(path_value).expanduser()
    try:
        before = path.lstat()
    except OSError as exc:
        raise FormalProblemDocumentError("source_docx_missing") from exc
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise FormalProblemDocumentError("source_docx_must_be_regular_non_symlink")
    if not 0 < before.st_size <= MAX_DOCX_BYTES:
        raise FormalProblemDocumentError("source_docx_size_invalid")
    try:
        with path.open("rb") as handle:
            opened = os.fstat(handle.fileno())
            if (
                opened.st_dev != before.st_dev
                or opened.st_ino != before.st_ino
                or opened.st_size != before.st_size
            ):
                raise FormalProblemDocumentError("source_docx_drifted_before_read")
            raw = handle.read(MAX_DOCX_BYTES + 1)
            after = os.fstat(handle.fileno())
    except FormalProblemDocumentError:
        raise
    except OSError as exc:
        raise FormalProblemDocumentError("source_docx_read_failed") from exc
    if (
        len(raw) != before.st_size
        or len(raw) > MAX_DOCX_BYTES
        or after.st_dev != before.st_dev
        or after.st_ino != before.st_ino
        or after.st_size != before.st_size
        or after.st_mtime_ns != before.st_mtime_ns
    ):
        raise FormalProblemDocumentError("source_docx_bytes_drifted")
    return path, raw


def _safe_zip_members(archive: zipfile.ZipFile) -> dict[str, zipfile.ZipInfo]:
    infos = archive.infolist()
    names = [info.filename for info in infos]
    if len(names) != len(set(names)):
        raise FormalProblemDocumentError("source_docx_duplicate_zip_member")
    total = 0
    result: dict[str, zipfile.ZipInfo] = {}
    for info in infos:
        pure = PurePosixPath(info.filename)
        unix_mode = (info.external_attr >> 16) & 0xFFFF
        if (
            pure.is_absolute()
            or ".." in pure.parts
            or info.flag_bits & 0x1
            or info.file_size < 0
            or info.file_size > MAX_ZIP_ENTRY_BYTES
            or (unix_mode and stat.S_ISLNK(unix_mode))
        ):
            raise FormalProblemDocumentError("source_docx_zip_member_unsafe")
        total += info.file_size
        if total > MAX_ZIP_TOTAL_BYTES:
            raise FormalProblemDocumentError("source_docx_zip_expansion_too_large")
        result[info.filename] = info
    required = {"[Content_Types].xml", "word/document.xml", "word/_rels/document.xml.rels"}
    if not required.issubset(result):
        raise FormalProblemDocumentError("source_docx_ooxml_parts_missing")
    return result


def _read_zip_part(
    archive: zipfile.ZipFile,
    infos: Mapping[str, zipfile.ZipInfo],
    name: str,
    *,
    maximum: int,
) -> bytes:
    info = infos.get(name)
    if info is None or info.file_size > maximum:
        raise FormalProblemDocumentError("source_docx_ooxml_part_invalid")
    try:
        raw = archive.read(info)
    except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
        raise FormalProblemDocumentError("source_docx_ooxml_part_unreadable") from exc
    if len(raw) != info.file_size:
        raise FormalProblemDocumentError("source_docx_ooxml_part_drifted")
    return raw


def _element_text(element: ET.Element) -> str:
    pieces: list[str] = []
    for node in element.iter():
        if node.tag == f"{{{NS['w']}}}t" and node.text:
            pieces.append(node.text)
        elif node.tag == f"{{{NS['w']}}}tab":
            pieces.append("\t")
        elif node.tag == f"{{{NS['w']}}}br":
            pieces.append("\n")
    return "".join(pieces).strip()


def _document_relationships(rels_root: ET.Element) -> dict[str, str]:
    result: dict[str, str] = {}
    for row in rels_root.findall("pr:Relationship", NS):
        rel_id = row.get("Id")
        target = row.get("Target")
        rel_type = row.get("Type") or ""
        if not rel_type.endswith("/image"):
            continue
        if (
            not rel_id
            or not target
            or row.get("TargetMode") == "External"
            or rel_id in result
        ):
            raise FormalProblemDocumentError("source_docx_image_relationship_invalid")
        normalized = posixpath.normpath(posixpath.join("word", target))
        pure = PurePosixPath(normalized)
        if pure.is_absolute() or ".." in pure.parts or pure.parts[:2] != ("word", "media"):
            raise FormalProblemDocumentError("source_docx_image_relationship_escape")
        result[rel_id] = pure.as_posix()
    return result


def _parse_blocks(document_root: ET.Element) -> list[dict[str, Any]]:
    body = document_root.find("w:body", NS)
    if body is None:
        raise FormalProblemDocumentError("source_docx_body_missing")
    blocks: list[dict[str, Any]] = []
    for index, child in enumerate(list(body)):
        if child.tag == f"{{{NS['w']}}}p":
            image_rids = [
                str(node.get(f"{{{NS['r']}}}embed"))
                for node in child.findall(".//a:blip", NS)
                if node.get(f"{{{NS['r']}}}embed")
            ]
            blocks.append(
                {
                    "source_block_index": index,
                    "kind": "paragraph",
                    "text": _element_text(child),
                    "image_relationship_ids": image_rids,
                }
            )
        elif child.tag == f"{{{NS['w']}}}tbl":
            rows: list[list[str]] = []
            for row in child.findall("w:tr", NS):
                rows.append(
                    [_element_text(cell) for cell in row.findall("w:tc", NS)]
                )
            blocks.append(
                {
                    "source_block_index": index,
                    "kind": "table",
                    "rows": rows,
                }
            )
    return blocks


def _paragraph_index(blocks: list[dict[str, Any]], text: str) -> int:
    expected = " ".join(text.split())
    matches = [
        int(row["source_block_index"])
        for row in blocks
        if row.get("kind") == "paragraph"
        and " ".join(str(row.get("text") or "").split()) == expected
    ]
    if len(matches) != 1:
        raise FormalProblemDocumentError("source_docx_section_boundary_invalid")
    return matches[0]


def _block_at(blocks: list[dict[str, Any]], source_index: int) -> dict[str, Any]:
    for row in blocks:
        if row.get("source_block_index") == source_index:
            return row
    raise FormalProblemDocumentError("source_docx_scope_block_missing")


def _next_caption(blocks: list[dict[str, Any]], source_index: int) -> str:
    for cursor in range(source_index + 1, source_index + 4):
        try:
            row = _block_at(blocks, cursor)
        except FormalProblemDocumentError:
            continue
        if row.get("kind") == "paragraph" and str(row.get("text") or "").strip():
            return str(row["text"])
    raise FormalProblemDocumentError("source_docx_image_caption_missing")


def _render_block(row: Mapping[str, Any]) -> str:
    if row.get("kind") == "paragraph":
        return str(row.get("text") or "")
    if row.get("kind") == "table":
        return "\n".join(" | ".join(cells) for cells in row.get("rows") or [])
    return ""


def _section_rows(
    blocks: list[dict[str, Any]], start: int, end: int
) -> list[dict[str, Any]]:
    return [
        copy.deepcopy(row)
        for row in blocks
        if start <= int(row.get("source_block_index", -1)) <= end
    ]


def _find_first_break(scope_rows: list[dict[str, Any]]) -> str:
    for row in scope_rows:
        if row.get("kind") != "table":
            continue
        for cells in row.get("rows") or []:
            if len(cells) >= 2 and cells[0].strip() == "用户真实第一断点":
                value = cells[1].strip()
                if value:
                    return value
    raise FormalProblemDocumentError("source_docx_first_break_missing")


def _bounded_join(values: list[str], maximum: int, label: str) -> str:
    text = "\n".join(value for value in values if value.strip()).strip()
    if not text or len(text.encode("utf-8")) > maximum:
        raise FormalProblemDocumentError(f"source_docx_{label}_invalid")
    return text


def _dialogue(
    scope_rows: list[dict[str, Any]], image_by_source_block: Mapping[int, list[dict[str, Any]]]
) -> dict[str, Any]:
    dialogue_start = next(
        (
            index
            for index, row in enumerate(scope_rows)
            if row.get("kind") == "paragraph" and row.get("text") == "3. 对话记录"
        ),
        None,
    )
    conclusion_start = next(
        (
            index
            for index, row in enumerate(scope_rows)
            if row.get("kind") == "paragraph"
            and row.get("text") == "4. 本题最终保留结论"
        ),
        None,
    )
    if (
        dialogue_start is None
        or conclusion_start is None
        or conclusion_start <= dialogue_start + 1
    ):
        raise FormalProblemDocumentError("source_docx_dialogue_boundary_invalid")
    turns: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for row in scope_rows[dialogue_start + 1 : conclusion_start]:
        text = _render_block(row).strip()
        if row.get("kind") == "paragraph" and SPEAKER_RE.fullmatch(text):
            if current is not None:
                turns.append(current)
            current = {
                "ordinal": len(turns) + 1,
                "role": "learner" if text.startswith("用户") else "assistant",
                "speaker_label": text,
                "content_blocks": [],
            }
            continue
        if current is None:
            if text:
                raise FormalProblemDocumentError("source_docx_dialogue_speaker_missing")
            continue
        block: dict[str, Any] = {
            "kind": str(row.get("kind")),
            "text": text,
        }
        image_rows = image_by_source_block.get(int(row["source_block_index"]), [])
        if image_rows:
            block["image_refs"] = [item["object_ref"] for item in image_rows]
        current["content_blocks"].append(block)
    if current is not None:
        turns.append(current)
    if not turns or {row["role"] for row in turns} != {"learner", "assistant"}:
        raise FormalProblemDocumentError("source_docx_dialogue_incomplete")
    for ordinal, turn in enumerate(turns, start=1):
        turn["ordinal"] = ordinal
        if not any(
            str(block.get("text") or "").strip() or block.get("image_refs")
            for block in turn["content_blocks"]
        ):
            raise FormalProblemDocumentError("source_docx_dialogue_turn_empty")
        turn["turn_sha256"] = _sha256_value(
            {key: value for key, value in turn.items() if key != "turn_sha256"}
        )
    core = {
        "schema": DIALOGUE_SCHEMA,
        "turns": turns,
        "turn_count": len(turns),
        "complete": True,
    }
    return {**core, "full_dialogue_sha256": _sha256_value(core)}


def _content_blocks(
    scope_rows: list[dict[str, Any]], image_by_source_block: Mapping[int, list[dict[str, Any]]]
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for ordinal, row in enumerate(scope_rows, start=1):
        material: dict[str, Any] = {
            "ordinal": ordinal,
            "source_block_index": int(row["source_block_index"]),
            "kind": str(row["kind"]),
        }
        if row["kind"] == "paragraph":
            material["text"] = str(row.get("text") or "")
            refs = image_by_source_block.get(int(row["source_block_index"]), [])
            material["image_refs"] = [item["object_ref"] for item in refs]
        else:
            material["rows"] = copy.deepcopy(row.get("rows") or [])
        result.append(material)
    return result


def _validate_contract(contract: Mapping[str, Any], scope_id: str) -> Mapping[str, Any]:
    if contract.get("schema_version") != "formal-problem-document-contract-v1":
        raise FormalProblemDocumentError("formal_problem_document_contract_invalid")
    if not SHA256_RE.fullmatch(str(contract.get("source_document_sha256") or "")):
        raise FormalProblemDocumentError("formal_problem_document_contract_invalid")
    scopes = contract.get("scopes")
    if not isinstance(scopes, Mapping) or set(scopes) != {"part1", "part2"}:
        raise FormalProblemDocumentError("formal_problem_document_contract_invalid")
    scope = scopes.get(scope_id)
    if not isinstance(scope, Mapping) or scope.get("scope_id") != scope_id:
        raise FormalProblemDocumentError("formal_problem_document_scope_invalid")
    return scope


def parse_formal_problem_document(
    source_docx: str | Path,
    scope_id: str,
    *,
    contract: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Parse and verify exactly one contract-defined scope from original OOXML."""

    selected_contract = contract or FORMAL_PROBLEM_DOCUMENT_CONTRACT
    scope_contract = _validate_contract(selected_contract, scope_id)
    _path, document_bytes = _read_regular_source(source_docx)
    document_sha256 = _sha256_bytes(document_bytes)
    if document_sha256 != selected_contract["source_document_sha256"]:
        raise FormalProblemDocumentError("source_docx_hash_drifted")
    try:
        archive = zipfile.ZipFile(io.BytesIO(document_bytes))
        infos = _safe_zip_members(archive)
        document_xml = _read_zip_part(
            archive, infos, "word/document.xml", maximum=MAX_XML_BYTES
        )
        rels_xml = _read_zip_part(
            archive,
            infos,
            "word/_rels/document.xml.rels",
            maximum=MAX_XML_BYTES,
        )
        document_root = ET.fromstring(document_xml)
        rels_root = ET.fromstring(rels_xml)
    except FormalProblemDocumentError:
        raise
    except (ET.ParseError, zipfile.BadZipFile, OSError) as exc:
        raise FormalProblemDocumentError("source_docx_ooxml_invalid") from exc
    blocks = _parse_blocks(document_root)
    if _paragraph_index(blocks, str(selected_contract["document_title"])) != 0:
        raise FormalProblemDocumentError("source_docx_title_mismatch")
    start = _paragraph_index(blocks, str(scope_contract["scope_title"]))
    next_start = _paragraph_index(blocks, str(scope_contract["next_boundary_title"]))
    end = next_start - 1
    if (
        start != scope_contract.get("block_start")
        or end != scope_contract.get("block_end")
        or end <= start
    ):
        raise FormalProblemDocumentError("source_docx_scope_boundary_drifted")
    scope_rows = _section_rows(blocks, start, end)
    relationships = _document_relationships(rels_root)
    parsed_images: list[dict[str, Any]] = []
    image_by_source_block: dict[int, list[dict[str, Any]]] = {}
    for row in scope_rows:
        for rel_id in row.get("image_relationship_ids") or []:
            part_name = relationships.get(str(rel_id))
            if part_name is None:
                raise FormalProblemDocumentError("source_docx_image_relationship_missing")
            image_bytes = _read_zip_part(
                archive, infos, part_name, maximum=evidence.MAX_ATTACHMENT_BYTES
            )
            try:
                detected_format, detected_mime = validate_image_bytes(image_bytes)
            except ImageIntegrityError as exc:
                raise FormalProblemDocumentError("source_docx_embedded_image_invalid") from exc
            caption = _next_caption(blocks, int(row["source_block_index"]))
            parsed = {
                "source_block_index": int(row["source_block_index"]),
                "caption": caption,
                "caption_sha256": _sha256_bytes(caption.encode("utf-8")),
                "source_part": part_name,
                "sha256": _sha256_bytes(image_bytes),
                "media_type": detected_mime,
                "detected_format": detected_format,
                "byte_count": len(image_bytes),
                "object_ref": SOURCE_OBJECT_REF_PREFIX + _sha256_bytes(image_bytes),
                "_bytes": image_bytes,
            }
            parsed_images.append(parsed)
            image_by_source_block.setdefault(
                int(row["source_block_index"]), []
            ).append(parsed)
    expected_images = scope_contract.get("images")
    if not isinstance(expected_images, list) or len(expected_images) != len(parsed_images):
        raise FormalProblemDocumentError("source_docx_scope_image_set_drifted")
    for parsed, expected in zip(parsed_images, expected_images, strict=True):
        if (
            not isinstance(expected, Mapping)
            or " ".join(parsed["caption"].split())
            != " ".join(str(expected.get("caption") or "").split())
            or parsed["sha256"] != expected.get("sha256")
            or expected.get("role")
            not in {
                "question_image",
                "solution_image",
                "learner_code_image",
                "interaction_diagram",
                "interaction_diagram_corrected",
            }
        ):
            raise FormalProblemDocumentError("source_docx_scope_image_hash_drifted")
        parsed["role"] = str(expected["role"])
    roles = {row["role"] for row in parsed_images}
    if not {"question_image", "solution_image"}.issubset(roles):
        raise FormalProblemDocumentError("source_docx_scope_required_images_missing")

    content_blocks = _content_blocks(scope_rows, image_by_source_block)
    question_start = next(
        (
            index
            for index, row in enumerate(scope_rows)
            if row.get("kind") == "paragraph" and row.get("text") == "1. 题目与图片材料"
        ),
        None,
    )
    dialogue_start = next(
        (
            index
            for index, row in enumerate(scope_rows)
            if row.get("kind") == "paragraph" and row.get("text") == "3. 对话记录"
        ),
        None,
    )
    if question_start is None or dialogue_start is None or dialogue_start <= question_start:
        raise FormalProblemDocumentError("source_docx_question_boundary_invalid")
    question_text = _bounded_join(
        [_render_block(row) for row in scope_rows[question_start:dialogue_start]],
        20_000,
        "question_text",
    )
    dialogue = _dialogue(scope_rows, image_by_source_block)
    learner_turns = [row for row in dialogue["turns"] if row["role"] == "learner"]
    assistant_turns = [row for row in dialogue["turns"] if row["role"] == "assistant"]
    learner_answer = _bounded_join(
        [
            _render_block(block)
            for turn in learner_turns
            for block in turn["content_blocks"]
        ],
        12_000,
        "learner_answer",
    )
    learner_reasoning = _bounded_join(
        [
            _render_block(block)
            for turn in learner_turns
            for block in turn["content_blocks"]
        ],
        16_000,
        "learner_reasoning",
    )
    assistant_text = _bounded_join(
        [
            _render_block(block)
            for turn in assistant_turns
            for block in turn["content_blocks"]
        ],
        24_000,
        "assistant_dialogue",
    )
    conclusion_index = next(
        index
        for index, row in enumerate(scope_rows)
        if row.get("kind") == "paragraph" and row.get("text") == "4. 本题最终保留结论"
    )
    final_correction = _bounded_join(
        [_render_block(row) for row in scope_rows[conclusion_index + 1 :]],
        16_000,
        "final_correction",
    )
    first_break = _find_first_break(scope_rows)
    first_action = next(
        (
            _render_block(block).strip()
            for block in learner_turns[0]["content_blocks"]
            if _render_block(block).strip()
        ),
        None,
    )
    if not isinstance(first_action, str) or len(first_action.encode("utf-8")) > 4_000:
        raise FormalProblemDocumentError("source_docx_first_action_invalid")

    ordered_image_sha256s = [row["sha256"] for row in parsed_images]
    scope_material = {
        "scope_id": scope_id,
        "scope_title": scope_contract["scope_title"],
        "source_document_sha256": document_sha256,
        "render_page_start": scope_contract["render_page_start"],
        "render_page_end": scope_contract["render_page_end"],
        "block_start": start,
        "block_end": end,
        "content_blocks": content_blocks,
        "ordered_embedded_image_sha256s": ordered_image_sha256s,
    }
    scope_sha256 = _sha256_value(scope_material)
    document_scope = {
        "scope_id": scope_id,
        "scope_title": str(scope_contract["scope_title"]),
        "scope_title_sha256": _sha256_bytes(
            str(scope_contract["scope_title"]).encode("utf-8")
        ),
        "source_document_sha256": document_sha256,
        "scope_sha256": scope_sha256,
        "render_page_start": int(scope_contract["render_page_start"]),
        "render_page_end": int(scope_contract["render_page_end"]),
        "block_start": start,
        "block_end": end,
        "question_sha256": _sha256_bytes(question_text.encode("utf-8")),
        "dialogue_sha256": str(dialogue["full_dialogue_sha256"]),
        "learner_answer_sha256": _sha256_bytes(learner_answer.encode("utf-8")),
        "first_break_sha256": _sha256_bytes(first_break.encode("utf-8")),
        "final_correction_sha256": _sha256_bytes(final_correction.encode("utf-8")),
        "ordered_embedded_image_sha256s": ordered_image_sha256s,
    }
    return {
        "schema_version": "formal-problem-document-parsed-scope-v1",
        "study_date": str(selected_contract["study_date"]),
        "document_scope": document_scope,
        "document_content": {
            "blocks": content_blocks,
            "block_count": len(content_blocks),
            "content_sha256": _sha256_value(content_blocks),
        },
        "question_text": question_text,
        "dialogue": dialogue,
        "learner_answer": learner_answer,
        "learner_reasoning": learner_reasoning,
        "assistant_text": assistant_text,
        "first_action": first_action,
        "first_break": first_break,
        "first_result": str(scope_contract["first_result"]),
        "final_correction": final_correction,
        "images": parsed_images,
        "_source_document_bytes": document_bytes,
    }


def _v4_attachment_rows(parsed: Mapping[str, Any]) -> list[dict[str, Any]]:
    document_scope = parsed["document_scope"]
    document_bytes = parsed["_source_document_bytes"]
    rows: list[dict[str, Any]] = [
        {
            "ordinal": 1,
            "role": "source_document",
            "object_ref": SOURCE_OBJECT_REF_PREFIX + document_scope["source_document_sha256"],
            "sha256": document_scope["source_document_sha256"],
            "media_type": DOCX_MEDIA_TYPE,
            "detected_format": "ooxml-docx",
            "byte_count": len(document_bytes),
            "source_part": "document.docx",
            "label_sha256": _sha256_bytes(b"source_document"),
        }
    ]
    for image in parsed["images"]:
        rows.append(
            {
                "ordinal": len(rows) + 1,
                "role": image["role"],
                "object_ref": image["object_ref"],
                "sha256": image["sha256"],
                "media_type": image["media_type"],
                "detected_format": image["detected_format"],
                "byte_count": image["byte_count"],
                "source_part": image["source_part"],
                "label_sha256": image["caption_sha256"],
            }
        )
    return rows


def build_formal_problem_document_bundle(parsed: Mapping[str, Any]) -> dict[str, Any]:
    scope = copy.deepcopy(parsed["document_scope"])
    rows = _v4_attachment_rows(parsed)
    study_date = str(parsed["study_date"])
    date_anchor = study_date + "T00:00:00+08:00"
    context_id = "FPDOC-" + str(scope["scope_sha256"])[:24].upper()
    source_id = (
        "formal-problem-document:"
        + str(scope["source_document_sha256"])
        + ":"
        + str(scope["scope_id"])
    )
    source_binding = {
        "source_document_sha256": scope["source_document_sha256"],
        "scope_id": scope["scope_id"],
        "scope_sha256": scope["scope_sha256"],
    }
    question_text = str(parsed["question_text"])
    final_correction = str(parsed["final_correction"])
    bundle = {
        "schema_version": BUNDLE_SCHEMA_V4,
        "capture_kind": CAPTURE_KIND,
        "context_id": context_id,
        "request_id": "FPDOC-REQUEST-" + str(scope["scope_sha256"])[:24].upper(),
        "session_id": None,
        "item_id": "FPDOC-ITEM-" + str(scope["scope_id"]).upper(),
        "source_id": source_id,
        "source_kind": CAPTURE_KIND,
        "study_date": study_date,
        "event_time": date_anchor,
        "timezone": "Asia/Shanghai",
        "source_binding_sha256": _sha256_value(source_binding),
        "question_mode": QUESTION_MODE,
        "source_document": {
            "object_ref": SOURCE_OBJECT_REF_PREFIX + scope["source_document_sha256"],
            "sha256": scope["source_document_sha256"],
            "byte_count": len(parsed["_source_document_bytes"]),
            "media_type": DOCX_MEDIA_TYPE,
            "package_format": "ooxml-docx",
        },
        "document_scope": scope,
        "document_content": copy.deepcopy(parsed["document_content"]),
        "current_question": {
            "public_text": question_text,
            "options": [],
            "response_instruction": "自由回答算法题；不得伪造选择题选项或 display receipt。",
            "public_surface_sha256": _sha256_value(
                {
                    "question_text": question_text,
                    "ordered_image_sha256s": scope["ordered_embedded_image_sha256s"],
                }
            ),
            "attachment_sha256s": [row["sha256"] for row in rows],
        },
        "learner_evidence": {
            "answer_text": parsed["learner_answer"],
            "choice": None,
            "confidence": None,
            "first_action": parsed["first_action"],
            "reasoning": parsed["learner_reasoning"],
            "prompt_level": "not_applicable",
            "observed_at": None,
        },
        "evaluation_evidence": {
            "grader_capsule_id": None,
            "correct_answer": None,
            "correct_option": None,
            "standard_explanation": parsed["assistant_text"],
            "grader_result": parsed["first_result"],
            "grader_basis": "source_document_explicit_dialogue",
            "provided_by": "formal_problem_document",
            "missing_fields": [],
        },
        "assessment": {
            "first_result": parsed["first_result"],
            "choice_result": "not_applicable",
            "reasoning_result": "documented_break",
            "confidence": None,
            "prompt_level": "not_applicable",
            "first_break": parsed["first_break"],
            "first_break_provenance": "source_document_explicit",
            "first_action": parsed["first_action"],
            "first_action_provenance": "source_document_explicit",
        },
        "frozen_assistant_feedback": {
            "text": final_correction,
            "sha256": _sha256_bytes(final_correction.encode("utf-8")),
        },
        "interaction_trace": copy.deepcopy(parsed["dialogue"]),
        "provenance": {
            "source_locator": (
                "formal-problem-document://sha256/"
                + str(scope["source_document_sha256"])
                + "/"
                + str(scope["scope_id"])
            ),
            "source_sha256": scope["source_document_sha256"],
            "context_sha256": scope["scope_sha256"],
            "attachment_provenance": copy.deepcopy(rows),
            "created_at": date_anchor,
            "missing_items": [],
        },
        "missing_fields": ["formal_identity", "main_knowledge"],
        "attachment_objects": rows,
        "model_call_count": 0,
        "luna_call_count": 0,
        "formal_write_count": 0,
    }
    _validate_v4_bundle(bundle)
    return bundle


def _validate_v4_attachment_rows(value: Any) -> list[dict[str, Any]]:
    required = {
        "ordinal",
        "role",
        "object_ref",
        "sha256",
        "media_type",
        "detected_format",
        "byte_count",
        "source_part",
        "label_sha256",
    }
    allowed_roles = {
        "source_document",
        "question_image",
        "solution_image",
        "learner_code_image",
        "interaction_diagram",
        "interaction_diagram_corrected",
    }
    if not isinstance(value, list) or not 3 <= len(value) <= evidence.MAX_ATTACHMENTS:
        raise FormalProblemDocumentError("formal_problem_attachment_rows_invalid")
    for ordinal, row in enumerate(value, start=1):
        if (
            not isinstance(row, dict)
            or set(row) != required
            or row.get("ordinal") != ordinal
            or row.get("role") not in allowed_roles
            or not SHA256_RE.fullmatch(str(row.get("sha256") or ""))
            or row.get("object_ref") != SOURCE_OBJECT_REF_PREFIX + str(row.get("sha256"))
            or not SHA256_RE.fullmatch(str(row.get("label_sha256") or ""))
            or isinstance(row.get("byte_count"), bool)
            or not isinstance(row.get("byte_count"), int)
            or not 0 < row["byte_count"] <= evidence.MAX_ATTACHMENT_BYTES
            or not isinstance(row.get("source_part"), str)
            or not row["source_part"]
            or ".." in PurePosixPath(row["source_part"]).parts
        ):
            raise FormalProblemDocumentError("formal_problem_attachment_rows_invalid")
    if value[0]["role"] != "source_document":
        raise FormalProblemDocumentError("formal_problem_source_document_missing")
    if (
        value[0]["media_type"] != DOCX_MEDIA_TYPE
        or value[0]["detected_format"] != "ooxml-docx"
        or value[0]["source_part"] != "document.docx"
        or value[0]["label_sha256"] != _sha256_bytes(b"source_document")
    ):
        raise FormalProblemDocumentError("formal_problem_source_document_invalid")
    for row in value[1:]:
        if row["media_type"] not in {"image/png", "image/jpeg"} or row[
            "detected_format"
        ] not in {"png", "jpeg"}:
            raise FormalProblemDocumentError("formal_problem_image_type_invalid")
    image_roles = {row["role"] for row in value[1:]}
    if not {"question_image", "solution_image"}.issubset(image_roles):
        raise FormalProblemDocumentError("formal_problem_required_images_missing")
    return value


def _validate_v4_bundle(bundle: Any) -> dict[str, Any]:
    required = {
        "schema_version",
        "capture_kind",
        "context_id",
        "request_id",
        "session_id",
        "item_id",
        "source_id",
        "source_kind",
        "study_date",
        "event_time",
        "timezone",
        "source_binding_sha256",
        "question_mode",
        "source_document",
        "document_scope",
        "document_content",
        "current_question",
        "learner_evidence",
        "evaluation_evidence",
        "assessment",
        "frozen_assistant_feedback",
        "interaction_trace",
        "provenance",
        "missing_fields",
        "attachment_objects",
        "model_call_count",
        "luna_call_count",
        "formal_write_count",
    }
    if not isinstance(bundle, dict) or set(bundle) != required:
        raise FormalProblemDocumentError("formal_problem_bundle_shape_invalid")
    if (
        bundle.get("schema_version") != BUNDLE_SCHEMA_V4
        or bundle.get("capture_kind") != CAPTURE_KIND
        or bundle.get("source_kind") != CAPTURE_KIND
        or bundle.get("question_mode") != QUESTION_MODE
        or bundle.get("timezone") != "Asia/Shanghai"
        or bundle.get("model_call_count") != 0
        or bundle.get("luna_call_count") != 0
        or bundle.get("formal_write_count") != 0
    ):
        raise FormalProblemDocumentError("formal_problem_bundle_identity_invalid")
    rows = _validate_v4_attachment_rows(bundle.get("attachment_objects"))
    if bundle.get("provenance", {}).get("attachment_provenance") != rows:
        raise FormalProblemDocumentError("formal_problem_attachment_provenance_mismatch")
    scope = bundle.get("document_scope")
    document = bundle.get("source_document")
    trace = bundle.get("interaction_trace")
    if (
        not isinstance(scope, Mapping)
        or set(scope)
        != {
            "scope_id",
            "scope_title",
            "scope_title_sha256",
            "source_document_sha256",
            "scope_sha256",
            "render_page_start",
            "render_page_end",
            "block_start",
            "block_end",
            "question_sha256",
            "dialogue_sha256",
            "learner_answer_sha256",
            "first_break_sha256",
            "final_correction_sha256",
            "ordered_embedded_image_sha256s",
        }
        or not isinstance(document, Mapping)
        or document.get("sha256") != scope.get("source_document_sha256")
        or document.get("object_ref") != SOURCE_OBJECT_REF_PREFIX + str(document.get("sha256"))
        or rows[0]["sha256"] != document.get("sha256")
        or not isinstance(trace, Mapping)
        or trace.get("schema") != DIALOGUE_SCHEMA
        or trace.get("complete") is not True
        or trace.get("full_dialogue_sha256") != scope.get("dialogue_sha256")
    ):
        raise FormalProblemDocumentError("formal_problem_scope_binding_invalid")
    document_content = bundle.get("document_content")
    if (
        not isinstance(document_content, Mapping)
        or set(document_content) != {"blocks", "block_count", "content_sha256"}
        or not isinstance(document_content.get("blocks"), list)
        or document_content.get("block_count") != len(document_content["blocks"])
        or document_content.get("content_sha256")
        != _sha256_value(document_content["blocks"])
    ):
        raise FormalProblemDocumentError("formal_problem_document_content_invalid")
    scope_material = {
        "scope_id": scope["scope_id"],
        "scope_title": scope["scope_title"],
        "source_document_sha256": scope["source_document_sha256"],
        "render_page_start": scope["render_page_start"],
        "render_page_end": scope["render_page_end"],
        "block_start": scope["block_start"],
        "block_end": scope["block_end"],
        "content_blocks": document_content["blocks"],
        "ordered_embedded_image_sha256s": scope[
            "ordered_embedded_image_sha256s"
        ],
    }
    if _sha256_value(scope_material) != scope["scope_sha256"]:
        raise FormalProblemDocumentError("formal_problem_scope_hash_mismatch")
    trace_core = {
        "schema": trace.get("schema"),
        "turns": trace.get("turns"),
        "turn_count": trace.get("turn_count"),
        "complete": trace.get("complete"),
    }
    if (
        not isinstance(trace_core["turns"], list)
        or trace_core["turn_count"] != len(trace_core["turns"])
        or _sha256_value(trace_core) != trace.get("full_dialogue_sha256")
    ):
        raise FormalProblemDocumentError("formal_problem_dialogue_hash_mismatch")
    for ordinal, turn in enumerate(trace_core["turns"], start=1):
        if (
            not isinstance(turn, Mapping)
            or turn.get("ordinal") != ordinal
            or turn.get("role") not in {"learner", "assistant"}
            or not isinstance(turn.get("content_blocks"), list)
            or turn.get("turn_sha256")
            != _sha256_value(
                {key: value for key, value in turn.items() if key != "turn_sha256"}
            )
        ):
            raise FormalProblemDocumentError("formal_problem_dialogue_turn_invalid")
    for key in (
        "scope_title_sha256",
        "source_document_sha256",
        "scope_sha256",
        "question_sha256",
        "dialogue_sha256",
        "learner_answer_sha256",
        "first_break_sha256",
        "final_correction_sha256",
    ):
        if not SHA256_RE.fullmatch(str(scope.get(key) or "")):
            raise FormalProblemDocumentError("formal_problem_scope_hash_invalid")
    if _sha256_bytes(str(scope["scope_title"]).encode("utf-8")) != scope["scope_title_sha256"]:
        raise FormalProblemDocumentError("formal_problem_scope_title_hash_mismatch")
    if _sha256_bytes(str(bundle["current_question"]["public_text"]).encode("utf-8")) != scope["question_sha256"]:
        raise FormalProblemDocumentError("formal_problem_question_hash_mismatch")
    if _sha256_bytes(str(bundle["learner_evidence"]["answer_text"]).encode("utf-8")) != scope["learner_answer_sha256"]:
        raise FormalProblemDocumentError("formal_problem_learner_answer_hash_mismatch")
    if _sha256_bytes(str(bundle["assessment"]["first_break"]).encode("utf-8")) != scope["first_break_sha256"]:
        raise FormalProblemDocumentError("formal_problem_first_break_hash_mismatch")
    if _sha256_bytes(str(bundle["frozen_assistant_feedback"]["text"]).encode("utf-8")) != scope["final_correction_sha256"]:
        raise FormalProblemDocumentError("formal_problem_final_correction_hash_mismatch")
    if bundle["current_question"].get("options") != [] or bundle["learner_evidence"].get("choice") is not None:
        raise FormalProblemDocumentError("formal_problem_mcq_fabrication_forbidden")
    if bundle["current_question"].get("attachment_sha256s") != [row["sha256"] for row in rows]:
        raise FormalProblemDocumentError("formal_problem_attachment_binding_mismatch")
    if scope["ordered_embedded_image_sha256s"] != [
        row["sha256"] for row in rows[1:]
    ]:
        raise FormalProblemDocumentError("formal_problem_scope_image_order_mismatch")
    return bundle


def _validate_formal_problem_document_receipt(value: Any) -> dict[str, Any]:
    required = {
        "schema_version",
        "status",
        "subject",
        "capture_kind",
        "capture_id",
        "event_id",
        "study_date",
        "idempotency_key_sha256",
        "capture_payload_sha256",
        "document_scope",
        "source_document",
        "attachment_objects",
        "evidence_manifest_locator",
        "evidence_manifest_sha256",
        "evidence_object_sha256",
        "raw_event",
        "hot_capture_receipt",
        "producer_binding",
        "background_handoff",
        "release_neutral",
        "model_call_count",
        "luna_call_count",
        "formal_write_count",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise FormalProblemDocumentError("formal_problem_receipt_shape_invalid")
    if (
        value.get("schema_version") != RECEIPT_SCHEMA
        or value.get("status") != "committed"
        or value.get("subject") != "cs408"
        or value.get("capture_kind") != CAPTURE_KIND
        or value.get("release_neutral") is not True
        or value.get("model_call_count") != 0
        or value.get("luna_call_count") != 0
        or value.get("formal_write_count") != 0
    ):
        raise FormalProblemDocumentError("formal_problem_receipt_identity_invalid")
    capture_id = str(value.get("capture_id") or "")
    event_id = str(value.get("event_id") or "")
    scope = value.get("document_scope")
    if (
        not CAPTURE_ID_RE.fullmatch(capture_id)
        or not EVENT_ID_RE.fullmatch(event_id)
        or not isinstance(scope, Mapping)
        or scope.get("scope_id") not in {"part1", "part2"}
        or not SHA256_RE.fullmatch(str(scope.get("source_document_sha256") or ""))
    ):
        raise FormalProblemDocumentError("formal_problem_receipt_capture_invalid")
    key = (
        "formal-problem-document:v1:"
        f"{scope['source_document_sha256']}:{scope['scope_id']}"
    )
    expected_capture_id = (
        f"CAP-{str(value['study_date']).replace('-', '')}-"
        f"{hashlib.sha256(key.encode('utf-8')).hexdigest()[:12]}"
    )
    expected_event_id = (
        "CE-"
        + hashlib.sha256(("capture:" + key).encode("utf-8")).hexdigest()[:20]
    )
    if (
        capture_id != expected_capture_id
        or event_id != expected_event_id
        or value.get("idempotency_key_sha256")
        != _sha256_bytes(key.encode("utf-8"))
    ):
        raise FormalProblemDocumentError("formal_problem_receipt_idempotency_invalid")
    for field in (
        "capture_payload_sha256",
        "evidence_manifest_sha256",
        "evidence_object_sha256",
    ):
        if not SHA256_RE.fullmatch(str(value.get(field) or "")):
            raise FormalProblemDocumentError("formal_problem_receipt_hash_invalid")
    if value.get("evidence_manifest_locator") != evidence.LOCATOR_PREFIX + str(
        value["evidence_manifest_sha256"]
    ):
        raise FormalProblemDocumentError("formal_problem_receipt_manifest_invalid")
    rows = _validate_v4_attachment_rows(value.get("attachment_objects"))
    source_document = value.get("source_document")
    if (
        not isinstance(source_document, Mapping)
        or source_document.get("sha256") != scope.get("source_document_sha256")
        or source_document.get("sha256") != rows[0]["sha256"]
        or source_document.get("object_ref") != rows[0]["object_ref"]
    ):
        raise FormalProblemDocumentError("formal_problem_receipt_source_invalid")
    raw_event = value.get("raw_event")
    if (
        not isinstance(raw_event, dict)
        or set(raw_event)
        != {
            "schema_version",
            "event_type",
            "event_id",
            "event_sha256",
            "capture_id",
            "payload_sha256",
            "ledger_relative_path",
        }
        or raw_event.get("schema_version") != "intake_fact_capture_event_v1"
        or raw_event.get("event_type") != "fact_captured"
        or raw_event.get("event_id") != event_id
        or raw_event.get("capture_id") != capture_id
        or raw_event.get("payload_sha256") != value["capture_payload_sha256"]
        or raw_event.get("ledger_relative_path") != SOURCE_LEDGER_RELATIVE_PATH
        or not SHA256_RE.fullmatch(str(raw_event.get("event_sha256") or ""))
    ):
        raise FormalProblemDocumentError("formal_problem_receipt_raw_event_invalid")
    hot = value.get("hot_capture_receipt")
    if (
        not isinstance(hot, dict)
        or set(hot) != {"schema_version", "receipt_sha256", "relative_path"}
        or hot.get("schema_version") != "capture_commit_receipt_v1"
        or not SHA256_RE.fullmatch(str(hot.get("receipt_sha256") or ""))
        or hot.get("relative_path")
        != SOURCE_HOT_RECEIPT_RELATIVE_ROOT
        + f"/{hot.get('receipt_sha256')}.json"
    ):
        raise FormalProblemDocumentError("formal_problem_receipt_hot_receipt_invalid")
    producer = value.get("producer_binding")
    if (
        not isinstance(producer, dict)
        or set(producer) != {"status", "attestation_sha256", "relative_path"}
        or producer.get("status") != "attested"
        or not SHA256_RE.fullmatch(str(producer.get("attestation_sha256") or ""))
        or producer.get("relative_path")
        != (
            "wiki/study_vaults/408-full/state/intake-curation/"
            "producer-binding-attestations/"
            f"{capture_id}.json"
        )
    ):
        raise FormalProblemDocumentError("formal_problem_receipt_producer_invalid")
    handoff = value.get("background_handoff")
    if (
        not isinstance(handoff, dict)
        or set(handoff)
        != {
            "status",
            "completion_kind",
            "locator",
            "object_sha256",
            "binding_sha256",
            "resolution_receipt_locator",
            "resolution_receipt_sha256",
            "interaction_trace_sha256",
        }
        or handoff.get("status") != "ready"
        or handoff.get("completion_kind") != "document_complete"
        or handoff.get("locator")
        != LEGACY_BACKGROUND_HANDOFF_LOCATOR_PREFIX
        + str(handoff.get("object_sha256") or "")
        or handoff.get("resolution_receipt_locator")
        != evidence.TURN_LOCATOR_PREFIX
        + str(handoff.get("resolution_receipt_sha256") or "")
        or handoff.get("interaction_trace_sha256") != scope.get("dialogue_sha256")
    ):
        raise FormalProblemDocumentError("formal_problem_receipt_handoff_invalid")
    for field in (
        "object_sha256",
        "binding_sha256",
        "resolution_receipt_sha256",
        "interaction_trace_sha256",
    ):
        if not SHA256_RE.fullmatch(str(handoff.get(field) or "")):
            raise FormalProblemDocumentError("formal_problem_receipt_handoff_hash_invalid")
    return value


def publish_formal_problem_document_bundle(
    parsed: Mapping[str, Any], *, private_root: str | Path | None = None
) -> dict[str, Any]:
    bundle = build_formal_problem_document_bundle(parsed)
    root = evidence._private_root(private_root)
    source_bytes = parsed["_source_document_bytes"]
    payloads = [source_bytes] + [row["_bytes"] for row in parsed["images"]]
    for row, raw in zip(bundle["attachment_objects"], payloads, strict=True):
        if len(raw) != row["byte_count"] or _sha256_bytes(raw) != row["sha256"]:
            raise FormalProblemDocumentError("formal_problem_attachment_bytes_drifted")
        evidence._atomic_private_write(
            root / "attachments" / f"{row['sha256']}.bin", raw, root=root
        )
    bundle_raw = _pretty_bytes(bundle)
    if not 0 < len(bundle_raw) <= evidence.MAX_BUNDLE_BYTES:
        raise FormalProblemDocumentError("formal_problem_bundle_too_large")
    bundle_sha256 = _sha256_bytes(bundle_raw)
    evidence._atomic_private_write(
        root / "objects" / f"{bundle_sha256}.json", bundle_raw, root=root
    )
    manifest = {
        "schema_version": evidence.MANIFEST_SCHEMA,
        "bundle_schema_version": BUNDLE_SCHEMA_V4,
        "context_id": bundle["context_id"],
        "request_id": bundle["request_id"],
        "source_id": bundle["source_id"],
        "source_kind": bundle["source_kind"],
        "session_id": bundle["session_id"],
        "item_id": bundle["item_id"],
        "study_date": bundle["study_date"],
        "source_binding_sha256": bundle["source_binding_sha256"],
        "object_sha256": bundle_sha256,
        "object_size_bytes": len(bundle_raw),
        "attachment_objects": copy.deepcopy(bundle["attachment_objects"]),
        "created_at": bundle["provenance"]["created_at"],
        "answer_safe_manifest": True,
        "formal_write_count": 0,
    }
    manifest_raw = _pretty_bytes(manifest)
    manifest_sha256 = _sha256_bytes(manifest_raw)
    evidence._atomic_private_write(
        root / "manifests" / f"{manifest_sha256}.json", manifest_raw, root=root
    )
    return {
        "schema": evidence.MANIFEST_SCHEMA,
        "locator": evidence.LOCATOR_PREFIX + manifest_sha256,
        "manifest_sha256": manifest_sha256,
        "object_sha256": bundle_sha256,
        "evidence_status": "ready",
        "formal_write_count": 0,
    }


def read_formal_problem_document_bundle(
    locator: str, *, private_root: str | Path | None = None
) -> tuple[dict[str, Any], dict[str, Any]]:
    root = evidence._private_root(private_root)
    if not isinstance(locator, str) or not locator.startswith(evidence.LOCATOR_PREFIX):
        raise FormalProblemDocumentError("formal_problem_manifest_locator_invalid")
    manifest_sha256 = locator[len(evidence.LOCATOR_PREFIX) :]
    manifest = evidence._read_object(
        root / "manifests" / f"{manifest_sha256}.json",
        manifest_sha256,
        root=root,
        maximum=evidence.MAX_METADATA_BYTES,
    )
    if (
        set(manifest) != evidence.MANIFEST_KEYS
        or manifest.get("schema_version") != evidence.MANIFEST_SCHEMA
        or manifest.get("bundle_schema_version") != BUNDLE_SCHEMA_V4
        or manifest.get("formal_write_count") != 0
    ):
        raise FormalProblemDocumentError("formal_problem_manifest_invalid")
    object_sha256 = str(manifest.get("object_sha256") or "")
    bundle = evidence._read_object(
        root / "objects" / f"{object_sha256}.json",
        object_sha256,
        root=root,
        maximum=evidence.MAX_BUNDLE_BYTES,
    )
    _validate_v4_bundle(bundle)
    object_path = root / "objects" / f"{object_sha256}.json"
    object_size = (
        object_path.stat().st_size
        if object_path.is_file()
        else len(_pretty_bytes(bundle))
    )
    if (
        manifest.get("object_size_bytes") != object_size
        or manifest.get("attachment_objects") != bundle.get("attachment_objects")
    ):
        raise FormalProblemDocumentError("formal_problem_manifest_bundle_mismatch")
    for row in bundle["attachment_objects"]:
        path = root / "attachments" / f"{row['sha256']}.bin"
        try:
            info = path.lstat()
            raw = path.read_bytes()
            local = True
        except OSError:
            local = False
            try:
                import archive_408_conversation_package as legacy_archive

                raw = legacy_archive.reopen_legacy_evidence_source_file(
                    evidence_root=root,
                    source_relative_path=f"attachments/{row['sha256']}.bin",
                    expected_sha256=str(row["sha256"]),
                    maximum=evidence.MAX_ATTACHMENT_BYTES,
                    archive_root=legacy_archive.ARCHIVE_ROOT,
                )
            except Exception as exc:
                raise FormalProblemDocumentError(
                    "formal_problem_attachment_missing_and_archived_reopen_failed"
                ) from exc
        if local and (
            stat.S_ISLNK(info.st_mode)
            or not stat.S_ISREG(info.st_mode)
            or stat.S_IMODE(info.st_mode) & 0o077
            or len(raw) != info.st_size
        ):
            raise FormalProblemDocumentError("formal_problem_attachment_drifted")
        if (
            len(raw) != row["byte_count"]
            or _sha256_bytes(raw) != row["sha256"]
        ):
            raise FormalProblemDocumentError("formal_problem_attachment_drifted")
    return manifest, bundle


def publish_formal_problem_document_receipt(
    receipt: Mapping[str, Any], *, private_root: str | Path | None = None
) -> dict[str, Any]:
    validated = _validate_formal_problem_document_receipt(dict(receipt))
    raw = _pretty_bytes(validated)
    if not 0 < len(raw) <= evidence.MAX_METADATA_BYTES:
        raise FormalProblemDocumentError("formal_problem_receipt_too_large")
    digest = _sha256_bytes(raw)
    root = evidence._private_root(private_root)
    evidence._atomic_private_write(
        root / "formal-problem-document-receipts" / f"{digest}.json",
        raw,
        root=root,
    )
    return {
        "locator": RECEIPT_LOCATOR_PREFIX + digest,
        "sha256": digest,
        "formal_write_count": 0,
    }


def read_formal_problem_document_receipt(
    locator: str, *, private_root: str | Path | None = None
) -> dict[str, Any]:
    if not isinstance(locator, str) or not locator.startswith(RECEIPT_LOCATOR_PREFIX):
        raise FormalProblemDocumentError("formal_problem_receipt_locator_invalid")
    digest = locator[len(RECEIPT_LOCATOR_PREFIX) :]
    root = evidence._private_root(private_root)
    value = evidence._read_object(
        root / "formal-problem-document-receipts" / f"{digest}.json",
        digest,
        root=root,
        maximum=evidence.MAX_METADATA_BYTES,
    )
    return _validate_formal_problem_document_receipt(value)


__all__ = [
    "BUNDLE_SCHEMA_V4",
    "CAPTURE_KIND",
    "DIALOGUE_SCHEMA",
    "FORMAL_PROBLEM_DOCUMENT_CONTRACT",
    "FormalProblemDocumentError",
    "RECEIPT_SCHEMA",
    "build_formal_problem_document_bundle",
    "parse_formal_problem_document",
    "publish_formal_problem_document_bundle",
    "publish_formal_problem_document_receipt",
    "read_formal_problem_document_bundle",
    "read_formal_problem_document_receipt",
]
