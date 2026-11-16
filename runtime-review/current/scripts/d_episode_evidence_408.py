"""Append verified late discussion to an existing D Capture; never navigate or relabel."""
import json
from pathlib import Path

import conversation_package_408 as packages
import d_concept_supplement_408 as native
import intake_fact_capture_408 as captures
import study_episode_capture_408 as episodes
import study_message_source_408 as messages
import study_transcript_408 as transcripts
from mark_study_episode_408 import build_payload


def _read(path):
    return json.loads(Path(path).read_bytes())


def supplement_evidence(body, repo, *, fault_injector=None):
    required = {"learning_request_file", "expected_user_text", "start_anchor"}
    if set(body) != required or any(not isinstance(body[k], str) or not body[k] for k in required):
        raise ValueError("D_evidence_input_invalid")
    repo = Path(repo).resolve()
    operation_root = native.writer.private_root(repo)
    private = episodes._default_package_root(repo)
    original_request = Path(body["learning_request_file"]).resolve()
    original_request.relative_to((operation_root / "d-learning").resolve())
    saved = _read(original_request)
    old_result = _read(original_request.with_name("result.json"))
    record = old_result["capture"]["episode_receipts"][0]
    proof = episodes.verify_episode_capture(repo, record["receipt_ref"])
    root_ref = dict(locator=proof["conversation_package_locator"], sha256=proof["conversation_package_sha256"])
    origin = packages.verify_package_binding(root_ref["locator"], expected_canonical_sha256=root_ref["sha256"],
                                             require_sealed=True, private_root=private)
    original_source = origin["source"]["source"]
    display = original_source["display_binding"]
    if (display.get("schema") != "cs408-D-supplement-display-v1"
            or display["receipt_sha256"] != saved["input"]["display_receipt_sha256"]
            or original_source["episode_id"] != proof["episode_id"]
            or proof["capture_id"] != old_result["capture_id"]):
        raise ValueError("D_evidence_original_binding_drift")
    material_sha = native.writer.digest(body)
    directory = operation_root / "d-evidence-supplements" / material_sha
    request_file, result_file = directory / "request.json", directory / "result.json"
    capture_id = proof["capture_id"]
    with episodes._operation_lock(operation_root / "d-evidence-supplements", capture_id):
        if request_file.exists():
            request = _read(request_file)
            if request["input"] != body:
                raise ValueError("D_evidence_request_conflict")
        else:
            state = captures.replay(captures._load_events(captures.capture_root(repo)))
            capture = state["captures"][capture_id]
            if capture["quality_status"] != "awaiting_web_enhancement":
                raise ValueError("D_evidence_requires_unconsumed_capture")
            parent_ref = (capture.get("supplement_history") or [root_ref])[-1]
            parent = packages.verify_package_binding(parent_ref["locator"],
                expected_canonical_sha256=parent_ref["sha256"], require_sealed=True, private_root=private)
            _, rows = messages._thread_messages()
            source = rows[-1]["_source"]
            payload = build_payload(path=Path(source["transcript_path"]), thread_id=source["thread_id"],
                client=source["client"], start_anchor=body["start_anchor"], end_text=body["expected_user_text"],
                require_latest=True, feedback_anchor=None)
            ids = payload["source"]["transcript_marker"]["message_ids"]
            selected = [row for row in rows if row["id"] in set(ids)]
            if [row["id"] for row in selected] != ids:
                raise ValueError("D_evidence_native_range_changed")
            old_turns = parent["conversation"]["turns"]
            if (old_turns[-1].get("observed_at") and selected[0].get("timestamp")
                    and selected[0]["timestamp"] < old_turns[-1]["observed_at"]):
                raise ValueError("D_evidence_range_overlaps_parent")
            assets = messages._assets(selected, directory / "source-evidence", complete=False,
                                      turn_offset=len(old_turns))
            request = dict(input=body, parent=parent_ref, turns=payload["turns"],
                authorization=payload["authorization"], source_message_refs=[messages._proof(row) for row in selected],
                assets=assets)
            episodes._atomic_write(request_file, native.writer.encoded(request))
        parent_ref = request["parent"]
        parent = packages.verify_package_binding(parent_ref["locator"],
            expected_canonical_sha256=parent_ref["sha256"], require_sealed=True, private_root=private)
        source = {**parent["source"]["source"], "evidence_revision_kind": "d_episode_supplement",
            "episode_capture_id": capture_id, "root_package_locator": root_ref["locator"],
            "root_package_sha256": root_ref["sha256"], "parent_package_locator": parent_ref["locator"],
            "parent_package_sha256": parent_ref["sha256"], "supplement_authorization": request["authorization"]}
        turns = [{k: v for k, v in t.items() if k != "ordinal"} for t in parent["conversation"]["turns"]] + request["turns"]
        attachments = [dict(data=(Path(parent["package_dir"]) / a["relative_path"]).read_bytes(),
                       **{k: a[k] for k in ("role", "mime_type", "label")}) for a in parent["manifest"]["attachments"]]
        attachments += [{k: a[k] for k in ("data", "role", "mime_type", "label")}
                        for a in episodes._normalize_attachments(request["assets"])]
        date, session, item = proof["study_date"], origin["manifest"]["session_id"], origin["manifest"]["item_id"]
        source_id = "DSUP-" + material_sha
        package_id = packages.package_id_for(study_date=date, session_id=session, item_id=item, source_id=source_id)
        locator = packages.LOCATOR_PREFIX + package_id
        if not (private / date / package_id / "receipt.json").exists():
            packages.record_package(study_date=date, session_id=session, item_id=item, source_id=source_id,
                source=source, conversation=turns, frozen_feedback="", attachments=attachments,
                missing_fields=parent["manifest"]["missing_fields"], seal=True, private_root=private)
        opened = packages.verify_package_binding(locator, require_sealed=True, private_root=private)
        if (opened["source"]["source"] != source
                or opened["conversation"]["turns"] != [{"ordinal": i, **t} for i, t in enumerate(turns, 1)]
                or transcripts.verify_package(opened)["coverage"] != "complete"):
            raise ValueError("D_evidence_package_verification_failed")
        if fault_injector:
            fault_injector("after_package")
        ref = dict(locator=locator, sha256=opened["canonical_sha256"])
        event = captures.append_d_evidence_supplement(capture_id, evidence=ref, parent_evidence=parent_ref,
            repo_root=repo, package_root=private, fault_injector=fault_injector)
        result = dict(status="episode_supplemented", capture_id=capture_id, capture_verified=True,
            package_verified=True, conversation_package_locator=locator, conversation_package_sha256=ref["sha256"],
            supplement_event_id=event["event_id"], supplement_request_file=str(request_file), result_file=str(result_file),
            appended_message_count=len(request["turns"]), frontier_advanced=False, navigation_write_count=0,
            formal_write_count=0, fsrs_write_count=0, personal_summary_write_count=0)
        episodes._atomic_write(result_file, native.writer.encoded(result))
        return result
