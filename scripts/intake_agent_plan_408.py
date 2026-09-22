#!/usr/bin/env python3
"""为 408 单题入库生成确定性的 Codex 子智能体计划。

本脚本只回答“当前路径应由几个只读子智能体参与”。它不会调用
``spawn_agent``，不会读取或写入正式仓库，也不会运行 preflight、apply、
closeout 或旧题检索脚本。真正的协作工具调用必须由主 Codex 任务完成。
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass


CONTRACT = "intake_agent_plan_408_v2"
VALID_PATHS = frozenset("ABCDEFG")
QUALITY_REVIEW_NOTE = "多智能体仅用于质量复核，不构成提速保证。"


class AgentPlanError(ValueError):
    """输入组合不符合 A-G 路径合同。"""


@dataclass(frozen=True)
class AgentPlan:
    contract: str
    path: str
    action: str
    worker_count: int
    roles: tuple[str, ...]
    reason: str

    def as_dict(self) -> dict[str, object]:
        value = asdict(self)
        value["roles"] = list(self.roles)
        return value


def _plan(
    path: str,
    action: str,
    roles: tuple[str, ...],
    reason: str,
) -> AgentPlan:
    return AgentPlan(
        contract=CONTRACT,
        path=path,
        action=action,
        worker_count=len(roles),
        roles=roles,
        reason=reason,
    )


def _degrade_to_d(source_path: str, reason: str) -> AgentPlan:
    return _plan(
        path="D",
        action="degrade_D",
        roles=(),
        reason=f"{source_path} 路径证据不足：{reason}；降级为 D 路径只读判断，不启动子智能体。",
    )


def plan_agents(
    path: str,
    *,
    evidence_complete: bool = False,
    complex_redo: bool = False,
    semantic_relation: bool = False,
    historical_unmapped: bool = False,
    quality_risk: bool = False,
    exact_identity_hit: bool = False,
) -> AgentPlan:
    """按固定 A-G 规则返回子智能体计划。

    ``worker_count`` 只计算子智能体，不包含负责冻结证据、唯一 fan-in 和
    唯一正式写入的主智能体。
    """

    normalized = path.strip().upper()
    if normalized not in VALID_PATHS:
        raise AgentPlanError(f"path 必须是 A-G，实际为 {path!r}")
    if complex_redo and normalized != "C":
        raise AgentPlanError("complex_redo 只能用于 C 路径")
    if semantic_relation and normalized != "F":
        raise AgentPlanError("semantic_relation 只能用于 F 路径")
    if historical_unmapped and normalized not in {"A", "B"}:
        raise AgentPlanError("historical_unmapped 只能用于 A 或 B 新题路径")
    if quality_risk and normalized not in {"A", "B", "F"}:
        raise AgentPlanError("quality_risk 只能用于 A、B 或 F 路径")
    if exact_identity_hit and normalized not in {"A", "B"}:
        raise AgentPlanError("exact_identity_hit 只能用于 A 或 B 新题路径")

    if exact_identity_hit:
        return _plan(
            path="D",
            action="reroute_existing",
            roles=(),
            reason=(
                f"{normalized} 路径精确命中既有正式节点；强制停止新题 fan-out，"
                "当前用户意图尚不足以猜测具体维护路径。root 必须先确认是 C redo、"
                "E 详情修复、F 关系维护还是 G 旧题回顾，再进入对应窄路径。"
            ),
        )

    if historical_unmapped:
        if not evidence_complete:
            return _degrade_to_d(normalized, "未映射历史题缺少足以建立正式节点的证据")
        return _plan(
            path=normalized,
            action="spawn_agents",
            roles=("facts_safety", "taxonomy_error", "duplicate_relation"),
            reason=(
                "未映射历史题且证据完整：facts_safety 额外复核三层身份与来源，"
                "并与知识点错因、查重关系两个分支并行；主任务仍负责唯一 fan-in。"
                f"{QUALITY_REVIEW_NOTE}"
            ),
        )

    if normalized == "A":
        if not evidence_complete:
            return _degrade_to_d("A", "durable 详情卡或正式字段尚不足")
        if not quality_risk:
            return _plan(
                path="A",
                action="serial_main",
                roles=(),
                reason=(
                    "A 路径证据完整且低风险；真实 benchmark 未显示稳定提速，"
                    "由主任务串行判断。"
                ),
            )
        return _plan(
            path="A",
            action="spawn_agents",
            roles=("facts_safety", "taxonomy_error", "duplicate_relation"),
            reason=(
                "A 路径证据完整且存在 quality_risk：事实与脱敏、知识点与错因、"
                f"查重与关系需要独立复核。{QUALITY_REVIEW_NOTE}"
            ),
        )

    if normalized == "B":
        if not evidence_complete:
            return _degrade_to_d("B", "临时截图未能支撑全部关键字段")
        if not quality_risk:
            return _plan(
                path="B",
                action="serial_main",
                roles=(),
                reason=(
                    "B 路径证据完整且低风险；主任务保留原始附件并串行完成脱敏与分类。"
                ),
            )
        return _plan(
            path="B",
            action="spawn_agents",
            roles=("facts_safety", "taxonomy_error"),
            reason=(
                "B 路径证据完整且存在 quality_risk：主任务保留原始截图并冻结脱敏快照，"
                "两个子智能体分别复核事实安全和分类错因，查重关系由主任务完成。"
                f"{QUALITY_REVIEW_NOTE}"
            ),
        )

    if normalized == "C":
        if complex_redo:
            return _plan(
                path="C",
                action="spawn_agents",
                roles=("redo_safety", "redo_consistency"),
                reason=(
                    "复杂 redo 涉及新错误机制、脱敏表述或标签一致性，"
                    f"需要两个独立只读复核分支。{QUALITY_REVIEW_NOTE}"
                ),
            )
        return _plan(
            path="C",
            action="serial_main",
            roles=(),
            reason="simple_redo 只追加既有节点记录，由主任务串行处理更稳定。",
        )

    if normalized == "F":
        if semantic_relation and quality_risk:
            return _plan(
                path="F",
                action="spawn_agents",
                roles=("relation_semantics", "relation_graph_guard"),
                reason=(
                    "semantic_relation 存在类型或强度争议且 quality_risk=true："
                    "relation_semantics 复核语义证据，relation_graph_guard"
                    f" 独立检查图约束与反证。{QUALITY_REVIEW_NOTE}"
                ),
            )
        if semantic_relation:
            return _plan(
                path="F",
                action="serial_main",
                roles=(),
                reason=(
                    "F 路径虽然需要语义判断，但 quality_risk=false；真实 benchmark"
                    " 不支持为低风险关系判断启动子智能体。"
                ),
            )
        return _plan(
            path="F",
            action="serial_main",
            roles=(),
            reason="mechanical 关系维护的端点、类型、强度和理由均已确认，无需子智能体。",
        )

    reasons = {
        "D": "D 路径是只读可入库判断，定位读取与 preflight 足够。",
        "E": "E 路径是详情卡或 bridge 的确定性修复，应使用项目脚本。",
        "G": "G 路径由 related_candidates 确定性完成筛选、定位与脱敏。",
    }
    return _plan(
        path=normalized,
        action="serial_main",
        roles=(),
        reason=reasons[normalized],
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="输出 408 A-G 路径的确定性 Codex 子智能体计划（只读，不实际启动）"
    )
    parser.add_argument("--path", required=True, help="A-G 路径")
    parser.add_argument("--evidence-complete", action="store_true", help="关键证据已经完整")
    parser.add_argument("--complex-redo", action="store_true", help="C 路径属于复杂 redo")
    parser.add_argument(
        "--semantic-relation",
        action="store_true",
        help="F 路径仍需判断关系类型或联系强度",
    )
    parser.add_argument(
        "--historical-unmapped",
        action="store_true",
        help="A/B 路径是尚未映射正式 ID 的历史题",
    )
    parser.add_argument(
        "--quality-risk",
        action="store_true",
        help="存在值得独立复核的歧义、泄露、身份或关系质量风险",
    )
    parser.add_argument(
        "--exact-identity-hit",
        "--exact-existing",
        dest="exact_identity_hit",
        action="store_true",
        help="A/B 新题精确命中既有正式节点，强制改走既有节点路径",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = plan_agents(
            args.path,
            evidence_complete=args.evidence_complete,
            complex_redo=args.complex_redo,
            semantic_relation=args.semantic_relation,
            historical_unmapped=args.historical_unmapped,
            quality_risk=args.quality_risk,
            exact_identity_hit=args.exact_identity_hit,
        )
    except AgentPlanError as exc:
        print(
            json.dumps(
                {
                    "contract": CONTRACT,
                    "path": str(args.path).strip().upper(),
                    "action": "invalid",
                    "worker_count": 0,
                    "roles": [],
                    "reason": str(exc),
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    print(json.dumps(result.as_dict(), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
