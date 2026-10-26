"""数据契约：证据（Evidence）、指标卡（IndicatorCard）、结论（Finding）、裁决（Verdict）。

设计原则
--------
1. **任何结论必须挂证据**。证据是三元组之外的第四元：值、来源、有效期、空间粒度。
2. 全部对象可 JSON 序列化，便于落盘审计与复现。
3. 不依赖第三方库。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

RISK_LABELS = {0: "低", 1: "中", 2: "高", 3: "极高"}

# 允许进入裁决的证据来源白名单。新增数据源必须在这里登记，否则被门禁丢弃。
SOURCE_ALLOWLIST = {
    "synthetic:v1",
    "sentinel-2:L2A",
    "landsat:C2L2:ST",
    "copernicus:DEM:GLO30",
    "meta:canopy-height:1m",
    "osm:highway",
    "open-meteo:forecast",
}


@dataclass
class Evidence:
    """一条可追溯的证据。缺任何一项都不能进入裁决。"""

    indicator: str  # 指标名，如 "lst_c"
    value: Any  # 数值或分类值
    unit: str
    source: str  # 必须命中 SOURCE_ALLOWLIST
    valid_from: str  # YYYY-MM-DD
    valid_to: str  # YYYY-MM-DD
    spatial_scale_m: float  # 空间粒度（像元/缓冲区尺度）
    quality: float  # 0~1，数据质量（云量、缺失率等折算）
    note: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def is_fresh(self, when: str) -> bool:
        return self.valid_from <= when <= self.valid_to


@dataclass
class IndicatorCard:
    """某个路段上的一项环境指标（证据的容器）。"""

    segment_id: str
    evidence: Evidence

    def to_dict(self) -> Dict[str, Any]:
        return {"segment_id": self.segment_id, "evidence": self.evidence.to_dict()}


@dataclass
class Point:
    lat: float
    lon: float
    ele: float = 0.0
    dist_m: float = 0.0  # 距起点的累计距离


@dataclass
class Segment:
    """把轨迹切成的小段，是风险评价与展示的基本单元。"""

    segment_id: str
    index: int
    points: List[Point]
    length_m: float
    mean_slope_pct: float
    max_slope_pct: float
    road_class: str = "unknown"
    surface: str = "unknown"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "segment_id": self.segment_id,
            "index": self.index,
            "length_m": round(self.length_m, 1),
            "mean_slope_pct": round(self.mean_slope_pct, 2),
            "max_slope_pct": round(self.max_slope_pct, 2),
            "road_class": self.road_class,
            "surface": self.surface,
            "start": {"lat": self.points[0].lat, "lon": self.points[0].lon} if self.points else None,
        }


@dataclass
class Finding:
    """一个角色对某个路段的判断。"""

    analyst: str
    segment_id: str
    risk: int  # 0..3
    confidence: float  # 0..1
    rationale: str
    evidences: List[Evidence] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "analyst": self.analyst,
            "segment_id": self.segment_id,
            "risk": self.risk,
            "risk_label": RISK_LABELS.get(self.risk, "?"),
            "confidence": round(self.confidence, 3),
            "rationale": self.rationale,
            "evidences": [e.to_dict() for e in self.evidences],
        }


@dataclass
class SegmentVerdict:
    """裁决结果。status 有三种，缺证据时必须拒答而不是猜。"""

    segment_id: str
    risk: int
    status: str  # ok | insufficient_evidence | conflict
    confidence: float
    findings: List[Finding] = field(default_factory=list)
    rejected: List[Dict[str, Any]] = field(default_factory=list)
    conflicts: List[str] = field(default_factory=list)
    # 展示与审计用的路段上下文（不参与裁决）
    length_m: float = 0.0
    max_slope_pct: float = 0.0
    shaded_ratio: float = 0.0

    @property
    def label(self) -> str:
        return RISK_LABELS.get(self.risk, "?")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "segment_id": self.segment_id,
            "risk": self.risk,
            "risk_label": self.label,
            "status": self.status,
            "confidence": round(self.confidence, 3),
            "length_m": round(self.length_m, 1),
            "max_slope_pct": round(self.max_slope_pct, 2),
            "shaded_ratio": round(self.shaded_ratio, 3),
            "conflicts": self.conflicts,
            "rejected": self.rejected,
            "findings": [f.to_dict() for f in self.findings],
        }


@dataclass
class RouteReport:
    route_id: str
    when: str  # 计划出发日期 YYYY-MM-DD
    generated_at: str
    segments: List[SegmentVerdict]
    meta: Dict[str, Any] = field(default_factory=dict)

    @property
    def max_risk(self) -> int:
        return max((s.risk for s in self.segments if s.status == "ok"), default=0)

    @property
    def insufficient_count(self) -> int:
        return sum(1 for s in self.segments if s.status == "insufficient_evidence")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "route_id": self.route_id,
            "when": self.when,
            "generated_at": self.generated_at,
            "summary": {
                "segments": len(self.segments),
                "max_risk": self.max_risk,
                "max_risk_label": RISK_LABELS.get(self.max_risk, "?"),
                "insufficient_evidence_segments": self.insufficient_count,
            },
            "meta": self.meta,
            "segments": [s.to_dict() for s in self.segments],
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)
