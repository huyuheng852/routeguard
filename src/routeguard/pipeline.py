"""端到端流水线。

两条主线：
1. `run_pipeline`      —— 轨迹 → 分段 → 指标 → 角色 → 裁决 → 风险报告
2. `audit_route_guide` —— 路书 → 断言抽取 → 证据核验 → 核验报告
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .agents import EnvironmentAnalyst, RouteAnalyst
from .audit import Ledger, summarize
from .claims import audit_claims, render_report, summarize_verdicts
from .consensus import adjudicate
from .extractors import RuleExtractor
from .geo import parse_gpx, route_stats, segment_route
from .indicators import IndicatorProvider, index_by_indicator
from .report import render_html
from .schema import Evidence, RouteReport, Segment
from .shade import shadow_profile


def build_segments(
    gpx_path: str | Path,
    when: str,
    provider: IndicatorProvider,
    departure_hour: int = 9,
    segment_len_m: float = 500.0,
    tz_hours: float = 8.0,
) -> Tuple[List[Segment], Dict[str, Dict[str, float]], Dict[str, Dict[str, Evidence]], Dict[str, Any]]:
    """路段画像：分段 + 遮荫剖面 + 指标卡。两条主线共用。"""
    points = parse_gpx(gpx_path)
    segments = segment_route(points, segment_len_m=segment_len_m)
    stats = route_stats(points)
    departure = datetime.strptime(f"{when} {departure_hour:02d}:00", "%Y-%m-%d %H:%M")

    shades: Dict[str, Dict[str, float]] = {}
    cards_by_segment: Dict[str, Dict[str, Evidence]] = {}

    for seg in segments:
        shade = shadow_profile(seg.points, provider.canopy_height_at, departure, tz_hours=tz_hours)
        evidences = provider.evidences(seg, when)
        cards = index_by_indicator(evidences)
        road = cards.get("road_class")
        if road is not None:
            seg.road_class = str(road.value)
        shades[seg.segment_id] = shade
        cards_by_segment[seg.segment_id] = cards

    return segments, shades, cards_by_segment, stats


def run_pipeline(
    gpx_path: str | Path,
    when: str,
    provider: IndicatorProvider,
    out_dir: str | Path = "out",
    segment_len_m: float = 500.0,
    departure_hour: int = 9,
    tz_hours: float = 8.0,
) -> RouteReport:
    """跑完一条线路的风险裁决，产出报告与审计账本。"""
    gpx_path = Path(gpx_path)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    segments, shades, cards_by_segment, stats = build_segments(
        gpx_path, when, provider, departure_hour, segment_len_m, tz_hours
    )

    route_analyst = RouteAnalyst()
    env_analyst = EnvironmentAnalyst()
    verdicts = []
    ledger_path = out_dir / "ledger.jsonl"

    with Ledger(ledger_path) as ledger:
        ledger.write(
            "run.start",
            {
                "gpx": str(gpx_path),
                "when": when,
                "departure_hour": departure_hour,
                "provider": provider.name,
                "segments": len(segments),
                "route_stats": stats,
            },
        )

        for seg in segments:
            shade = shades[seg.segment_id]
            cards = cards_by_segment[seg.segment_id]
            findings = [
                route_analyst.evaluate(seg, cards, shade),
                env_analyst.evaluate(seg, cards, shade),
            ]
            verdict = adjudicate(seg.segment_id, findings, when)
            verdict.length_m = seg.length_m
            verdict.max_slope_pct = seg.max_slope_pct
            verdict.shaded_ratio = shade.get("shaded_ratio", 0.0)
            verdicts.append(verdict)

            ledger.write(
                "segment.verdict",
                {
                    "segment_id": seg.segment_id,
                    "shade": shade,
                    "risk": verdict.risk,
                    "status": verdict.status,
                    "rejected": verdict.rejected,
                    "conflicts": verdict.conflicts,
                },
            )

        ledger.write("run.end", {"segments": len(verdicts)})

    report = RouteReport(
        route_id=gpx_path.stem,
        when=when,
        generated_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
        segments=verdicts,
        meta={
            "provider": provider.name,
            "departure_hour": departure_hour,
            "segment_len_m": segment_len_m,
            "route_stats": stats,
            "ledger": str(ledger_path),
        },
    )

    (out_dir / "report.json").write_text(report.to_json(), encoding="utf-8")
    (out_dir / "summary.json").write_text(
        json.dumps(summarize(report), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return report


def audit_route_guide(
    gpx_path: str | Path,
    guide_path: str | Path,
    when: str,
    provider: IndicatorProvider,
    out_dir: str | Path = "out/audit",
    departure_hour: int = 9,
    segment_len_m: float = 500.0,
    tz_hours: float = 8.0,
    extractor: Optional[Any] = None,
    osm: Optional[Any] = None,
) -> Dict[str, Any]:
    """核验一份路书：逐条断言判定 支持 / 矛盾 / 未核实 / 主观。

    extractor 为空时用规则抽取；osm 为空时不取 OSM 证据（相关断言落为"未核实"）。
    """
    gpx_path = Path(gpx_path)
    guide_path = Path(guide_path)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    segments, shades, cards_by_segment, stats = build_segments(
        gpx_path, when, provider, departure_hour, segment_len_m, tz_hours
    )

    contexts: Dict[str, Any] = {}
    osm_error = ""
    if osm is not None:
        contexts = osm.fetch(segments)
        osm_error = getattr(osm, "last_error", "") or ""

    guide_md = guide_path.read_text(encoding="utf-8")
    if extractor is None:
        extractor = RuleExtractor()
    claims = extractor.extract(guide_md, [s.segment_id for s in segments])
    verdicts = audit_claims(claims, segments, shades, cards_by_segment, when, contexts)

    summary = summarize_verdicts(verdicts)
    result = {
        "guide": str(guide_path),
        "route": str(gpx_path),
        "when": when,
        "departure_hour": departure_hour,
        "route_stats": stats,
        "extractor": getattr(extractor, "name", "unknown"),
        "extractor_fallback": getattr(extractor, "last_error", "") or "",
        "osm_error": osm_error,
        "counts": summary["counts"],
        "total": summary["total"],
        "coverage": summary["coverage"],
        "unverified_rate": summary["unverified_rate"],
        "claims": [v.to_dict() for v in verdicts],
        "contexts": {k: v.to_dict() for k, v in contexts.items()},
    }

    (out_dir / "claims.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    title = f"路书核验报告：{guide_path.stem}"
    (out_dir / "route_audit.md").write_text(render_report(verdicts, title=title), encoding="utf-8")
    (out_dir / "route_audit.html").write_text(
        render_html(
            verdicts,
            title=title,
            meta={
                "线路": gpx_path.stem,
                "计划日期": when,
                "出发": f"{departure_hour}:00",
                "抽取器": getattr(extractor, "name", "unknown"),
                "数据源": "OSM" if contexts else "无（离线）",
            },
        ),
        encoding="utf-8",
    )
    return result
