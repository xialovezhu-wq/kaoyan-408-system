#!/usr/bin/env python3
"""Explicitly authorized standalone 408 quick intake.

The operation is deliberately local and ordered:

1. verify an explicit quick-intake command against the latest user turn;
2. bind the standalone item and request to a canonical private-payload hash;
3. seal and reopen the complete conversation package;
4. append and reopen the answer-safe pending capture;
5. publish one deterministic combined operation receipt.

Only hashes and opaque locators are written below the repository intake ledger.
Full question, options, dialogue, feedback, and attachment bytes stay below the
explicit private package root.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import mimetypes
import os
import re
import stat
import sys
import tempfile
import unicodedata
from pathlib import Path
from typing import Any, Callable, Iterator

import conversation_package_408 as conversation_package
import capture_hot_writer_408 as capture_hot_writer
import intake_fact_capture_408 as fact_capture
import study_episode_capture_408 as episode_capture


INPUT_SCHEMA = "cs408-standalone-question-intake-v1"
AUTHORIZATION_SCHEMA = "cs408-explicit-fast-intake-authorization-v1"
AUTHORIZATION_KIND = "explicit_fast_intake"
REQUEST_BINDING_SCHEMA = "cs408-standalone-question-request-binding-v1"
ITEM_BINDING_SCHEMA = "cs408-standalone-question-item-binding-v1"
OPERATION_RECEIPT_SCHEMA = "cs408-standalone-question-intake-receipt-v1"
TIMEZONE = "Asia/Shanghai"
REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@+-]{2,159}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
MAX_INPUT_BYTES = 8 * 1024 * 1024
MAX_TEXT_BYTES = 8 * 1024 * 1024
MAX_AUTHORIZATION_MESSAGE_BYTES = 4096
EXPLICIT_FAST_INTAKE_MESSAGES = frozenset(
    {
        "快速入库",
        "你记录一下",
        "你快记录一下",
        "请记录一下",
        "帮我记录一下",
        "请快速入库",
        "将这道题快速入库",
        "把这道题快速入库",
        "请将这道题快速入库",
        "请把这道题快速入库",
        "将此题快速入库",
        "把此题快速入库",
        "请将此题快速入库",
        "请把此题快速入库",
        "将当前题快速入库",
        "把当前题快速入库",
        "请将当前题快速入库",
        "请把当前题快速入库",
        "将这道题的学习记录快速入库",
        "把这道题的学习记录快速入库",
        "请将这道题的学习记录快速入库",
        "请把这道题的学习记录快速入库",
        "好,将这道题快速入库。它要取反,对对对",
        "此时客户端在等待服务器的 FIN将这道题快速入库",
    }
)
EXPLICIT_FAST_INTAKE_COMMAND_RE = re.compile(
    r"^(?:(?:这道题(?:标是|编号是)|题号(?:是|为)?)\s*[：:]?\s*"
    r"[A-Za-z0-9_.:@+-]{1,80}\s*[，,]\s*)?"
    r"(?:好(?:的)?\s*)?(?:直接\s*)?(?:(?:请|现在|开始|立即|马上|麻烦(?:你)?|帮我|请帮我|你)\s*)?"
    r"(?:(?:(?:把|将)\s*)?(?:这道题|这题|此题|该题|本题|当前题|这个点|(?:第\s*)?(?:\d{1,3}|[一二三四五六七八九十百]{1,6})\s*题)"
    r"(?:第[一二三四五六七八九十\d]+(?:小)?问)?"
    r"(?:(?:(?:关于|前面)[^，,。；;!?！？\n\"'“”‘’「」]{1,100})?(?:的|这段|这次)?(?:完整)?(?:学习记录|讨论|讲解|复盘))?"
    r"(?:(?:和|及|与)(?:这些|这几种|这段|上述|相关)[^，,。；;!?！？\n\"'“”‘’「」]{0,40}(?:讲解|讨论|复盘))?"
    r"(?:(?:、|和|及|与)(?:图解|(?:我的)?原稿|(?:修改前后|修改前|修改后)(?:的)?代码|代码)){0,4}\s*)?"
    r"(?:(?:也|一起|一块儿|一并)\s*)?快速(?:入库|录入|保存)(?:一下|吧)?[。.!！]?$"
)
EXPLICIT_METHOD_RECORD_COMMAND_RE = re.compile(
    r"^(?:所以)?(?:请)?(?:你)?(?:把|将)(?:我)?(?:这个|这道题的|这题的|当前的)"
    r"(?:思路|做法|方案|算法(?:流程)?)(?:记录|保存)(?:下来)?(?:就好|即可|吧)?[。.!！]?$"
)
RECORD_NAVIGATION_SUFFIX_RE = re.compile(
    r"^(?:好(?:的)?|(?:然后)?(?:直接)?(?:看|进入|开始|继续)?(?:下一道题|下一题)(?:吧)?)$"
)
SOURCE_KEYS = {
    "source_id",
    "title",
    "year",
    "subject",
    "question_type",
    "locator",
    "question",
    "options",
}


class StandaloneIntakeError(RuntimeError):
    """The standalone request is unsafe, malformed, or conflicts with state."""


def _json_bytes(value: Any, *, pretty: bool = True) -> bytes:
    options: dict[str, Any] = {"ensure_ascii": False, "sort_keys": True}
    if pretty:
        options["indent"] = 2
    else:
        options["separators"] = (",", ":")
    return (json.dumps(value, **options) + "\n").encode("utf-8")


def _sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _sha256_value(value: Any) -> str:
    return _sha256_bytes(_json_bytes(value, pretty=False))


def _bounded_text(value: object, label: str, *, required: bool = False) -> str:
    if not isinstance(value, str):
        raise StandaloneIntakeError(f"{label} must be a string")
    normalized = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    if required and not normalized:
        raise StandaloneIntakeError(f"{label} is required")
    if len(normalized.encode("utf-8")) > MAX_TEXT_BYTES:
        raise StandaloneIntakeError(f"{label} exceeds the private size ceiling")
    return normalized


def _raw_text(value: object, label: str, *, required: bool = False) -> str:
    # Evidence is never normalized; identity comparison uses _bounded_text separately.
    if not isinstance(value, str):
        raise StandaloneIntakeError(f"{label} must be a string")
    if required and not value.strip():
        raise StandaloneIntakeError(f"{label} is required")
    if len(value.encode("utf-8")) > MAX_TEXT_BYTES:
        raise StandaloneIntakeError(f"{label} exceeds the private size ceiling")
    return value


def _normalize_source(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        raise StandaloneIntakeError("source must be an object")
    unknown = sorted(set(value) - SOURCE_KEYS)
    if unknown:
        raise StandaloneIntakeError(f"source contains unknown fields: {unknown}")
    source_id = _bounded_text(value.get("source_id"), "source.source_id", required=True)
    result = {"source_id": source_id}
    for key in sorted(SOURCE_KEYS - {"source_id", "question", "options"}):
        if key in value:
            text = _bounded_text(value[key], f"source.{key}")
            if text:
                result[key] = text
    return result


def _normalize_options(value: object) -> list[dict[str, str]]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > 64:
        raise StandaloneIntakeError("options must be an array with at most 64 rows")
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, row in enumerate(value):
        if isinstance(row, str):
            label = chr(ord("A") + index) if index < 26 else str(index + 1)
            text = _raw_text(row, f"options[{index}]", required=True)
        elif isinstance(row, dict) and set(row) == {"label", "text"}:
            label = _bounded_text(row["label"], f"options[{index}].label", required=True)
            text = _raw_text(row["text"], f"options[{index}].text", required=True)
        else:
            raise StandaloneIntakeError(
                f"options[{index}] must be text or an exact label/text object"
            )
        if label in seen:
            raise StandaloneIntakeError("option labels must be unique")
        seen.add(label)
        result.append({"label": label, "text": text})
    return result


def _normalize_conversation(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise StandaloneIntakeError("conversation must be a non-empty array")
    result: list[dict[str, Any]] = []
    for index, row in enumerate(value):
        if not isinstance(row, dict):
            raise StandaloneIntakeError(f"conversation[{index}] must be an object")
        unknown = sorted(set(row) - {"role", "kind", "text", "observed_at"})
        if unknown:
            raise StandaloneIntakeError(
                f"conversation[{index}] contains unknown fields: {unknown}"
            )
        role = str(row.get("role") or "")
        if role == "learner":
            role = "user"
        if role not in {"user", "assistant"}:
            raise StandaloneIntakeError(f"conversation[{index}].role is invalid")
        kind = _bounded_text(
            row.get("kind", "utterance"),
            f"conversation[{index}].kind",
            required=True,
        )
        text = _raw_text(
            row.get("text"), f"conversation[{index}].text", required=True
        )
        observed_at = row.get("observed_at")
        if observed_at is not None:
            observed_at = _bounded_text(
                observed_at, f"conversation[{index}].observed_at", required=True
            )
        result.append(
            {
                "role": role,
                "kind": kind,
                "text": text,
                "observed_at": observed_at,
            }
        )
    return result


def explicit_batch_part_numbers(message: str):
    normalized = unicodedata.normalize("NFKC", message).strip()
    last = re.split(r"[。！？，,\n；;]\s*", normalized.rstrip("。.!！ \t\r\n"))[-1].strip()
    command = re.fullmatch(
        r"(?:请)?(?:把|将)第\s*(\d{1,3})\s*题"
        r"(第[一二三四五六七八九十\d]+问(?:\s*(?:和|、|及|与)\s*第[一二三四五六七八九十\d]+问)+)"
        r"\s*(?:都|一起|一并|也)?\s*快速(?:入库|录入|保存)", last)
    if not command:
        return None
    chinese = dict(zip('一二三四五六七八九十', range(1, 11)))
    parts = []
    for token in re.findall(r'第([一二三四五六七八九十\d]+)问', command[2]):
        number = int(token) if token.isdigit() else chinese.get(token)
        if number is None:
            return None
        if number not in parts:
            parts.append(number)
    return int(command[1]), tuple(parts)


def explicit_batch_question_numbers(message: str) -> tuple[int, ...]:
    normalized = unicodedata.normalize("NFKC", message).strip()
    last = re.split(r"[。！？，,\n；;]\s*", normalized.rstrip("。.!！ \t\r\n"))[-1].strip()
    command = re.fullmatch(
        r"(?:请)?(?:把|将)\s*(第\s*\d{1,3}\s*题(?:\s*(?:和|、|及|与)\s*第?\s*\d{1,3}\s*题)+)"
        r"\s*(?:都|一起|一并|也)?\s*快速(?:入库|录入|保存)", last)
    if not command:
        return ()
    return tuple(dict.fromkeys(int(n) for n in re.findall(r"\d+", command[1])))


def _is_explicit_fast_intake_message(message: str) -> bool:
    # Normalize only recognition; authorization and stored evidence retain the
    # complete original message, including transport line endings.
    normalized = unicodedata.normalize("NFKC", message).strip()
    # Native attachment placeholders can follow the user's final command.
    # Strip only empty trailing image tags for recognition, never evidence.
    normalized = re.sub(r'(?:\s*<image\s+[^<>]*></image>)+\s*$', '', normalized).strip()
    record_only = re.fullmatch(
        r'(.+?)[，,。；;\n]\s*(?:只记录|只保存|仅记录|仅保存)'
        r'(?:[，,、]\s*不(?:总结|讲解|复盘|输出知识点树)){0,3}[。.!！]?', normalized, re.S)
    if record_only:
        return _is_explicit_fast_intake_message(record_only[1])
    # An explicit completed save sentence can be followed by another question.
    # Keep all text as evidence and honor any later withdrawal of the save.
    for boundary in re.finditer(r"[。！？!?；;\n]", normalized):
        prefix = normalized[:boundary.end()].strip()
        suffix = normalized[boundary.end():].strip()
        if (suffix and len(prefix) < len(normalized)
                and not re.search(r"(?:不要|不用|别|取消|暂不|先不|无需|不必).{0,12}(?:保存|入库|录入|记录)", suffix)
                and _is_explicit_fast_intake_message(prefix)):
            return True
    # A later conditional cannot cancel an earlier complete save command.
    # A conditional with no preceding command still does not authorize a save.
    last_sentence = re.split(r'[。！？!?；;\n]', normalized.rstrip('。.!！ \t\r\n'))[-1].strip()
    if re.match(r'^(?:如果|假如|若是|要是|等到|等我|只有).+[，,]', last_sentence):
        return False
    if re.fullmatch(r"(?:然后)?第?[一二三四五六七八九十百\d]+题(?:我)?(?:也)?做对了[，,]\s*(?:请)?记录一下[。.!！]?", normalized):
        return True
    if normalized in EXPLICIT_FAST_INTAKE_MESSAGES:
        return True
    if EXPLICIT_FAST_INTAKE_COMMAND_RE.fullmatch(normalized):
        return True
    # A score self-report may precede an explicit numbered save request in
    # unpunctuated speech. Recognize the command without changing the evidence.
    score_then_command = re.fullmatch(
        r"(?:然后)?我第?[一二三四五六七八九十百\d]+题"
        r"(?:到|至)第?[一二三四五六七八九十百\d]+题"
        r"(?:全对|都对)(?:的)?[，,]?然后(.+)", normalized)
    if (score_then_command
            and EXPLICIT_FAST_INTAKE_COMMAND_RE.fullmatch(score_then_command[1])):
        return True
    # A complete save command can precede a separate follow-up request, with
    # an app thread link between them. Strip the link only for recognition.
    without_thread_links = re.sub(
        r"\[[^\]\n]*\]\(codex://threads/[0-9a-f-]+\)", "\n", normalized)
    sentences = re.split(r"[。！？!?；;\n]", without_thread_links)
    if not re.search(r"(?:不要|不用|别|取消|暂不|先不|无需|不必).{0,12}(?:保存|入库|录入|记录)",
                     without_thread_links):
        for sentence in sentences:
            command = re.sub(r"^(?:(?:好(?:的)?|我(?:理解|明白|懂)了)[，,]\s*)?(?:那\s*)?", "", sentence.strip())
            if EXPLICIT_FAST_INTAKE_COMMAND_RE.fullmatch(command):
                return True
            # Preservation notes may contain commas of their own.
            head, *tail = re.split(r"[，,]", command, maxsplit=1)
            if (tail and EXPLICIT_FAST_INTAKE_COMMAND_RE.fullmatch(head.strip())
                    and re.match(r"(?:(?:请|记得)?(?:保留|记录)|(?:保存|入库|录入)后|第[一二三四五六七八九十0-9]+问)", tail[0].strip())):
                return True
    if EXPLICIT_METHOD_RECORD_COMMAND_RE.fullmatch(normalized):
        return True
    if explicit_batch_question_numbers(normalized) or explicit_batch_part_numbers(normalized):
        return True
    opening_clauses = [c.strip() for c in re.split(r"[。！？，,\n；;]\s*", normalized) if c.strip()]
    while opening_clauses and re.fullmatch(r"(?:好(?:的)?|我(?:理解|明白|懂)了)", opening_clauses[0]):
        opening_clauses.pop(0)
    if (len(opening_clauses) > 1
            and EXPLICIT_FAST_INTAKE_COMMAND_RE.fullmatch(opening_clauses[0])
            and re.match(r"(?:同时|然后)?\s*第?\s*[一二三四五六七八九十\d]+(?:\s*(?:到|至|[-—~～])\s*第?\s*[一二三四五六七八九十\d]+)?\s*(?:题|问)"
                         r"|(?:保存|入库|录入)后(?=[^。！？?]{0,80}(?:总结|考点树))", opening_clauses[1])
            and not re.search(r"(?:不要|不用|别|取消|暂不|先不|无需|不必).{0,12}(?:保存|入库|录入|记录)",
                              "\n".join(opening_clauses[1:]))):
        return True
    # A standalone opening save command may be followed by a new numbered
    # question. Preserve the whole message, but do not ignore a later withdrawal.
    paragraphs = re.split(r"\n\s*\n", normalized)
    if (len(paragraphs) > 1
            and re.match(r"(?:同时|然后)\s*第?\s*\d+(?:\s*(?:到|至|[-—~～])\s*第?\s*\d+)?\s*题", paragraphs[1])
            and not re.search(r"(?:不要|不用|别|取消|暂不|先不|无需|不必).{0,12}(?:保存|入库|录入|记录)",
                              "\n".join(paragraphs[1:]))
            and _is_explicit_fast_intake_message(paragraphs[0])):
        return True
    # The learner may finish an explanation with a separate intake command.
    # Keep the entire latest message as evidence; inspect only its final clause
    # for authorization, so a quoted or negated mention does not authorize it.
    clauses = re.split(r"[。！？，,\n；;]\s*", normalized.rstrip("。.!！ \t\r\n"))
    clauses = [re.sub(r"^\d{1,3}[.)、]\s*", "", clause.strip()) for clause in clauses]
    # A learner may explicitly request recording and then move to the next
    # question. Only remove narrowly recognized navigation, not arbitrary prose.
    navigation_removed = False
    while clauses and (not clauses[-1] or RECORD_NAVIGATION_SUFFIX_RE.fullmatch(clauses[-1])):
        navigation_removed = navigation_removed or bool(clauses[-1])
        clauses.pop()
    # Preserve an evidence note after a save command without mistaking it for
    # a withdrawal. The full original message still enters the native package.
    while clauses and re.fullmatch(r"(?:(?:记得|请)?(?:保留|记录)[^。！？?\"'“”‘’]{1,120}|(?:保存|入库|录入)后[^。！？?\"'“”‘’]{0,80}(?:总结|考点树))", clauses[-1]) and not re.search(
            r"(?:不要|不用|别|取消|暂不|先不|无需|不必).{0,12}(?:保存|入库|录入)", clauses[-1]):
        clauses.pop()
    if not clauses:
        return False
    return (len(clauses) > 1 or navigation_removed) and (
        EXPLICIT_FAST_INTAKE_COMMAND_RE.fullmatch(clauses[-1]) is not None
        or EXPLICIT_METHOD_RECORD_COMMAND_RE.fullmatch(clauses[-1]) is not None
        or clauses[-1] in EXPLICIT_FAST_INTAKE_MESSAGES
        or clauses[-1] in {"这道题就快速入库", "讲的这道题快速入库", "讲这个快速入库", "那我理解了将这道题快速入库", "那这道题快速入库"}
    )


def _is_explicit_supplement_message(message: str) -> bool:
    normalized = unicodedata.normalize("NFKC", message).strip()
    if normalized in {"补充入库", "补充这道题", "更新这道题", "补充这道题快速入库"}:
        return True
    if re.search(r"不要|不用|取消|暂不|先不|不必|无需|是否|能否|可否", normalized):
        return False
    if re.fullmatch(
        r"(?:明白了[，,]\s*)?(?:请)?(?:把|将)这个补充保存到第\s*\d{1,3}\s*题"
        r"(?:[，,]\s*(?:然后)?继续第\s*\d{1,3}\s*题)?[。.!！]?", normalized
    ):
        return True
    return re.fullmatch(
        r"(?:好[，,]\s*)?(?:请)?(?:把|将)[^。！？；;\n\"'“”‘’]{1,120}"
        r"(?:补充|追加)(?:到|进)第\s*\d{1,3}\s*题的学习记录(?:里|中)?"
        r"(?:[，,]\s*(?:然后)?继续第\s*\d{1,3}\s*题)?[。.!！]?",
        normalized,
    ) is not None


def _normalize_authorization(
    value: object, conversation: list[dict[str, Any]], *, supplement: bool = False
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise StandaloneIntakeError(
            "explicit fast-intake authorization is required"
        )
    expected_keys = {"schema", "kind", "user_message"}
    unknown = sorted(set(value) - expected_keys)
    if unknown:
        raise StandaloneIntakeError(
            f"authorization contains unknown fields: {unknown}"
        )
    if value.get("schema") != AUTHORIZATION_SCHEMA:
        raise StandaloneIntakeError(
            f"authorization.schema must be {AUTHORIZATION_SCHEMA}"
        )
    if value.get("kind") != AUTHORIZATION_KIND:
        raise StandaloneIntakeError(
            f"authorization.kind must be {AUTHORIZATION_KIND}"
        )
    user_message = value.get("user_message")
    if not isinstance(user_message, str) or not user_message:
        raise StandaloneIntakeError("authorization.user_message is required")
    message_bytes = user_message.encode("utf-8")
    if len(message_bytes) > MAX_AUTHORIZATION_MESSAGE_BYTES:
        raise StandaloneIntakeError(
            "authorization.user_message exceeds the explicit-command ceiling"
        )
    latest_user_turn = next(
        (row for row in reversed(conversation) if row["role"] == "user"), None
    )
    if latest_user_turn is None or user_message != latest_user_turn["text"]:
        raise StandaloneIntakeError(
            "authorization.user_message must exactly equal the latest user turn"
        )
    supplement_command = supplement and _is_explicit_supplement_message(user_message)
    if not (_is_explicit_fast_intake_message(user_message) or supplement_command):
        raise StandaloneIntakeError(
            "latest user turn does not contain an explicit fast-intake command"
        )
    return {
        "schema": AUTHORIZATION_SCHEMA,
        "kind": AUTHORIZATION_KIND,
        "user_message_sha256": _sha256_bytes(message_bytes),
        "user_message_byte_count": len(message_bytes),
        "authorization_body_stored": False,
    }


def _read_attachment(path_value: object, label: str) -> tuple[Path, bytes]:
    path = Path(_bounded_text(path_value, label, required=True)).expanduser()
    try:
        before = path.lstat()
    except OSError as exc:
        raise StandaloneIntakeError(f"{label} is unreadable") from exc
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise StandaloneIntakeError(f"{label} must be a non-symlink regular file")
    if not 0 < before.st_size <= conversation_package.MAX_ATTACHMENT_BYTES:
        raise StandaloneIntakeError(f"{label} has an invalid byte count")
    try:
        with path.open("rb") as handle:
            opened = os.fstat(handle.fileno())
            if (
                opened.st_dev != before.st_dev
                or opened.st_ino != before.st_ino
                or opened.st_size != before.st_size
            ):
                raise StandaloneIntakeError(f"{label} drifted before read")
            data = handle.read(conversation_package.MAX_ATTACHMENT_BYTES + 1)
            after = os.fstat(handle.fileno())
    except OSError as exc:
        raise StandaloneIntakeError(f"{label} could not be read") from exc
    if (
        len(data) != before.st_size
        or after.st_dev != before.st_dev
        or after.st_ino != before.st_ino
        or after.st_size != before.st_size
        or after.st_mtime_ns != before.st_mtime_ns
    ):
        raise StandaloneIntakeError(f"{label} drifted during read")
    return path, data


def _normalize_attachments(value: object) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > conversation_package.MAX_ATTACHMENTS_PER_REVISION:
        raise StandaloneIntakeError("attachments must be a bounded array")
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for index, row in enumerate(value):
        if not isinstance(row, dict):
            raise StandaloneIntakeError(f"attachments[{index}] must be an object")
        unknown = sorted(set(row) - {"path", "role", "mime_type", "label"})
        if unknown:
            raise StandaloneIntakeError(
                f"attachments[{index}] contains unknown fields: {unknown}"
            )
        path, data = _read_attachment(row.get("path"), f"attachments[{index}].path")
        role = _bounded_text(
            row.get("role", "other_attachment"),
            f"attachments[{index}].role",
            required=True,
        )
        if role not in conversation_package.ATTACHMENT_ROLES:
            raise StandaloneIntakeError(f"attachments[{index}].role is invalid")
        mime_type = _bounded_text(
            row.get("mime_type") or mimetypes.guess_type(path.name)[0] or "application/octet-stream",
            f"attachments[{index}].mime_type",
            required=True,
        ).lower()
        attachment_label = _bounded_text(
            row.get("label") or role,
            f"attachments[{index}].label",
            required=True,
        )
        digest = _sha256_bytes(data)
        identity = (digest, role, attachment_label)
        if identity in seen:
            continue
        seen.add(identity)
        result.append(
            {
                "data": data,
                "role": role,
                "mime_type": mime_type,
                "label": attachment_label,
                "byte_count": len(data),
                "sha256": digest,
            }
        )
    return result


def _normalize_input(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise StandaloneIntakeError("input must be a JSON object")
    allowed = {
        "schema",
        "request_id",
        "study_date",
        "timezone",
        "source",
        "question",
        "options",
        "conversation",
        "frozen_feedback",
        "attachments",
        "missing_fields",
        "question_identity_hint",
        "authorization",
    }
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise StandaloneIntakeError(f"input contains unknown fields: {unknown}")
    if raw.get("schema") != INPUT_SCHEMA:
        raise StandaloneIntakeError(f"schema must be {INPUT_SCHEMA}")
    request_id = str(raw.get("request_id") or "")
    if not REQUEST_ID_RE.fullmatch(request_id):
        raise StandaloneIntakeError("request_id has an invalid format")
    study_date = str(raw.get("study_date") or "")
    try:
        dt.date.fromisoformat(study_date)
    except ValueError as exc:
        raise StandaloneIntakeError("study_date must be a real YYYY-MM-DD date") from exc
    if raw.get("timezone", TIMEZONE) != TIMEZONE:
        raise StandaloneIntakeError(f"timezone, when supplied, must be {TIMEZONE}")
    raw_source = raw.get("source")
    source = _normalize_source(raw_source)
    assert isinstance(raw_source, dict)
    top_question = _raw_text(raw.get("question", ""), "question")
    embedded_question = _raw_text(
        raw_source.get("question", ""), "source.question"
    )
    if top_question and embedded_question and top_question != embedded_question:
        raise StandaloneIntakeError(
            "top-level question conflicts with source.question"
        )
    question = top_question or embedded_question
    top_options = _normalize_options(raw.get("options"))
    embedded_options = _normalize_options(raw_source.get("options"))
    if top_options and embedded_options and top_options != embedded_options:
        raise StandaloneIntakeError(
            "top-level options conflict with source.options"
        )
    options = top_options or embedded_options
    conversation = _normalize_conversation(raw.get("conversation"))
    authorization = _normalize_authorization(
        raw.get("authorization"), conversation
    )
    frozen_feedback = _raw_text(
        raw.get("frozen_feedback", ""), "frozen_feedback"
    )
    question_identity_hint = _bounded_text(
        raw.get("question_identity_hint", ""), "question_identity_hint"
    )
    attachments = _normalize_attachments(raw.get("attachments"))
    if not question.strip() and not any(row["role"] == "question_image" for row in attachments):
        raise StandaloneIntakeError("question text or a question_image is required")
    missing_fields = raw.get("missing_fields") or []
    if not isinstance(missing_fields, list) or not all(
        isinstance(item, str) for item in missing_fields
    ):
        raise StandaloneIntakeError("missing_fields must be a string array")
    normalized_missing = sorted(
        {
            _bounded_text(item, "missing_fields[]", required=True)
            for item in missing_fields
        }
    )
    attachment_material = [
        {
            "role": row["role"],
            "mime_type": row["mime_type"],
            "label": row["label"],
            "byte_count": row["byte_count"],
            "sha256": row["sha256"],
        }
        for row in attachments
    ]
    question_image_sha256s = sorted(
        row["sha256"] for row in attachments if row["role"] == "question_image"
    )
    item_identity_material = {
        # A new study date is a new learning episode even when the question and
        # explicit authorization text are identical.  Same-day request retries
        # still converge on one item/package/Capture identity.
        "study_date": study_date,
        "question": {
            "text": _bounded_text(question, "question identity"),
            "question_image_sha256s": question_image_sha256s,
        },
        "options": [{"label": row["label"], "text": _bounded_text(row["text"], "option identity")} for row in options],
        "source": source,
    }
    item_identity_sha = _sha256_value(item_identity_material)
    item_id = f"SQI-{item_identity_sha[:24]}"
    request_material = {
        "schema": INPUT_SCHEMA,
        "study_date": study_date,
        "timezone": TIMEZONE,
        "source": source,
        "question": question,
        "options": options,
        "conversation": conversation,
        "frozen_feedback": frozen_feedback,
        "question_identity_hint": question_identity_hint,
        "attachments": attachment_material,
        "missing_fields": normalized_missing,
        "authorization": authorization,
    }
    return {
        "request_id": request_id,
        "request_id_sha256": _sha256_bytes(request_id.encode("utf-8")),
        "request_payload_sha256": _sha256_value(request_material),
        "study_date": study_date,
        "source": source,
        "question": question,
        "options": options,
        "conversation": conversation,
        "frozen_feedback": frozen_feedback,
        "question_identity_hint": question_identity_hint,
        "attachments": attachments,
        "attachment_material": attachment_material,
        "missing_fields": normalized_missing,
        "authorization": authorization,
        "item_id": item_id,
        "item_identity_sha256": item_identity_sha,
    }


def _operation_root(repo: Path) -> Path:
    return fact_capture.capture_root(repo) / "standalone-question-intake-v1"


def _capture_paths(repo: Path) -> tuple[Path, Path, Path]:
    root = fact_capture.capture_root(repo)
    return (
        root / "events.jsonl",
        root / "state.json",
        root / ".capture-hot-writer-v1",
    )


def _atomic_write(path: Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    tmp = Path(temporary)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        os.chmod(path, 0o600)
    finally:
        if tmp.exists():
            tmp.unlink()


@contextlib.contextmanager
def _operation_lock(root: Path, operation_identity: str) -> Iterator[None]:
    lock_dir = root / ".locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    path = lock_dir / f"{operation_identity}.lock"
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _read_json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise StandaloneIntakeError(f"{label} is unreadable") from exc
    if not isinstance(value, dict):
        raise StandaloneIntakeError(f"{label} must be an object")
    return value


def _write_or_verify_request_binding(root: Path, normalized: dict[str, Any]) -> str:
    binding = {
        "schema": REQUEST_BINDING_SCHEMA,
        "request_id_sha256": normalized["request_id_sha256"],
        "request_payload_sha256": normalized["request_payload_sha256"],
        "item_id": normalized["item_id"],
        "item_identity_sha256": normalized["item_identity_sha256"],
        "authorization": normalized["authorization"],
        "formal_write_count": 0,
        "background_processing": "none",
    }
    raw = _json_bytes(binding)
    path = root / "requests" / f"{normalized['request_id_sha256']}.json"
    if path.exists():
        if path.is_symlink() or not path.is_file():
            raise StandaloneIntakeError("request binding path is unsafe")
        if path.read_bytes() != raw:
            raise StandaloneIntakeError(
                "request_id is already bound to a different payload"
            )
    else:
        _atomic_write(path, raw)
    return _sha256_bytes(raw)


def _write_or_verify_item_binding(root: Path, normalized: dict[str, Any]) -> str:
    binding = {
        "schema": ITEM_BINDING_SCHEMA,
        "item_id": normalized["item_id"],
        "item_identity_sha256": normalized["item_identity_sha256"],
        "request_payload_sha256": normalized["request_payload_sha256"],
        "authorization": normalized["authorization"],
        "formal_write_count": 0,
        "background_processing": "none",
    }
    raw = _json_bytes(binding)
    path = root / "items" / f"{normalized['item_identity_sha256']}.json"
    if path.exists():
        if path.is_symlink() or not path.is_file():
            raise StandaloneIntakeError("item binding path is unsafe")
        if path.read_bytes() != raw:
            raise StandaloneIntakeError(
                "standalone_item_id is already bound to a different intake payload"
            )
    else:
        _atomic_write(path, raw)
    return _sha256_bytes(raw)


def _package_source(normalized: dict[str, Any]) -> dict[str, Any]:
    return {
        "entry_kind": "standalone_question_intake",
        "standalone_item_id": normalized["item_id"],
        "standalone_item_identity_sha256": normalized["item_identity_sha256"],
        "question_identity_hint": normalized["question_identity_hint"],
        "source": normalized["source"],
        "question": normalized["question"],
        "options": normalized["options"],
        "authorization": normalized["authorization"],
    }


def _package_source_id(normalized: dict[str, Any]) -> str:
    source_sha = _sha256_value(normalized["source"])
    return (
        f"SQISRC-{source_sha[:12]}-ITEM-"
        f"{normalized['item_identity_sha256'][:16]}-AUTH-"
        f"{normalized['authorization']['user_message_sha256'][:12]}"
    )


def _expected_package_turns(normalized: dict[str, Any]) -> list[dict[str, Any]]:
    turns = [
        {
            "ordinal": index,
            "role": row["role"],
            "kind": row["kind"],
            "text": row["text"],
            "observed_at": row["observed_at"],
        }
        for index, row in enumerate(normalized["conversation"], start=1)
    ]
    if normalized["frozen_feedback"]:
        turns.append(
            {
                "ordinal": len(turns) + 1,
                "role": "assistant",
                "kind": "frozen_feedback",
                "text": normalized["frozen_feedback"],
                "observed_at": None,
            }
        )
    return turns


def _verify_package(
    opened: dict[str, Any], normalized: dict[str, Any], source_id: str
) -> dict[str, Any]:
    manifest = opened.get("manifest")
    receipt = opened.get("receipt")
    source = opened.get("source")
    conversation = opened.get("conversation")
    if not all(isinstance(value, dict) for value in (manifest, receipt, source, conversation)):
        raise StandaloneIntakeError("conversation package surfaces are incomplete")
    expected_source_fields = {
        "subject": "cs408",
        "study_date": normalized["study_date"],
        "timezone": TIMEZONE,
        "session_id": None,
        "item_id": normalized["item_id"],
        "source_id": source_id,
        "source": _package_source(normalized),
    }
    if any(source.get(key) != value for key, value in expected_source_fields.items()):
        raise StandaloneIntakeError("conversation package source binding drifted")
    expected_turns = _expected_package_turns(normalized)
    if (
        conversation.get("turns") != expected_turns
        or conversation.get("turn_count") != len(expected_turns)
        or conversation.get("complete_order_preserved") is not True
        or conversation.get("truncated") is not False
    ):
        raise StandaloneIntakeError("conversation package trace binding drifted")
    expected_attachments = normalized["attachment_material"]
    actual_attachments = [
        {
            "role": row.get("role"),
            "mime_type": row.get("mime_type"),
            "label": row.get("label"),
            "byte_count": row.get("byte_count"),
            "sha256": row.get("sha256"),
        }
        for row in manifest.get("attachments") or []
        if isinstance(row, dict)
    ]
    if actual_attachments != expected_attachments:
        raise StandaloneIntakeError("conversation package attachment binding drifted")
    if (
        manifest.get("session_id") is not None
        or manifest.get("item_id") != normalized["item_id"]
        or manifest.get("source_id") != source_id
        or manifest.get("missing_fields") != normalized["missing_fields"]
        or manifest.get("sealed") is not True
        or manifest.get("formal_write_count") != 0
        or manifest.get("background_processing") != "none"
        or receipt.get("sealed") is not True
        or receipt.get("status") != fact_capture.PENDING_FORMAL_STATUS
        or receipt.get("formal_write_count") != 0
        or receipt.get("background_processing") != "none"
        or receipt.get("canonical_sha256") != manifest.get("canonical_sha256")
    ):
        raise StandaloneIntakeError("conversation package receipt binding drifted")
    canonical_sha = str(receipt.get("canonical_sha256") or "")
    if not SHA256_RE.fullmatch(canonical_sha):
        raise StandaloneIntakeError("conversation package hash is invalid")
    return {
        "locator": receipt["locator"],
        "canonical_sha256": canonical_sha,
    }


def _seal_or_reopen_package(
    normalized: dict[str, Any], package_root: Path
) -> dict[str, Any]:
    source_id = _package_source_id(normalized)
    package_id = conversation_package.package_id_for(
        study_date=normalized["study_date"],
        session_id=None,
        item_id=normalized["item_id"],
        source_id=source_id,
    )
    locator = conversation_package.LOCATOR_PREFIX + package_id
    package_dir = package_root / normalized["study_date"] / package_id
    if package_dir.exists():
        opened = conversation_package.read_package(
            locator, private_root=package_root
        )
    else:
        descriptors = [
            {
                "data": row["data"],
                "role": row["role"],
                "mime_type": row["mime_type"],
                "label": row["label"],
            }
            for row in normalized["attachments"]
        ]
        receipt = conversation_package.record_package(
            study_date=normalized["study_date"],
            session_id=None,
            item_id=normalized["item_id"],
            source_id=source_id,
            source=_package_source(normalized),
            conversation=normalized["conversation"],
            frozen_feedback=normalized["frozen_feedback"],
            attachments=descriptors,
            missing_fields=normalized["missing_fields"],
            seal=True,
            private_root=package_root,
        )
        if receipt.get("locator") != locator:
            raise StandaloneIntakeError("conversation package identity drifted")
        opened = conversation_package.read_package(
            locator, private_root=package_root
        )
    return _verify_package(opened, normalized, source_id)


def _capture_payload(
    normalized: dict[str, Any], package_receipt: dict[str, Any]
) -> dict[str, Any]:
    source_sha = _sha256_value(normalized["source"])
    source_facts: dict[str, str] = {
        "source_id": f"SQISRC-{source_sha[:20]}",
        "details_id": (
            "SQIAUTH-"
            + normalized["authorization"]["user_message_sha256"][:20]
        ),
    }
    subject = normalized["source"].get("subject")
    if subject in {"DS", "CO", "OS", "CN", "未确认"}:
        source_facts["subject"] = subject
    raw = {
        "schema": fact_capture.SCHEMA,
        "study_date": normalized["study_date"],
        "timezone": TIMEZONE,
        "idempotency_key": (
            "standalone-question-fast-intake:"
            + normalized["item_identity_sha256"][:24]
            + ":"
            + normalized["authorization"]["user_message_sha256"][:16]
        ),
        "stable_evidence_refs": [
            {
                "kind": fact_capture.CONVERSATION_PACKAGE_REF_KIND,
                "locator": package_receipt["locator"],
                "sha256": package_receipt["canonical_sha256"],
            }
        ],
        "source_facts": source_facts,
        "user_facts": {
            "user_error_entry": "未观察到",
            "user_error_provenance": "not_observed",
        },
        "identity_hint": {
            "status": "unknown",
            "mode": "unknown",
            "basis": "standalone-private-package",
        },
        "answer_safe_context_anchor": "standalone-cs408-private-package",
        "missing_fields": normalized["missing_fields"],
        "formalization_authorized": True,
        "authorization_policy": fact_capture.CONVERSATION_PACKAGE_PENDING_POLICY,
    }
    try:
        return fact_capture._validate_capture_payload(raw)
    except fact_capture.CaptureError as exc:
        raise StandaloneIntakeError(
            f"standalone capture payload is invalid: {exc}"
        ) from exc


def _commit_capture(
    repo: Path, capture_payload: dict[str, Any]
) -> dict[str, Any]:
    ledger, state, hot_root = _capture_paths(repo)
    if not ledger.is_file() or not state.is_file():
        raise StandaloneIntakeError(
            "verified capture hot writer is not prepared"
        )
    try:
        with capture_hot_writer.capture_ledger_lock(ledger) as lock_token:
            result = capture_hot_writer.capture_fact(
                ledger,
                state,
                hot_root,
                lock_token=lock_token,
                validated_payload=capture_payload,
            )
        durable = capture_hot_writer.verify_joint_commit(
            ledger,
            state,
            hot_root,
            event_id=str(result.get("event_id") or ""),
        )
        canonical = capture_hot_writer.canonical_fact_event_from_payload(
            capture_payload
        )
    except (capture_hot_writer.CaptureHotWriterError, OSError) as exc:
        raise StandaloneIntakeError(
            f"verified capture commit failed: {exc}"
        ) from exc
    joint = result.get("joint_verification")
    if (
        result.get("status") not in {"CAPTURED", "ALREADY_COMMITTED"}
        or result.get("quality_status") != fact_capture.PENDING_FORMAL_STATUS
        or not isinstance(joint, dict)
        or joint.get("status") != "PASS"
        or durable.get("status") != "PASS"
        or durable.get("event_type") != "fact_captured"
        or durable.get("event_id") != canonical["event_id"]
        or durable.get("capture_id") != canonical["capture_id"]
        or result.get("event_id") != canonical["event_id"]
        or result.get("capture_id") != canonical["capture_id"]
        or result.get("formal_write_count") != 0
        or durable.get("formal_write_count") != 0
    ):
        raise StandaloneIntakeError(
            "capture did not produce a durable verified joint receipt"
        )
    return {
        **result,
        "capture_id": canonical["capture_id"],
        "payload_sha256": canonical["payload_sha256"],
        "event_id": canonical["event_id"],
        "event_sha256": durable["event_sha256"],
        "joint_verification": durable,
    }


def _verify_capture_binding(
    normalized: dict[str, Any],
    capture_payload: dict[str, Any],
    capture_result: dict[str, Any],
    package_receipt: dict[str, Any],
    repo: Path,
) -> dict[str, Any]:
    binding = fact_capture.reopen_capture_binding(
        str(capture_result.get("capture_id") or ""),
        expected_payload_sha256=str(capture_result.get("payload_sha256") or ""),
        expected_idempotency_key=capture_payload["idempotency_key"],
        repo_root=repo,
    )
    expected_ref = {
        "kind": fact_capture.CONVERSATION_PACKAGE_REF_KIND,
        "locator": package_receipt["locator"],
        "sha256": package_receipt["canonical_sha256"],
    }
    joint = capture_result.get("joint_verification")
    if (
        binding.get("stable_evidence_refs") != [expected_ref]
        or binding.get("quality_status") != fact_capture.PENDING_FORMAL_STATUS
        or binding.get("formalization_authorized") is not True
        or binding.get("formal_write_count") != 0
        or not isinstance(joint, dict)
        or joint.get("status") != "PASS"
        or joint.get("event_type") != "fact_captured"
        or joint.get("event_id") != binding.get("event_id")
        or joint.get("capture_id") != binding.get("capture_id")
        or joint.get("formal_write_count") != 0
        or not SHA256_RE.fullmatch(str(joint.get("receipt_sha256") or ""))
        or not SHA256_RE.fullmatch(
            str(joint.get("joint_manifest_sha256") or "")
        )
        or not SHA256_RE.fullmatch(
            str(joint.get("bounded_manifest_sha256") or "")
        )
    ):
        raise StandaloneIntakeError("capture did not reopen with the package binding")
    return {
        **binding,
        "joint_receipt_sha256": joint["receipt_sha256"],
        "joint_manifest_sha256": joint["joint_manifest_sha256"],
        "bounded_manifest_sha256": joint["bounded_manifest_sha256"],
        "joint_status": "PASS",
    }


def _operation_receipt(
    normalized: dict[str, Any],
    item_binding_sha256: str,
    request_binding_sha256: str,
    package_receipt: dict[str, Any],
    capture_binding: dict[str, Any],
) -> dict[str, Any]:
    try:
        capture_time = dt.datetime.fromisoformat(
            str(capture_binding["capture_committed_at_utc"]).replace("Z", "+00:00")
        )
    except (KeyError, ValueError) as exc:
        raise StandaloneIntakeError("capture commit time binding is invalid") from exc
    if capture_time.tzinfo is None:
        raise StandaloneIntakeError("capture commit time must include timezone")
    captured_at = capture_time.astimezone(
        dt.timezone(dt.timedelta(hours=8))
    ).isoformat()
    return {
        "schema": OPERATION_RECEIPT_SCHEMA,
        "status": "awaiting_sol_formalization",
        "request_id_sha256": normalized["request_id_sha256"],
        "request_payload_sha256": normalized["request_payload_sha256"],
        "request_binding_sha256": request_binding_sha256,
        "item_binding_sha256": item_binding_sha256,
        "standalone_item_id": normalized["item_id"],
        "standalone_item_identity_sha256": normalized["item_identity_sha256"],
        "authorization_kind": normalized["authorization"]["kind"],
        "authorization_user_message_sha256": normalized["authorization"][
            "user_message_sha256"
        ],
        "authorization_user_message_byte_count": normalized["authorization"][
            "user_message_byte_count"
        ],
        "authorization_body_stored": False,
        "session_id": None,
        "conversation_package_locator": package_receipt["locator"],
        "conversation_package_sha256": package_receipt["canonical_sha256"],
        "capture_id": capture_binding["capture_id"],
        "capture_payload_sha256": capture_binding["payload_sha256"],
        "capture_event_id": capture_binding["event_id"],
        "capture_event_sha256": capture_binding["event_sha256"],
        "capture_joint_receipt_sha256": capture_binding[
            "joint_receipt_sha256"
        ],
        "capture_joint_manifest_sha256": capture_binding[
            "joint_manifest_sha256"
        ],
        "capture_bounded_manifest_sha256": capture_binding[
            "bounded_manifest_sha256"
        ],
        "capture_joint_status": capture_binding["joint_status"],
        "captured_at": captured_at,
        "captured_at_timezone": TIMEZONE,
        "time_quality": "capture_event_committed_at",
        "formal_write_count": 0,
        "background_processing": "none",
        "managed_session_write_count": 0,
        "display_write_count": 0,
        "answer_operation_write_count": 0,
        "navigation_write_count": 0,
        "t9_write_count": 0,
        "obsidian_write_count": 0,
    }


def supplement_value(raw: dict[str, Any], *, repo_root: str | Path,
                     package_root: str | Path, fault_injector: Any = None) -> dict[str, Any]:
    """Explicit append-only evidence update; dates and source are inherited."""
    allowed = {"schema", "request_id", "capture_id", "parent_package_locator",
               "parent_package_sha256", "new_turns", "authorization", "attachments",
               "frozen_feedback", "missing_fields"}
    if set(raw) - allowed or raw.get("schema") != "cs408-standalone-question-supplement-v1":
        raise StandaloneIntakeError("supplement input fields invalid")
    request_id = raw.get("request_id", "")
    if not isinstance(request_id, str) or not REQUEST_ID_RE.fullmatch(request_id):
        raise StandaloneIntakeError("request_id has an invalid format")
    turns = _normalize_conversation(raw.get("new_turns"))
    authorization = _normalize_authorization(raw.get("authorization"), turns, supplement=True)
    attachment_input = raw.get("attachments")
    if isinstance(attachment_input, list) and any(isinstance(row, dict) and "data" in row for row in attachment_input):
        new_attachments = episode_capture._normalize_attachments(attachment_input)
    else:
        new_attachments = _normalize_attachments(attachment_input)
    frozen = _raw_text(raw.get("frozen_feedback", ""), "frozen_feedback")
    missing = raw.get("missing_fields")
    if missing is not None and (not isinstance(missing, list) or not all(isinstance(x, str) for x in missing)):
        raise StandaloneIntakeError("missing_fields must be a string array")
    repo, private = Path(repo_root).resolve(), Path(package_root).resolve()
    operation_root = _operation_root(repo)
    capture_id = fact_capture._validate_id(raw.get("capture_id"), "capture_id")
    parent_ref = {"locator": raw.get("parent_package_locator"), "sha256": raw.get("parent_package_sha256")}
    if not SHA256_RE.fullmatch(str(parent_ref["sha256"])):
        raise StandaloneIntakeError("parent_package_sha256 invalid")
    # Only hashes enter request bindings; source paths do not affect retry identity.
    material = {"schema": raw["schema"], "capture_id": capture_id,
                "parent_evidence": parent_ref, "new_turns": turns,
                "authorization": authorization, "frozen_feedback": frozen,
                "missing_fields": missing,
                "attachments": [{k: v for k, v in row.items() if k != "data"} for row in new_attachments]}
    payload_sha = _sha256_value(material)
    request_sha = _sha256_bytes(request_id.encode())
    with _operation_lock(operation_root, "request-" + request_sha):
        with _operation_lock(operation_root, "supplement-" + capture_id):
            with fact_capture._ledger_lock(fact_capture.capture_root(repo)):
                state = fact_capture.replay(fact_capture._load_events(fact_capture.capture_root(repo)))
            capture = state["captures"].get(capture_id)
            if not capture:
                raise StandaloneIntakeError("supplement capture missing")
            origin_ref = capture["capture"]["stable_evidence_refs"][0]
            origin = conversation_package.verify_package_binding(origin_ref["locator"],
                expected_canonical_sha256=origin_ref["sha256"], require_sealed=True, private_root=private)
            origin_source = origin["source"]["source"]
            native_origin = (origin_source.get("entry_kind") == "study_episode_capture"
                             and origin_source.get("execution_kind") == "standalone_question")
            if origin_source.get("entry_kind") != "standalone_question_intake" and not native_origin:
                raise StandaloneIntakeError("supplement requires a standalone episode")
            parent = conversation_package.verify_package_binding(parent_ref["locator"],
                expected_canonical_sha256=parent_ref["sha256"], require_sealed=True, private_root=private)
            source_id = "SQISUP-" + payload_sha
            item_id = origin["manifest"]["item_id"]
            date = capture["study_date"]
            package_id = conversation_package.package_id_for(study_date=date, session_id=None,
                item_id=item_id, source_id=source_id)
            locator = conversation_package.LOCATOR_PREFIX + package_id
            history = capture.get("supplement_history", [])
            known = next((ref for ref in history if ref["locator"] == locator), None)
            expected_parent = history[-1] if history else {k: origin_ref[k] for k in ("locator", "sha256")}
            if known is None:
                if parent_ref != expected_parent:
                    raise StandaloneIntakeError("supplement parent is stale")
                if any(b.get("status") == "active" for b in state["batches"].values()):
                    raise StandaloneIntakeError("active batch blocks supplement")
            source = {**origin["source"]["source"],
                "entry_kind": "study_episode_capture" if native_origin else "standalone_question_supplement",
                "episode_capture_id": capture_id,
                "root_package_locator": origin_ref["locator"], "root_package_sha256": origin_ref["sha256"],
                "parent_package_locator": parent_ref["locator"], "parent_package_sha256": parent_ref["sha256"],
                "supplement_request_payload_sha256": payload_sha, "supplement_authorization": authorization}
            if native_origin:
                source["evidence_revision_kind"] = "native_standalone_supplement"
            binding_normalized = {"request_id_sha256": request_sha,
                "request_payload_sha256": payload_sha, "item_id": item_id,
                "item_identity_sha256": origin_source.get("standalone_item_identity_sha256", origin["canonical_sha256"]),
                "authorization": authorization}
            _write_or_verify_request_binding(operation_root, binding_normalized)
            old_turns = [{k: v for k, v in row.items() if k != "ordinal"}
                         for row in parent["conversation"]["turns"]]
            descriptors = []
            for row in parent["manifest"]["attachments"]:
                descriptors.append({"data": (Path(parent["package_dir"]) / row["relative_path"]).read_bytes(),
                                    **{k: row[k] for k in ("role", "mime_type", "label")}})
            seen = {(hashlib.sha256(row["data"]).hexdigest(), row["role"], row["label"]) for row in descriptors}
            for row in new_attachments:
                identity = (row["sha256"], row["role"], row["label"])
                if identity not in seen:
                    descriptors.append({k: row[k] for k in ("data", "role", "mime_type", "label")})
                    seen.add(identity)
            final_missing = missing if missing is not None else parent["manifest"]["missing_fields"]
            expected_turns = _expected_package_turns({"conversation": old_turns + turns, "frozen_feedback": frozen})
            if not (private / date / package_id / "receipt.json").exists():
                conversation_package.record_package(study_date=date, session_id=None, item_id=item_id,
                    source_id=source_id, source=source, conversation=old_turns + turns,
                    frozen_feedback=frozen, attachments=descriptors, missing_fields=final_missing,
                    seal=True, private_root=private)
            opened = conversation_package.verify_package_binding(locator, require_sealed=True, private_root=private)
            if (opened["source"]["source"] != source or opened["conversation"]["turns"] != expected_turns
                or opened["manifest"]["missing_fields"] != sorted(set(final_missing))):
                raise StandaloneIntakeError("supplement package binding drifted")
            if fault_injector:
                fault_injector("after_package")
            ref = {"locator": locator, "sha256": opened["canonical_sha256"]}
            append = (fact_capture.append_native_standalone_supplement if native_origin
                      else fact_capture.append_standalone_supplement)
            committed = append(capture_id, evidence=ref,
                parent_evidence=parent_ref, repo_root=repo, package_root=private,
                fault_injector=fault_injector)
            receipt = {"schema": "cs408-standalone-question-supplement-receipt-v1",
                "status": "awaiting_sol_formalization", "capture_id": capture_id,
                "standalone_item_id": item_id, "study_date": date,
                "request_payload_sha256": payload_sha, "conversation_package_locator": locator,
                "conversation_package_sha256": ref["sha256"], "parent_evidence": parent_ref,
                "supplement_event_id": committed["event_id"], "formal_write_count": 0,
                "capture_write_count": 0, "background_processing": "none", "navigation_write_count": 0}
            path = operation_root / "receipts" / f"{request_sha}.json"
            encoded = _json_bytes(receipt)
            if path.exists() and path.read_bytes() != encoded:
                raise StandaloneIntakeError("supplement receipt conflicts with reopened state")
            if not path.exists():
                if fault_injector:
                    fault_injector("before_receipt")
                _atomic_write(path, encoded)
            if path.read_bytes() != encoded:
                raise StandaloneIntakeError("supplement receipt verification failed")
            return {**receipt, "receipt_ref": str(path)} if native_origin else receipt


def intake_value(
    raw: dict[str, Any],
    *,
    repo_root: str | Path,
    package_root: str | Path,
    fault_injector: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Execute or recover one standalone intake operation."""

    if raw.get("schema") == episode_capture.SCHEMA:
        if raw.get("execution_kind") != "standalone_question":
            raise StandaloneIntakeError(
                "standalone entry requires execution_kind=standalone_question"
            )
        turns = _normalize_conversation(raw.get("turns"))
        # The shared episode writer accepts the owning flow's authorization as
        # typed private evidence.  This standalone facade additionally proves
        # that the latest real user turn is an explicit quick-intake command;
        # ordinary explanation still cannot enter Capture.
        _normalize_authorization(raw.get("authorization"), turns)
        try:
            return episode_capture.record_episode(
                repo_root,
                raw,
                package_root=package_root,
                fault_injector=fault_injector,
            )
        except episode_capture.StudyEpisodeCaptureError as exc:
            raise StandaloneIntakeError(str(exc)) from exc
    if raw.get("schema") == "cs408-standalone-question-supplement-v1":
        return supplement_value(raw, repo_root=repo_root, package_root=package_root, fault_injector=fault_injector)
    normalized = _normalize_input(raw)
    repo = Path(repo_root).expanduser().resolve()
    if not repo.is_dir():
        raise StandaloneIntakeError("repo_root does not exist")
    private_root = Path(package_root).expanduser().resolve()
    operation_root = _operation_root(repo)
    operation_root.mkdir(parents=True, exist_ok=True)

    def fault(stage: str) -> None:
        if fault_injector is not None:
            fault_injector(stage)

    request_lock = "request-" + normalized["request_id_sha256"]
    item_lock = "item-" + normalized["item_identity_sha256"]
    with _operation_lock(operation_root, request_lock):
        with _operation_lock(operation_root, item_lock):
            item_binding_sha = _write_or_verify_item_binding(
                operation_root, normalized
            )
            request_binding_sha = _write_or_verify_request_binding(
                operation_root, normalized
            )
            receipt_path = (
                operation_root
                / "receipts"
                / f"{normalized['request_id_sha256']}.json"
            )
            package_receipt = _seal_or_reopen_package(
                normalized, private_root
            )
            fault("after_package")
            capture_payload = _capture_payload(normalized, package_receipt)
            capture_result = _commit_capture(repo, capture_payload)
            fault("after_capture")
            capture_binding = _verify_capture_binding(
                normalized,
                capture_payload,
                capture_result,
                package_receipt,
                repo,
            )
            receipt = _operation_receipt(
                normalized,
                item_binding_sha,
                request_binding_sha,
                package_receipt,
                capture_binding,
            )
            receipt_raw = _json_bytes(receipt)
            if receipt_path.exists():
                if receipt_path.is_symlink() or not receipt_path.is_file():
                    raise StandaloneIntakeError(
                        "operation receipt path is unsafe"
                    )
                if receipt_path.read_bytes() != receipt_raw:
                    raise StandaloneIntakeError(
                        "operation receipt conflicts with reopened state"
                    )
            else:
                fault("before_receipt")
                _atomic_write(receipt_path, receipt_raw)
            reopened = _read_json_object(receipt_path, "operation receipt")
            if (
                reopened != receipt
                or _sha256_bytes(receipt_path.read_bytes())
                != _sha256_bytes(receipt_raw)
            ):
                raise StandaloneIntakeError(
                    "operation receipt reopen verification failed"
                )
            return reopened


def intake(
    payload_path: str | Path,
    *,
    repo_root: str | Path,
    package_root: str | Path,
) -> dict[str, Any]:
    path = Path(payload_path).expanduser()
    try:
        before = path.lstat()
    except OSError as exc:
        raise StandaloneIntakeError("payload file is unreadable") from exc
    if (
        stat.S_ISLNK(before.st_mode)
        or not stat.S_ISREG(before.st_mode)
        or before.st_size > MAX_INPUT_BYTES
    ):
        raise StandaloneIntakeError("payload file must be a bounded regular file")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise StandaloneIntakeError("payload file is invalid JSON") from exc
    return intake_value(
        raw,
        repo_root=repo_root,
        package_root=package_root,
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Seal one explicitly authorized standalone 408 quick intake and "
            "index it for Sol formalization"
        )
    )
    parser.add_argument("--repo", default=".")
    parser.add_argument(
        "--package-root", default=str(conversation_package.DEFAULT_ROOT)
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    record = subparsers.add_parser("record")
    record.add_argument("--input-json", required=True)
    record.add_argument("--repo", dest="record_repo")
    record.add_argument(
        "--package-root", dest="record_package_root"
    )
    args = parser.parse_args()
    try:
        result = intake(
            args.input_json,
            repo_root=args.record_repo or args.repo,
            package_root=args.record_package_root or args.package_root,
        )
    except (
        StandaloneIntakeError,
        conversation_package.ConversationPackageError,
        fact_capture.CaptureError,
        OSError,
    ) as exc:
        print(f"BLOCKED: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
