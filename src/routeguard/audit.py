"""审计账本：把每一步决策落成 JSONL，支持事后复算与答辩演示。"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, Iterable

from .schema import RISK_LABELS, RouteReport


class Ledger:
    """一行一条事件，便于 grep / 审计 / 复现。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self.path.open("w", encoding="utf-8")

    def write(self, event: str, payload: Dict[str, Any]) -> None:
        record = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "event": event, "payload": payload}
        self._fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._fh.flush()

    def close(self) -> None:
        self._fh.close()

    def __enter__(self) -> "Ledger":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def summarize(report: RouteReport) -> Dict[str, Any]:
    """给报告生成一份可放进论文的统计摘要。"""
    dist: Dict[str, int] = {RISK_LABELS[k]: 0 for k in RISK_LABELS}
    for seg in report.segments:
        dist[seg.label] = dist.get(seg.label, 0) + 1

    rejected = sum(len(seg.rejected) for seg in report.segments)
    conflicts = sum(len(seg.conflicts) for seg in report.segments)
    return {
        "segments": len(report.segments),
        "risk_distribution": dist,
        "max_risk": report.max_risk,
        "insufficient_evidence": report.insufficient_count,
        "rejected_evidence": rejected,
        "conflicts": conflicts,
        "avg_confidence": round(
            sum(s.confidence for s in report.segments) / max(len(report.segments), 1), 3
        ),
    }


def high_risk_segments(report: RouteReport, threshold: int = 2) -> Iterable[str]:
    for seg in report.segments:
        if seg.risk >= threshold:
            yield seg.segment_id
