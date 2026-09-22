#!/usr/bin/env python3
"""Freeze a Shanghai day's raw Capture packages without grading or formal intake."""
from __future__ import annotations

import argparse
from contextlib import nullcontext
import datetime as dt
import fcntl
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import zipfile
from zoneinfo import ZoneInfo

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path.home() / "Documents/Study-Pro-Bridge"))
from bridge_archives import safe_path, source_file, write_zip, file_hash
import conversation_package_408 as packages
import intake_fact_capture_408 as captures

SCHEMA = "cs408-raw-capture-day-v1"


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def require(condition, message):
    if not condition:
        raise ValueError(message)


def instant(text):
    value = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    require(value.tzinfo is not None, "export_cutoff_timezone_required")
    return value.astimezone(dt.timezone.utc)


def _open_package(repo, locator, package_root, expected_sha):
    try:
        return packages.verify_package_binding(locator, private_root=package_root,
                    expected_canonical_sha256=expected_sha, require_sealed=True)
    except packages.ConversationPackageError:
        # Exact archived package only; no archive scan or invented replacement.
        import archive_408_conversation_package as archive
        package_id = locator.removeprefix(packages.LOCATOR_PREFIX)
        proof = archive.reopen_archived_package(repo=repo, package_id=package_id,
                    requested_relative_paths=["conversation.json", "source.json"])
        note = archive._read_locator_note(Path(repo) / archive.LOCATOR_NOTE_ROOT / (package_id + ".md"))
        folder = Path(archive.ARCHIVE_ROOT) / note["raw_archive_relpath"]
        manifest = json.loads((folder / "manifest.json").read_text())
        require(manifest["canonical_sha256"] == proof["package_sha256"], "export_archive_binding_drift")
        require(proof["package_sha256"] == expected_sha, "export_archive_capture_binding_drift")
        objects = {row["relative_path"]: row["json"] for row in proof["requested_objects"]}
        return {"package_dir": str(folder), "manifest": manifest,
                "conversation": objects["conversation.json"], "source": objects["source.json"],
                "archive_proof": proof}


def _export_day(repo, study_date, *, output=None, cutoff=None, package_root=None,
               baseline_snapshot_id=None, details_root=None):
    repo = Path(repo).resolve()
    require(dt.date.fromisoformat(study_date).isoformat() == study_date, "export_study_date_invalid")
    end = instant(cutoff) if cutoff else dt.datetime.now(dt.timezone.utc)
    root = captures.capture_root(repo)
    lock_path = root / ".lock"
    with (lock_path.open("rb") if lock_path.exists() else nullcontext()) as lock:
        if lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_SH)
        events = captures._load_events(root)
        event_lines = (root / "events.jsonl").read_bytes().splitlines(keepends=True) if (root / "events.jsonl").is_file() else []
        eligible = [event for event in events if instant(event["created_at"]) <= end]
        state = captures.replay(eligible)
    selected = [row for _, row in sorted(state.get("captures", {}).items()) if row["study_date"] == study_date]
    require(selected, "export_no_captures_for_date")
    bound, opened, compatibility, missing = [], {}, [], []
    for capture in selected:
        cid = capture["capture_id"]
        locators = [loc for loc in captures._capture_evidence_locators(capture) if loc.startswith(packages.LOCATOR_PREFIX)]
        locators.extend(ref['locator'] for ref in capture.get('supplement_history', [])
                        if str(ref.get('locator', '')).startswith(packages.LOCATOR_PREFIX))
        try:
            require(locators, "canonical_package_locator_missing")
            # A supplement can retain its parent refs. Its manifest proves the
            # same Capture episode; retain every version as raw evidence while
            # selecting the latest cumulative version as the enhancement input.
            references = [ref for ref in capture["capture"].get("stable_evidence_refs", [])
                          if isinstance(ref, dict) and str(ref.get("locator", "")).startswith(packages.LOCATOR_PREFIX)]
            history = capture.get("supplement_history", [])
            bindings = {ref["locator"]: ref["sha256"] for ref in references + history}
            require(all(loc in bindings for loc in locators), "export_package_hash_binding_missing")
            versions = [_open_package(repo, loc, package_root, bindings[loc]) for loc in dict.fromkeys(locators)]
            primary = history[-1] if history else references[0]
            package = next(value for value in versions
                           if packages.LOCATOR_PREFIX + value["manifest"]["package_id"] == primary["locator"])
            opened[cid] = versions
            inner = package["source"].get("source") or {}
            facts = capture["capture"].get("source_facts") or {}
            kind = inner.get("execution_kind") or facts.get("execution_kind")
            episode = inner.get("episode_id") or facts.get("episode_id")
            if not kind or not episode:
                compatibility.append({"capture_id": cid, "original_episode_id": episode,
                                      "original_execution_kind": kind,
                                      "note": "Legacy Capture identity is the compatibility key, not a fabricated learning event."})
            row = {"capture_id": cid, "capture_payload_sha256": capture["payload_sha256"],
                   "episode_id": episode or cid,
                   "execution_kind": kind or "standalone_question",
                   "evidence_locator": packages.LOCATOR_PREFIX + package["manifest"]["package_id"],
                   "evidence_sha256": package["manifest"]["canonical_sha256"]}
            require(row["execution_kind"] in {"original_review", "concept_supplement", "standalone_question"}, "unknown_execution_kind")
            bound.append(row)
        except (ValueError, OSError, KeyError) as exc:
            missing.append({"capture_id": cid, "reason": str(exc)})
    # Never make an incomplete day look like an all-Capture learning package.
    if missing:
        return {"status": "BLOCKED", "study_date": study_date, "expected_capture_count": len(selected),
                "unresolved": missing, "formal_write_count": 0, "learning_write_count": 0}
    from start_morning_coverage_campaign_408 import pending_for_date
    pending = pending_for_date(repo, study_date=study_date)["episodes"]
    from d_concept_supplement_408 import receipt_root, read_receipt
    bound_displays = {
        ((package["source"].get("source") or {}).get("display_binding") or {}).get("receipt_sha256")
        for versions in opened.values() for package in versions
    }
    for path in receipt_root(repo).glob("*.json"):
        display = read_receipt(path)
        if display.get("execution_kind") != "concept_supplement":
            continue
        displayed = instant(display["displayed_at"])
        if (displayed <= end and displayed.astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat() == study_date
                and display["receipt_sha256"] not in bound_displays):
            pending.append({"request_id": display["request_id"], "episode_id": None,
                            "display_receipt_path": str(path), "status": "displayed_unsealed"})
    if pending:
        return {"status": "BLOCKED", "reason": "unsealed_episodes_remain", "study_date": study_date,
                "sealed_capture_count": len(bound), "unsealed_episodes": pending,
                "formal_write_count": 0, "learning_write_count": 0}
    identity = digest(bound)
    details = Path(details_root or "/Users/your-user/Documents/kaoyan-408-details").resolve()
    target = Path(output).expanduser().resolve() if output else details / "learning-deliveries" / study_date / ("408_learning_" + study_date + "_" + identity[:12] + ".zip")
    require(not target.is_relative_to(repo) and not target.is_relative_to(Path(package_root or packages.DEFAULT_ROOT).expanduser().resolve()),
            "export_destination_must_be_outside_source_store")
    if target.exists():
        with zipfile.ZipFile(source_file(target)) as old:
            manifest = json.loads(old.read("manifest.json"))
            for row in manifest.get("files", []):
                raw = old.read(row["path"])
                require(len(raw) == row["size_bytes"] and hashlib.sha256(raw).hexdigest() == row["sha256"],
                        "export_existing_archive_hash_drift")
        require(manifest.get("input_set_sha256") == identity and manifest.get("schema") == SCHEMA, "export_output_conflict")
        return {"status": "ALREADY_EXPORTED", "path": str(target), "sha256": file_hash(target),
                "capture_count": len(bound), "formal_write_count": 0, "learning_write_count": 0}
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".408-day-", dir=target.parent) as temporary:
        stage = Path(temporary)
        files, roles, index = {}, {}, []

        def add_bytes(relative, raw, role):
            relative = safe_path(relative)
            dest = stage / relative
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(raw)
            files[relative], roles[relative] = dest, role

        for capture in selected:
            cid = capture["capture_id"]
            add_bytes("captures/" + cid + "/capture.json", encoded(capture["capture"]), "raw_capture")
            add_bytes("captures/" + cid + "/local_status.json", encoded({
                "capture_id": cid, "study_date": capture["study_date"],
                "capture_payload_sha256": capture["payload_sha256"],
                "quality_status": capture.get("quality_status"),
                "current_batch_id": capture.get("current_batch_id"),
                "formal_id_hint": capture.get("formal_id"),
                "recorded_at": capture.get("recorded_at"),
                "scope": "frozen canonical Capture state; not proof that an unbound formal transaction never committed",
            }), "frozen_local_status")
            for package in opened[cid]:
                manifest = package["manifest"]
                folder = Path(package["package_dir"])
                prefix = "captures/" + cid + "/packages/" + manifest["package_id"] + "/"
                for row in manifest["files"]:
                    relative = safe_path(row["relative_path"])
                    path = source_file(folder / relative, folder)
                    raw = path.read_bytes()
                    require(len(raw) == row["byte_count"] and hashlib.sha256(raw).hexdigest() == row["sha256"], "export_package_changed_during_copy")
                    add_bytes(prefix + relative, raw, row.get("role") or "raw_evidence")
                add_bytes(prefix + "manifest.json", (folder / "manifest.json").read_bytes(), "raw_manifest")
                receipt = folder / "receipt.json"
                if receipt.is_file() and prefix + "receipt.json" not in files:
                    add_bytes(prefix + "receipt.json", receipt.read_bytes(), "raw_receipt")
                if package.get("archive_proof"):
                    add_bytes(prefix + "archive-reopen-proof.json", encoded(package["archive_proof"]), "archive_proof")
                index.append({"capture_id": cid, "package_id": manifest["package_id"],
                              "canonical_sha256": manifest["canonical_sha256"], "path": prefix,
                              "missing_fields": manifest.get("missing_fields", [])})
        add_bytes("capture_index.json", encoded({"captures": bound, "packages": index, "legacy_identity_notes": compatibility}), "index")
        selected_ids = {row["capture_id"] for row in selected}
        event_ids = {event["event_id"] for event in eligible if event.get("capture_id") in selected_ids}
        raw_events = [line for line in event_lines if line.strip() and json.loads(line).get("event_id") in event_ids]
        add_bytes("capture_events.jsonl", b"".join(raw_events), "raw_capture_events")
        for filename, target_name in (
            ("408-formal-enhancement-v1.md", "formal-enhancement-contract.md"),
            ("408-knowledge-profile-v1.md", "knowledge-profile-contract.md"),
            ("web-execution-report-408-v1.md", "web-execution-report-contract.md"),
        ):
            contract = repo / "schema" / filename
            if contract.is_file():
                add_bytes("contracts/" + target_name, contract.read_bytes(), "contract")
        authoring = repo / "schema/408-web-enhancement-authoring.md"
        if authoring.is_file():
            add_bytes("WEB_AUTHORING_GUIDE.md", authoring.read_bytes(), "instructions")
        template_run = "WEB-408-" + study_date.replace("-", "") + "-" + identity[:16]
        add_bytes("return-template/enhancement.json", encoded({
            "schema": "cs408-formal-enhancement-v1", "run_id": template_run,
            "source_commit": baseline_snapshot_id or "", "items": [],
        }), "unfilled_template")
        add_bytes("return-template/knowledge_updates.json", encoded({
            "schema": "cs408-knowledge-profile-updates-v1", "updates": [],
        }), "unfilled_template")
        add_bytes("START_HERE.md", (
            "# 408 原始学习总包\n\n本包是指定日期的完整Capture证据，不是正式入库决定。"
            "正确题、提示后完成、未解决和点级补充保持原始身份；Capture不代表错误。\n\n"
            "请交给GPT-6 Pro的408正式入库增强业务，完整读取本批原件后，结合MCP当前正式历史，"
            "按 support/d408/formal-enhancement-contract.md 与 knowledge-profile-contract.md "
            "输出最终 enhancement.json 和 knowledge_updates.json。"
            "manifest.captures可直接沿用，snapshot/source_commit需来自本次library实际返回，"
            "不要把本包的baseline_snapshot_id当成新正式版本。\n\n"
            "每个Capture和知识点独立处理并及时保存成功成稿；单项失败记录后继续其他不依赖它的工作。"
            "无论完整、PARTIAL或BLOCKED，最终都必须交已有实际文件、EXECUTION_REPORT.md和web_execution_report.json，"
            "列明成功、失败、未执行及真实失败调用的工具名、参数、原始错误/返回；缺原生call_id则null，不编造。"
            "报告按support/d408/web-execution-report-contract.md；manifest增加web_execution_report引用并给该文件相同role。"
            "不能因为最后一项失败把前面成功清零，真实零成功才报告零。\n\n"
            "回包由本地保全成功成果、从原件补齐失败部分并完成正式入库；已COMMITTED不重复写。"
            "本地还会根据真实读取错误作必要修复，更新下一轮规则；正式记录/摘要/快照完成后自动导出下一轮晨间资料。\n\n"
            "正式判分、FSRS和个人摘要由加强包本地入库统一更新；本次导出不产生学习或正式事件。\n"
            "\n先读WEB_AUTHORING_GUIDE.md和contracts。return-template是待填模板，不是完成结果；"
            "网页应补齐语义、真实来源引用和附件可见性枚举，只将本机回执/事务工作留到本地。"
            "正式ID的来源绑定用question_refs查询；日期/run/Capture共同限定所有MCQ说明。\n"
        ).encode(), "instructions")
        manifest = {"schema": SCHEMA, "schema_version": 1, "subject": "408", "study_date": study_date,
                    "timezone": "Asia/Shanghai", "run_id": "408-DAY-" + study_date.replace("-", "") + "-" + identity[:16],
                    "baseline_snapshot_id": baseline_snapshot_id, "cutoff": end.isoformat(),
                    "input_set_sha256": identity, "capture_count": len(bound), "captures": bound,
                    "capture_set_sha256": hashlib.sha256(json.dumps([row["capture_id"] for row in bound], ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
                    "formal_write_count": 0, "learning_write_count": 0,
                    "files": [{"path": path, "sha256": file_hash(file), "size_bytes": file.stat().st_size, "role": roles[path]}
                              for path, file in sorted(files.items())]}
        add_bytes("manifest.json", encoded(manifest), "manifest")
        write_zip(files, target)
    return {"status": "EXPORTED", "path": str(target), "sha256": file_hash(target),
            "capture_count": len(bound), "package_count": len(index), "input_set_sha256": identity,
            "formal_write_count": 0, "learning_write_count": 0}


def export_day(repo, study_date, *, output=None, cutoff=None, package_root=None,
               baseline_snapshot_id=None, details_root=None):
    result = _export_day(repo, study_date, output=output, cutoff=cutoff, package_root=package_root,
                         baseline_snapshot_id=baseline_snapshot_id, details_root=details_root)
    if result.get("capture_count", 0) > 40 and result.get("path"):
        try:
            from split_web_input_408 import split_captures
            result.update(split_captures(result["path"]))
        except Exception as exc:
            result.update(partition_status="PENDING", partition_error=str(exc))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--date", required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--cutoff")
    parser.add_argument("--package-root", type=Path)
    parser.add_argument("--details-root", type=Path)
    args = parser.parse_args()
    try:
        result = export_day(args.repo, args.date, output=args.output, cutoff=args.cutoff,
                            package_root=args.package_root, details_root=args.details_root)
    except Exception as exc:
        result = {"status": "BLOCKED", "reason": str(exc), "formal_write_count": 0, "learning_write_count": 0}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] in {"EXPORTED", "ALREADY_EXPORTED"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
