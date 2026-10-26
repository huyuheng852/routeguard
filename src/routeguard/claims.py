"""断言层：把路书拆成可核验的原子断言，逐条裁定。

设计要点
--------
1. **路书是待核验对象，不是证据**。证据只能来自外部数据源，避免自我循环。
2. 三态 + 两特例：supported / contradicted / unverified / conflict / subjective。
3. 不可核验的主观描述标 subjective 而不是 unverified——这是控制"过度保守"的闸门：
   不是所有句子都该被核验，否则未核实率虚高，用户就不信了。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from .consensus import validate_evidence
from .schema import Evidence, Segment
from .sources.osm import SegmentContext

KIND_KEYWORDS: Dict[str, Sequence[str]] = {
    "slope": ("坡", "平路", "爬升", "起伏", "爬坡"),
    "surface": ("路面", "柏油", "铺装", "砂石", "碎石", "水泥", "土路"),
    "traffic": ("车流", "机动车", "混行", "国道", "大车", "卡车"),
    "supply": ("补给", "便利店", "超市", "商店", "饭店", "补水", "修车"),
    "shade": ("树荫", "林荫", "遮荫", "暴晒", "晒"),
    "passable": ("隧道", "禁行", "封闭", "施工", "可通行", "轮渡"),
    "scenery": ("风景", "景色", "好看", "美", "视野"),
}

KIND_LABELS = {
    "slope": "坡度",
    "surface": "路面",
    "traffic": "交通",
    "supply": "补给",
    "shade": "遮荫",
    "passable": "通行",
    "scenery": "主观",
}

LINE_RE = re.compile(r"^\s*[-*]\s*\[(?P<seg>S\d{3}|全程)\]\s*(?P<text>.+?)\s*$")

SLOPE_LEVELS = [("平", 2.0), ("缓", 4.0), ("陡", 8.0), ("很陡", 99.0)]


def class_kind_list() -> List[str]:
    return list(KIND_KEYWORDS.keys())


@dataclass
class Claim:
    claim_id: str
    text: str
    segment_id: str  # "全程" 表示展开到所有路段
    kind: str
    stated: str
    polarity: str = "positive"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "claim_id": self.claim_id,
            "segment_id": self.segment_id,
            "kind": self.kind,
            "kind_label": KIND_LABELS.get(self.kind, self.kind),
            "stated": self.stated,
            "polarity": self.polarity,
            "text": self.text,
        }


@dataclass
class ClaimVerdict:
    claim: Claim
    status: str  # supported | contradicted | unverified | conflict | subjective
    note: str
    evidences: List[Evidence] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            **self.claim.to_dict(),
            "status": self.status,
            "note": self.note,
            "evidences": [e.to_dict() for e in self.evidences],
        }


def classify(text: str) -> Optional[str]:
    """按关键词判定断言类型；返回 None 表示无法归类。"""
    for kind, words in KIND_KEYWORDS.items():
        if any(w in text for w in words):
            return kind
    return None


def normalize_slope(text: str) -> str:
    """把"坡度平缓""缓上"等表述归一到 平/缓/陡/很陡。"""
    if "很陡" in text or "陡峭" in text or "巨陡" in text:
        return "很陡"
    if "陡" in text:
        return "陡"
    if "缓" in text:
        return "缓"
    if "平" in text or "起伏不大" in text:
        return "平"
    return "未知"


def extract_claims(guide_md: str) -> List[Claim]:
    """规则抽取。行格式：`- [S002] 坡度平缓`。"""
    claims: List[Claim] = []
    for line in guide_md.splitlines():
        m = LINE_RE.match(line)
        if not m:
            continue
        text = m.group("text").strip()
        kind = classify(text)
        if kind is None:
            continue
        stated = normalize_slope(text) if kind == "slope" else text
        claims.append(
            Claim(
                claim_id=f"C{len(claims) + 1:03d}",
                text=text,
                segment_id=m.group("seg"),
                kind=kind,
                stated=stated,
            )
        )
    return claims


def _level_of(slope_pct: float) -> str:
    s = abs(slope_pct)
    for name, upper in SLOPE_LEVELS:
        if s < upper:
            return name
    return "很陡"


def _surface_class(text: str) -> Optional[str]:
    if any(w in text for w in ("柏油", "铺装", "沥青", "好路")):
        return "paved"
    if any(w in text for w in ("砂石", "碎石", "土路", "烂路")):
        return "unpaved"
    return None


# OSM surface 标签 → 二分类，避免"asphalt"和"paved"对不上
OSM_SURFACE_MAP = {
    "asphalt": "paved",
    "concrete": "paved",
    "concrete:plates": "paved",
    "paved": "paved",
    "chipseal": "paved",
    "gravel": "unpaved",
    "unpaved": "unpaved",
    "ground": "unpaved",
    "dirt": "unpaved",
    "compacted": "unpaved",
    "fine_gravel": "unpaved",
    "cobblestone": "unpaved",
}


def _osm_evidence(ctx: SegmentContext) -> List[Evidence]:
    """把 OSM 上下文转成标准证据对象（来源进白名单才可能被采纳）。"""
    out: List[Evidence] = []
    if ctx.surface:
        out.append(
            Evidence(
                indicator="osm_surface",
                value=ctx.surface,
                unit="",
                source="osm:highway",
                valid_from="2025-01-01",
                valid_to="2027-12-31",
                spatial_scale_m=10.0,
                quality=0.7,
                note=f"OSM surface 标签（{ctx.road_class or 'unknown'}）",
            )
        )
    if ctx.poi_supply:
        out.append(
            Evidence(
                indicator="osm_supply_poi",
                value=ctx.poi_supply,
                unit="个",
                source="osm:poi",
                valid_from="2025-01-01",
                valid_to="2027-12-31",
                spatial_scale_m=300.0,
                quality=0.6,
                note="周边便利店/超市数量（OSM，时效依赖社区更新）",
            )
        )
    if ctx.tunnel is not None:
        out.append(
            Evidence(
                indicator="osm_tunnel",
                value=ctx.tunnel,
                unit="",
                source="osm:highway",
                valid_from="2025-01-01",
                valid_to="2027-12-31",
                spatial_scale_m=10.0,
                quality=0.85,
                note="是否隧道（OSM tunnel 标签）",
            )
        )
    return out


def adjudicate_claim(
    claim: Claim,
    segment: Segment,
    shade: Dict[str, float],
    cards: Dict[str, Evidence],
    when: str,
    ctx: Optional[SegmentContext] = None,
) -> ClaimVerdict:
    """对单条断言做三态裁定。"""
    usable = {k: v for k, v in cards.items() if validate_evidence(v, when)[0]}

    if claim.kind == "scenery":
        return ClaimVerdict(claim, "subjective", "主观描述，不纳入核验范围（非事实断言）")

    if claim.kind == "slope":
        actual = _level_of(segment.max_slope_pct)
        note = f"DEM 计算最大坡度 {segment.max_slope_pct:.1f}%，判定为「{actual}」"
        status = "supported" if claim.stated == actual else "contradicted"
        return ClaimVerdict(claim, status, note)

    if claim.kind == "shade":
        ratio = shade.get("shaded_ratio", 0.0)
        expects_shade = ("荫" in claim.text) or ("遮" in claim.text)
        note = (
            f"遮荫模型：该段遮荫率 {ratio * 100:.0f}%"
            f"（出发时刻太阳高度角 {shade.get('sun_elevation_deg', 0)}°）"
        )
        if expects_shade and ratio >= 0.5:
            return ClaimVerdict(claim, "supported", note)
        if expects_shade and ratio <= 0.2:
            return ClaimVerdict(claim, "contradicted", note)
        if (not expects_shade) and ratio <= 0.2:
            return ClaimVerdict(claim, "supported", note)
        if (not expects_shade) and ratio >= 0.6:
            return ClaimVerdict(claim, "contradicted", note)
        return ClaimVerdict(claim, "unverified", note + "；处于中间区间，无法判定")

    if claim.kind == "traffic":
        road = cards.get("road_class")
        if not road or "road_class" not in usable:
            return ClaimVerdict(claim, "unverified", "缺少可用的道路等级证据")
        busy = str(road.value) in ("primary", "secondary", "trunk")
        expects_busy = any(w in claim.text for w in ("大车", "混行", "车多"))
        status = "supported" if busy == expects_busy else "contradicted"
        return ClaimVerdict(claim, status, f"OSM 道路等级 = {road.value}", [road])

    if claim.kind == "surface":
        if ctx is None or not ctx.available or not ctx.surface:
            return ClaimVerdict(claim, "unverified", "无可用路面证据（OSM surface 标签缺失或未联网）")
        wanted = _surface_class(claim.text)
        actual = OSM_SURFACE_MAP.get(ctx.surface, ctx.surface)
        if wanted is None:
            return ClaimVerdict(claim, "unverified", f"OSM surface = {ctx.surface}，断言未给出可判定类型")
        status = "supported" if wanted == actual else "contradicted"
        return ClaimVerdict(
            claim, status, f"OSM surface = {ctx.surface}（归类 {actual}）", _osm_evidence(ctx)
        )

    if claim.kind == "supply":
        if ctx is None or not ctx.available:
            return ClaimVerdict(claim, "unverified", "未获取 OSM POI，无法核验补给点（没查到≠没有）")
        count = ctx.poi_supply
        expects_supply = any(w in claim.text for w in ("便利店", "超市", "商店", "补给", "补水"))
        if expects_supply and count > 0:
            return ClaimVerdict(
                claim, "supported", f"周边 300m 内 OSM 记录到 {count} 处便利店/超市", _osm_evidence(ctx)
            )
        if expects_supply and count == 0:
            if ctx.poi_sparse:
                return ClaimVerdict(
                    claim,
                    "unverified",
                    "OSM 在该区域 POI 覆盖稀疏，检索不到不足以否定该断言（需实地或电话核实）",
                )
            return ClaimVerdict(
                claim, "contradicted", "周边 300m 内未检索到便利店/超市（OSM 可能滞后）", _osm_evidence(ctx)
            )
        return ClaimVerdict(claim, "unverified", f"周边 POI 数 {count}，无法与断言对应")

    if claim.kind == "passable":
        if ctx is None or not ctx.available or ctx.tunnel is None:
            return ClaimVerdict(claim, "unverified", "无可用通行性证据（OSM tunnel 标签缺失或未联网）")
        mentions_tunnel = "隧道" in claim.text
        if mentions_tunnel and not ctx.tunnel:
            return ClaimVerdict(
                claim, "contradicted", "OSM 未在该段标注 tunnel 标签", _osm_evidence(ctx)
            )
        return ClaimVerdict(claim, "supported" if mentions_tunnel == ctx.tunnel else "unverified",
                            f"OSM tunnel = {ctx.tunnel}", _osm_evidence(ctx))

    return ClaimVerdict(claim, "unverified", "未知断言类型")


def audit_claims(
    claims: Sequence[Claim],
    segments: Sequence[Segment],
    shades: Dict[str, Dict[str, float]],
    cards_by_segment: Dict[str, Dict[str, Evidence]],
    when: str,
    contexts: Optional[Dict[str, SegmentContext]] = None,
) -> List[ClaimVerdict]:
    """核验一组断言。全程断言展开到所有路段。"""
    seg_map = {s.segment_id: s for s in segments}
    contexts = contexts or {}
    verdicts: List[ClaimVerdict] = []

    for claim in claims:
        targets = (
            list(seg_map.values())
            if claim.segment_id == "全程"
            else [seg_map.get(claim.segment_id)]
        )
        targets = [t for t in targets if t is not None]
        if not targets:
            verdicts.append(
                ClaimVerdict(claim, "unverified", f"路书引用的路段 {claim.segment_id} 在轨迹中不存在")
            )
            continue

        sub = [
            adjudicate_claim(
                claim,
                seg,
                shades.get(seg.segment_id, {}),
                cards_by_segment.get(seg.segment_id, {}),
                when,
                contexts.get(seg.segment_id),
            )
            for seg in targets
        ]
        if len(sub) == 1:
            verdicts.append(sub[0])
            continue

        statuses = {v.status for v in sub}
        if statuses == {"supported"}:
            merged = "supported"
        elif statuses == {"subjective"}:
            merged = "subjective"
        elif "contradicted" in statuses:
            merged = "contradicted"
        elif "conflict" in statuses:
            merged = "conflict"
        else:
            merged = "unverified"
        detail = "；".join(f"{t.segment_id}:{v.status}" for t, v in zip(targets, sub))
        verdicts.append(ClaimVerdict(claim, merged, f"全程展开 → {detail}"))

    return verdicts


def audit_guide(
    guide_md: str,
    segments: Sequence[Segment],
    shades: Dict[str, Dict[str, float]],
    cards_by_segment: Dict[str, Dict[str, Evidence]],
    when: str,
    contexts: Optional[Dict[str, SegmentContext]] = None,
) -> List[ClaimVerdict]:
    """规则抽取 + 核验（保持向后兼容的入口）。"""
    return audit_claims(
        extract_claims(guide_md), segments, shades, cards_by_segment, when, contexts
    )


STATUS_ICONS = {
    "supported": "✅ 支持",
    "contradicted": "⚠️ 矛盾",
    "unverified": "❔ 未核实",
    "conflict": "⚡ 冲突",
    "subjective": "💬 主观",
}


def summarize_verdicts(verdicts: Sequence[ClaimVerdict]) -> Dict[str, Any]:
    counts: Dict[str, int] = {}
    for v in verdicts:
        counts[v.status] = counts.get(v.status, 0) + 1
    total = len(verdicts)
    verified = counts.get("supported", 0) + counts.get("contradicted", 0)
    checkable = total - counts.get("subjective", 0)
    return {
        "total": total,
        "counts": counts,
        "verified": verified,
        "coverage": round(verified / max(checkable, 1), 3),
        "unverified_rate": round(counts.get("unverified", 0) / max(checkable, 1), 3),
    }


def render_report(verdicts: Sequence[ClaimVerdict], title: str = "路书核验报告") -> str:
    """渲染成可交付的 Markdown。"""
    s = summarize_verdicts(verdicts)
    lines = [f"# {title}", ""]
    lines.append(f"- 断言总数：{s['total']}")
    lines.append(f"- 已核验：{s['verified']}（覆盖率 {s['coverage'] * 100:.0f}%）")
    lines.append(f"- 未核实率：{s['unverified_rate'] * 100:.0f}%")
    for key, name in (
        ("contradicted", "矛盾"),
        ("unverified", "未核实"),
        ("subjective", "主观"),
        ("supported", "支持"),
    ):
        lines.append(f"- {name}：{s['counts'].get(key, 0)}")
    lines.append("")

    for v in verdicts:
        lines.append(f"## [{v.claim.segment_id}] {v.claim.text}")
        lines.append(f"- 结论：{STATUS_ICONS.get(v.status, v.status)}")
        lines.append(f"- 依据：{v.note}")
        for ev in v.evidences:
            lines.append(
                f"  - 证据：{ev.indicator} = {ev.value}{ev.unit}"
                f"（{ev.source}，有效期 {ev.valid_from}~{ev.valid_to}，质量 {ev.quality:.2f}）"
            )
        lines.append("")
    return "\n".join(lines)
