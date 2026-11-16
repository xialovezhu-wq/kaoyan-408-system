"""Optional local discovery backends; authoritative evidence stays in native 408.

No backend is auto-installed, auto-indexed, or chosen here.  A failed local
backend is explicit and never triggers a cloud API or a formal write.
"""
from __future__ import annotations

import json
import hashlib
import os
import plistlib
import re
import subprocess
import tempfile
import unicodedata
from pathlib import Path
from typing import Any, Sequence


def projection_hash(documents: Sequence[dict[str, Any]]) -> str:
    def norm(value):
        return " ".join(unicodedata.normalize("NFKC", str(value)).casefold().split())
    projected = [{"id": d["id"], "label": norm(d.get("label", d["id"])),
                  "aliases": sorted({norm(x) for x in d.get("aliases", [])})}
                 for d in documents if re.fullmatch(r"(?:DS|CO|OS|CN)\d{2}-\d{2}", str(d.get("id", "")))]
    raw = json.dumps(sorted(projected, key=lambda d: d["id"]), sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def local_environment(runtime: Path, corpus: Path, *, offline=False) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items()
           if not any(token in k.upper() for token in ("API_KEY", "TOKEN", "SECRET", "PASSWORD"))}
    env.update(BASIC_MEMORY_CONFIG_DIR=str(runtime / "bm-config"),
               BASIC_MEMORY_HOME=str(corpus), BASIC_MEMORY_FORCE_LOCAL="true",
               HF_HOME=str(runtime / "hf-cache"), FASTEMBED_CACHE_PATH=str(runtime / "fastembed-cache"),
               HF_HUB_DISABLE_TELEMETRY="1", BASIC_MEMORY_SEMANTIC_EMBEDDING_PROVIDER="fastembed",
               BASIC_MEMORY_RERANKER_ENABLED="false", BASIC_MEMORY_LOGFIRE_ENABLED="false")
    if offline:
        env["HF_HUB_OFFLINE"] = "1"
    return env


def json_from_output(text: str) -> Any:
    """CLI wrappers can emit non-JSON progress before the structured result."""
    decoder = json.JSONDecoder()
    for pos, ch in enumerate(text):
        if ch in "[{":
            try:
                value, end = decoder.raw_decode(text[pos:])
                if not text[pos + end:].strip():
                    return value
            except json.JSONDecodeError:
                continue
    raise ValueError("backend_output_not_json")


def candidate_ids(value: Any) -> list[dict[str, Any]]:
    """Read only result-item identity fields, never trust snippet instructions."""
    if isinstance(value, dict):
        for key in ("results", "items", "notes", "matches"):
            if isinstance(value.get(key), list):
                value = value[key]
                break
        else:
            for key in ("data", "result"):
                if isinstance(value.get(key), (dict, list)):
                    return candidate_ids(value[key])
            return []
    if not isinstance(value, list):
        return []
    results = []
    for row in value:
        if not isinstance(row, dict):
            continue
        for field in ("file", "file_path", "path", "permalink", "title", "entity_permalink", "identifier", "id"):
            text = str(row.get(field) or "")
            match = re.search(r"(?:^|[/\s])((?:DS|CO|OS|CN)\d{2}-\d{2})(?:\.md|$|\s)", text, re.I)
            if match:
                results.append({"id": match[1].upper(), "score": row.get("score")})
                break
    return results


def search_candidates(query: str, documents: Sequence[dict[str, Any]], *, backend: str,
                      limit: int = 8, runtime_root: str | Path | None = None,
                      trial: bool = False) -> dict[str, Any]:
    if backend not in {"qmd", "basic_memory"}:
        raise ValueError("unsupported_local_memory_backend")
    if not isinstance(query, str) or not query.strip() or len(query) > 1000 or "\x00" in query:
        raise ValueError("invalid_memory_search_query")
    if type(limit) is not int or not 1 <= limit <= 8:
        raise ValueError("invalid_memory_search_limit")
    allowed = {str(d["id"]): d for d in documents if isinstance(d, dict) and isinstance(d.get("id"), str)}
    runtime = Path(runtime_root or Path(__file__).resolve().parents[1] / ".private/memory-backends-v1")
    base = {"backend": backend, "api_request_count": 0, "formal_write_count": 0, "candidates": []}
    config_path = runtime / "backend-config.json"
    if not config_path.is_file():
        return {**base, "status": "unavailable", "degraded": "backend_not_prepared"}
    try:
        cfg = json.loads(config_path.read_text()).get(backend) or {}
    except (OSError, ValueError):
        return {**base, "status": "unavailable", "degraded": "backend_config_unreadable"}
    if cfg.get("trial_only", True) and trial is not True:
        return {**base, "status": "unavailable", "degraded": "backend_not_qualified_trial_only"}
    corpus = Path(cfg.get("corpus", ""))
    if cfg.get("status") != "ready" or not corpus.is_absolute() or not corpus.is_dir():
        return {**base, "status": "unavailable", "degraded": "backend_index_not_ready"}
    index_path = runtime / ("qmd-index.sqlite" if backend == "qmd" else "bm-config/memory.db")
    if not index_path.is_file():
        return {**base, "status": "unavailable", "degraded": "backend_index_missing"}
    indexed_ids = set(cfg.get("candidate_ids") or [])
    requested_points = {identity for identity in allowed if re.fullmatch(r"(?:DS|CO|OS|CN)\d{2}-\d{2}", identity)}
    # Both engines currently expose a dedicated type=note collection containing
    # these exact parent points. We do not claim pre-filtering of narrower scopes.
    if not indexed_ids or requested_points != indexed_ids:
        return {**base, "status": "unavailable", "degraded": "scope_unsupported"}
    if projection_hash([allowed[identity] for identity in sorted(requested_points)]) != cfg.get("projection_sha256"):
        return {**base, "status": "unavailable", "degraded": "backend_projection_stale"}
    if corpus.is_relative_to(Path("/Volumes/T9-Data")):
        try:
            volume = plistlib.loads(subprocess.check_output(
                ["/usr/sbin/diskutil", "info", "-plist", "/Volumes/T9-Data"], timeout=10))
            if volume.get("VolumeUUID") != "00000000-0000-0000-0000-000000000000":
                raise ValueError("wrong_volume")
        except (OSError, ValueError, subprocess.SubprocessError):
            return {**base, "status": "unavailable", "degraded": "backend_cold_corpus_volume_unverified"}
    if not allowed:
        return {**base, "status": "ready"}
    env = local_environment(runtime, corpus, offline=True)
    try:
        if backend == "qmd":
            # Runtime scratch is a bounded query handoff, not a learning record.
            scratch = runtime / "query-handoffs"
            scratch.mkdir(exist_ok=True)
            with tempfile.NamedTemporaryFile(mode="w", suffix=".json", dir=scratch, delete=False) as f:
                json.dump([{"id": "query", "query": query}], f, ensure_ascii=False)
                request_path = Path(f.name)
            try:
                result = subprocess.run([cfg.get("node", "node"), "--use-env-proxy",
                    str(Path(__file__).with_name("memory_backend_qmd.mjs")), "search", str(runtime),
                    str(runtime / "qmd-index.sqlite"), str(corpus), str(request_path)],
                    env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=30)
            finally:
                request_path.unlink(missing_ok=True)
            if result.returncode:
                raise ValueError("qmd_query_failed")
            payload = json_from_output(result.stdout)
            candidates = payload["rows"][0]["candidates"]
        else:
            result = subprocess.run([str(runtime / "basic-memory/bin/bm"), "tool", "search", query,
                "--mode", "hybrid", "--project", cfg.get("project", "cs408-benchmark"),
                "--note-types", "note", "--items", "note", "--page-size", "8", "--compact", "--json", "--local"],
                env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=30)
            if result.returncode:
                raise ValueError("basic_memory_query_failed")
            candidates = candidate_ids(json_from_output(result.stdout))
        selected, seen = [], set()
        for row in candidates:
            identity = row.get("id")
            if identity not in allowed or identity in seen:
                continue
            seen.add(identity)
            selected.append({"id": identity, "score": row.get("score")})
        return {**base, "status": "ready", "candidates": selected[:limit], "backend_content_discarded": True,
                "indexed_source_version": cfg.get("source_version"), "index_version_is_not_current_evidence_authority": True}
    except (OSError, ValueError, KeyError, IndexError, subprocess.TimeoutExpired) as exc:
        return {**base, "status": "unavailable", "degraded": type(exc).__name__ + ":local_backend_failed"}
