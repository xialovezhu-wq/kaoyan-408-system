#!/usr/bin/env python3
"""408 最近真实接触日期投影（只读；另有一个 Capture 真题题源登记入口）。

节点总表的“最近复做日期”由正式 new / redo 维护为真实学习日期。按合同，独立正确复习
（correct_review / already_applied）、概念补充里的原题展示、按 concept_only 保存的
日常做题都不会改这一列。凡是要排除“最近做过的题”的入口，都用本模块给出的
最近真实接触日期：

    max(节点总表 首次做题日期 / 最近复做日期,
        复习账本中同一题面的原题作答与正式绑定日期（套用日期更正）,
        已验证的学习者可见展示日期（晨间与概念补充）,
        已核验的 Capture 真题题源绑定日期,
        年度学习记录中的真实做题/明确自报日期，不含计划日期)

结果分两份：by_identity 按精确题面（EXAM408 真题号、FOS 原题题面号），
by_node 按正式节点（只认该节点自己的题面）。未知日期表示本地没有任何真实接触
记录，既不代表更久未做，也不代表近期做过。没有 true_item_id 的早期展示回执
（2026-08 以前的资产/题干身份）不在这里映射。

用法：
  python3 scripts/recent_contact_408.py contacts --date 2026-09-29 [--node ID] [--identity EXAM408-...] [--json]
  python3 scripts/recent_contact_408.py lag --date 2026-09-29 [--json]
  python3 scripts/recent_contact_408.py unbound-captures --since 2026-09-15
  python3 scripts/recent_contact_408.py bind-capture --capture-id CAP-... --canonical-id EXAM408-YYYY-QNN \\
      --source-commit <40hex> --basis "核对依据" [--reviewed-by NAME]

bind-capture 给 concept_only 做题登记主题目身份；bind-result-exposure 按已封存原话
及精确 turn SHA 补记同父其他小问的实际结果曝光，保留主题目绑定。
两者仅写 .private/exam-source-bindings-v1 的身份/曝光投影，不新增作答、错误次数、
掌握度或 FSRS。兄弟小问或同知识点关联本身不构成曝光。
"""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
import question_exposure_408 as question_exposure  # noqa: E402
from start_morning_coverage_campaign_408 import (  # noqa: E402
    CANONICAL_RE,
    DETAIL_LOCATOR_RE,
    FORMAL_NODE_RE,
)

import runtime_paths_408 as _paths

SCHEMA = "cs408-real-contact-projection-v1"
RECOMMENDATION_COOLDOWN_DAYS = 14
BINDING_SCHEMA = "reviewed-capture-exam-source-v1"
DEFAULT_DETAILS_ROOT = _paths.details_root()
MASTER_REL = Path("节点总表.md")
LEDGER_REL = Path("wiki/study_vaults/408-full/state/review-loop/events.jsonl")
BINDING_REL = Path(".private/exam-source-bindings-v1")
FOS_MANIFEST_REL = Path("formal-original-surfaces/v1/published/manifests")
FOS_RE = re.compile(r"^FOS-[0-9A-F]{24}$")
SHA1_RE = re.compile(r"^[0-9a-f]{40}$")
EPOCH = dt.date(1970, 1, 1)
DERIVED_PRESENTATIONS = {
    "variant", "variant_question", "derived_variant", "derived_d0", "derived_review_check",
}
# 同一天有多个来源时，保留更直接的那条作为展示依据。
BASIS_RANK = {
    "annual_self_reported_contact": 0,
    "annual_recorded_study_contact": 0,
    "verified_actual_display": 4,
    "verified_additional_result_exposure": 4,
    "review_ledger_outcome": 3,
    "review_ledger_formal_binding": 3,
    "verified_capture_exam_source": 2,
    "formal_node_date": 1,
}
BASIS_LABEL = {
    "annual_self_reported_contact": "年度真实做题自报",
    "annual_recorded_study_contact": "年度已记录学习接触",
    "verified_actual_display": "已验证真实展示",
    "verified_additional_result_exposure": "已核验问答结果曝光",
    "review_ledger_outcome": "复习账本作答",
    "review_ledger_formal_binding": "复习账本正式绑定",
    "verified_capture_exam_source": "已核验做题题源",
    "formal_node_date": "节点总表日期",
}


class RecentContactError(RuntimeError):
    """稳定的 fail-closed 错误。"""


def _date(value: object) -> dt.date | None:
    text = str(value or "").strip()
    try:
        return dt.date.fromisoformat(text) if len(text) == 10 else None
    except ValueError:
        return None


def latest_date(*values: object) -> str | None:
    """返回若干日期里最晚的一个（YYYY-MM-DD）；都无效时返回 None。"""
    dates = [d for d in (_date(v) for v in values) if d is not None]
    return max(dates).isoformat() if dates else None


def gap_days(fact: dict[str, Any] | None, day: dt.date) -> int | None:
    observed = _date((fact or {}).get("study_date"))
    return None if observed is None else (day - observed).days


def latest_contact(*facts: dict[str, Any] | None) -> dict[str, Any] | None:
    """Keep the newest verified fact; malformed evidence cannot become unknown."""
    result = None
    for fact in facts:
        if not fact or fact.get("study_date") is None:
            continue
        if _date(fact["study_date"]) is None:
            raise RecentContactError("real_contact_study_date_invalid")
        if result is None or fact["study_date"] > result["study_date"]:
            result = fact
    return result


def apply_cooldown(eligibility: dict[str, Any], contacts: dict[str, Any],
                   day: dt.date, window: int) -> dict[str, Any]:
    """Overlay one contact/window policy without reopening any rejected candidate."""
    result = copy.deepcopy(eligibility)

    def project_contact(fact):
        projected = copy.deepcopy(fact)
        observed = _date((projected or {}).get("study_date"))
        if observed is not None:
            if "eligible_on" in projected:
                projected["eligible_on"] = (observed + dt.timedelta(days=window)).isoformat()
            if "gap_days" in projected:
                projected["gap_days"] = (day - observed).days
        return projected

    merged = dict(result.get("last_true_exam_contacts") or {})
    for identity, fact in contacts.items():
        merged[identity] = latest_contact(merged.get(identity), fact)
    for rows in result.get("candidates_by_formal_id", {}).values():
        for row in rows:
            identity = row["true_item_id"]
            merged[identity] = latest_contact(merged.get(identity), row.get("last_learning_contact"))
    merged = {identity: project_contact(fact) for identity, fact in merged.items() if fact is not None}
    for rows in result.get("candidates_by_formal_id", {}).values():
        for row in rows:
            fact = merged.get(row["true_item_id"])
            row["last_learning_contact"] = fact
            if isinstance(row.get("formal_study_evidence"), dict):
                row["formal_study_evidence"] = project_contact(row["formal_study_evidence"])
            row["recent_contact_exclusion_days"] = window
            gap = gap_days(fact, day)
            if gap is not None:
                row["cooldown_gap_days"] = gap
                row["eligible_on"] = (
                    (_date(fact["study_date"]) + dt.timedelta(days=window)).isoformat()
                    if gap < window else None
                )
            else:
                # A date computed for an older window or an associated anchor
                # is not a verified contact date for this exact surface.
                row["eligible_on"] = None
            if gap is not None and gap < window:
                row["eligible"] = False
                row["rejection_reasons"] = list(dict.fromkeys([
                    *row.get("rejection_reasons", []), "recent_real_contact_cooldown"]))
    result["last_true_exam_contacts"] = merged
    result["exposure_window_days"] = window
    result["window_days"] = window
    result["recent_contact_exclusion_days"] = window
    if isinstance(result.get("recent_exact_formal_study"), dict):
        result["recent_exact_formal_study"] = {
            identity: project_contact(fact)
            for identity, fact in result["recent_exact_formal_study"].items()
        }
    if str(result.get("selection_policy", "")).startswith("true_exams_only_exact_item_cooldown_"):
        result["selection_policy"] = f"true_exams_only_exact_item_cooldown_{window}_days"
    material = json.dumps({"as_of": day.isoformat(), "window": window, "contacts": merged},
                          ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    result.setdefault("snapshot", {})["real_contact_sha256"] = hashlib.sha256(material).hexdigest()
    return result


def _offer(target: dict[str, dict[str, Any]], key: str, observed: dt.date | None,
           basis: str, evidence: str, day: dt.date) -> None:
    if not key or observed is None or observed > day:
        return
    candidate = {"study_date": observed.isoformat(), "basis": basis, "evidence": evidence}
    current = target.get(key)
    if current is None or (
        (candidate["study_date"], BASIS_RANK[basis], evidence)
        > (current["study_date"], BASIS_RANK[current["basis"]], current["evidence"])
    ):
        target[key] = candidate


def _master_rows(repo: Path) -> dict[str, dict[str, str]]:
    path = repo / MASTER_REL
    if not path.is_file():
        return {}
    header: list[str] | None = None
    rows: dict[str, dict[str, str]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not (stripped.startswith("|") and stripped.endswith("|")):
            continue
        cells = [cell.strip() for cell in stripped[1:-1].split("|")]
        if header is None:
            if cells and cells[0] == "ID":
                header = cells
            continue
        if not FORMAL_NODE_RE.fullmatch(cells[0]):
            continue
        if len(cells) != len(header):
            raise RecentContactError(f"节点总表 {cells[0]} 列数异常")
        if cells[0] in rows:
            raise RecentContactError(f"节点总表 ID 重复：{cells[0]}")
        rows[cells[0]] = dict(zip(header, cells))
    return rows


def node_identities(repo: Path, details_root: Path | None,
                    rows: dict[str, dict[str, str]] | None = None) -> dict[str, set[str]]:
    """每个正式节点自己的精确题面身份：规范真题号、FOS 原题题面号和节点 ID 本身。"""
    rows = _master_rows(repo) if rows is None else rows
    identities: dict[str, set[str]] = {}
    for node_id, row in rows.items():
        found = {node_id}
        found.update(token for token in re.split(r"[\s；;、，,]+", row.get("来源ID", ""))
                     if CANONICAL_RE.fullmatch(token))
        found.update(DETAIL_LOCATOR_RE.findall(row.get("详情入口", "")))
        identities[node_id] = found
    manifest_root = details_root / FOS_MANIFEST_REL if details_root is not None else None
    if manifest_root is not None and manifest_root.is_dir():
        for path in sorted(manifest_root.glob("*.json")):
            raw = path.read_bytes()
            if path.is_symlink() or hashlib.sha256(raw).hexdigest() != path.stem:
                raise RecentContactError("formal_original_identity_hash_drift")
            manifest = json.loads(raw)
            node_id = str(manifest.get("formal_node_id") or "")
            true_item_id = str(manifest.get("true_item_id") or "")
            if node_id in identities and FOS_RE.fullmatch(true_item_id):
                identities[node_id].add(true_item_id)
    return identities


def _ledger_events(repo: Path) -> list[dict[str, Any]]:
    path = repo / LEDGER_REL
    if not path.is_file():
        return []
    text = path.read_text(encoding="utf-8")
    lines = text.split("\n")
    if not text.endswith("\n"):
        # 写入方正在追加的最后半行不算已提交事件。
        lines = lines[:-1]
    events = []
    for number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise RecentContactError(f"复习账本第 {number} 行不是 JSON") from exc
    return events


def _ledger_basis(event: dict[str, Any]) -> str | None:
    kind = event.get("event_kind")
    if (event.get("practice_mode") == "variant" or event.get("variant_id")
            or event.get("surface_kind") == "verified_variant"
            or event.get("presentation_kind") in DERIVED_PRESENTATIONS):
        return None
    if kind == "formal_review_binding_v1":
        return "review_ledger_formal_binding"
    if kind != "outcome" or event.get("question_valid") is not True:
        return None
    return "review_ledger_outcome"


def _is_surface_identity(value: str) -> bool:
    return bool(CANONICAL_RE.fullmatch(value) or FOS_RE.fullmatch(value))


def real_contacts(repo: str | Path, day: dt.date, *, details_root: str | Path | None = None,
                  strict: bool = False, hot_capture_proof: dict[str, Any] | None = None) -> dict[str, Any]:
    """计算截至 day（含当天）的最近真实接触。

    strict=True 时任何来源读取失败都直接抛错（晨间选题与回包复核使用）；
    strict=False 时展示层或题源绑定层失败只记 warnings（手动推荐使用）。
    节点总表与复习账本读取失败始终抛错。
    """
    repo = Path(repo).expanduser().resolve()
    if details_root is None and repo == question_exposure.CANONICAL_REPO_ROOT.resolve():
        details_root = DEFAULT_DETAILS_ROOT
    details = Path(details_root).expanduser().resolve() if details_root is not None else None
    rows = _master_rows(repo)
    identities = node_identities(repo, details, rows)
    by_identity: dict[str, dict[str, Any]] = {}
    by_node: dict[str, dict[str, Any]] = {}
    warnings: list[str] = []
    counts = {"formal_node_date": 0, "review_ledger": 0, "verified_actual_display": 0,
              "verified_capture_exam_source": 0, "verified_additional_result_exposure": 0,
              "annual_self_reported_contact": 0, "annual_recorded_study_contact": 0}

    for node_id, row in rows.items():
        observed = _date(latest_date(row.get("首次做题日期"), row.get("最近复做日期")))
        if observed is None:
            continue
        counts["formal_node_date"] += 1
        evidence = f"节点总表.md#{node_id}"
        _offer(by_node, node_id, observed, "formal_node_date", evidence, day)
        for value in identities[node_id]:
            if _is_surface_identity(value):
                _offer(by_identity, value, observed, "formal_node_date", evidence, day)

    events = _ledger_events(repo)
    events_by_id = {event.get("event_id"): event for event in events}
    corrections = {
        str(event["source_event_id"]): str(value)
        for event in events
        if event.get("event_kind") == "date_correction" and event.get("source_event_id")
        and (value := event.get("effective_observed_date") or event.get("actual_observed_date")
             or event.get("corrected_observed_date"))
    }
    for event in events:
        basis = _ledger_basis(event)
        if basis is None:
            continue
        original = events_by_id.get(event.get("source_review_event_id"))
        if original and original.get("event_kind") == "outcome" and _ledger_basis(original) is None:
            continue
        event_id = str(event.get("event_id") or "")
        observed = _date(corrections.get(event_id) or corrections.get(event.get("source_review_event_id"))
                         or event.get("observed_date"))
        if observed is None:
            continue
        counts["review_ledger"] += 1
        source_id = str(event.get("true_item_id") or event.get("source_id") or "")
        node_id = str(event.get("formal_node_id") or "")
        if _is_surface_identity(source_id):
            _offer(by_identity, source_id, observed, basis, event_id, day)
        if source_id in identities.get(node_id, ()):
            _offer(by_node, node_id, observed, basis, event_id, day)
        # Older original attempts can name the node itself. Only its verified
        # exact aliases inherit this date; related/mapped nodes never do.
        if source_id in identities:
            _offer(by_node, source_id, observed, basis, event_id, day)
            for identity in identities[source_id]:
                if _is_surface_identity(identity):
                    _offer(by_identity, identity, observed, basis, event_id, day)

    try:
        exposure = question_exposure.recent_exposure_set(
            repo, day, window_days=(day - EPOCH).days + 1)
        for row in exposure["rows"]:
            identity = str(row.get("true_item_id") or "")
            if not identity:
                continue
            observed = _date(row.get("review_date"))
            counts["verified_actual_display"] += 1
            evidence = str(row.get("receipt_sha256") or "")
            _offer(by_identity, identity, observed, "verified_actual_display", evidence, day)
    except (question_exposure.QuestionExposureError, OSError, ValueError, KeyError) as exc:
        if strict:
            raise
        warnings.append(f"真实展示记录不可读，已退化为节点总表+复习账本：{exc}")

    try:
        from morning_question_policy_408 import recent_verified_capture_exams
        proof_args = {"hot_capture_proof": hot_capture_proof} if hot_capture_proof is not None else {}
        bindings = recent_verified_capture_exams(repo, day.isoformat(), window_days=None, **proof_args)
    except Exception as exc:  # noqa: BLE001 - 热证明发生漂移时不允许静默退化
        if strict or hot_capture_proof is not None:
            raise
        warnings.append(f"Capture 真题题源绑定不可读：{exc}")
        bindings = {}
    for identity, fact in bindings.items():
        observed = _date(fact.get("study_date"))
        additional = fact.get("binding_role") == "additional_result_exposure"
        basis = "verified_additional_result_exposure" if additional else "verified_capture_exam_source"
        counts[basis] += 1
        evidence = str(fact.get("source_binding_sha256") if additional else fact.get("capture_id") or "")
        _offer(by_identity, identity, observed, basis, evidence, day)

    # A whole-paper self report is a real contact even when correct questions
    # were not discussed or given separate Captures. Never synthesize displays,
    # attempts or scores to make the cooldown see these existing facts.
    from exam_year_contact_408 import collect_contacts
    annual = collect_contacts(repo, day)  # Source failures remain fail-closed.
    for identity, fact in annual['by_identity'].items():
        basis = ('annual_self_reported_contact' if fact['source_role'] == 'self_report'
                 else 'annual_recorded_study_contact')
        counts[basis] += 1
        _offer(by_identity, identity, _date(fact['study_date']), basis, fact['evidence'], day)
        selected = by_identity.get(identity)
        if selected and selected['basis'] == basis and selected['evidence'] == fact['evidence']:
            selected.update(source_role=fact['source_role'], source_record=fact['source_record'],
                            independent_mastery_inferred=False)

    # A canonical question may have multiple historical node aliases. Share
    # contact only across these proven identities, never through topic edges.
    for node_id, values in identities.items():
        for identity in values:
            fact = by_identity.get(identity)
            if fact:
                _offer(by_node, node_id, _date(fact["study_date"]), fact["basis"], fact["evidence"], day)
        fact = by_node.get(node_id)
        if fact:
            for identity in values:
                if _is_surface_identity(identity):
                    _offer(by_identity, identity, _date(fact["study_date"]), fact["basis"], fact["evidence"], day)

    from exam_practice_units_408 import load_units, contact_projection
    unit_details = details_root or (DEFAULT_DETAILS_ROOT if repo == question_exposure.CANONICAL_REPO_ROOT.resolve() else None)
    if unit_details is not None:
        by_identity = contact_projection(by_identity, load_units(unit_details).values())

    return {"schema": SCHEMA, "as_of": day.isoformat(), "by_identity": by_identity,
            "by_node": by_node, "source_counts": counts, "warnings": warnings,
            "annual_record_sources": annual['record_files'],
            "annual_unknown_date_records": annual['unknown_date_records']}


def effective_node_last(row: dict[str, str], fact: dict[str, Any] | None) -> str:
    """节点的有效最近日期：正式列与真实接触取较晚者；都没有时保留正式列原文（如“未记录”）。"""
    formal = row.get("最近复做日期", "")
    return latest_date(formal, row.get("首次做题日期"), (fact or {}).get("study_date")) or formal


def describe(fact: dict[str, Any] | None) -> str:
    if not fact:
        return "无真实接触记录"
    return f"{fact['study_date']}（{BASIS_LABEL.get(fact['basis'], fact['basis'])}）"


# ---------------------------------------------------------------- 写入口：Capture 真题题源绑定

def bind_capture(repo: str | Path, *, capture_id: str, canonical_id: str, source_commit: str,
                 basis: str, reviewed_by: str, allow_pending_review: bool = False) -> Path:
    """登记本地已核题源；显式待审入口只补身份，不提交学习或评分。"""
    repo = Path(repo).expanduser().resolve()
    if not CANONICAL_RE.fullmatch(canonical_id):
        raise RecentContactError(f"不是规范真题号：{canonical_id}")
    if not SHA1_RE.fullmatch(source_commit):
        raise RecentContactError("source_commit 必须是 40 位快照号")
    if len(basis.strip()) < 10:
        raise RecentContactError("basis 需写明核对依据（至少 10 个字符）")
    import intake_fact_capture_408 as captures
    import study_bridge_408 as published
    state = captures.replay(captures._load_events(captures.capture_root(repo)))["captures"]
    capture = state.get(capture_id)
    if not capture:
        raise RecentContactError(f"Capture 不存在：{capture_id}")
    if capture.get("formal_id"):
        raise RecentContactError(
            f"{capture_id} 已绑定正式节点 {capture['formal_id']}，由节点总表日期覆盖，不另登记")
    pending_review = (allow_pending_review
                      and capture.get("quality_status") == "awaiting_web_enhancement")
    if capture.get("quality_status") != "concept_only" and not pending_review:
        raise RecentContactError(
            f"{capture_id} 的状态是 {capture.get('quality_status')}，只登记 concept_only 做题")
    pending_proofs = []
    if pending_review:
        facts = capture.get("capture", {}).get("source_facts") or {}
        if (facts.get("execution_kind") != "standalone_question"
                or facts.get("source_id") != canonical_id or not reviewed_by.strip()):
            raise RecentContactError("pending_source_review_requires_matching_standalone_identity")
        refs = capture.get("capture", {}).get("stable_evidence_refs", [])
        refs = [ref for ref in refs if str(ref.get("locator", "")).startswith("cs408-conversation-package://")]
        if not refs:
            raise RecentContactError("pending_source_review_requires_sealed_package")
        for ref in refs:
            opened = _open_result_exposure_package(repo, ref["locator"], ref["sha256"])
            conversation = opened.get("conversation") or {}
            if (conversation.get("complete_order_preserved") is not True
                    or conversation.get("truncated") is not False):
                raise RecentContactError("pending_source_review_requires_complete_conversation")
            pending_proofs.append({"locator": ref["locator"], "sha256": ref["sha256"]})
    library = published._published_library(source_commit, repo)
    original = {row["question_id"]: row for row in library["original_mappings"]}.get(canonical_id)
    if original is None:
        raise RecentContactError(f"{source_commit} 的发布版没有 {canonical_id}")
    mapping_bytes = (json.dumps(original, ensure_ascii=False, sort_keys=True,
                                separators=(",", ":")) + "\n").encode()
    row = {
        "basis": basis.strip(),
        "canonical_id": canonical_id,
        "capture_id": capture_id,
        "capture_payload_sha256": capture["payload_sha256"],
        "formal_write_count": 0,
        "learner_event_write_count": 0,
        "original_mapping_sha256": hashlib.sha256(mapping_bytes).hexdigest(),
        "reviewed_by": reviewed_by,
        "schema": BINDING_SCHEMA,
        "source_commit": source_commit,
        "study_date": capture["study_date"],
    }
    if pending_review:
        row.update(binding_stage="pending_source_review", sealed_evidence_refs=pending_proofs)
    root = repo / BINDING_REL
    root.mkdir(parents=True, exist_ok=True)
    for path in sorted(root.glob("*.json")):
        existing = json.loads(path.read_bytes())
        if (existing.get("capture_id") == capture_id and existing.get("canonical_id") != canonical_id
                and existing.get("binding_role") != "additional_result_exposure"):
            raise RecentContactError(
                f"{capture_id} 已登记为 {existing.get('canonical_id')}，与 {canonical_id} 冲突")
    raw = (json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()
    path = root / (hashlib.sha256(raw).hexdigest() + ".json")
    if not path.exists():
        path.write_bytes(raw)
    return path


def _open_result_exposure_package(repo: Path, locator: str, expected_sha: str) -> dict[str, Any]:
    from export_capture_day_408 import _open_package
    return _open_package(repo, locator, None, expected_sha)


def validate_result_exposure_binding(repo: Path, row: dict[str, Any], capture: dict[str, Any],
                                     *, verified_immutable_turns: bool = False) -> None:
    """Verify exact prior turns without changing the Capture's primary identity."""
    if row.get("binding_role") != "additional_result_exposure":
        raise RecentContactError("result_exposure_role_invalid")
    primary = str((capture.get("capture", {}).get("source_facts") or {}).get("source_id") or "")
    target = str(row.get("canonical_id") or "")
    if (row.get("source_canonical_id") != primary or "-P" not in primary or "-P" not in target
            or target == primary or primary.rsplit("-P", 1)[0] != target.rsplit("-P", 1)[0]):
        raise RecentContactError("result_exposure_requires_distinct_same_parent_subquestion")
    refs = list(capture.get("capture", {}).get("stable_evidence_refs", [])) + list(capture.get("supplement_history", []))
    binding = (row.get("evidence_locator"), row.get("evidence_sha256"))
    if binding not in {(ref.get("locator"), ref.get("sha256")) for ref in refs if isinstance(ref, dict)}:
        raise RecentContactError("result_exposure_capture_package_binding_drift")
    if verified_immutable_turns:
        return
    opened = _open_result_exposure_package(Path(repo), binding[0], binding[1])
    conversation = opened["conversation"]
    if not conversation.get("complete_order_preserved") or conversation.get("truncated"):
        raise RecentContactError("result_exposure_complete_conversation_required")
    turns = conversation["turns"]
    evidence = row.get("turn_refs") or []
    if not evidence or len({ref.get("turn") for ref in evidence}) != len(evidence):
        raise RecentContactError("result_exposure_unique_turns_required")
    for ref in evidence:
        index = ref.get("turn")
        if type(index) is not int or not 1 <= index <= len(turns):
            raise RecentContactError("result_exposure_turn_invalid")
        turn = turns[index - 1]
        raw = json.dumps(turn, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        if hashlib.sha256(raw).hexdigest() != ref.get("sha256"):
            raise RecentContactError("result_exposure_turn_hash_drift")
        if turn.get("role") not in {"user", "assistant"} or not turn.get("text"):
            raise RecentContactError("result_exposure_original_text_required")
        when = dt.datetime.fromisoformat(str(turn.get("observed_at") or "").replace("Z", "+00:00"))
        if when.tzinfo is None or when.astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat() != row["study_date"]:
            raise RecentContactError("result_exposure_original_date_drift")
    if (row.get("formal_write_count") != 0 or row.get("learner_event_write_count") != 0
            or row.get("independent_mastery_inferred") is not False or len(str(row.get("basis") or "").strip()) < 10):
        raise RecentContactError("result_exposure_is_not_new_learning_or_mastery")


def bind_result_exposure(repo: str | Path, *, capture_id: str, canonical_id: str,
                         source_commit: str, basis: str, turns: list[int], reviewed_by: str) -> Path:
    """Add a reviewed sibling-result exposure; primary mapping and learning stay intact."""
    repo = Path(repo).expanduser().resolve()
    if not CANONICAL_RE.fullmatch(canonical_id) or not SHA1_RE.fullmatch(source_commit):
        raise RecentContactError("result_exposure_identity_invalid")
    import intake_fact_capture_408 as captures
    import study_bridge_408 as published
    state = captures.replay(captures._load_events(captures.capture_root(repo)))["captures"]
    capture = state.get(capture_id) or {}
    if capture.get("quality_status") not in {"concept_only", "curated", "already_current"}:
        raise RecentContactError("result_exposure_requires_formally_closed_capture")
    library = published._published_library(source_commit, repo, original_mappings_only=True)
    original = {row["question_id"]: row for row in library["original_mappings"]}.get(canonical_id)
    if original is None:
        raise RecentContactError("result_exposure_canonical_source_missing")
    refs = [ref for ref in list(capture.get("capture", {}).get("stable_evidence_refs", []))
            + list(capture.get("supplement_history", []))
            if isinstance(ref, dict) and str(ref.get("locator") or "").startswith("cs408-conversation-package://")]
    if not refs:
        raise RecentContactError("result_exposure_package_missing")
    selected = refs[-1]
    opened = _open_result_exposure_package(repo, selected["locator"], selected["sha256"])
    actual = opened["conversation"]["turns"]
    if not turns or any(type(index) is not int or not 1 <= index <= len(actual) for index in turns):
        raise RecentContactError("result_exposure_turn_invalid")
    row = {"schema": BINDING_SCHEMA, "binding_role": "additional_result_exposure",
        "capture_id": capture_id, "capture_payload_sha256": capture["payload_sha256"],
        "canonical_id": canonical_id, "source_canonical_id": capture["capture"]["source_facts"]["source_id"],
        "source_commit": source_commit, "study_date": capture["study_date"],
        "original_mapping_sha256": hashlib.sha256((json.dumps(original, ensure_ascii=False,
            sort_keys=True, separators=(",", ":")) + "\n").encode()).hexdigest(),
        "evidence_locator": selected["locator"], "evidence_sha256": selected["sha256"],
        "turn_refs": [{"turn": index, "sha256": hashlib.sha256(json.dumps(actual[index - 1],
            ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()} for index in sorted(set(turns))],
        "basis": basis.strip(), "reviewed_by": reviewed_by,
        "formal_write_count": 0, "learner_event_write_count": 0, "independent_mastery_inferred": False}
    validate_result_exposure_binding(repo, row, capture)
    raw = (json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()
    root = repo / BINDING_REL
    root.mkdir(parents=True, exist_ok=True)
    path = root / (hashlib.sha256(raw).hexdigest() + ".json")
    if not path.exists():
        path.write_bytes(raw)
    return path


def unbound_concept_captures(repo: str | Path, since: dt.date) -> list[dict[str, Any]]:
    """列出 since 以来没有正式节点、也没有真题题源绑定的 concept_only 独立做题。

    概念补充与晨间原题复习由展示回执覆盖，不列出。
    """
    repo = Path(repo).expanduser().resolve()
    import intake_fact_capture_408 as captures
    state = captures.replay(captures._load_events(captures.capture_root(repo)))["captures"]
    root = repo / BINDING_REL
    bound = {json.loads(path.read_bytes()).get("capture_id")
             for path in root.glob("*.json")} if root.is_dir() else set()
    result = []
    for capture_id, value in sorted(state.items()):
        facts = (value.get("capture") or {}).get("source_facts") or {}
        observed = _date(value.get("study_date"))
        if (observed is None or observed < since or value.get("formal_id")
                or value.get("quality_status") != "concept_only"
                or facts.get("execution_kind") in {"concept_supplement", "original_review"}
                or capture_id in bound):
            continue
        result.append({
            "capture_id": capture_id, "study_date": value.get("study_date"),
            "execution_kind": facts.get("execution_kind"),
            "evidence": [ref.get("locator") for ref in
                         (value.get("capture") or {}).get("stable_evidence_refs", [])],
        })
    return result


def lag_report(repo: str | Path, day: dt.date) -> dict[str, Any]:
    """正式列落后于同题面真实接触的节点（按合同预期存在，不是写入失败）。"""
    repo = Path(repo).expanduser().resolve()
    projection = real_contacts(repo, day)
    nodes = []
    for node_id, row in _master_rows(repo).items():
        fact = projection["by_node"].get(node_id)
        formal = _date(row.get("最近复做日期"))
        real = _date((fact or {}).get("study_date"))
        if real is not None and (formal is None or real > formal):
            nodes.append({"id": node_id, "formal_last_review_date": row.get("最近复做日期"),
                          "real_last_contact": fact, "within_7_days": (day - real).days < 7})
    return {"as_of": day.isoformat(), "lagging_node_count": len(nodes),
            "within_7_days_count": sum(1 for row in nodes if row["within_7_days"]),
            "nodes": nodes, "warnings": projection["warnings"]}


def main() -> None:
    parser = argparse.ArgumentParser(description="408 最近真实接触日期（只读投影与 Capture 题源登记）")
    parser.add_argument("--repo", default=str(Path(__file__).resolve().parents[1]))
    sub = parser.add_subparsers(dest="command", required=True)
    contacts = sub.add_parser("contacts")
    contacts.add_argument("--date", required=True)
    contacts.add_argument("--node", action="append", default=[])
    contacts.add_argument("--identity", action="append", default=[])
    contacts.add_argument("--json", action="store_true")
    lag = sub.add_parser("lag")
    lag.add_argument("--date", required=True)
    lag.add_argument("--json", action="store_true")
    unbound = sub.add_parser("unbound-captures")
    unbound.add_argument("--since", required=True)
    bind = sub.add_parser("bind-capture")
    bind.add_argument("--capture-id", required=True)
    bind.add_argument("--canonical-id", required=True)
    bind.add_argument("--source-commit", required=True)
    bind.add_argument("--basis", required=True)
    bind.add_argument("--reviewed-by", default="local-claude-code-source-review")
    bind.add_argument("--allow-pending-review", action="store_true",
                      help="仅本地已核原图的待增强standalone；只补题源身份，不评分或正式入库")
    result = sub.add_parser("bind-result-exposure")
    result.add_argument("--capture-id", required=True)
    result.add_argument("--canonical-id", required=True)
    result.add_argument("--source-commit", required=True)
    result.add_argument("--basis", required=True)
    result.add_argument("--turn", action="append", type=int, required=True)
    result.add_argument("--reviewed-by", default="local-codex-result-exposure-review")
    args = parser.parse_args()

    if args.command == "contacts":
        projection = real_contacts(args.repo, dt.date.fromisoformat(args.date))
        if args.node or args.identity:
            selected = {"by_node": {k: projection["by_node"].get(k) for k in args.node},
                        "by_identity": {k: projection["by_identity"].get(k) for k in args.identity}}
        else:
            selected = projection
        if args.json:
            print(json.dumps(selected, ensure_ascii=False, indent=2))
        else:
            for key, fact in {**selected["by_node"], **selected["by_identity"]}.items():
                print(f"{key}：最近真实接触 {describe(fact)}")
            for warning in projection["warnings"]:
                print("[警告]", warning)
    elif args.command == "lag":
        report = lag_report(args.repo, dt.date.fromisoformat(args.date))
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            print(f"正式列落后于真实接触：{report['lagging_node_count']} 个节点；"
                  f"其中近 7 天内有真实接触 {report['within_7_days_count']} 个（按合同预期，不是写入失败）")
    elif args.command == "unbound-captures":
        rows = unbound_concept_captures(args.repo, dt.date.fromisoformat(args.since))
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    elif args.command == "bind-capture":
        path = bind_capture(args.repo, capture_id=args.capture_id, canonical_id=args.canonical_id,
                            source_commit=args.source_commit, basis=args.basis,
                            reviewed_by=args.reviewed_by, allow_pending_review=args.allow_pending_review)
        print(path)
    elif args.command == "bind-result-exposure":
        print(bind_result_exposure(args.repo, capture_id=args.capture_id, canonical_id=args.canonical_id,
            source_commit=args.source_commit, basis=args.basis, turns=args.turn, reviewed_by=args.reviewed_by))


if __name__ == "__main__":
    main()
