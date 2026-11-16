#!/usr/bin/env python3
"""Prepare and qualify a versioned local 408 QMD projection without cloud calls."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from formal_quality_api_408 import QualityError, atomic, read
from formal_quality_index_408 import public_projection, sha_file
from memory_semantic_precision_408 import _node_call

RUNTIME = Path(".private/memory-retrieval-runtime")
RETRIEVAL = Path(".private/memory-backends-v1")
HOLDOUT_ROOT = Path("/Volumes/T9-Data/01_408/408网页中转/系统改造/"
                    "2026-11-06-retrieval-precision-01a1aee4/evaluation")
BASELINE_SHA = "a922fa118e919f4a4d9016a5ee7e7d8fa135dcf397ee9390941f2f8c1328a92e"


def qualify(repo, *, output_root=None, activate=False):
    from benchmark_retrieval_precision_408 import load_cases, score_result
    from memory_access_408 import access

    repo = Path(repo).resolve()
    snap, projection, _, _ = public_projection(repo)
    fingerprint = projection["projection_sha256"]
    policy = repo / RUNTIME / "discovery-policy.json"
    runtime_root = repo / RETRIEVAL
    old = read(policy)
    old_semantic = old["backend_options"]["semantic"]
    model = Path(old_semantic["model_path"])
    if not model.is_file() or not model.resolve().is_relative_to(runtime_root.resolve()):
        raise QualityError("qmd_qualified_local_model_unavailable")
    root = Path(output_root).resolve() if output_root else repo / RUNTIME / "generations" / fingerprint
    if activate and not root.is_relative_to(repo / RUNTIME):
        raise QualityError("qmd_activation_requires_native_private_generation")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    corpus = root / "corpus"
    corpus.mkdir(exist_ok=True, mode=0o700)
    for doc in projection["documents"]:
        path = corpus / (doc["id"] + ".md")
        if not path.is_file():
            path.write_text(doc["text"], encoding="utf-8")
        if sha_file(path) != doc["text_sha256"]:
            raise QualityError("qmd_staged_corpus_drift:" + doc["id"])
    projected = root / "projection.json"
    atomic(projected, projection)
    index_path = root / "index.sqlite"
    manifest_path = root / "semantic-scope-manifest.json"
    if not manifest_path.is_file():
        built = _node_call({"operation": "build_shadow", "trial": True, "shadowRoot": str(root),
                            "corpusPath": str(corpus), "indexPath": str(index_path),
                            "modelPath": str(model), "runtimeRoot": str(runtime_root),
                            "projectionPath": str(projected), "sourceVersion": snap.manifest["source_version"]},
                           timeout=900)
        if built.get("status") != "ready":
            raise QualityError("qmd_generation_pending:" + str(built.get("reason")))
    manifest = read(manifest_path)
    if manifest["projection_sha256"] != fingerprint or not index_path.is_file():
        raise QualityError("qmd_generation_drift")

    case_path = HOLDOUT_ROOT / "private/holdout-24-v2.json"
    baseline_path = HOLDOUT_ROOT / "final-holdout/C-holdout.json"
    if sha_file(baseline_path) != BASELINE_SHA:
        raise QualityError("qmd_holdout_baseline_drift")
    gold, cases = load_cases(case_path, "holdout", allow_holdout=True)
    baseline = read(baseline_path)
    if gold.get("parent_gold_sha256", gold.get("gold_sha256")) != baseline["gold_sha256"]:
        raise QualityError("qmd_holdout_gold_drift")
    previous = {row["id"]: row for row in baseline["rows"]}
    semantic = {"expected_projection_hash": fingerprint, "index_path": str(index_path),
                "manifest_path": str(manifest_path), "model_path": str(model),
                "runtime_root": str(runtime_root), "trial": True}
    checks, regressions, safety = [], [], []
    for case in cases:
        request = dict(case["request"])
        view = request.pop("view", "protected")
        result = access(repo, "discover", view=view, query=case["query"], limit=8, **request,
                        backend="hybrid_local", backend_options={"semantic": {**semantic, "timeout": 10}})
        scored = score_result(case, result, unsupported=False)
        old_score = previous[scored["id"]]
        for metric in ("top1", "top5", "control_pass"):
            if old_score.get(metric) is True and scored.get(metric) is not True:
                regressions.append({"id": scored["id"], "metric": metric})
        if scored.get("safety_errors"):
            safety.append(scored["id"])
        checks.append({key: scored.get(key) for key in ("id", "group", "top1", "top5", "control_pass", "safety_errors")})
    qualified = not regressions and not safety
    receipt = {"schema": "cs408-local-backend-qualification-v1",
               "public_projection_sha256": fingerprint, "source_version": snap.manifest["source_version"],
               "candidate": "hybrid_local_no_rerank", "cases": len(cases), "cloud_calls": 0,
               "reranker_enabled": False, "scope_errors": len(safety),
               "gold_sha256": sha_file(case_path), "baseline_sha256": BASELINE_SHA,
               "status": "qualified" if qualified else "lexical_fallback",
               "regressions": regressions, "checks": checks}
    receipt_path = root / "qualification.json"
    atomic(receipt_path, receipt)
    if activate and qualified:
        now, actual, _, _ = public_projection(repo)
        if actual["projection_sha256"] != fingerprint or now.manifest["source_version"] != snap.manifest["source_version"]:
            raise QualityError("qmd_native_source_advanced")
        # Readers retain the old qualified policy until both the new receipt
        # and public generation are durable. Read back verifies the hash.
        next_policy = {"schema": "cs408-local-discovery-policy-v1", "status": "qualified",
                       "default_backend": "hybrid_local", "projection_sha256": fingerprint,
                       "qualification_sha256": sha_file(receipt_path),
                       "qualification_ref": str(receipt_path.relative_to(repo / RUNTIME)),
                       "backend_options": {"semantic": semantic},
                       "experience_backend": "native_source_evidence", "reranker_enabled": False}
        atomic(policy, next_policy)
        if read(policy) != next_policy:
            raise QualityError("qmd_policy_readback_drift")
    return {"status": receipt["status"], "qualification_path": str(receipt_path),
            "regressions": regressions, "source_version": snap.manifest["source_version"],
            "cases": len(cases), "cloud_calls": 0, "activated": activate and qualified}


def promote_qualified(repo, staged):
    """Activate an already qualified public generation after exact hash binding."""
    import os
    import shutil
    import tempfile

    repo = Path(repo).resolve()
    staged = Path(staged).resolve()
    snap, projection, _, _ = public_projection(repo)
    fingerprint = projection["projection_sha256"]
    receipt = read(staged / "qualification.json")
    manifest = read(staged / "semantic-scope-manifest.json")
    if (receipt["status"] != "qualified" or receipt["public_projection_sha256"] != fingerprint
            or manifest["projection_sha256"] != fingerprint or receipt["baseline_sha256"] != BASELINE_SHA
            or receipt["cases"] != 24 or receipt["regressions"] or receipt["scope_errors"]):
        raise QualityError("qmd_staged_qualification_not_current")
    wal = staged / "index.sqlite-wal"
    if wal.is_file() and wal.stat().st_size:
        raise QualityError("qmd_staged_index_requires_checkpoint")
    model = Path(manifest["model"])
    if sha_file(model) != manifest["model_sha256"] or not model.resolve().is_relative_to((repo / RETRIEVAL).resolve()):
        raise QualityError("qmd_staged_model_changed")
    dest = repo / RUNTIME / "generations" / fingerprint
    dest.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    files = ("index.sqlite", "semantic-scope-manifest.json", "qualification.json")
    if not dest.exists():
        temp = Path(tempfile.mkdtemp(prefix=".install-", dir=dest.parent))
        for name in files:
            shutil.copyfile(staged / name, temp / name)
            os.chmod(temp / name, 0o600)
        os.rename(temp, dest)
    if any(sha_file(dest / name) != sha_file(staged / name) for name in files):
        raise QualityError("qmd_staged_generation_copy_drift")
    current, projection_now, _, _ = public_projection(repo)
    if projection_now["projection_sha256"] != fingerprint or current.manifest_sha != snap.manifest_sha:
        raise QualityError("qmd_staged_native_generation_advanced")
    receipt_path = dest / "qualification.json"
    policy = {"schema": "cs408-local-discovery-policy-v1", "status": "qualified",
              "default_backend": "hybrid_local", "projection_sha256": fingerprint,
              "qualification_sha256": sha_file(receipt_path),
              "qualification_ref": str(receipt_path.relative_to(repo / RUNTIME)),
              "backend_options": {"semantic": {"expected_projection_hash": fingerprint,
                "index_path": str(dest / "index.sqlite"), "manifest_path": str(dest / "semantic-scope-manifest.json"),
                "model_path": str(model), "runtime_root": str(repo / RETRIEVAL), "trial": True}},
              "experience_backend": "native_source_evidence", "reranker_enabled": False}
    target = repo / RUNTIME / "discovery-policy.json"
    atomic(target, policy)
    if read(target) != policy:
        raise QualityError("qmd_policy_readback_drift")
    return {"status": "qualified", "source_version": current.manifest["source_version"],
            "cases": receipt["cases"], "regressions": [], "activated": True}


def main():
    from formal_quality_408 import CANONICAL_REPO
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--repo", type=Path, default=CANONICAL_REPO)
    p.add_argument("--output-root", type=Path)
    p.add_argument("--activate", action="store_true")
    args = p.parse_args()
    try:
        result = qualify(args.repo, output_root=args.output_root, activate=args.activate)
    except (OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
        result = {"status": "lexical_fallback", "reason": str(exc)}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "qualified" else 2


if __name__ == "__main__":
    raise SystemExit(main())
