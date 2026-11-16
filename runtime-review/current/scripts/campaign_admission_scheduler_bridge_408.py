#!/usr/bin/env python3
"""Bridge a verified formal admission into the next unfrozen scheduler projection."""
from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import tempfile
from typing import Any

import campaign_admission_backflow_408 as admission
import morning_coverage_campaign_408 as scheduler


class SchedulerBridgeError(RuntimeError):
    pass


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(scheduler._canonical_bytes(value))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _formal_row(repo: Path, formal_node_id: str) -> tuple[str, str]:
    header, rows = scheduler._markdown_rows(repo / "节点总表.md")
    matches = [row for row in rows if row.get("ID") == formal_node_id]
    if len(matches) != 1:
        raise SchedulerBridgeError("scheduler_bridge_formal_row_not_unique")
    row = matches[0]
    row_sha = scheduler.canonical_sha256({key: row.get(key, "") for key in header})
    review_unit_id = admission._real_review_unit(repo, formal_node_id)
    return row_sha, review_unit_id


def _scheduler_event(
    repo: Path,
    receipt: dict[str, Any],
    receipt_sha256: str,
    ledger_path: Path,
    config: dict[str, Any],
) -> dict[str, Any]:
    formal_node_id = str(receipt["formal_node_id"])
    row_sha, review_unit_id = _formal_row(repo, formal_node_id)
    if review_unit_id != receipt.get("review_unit_id"):
        raise SchedulerBridgeError("scheduler_bridge_review_unit_binding_drift")
    current = scheduler.read_admission_ledger(ledger_path)
    previous = current["latest_by_node"].get(formal_node_id)
    if previous is not None and previous.get("source_receipt_sha256") == receipt_sha256:
        if previous.get("formal_node_row_sha256") != row_sha:
            raise SchedulerBridgeError("scheduler_bridge_existing_row_binding_drift")
        return previous
    status = receipt.get("status")
    if status == "campaign_node_admitted":
        if previous is not None:
            raise SchedulerBridgeError("scheduler_bridge_new_node_already_admitted")
        event_kind = "campaign_node_admitted"
    elif status == "campaign_node_refreshed":
        event_kind = "campaign_node_refreshed"
        if previous is None:
            _, rows = scheduler._markdown_rows(repo / "节点总表.md")
            native_history = admission._read_events(repo)
            prior_native_admission = any(
                row.get("formal_node_id") == formal_node_id
                and row.get("event_type") == "campaign_node_admitted"
                for row in native_history
            )
            if len(rows) < int(config["cohort_expected_count"]) or prior_native_admission:
                raise SchedulerBridgeError("scheduler_bridge_baseline_membership_unproven")
    else:
        raise SchedulerBridgeError("scheduler_bridge_admission_status_invalid")
    event = {
        "schema": scheduler.ADMISSION_SCHEMA,
        "event_id": "RCAE-" + receipt_sha256[:24],
        "event_kind": event_kind,
        "event_time": str(receipt["study_date"]) + "T23:59:59+08:00",
        "formal_node_id": formal_node_id,
        "formal_node_row_sha256": row_sha,
        "status": "succeeded",
        "idempotency_key": "formal-admission-receipt:" + receipt_sha256,
        "source_receipt_ref": str(receipt["object_path"]),
        "source_receipt_sha256": receipt_sha256,
        "previous_admission_event_id": None if previous is None else previous["event_id"],
        "target_run_policy": "next_unfrozen_run_only",
        "frozen_run_mutation_count": 0,
        "active_run_mutation_count": 0,
        "formal_write_count": 0,
    }
    if event_kind == "campaign_node_refreshed" and previous is None:
        event["baseline_member"] = True
    event["event_sha256"] = scheduler.canonical_sha256(event)
    return event


def _append_scheduler_event(path: Path, event: dict[str, Any]) -> dict[str, Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        current = scheduler.read_admission_ledger(path)
        prior = [row for row in current["events"] if row["idempotency_key"] == event["idempotency_key"]]
        if prior:
            if len(prior) != 1 or prior[0] != event:
                raise SchedulerBridgeError("scheduler_bridge_idempotency_conflict")
            return prior[0]
        latest = current["latest_by_node"].get(event["formal_node_id"])
        if event["event_kind"] == "campaign_node_admitted" and latest is not None:
            raise SchedulerBridgeError("scheduler_bridge_admission_chain_changed")
        if event["event_kind"] == "campaign_node_refreshed" and event.get(
            "previous_admission_event_id"
        ) != (None if latest is None else latest["event_id"]):
            raise SchedulerBridgeError("scheduler_bridge_admission_chain_changed")
        with path.open("ab") as stream:
            stream.write(scheduler._canonical_bytes(event))
            stream.flush()
            os.fsync(stream.fileno())
        scheduler.read_admission_ledger(path)
        return event
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def register_admission(repo_root: str | Path, *, admission_receipt_sha256: str) -> dict[str, Any]:
    """Register a verified new batch member before checking whole-cohort forecasts."""
    repo = Path(repo_root).expanduser().resolve(strict=True)
    receipt = admission.validate_admission_receipt(repo, admission_receipt_sha256)
    if (receipt.get("target_run_policy") != "next_unfrozen_run_only"
            or receipt.get("frozen_run_mutation_allowed") is not False
            or receipt.get("active_run_mutation_allowed") is not False):
        raise SchedulerBridgeError("scheduler_bridge_next_unfrozen_boundary_required")
    config = scheduler.read_config(None)
    if receipt.get("campaign_id") != config["campaign_id"]:
        raise SchedulerBridgeError("scheduler_bridge_campaign_identity_drift")
    ledger = repo / scheduler.DEFAULT_ADMISSION_REL
    event = _scheduler_event(repo, receipt, admission_receipt_sha256, ledger, config)
    return _append_scheduler_event(ledger, event)


def bridge(
    repo_root: str | Path,
    *,
    admission_receipt_sha256: str,
    target_date: str,
    output_dir: str | Path,
    config_path: str | Path | None = None,
    admission_ledger: str | Path | None = None,
) -> dict[str, Any]:
    repo = Path(repo_root).expanduser().resolve(strict=True)
    day = scheduler.parse_date(target_date, "target_date")
    output = Path(output_dir).expanduser().resolve()
    if output.is_symlink():
        raise SchedulerBridgeError("scheduler_bridge_output_symlink_forbidden")
    config_ref = None if config_path is None else Path(config_path).expanduser().resolve(strict=True)
    if admission_ledger is None:
        ledger = repo / scheduler.DEFAULT_ADMISSION_REL
    else:
        ledger = Path(admission_ledger).expanduser()
        ledger = (repo / ledger).resolve() if not ledger.is_absolute() else ledger.resolve()
    receipt = admission.validate_admission_receipt(repo, admission_receipt_sha256)
    if receipt.get("target_run_policy") != "next_unfrozen_run_only" or receipt.get(
        "frozen_run_mutation_allowed"
    ) is not False or receipt.get("active_run_mutation_allowed") is not False:
        raise SchedulerBridgeError("scheduler_bridge_next_unfrozen_boundary_required")
    config = scheduler.read_config(config_ref)
    if receipt.get("campaign_id") != config["campaign_id"]:
        raise SchedulerBridgeError("scheduler_bridge_campaign_identity_drift")
    event = _scheduler_event(repo, receipt, admission_receipt_sha256, ledger, config)
    pending = admission.refresh_campaign_projection(
        repo, admission_receipt_sha256=admission_receipt_sha256
    )
    if pending.get("status") != admission.STATUS_FORECAST_PENDING:
        raise SchedulerBridgeError("scheduler_bridge_refresh_request_not_pending")
    event = _append_scheduler_event(ledger, event)
    raw_admissions = scheduler.read_admission_ledger(ledger)
    scheduler_handoff = {
        "schema": scheduler.REFRESH_HANDOFF_SCHEMA,
        "request_id": "scheduler-refresh:" + pending["refresh_request_sha256"][:24],
        "admission_ledger_sha256": raw_admissions["sha256"],
        "trigger_admission_event_id": event["event_id"],
        "trigger_admission_event_sha256": event["event_sha256"],
    }
    scheduler_handoff_path = output / "review-campaign-refresh-handoff.json"
    _atomic_json(scheduler_handoff_path, scheduler_handoff)
    config, cohort, memory, bindings, normalized_admissions = scheduler.load_projection(
        repo, config_ref, day, ledger
    )
    validated_handoff = scheduler.validate_refresh_handoff(
        scheduler_handoff_path, normalized_admissions
    )
    due_run = scheduler.build_due_run(
        cohort, memory, config, day, source_bindings=bindings, refresh_handoff=validated_handoff
    )
    forecast = scheduler.build_forecast(
        cohort, memory, config, refresh_handoff=validated_handoff
    )
    due_path = output / f"morning-due-run-{day.isoformat()}.json"
    forecast_path = output / f"morning-coverage-forecast-{day.isoformat()}.json"
    _atomic_json(due_path, due_run)
    _atomic_json(forecast_path, forecast)
    if scheduler.canonical_sha256(json.loads(due_path.read_text())) != scheduler.canonical_sha256(due_run):
        raise SchedulerBridgeError("scheduler_bridge_due_run_readback_failed")
    if scheduler.canonical_sha256(json.loads(forecast_path.read_text())) != scheduler.canonical_sha256(forecast):
        raise SchedulerBridgeError("scheduler_bridge_forecast_readback_failed")
    handoff = {
        "schema": admission.HANDOFF_RECEIPT_SCHEMA,
        "status": "accepted",
        "request_sha256": pending["refresh_request_sha256"],
        "admission_receipt_sha256": admission_receipt_sha256,
        "formal_node_id": receipt["formal_node_id"],
        "campaign_id": receipt["campaign_id"],
        "campaign_cohort_sha256": receipt["campaign_cohort_sha256"],
        "target_run_policy": "next_unfrozen_run_only",
        "frozen_run_mutation_count": 0,
        "active_run_mutation_count": 0,
        "formal_write_count": 0,
        "scheduler_admission_event_id": event["event_id"],
        "scheduler_admission_event_sha256": event["event_sha256"],
        "scheduler_admission_cutoff": cohort["admission_cutoff"],
        "due_run_ref": str(due_path),
        "due_run_sha256": due_run["run_sha256"],
        "forecast_ref": str(forecast_path),
        "forecast_sha256": forecast["forecast_sha256"],
        "created_at": str(receipt["study_date"]) + "T23:59:59+08:00",
    }
    handoff_path = output / "campaign-forecast-handoff-receipt.json"
    _atomic_json(handoff_path, handoff)
    bound = admission.refresh_campaign_projection(
        repo,
        admission_receipt_sha256=admission_receipt_sha256,
        handoff_receipt=handoff_path,
        handoff_receipt_sha256=admission._sha_value(handoff),
    )
    if bound.get("status") != admission.STATUS_COMPLETE:
        raise SchedulerBridgeError("scheduler_bridge_handoff_not_bound")
    result = {
        "status": "SCHEDULER_ADMISSION_FORECAST_BOUND",
        "formal_node_id": receipt["formal_node_id"],
        "scheduler_admission_event_id": event["event_id"],
        "scheduler_admission_event_sha256": event["event_sha256"],
        "admission_ledger": str(ledger),
        "due_run_path": str(due_path),
        "due_run_sha256": due_run["run_sha256"],
        "forecast_path": str(forecast_path),
        "forecast_sha256": forecast["forecast_sha256"],
        "handoff_receipt_path": str(handoff_path),
        "handoff_receipt_sha256": admission._sha_value(handoff),
        "refresh_bound_event_id": bound["refresh_bound_event_id"],
        "target_run_policy": "next_unfrozen_run_only",
        "frozen_run_mutation_count": 0,
        "active_run_mutation_count": 0,
        "formal_write_count": 0,
        "learner_evidence_write_count": 0,
    }
    _atomic_json(output / "bridge-receipt.json", result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--admission-receipt-sha256", required=True)
    parser.add_argument("--target-date", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--admission-ledger", type=Path)
    args = parser.parse_args()
    try:
        result = bridge(
            args.repo,
            admission_receipt_sha256=args.admission_receipt_sha256,
            target_date=args.target_date,
            output_dir=args.output_dir,
            config_path=args.config,
            admission_ledger=args.admission_ledger,
        )
    except (SchedulerBridgeError, admission.AdmissionError, scheduler.CampaignError) as exc:
        print(json.dumps({"status": "BLOCKED", "reason": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
