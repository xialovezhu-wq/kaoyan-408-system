"""Read-only HTTP and current-question attachments for prebuilt 3D assets.

No route starts a generator, opens a desktop application, or writes study data.
The cache reader owns source/version validation; this layer owns HTTP scope and
the explicit stage-request boundary. The intent header prevents accidental
prefetching, not access by an owner of the local filesystem.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import importlib
import json
import re
from http import HTTPStatus
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qs, unquote, urlsplit

import runtime_paths_408 as _paths

PREFIX = "/study-visuals"
IDENTITY = re.compile(r"(?:EXAM408-\d{4}-Q\d{2}(?:-P\d{2})?|(?:MATH|EN|DEMO)-[A-Za-z0-9][A-Za-z0-9_-]{0,100})\Z")
EXAM_IDENTITY = re.compile(r"EXAM408-\d{4}-Q\d{2}(?:-P\d{2})?\Z")
SHA = re.compile(r"[0-9a-f]{64}\Z")
ASSET_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,120}\.(?:png|webp|jpe?g)\Z")
PUBLIC_STATIC = {
    "web/index.html": "text/html; charset=utf-8",
    "web/player.js": "text/javascript; charset=utf-8",
    "web/player.css": "text/css; charset=utf-8",
    "web/tablet-view.js": "text/javascript; charset=utf-8",
    "web/tablet-view.css": "text/css; charset=utf-8",
    "models.mjs": "text/javascript; charset=utf-8",
    "model-scenes.mjs": "text/javascript; charset=utf-8",
    "execution-models.mjs": "text/javascript; charset=utf-8",
    "vendor/three.module.js": "text/javascript; charset=utf-8",
    "vendor/three.core.js": "text/javascript; charset=utf-8",
    "vendor/OrbitControls.js": "text/javascript; charset=utf-8",
    "vendor/CSS2DRenderer.js": "text/javascript; charset=utf-8",
}
POINTER_FIELDS = {
    "status", "question_id", "stage", "cache_key", "viewer_url", "preview_url",
    "preview_path", "source_version", "template_id", "reason", "hint_level",
    "source_content_sha256", "available_stages", "available_hint_levels",
    "lesson_revision",
}
STATUS_CODES = {
    "unsupported": HTTPStatus.NOT_FOUND,
    "missing": HTTPStatus.NOT_FOUND,
    "out_of_scope": HTTPStatus.NOT_FOUND,
    "stale": HTTPStatus.CONFLICT,
    "invalid": HTTPStatus.BAD_REQUEST,
}


def _reader():
    # Lazy import preserves original study flows if optional assets are absent.
    return importlib.import_module("study_visuals_408")


def question_pointer(repo: Path, question_id: Any, *, expected_surface_sha256=None):
    """Read only an explicit current exam identity; never use a formal prototype."""
    if not isinstance(question_id, str) or not EXAM_IDENTITY.fullmatch(question_id):
        return None
    try:
        value = _reader().get_visual_pointer(
            Path(repo), question_id, stage="question",
            expected_surface_sha256=expected_surface_sha256,
        )
    except (ImportError, OSError, ValueError, RuntimeError, KeyError, TypeError, AttributeError):
        return None
    if not isinstance(value, dict) or value.get("status") == "out_of_scope":
        return None
    if value.get("question_id") != question_id or value.get("stage") != "question":
        return None
    return {key: copy.deepcopy(value[key]) for key in POINTER_FIELDS if key in value}


def attach_delivery(repo: Path, display: dict[str, Any]) -> dict[str, Any]:
    """Add a safe preview to a runtime delivery copy, not its immutable receipt."""
    from study_visual_display_408 import teaching_contract, hide_previous_question
    # Moving on also dismisses a previous owned scene when this new item has no
    # prepared 3D asset. It must never leave another question on the iPad.
    try:
        hide_previous_question(repo, display.get("true_item_id"))
    except (OSError, ValueError, RuntimeError):
        pass
    pointer = question_pointer(repo, display.get("true_item_id"),
                               expected_surface_sha256=display.get("surface_sha256"))
    if pointer is None:
        return display
    result = dict(display)
    result["cached_visuals"] = pointer
    automatic = teaching_contract(repo, pointer["question_id"])
    if automatic:
        # Current user policy: ordinary questions and correct answers stay in
        # chat. The tutor explicitly sends its current diagnosis before teaching.
        result["automatic_visual_teaching"] = automatic
        marker = f"<!-- study-visuals-3d:{pointer['question_id']}:question -->"
        if marker in str(result.get("presentation_markdown") or ""):
            result["presentation_markdown"] = result["presentation_markdown"].split(marker, 1)[0].rstrip()
        return result
    identity = pointer["question_id"]
    marker = f"<!-- study-visuals-3d:{identity}:question -->"
    current = str(result.get("presentation_markdown") or "")
    if marker in current:
        return result
    if pointer.get("status") == "available":
        preview = pointer.get("preview_path")
        viewer = pointer.get("viewer_url")
        try:
            viewer_parts = urlsplit(viewer) if isinstance(viewer, str) else None
        except ValueError:
            viewer_parts = None
        # The cache reader verifies the actual PNG and its source binding. Do
        # not turn malformed optional metadata into a link to another surface.
        if (isinstance(preview, str) and Path(preview).is_absolute()
                and Path(preview).suffix.lower() == ".png"
                and viewer_parts is not None and viewer_parts.scheme == "http"
                and viewer_parts.hostname in {"127.0.0.1", "localhost", "::1"}
                and viewer_parts.path == PREFIX + "/" + identity):
            result["presentation_markdown"] = (
                current + "\n\n" + marker + "\n\n"
                + f"![三维题面预览]({preview})\n\n[查看立体图]({viewer})"
            )
    elif pointer.get("status") == "unsupported":
        result["presentation_markdown"] = current + "\n\n" + marker + "\n\n三维课件：模板尚不支持，本题保留原题材料。"
    return result


def current_question_id(query: Mapping[str, Any]):
    # An explicit null means no known current question and forbids fallback.
    if "current_question_id" in query:
        identity = query.get("current_question_id")
    elif "exam_question_id" in query:
        identity = query.get("exam_question_id")
    else:
        identity = query.get("source_id")
    if not isinstance(identity, str) or not EXAM_IDENTITY.fullmatch(identity):
        return None
    part = query.get("current_question_part")
    if part is not None:
        if not isinstance(part, str) or not re.fullmatch(r"P\d{2}", part):
            return None
        if "-P" in identity:
            if not identity.endswith("-" + part):
                return None
        else:
            identity += "-" + part
    return identity


def attach_context(repo: Path, query: Mapping[str, Any], result: dict[str, Any]):
    """All teacher views get only the safe current-question cache pointer."""
    identity = current_question_id(query)
    pointer = question_pointer(repo, identity)
    if pointer is None:
        return result
    response = {**result, "cached_visuals": pointer}
    # resolve passes its view separately; the native result carries the
    # effective view. Prefer it so a protected result cannot inherit a broader
    # request, and teacher corrections reach ordinary resolve calls.
    if (result.get("view") or query.get("view")) in {"direct", "after_attempt"}:
        try:
            from study_visual_interaction_408 import teacher_corrections
            corrections = teacher_corrections(repo, identity)
            if corrections:
                response["current_question_source_corrections"] = {
                    "teacher_only": True, "question_id": identity,
                    "instruction": "旧教案与原题原图冲突处按此精确题纠正；仍须遵守本轮提示权限，不直接序列化教师材料。",
                    "items": corrections}
        except (OSError, ValueError, KeyError):
            pass
    from study_visual_display_408 import teaching_contract
    automatic = teaching_contract(repo, identity)
    if automatic:
        response["automatic_visual_teaching"] = automatic
        import sys
        bridge = str(_paths.bridge_root())
        if bridge not in sys.path:
            sys.path.insert(0, bridge)
        try:
            from study_visual_dispatch import attach_protocol
            response = attach_protocol(response, '408', current_id=identity,
                                       source_version=result.get('source_version') or result.get('version'))
        except ImportError:
            pass
    return response


def _send(handler, data: bytes, status=HTTPStatus.OK, content_type="application/json; charset=utf-8", *, csp=None):
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(data)))
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.send_header("Referrer-Policy", "same-origin")
    if csp:
        handler.send_header("Content-Security-Policy", csp)
    handler.end_headers()
    if getattr(handler, "command", "GET") != "HEAD":
        handler.wfile.write(data)


def _json(handler, value, status=HTTPStatus.OK):
    _send(handler, (json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n").encode(), status)


def _error(handler, reason, status=HTTPStatus.BAD_REQUEST, state="invalid"):
    _json(handler, {"status": state, "reason": reason, "learning_write_count": 0}, status)


def _same_origin(handler):
    port = int(handler.server.server_address[1])
    host = handler.headers.get("Host", "")
    try:
        parsed_host = urlsplit("http://" + host)
        if (parsed_host.hostname not in {"127.0.0.1", "localhost", "::1"}
                or parsed_host.port != port or parsed_host.username is not None
                or parsed_host.password is not None or parsed_host.path):
            return False
        origin = handler.headers.get("Origin")
        if origin:
            parsed = urlsplit(origin)
            if (parsed.scheme != "http" or parsed.hostname != parsed_host.hostname
                    or parsed.port != port or parsed.path not in {"", "/"}
                    or parsed.query or parsed.fragment or parsed.username is not None):
                return False
        return handler.headers.get("Sec-Fetch-Site", "") != "cross-site"
    except (ValueError, TypeError):
        return False


def _static(repo, relative):
    if relative not in PUBLIC_STATIC:
        raise FileNotFoundError("static_not_allowed")
    root = Path(repo).resolve() / "support/study-visuals-3d"
    path = root / relative
    if not path.is_file() or any(p.is_symlink() for p in (path, *path.parents)):
        raise FileNotFoundError("static_not_available")
    if not path.resolve().is_relative_to(root):
        raise ValueError("static_path_escape")
    data = path.read_bytes()
    csp = None
    if relative == "web/index.html":
        # Only the checked local importmap needs an inline script allowance.
        hashes = ["'sha256-" + base64.b64encode(hashlib.sha256(m).digest()).decode() + "'"
                  for m in re.findall(rb'<script\b[^>]*type=["\']importmap["\'][^>]*>(.*?)</script>', data, re.S)]
        csp = ("default-src 'self'; script-src 'self' " + " ".join(hashes)
               + "; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
               + "font-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'")
    return data, PUBLIC_STATIC[relative], csp


def _stage(params):
    if set(params) - {"stage", "hint_level", "key", "cache_key", "revision"} or any(len(v) != 1 for v in params.values()):
        raise ValueError("query_not_allowed")
    stage = params.get("stage", ["question"])[0]
    if stage not in {"question", "hint", "solution"}:
        raise ValueError("stage_invalid")
    raw_level = params.get("hint_level", ["1" if stage == "hint" else "0"])[0]
    if not re.fullmatch(r"[0-4]", raw_level) or (stage == "hint" and raw_level == "0") or (stage != "hint" and raw_level != "0"):
        raise ValueError("hint_level_invalid")
    key = params.get("key", params.get("cache_key", [None]))[0]
    if key is not None and not SHA.fullmatch(key):
        raise ValueError("cache_key_invalid")
    if "key" in params and "cache_key" in params and params["key"] != params["cache_key"]:
        raise ValueError("cache_key_conflict")
    revision = _revision(params)
    return stage, int(raw_level) if stage == "hint" else 1, key, revision


def _revision(params):
    values = params.get("revision")
    if values is None:
        return None
    if len(values) != 1 or not SHA.fullmatch(values[0]):
        raise ValueError("lesson_revision_invalid")
    return values[0]


def dispatch(handler, repo: Path) -> bool:
    """Serve only the new namespace; return False for all pre-existing routes."""
    parsed = urlsplit(handler.path)
    if parsed.path != PREFIX and not parsed.path.startswith(PREFIX + "/"):
        return False
    if not _same_origin(handler):
        _error(handler, "仅允许本机同源读取课件。", HTTPStatus.FORBIDDEN)
        return True
    path = unquote(parsed.path)
    if ("\\" in path or "\x00" in path or any(part in {".", ".."} for part in path.split("/"))
            or "%" in path):
        _error(handler, "资源路径无效。")
        return True
    try:
        if path.startswith(PREFIX + "/static/"):
            if parsed.query:
                raise ValueError("static_query_not_allowed")
            relative = path.removeprefix(PREFIX + "/static/")
            data, mime, csp = _static(repo, relative)
            _send(handler, data, content_type=mime, csp=csp)
            return True
        if path == PREFIX + "/api/index":
            if parsed.query:
                raise ValueError("index_query_not_allowed")
            _json(handler, _reader().list_scope(Path(repo)))
            return True
        route = None
        name = "preview.png"
        for stem, kind in (("/api/scene/", "scene"), ("/api/interactions/", "interactions"), ("/preview/", "asset"), ("/asset/", "asset")):
            if path.startswith(PREFIX + stem):
                route = kind
                tail = path.removeprefix(PREFIX + stem)
                if stem == "/asset/":
                    parts = tail.split("/")
                    if len(parts) != 2:
                        raise ValueError("asset_path_invalid")
                    identity, name = parts
                else:
                    identity = tail
                break
        if route:
            if not IDENTITY.fullmatch(identity) or not ASSET_NAME.fullmatch(name) or ".." in name:
                raise ValueError("identity_or_asset_invalid")
            stage, level, key, revision = _stage(parse_qs(parsed.query, keep_blank_values=True))
            if stage != "question" and handler.headers.get("X-Study-Visual-Intent") != stage:
                _error(handler, "请明确点击对应的提示或解析按钮后再读取该阶段。", HTTPStatus.FORBIDDEN, "stage_not_requested")
                return True
            if stage == "question" and route == "asset" and not (name == "preview.png" or re.fullmatch(r"original-question-\d+\.(?:png|webp|jpe?g)", name)):
                raise ValueError("question_asset_not_allowed")
            reader = _reader()
            pointer = reader.get_visual_pointer(Path(repo), identity, stage=stage, hint_level=level)
            status = pointer.get("status") if isinstance(pointer, dict) else "invalid"
            active_revision = pointer.get("lesson_revision") if isinstance(pointer, dict) else None
            if revision is not None and active_revision is not None and active_revision != revision:
                _error(handler, "课件整套版本已变化，请重新打开本题后再切换阶段。", HTTPStatus.CONFLICT, "stale")
                return True
            if status != "available":
                _error(handler, (pointer.get("reason") or "暂无匹配图形。") if isinstance(pointer, dict) else "缓存返回无效。",
                       STATUS_CODES.get(status, HTTPStatus.SERVICE_UNAVAILABLE), status or "missing")
                return True
            if pointer.get("question_id") != identity or pointer.get("stage") != stage:
                raise ValueError("cache_identity_mismatch")
            if not isinstance(active_revision, str) or not SHA.fullmatch(active_revision):
                _error(handler, "课件整套版本信息尚未就绪，请重新读取本题。", HTTPStatus.CONFLICT, "stale")
                return True
            if revision is not None and active_revision != revision:
                _error(handler, "课件整套版本已变化，请重新打开本题后再切换阶段。", HTTPStatus.CONFLICT, "stale")
                return True
            if key is not None and pointer.get("cache_key") != key:
                _error(handler, "图形版本已变化，请重新读取当前题目。", HTTPStatus.CONFLICT, "stale")
                return True
            if route == "interactions":
                from study_visual_interaction_408 import read as read_interactions
                value = read_interactions(Path(repo), identity, stage, level, expected_revision=active_revision)
                body = (json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
                mime = "application/json; charset=utf-8"
            elif route == "scene":
                value = reader.read_scene(Path(repo), identity, stage=stage, hint_level=level)
                if not isinstance(value, dict) or value.get("schema") != "study-visual-scene-v1" or value.get("question_id") != identity or value.get("stage") != stage:
                    raise ValueError("scene_identity_mismatch")
                envelope = {"scene": value, "lesson_revision": active_revision, "cache_key": pointer["cache_key"]}
                body = (json.dumps(envelope, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
                mime = "application/json; charset=utf-8"
            else:
                body, mime = reader.read_asset(Path(repo), identity, stage, name, hint_level=level)
                if not isinstance(body, bytes) or mime not in {"image/png", "image/jpeg", "image/webp"}:
                    raise ValueError("asset_type_invalid")
            after = reader.get_visual_pointer(Path(repo), identity, stage=stage, hint_level=level)
            if (after.get("status") != "available" or after.get("cache_key") != pointer.get("cache_key")
                    or after.get("lesson_revision") != active_revision):
                _error(handler, "课件整套版本正在更新，请重新打开本题。", HTTPStatus.CONFLICT, "stale")
                return True
            _send(handler, body, content_type=mime)
            return True
        tail = path.removeprefix(PREFIX).strip("/")
        if tail and ("/" in tail or not IDENTITY.fullmatch(tail)):
            raise FileNotFoundError("route_not_found")
        # A pinned viewer may keep its old URL while the data endpoints refuse
        # mixing a newly published lesson into that old page. Never preload a
        # requested solution from URL parameters; validate only the revision.
        _revision(parse_qs(parsed.query, keep_blank_values=True))
        # A viewer URL never carries solution bytes. The player must explicitly
        # request its independent stage endpoint after the user clicks.
        data, mime, csp = _static(repo, "web/index.html")
        # A presentation-only shell is independent of the immutable scene and
        # screenshot runtime. It never fetches another stage or changes a model.
        data = data.replace(b"</head>", b'<script defer src="/study-visuals/static/web/tablet-view.js"></script>\n</head>', 1)
        _send(handler, data, content_type=mime, csp=csp)
    except FileNotFoundError:
        _error(handler, "未找到已发布的课件资源。", HTTPStatus.NOT_FOUND, "missing")
    except (ValueError, TypeError, KeyError):
        _error(handler, "课件请求或缓存校验未通过，原题仍可使用。", HTTPStatus.BAD_REQUEST)
    except (ImportError, OSError, RuntimeError, AttributeError):
        _error(handler, "课件暂不可读取，原题仍可使用。", HTTPStatus.SERVICE_UNAVAILABLE, "missing")
    return True
