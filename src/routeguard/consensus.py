"""证据门禁与安全优先的裁决（创新点 2、3）。

三条硬规则
----------
1. **无证据不采纳**：结论所依赖的证据必须过白名单、时效、质量三道检查。
2. **安全单调**：最终风险 = 有效结论中的最大风险。任何角色都不能用"乐观结论"把
   另一个角色已经指出的高风险压下去 —— 这条性质由结构保证，可被单测验证。
3. **未知不等于安全**：证据全部失效时，不返回"低风险"，而是标记 insufficient_evidence
   并按保守值处理，直到补证。
"""

from __future__ import annotations

from typing import Any, Dict, List, Sequence, Tuple

from .schema import (
    RISK_LABELS,
    SOURCE_ALLOWLIST,
    Evidence,
    Finding,
    SegmentVerdict,
)

MIN_QUALITY = 0.5
SAFE_DEFAULT_RISK = 2  # 证据不足时的保守取值：按"高"处理


def validate_evidence(ev: Evidence, when: str) -> Tuple[bool, str]:
    """三道检查：来源白名单 / 质量下限 / 时效窗口。"""
    if ev.source not in SOURCE_ALLOWLIST:
        return False, f"来源未登记：{ev.source}"
    if ev.quality < MIN_QUALITY:
        return False, f"质量过低：{ev.quality:.2f} < {MIN_QUALITY}"
    if not ev.is_fresh(when):
        return False, f"证据过期：有效期 {ev.valid_from}~{ev.valid_to}，计划日期 {when}"
    if ev.value is None or ev.value == "":
        return False, "证据值为空"
    return True, ""


def _validated_findings(
    findings: Sequence[Finding], when: str
) -> Tuple[List[Finding], List[Dict[str, Any]]]:
    kept: List[Finding] = []
    rejected: List[Dict[str, Any]] = []

    for finding in findings:
        valid_evs: List[Evidence] = []
        for ev in finding.evidences:
            ok, reason = validate_evidence(ev, when)
            if ok:
                valid_evs.append(ev)
            else:
                rejected.append(
                    {
                        "analyst": finding.analyst,
                        "indicator": ev.indicator,
                        "value": ev.value,
                        "reason": reason,
                    }
                )
        if not valid_evs:
            rejected.append(
                {
                    "analyst": finding.analyst,
                    "indicator": "*",
                    "value": None,
                    "reason": "该结论没有任何通过校验的证据，整条结论被丢弃",
                }
            )
            continue
        kept.append(
            Finding(
                analyst=finding.analyst,
                segment_id=finding.segment_id,
                risk=finding.risk,
                confidence=finding.confidence,
                rationale=finding.rationale,
                evidences=valid_evs,
            )
        )
    return kept, rejected


def adjudicate(
    segment_id: str,
    findings: Sequence[Finding],
    when: str,
) -> SegmentVerdict:
    """给定若干角色的结论，产出该路段的最终裁决。"""
    kept, rejected = _validated_findings(findings, when)

    if not kept:
        return SegmentVerdict(
            segment_id=segment_id,
            risk=SAFE_DEFAULT_RISK,
            status="insufficient_evidence",
            confidence=0.0,
            findings=[],
            rejected=rejected,
            conflicts=["全部结论因证据失效被丢弃，按保守值处理"],
        )

    risks = [f.risk for f in kept]
    final_risk = max(risks)  # 安全单调：结构上不可能被压低

    # 冲突：角色间风险差 >= 2 视为实质冲突，需人工关注
    conflicts: List[str] = []
    if len(kept) >= 2 and (max(risks) - min(risks)) >= 2:
        detail = "，".join(f"{f.analyst}={RISK_LABELS[f.risk]}" for f in kept)
        conflicts.append(f"角色判断分歧较大（{detail}），按最高风险裁决")

    # 置信度：按证据质量加权的角色置信度均值，再乘以证据新鲜度折扣
    num = 0.0
    den = 0.0
    for f in kept:
        q = sum(e.quality for e in f.evidences) / len(f.evidences)
        num += f.confidence * q
        den += q
    confidence = (num / den) if den else 0.0
    if rejected:
        confidence *= 0.9  # 有证据被丢弃，整体可信度打折

    status = "conflict" if conflicts else "ok"
    return SegmentVerdict(
        segment_id=segment_id,
        risk=final_risk,
        status=status,
        confidence=confidence,
        findings=kept,
        rejected=rejected,
        conflicts=conflicts,
    )
