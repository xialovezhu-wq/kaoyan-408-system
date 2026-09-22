#!/usr/bin/env python3
"""Read-only compatibility classifier for the retired direct option router.

All current-question writes must enter through ``answer-current-and-next``.
This compatibility module may classify old messages, but never invokes a
current-question writer.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import unicodedata
from pathlib import Path
from typing import Any


ROUTER_SCHEMA = "managed_408_direct_router_v1"
ERROR_SCHEMA = "managed_408_direct_router_error_v1"
MAX_MESSAGE_BYTES = 16384
REQUEST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@-]{1,159}$")
OPTION_RE = re.compile(
    r"\A[ \t]*([A-Da-d])(?:"
    r"(?:[ \t]+|[ \t]*[,，、:：][ \t]*)"
    r"(high|medium|low|高|中|低)(?:[ \t]*置信度)?"
    r"|(高|中|低)(?:置信度)?"
    r")?[ \t]*[。.!！]?[ \t]*\Z",
    re.IGNORECASE,
)
SAVE_ONLY_RE = re.compile(
    r"\A\s*(?:请|现在|立即|马上|麻烦(?:你)?|帮我|请帮我)?\s*"
    r"(?:"
    r"(?:(?:把|将)\s*)?(?:这道题|本题)?\s*(?:快速)?入库"
    r"|提交\s*(?:这道题|本题)\s*到\s*408\s*主库"
    r"|记录\s*(?:这道题|本题)"
    r"|(?:(?:把|将)\s*)?(?:这道题|本题)的学习记录\s*入库"
    r")\s*(?:吧)?\s*[。！!]?\s*\Z"
)
CONFIDENCE = {
    "high": "high",
    "medium": "medium",
    "low": "low",
    "高": "high",
    "中": "medium",
    "低": "low",
}


class DirectRouterError(RuntimeError):
    """A stable direct-route failure after a route has been selected."""


def _json_bytes(value: Any, *, pretty: bool = False) -> bytes:
    options: dict[str, Any] = {"ensure_ascii": False, "sort_keys": True}
    if pretty:
        options["indent"] = 2
    else:
        options["separators"] = (",", ":")
    return (json.dumps(value, **options) + "\n").encode("utf-8")


def _sha_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _message_bytes(message: str | bytes) -> bytes:
    if isinstance(message, str):
        return message.encode("utf-8")
    if isinstance(message, bytes):
        return message
    raise TypeError("message_must_be_text_or_bytes")


def classify_message(message: str | bytes) -> dict[str, Any]:
    """Return one strict route without touching repository state."""

    raw = _message_bytes(message)
    message_sha = _sha_bytes(raw)
    if not raw or len(raw) > MAX_MESSAGE_BYTES:
        return {
            "route": "model_required",
            "reason_code": "message_shape_not_direct",
            "message_sha256": message_sha,
            "message_size_bytes": len(raw),
        }
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return {
            "route": "model_required",
            "reason_code": "message_not_utf8",
            "message_sha256": message_sha,
            "message_size_bytes": len(raw),
        }
    if "\n" in text or "\r" in text or "\x00" in text:
        return {
            "route": "model_required",
            "reason_code": "message_shape_not_direct",
            "message_sha256": message_sha,
            "message_size_bytes": len(raw),
        }
    normalized = unicodedata.normalize("NFKC", text)
    if SAVE_ONLY_RE.fullmatch(normalized) is not None:
        return {
            "route": "save_current_result",
            "message_sha256": message_sha,
            "message_size_bytes": len(raw),
        }
    option = OPTION_RE.fullmatch(normalized)
    if option is not None:
        confidence = option.group(2) or option.group(3)
        return {
            "route": "prepared_option_turn",
            "learner_choice": option.group(1).upper(),
            "confidence": (
                CONFIDENCE[confidence.casefold()]
                if confidence is not None
                else None
            ),
            "message_sha256": message_sha,
            "message_size_bytes": len(raw),
        }
    return {
        "route": "model_required",
        "reason_code": "message_shape_not_direct",
        "message_sha256": message_sha,
        "message_size_bytes": len(raw),
    }


def route_message(
    repo: str | Path,
    *,
    message: str | bytes,
    request_id: str,
    raw_context: dict[str, Any] | None = None,
    feedback_text: str | None = None,
    grader_capsule_locator: str | None = None,
    grader_capsule_sha256: str | None = None,
    private_root: str | Path | None = None,
) -> dict[str, Any]:
    """Reject every legacy write route without touching repository state."""

    decision = classify_message(message)
    if not REQUEST_RE.fullmatch(str(request_id or "")):
        raise DirectRouterError("request_id_invalid")
    reason_code = {
        "save_current_result": "legacy_save_route_disabled",
        "prepared_option_turn": (
            "legacy_direct_router_retired_use_answer_current_and_next"
        ),
    }.get(decision["route"], decision.get("reason_code", "message_shape_not_direct"))
    return {
        "schema": ROUTER_SCHEMA,
        "status": "model_required",
        "route": "model_required",
        "reason_code": reason_code,
        "request_id": request_id,
        "message_sha256": decision["message_sha256"],
        "message_size_bytes": decision["message_size_bytes"],
        "feedback_authorized": False,
        "business_call_count": 0,
        "formal_write_count": 0,
    }


class _CliParseError(ValueError):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise _CliParseError(message)


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--request-id", required=True)
    message = parser.add_mutually_exclusive_group(required=True)
    message.add_argument("--message-text")
    message.add_argument("--message-stdin", action="store_true")
    parser.add_argument(
        "--turn-json",
        help="Public-only JSON with context and feedback_text; no grader data",
    )
    parser.add_argument("--grader-capsule-locator")
    parser.add_argument("--grader-capsule-sha256")
    parser.add_argument("--private-root", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
    except _CliParseError as exc:
        sys.stdout.buffer.write(
            _json_bytes(
                {
                    "schema": ERROR_SCHEMA,
                    "status": "blocked",
                    "route": None,
                    "reason_code": str(exc),
                    "failure_stage": "preparse",
                    "display_authorized": False,
                    "business_call_count": 0,
                    "recovery_receipt_sha256": None,
                    "formal_write_count": 0,
                }
            )
        )
        return 2
    message = (
        sys.stdin.buffer.read() if args.message_stdin else args.message_text
    )
    try:
        turn = json.loads(args.turn_json) if args.turn_json else {}
        if turn and (not isinstance(turn, dict) or set(turn) != {"context", "feedback_text"}):
            raise DirectRouterError("public_turn_json_fields_invalid")
        result = route_message(
            args.repo,
            message=message,
            request_id=args.request_id,
            raw_context=turn.get("context"),
            feedback_text=turn.get("feedback_text"),
            grader_capsule_locator=args.grader_capsule_locator,
            grader_capsule_sha256=args.grader_capsule_sha256,
            private_root=args.private_root,
        )
    except (DirectRouterError, ValueError, RuntimeError) as exc:
        result = {
            "schema": ERROR_SCHEMA,
            "status": "blocked",
            "route": None,
            "reason_code": str(exc),
            "failure_stage": "preparse",
            "display_authorized": False,
            "business_call_count": 0,
            "recovery_receipt_sha256": None,
            "formal_write_count": 0,
        }
    sys.stdout.buffer.write(_json_bytes(result))
    return 0 if result.get("status") != "blocked" else 2


if __name__ == "__main__":
    raise SystemExit(main())
