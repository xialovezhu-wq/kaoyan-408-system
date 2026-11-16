"""Narrow 408 boundary for the existing formal/review owners and shared publisher.

The descriptor reads actual source identities, never Git's tracked-file set.
Cloud output is proposal/input evidence; this module never treats a worksheet as
a learner answer or a model summary as an independently verified local decision.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import fcntl
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Any, Iterator

from study_commit_followup_408 import study_commit_boundary

import runtime_paths_408 as _paths

REPO = Path(__file__).resolve().parents[1]
DETAILS = _paths.details_root()
PACKAGES = _paths.package_root()
ARCHIVE = Path("/Volumes/T9-Data")
EXAM_SOURCE_ROOT = _paths.source_root()
PUBLICATION_ROOT = _paths.bridge_root() / 'publication/408'
FORMAL_RE = re.compile(r"(?:DS|CO|OS|CN)_(?:\d{4}|UNK)_\d{3}")
TEXT_FILES = {
    "节点总表.md", "关系边表.md", "关系规则.md", "年份索引.md", "概念词典.md",
    "知识点命中索引.md", "知识点标签表.md", "错因标签表.md", "错题日期索引.md",
    "错题复做记录.md", "复习单元总表.md", "复习单元节点映射.md",
    "原题复做轨总表.md", "复习单元迁移记录.md", "复盘调度规则.md", "index.md",
}
FORMAL_DIRS = ("节点库", "知识体系", "专题链", "复习单元卡", "历史错题归档",
               "历史复习单元归档", "复盘输出", "wiki/sources/raw_archives",
               "wiki/study_vaults/408-full/input")
STATE_FILES = (
    "review-loop/events.jsonl", "intake-curation/events.jsonl",
    "review-campaign/formal-admission/events.jsonl",
)
FORBIDDEN_PARTS = {".git", ".obsidian", "__pycache__", "node_modules", "tmp", "backups"}
FORBIDDEN_SUFFIXES = {".key", ".pem", ".p12", ".lock", ".sqlite", ".sqlite3", ".db", ".pyc"}
MACHINE_FILENAMES = {".DS_Store", "Thumbs.db", "desktop.ini"}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
# Only these release artifacts are answer-safe. ``canonical-exposure.jsonl``
# and ``source-reconciliation.jsonl`` carry solution OCR text and must never be
# published; ``style-releases-private`` is excluded by name as well.
VARIANT_STYLE_RELEASE_FILES = ("style-guide.md", "style-profiles.jsonl")


class BridgeError(ValueError):
    pass


def _json(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise BridgeError("missing_or_unsafe_json:" + path.name)
    value = json.loads(path.read_bytes())
    if not isinstance(value, dict):
        raise BridgeError("json_object_required")
    return value


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode()).hexdigest()


def _relative(raw: str) -> Path:
    path = Path(raw)
    if path.is_absolute() or not path.parts or ".." in path.parts:
        raise BridgeError("unsafe_relative_path")
    return path


def _store(repo: Path, group: str, value: dict[str, Any]) -> tuple[Path, str]:
    """A content-addressed handoff receipt, never a second learning ledger."""
    from exam_tutoring_context_408 import _atomic_write, _canonical_bytes
    repo = repo.expanduser().resolve()
    raw = _canonical_bytes(value)
    digest = hashlib.sha256(raw).hexdigest()
    path = repo / ".private/study-bridge" / group / (digest + ".json")
    if path.is_symlink() or any(parent.is_symlink() for parent in path.parents):
        raise BridgeError("handoff_receipt_path_unsafe")
    if path.exists() and path.read_bytes() != raw:
        raise BridgeError("handoff_receipt_conflict")
    if not path.exists():
        _atomic_write(path, raw, mode=0o600)
    return path, digest


def _prune_export_generations(root: Path, *, keep: int = 3) -> int:
    """Keep only the newest exported personalization generations.

    Each export is a derived copy of the native manifest; an older generation
    is only an input to an already finished publication and is rebuilt on
    demand. Keeping every generation was the main source of the local 2.9 GB.
    """
    if not root.is_dir() or root.is_symlink():
        return 0
    entries = []
    for path in root.iterdir():
        if path.is_dir() and not path.is_symlink():
            try:
                entries.append((path.stat().st_mtime, path))
            except OSError:
                continue
    entries.sort(reverse=True)
    removed = 0
    for _mtime, path in entries[keep:]:
        shutil.rmtree(path, ignore_errors=True)
        removed += 1
    return removed


def _quarantined_assets(details: Path) -> dict[str, set[str]]:
    """Node -> asset filenames whose question/solution binding is known bad.

    The detail vault keeps these in dated quarantine CSVs. A quarantined asset
    must never be published as a normal question or solution image, because the
    binding itself is what is under review.
    """
    result: dict[str, set[str]] = {}
    for path in sorted((details / "indexes").glob("wrong-question-binding-quarantine-*.csv")):
        with path.open(encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                node = (row.get("node_id") or "").strip()
                raw = (row.get("quarantined_assets") or row.get("moved_active_png") or "").strip()
                if not node:
                    continue
                names = {part.strip() for part in re.split(r"[;、,]", raw) if part.strip()}
                result.setdefault(node, set()).update(names)
    return result


def validate_package(package_root: Path, *, repo: Path = REPO,
                     expected_sha256: str | None = None) -> dict[str, Any]:
    import conversation_package_408 as packages
    path = Path(package_root).expanduser().resolve(strict=True)
    package_id = path.name
    if not packages.SAFE_ID_RE.fullmatch(package_id) or not package_id.startswith("CS408-"):
        raise BridgeError("exact_current_package_directory_required")
    pointer_path = path / "archive-pointer.json"
    if pointer_path.is_file():
        import archive_408_conversation_package as archive
        pointer = _json(pointer_path)
        verified = archive.reopen_archived_package(
            repo=repo, package_id=package_id,
            requested_relative_paths=["conversation.json", "source.json"],
        )
        if pointer.get("package_id") != package_id or pointer.get("package_sha256") != verified["package_sha256"]:
            raise BridgeError("local_archive_pointer_binding_mismatch")
        path = archive.ARCHIVE_ROOT / _relative(pointer["archive_relative_path"])
        manifest = _json(path / "manifest.json")
        objects = {row["relative_path"]: row["json"] for row in verified["requested_objects"]}
        conversation = objects["conversation.json"]
        origin = "verified_archive"
    else:
        opened = packages.verify_package_binding(
            packages.LOCATOR_PREFIX + package_id,
            expected_canonical_sha256=expected_sha256, require_sealed=True,
            private_root=path.parent.parent,
        )
        manifest = opened["manifest"]
        conversation = opened["conversation"]
        origin = "verified_local"
    canonical = manifest.get("canonical_sha256")
    if expected_sha256 and canonical != expected_sha256:
        raise BridgeError("package_expected_sha256_mismatch")
    return {"status": "verified", "subject": "408", "package_id": package_id,
            "package_sha256": canonical, "actual_root": str(path),
            "manifest_sha256": _sha(path / "manifest.json"), "manifest": manifest,
            "conversation_complete": conversation.get("complete_order_preserved") is True and conversation.get("truncated") is False,
            "turn_count": len(conversation.get("turns") or []),
            "verification_origin": origin, "formal_write_count": 0}


def question_source(formal_id: str, *, repo: Path = REPO,
                    details_root: Path = DETAILS) -> dict[str, Any]:
    """Resolve one known question without searching planning/history or exposing a key."""
    if not FORMAL_RE.fullmatch(formal_id) or not (repo / (formal_id + ".md")).is_file():
        raise BridgeError("known_formal_question_required")
    index = details_root / "corpus/408统考真题/indexes/question-index.csv"
    matches = []
    if index.is_file():
        with index.open(encoding="utf-8-sig", newline="") as handle:
            matches = [row for row in csv.DictReader(handle)
                       if formal_id in FORMAL_RE.findall(row.get("formal_node_ids", ""))]
    public_text = None
    if len(matches) == 1:
        row = matches[0]
        source_id = row["exam_question_id"]
        identity = re.fullmatch(r"EXAM408-(20\d{2})-Q(\d{2})", source_id)
        if not identity:
            raise BridgeError("canonical_question_identity_invalid")
        label = f"真题｜{identity.group(1)} 年考研 408 第 {int(identity.group(2))} 题"
        refs = json.loads(row["question_assets"])
        source_proof = {"index_path": str(index), "question_binding_sha256": _digest({
            key: row.get(key) for key in ("exam_question_id", "formal_node_ids", "question_assets", "question_source_sha256")})}
    elif len(matches) > 1:
        raise BridgeError("formal_question_has_ambiguous_canonical_sources")
    else:
        card = details_root / "cards" / (formal_id + ".md")
        text = card.read_text(encoding="utf-8")
        match = re.search(r"(?m)^>\s*\[!question\][^\n]*\n", text)
        if not match:
            raise BridgeError("exact_public_question_section_unavailable")
        section = re.split(r"(?m)^>\s*\[!(?:answer|solution|tip|note)\]|^##\s", text[match.end():], maxsplit=1)[0]
        refs = re.findall(r"!\[\[([^\]|]+)(?:\|[^\]]+)?\]\]", section)
        refs += re.findall(r"!\[[^\]]*\]\(([^)]+)\)", section)
        public_text = section.strip() if not refs else None
        source_id = None
        label = "来源原题｜非系统生成变式｜真题年份未确认"
        source_proof = {"details_card_path": str(card), "card_sha256": _sha(card)}
    assets = []
    for raw in refs:
        path = details_root / _relative(raw)
        if not path.is_file() or path.is_symlink():
            raise BridgeError("question_asset_unavailable")
        path.resolve().relative_to(details_root.resolve())
        with path.open("rb") as handle:
            magic = handle.read(12)
        if not (magic.startswith(b"\x89PNG\r\n\x1a\n") or magic.startswith(b"\xff\xd8\xff")
                or magic.startswith((b"GIF87a", b"GIF89a")) or (magic[:4] == b"RIFF" and magic[8:12] == b"WEBP")):
            raise BridgeError("question_asset_format_invalid")
        assets.append({"path": str(path.resolve()), "sha256": _sha(path),
                       "size_bytes": path.stat().st_size, "role": "question_image"})
    return {"status": "source_bound", "formal_id": formal_id, "source_id": source_id,
            "question_provenance": {"label": label}, "question_assets": assets,
            "public_question_text": public_text, "source_proof": source_proof,
            "answer_material_returned": False, "display_recorded": False,
            "formal_write_count": 0, "learner_evidence_write_count": 0}


def seal_a(body: dict[str, Any], local: dict[str, Any], *, repo: Path = REPO,
           package_root: Path = PACKAGES, decision_root: Path | None = None) -> dict[str, Any]:
    import sol_curation_decision_408 as decision
    if body.get("subject") != "408" or body.get("project") != "A":
        raise BridgeError("project_A_408_input_required")
    required = ("task_id", "batch_id", "capture_id", "evidence_locator", "evidence_sha256", "proposal")
    if any(key not in body for key in required) or not (body.get("library_version") or body.get("source_commit")):
        raise BridgeError("A_source_and_frozen_capture_binding_required")
    proposal_sha = _digest(body["proposal"])
    if (local.get("reviewed_proposal_sha256") != proposal_sha or not isinstance(local.get("decision"), dict)
            or local.get("advice_adjudication") not in {"accepted", "modified", "rejected"}):
        raise BridgeError("independent_local_decision_file_required")
    sealed = decision.seal_decision(
        repo=repo, batch_id=body["batch_id"], capture_id=body["capture_id"],
        evidence_locator=body["evidence_locator"], evidence_sha256=body["evidence_sha256"],
        decision=local["decision"], package_root=package_root, decision_root=decision_root,
    )
    binding = {"schema": "cs408-A-local-decision-binding-v1", "subject": "408",
               "task_id": body["task_id"], "batch_id": body["batch_id"], "capture_id": body["capture_id"],
               "source_commit": body.get("source_commit"), "library_version": body.get("library_version"),
               "proposal_sha256": proposal_sha, "input_sha256": _digest(body),
               "advice_adjudication": local["advice_adjudication"],
               "evidence_locator": body["evidence_locator"], "evidence_sha256": body["evidence_sha256"],
               "local_decision_file_sha256": _digest(local), "decision_locator": sealed["locator"],
               "decision_sha256": sealed["sha256"], "formal_write_count": 0}
    path, digest = _store(repo, "a-bindings", binding)
    return {**sealed, "a_binding_path": str(path), "a_binding_sha256": digest,
            "formal_complete": False, "next_owner": "kaoyan-408-daily-intake-curation"}


def verify_a(body: dict[str, Any], *, repo: Path = REPO,
             decision_root: Path | None = None, formal_runtime_root: Path | None = None) -> dict[str, Any]:
    import sol_curation_decision_408 as decision
    import formal_intake_formal_proof_408 as proof
    path = Path(body["a_binding_path"]).expanduser().resolve()
    path.relative_to((repo / ".private/study-bridge/a-bindings").resolve())
    if _sha(path) != body["a_binding_sha256"]:
        raise BridgeError("A_binding_hash_mismatch")
    binding = _json(path)
    sealed = decision.read_sealed_decision(binding["decision_locator"], binding["decision_sha256"], decision_root=decision_root)
    terminal = decision.read_terminal_binding(body["terminal_locator"], body["terminal_sha256"], decision_root=decision_root, repo=repo)
    if terminal.get("decision_locator") != binding["decision_locator"] or terminal.get("capture_id") != binding["capture_id"]:
        raise BridgeError("A_terminal_belongs_to_other_decision")
    if sealed.get("evidence_locator") != binding["evidence_locator"]:
        raise BridgeError("A_evidence_binding_drift")
    outcome = terminal.get("outcome")
    if outcome not in {"curated", "created", "updated", "already_current"}:
        raise BridgeError("A_has_no_formal_success_proof")
    verified = proof.verify_formal_proof(
        repo, batch_id=binding["batch_id"], capture_id=binding["capture_id"],
        formal_id=terminal["formal_id"], formal_result_status="already_current" if outcome == "already_current" else "curated",
        formal_result_receipt_sha256=terminal.get("verification_sha256") or terminal["normal_receipt_sha256"],
        runtime_root=formal_runtime_root or proof.DEFAULT_RUNTIME_ROOT,
    )
    return {"status": "verified", "subject": "408", "task_id": binding["task_id"],
            "capture_id": binding["capture_id"], "evidence_locator": binding["evidence_locator"],
            "evidence_sha256": binding["evidence_sha256"], "formal_id": verified.formal_id,
            "formal_receipt_sha256": verified.sha256, "transaction_id": verified.transaction_id,
            "source_commit": binding.get("source_commit"), "library_version": binding.get("library_version"),
            "proposal_sha256": binding["proposal_sha256"],
            "advice_adjudication": binding.get("advice_adjudication"),
            "advice_adjudication_bound": binding.get("advice_adjudication") in {"accepted", "modified", "rejected"},
            "decision": sealed["decision"]["decision"], "outcome": outcome,
            "package_id": binding["evidence_locator"].removeprefix("cs408-conversation-package://")
                if binding["evidence_locator"].startswith("cs408-conversation-package://") else None,
            "publication_event_id": terminal.get("publication_event_id"),
            "terminal_sha256": body["terminal_sha256"], "formal_complete": True,
            "publication_required": True, "formal_write_count": 0}


def _concepts(row: dict[str, Any]) -> list[str]:
    return list(dict.fromkeys(re.findall(r"(?:DS|CO|OS|CN)\d{2}-\d{2}", str(row.get("主知识点") or ""))))


def canonical_formal_ids(master, canonical_rows):
    """Join the accepted exam catalog to formal nodes by explicit source identity."""
    source_to_nodes = {}
    for fid, master_row in master.items():
        identity = str(master_row.get("来源ID") or "").strip()
        if identity and identity != "VISUAL_PENDING":
            source_to_nodes.setdefault(identity, set()).add(fid)
    result = {}
    for row in canonical_rows:
        identity = row["exam_question_id"]
        ids = set(FORMAL_RE.findall(row.get("formal_node_ids", "")))
        ids |= source_to_nodes.get(identity, set())
        try:
            for alias in json.loads(row.get("legacy_corpus_ids") or "[]"):
                ids |= source_to_nodes.get(str(alias), set())
        except (TypeError, ValueError):
            pass
        result[identity] = sorted(ids)
    return result


def due(*, repo: Path = REPO, target_date: str, config_path: Path | None = None) -> dict[str, Any]:
    import morning_coverage_campaign_408 as scheduler
    from intake_lib_408 import Repo
    day = scheduler.parse_date(target_date)
    with _source_locks(repo):
        config, cohort, memory, bindings, _ = scheduler.load_projection(repo, config_path, day)
        run = scheduler.build_due_run(cohort, memory, config, day, source_bindings=bindings)
        master = Repo(repo).master()
    from concept_review_priority_408 import due_context
    return due_context(run, master)


def load_imported_d_receipt(repo: Path, target_date: str) -> dict[str, Any] | None:
    """Verify the immutable imported plan, independently of later scheduling writes."""
    repo = Path(repo)
    pointer = repo / ".private/study-bridge/d-current" / (target_date + ".json")
    if not pointer.is_file():
        return None
    ref = _json(pointer)
    path = Path(ref["receipt_path"])
    path.resolve().relative_to((repo / ".private/study-bridge/d-imports").resolve())
    if _sha(path) != ref["receipt_sha256"]:
        raise BridgeError("imported_D_receipt_hash_mismatch")
    receipt = _json(path)
    if receipt.get("target_date") != target_date:
        raise BridgeError("imported_D_target_date_mismatch")
    if receipt.get("local_scope_ref"):
        from d_review_scope_408 import load_scope
        load_scope(repo, receipt["local_scope_ref"], receipt,
                   receipt.get("review_concept_ids", receipt["due_concept_ids"]))
    output_root = Path(receipt["output_root"])
    for row in receipt["output_files"]:
        if _d_output_file(output_root, row["relative_path"]) != row:
            raise BridgeError("imported_D_output_file_drift")
    return receipt


def load_imported_d_run(repo: Path, target_date: str, *, _current_context=None) -> dict[str, Any] | None:
    receipt = load_imported_d_receipt(repo, target_date)
    if receipt is None:
        return None
    # Only native callers may pass the projection they just computed in this
    # invocation. This is not a persisted cache or a caller-provided CLI field.
    current = _current_context if _current_context is not None else due(repo=repo, target_date=target_date)
    if receipt["due_version"] != current["due_version"]:
        raise BridgeError("imported_D_due_version_stale")
    if receipt.get("local_scope_ref"):
        from d_review_scope_408 import load_scope
        load_scope(repo, receipt["local_scope_ref"], receipt,
                   current.get("review_concept_ids", current["due_concept_ids"]))
    return receipt["execution_due_run"]


def _mapping_cache_value(value: dict[str, Any], source_commit: str,
                         receipts: list[dict[str, Any]]) -> dict[str, Any]:
    """Verify cached original bytes against the publisher, not a cache checksum."""
    if (value.get("schema") != "cs408-published-mappings-cache-v1"
            or value.get("source_commit") != source_commit):
        raise BridgeError("published_mapping_cache_identity_drift")
    raw = value["manifest_text"].encode("utf-8")
    digest = hashlib.sha256(raw).hexdigest()
    matching = [row for row in receipts if row.get("manifest_sha256") == digest]
    if not matching:
        raise BridgeError("published_manifest_receipt_binding_drift")
    manifest = json.loads(raw)
    if (manifest.get("subject") != "408" or not any(
            manifest.get("local_version") == row.get("local_version")
            and manifest.get("repository") == row.get("repository") for row in matching)):
        raise BridgeError("published_mapping_manifest_identity_drift")
    descriptors = manifest.get("original_mapping_inventory") or []
    chunks = value["mapping_shards"]
    if len(descriptors) != len(chunks):
        raise BridgeError("published_mapping_inventory_count_drift")
    mappings = []
    for descriptor, chunk in zip(descriptors, chunks):
        data = chunk["text"].encode("utf-8")
        if (chunk["path"] != descriptor["path"]
                or hashlib.sha256(data).hexdigest() != descriptor["sha256"]):
            raise BridgeError("published_mapping_inventory_binding_drift")
        rows = [json.loads(line) for line in data.splitlines() if line.strip()]
        if len(rows) != descriptor["count"] or any(not isinstance(row, dict) for row in rows):
            raise BridgeError("published_mapping_inventory_count_drift")
        mappings.extend(rows)
    if not descriptors:
        mappings = manifest.get("original_mappings", [])
    return {**manifest, "original_mappings": mappings, "_verified_publication": {
        "source_commit": source_commit, "manifest_sha256": digest}}


def _published_mappings(source_commit: str, repo: Path,
                        receipts: list[dict[str, Any]]) -> dict[str, Any]:
    """Load only source mappings; file inventories are unrelated to this query.

    The optional disk cache contains original manifest/shard bytes. Every read
    rechecks the current publication receipt and all referenced mapping hashes.
    Live Capture/exposure/redo records are deliberately not cached here.
    """
    receipts = [row for row in receipts if row.get("branch") == "main"]
    if not receipts:
        raise BridgeError("D_requires_verified_main_publication")
    repo = Path(repo).resolve()
    cache = repo / ".private/study-bridge/published-mapping-cache" / (source_commit + ".json")
    safe_cache = not cache.is_symlink() and not any(p.is_symlink() for p in cache.parents)
    if safe_cache and cache.is_file():
        try:
            return _mapping_cache_value(_json(cache), source_commit, receipts)
        except (ValueError, OSError, KeyError, TypeError, UnicodeError, AttributeError):
            # Derived cache damage is recoverable only by rereading pinned data.
            pass
    bridge_root = PUBLICATION_ROOT.parent.parent
    if str(bridge_root) not in sys.path:
        sys.path.insert(0, str(bridge_root))
    from study_bridge import Published
    publisher = Published(PUBLICATION_ROOT.parent)
    receipt = receipts[0]
    with tempfile.TemporaryDirectory(prefix="cs408-published-mappings-") as temporary:
        root = Path(temporary)
        manifest_file = publisher.materialize("408", source_commit, "PUBLISHED_LIBRARY.json",
                                              root / "manifest.json", receipt["manifest_sha256"])
        manifest_text = manifest_file.read_bytes().decode("utf-8")
        manifest = json.loads(manifest_text)
        chunks = []
        for index, shard in enumerate(manifest.get("original_mapping_inventory") or []):
            path = publisher.materialize("408", source_commit, shard["path"],
                                         root / f"mapping-{index}.jsonl", shard["sha256"])
            chunks.append({"path": shard["path"], "text": path.read_bytes().decode("utf-8")})
    value = {"schema": "cs408-published-mappings-cache-v1", "source_commit": source_commit,
             "manifest_text": manifest_text, "mapping_shards": chunks}
    result = _mapping_cache_value(value, source_commit, receipts)
    if safe_cache:
        from exam_tutoring_context_408 import _atomic_write
        try:
            _atomic_write(cache, json.dumps(value, ensure_ascii=False).encode("utf-8"), mode=0o600)
        except OSError:
            pass  # Caching is optional; the pinned source has already passed verification.
    return result


def _published_library(source_commit: str, repo: Path,
                       fixture_manifest: str | None = None, *,
                       original_mappings_only: bool = False) -> dict[str, Any]:
    """Reuse publisher-verified local material without another network request."""
    if repo.resolve() != REPO.resolve() and fixture_manifest:
        result = _json(Path(fixture_manifest))
        if result.get("subject") != "408":
            raise BridgeError("fixture_publication_subject_mismatch")
        result["_verified_publication"] = {
            "source_commit": source_commit,
            "manifest_sha256": _sha(Path(fixture_manifest)),
        }
        return result
    if fixture_manifest:
        raise BridgeError("production_publication_path_is_not_caller_selected")
    if not re.fullmatch(r"[0-9a-f]{40}", source_commit):
        raise BridgeError("invalid_published_source_commit")
    receipts = []
    for path in (PUBLICATION_ROOT / "receipts").glob("*.json"):
        value = _json(path)
        if value.get("status") == "PUBLISHED" and value.get("source_commit") == source_commit and value.get("subject") == "408":
            receipts.append(value)
    if not receipts:
        raise BridgeError("D_requires_verified_published_source_commit")
    if original_mappings_only:
        return _published_mappings(source_commit, repo, receipts)
    raw = PUBLICATION_ROOT / "verified-manifests" / (source_commit + ".json")
    resolved = PUBLICATION_ROOT / "verified-manifests" / (source_commit + ".resolved.json")
    if not raw.is_file() or not resolved.is_file():
        # Local publications may not have the optional expanded disk cache.
        # The shared reader checks the receipt, pinned root and every inventory
        # shard directly; never substitute HEAD or an unverified current tree.
        bridge_root = PUBLICATION_ROOT.parent.parent
        if str(bridge_root) not in sys.path:
            sys.path.insert(0, str(bridge_root))
        from study_bridge import Published
        receipt, result = Published(PUBLICATION_ROOT.parent).inspect_version("408", source_commit)
        result["_verified_publication"] = {
            "source_commit": source_commit,
            "manifest_sha256": receipt["manifest_sha256"],
        }
        return result
    if _sha(raw) not in {row.get("manifest_sha256") for row in receipts}:
        raise BridgeError("published_manifest_receipt_binding_drift")
    original, result = _json(raw), _json(resolved)
    if result.get("subject") != "408" or result.get("local_version") != original.get("local_version"):
        raise BridgeError("published_resolved_manifest_identity_drift")
    # Expanded inventories must retain each original publisher shard's exact
    # row bytes, rather than trusting a mutable resolved index by its own hash.
    for inventory, field in (("file_inventory", "files"), ("original_mapping_inventory", "original_mappings")):
        offset = 0
        rows = result.get(field, [])
        for shard in original.get(inventory, []):
            chunk = rows[offset:offset + shard["count"]]
            raw_rows = b"".join((json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n").encode() for row in chunk)
            if len(chunk) != shard["count"] or hashlib.sha256(raw_rows).hexdigest() != shard["sha256"]:
                raise BridgeError("published_inventory_binding_drift")
            offset += shard["count"]
        if original.get(inventory) and offset != len(rows):
            raise BridgeError("published_inventory_count_drift")
    result["_verified_publication"] = {
        "source_commit": source_commit,
        "manifest_sha256": _sha(raw),
    }
    return result


def _d_output_file(root: Path, raw: Any) -> dict[str, Any]:
    if not isinstance(raw, str):
        raise BridgeError("D_output_relative_file_required")
    relative = _relative(raw)
    path = root / relative
    if path.is_symlink() or any(p.is_symlink() for p in path.parents):
        raise BridgeError("D_output_symlink_rejected")
    path.resolve().relative_to(root.resolve())
    if not path.is_file():
        raise BridgeError("D_output_file_missing:" + relative.as_posix())
    return {"relative_path": relative.as_posix(), "sha256": _sha(path), "byte_count": path.stat().st_size}


def import_d(body: dict[str, Any], *, repo: Path = REPO, details_root: Path = DETAILS,
             native_sources=None, hot_capture_proof=None) -> dict[str, Any]:
    """Admit a concept-deduplicated view; unanswered node evidence stays due."""
    import morning_coverage_campaign_408 as scheduler
    from intake_lib_408 import Repo
    prior_result = None
    if body.get("subject") != "408" or body.get("project") != "D":
        raise BridgeError("project_D_408_input_required")
    from native_morning_sources_408 import SOURCE_BASIS
    native = body.get("source_basis") == SOURCE_BASIS
    if native != (native_sources is not None) or body.get("source_basis") not in (None, SOURCE_BASIS):
        raise BridgeError("D_native_source_requires_local_prepare")
    if not re.fullmatch(r"[0-9a-f]{64}" if native else r"[0-9a-f]{40}", str(body.get("source_commit") or "")) or not body.get("task_id"):
        raise BridgeError("D_source_commit_and_task_required")
    target_date = scheduler.parse_date(body["target_date"]).isoformat()
    contact_options = {}
    if native:
        if (native_sources.repo != Path(repo).resolve() or native_sources.details != Path(details_root).resolve()
                or native_sources.version != body["source_commit"]
                or str(native_sources.release_dir) != body.get("native_release_dir")
                or hot_capture_proof is None or body.get("web_variants") or body.get("fixture_publication_manifest")
                or not body.get("selection_input_version")):
            raise BridgeError("D_native_source_binding_invalid")
        contact_options["hot_capture_proof"] = hot_capture_proof
    current = due(repo=repo, target_date=target_date)
    if body.get("due_version") != current["due_version"]:
        raise BridgeError("D_due_version_stale")
    if current.get("concept_requests"):
        if body.get("concept_priority_version") != current["concept_priority_version"]:
            raise BridgeError("D_concept_priority_version_stale")
        if body.get("concept_requests") != current["concept_requests"]:
            raise BridgeError("D_concept_request_evidence_mismatch")
        if body.get("review_concept_ids") != current["review_concept_ids"]:
            raise BridgeError("D_review_concept_set_mismatch")
    from d_review_scope_408 import load_scope, stage_source_order
    full_targets = current.get("review_concept_ids", current["due_concept_ids"])
    local_scope = load_scope(repo, body.get("local_scope_ref"), body, full_targets)
    pre_campaign_concepts = (local_scope or {}).get("pre_campaign_concept_ids", [])
    selected_items = body.get("selected_items")
    if not isinstance(selected_items, list):
        raise BridgeError("D_selected_items_array_required")
    supplemental_items = body.get("supplemental_items", [])
    if not isinstance(supplemental_items, list):
        raise BridgeError("D_supplemental_items_array_required")
    substitute_items = body.get("substitute_items", [])
    if not isinstance(substitute_items, list):
        raise BridgeError("D_substitute_items_array_required")
    from morning_question_policy_408 import order_items, source_class
    if all(isinstance(item, dict) for item in selected_items + supplemental_items + substitute_items):
        concept_order = current.get("review_concept_ids", current["due_concept_ids"])
        selected_items = order_items(selected_items, concept_order)
        supplemental_items = order_items(
            supplemental_items, concept_order, unclassified_class="variant"
        )
        substitute_items = order_items(substitute_items, concept_order)
        body = {
            **body,
            "selected_items": selected_items,
            "supplemental_items": supplemental_items,
            "substitute_items": substitute_items,
        }
        try:
            stage_source_order(selected_items, supplemental_items, substitute_items, pre_campaign_concepts)
        except ValueError as exc:
            raise BridgeError(str(exc)) from exc
    pointer = repo / ".private/study-bridge/d-current" / (target_date + ".json")
    if pointer.is_file():
        ref = _json(pointer)
        prior_path = Path(ref["receipt_path"])
        prior_path.resolve().relative_to((repo / ".private/study-bridge/d-imports").resolve())
        if _sha(prior_path) != ref["receipt_sha256"]:
            raise BridgeError("D_prior_receipt_drift")
        prior = _json(prior_path)
        if prior["task_id"] == body["task_id"]:
            if prior["input_sha256"] != _digest(body):
                raise BridgeError("D_task_identity_reused_with_changed_input")
            prior_result = {"status": "ALREADY_IMPORTED", "receipt_path": str(prior_path),
                    "receipt_sha256": ref["receipt_sha256"], "formal_write_count": 0,
                    "learner_evidence_write_count": 0, "concept_coverage_complete": False}
    targets = set(local_scope["admitted_concept_ids"] if local_scope else full_targets)
    if len(body.get("due_concept_ids", [])) != len(set(body.get("due_concept_ids", []))) or set(body.get("due_concept_ids", [])) != set(current["due_concept_ids"]):
        raise BridgeError("D_due_concept_set_mismatch")
    groups = {row["concept_id"]: set(row["formal_node_ids"]) for row in current["due_concepts"]}
    master = Repo(repo).master()
    covered: set[str] = set()
    selected_ids: list[str] = []
    all_items = selected_items + supplemental_items + substitute_items
    requests = {row["request_id"]: row for row in current.get("concept_requests", [])}
    verified_supplements = []
    if native:
        from morning_question_policy_408 import require_true_exam_items
        library = native_sources.import_inventory(all_items, body.get("asset_refs", []))
        require_true_exam_items(body, library["question_refs"], repo=repo)
    else:
        library = _published_library(body["source_commit"], repo, body.get("fixture_publication_manifest"))
    published = {row["path"]: row for row in library.get("files", [])}
    output_root = Path(str(body.get("output_root") or "")).expanduser().resolve()
    if not body.get("output_root") or not output_root.is_dir():
        raise BridgeError("D_output_root_required")
    output_files: dict[str, dict[str, Any]] = {}
    if "asset_delivery" in body or body.get("asset_refs"):
        from d_asset_delivery_408 import validate_materialized_assets
        for asset in validate_materialized_assets(body, library):
            output_files[asset["relative_path"]] = asset
    if body.get("fixture_release_dir") and repo.resolve() == REPO.resolve():
        raise BridgeError("D_fixture_release_not_allowed_in_production")
    import start_morning_coverage_campaign_408 as control
    release_dir = Path(body.get("native_release_dir") or body.get("fixture_release_dir") or control.DEFAULT_RELEASE).resolve()
    if body.get("native_release_dir"):
        if release_dir.parent != (details_root / "corpus/408统考真题/style-releases").resolve():
            raise BridgeError("D_native_release_outside_managed_corpus")
        control.load_style_release(release_dir)
    nodes = control._node_snapshot(repo)
    if body.get("ordering_policy"):
        from morning_question_policy_408 import verified_exact_original_ids
        for item in selected_items:
            fid = item.get("question_id")
            if fid in nodes:
                nodes[fid] = {**nodes[fid], "exact_sources": sorted(
                    verified_exact_original_ids(nodes[fid], fid, library))}
    from local_concept_review_408 import substitute_request, select_substitute
    substitute_requests = {}
    for item in substitute_items:
        request = substitute_request(item, current, nodes)
        request_key = (request["question_id"], request["concept_id"])
        if request_key in substitute_requests:
            raise BridgeError("D_substitute_question_duplicate")
        substitute_requests[request_key] = request
    web_variants = body.get("web_variants", [])
    if not isinstance(web_variants, list):
        raise BridgeError("D_web_variants_array_required")
    # Finish every side-effect-free D/source/coverage check before admitting a
    # web-authored object to the private bank. The normal loop below repeats
    # these bindings while constructing the receipt and native run.
    preflight_covered: set[str] = set()
    preflight_selected: set[str] = set()
    preflight_preferred: set[str] = set()
    for preflight_item, preflight_is_supplement in (
        [(item, False) for item in selected_items]
        + [(item, True) for item in supplemental_items]
        + [(item, False) for item in substitute_items]
    ):
        if not isinstance(preflight_item, dict):
            raise BridgeError("D_selected_item_invalid")
        fid = str(preflight_item.get("question_id") or "")
        preflight_is_substitute = preflight_item in substitute_items
        item_targets = preflight_item.get("review_target_concept_ids")
        if fid not in master or not isinstance(item_targets, list) or not item_targets:
            raise BridgeError("D_selected_formal_question_and_targets_required")
        if (
            (not preflight_is_supplement and not preflight_is_substitute and fid in preflight_selected)
            or len(item_targets) != len(set(item_targets))
            or preflight_covered.intersection(item_targets)
        ):
            raise BridgeError("D_duplicate_question_or_concept")
        request = requests.get(preflight_item.get("request_id")) if preflight_is_supplement else None
        if preflight_is_supplement:
            if (
                not request
                or item_targets != [request["concept_id"]]
                or fid not in request["candidate_formal_node_ids"]
            ):
                raise BridgeError("D_supplemental_request_binding_mismatch")
            if request["concept_id"] in current["due_concept_ids"]:
                raise BridgeError("D_primary_due_concept_requires_due_representative")
        elif not preflight_is_substitute and (not set(item_targets) <= targets or not all(
            fid in groups.get(concept, set()) for concept in item_targets
        )):
            raise BridgeError("D_question_concept_binding_mismatch")
        suppressed = preflight_item.get("suppressed_same_concept_question_ids") or []
        expected_suppressed = (
            set()
            if preflight_is_supplement
            else set().union(*(groups[concept] for concept in item_targets))
                 - (set() if preflight_is_substitute else {fid})
        )
        if (
            not isinstance(suppressed, list)
            or len(suppressed) != len(set(suppressed))
            or set(suppressed) != expected_suppressed
        ):
            raise BridgeError("D_suppressed_set_must_include_all_other_same_concept_due_questions")
        if not preflight_item.get("source_label") or not isinstance(preflight_item.get("source_refs"), list) or not preflight_item["source_refs"]:
            raise BridgeError("D_source_provenance_required")
        bound_to_question = False
        for ref in preflight_item["source_refs"]:
            published_path = ref if isinstance(ref, str) else ref.get("path") if isinstance(ref, dict) else None
            if not isinstance(published_path, str) or published_path not in published:
                raise BridgeError("D_source_ref_not_in_published_version")
            published_row = published[published_path]
            if isinstance(ref, dict) and ref.get("sha256") not in (None, published_row["sha256"]):
                raise BridgeError("D_source_ref_hash_mismatch")
            bound_to_question |= fid in (published_row.get("formal_ids") or []) or published_path in {
                fid + ".md", "details/cards/" + fid + ".md"
            }
            local_source = (
                repo / published_path
                if published_path == fid + ".md"
                else details_root / "cards" / (fid + ".md")
                if published_path == "details/cards/" + fid + ".md"
                else None
            )
            if local_source is not None and (
                not local_source.is_file() or _sha(local_source) != published_row["sha256"]
            ):
                raise BridgeError("D_selected_local_source_changed_after_publication")
        if not bound_to_question:
            raise BridgeError("D_source_refs_do_not_bind_selected_question")
        for path_key in ("problem_path", "solution_path"):
            output_file = _d_output_file(output_root, preflight_item.get(path_key))
            output_files[output_file["relative_path"]] = output_file
        for raw_path in (
            list(preflight_item.get("image_paths") or [])
            + ([preflight_item["history_path"]] if preflight_item.get("history_path") else [])
            + ([preflight_item["teaching_path"]] if preflight_item.get("teaching_path") else [])
        ):
            output_file = _d_output_file(output_root, raw_path)
            output_files[output_file["relative_path"]] = output_file
        preferred = str(
            preflight_item.get("web_variant_id")
            or preflight_item.get("preferred_true_item_id")
            or (preflight_item.get("true_item_id") if preflight_is_substitute else None)
            or ""
        )
        if preferred:
            if preferred in preflight_preferred:
                raise BridgeError("D_preferred_variant_reference_duplicate")
            preflight_preferred.add(preferred)
        if not preflight_is_supplement and not preflight_is_substitute:
            preflight_selected.add(fid)
        preflight_covered.update(item_targets)
    preflight_unresolved = body.get("unresolved") or []
    if not isinstance(preflight_unresolved, list):
        raise BridgeError("D_unresolved_array_required")
    preflight_unresolved_ids: set[str] = set()
    for unresolved_item in preflight_unresolved:
        if not isinstance(unresolved_item, dict) or not unresolved_item.get("reason"):
            raise BridgeError("D_unresolved_reason_required")
        preflight_unresolved_ids.update(
            unresolved_item.get("concept_ids")
            or ([unresolved_item["concept_id"]] if unresolved_item.get("concept_id") else [])
        )
    coverage = body.get("coverage") or {}
    if not isinstance(coverage, dict):
        raise BridgeError("D_coverage_object_required")
    if (
        preflight_covered & preflight_unresolved_ids
        or preflight_covered | preflight_unresolved_ids != targets
        or set(coverage.get("covered_concept_ids", [])) != preflight_covered
        or set(coverage.get("unresolved_concept_ids", [])) != preflight_unresolved_ids
    ):
        raise BridgeError("D_coverage_report_mismatch")
    native_due_ids = {
        row["formal_node_id"] for row in current["native_due_run"]["due_items"]
    }
    if not preflight_selected <= native_due_ids:
        raise BridgeError("D_selected_question_not_due")
    has_preferred_variants = any(
        isinstance(item, dict)
        and (item.get("web_variant_id") or item.get("preferred_true_item_id"))
        for item in all_items
    )
    if has_preferred_variants or substitute_items:
        from local_concept_review_408 import assert_no_active

        assert_no_active(repo)
    imported_web: dict[str, dict[str, Any]] = {}
    if web_variants:
        import exam_variant_bank_408 as variant_bank
        for submission in web_variants:
            private_material = submission.get("private_material") if isinstance(submission, dict) else None
            if not isinstance(private_material, dict):
                raise BridgeError("D_web_variant_private_material_required")
            for path_key, hash_key in (
                ("grading_path", "grading_sha256"),
                ("solution_path", "solution_sha256"),
                ("self_check_path", "self_check_sha256"),
            ):
                output_file = _d_output_file(output_root, private_material.get(path_key))
                if output_file["sha256"] != private_material.get(hash_key):
                    raise BridgeError("D_web_variant_private_material_hash_mismatch")
                output_files[output_file["relative_path"]] = output_file
        referenced: dict[str, tuple[dict[str, Any], bool]] = {}
        for item in selected_items:
            if isinstance(item, dict) and item.get("web_variant_id"):
                key = str(item["web_variant_id"])
                if key in referenced:
                    raise BridgeError("D_web_variant_reference_duplicate")
                referenced[key] = (item, False)
        for item in supplemental_items:
            if isinstance(item, dict) and item.get("web_variant_id"):
                key = str(item["web_variant_id"])
                if key in referenced:
                    raise BridgeError("D_web_variant_reference_duplicate")
                referenced[key] = (item, True)
        supplied_ids = [str(row.get("web_variant_id") or "") for row in web_variants if isinstance(row, dict)]
        if set(supplied_ids) != set(referenced) or len(supplied_ids) != len(set(supplied_ids)):
            raise BridgeError("D_web_variant_set_must_equal_referenced_set")
        expected_targets: dict[str, dict[str, Any]] = {}
        for web_id, (item, is_supplement) in referenced.items():
            fid = str(item.get("question_id") or "")
            node = nodes.get(fid)
            request = requests.get(item.get("request_id")) if is_supplement else None
            point = str(request.get("concept_id") or "") if request else str((node or {}).get("main_knowledge") or "")
            if node is None or not point:
                raise BridgeError("D_web_variant_native_target_missing")
            expected_targets[web_id] = {
                "formal_node_id": fid,
                "formal_node_sha256": node["formal_node_sha256"],
                "target_knowledge": point,
            }
        try:
            imported = variant_bank.import_web_self_checked_variants(
                details_root,
                release_dir,
                output_root,
                web_variants,
                expected_targets=expected_targets,
                expected_due_version=current["due_version"],
                expected_concept_priority_version=current.get("concept_priority_version"),
            )
        except variant_bank.VariantBankError as exc:
            raise BridgeError("D_web_variant_invalid:" + str(exc)) from exc
        imported_web = {row["web_variant_id"]: row for row in imported["items"]}
    pool = control.variant_bank.pool_status(details_root, release_dir, **({"persist_index": False} if native else {}))
    pool_variants = {
        str(row.get("variant_id")): row
        for row in pool.get("entries", [])
        if row.get("pool_status") in {"verified", "consumed"}
    }
    preferred_variants_by_node: dict[str, str] = {}
    preferred_originals_by_node: dict[str, dict[str, str]] = {}
    verified_original_surfaces: dict[str, dict[str, Any]] = {}
    original_context = None
    used_preferred_true_items: set[str] = set()
    for item in all_items:
        if not isinstance(item, dict):
            raise BridgeError("D_selected_item_invalid")
        fid = str(item.get("question_id") or "")
        is_supplement = item in supplemental_items
        is_substitute = item in substitute_items
        item_targets = item.get("review_target_concept_ids")
        if fid not in master or not isinstance(item_targets, list) or not item_targets:
            raise BridgeError("D_selected_formal_question_and_targets_required")
        if (not is_supplement and not is_substitute and fid in selected_ids) or len(item_targets) != len(set(item_targets)) or covered.intersection(item_targets):
            raise BridgeError("D_duplicate_question_or_concept")
        request = requests.get(item.get("request_id")) if is_supplement else None
        if is_supplement:
            if not request or item_targets != [request["concept_id"]] or fid not in request["candidate_formal_node_ids"]:
                raise BridgeError("D_supplemental_request_binding_mismatch")
            if request["concept_id"] in current["due_concept_ids"]:
                raise BridgeError("D_primary_due_concept_requires_due_representative")
        elif not is_substitute and (not set(item_targets) <= targets or not all(fid in groups.get(c, set()) for c in item_targets)):
            raise BridgeError("D_question_concept_binding_mismatch")
        suppressed = item.get("suppressed_same_concept_question_ids") or []
        expected_suppressed = (set() if is_supplement else
            set().union(*(groups[c] for c in item_targets)) - (set() if is_substitute else {fid}))
        if not isinstance(suppressed, list) or len(suppressed) != len(set(suppressed)) or set(suppressed) != expected_suppressed:
            raise BridgeError("D_suppressed_set_must_include_all_other_same_concept_due_questions")
        if not item.get("source_label") or not item.get("source_refs"):
            raise BridgeError("D_source_provenance_required")
        bound_to_question = False
        for ref in item["source_refs"]:
            path = ref if isinstance(ref, str) else ref.get("path") if isinstance(ref, dict) else None
            if not isinstance(path, str) or path not in published:
                raise BridgeError("D_source_ref_not_in_published_version")
            row = published[path]
            if isinstance(ref, dict) and ref.get("sha256") not in (None, row["sha256"]):
                raise BridgeError("D_source_ref_hash_mismatch")
            bound_to_question |= fid in (row.get("formal_ids") or []) or path in {fid + ".md", "details/cards/" + fid + ".md"}
            if path == fid + ".md":
                local_source = repo / path
            elif path == "details/cards/" + fid + ".md":
                local_source = details_root / "cards" / (fid + ".md")
            else:
                local_source = None
            if local_source is not None:
                if not local_source.is_file():
                    raise BridgeError("D_selected_local_source_unavailable")
                if _sha(local_source) != row["sha256"]:
                    raise BridgeError("D_selected_local_source_changed_after_publication")
        if not bound_to_question:
            raise BridgeError("D_source_refs_do_not_bind_selected_question")
        web_id = str(item.get("web_variant_id") or "")
        preferred_id = str(item.get("preferred_true_item_id") or "")
        if web_id:
            resolved_web = imported_web.get(web_id)
            if resolved_web is None:
                raise BridgeError("D_web_variant_import_binding_missing")
            if preferred_id and preferred_id != resolved_web["variant_id"]:
                raise BridgeError("D_web_and_preferred_variant_identity_mismatch")
            preferred_id = str(resolved_web["variant_id"])
        is_original = bool(control.CANONICAL_RE.fullmatch(preferred_id)
                           or re.fullmatch(r"FOS-[0-9A-F]{24}", preferred_id))
        canonical_supplement = None
        if is_substitute:
            substitute_surface, reasons = select_substitute(
                repo, target_date, details_root, release_dir, current, item, used_preferred_true_items, **contact_options)
            if substitute_surface is None:
                raise BridgeError("D_substitute_surface_unavailable:" + str(reasons))
            used_preferred_true_items.add(substitute_surface["true_item_id"])
        elif is_supplement and control.CANONICAL_RE.fullmatch(preferred_id):
            from local_concept_review_408 import select_canonical_concept
            canonical_supplement, reasons = select_canonical_concept(
                repo, target_date, details_root, release_dir, fid, request["concept_id"],
                preferred_id, item.get("surface_sha256"), used_preferred_true_items, request["request_id"], **contact_options)
            if canonical_supplement is None:
                raise BridgeError("D_supplemental_canonical_unavailable:" + str(reasons))
            if item.get("true_item_id") not in (None, preferred_id):
                raise BridgeError("D_supplemental_surface_binding_mismatch")
            used_preferred_true_items.add(preferred_id)
        elif preferred_id and is_original:
            if is_supplement or preferred_id in used_preferred_true_items:
                raise BridgeError("D_preferred_original_target_binding_mismatch")
            if original_context is None:
                release = control.variant_bank._release_snapshot(release_dir)
                from exam_practice_units_408 import overlay_release
                release = overlay_release(release, details_root)
                formal_pool = control.formal_original.pool_status(details_root)
                raw_exposure = control.question_exposure.recent_exposure_set(
                    repo, scheduler.parse_date(target_date), window_days=7)
                original_context = (release, control.map_recent_exposures(
                    raw_exposure, release, variant_entries=pool.get("entries") or [],
                    formal_original_entries=formal_pool.get("entries") or []))
            release, mapped = original_context
            from morning_question_policy_408 import verified_exact_original_ids
            selection_node = {**nodes[fid], "exact_sources": sorted(
                verified_exact_original_ids(nodes[fid], fid, library))}
            surface, reasons = control._selection(
                details_root=details_root, release_dir=release_dir, review_date=target_date,
                formal_node_id=fid, node=selection_node, release=release, mapped_exposures=mapped,
                used_true_items=used_preferred_true_items, selector=control.variant_bank.select_surface,
                formal_original_selector=control.formal_original.select_surface,
                variant_entries=pool.get("entries") or [],
                campaign_commitment_id="DIMPORT-" + _digest(body)[:24].upper(),
                preferred_original={"true_item_id": preferred_id, "surface_sha256": item.get("surface_sha256")},
            )
            if surface is None:
                raise BridgeError("D_preferred_original_unavailable:" + str(reasons))
            if item.get("true_item_id") not in (None, preferred_id):
                raise BridgeError("D_preferred_original_surface_binding_mismatch")
            preferred_originals_by_node[fid] = {
                "true_item_id": preferred_id, "surface_sha256": surface["surface_sha256"]}
            verified_original_surfaces[fid] = surface
            used_preferred_true_items.add(preferred_id)
        elif preferred_id:
            preferred = pool_variants.get(preferred_id)
            expected_point = request["concept_id"] if is_supplement else nodes[fid]["main_knowledge"]
            if (
                preferred is None
                or preferred.get("formal_node_id") != fid
                or preferred.get("main_knowledge") != expected_point
                or preferred_id in used_preferred_true_items
            ):
                raise BridgeError("D_preferred_variant_target_binding_mismatch")
            if item.get("true_item_id") not in (None, preferred_id) or item.get("surface_sha256") not in (None, preferred["surface_sha256"]):
                raise BridgeError("D_preferred_variant_surface_binding_mismatch")
            used_preferred_true_items.add(preferred_id)
        for key in ("problem_path", "solution_path"):
            row = _d_output_file(output_root, item.get(key))
            output_files[row["relative_path"]] = row
        for raw in (
            list(item.get("image_paths") or [])
            + ([item["history_path"]] if item.get("history_path") else [])
            + ([item["teaching_path"]] if item.get("teaching_path") else [])
        ):
            row = _d_output_file(output_root, raw)
            output_files[row["relative_path"]] = row
        if is_substitute:
            request = substitute_requests[(fid, item_targets[0])]
            verified_supplements.append({**request, "selection": substitute_surface,
                "execution_kind": "concept_supplement", "question_due": False,
                "question_fsrs_write_count": 0, "original_question_redo_write_count": 0})
        elif is_supplement:
            if canonical_supplement is not None:
                surface, reasons = canonical_supplement, []
            else:
                from local_concept_review_408 import selectors
                _, select_supplement = selectors(repo, target_date, details_root,
                    release_dir, current)
                surface, reasons = select_supplement(fid, request["concept_id"],
                    {s["selection"]["true_item_id"] for s in verified_supplements},
                    preferred_id or None)
            if surface is None:
                raise BridgeError("D_supplemental_verified_variant_required:" + str(reasons))
            if item.get("true_item_id") not in (None, surface["true_item_id"]) or item.get("surface_sha256") not in (None, surface["surface_sha256"]):
                raise BridgeError("D_supplemental_surface_binding_mismatch")
            verified_supplements.append({"request_id": request["request_id"], "concept_id": request["concept_id"],
                "question_id": fid, "selection": surface, "execution_kind": "independent_concept_review"})
        else:
            selected_ids.append(fid)
            if preferred_id and not is_original:
                preferred_variants_by_node[fid] = preferred_id
        covered.update(item_targets)
    unresolved = body.get("unresolved") or []
    unresolved_ids = set()
    for item in unresolved:
        if not isinstance(item, dict) or not item.get("reason"):
            raise BridgeError("D_unresolved_reason_required")
        unresolved_ids.update(item.get("concept_ids") or ([item["concept_id"]] if item.get("concept_id") else []))
    coverage = body.get("coverage") or {}
    if covered & unresolved_ids or covered | unresolved_ids != targets:
        raise BridgeError("D_covered_and_unresolved_must_cover_all_due_concepts")
    if set(coverage.get("covered_concept_ids", [])) != covered or set(coverage.get("unresolved_concept_ids", [])) != unresolved_ids:
        raise BridgeError("D_coverage_report_mismatch")
    original = current["native_due_run"]
    concept_order = {
        concept_id: index
        for index, concept_id in enumerate(current.get("review_concept_ids", current["due_concept_ids"]))
    }
    supplemental_source_rank = {
        "canonical_true_exam": 0,
        "formal_source_original": 1,
        "identity_unclassified": 2,
        "variant": 3,
    }
    verified_supplements.sort(key=lambda row: (
        supplemental_source_rank[source_class(row["selection"])],
        concept_order.get(str(row.get("concept_id") or ""), len(concept_order)),
        str(row.get("request_id") or ""),
    ))
    rows = {row["formal_node_id"]: row for row in original["due_items"]}
    if not set(selected_ids) <= set(rows):
        raise BridgeError("D_selected_question_not_due")
    selected_rows = [rows[fid] for fid in selected_ids]
    segments = []
    for offset in range(0, len(selected_rows), int(original["segment_size"])):
        chunk = selected_rows[offset:offset + int(original["segment_size"])]
        segment = {"segment_index": len(segments) + 1, "segment_size": len(chunk),
                   "formal_node_ids": [row["formal_node_id"] for row in chunk],
                   "item_set_sha256": scheduler.canonical_sha256(chunk)}
        segment["segment_sha256"] = scheduler.canonical_sha256(segment)
        segments.append(segment)
    execution = dict(original)
    execution.pop("run_sha256", None)
    execution.update(due_items=selected_rows, due_count=len(selected_rows), scheduled_count=len(selected_rows),
                     segments=segments,
                     execution_scope="D_concept_representatives", full_due_count=original["due_count"],
                     full_due_run_sha256=original["run_sha256"],
                     suppressed_carryover_formal_node_ids=[fid for fid in rows if fid not in selected_ids])
    execution["independent_supplemental_request_ids"] = [row["request_id"] for row in verified_supplements]
    execution["preferred_variant_ids_by_formal_node"] = preferred_variants_by_node
    execution["preferred_original_items_by_formal_node"] = preferred_originals_by_node
    if local_scope:
        execution["local_scope_ref"] = body["local_scope_ref"]
        execution["admitted_review_concept_ids"] = local_scope["admitted_concept_ids"]
        execution["deferred_concept_ids"] = [r["concept_id"] for r in local_scope["deferred_concepts"]]
    execution["run_sha256"] = scheduler.canonical_sha256(execution)
    receipt = {"schema": "cs408-D-import-receipt-v1", "subject": "408", "task_id": body["task_id"],
               "source_commit": body["source_commit"], "target_date": body["target_date"],
               "due_version": current["due_version"], "input_sha256": _digest(body),
               "concept_priority_version": current.get("concept_priority_version"),
               "concept_requests": current.get("concept_requests", []),
               "review_concept_ids": current.get("review_concept_ids", []),
               "supplemental_items": verified_supplements,
               "web_variant_imports": list(imported_web.values()),
               "full_due_formal_node_ids": list(rows), "due_concept_ids": sorted(targets),
               "selected_items": selected_items, "substitute_items": substitute_items,
               "unresolved_concept_ids": sorted(unresolved_ids),
               "output_root": str(output_root), "output_files": list(output_files.values()),
               "source_local_version": library.get("local_version"),
               "execution_due_run": execution, "formal_write_count": 0, "learner_evidence_write_count": 0}
    if native:
        receipt["source_basis"] = SOURCE_BASIS
    if body.get("native_release_dir"):
        receipt["native_release_dir"] = str(release_dir)
    if body.get('selection_input_version'):
        # Preserve the verified registered pool for date-specific fallback
        # rechecks before the first actual exposure; old receipts stay intact.
        receipt['selection_input_version'] = body['selection_input_version']
    if local_scope:
        receipt["local_scope_ref"] = body["local_scope_ref"]
        receipt["local_scope_decision"] = local_scope
        receipt["pre_campaign_supplemental_request_ids"] = [
            row["request_id"] for row in verified_supplements if row["concept_id"] in pre_campaign_concepts]
    if "asset_delivery" in body:
        receipt["asset_delivery"] = body["asset_delivery"]
        receipt["asset_refs"] = body["asset_refs"]
    if body.get("ordering_policy"):
        from morning_question_policy_408 import RECENCY_POLICY, KEYWORD_POLICY, FREQUENCY_KEYWORD_POLICY, last_true_exam_contacts, ordered_learning_items
        if body["ordering_policy"] not in {RECENCY_POLICY, KEYWORD_POLICY, FREQUENCY_KEYWORD_POLICY}:
            raise BridgeError("D_ordering_policy_invalid")
        contacts = last_true_exam_contacts(repo, library.get("question_refs", {}), target_date, release_dir, **contact_options)
        receipt["ordering_policy"] = body["ordering_policy"]
        keyword_metadata = None
        if body["ordering_policy"] in {KEYWORD_POLICY, FREQUENCY_KEYWORD_POLICY}:
            from morning_selection_408 import registered_keyword_metadata
            keyword_metadata = registered_keyword_metadata(repo, body)
        receipt["learning_items"] = ordered_learning_items(
            selected_items, verified_original_surfaces, verified_supplements, contacts, current["due_version"],
            keyword_metadata=keyword_metadata)
    if "question_presentation" in body:
        from d_asset_delivery_408 import prepared_question_assets, validate_presentation_crops
        receipt["question_presentation"] = body["question_presentation"]
        if not receipt.get("learning_items"):
            raise BridgeError("D_prepared_presentation_requires_ordered_plan")
        if body.get("question_presentation_crops"):
            for output in validate_presentation_crops(body):
                output_files[output["relative_path"]] = output
            receipt["output_files"] = list(output_files.values())
            receipt["question_presentation_crops"] = body["question_presentation_crops"]
        for item in receipt["learning_items"]:
            prepared_question_assets(receipt, item)
        if any("display_order" in item for item in all_items):
            ranks = [item.get("display_order") for item in all_items]
            if (any(type(rank) is not int for rank in ranks)
                    or sorted(ranks) != list(range(1, len(all_items) + 1))):
                raise BridgeError("D_frozen_display_order_invalid")
            frozen_order = {item.get("preferred_true_item_id") or item["true_item_id"]:
                            item["display_order"] for item in all_items}
            from morning_question_policy_408 import frozen_display_order_key
            receipt["learning_items"].sort(
                key=lambda row: frozen_display_order_key(row, contacts, frozen_order))
            receipt["same_date_order"] = "verified_web_display_order"
    if native:
        native_sources.verify_current()
        current_again = due(repo=repo, target_date=target_date)
        if any(current_again[key] != current[key] for key in ("due_version", "concept_priority_version")):
            raise BridgeError("D_native_due_changed_during_import")
    if prior_result is not None:
        load_imported_d_run(repo, target_date)
        return prior_result
    path, digest = _store(repo, "d-imports", receipt)
    if unresolved_ids or current["coverage"]["unmapped_due_formal_node_ids"]:
        return {"status": "NEEDS_SOURCE", "receipt_path": str(path), "receipt_sha256": digest,
                "concept_coverage_complete": False, "formal_write_count": 0, "learner_evidence_write_count": 0}
    if not selected_ids and not verified_supplements:
        return {"status": "NO_DUE_CONCEPTS", "receipt_path": str(path), "receipt_sha256": digest,
                "nothing_due": True, "formal_write_count": 0, "learner_evidence_write_count": 0}
    from exam_tutoring_context_408 import _atomic_write, _canonical_bytes
    _atomic_write(pointer, _canonical_bytes({"receipt_path": str(path), "receipt_sha256": digest}), mode=0o600)
    return {"status": "IMPORTED_FOR_NATIVE_PREPARATION", "receipt_path": str(path), "receipt_sha256": digest,
            "native_prepare_command": "start_morning_coverage_campaign_408.py prepare",
            "full_due_node_count": len(rows), "selected_question_count": len(selected_ids),
            "suppressed_carryover_formal_node_ids": execution["suppressed_carryover_formal_node_ids"],
            "concept_coverage_complete": False, "formal_write_count": 0, "learner_evidence_write_count": 0}


@study_commit_boundary
def record_review(body: dict[str, Any], *, repo: Path = REPO) -> dict[str, Any]:
    import morning_campaign_turn_408 as turn
    if body.get("subject") != "408" or body.get("project") not in {"B", "D"}:
        raise BridgeError("actual_408_B_or_D_review_required")
    if not body.get("task_id") or not body.get("attempt_id") or not (body.get("source_commit") or body.get("library_version")):
        raise BridgeError("review_source_and_actual_attempt_identity_required")
    operation = body.get("operation")
    payload = body.get("input")
    if operation not in turn.COMMANDS or not isinstance(payload, dict) or not payload.get("display_receipt_sha256"):
        raise BridgeError("needs_local_display_and_native_turn_input")
    # The external document carries no authority to grade. The verified current
    # display and original owner decide correctness, draft/Capture and navigation.
    key = "bridge-review:" + _digest([body["project"], body["task_id"], body["attempt_id"], operation])[:40]
    if payload.get("idempotency_key") not in (None, key):
        raise BridgeError("review_idempotency_identity_mismatch")
    payload = {**payload, "idempotency_key": key}
    from question_exposure_408 import private_root_for_repo
    locations = turn.roots(repo_root=repo, package_root=private_root_for_repo(repo))
    allowed = body.get("allowed_formal_ids")
    if (not isinstance(allowed, list) or not allowed or len(allowed) != len(set(allowed))
            or any(not isinstance(fid, str) or not FORMAL_RE.fullmatch(fid) for fid in allowed)):
        raise BridgeError("verified_output_allowed_formal_ids_required")
    display_sha = turn.answer._safe_sha(payload["display_receipt_sha256"], "display_receipt_sha256")
    display_path = Path(locations["private_root"]) / "turns" / (display_sha + ".json")
    _, display, _ = turn.answer.reopen_campaign_display_receipt(display_path, display_sha)
    if display["formal_node_id"] not in allowed:
        raise BridgeError("current_display_is_outside_external_review_output")
    if body["project"] == "D":
        pointer = repo / ".private/study-bridge/d-current" / (display["campaign_date"] + ".json")
        reference = _json(pointer)
        imported_path = Path(reference["receipt_path"])
        imported_path.resolve().relative_to((repo / ".private/study-bridge/d-imports").resolve())
        if _sha(imported_path) != reference["receipt_sha256"]:
            raise BridgeError("D_review_import_receipt_drift")
        imported = _json(imported_path)
        if imported["task_id"] != body["task_id"] or display["formal_node_id"] not in {row["question_id"] for row in imported["selected_items"]}:
            raise BridgeError("current_display_is_outside_imported_D_task")
    result = turn.run(operation, payload, locations)
    receipt = {"schema": "cs408-external-review-binding-v1", "subject": "408",
               "binding_kind": "source_to_native_request_not_completion",
               "project": body["project"], "task_id": body["task_id"], "attempt_id": body["attempt_id"],
               "source_commit": body.get("source_commit"), "library_version": body.get("library_version"),
               "input_sha256": _digest(body), "native_request_file": result.get("request_file"),
               "native_idempotency_key": key,
               "source_request": body,
               "formal_write_count": 0}
    path, digest = _store(repo, "review-bindings", receipt)
    return {**result, "bridge_binding_path": str(path), "bridge_binding_sha256": digest}


@contextmanager
def _source_locks(repo: Path) -> Iterator[list[str]]:
    from intake_lock_408 import repo_lock_path
    from concept_review_408 import private_root
    from seal_knowledge_observations_408 import _formal_root
    paths = [repo_lock_path(repo), repo / "wiki/study_vaults/408-full/state/review-loop/.lock"]
    paths += [private_root(repo) / ".commit.lock", _formal_root(repo, None) / ".seal.lock"]
    handles = []
    try:
        for path in paths:
            # Reading existing lock files does not truncate writer ownership metadata.
            if path.is_file() and not path.is_symlink():
                handle = path.open("rb")
                fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
                handles.append(handle)
        yield [str(p) for p in paths]
    finally:
        for handle in reversed(handles):
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()


def _source_visual_publication_assets(repo: Path, details: Path, canonical_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Publish only activated, hash-bound exam figure pixels, never atlas prose.

    Surface entries attest already published exact question/solution assets.
    Figure origins may also be explicitly reviewed condition crops from this
    parent's registered subquestions; sibling solution arrays never grant access.
    No directory discovery or teacher-plan export occurs through this boundary.
    """
    from source_visual_408 import MANIFEST_REL, INDEX_SCHEMA

    manifest_path = repo / MANIFEST_REL
    if not manifest_path.exists():
        return []
    if manifest_path.is_symlink() or any(p.is_symlink() for p in manifest_path.parents):
        raise BridgeError("source_visual_manifest_symlink_rejected")
    manifest = _json(manifest_path)
    if manifest.get("schema") != INDEX_SCHEMA:
        raise BridgeError("source_visual_manifest_schema_invalid")
    atlas = details / "corpus/408统考真题/visual-atlas"
    reference = manifest.get("atlas_inventory") or {}
    inventory_path = Path(str(reference.get("path") or ""))
    if inventory_path != atlas / "inventory.json":
        raise BridgeError("source_visual_inventory_outside_hot_root")
    if inventory_path.is_symlink() or any(p.is_symlink() for p in inventory_path.parents):
        raise BridgeError("source_visual_inventory_symlink_rejected")
    digest = reference.get("sha256")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise BridgeError("source_visual_inventory_hash_invalid")
    if not inventory_path.is_file() or inventory_path.stat().st_size > 32 * 1024 * 1024:
        raise BridgeError("source_visual_inventory_missing_or_oversize")
    inventory_raw = inventory_path.read_bytes()
    if hashlib.sha256(inventory_raw).hexdigest() != digest:
        raise BridgeError("source_visual_inventory_hash_drift")
    inventory = json.loads(inventory_raw)
    if (not isinstance(inventory, dict) or inventory.get("schema") != "cs408-figure-atlas-v1"
            or not isinstance(inventory.get("assets"), dict)):
        raise BridgeError("source_visual_inventory_schema_invalid")
    canonical = {}
    for row in canonical_rows:
        identity = row["exam_question_id"]
        for field, role in (("question_assets", "question_surface"), ("solution_assets", "solution_surface")):
            for relative in json.loads(row.get(field) or "[]"):
                path = details / _relative(relative)
                canonical.setdefault((identity, str(path)), set()).add(role)
    # An answer-page crop is never promoted to a question figure merely because
    # it belongs to the same parent. Condition crops have a separate allowlist.
    figure_sources = {key for key, roles in canonical.items() if 'question_surface' in roles}
    canonical_by_id = {row['exam_question_id']: row for row in canonical_rows}
    subquestion_index = details / "corpus/408统考真题/subquestions/index.json"
    if subquestion_index.is_file():
        subquestions = _json(subquestion_index)
        original_index = details / "corpus/408统考真题/indexes/question-index.csv"
        if (subquestions.get("schema") != "cs408-exam-subquestions-v1"
                or subquestions.get("status") != "accepted"
                or not original_index.is_file()
                or subquestions.get("source_sha256") != _sha(original_index)):
            raise BridgeError("source_visual_condition_index_not_current")
        items = subquestions.get("items")
        if isinstance(items, dict):
            items = list(items.values())
        if not isinstance(items, list):
            raise BridgeError("source_visual_condition_index_items_invalid")
        parents = {row["exam_question_id"] for row in canonical_rows}
        for item in items:
            if not isinstance(item, dict):
                raise BridgeError("source_visual_condition_identity_invalid")
            child = item.get("subquestion_id") or item.get("true_item_id")
            match = re.fullmatch(r"(EXAM408-\d{4}-Q\d{2})-P\d{2}", str(child or ""))
            if not match or match.group(1) not in parents:
                raise BridgeError("source_visual_condition_identity_invalid")
            parent = match.group(1)
            if any(item.get(key) not in (None, parent) for key in ("parent_exam_id", "parent_true_item_id")):
                raise BridgeError("source_visual_condition_parent_mismatch")
            conditions = item.get("condition_assets", [])
            if not isinstance(conditions, list):
                raise BridgeError("source_visual_condition_assets_invalid")
            if conditions and item.get("review_status") != "reviewed":
                raise BridgeError("source_visual_condition_not_reviewed")
            solutions = set(item.get("solution_images", []))
            for relative in conditions:
                if not isinstance(relative, str) or relative in solutions:
                    raise BridgeError("source_visual_condition_contains_solution")
                path = details / _relative(relative)
                if not path.is_relative_to(details / "corpus/408统考真题"):
                    raise BridgeError("source_visual_condition_outside_exam_corpus")
                # A whole parent may use all its reviewed conditions. A child
                # receives only its own explicitly listed condition images.
                figure_sources.add((parent, str(path)))
                figure_sources.add((child, str(path)))
    policies = {"question_surface": "teacher_only_never_publish_whole_parent",
                "solution_surface": "teacher_only_answer_material",
                "question_figure": "current_question_after_disclosure_review"}
    output, verified = [], {}

    def verify_image(path: Path, expected_sha: str) -> None:
        if (not path.is_absolute() or path.suffix.lower() not in IMAGE_SUFFIXES
                or not path.is_file() or path.is_symlink()
                or any(p.is_symlink() for p in path.parents)
                or path != path.resolve()):
            raise BridgeError("source_visual_image_path_unsafe")
        if not isinstance(expected_sha, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_sha):
            raise BridgeError("source_visual_image_hash_invalid")
        if path not in verified:
            before = path.stat()
            observed = _sha(path)
            after = path.stat()
            if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise BridgeError("source_visual_image_changed_during_read")
            verified[path] = observed
        if verified[path] != expected_sha:
            raise BridgeError("source_visual_image_hash_drift")

    for identity, asset in inventory["assets"].items():
        if not isinstance(asset, dict) or asset.get("asset_id") != identity:
            raise BridgeError("source_visual_asset_identity_invalid")
        role = asset.get("role")
        if role not in policies or asset.get("publication_scope") != policies[role]:
            raise BridgeError("source_visual_asset_role_or_scope_invalid")
        question_id = identity.split(":", 1)[0]
        path = Path(str(asset.get("path") or ""))
        asset_sha = asset.get("sha256")
        if role in {"question_surface", "solution_surface"}:
            if role not in canonical.get((question_id, str(path)), set()):
                raise BridgeError("source_visual_surface_not_in_exact_exam")
            verify_image(path, asset_sha)
            continue
        if (not isinstance(asset_sha, str) or path != atlas / "figures" / (asset_sha + path.suffix.lower())
                or asset.get("review_status") != "visually_reviewed"):
            raise BridgeError("source_visual_figure_outside_reviewed_hot_root")
        original = Path(str(asset.get("source_path") or ""))
        if (question_id, str(original)) not in figure_sources:
            shared_owner = asset.get('shared_condition_source_identity')
            shared_surface = asset.get('shared_condition_surface_sha256')
            shared_valid = False
            if shared_owner in canonical_by_id and (shared_owner, str(original)) in figure_sources and isinstance(shared_surface, str):
                def has_shared_surface(identity):
                    return any('question_surface' in roles and key[0] == identity and _sha(Path(key[1])) == shared_surface
                               for key, roles in canonical.items())
                # Some exam questions explicitly share the very same diagram
                # surface. Reuse a reviewed condition crop only when both exact
                # questions publish byte-identical question surfaces.
                shared_valid = has_shared_surface(question_id) and has_shared_surface(shared_owner)
            row = canonical_by_id.get(question_id, {})
            document = Path(str(asset.get('source_document_path') or ''))
            expected_document = Path(str(row.get('question_source_path') or ''))
            pages = json.loads(row.get('question_pages') or '[]')
            page = asset.get('source_page')
            # Explicit recovery from the current source PDF is required when
            # an older accepted question crop clipped a graph. A foreign paper,
            # answer PDF or unbound cold image is not an equivalent source.
            if not shared_valid and (not document.is_absolute() or not expected_document.is_absolute()
                    or not document.is_file() or document.resolve() != expected_document.resolve()
                    or asset.get('source_document_sha256') != row.get('question_source_sha256')
                    or _sha(document) != row.get('question_source_sha256')
                    or type(page) is not int or page not in pages
                    or not asset.get('crop_coordinate_space')
                    or Path(str(asset.get('source_render_path') or '')) != original):
                raise BridgeError("source_visual_figure_source_not_in_exact_exam")
        verify_image(original, asset.get("source_sha256"))
        verify_image(path, asset_sha)
        output.append({"path": path, "publish_path": "details/" + path.relative_to(details).as_posix(),
                       "question_id": question_id, "sha256": asset_sha})
    return output


def descriptor(*, repo: Path = REPO, details_root: Path = DETAILS,
               package_root: Path = PACKAGES, archive_root: Path = ARCHIVE) -> dict[str, Any]:
    repo, details, packages, archive = [Path(p).expanduser().resolve()
                                      for p in (repo, details_root, package_root, archive_root)]
    with _source_locks(repo) as lock_paths:
        return _descriptor(repo, details, packages, archive, lock_paths)


def _workflow_contract_source(source: Path, archive: Path) -> Path:
    """Resolve only declared workflow inputs through the verified storage map.

    The descriptor's generic ``add`` still rejects all symlinks. A migrated
    directory alias is accepted here only when the exact file has a current
    verified registry record on the intended volume; no historical hash is
    hard-coded, so a later registered revision keeps its own current bytes.
    """
    if not source.is_symlink() and not any(parent.is_symlink() for parent in source.parents):
        return source
    if source.is_symlink():
        raise BridgeError("source_symlink_rejected:" + source.name)
    from archive_408_conversation_package import VOLUME_UUID
    storage_map = _paths.storage_map_root() / 'storage_map.py'
    try:
        resolved = source.resolve(strict=True)
        result = subprocess.run(
            [sys.executable, str(storage_map), "path", str(source), "--verify"],
            capture_output=True, text=True, check=True, timeout=30,
        )
        proof = json.loads(result.stdout)
        volume = proof.get("volume", {})
        rows = proof.get("results", [])
        if (not volume.get("identity_verified")
                or volume.get("actual_uuid") != VOLUME_UUID
                or volume.get("expected_uuid") != VOLUME_UUID
                or len(rows) != 1):
            raise ValueError("volume_or_exact_mapping_not_verified")
        row = rows[0]
        verified = row.get("verification_now", {})
        if (row.get("source") != str(source)
                or row.get("destination") != str(resolved)
                or row.get("kind") != "file"
                or row.get("migration_status") not in {"moved_to_t9", "registered_on_t9"}
                or row.get("proof_kind") not in {
                    "native-copy-v1", "t9-verified-existing-copy-v1", "t9-produced-artifact-v1"}
                or verified.get("status") not in {"matches_migration_hash", "matches_registered_hash"}
                or verified.get("checked_path") != str(resolved)
                or not verified.get("stable_during_read")
                or not resolved.is_relative_to(archive / "01_408")
                or _sha(resolved) != verified.get("sha256_now")):
            raise ValueError("workflow_source_mapping_or_current_bytes_not_verified")
        return resolved
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise BridgeError("workflow_contract_migration_unverified:" + source.name) from exc


def _descriptor(repo: Path, details: Path, packages: Path, archive: Path,
                lock_paths: list[str]) -> dict[str, Any]:
    from intake_lib_408 import Repo
    master = Repo(repo).master()
    if not master:
        raise BridgeError("formal_master_empty")
    formal_ids = set(master)
    sources: dict[str, dict[str, Any]] = {}
    history: list[dict[str, Any]] = []
    gaps: list[dict[str, Any]] = []
    question_refs = {fid: {"formal_id": fid, "source_id": row.get("来源ID"),
                           "safe_card": fid + ".md", "detail_card": "details/cards/" + fid + ".md",
                           "formal_state": {"formal_row_sha256": _digest(row),
                               "safe_card_sha256": _sha(repo / (fid + ".md")) if (repo / (fid + ".md")).is_file() else None},
                           "main_knowledge_points": _concepts(row),
                           "knowledge_points": list(dict.fromkeys(re.findall(r"(?:DS|CO|OS|CN)\d{2}-\d{2}",
                               " ".join(str(row.get(key) or "") for key in ("主知识点", "副知识点", "命中知识点"))))),
                           "canonical_ids": [], "raw_packages": []}
                     for fid, row in master.items()}
    mappings: list[dict[str, Any]] = []

    def add(path: Path, destination: str, kind: str, ids: list[str] | None = None) -> None:
        if path.name in MACHINE_FILENAMES or path.name.startswith("._"):
            return
        target = _relative(destination).as_posix()
        if path.is_symlink() or any(part.is_symlink() for part in path.parents):
            raise BridgeError("source_symlink_rejected:" + path.name)
        if path.suffix in FORBIDDEN_SUFFIXES or any(p in FORBIDDEN_PARTS for p in _relative(destination).parts):
            raise BridgeError("secret_or_runtime_source_rejected:" + path.name)
        if not path.is_file():
            gaps.append({"code": "SOURCE_MISSING", "publish_path": target, "formal_ids": ids or []})
            return
        row = {"source_path": str(path), "publish_path": target, "kind": kind,
               "formal_ids": ids or []}
        prior = sources.get(target)
        if prior is not None and prior["source_path"] != row["source_path"]:
            if _sha(Path(prior["source_path"])) != _sha(path):
                raise BridgeError("conflicting_source_for_publish_path:" + target)
            return
        if prior is not None:
            row["formal_ids"] = sorted(set(prior["formal_ids"]) | set(row["formal_ids"]))
        sources[target] = row

    for name in sorted(TEXT_FILES):
        if (repo / name).exists():
            add(repo / name, name, "formal_index")
    for path in sorted(repo.glob("*.md")):
        if FORMAL_RE.fullmatch(path.stem) or re.fullmatch(r"H(?:DS|CO|OS|CN)_\d{4}", path.stem):
            add(path, path.name, "formal_card" if path.stem in formal_ids else "historical_card", [path.stem])
    for directory in FORMAL_DIRS:
        for path in sorted((repo / directory).rglob("*.md")):
            add(path, path.relative_to(repo).as_posix(), "formal_history")
    state = repo / "wiki/study_vaults/408-full/state"
    for relative in STATE_FILES:
        if (state / relative).is_file():
            add(state / relative, "learning-state/" + relative, "learning_event_ledger")
    for path in sorted((repo / ".private/study-bridge/review-bindings").glob("*.json")):
        value = _json(path)
        if value.get("schema") != "cs408-external-review-binding-v1" or value.get("subject") != "408":
            raise BridgeError("external_review_source_binding_invalid")
        add(path, "review-sources/" + path.name, "external_review_source")
    # Date-specific memory/forecast/due outputs are scheduling evidence. Runtime
    # cursor, hot SQLite, prompts, grader keys and partial preparation stay local.
    for path in sorted((state / "review-campaign").rglob("*.json")):
        relative = path.relative_to(state)
        if not any(p.startswith(".") for p in relative.parts):
            add(path, "learning-state/" + relative.as_posix(), "review_schedule_evidence")

    for path in sorted((details / "cards").glob("*.md")):
        ids = [path.stem] if path.stem in formal_ids else []
        add(path, "details/" + path.relative_to(details).as_posix(), "question_and_solution", ids)
        text = path.read_text(encoding="utf-8")
        refs = re.findall(r"!\[\[([^\]|]+)(?:\|[^\]]+)?\]\]", text)
        refs += re.findall(r"!\[[^\]]*\]\(([^)]+)\)", text)
        for ref in refs:
            raw = ref.split("#")[0].strip("<>")
            if "://" in raw:
                gaps.append({"code": "EXTERNAL_IMAGE_REFERENCE", "formal_ids": ids})
                continue
            candidate = details / raw
            if not candidate.exists():
                candidate = path.parent / raw
            candidate = candidate.resolve()
            try:
                relative = candidate.relative_to(details)
            except ValueError as exc:
                raise BridgeError("details_image_escapes_vault") from exc
            add(candidate, "details/" + relative.as_posix(), "question_or_solution_image", ids)
    for name in ("manifest.csv", "image-qa-manifest.csv", "missing-map.md", "by-subject.md"):
        if (details / "indexes" / name).is_file():
            add(details / "indexes" / name, "details/indexes/" + name, "source_index")
    # Enumerate each formal node's own detail-image directory. Many cards keep
    # their solution as inline text rather than an image reference, so
    # reference-only collection silently dropped solution files that exist on
    # disk, which is what left due questions without a solution layer.
    quarantine = _quarantined_assets(details)
    for node_id in sorted(formal_ids):
        directory = details / "assets" / node_id
        if not directory.is_dir():
            continue
        blocked = quarantine.get(node_id, set())
        for path in sorted(directory.iterdir()):
            if not path.is_file() or path.name in MACHINE_FILENAMES or path.name.startswith("._"):
                continue
            if path.suffix.lower() not in IMAGE_SUFFIXES:
                continue
            if path.name in blocked:
                gaps.append({"code": "ASSET_BINDING_QUARANTINED", "formal_ids": [node_id],
                             "asset": path.name})
                continue
            destination = "details/" + path.relative_to(details).as_posix()
            if destination in sources:
                continue
            kind = ("solution_image" if path.name.startswith("solution")
                    else "question_image" if path.name.startswith("question")
                    else "detail_image")
            add(path, destination, kind, [node_id])
    # The variant bank is answer-split: surfaces carry the stem and options,
    # answer capsules stay private. Only surfaces, the bank index, the
    # answer-safe style artifacts and the plan requests are published.
    variant_root = details / "exam-variant-bank/v1"
    for name, destination in (
        ("408-knowledge-profile-v1.md", "support/d408/knowledge-profile-contract.md"),
        ("408-formal-enhancement-v1.md", "support/d408/formal-enhancement-contract.md"),
        ("raw-capture-day-408-v1.md", "support/d408/raw-capture-day-contract.md"),
        ("formal-point-observations-408-v1.md", "support/d408/formal-point-observations-contract.md"),
        ("concept_review_v1.md", "support/d408/concept-review-contract.md"),
        ("study-episode-capture-v1.md", "support/d408/study-episode-capture-contract.md"),
        ("d-asset-delivery-408-v1.md", "support/d408/asset-delivery-contract.md"),
        ("morning-input-408-v1.md", "support/d408/morning-input-contract.md"),
        ("web-execution-report-408-v1.md", "support/d408/web-execution-report-contract.md"),
    ):
        contract = repo / "schema" / name
        if contract.is_file():
            add(contract, destination, "408_workflow_contract")
    frequency_workflow_entrypoints = {}
    for relative, destination, entrypoint in (
        ('schema/exam-year-study-408-v1.md', 'support/d408/exam-year-study-contract.md', 'exam_year_study_contract'),
        ('.private/exam-year-study/plan.json', 'support/d408/exam-year-study-plan.json', 'exam_year_study_plan'),
        ('schema/evening-review-408-v1.md', 'support/d408/evening-review-contract.md', 'evening_review_contract'),
        ('schema/408-web-enhancement-authoring.md', 'support/d408/web-enhancement-authoring.md', 'web_enhancement_authoring_guide'),
        ('schema/408-portable-visual-teaching-v1.md', 'support/d408/portable-visual-teaching-contract.md', 'portable_visual_teaching_contract'),
        ('schema/408-portable-question-teaching-v1.md', 'support/d408/portable-question-teaching-contract.md', 'portable_question_teaching_contract'),
        ('schema/question-visual-408-v1.md', 'support/d408/question-visual-contract.md', 'question_visual_contract'),
        ('schema/source-visual-408-v1.md', 'support/d408/source-visual-contract.md', 'source_visual_contract'),
        ('planning/2026-09-13-d-deepseek-optimization/408正式入库加强-网页提示词-v4.2.txt',
         'support/d408/enhancement-project-instructions.txt', 'enhancement_project_instructions'),
        ('planning/2026-09-13-d-deepseek-optimization/D-408晨间备题-网页提示词-v7.3.txt',
         'support/d408/morning-project-instructions.txt', 'morning_project_instructions'),
        ('codex-skill-sources/kaoyan-408-evening-review/references/WEB_PROJECT_PROMPT.txt',
         'support/d408/evening-project-instructions.txt', 'evening_project_instructions'),
    ):
        source = repo / relative
        if source.is_file() or source.is_symlink() or any(parent.is_symlink() for parent in source.parents):
            add(_workflow_contract_source(source, archive), destination, '408_workflow_contract')
            frequency_workflow_entrypoints[entrypoint] = destination
    web_contract = repo / "schema/exam-variant-web-self-check-v1.md"
    if web_contract.is_file():
        add(
            web_contract,
            "variant-bank/web-self-check-contract.md",
            "variant_web_self_check_contract",
        )
    bank_index = variant_root / "index.json"
    if bank_index.is_file():
        add(bank_index, "variant-bank/index.json", "variant_bank_index")
        for entry in _json(bank_index).get("entries", []):
            surface_sha = entry.get("surface_sha256")
            surface_ref = entry.get("surface_ref")
            if not isinstance(surface_sha, str) or not isinstance(surface_ref, str):
                continue
            source = variant_root / _relative(surface_ref.replace("exam-variant-bank/v1/", "", 1))
            if not source.is_file():
                gaps.append({"code": "VARIANT_SURFACE_MISSING", "variant_id": entry.get("variant_id")})
                continue
            destination = "variant-bank/surfaces/%s.json" % surface_sha
            add(source, destination, "variant_surface",
                [entry["formal_node_id"]] if entry.get("formal_node_id") else [])
    for path in sorted((variant_root / "requests").glob("*.json")):
        add(path, "variant-bank/requests/" + path.name, "variant_plan_request")
    variant_style_entry = None
    from exam_variant_bank_408 import FINAL_PRODUCTION_RELEASE_ID
    for release in sorted((details / "corpus/408统考真题/style-releases").glob("ESR-*")):
        for name in VARIANT_STYLE_RELEASE_FILES:
            source = release / name
            if source.is_file():
                destination = "variant-bank/style/%s/%s" % (release.name, name)
                add(source, destination, "variant_style_release")
                if name == "style-guide.md" and (variant_style_entry is None or release.name == FINAL_PRODUCTION_RELEASE_ID):
                    variant_style_entry = destination

    index = details / "corpus/408统考真题/indexes/question-index.csv"
    canonical_rows: list[dict[str, Any]] = []
    pdf_digests: dict[Path, str] = {}
    point_links_path = repo / "support/syllabus-2027/point-question-links.json"
    exam_points = _json(point_links_path).get("questions", {}) if point_links_path.is_file() else {}
    if point_links_path.is_file():
        add(point_links_path, "support/syllabus-2027/point-question-links.json", "verified_exam_point_mapping")
    if index.is_file():
        add(index, "details/" + index.relative_to(details).as_posix(), "complete_exam_index")
        with index.open(encoding="utf-8-sig", newline="") as handle:
            canonical_rows = list(csv.DictReader(handle))
        # The corpus index carries a node id for only part of its rows. The
        # formal master names the corpus question as its 来源ID, so join that
        # way as well; otherwise an existing corpus solution image never binds
        # to the node that cites it.
        canonical_ids = canonical_formal_ids(master, canonical_rows)
        for row in canonical_rows:
            identity = row["exam_question_id"]
            ids = canonical_ids[identity]
            mapping = {"question_id": identity, "formal_ids": ids,
                       "year": row.get("year"), "official_question_no": row.get("official_question_no"),
                       "question_images": [], "solution_images": [],
                       "exam_point_context": exam_points.get(identity, {})}
            for field, output in (("question_assets", "question_images"), ("solution_assets", "solution_images")):
                for raw in json.loads(row.get(field) or "[]"):
                    relative = _relative(raw)
                    destination = "details/" + relative.as_posix()
                    add(details / relative, destination, output, ids)
                    mapping[output].append(destination)
            for field in ("question_source_path", "solution_source_path"):
                if row.get(field):
                    path = Path(row[field]).expanduser()
                    allowed_archive = archive / "01_408/资料库/408考研电子书资料"
                    if not any(path.is_relative_to(root) for root in (details, EXAM_SOURCE_ROOT, allowed_archive)):
                        gaps.append({"code": "ORIGINAL_EXAM_SOURCE_OUTSIDE_ALLOWLIST", "question_id": identity, "role": field})
                        continue
                    if path.is_relative_to(EXAM_SOURCE_ROOT) or path.is_relative_to(allowed_archive):
                        from archive_408_conversation_package import verify_volume
                        verify_volume(archive)
                        resolved = path.resolve()
                        if not resolved.is_relative_to(allowed_archive):
                            raise BridgeError("exam_source_alias_escaped_verified_408_archive")
                        path = resolved
                    if path.is_file() and path.suffix.lower() == ".pdf":
                        if path not in pdf_digests:
                            pdf_digests[path] = _sha(path)
                        digest = pdf_digests[path]
                        destination = "original-exams/" + str(digest) + ".pdf"
                        add(path, destination, "original_exam_pdf", ids)
                        mapping[field.replace("_path", "_published_path")] = destination
                    else:
                        gaps.append({"code": "ORIGINAL_EXAM_PDF_MISSING", "question_id": identity, "role": field})
            mappings.append(mapping)
            for fid in ids:
                if fid in question_refs:
                    question_refs[fid]["canonical_ids"].append(identity)
    else:
        gaps.append({"code": "CANONICAL_EXAM_INDEX_MISSING"})

    exam_formal_ids = {row["question_id"]: row["formal_ids"] for row in mappings}
    for asset in _source_visual_publication_assets(repo, details, canonical_rows):
        add(asset["path"], asset["publish_path"], "source_visual_figure",
            exam_formal_ids.get(asset["question_id"], []))

    # Only source bytes referenced by activated teaching objects are portable.
    # Candidate uploads/journals and arbitrary private files are not enumerated.
    from portable_question_teaching_408 import publication_sources as question_teaching_sources
    for source in question_teaching_sources(repo):
        add(Path(source["source_path"]), source["publish_path"], source["kind"])

    # Verified standalone practice units include complete subquestion conditions
    # and the newly supplied exam source. They never create learner wrong cards.
    from exam_practice_units_408 import published_units, verified_unit
    practice_rows = published_units(details)
    if practice_rows:
        practice_index = details / "corpus/408统考真题/practice-units/index.json"
        add(practice_index, "details/" + practice_index.relative_to(details).as_posix(), "verified_practice_unit_index")
    for unit in practice_rows:
        identity = unit["question_id"]
        verified_unit(details, identity)
        ids = sorted(fid for fid, row in master.items() if row.get("来源ID") == identity)
        original_mapping = next((row for row in mappings if row["question_id"] == identity), {})
        mapping = {**original_mapping, **{key: unit.get(key) for key in (
            "question_id", "parent_true_item_id", "main_knowledge", "knowledge_points", "keywords",
            "dependency_kind", "question_type", "surface_sha256", "year", "official_question_no",
            "contact_dependency_ids", "source_authority")}}
        mapping.update(formal_ids=ids, question_images=[], solution_images=[])
        for field in ("question_images", "solution_images"):
            for raw in unit.get(field, []):
                relative = _relative(raw)
                destination = "details/" + relative.as_posix()
                add(details / relative, destination, field, ids)
                mapping[field].append(destination)
        for field in ("card_path", "payload_path"):
            if unit.get(field):
                relative = _relative(unit[field])
                destination = "details/" + relative.as_posix()
                add(details / relative, destination, "verified_practice_unit_" + field, ids)
                mapping[field] = destination
        # Replacing the canonical source mapping is intentional only for the
        # same verified exact question; a subquestion gets its own mapping.
        mappings = [row for row in mappings if row["question_id"] != identity]
        mappings.append(mapping)
        for fid in ids:
            if fid in question_refs and "-P" not in identity:
                question_refs[fid]["canonical_ids"].append(identity)

    archived_ids: set[str] = set()
    notes = sorted((repo / "wiki/sources/raw_archives").glob("*.md"))
    volume_ready = False
    if notes:
        try:
            from archive_408_conversation_package import verify_volume
            verify_volume(archive)
            volume_ready = True
        except (OSError, ValueError, RuntimeError):
            raise OSError("archive_volume_unavailable_existing_history_must_not_be_dropped")
    for note in notes:
        text = note.read_text(encoding="utf-8")
        relative = re.search(r"^raw_archive_relpath:\s*(.+)$", text, re.M)
        expected = re.search(r"^raw_archive_manifest_sha256:\s*(.+)$", text, re.M)
        if not relative or not expected:
            raise BridgeError("archive_locator_binding_missing:" + note.name)
        rel = _relative(relative.group(1).strip().strip('"'))
        if rel.parts[:3] != ("01_408", "资料库", "原始会话资料"):
            raise BridgeError("archive_locator_subject_mismatch")
        if not volume_ready:
            continue
        package = archive / rel
        manifest = package / "manifest.json"
        if not manifest.is_file() or _sha(manifest) != expected.group(1).strip():
            raise BridgeError("archive_manifest_missing_or_drift:" + note.stem)
        value = _json(manifest)
        package_id = str(value.get("package_id") or note.stem)
        archived_ids.add(package_id)
        ids = sorted(set(FORMAL_RE.findall(text)))
        complete = value.get("conversation_complete")
        conversation = package / "conversation.json"
        if conversation.is_file():
            content = _json(conversation)
            complete = content.get("complete_order_preserved") is True and content.get("truncated") is False
        history.append({"package_id": package_id, "formal_ids": ids,
                        "publish_root": "raw-conversations/" + package_id,
                        "conversation_complete": complete,
                        "manifest_sha256": _sha(manifest), "archive_locator": note.relative_to(repo).as_posix()})
        if complete is not True:
            gaps.append({"code": "HISTORICAL_CONVERSATION_INCOMPLETE", "package_id": package_id, "formal_ids": ids})
        for path in sorted(package.rglob("*")):
            if path.is_file():
                add(path, "raw-conversations/" + package_id + "/" + path.relative_to(package).as_posix(), "original_conversation_evidence", ids)
        for fid in ids:
            if fid in question_refs:
                question_refs[fid]["raw_packages"].append(package_id)
    # Sealed current packages remain explicitly pending local formal judgment;
    # they are not relabeled as formal history and never include private drafts.
    for manifest in sorted(packages.glob("20*/CS408-*/manifest.json")):
        value = _json(manifest)
        package_id = str(value.get("package_id") or manifest.parent.name)
        if package_id in archived_ids or value.get("sealed") is not True:
            continue
        history.append({"package_id": package_id, "formal_ids": [],
                        "publish_root": "raw-conversations/" + package_id,
                        "formal_status": "pending_local_judgment", "manifest_sha256": _sha(manifest)})
        for path in sorted(manifest.parent.rglob("*")):
            if path.is_file() and path.suffix not in FORBIDDEN_SUFFIXES:
                add(path, "raw-conversations/" + package_id + "/" + path.relative_to(manifest.parent).as_posix(), "sealed_pending_evidence")

    for fid in sorted(formal_ids):
        if fid + ".md" not in sources:
            gaps.append({"code": "FORMAL_CARD_MISSING", "formal_ids": [fid]})
        if "details/cards/" + fid + ".md" not in sources:
            gaps.append({"code": "DETAIL_CARD_MISSING", "formal_ids": [fid]})
    # Include excluded historical identities in the denominator explicitly.
    history_text = repo / "历史错题归档/定位索引.md"
    if history_text.is_file():
        known = set(re.findall(r"\bH(?:DS|CO|OS|CN)_\d{4}\b", history_text.read_text()))
        mapped = set(re.findall(r"\bH(?:DS|CO|OS|CN)_\d{4}\b", "\n".join(str(r.get("来源ID", "")) for r in master.values())))
        for identity in sorted(known - mapped):
            gaps.append({"code": "HISTORICAL_ID_NOT_IN_CURRENT_FORMAL_COHORT", "source_id": identity,
                         "retained_in": "历史错题归档/定位索引.md"})
    from study_personalization_408 import current_identity, export_snapshot
    personal_identity = current_identity(repo)
    if personal_identity["status"] != "ready":
        raise BridgeError("personalization_not_ready:" + personal_identity["status"])
    personal = export_snapshot(repo, repo / ".private/study-bridge/personalization-export" / personal_identity["version"])
    _prune_export_generations(repo / ".private/study-bridge/personalization-export", keep=3)
    for item in personal["files"]:
        add(Path(item["source_path"]), item["publish_path"], item["kind"])
    # Resolve the ambiguous card-reference role. A card may embed a solution
    # image next to a question image; without a per-file role the reader cannot
    # tell which published image is the answer layer, which is what made a
    # published solution look missing. The filename is the vault's own
    # convention for that role.
    for row in sources.values():
        if row["kind"] != "question_or_solution_image":
            continue
        name = PurePosixPath(row["publish_path"]).name.lower()
        if name.startswith("solution"):
            row["kind"] = "solution_image"
        elif name.startswith("question"):
            row["kind"] = "question_image"
    # Give every formal node an explicit, published question/solution locator
    # map so a reader does not have to re-derive the joins from three indexes.
    question_kinds = {"question_image", "question_images"}
    solution_kinds = {"solution_image", "solution_images"}
    for row in sources.values():
        if row["kind"] in question_kinds:
            bucket = "question_images"
        elif row["kind"] in solution_kinds:
            bucket = "solution_images"
        else:
            continue
        for fid in row["formal_ids"]:
            ref = question_refs.get(fid)
            if ref is not None:
                ref.setdefault(bucket, []).append(row["publish_path"])
    for fid, ref in question_refs.items():
        card = details / "cards" / (fid + ".md")
        question_images = sorted(set(ref.get("question_images", [])))
        solution_images = sorted(set(ref.get("solution_images", [])))
        ref["question_images"] = question_images
        ref["solution_images"] = solution_images
        # The published detail card always carries the question, and carries the
        # inline solution when no solution image exists. A node therefore always
        # has both layers, with the layer kind stated rather than implied.
        card_published = card.is_file() and "details/cards/" + fid + ".md" in sources
        ref["question_layer"] = "image" if question_images else "card_text" if card_published else "missing"
        ref["solution_layer"] = "image" if solution_images else "card_text" if card_published else "missing"
    rows = sorted(sources.values(), key=lambda row: row["publish_path"])
    stamps = [[row["publish_path"], Path(row["source_path"]).stat().st_size,
               Path(row["source_path"]).stat().st_mtime_ns] for row in rows]
    return {"schema": "cs408-study-publication-descriptor-v1", "subject": "408",
            "local_version": _digest(stamps), "sources": rows,
            "personalization": {key: personal[key] for key in ("version", "formal_version", "entrypoint", "coverage", "schema")},
            "formal_node_count": len(formal_ids), "canonical_exam_count": len(canonical_rows),
            "verified_practice_unit_count": len(practice_rows),
            "history_index": history, "question_refs": question_refs,
            "original_mappings": mappings, "gaps": gaps,
            "source_stability": {"source_locks": lock_paths, "copy_requires_pre_post_source_stamp_check": True},
            "exclusions": ["credentials and machine configuration", "runtime databases/locks/hot caches",
                           "private nonterminal drafts", "temporary/backup authoring", "unrelated textbooks/global prompts"],
            "entrypoints": {"personalization": personal["entrypoint"], "formal_nodes": "节点总表.md",
                            "knowledge_points": "知识点标签表.md",
                            "concepts": "概念词典.md",
                            "review_units": "复习单元总表.md",
                            "learning_events": "learning-state/review-loop/events.jsonl",
                            "historical_questions": "历史错题归档/定位索引.md",
                            "canonical_exam_index": "details/corpus/408统考真题/indexes/question-index.csv",
                            "variant_bank": "variant-bank/index.json",
                            "variant_web_self_check_contract": "variant-bank/web-self-check-contract.md",
                            "knowledge_profile_contract": "support/d408/knowledge-profile-contract.md",
                            "formal_enhancement_contract": "support/d408/formal-enhancement-contract.md",
                            "raw_capture_day_contract": "support/d408/raw-capture-day-contract.md",
                            "formal_point_observations_contract": "support/d408/formal-point-observations-contract.md",
                            "concept_review_contract": "support/d408/concept-review-contract.md",
                            "study_episode_capture_contract": "support/d408/study-episode-capture-contract.md",
                            "d_asset_delivery_contract": "support/d408/asset-delivery-contract.md",
                            "morning_input_contract": "support/d408/morning-input-contract.md",
                            "web_execution_report_contract": "support/d408/web-execution-report-contract.md",
                            **frequency_workflow_entrypoints,
                            **({"variant_style_guide": variant_style_entry} if variant_style_entry else {}),
                            "complete_question_and_history_mapping": "PUBLISHED_LIBRARY.json"},
            "local_owner_commands": {"local_decision": "scripts/sol_curation_decision_408.py seal",
                                     "formal_apply": "scripts/intake_apply_408.py",
                                     "current_answer": "scripts/morning_campaign_turn_408.py",
                                     "tutoring": "scripts/exam_tutoring_context_408.py query"}}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    export = sub.add_parser("descriptor")
    export.add_argument("--repo", type=Path, default=REPO)
    export.add_argument("--details-root", type=Path, default=DETAILS)
    export.add_argument("--package-root", type=Path, default=PACKAGES)
    export.add_argument("--archive-root", type=Path, default=ARCHIVE)
    validate = sub.add_parser("validate-package", help="Verify one exact local or archived canonical package")
    validate.add_argument("--repo", type=Path, default=REPO)
    validate.add_argument("--package-root", type=Path, required=True)
    validate.add_argument("--expected-sha256")
    source = sub.add_parser("question-source", help="Resolve one exact historical question and its public image hashes")
    source.add_argument("--repo", type=Path, default=REPO)
    source.add_argument("--details-root", type=Path, default=DETAILS)
    source.add_argument("--formal-id", required=True)
    seal = sub.add_parser("seal-a", help="Bind an external A proposal to an independently supplied local sealed decision")
    seal.add_argument("--repo", type=Path, default=REPO)
    seal.add_argument("--input-json", type=Path, required=True)
    seal.add_argument("--local-decision", type=Path, required=True)
    seal.add_argument("--package-root", type=Path, default=PACKAGES)
    seal.add_argument("--decision-root", type=Path)
    verify = sub.add_parser("verify-a", help="Read back A binding, terminal and committed formal receipt; never apply")
    verify.add_argument("--repo", type=Path, default=REPO)
    verify.add_argument("--input-json", type=Path, required=True)
    verify.add_argument("--decision-root", type=Path)
    verify.add_argument("--formal-runtime-root", type=Path)
    daily = sub.add_parser("due", help="Read the full native due cohort and its primary-knowledge groups")
    daily.add_argument("--repo", type=Path, default=REPO)
    daily.add_argument("--date", required=True)
    daily.add_argument("--config", type=Path)
    for name in ("import-d", "record-review"):
        command = sub.add_parser(name)
        command.add_argument("--repo", type=Path, default=REPO)
        command.add_argument("--input-json", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        args.repo = args.repo.expanduser().resolve()
        if args.command == "descriptor":
            result = descriptor(repo=args.repo, details_root=args.details_root,
                                package_root=args.package_root, archive_root=args.archive_root)
        elif args.command == "validate-package":
            result = validate_package(args.package_root, repo=args.repo, expected_sha256=args.expected_sha256)
        elif args.command == "question-source":
            result = question_source(args.formal_id, repo=args.repo, details_root=args.details_root)
        elif args.command == "seal-a":
            result = seal_a(_json(args.input_json), _json(args.local_decision), repo=args.repo,
                            package_root=args.package_root, decision_root=args.decision_root)
        elif args.command == "verify-a":
            result = verify_a(_json(args.input_json), repo=args.repo, decision_root=args.decision_root,
                              formal_runtime_root=args.formal_runtime_root)
        elif args.command == "due":
            result = due(repo=args.repo, target_date=args.date, config_path=args.config)
        elif args.command == "import-d":
            result = import_d(_json(args.input_json), repo=args.repo)
        else:
            result = record_review(_json(args.input_json), repo=args.repo)
    except (BridgeError, ValueError, KeyError, TypeError) as exc:
        print(json.dumps({"subject": "408", "status": "INVALID_INPUT", "reason": str(exc)}, ensure_ascii=False))
        return 2
    except (OSError, RuntimeError) as exc:
        print(json.dumps({"subject": "408", "status": "RETRYABLE", "reason": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
