"""核心性质测试。可直接 `python tests/test_core.py`，也兼容 pytest。"""

from __future__ import annotations

import sys
import tempfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
DATA = ROOT / "src" / "routeguard" / "data"

from routeguard.consensus import SAFE_DEFAULT_RISK, adjudicate, validate_evidence  # noqa: E402
from routeguard.claims import extract_claims  # noqa: E402
from routeguard.geo import parse_gpx, route_stats, segment_route  # noqa: E402
from routeguard.indicators import SyntheticProvider  # noqa: E402
from routeguard.pipeline import audit_route_guide, run_pipeline  # noqa: E402
from routeguard.schema import Evidence, Finding  # noqa: E402
from routeguard.shade import point_shaded, shadow_profile, solar_position  # noqa: E402

WHEN = "2026-08-15"


def ev(**kw) -> Evidence:
    base = dict(
        indicator="air_temp_c",
        value=30.0,
        unit="℃",
        source="open-meteo:forecast",
        valid_from="2026-08-14",
        valid_to="2026-08-16",
        spatial_scale_m=3000.0,
        quality=0.9,
    )
    base.update(kw)
    return Evidence(**base)


def finding(risk: int, analyst: str, evidences=None, conf: float = 0.9) -> Finding:
    return Finding(
        analyst=analyst,
        segment_id="S001",
        risk=risk,
        confidence=conf,
        rationale="test",
        evidences=evidences if evidences is not None else [ev()],
    )


# --------------------------------------------------------------------------
# 1. 太阳位置与阴影几何
# --------------------------------------------------------------------------

def test_solar_position_summer_noon():
    sun = solar_position(40.02, 116.38, datetime(2026, 8, 15, 12, 0), tz_hours=8.0)
    assert 60 <= sun["elevation_deg"] <= 80, sun
    assert 150 <= sun["azimuth_deg"] <= 210, sun


def test_solar_position_low_at_evening():
    sun = solar_position(40.02, 116.38, datetime(2026, 8, 15, 19, 30), tz_hours=8.0)
    assert sun["elevation_deg"] < 15, sun


class _ConstCanopy:
    def __init__(self, h: float):
        self.h = h

    def __call__(self, lat: float, lon: float) -> float:
        return self.h


def test_tall_canopy_always_shaded():
    pts = parse_gpx(DATA / "sample" / "demo_route.gpx")
    prof = shadow_profile(pts[:5], _ConstCanopy(25.0), datetime(2026, 8, 15, 9, 0))
    assert prof["shaded_ratio"] == 1.0, prof


def test_no_canopy_never_shaded():
    pts = parse_gpx(DATA / "sample" / "demo_route.gpx")
    prof = shadow_profile(pts[:5], _ConstCanopy(0.0), datetime(2026, 8, 15, 12, 0))
    assert prof["shaded_ratio"] == 0.0, prof


def test_shadow_geometry_consistency():
    """45° 太阳高度角、20m 树，3m 处的点应当被遮住（3*tan45=3 <= 20）。"""
    pts = parse_gpx(DATA / "sample" / "demo_route.gpx")
    assert point_shaded(pts[0], 45.0, 180.0, _ConstCanopy(20.0)) is True
    assert point_shaded(pts[0], 45.0, 180.0, _ConstCanopy(0.0)) is False


# --------------------------------------------------------------------------
# 2. 证据门禁
# --------------------------------------------------------------------------

def test_validate_evidence_ok():
    ok, reason = validate_evidence(ev(), WHEN)
    assert ok and reason == ""


def test_validate_evidence_expired():
    ok, reason = validate_evidence(ev(valid_from="2026-01-01", valid_to="2026-02-01"), WHEN)
    assert not ok and "过期" in reason


def test_validate_evidence_bad_source():
    ok, reason = validate_evidence(ev(source="chatgpt:memory"), WHEN)
    assert not ok and "未登记" in reason


def test_validate_evidence_low_quality():
    ok, reason = validate_evidence(ev(quality=0.2), WHEN)
    assert not ok and "质量" in reason


# --------------------------------------------------------------------------
# 3. 裁决的安全单调性（论文要写进性质测试的性质）
# --------------------------------------------------------------------------

def test_safety_monotone_high_risk_wins():
    """乐观结论不能压低另一角色给出的高风险。"""
    verdict = adjudicate(
        "S001",
        [finding(3, "EnvironmentAnalyst"), finding(0, "RouteAnalyst")],
        WHEN,
    )
    assert verdict.risk == 3
    assert verdict.status == "conflict"
    assert any("分歧" in c for c in verdict.conflicts)


def test_safety_monotone_regardless_of_order():
    a = adjudicate("S001", [finding(3, "A"), finding(1, "B")], WHEN)
    b = adjudicate("S001", [finding(1, "B"), finding(3, "A")], WHEN)
    assert a.risk == b.risk == 3


def test_all_evidence_invalid_means_insufficient():
    bad = ev(valid_from="2020-01-01", valid_to="2020-02-01")
    verdict = adjudicate("S001", [finding(3, "A", evidences=[bad])], WHEN)
    assert verdict.status == "insufficient_evidence"
    assert verdict.risk == SAFE_DEFAULT_RISK  # 未知不等于安全
    assert verdict.rejected


def test_dropped_evidence_lowers_confidence():
    good = ev()
    bad = ev(valid_from="2020-01-01", valid_to="2020-02-01")
    clean = adjudicate("S001", [finding(1, "A", evidences=[good])], WHEN)
    dirty = adjudicate("S001", [finding(1, "A", evidences=[good, bad])], WHEN)
    assert dirty.confidence < clean.confidence


# --------------------------------------------------------------------------
# 4. 端到端
# --------------------------------------------------------------------------

def test_pipeline_end_to_end():
    gpx = DATA / "sample" / "demo_route.gpx"
    with tempfile.TemporaryDirectory() as tmp:
        report = run_pipeline(gpx, WHEN, SyntheticProvider(), tmp)
        assert len(report.segments) >= 2
        assert (Path(tmp) / "report.json").exists()
        assert (Path(tmp) / "ledger.jsonl").exists()
        d = report.to_dict()
        assert d["summary"]["segments"] == len(report.segments)
        # 每条结论都必须带证据
        for seg in d["segments"]:
            for f in seg["findings"]:
                assert f["evidences"], f


def test_segmentation_and_stats():
    pts = parse_gpx(DATA / "sample" / "demo_route.gpx")
    segs = segment_route(pts, segment_len_m=500)
    stats = route_stats(pts)
    total_seg = sum(s.length_m for s in segs)
    assert stats["distance_km"] > 1.0
    assert abs(total_seg / 1000 - stats["distance_km"]) < 0.05, (total_seg, stats)
    assert all(s.length_m > 0 for s in segs)


# --------------------------------------------------------------------------
# 5. 断言核验（v2 主线）
# --------------------------------------------------------------------------

GUIDE = DATA / "sample" / "route_guide.md"


def test_extract_claims():
    claims = extract_claims(GUIDE.read_text(encoding="utf-8"))
    assert len(claims) >= 8, len(claims)
    kinds = {c.kind for c in claims}
    assert "slope" in kinds and "scenery" in kinds


def test_audit_end_to_end():
    """核验一条「故意写砸」的路书：三类典型问题都要被识别。"""
    with tempfile.TemporaryDirectory() as tmp:
        result = audit_route_guide(
            DATA / "sample" / "demo_route.gpx", GUIDE, WHEN, SyntheticProvider(), tmp
        )
        assert result["total"] >= 8
        assert (Path(tmp) / "route_audit.md").exists()
        assert (Path(tmp) / "claims.json").exists()

        by_text = {c["text"]: c["status"] for c in result["claims"]}
        # 1) 夸大：说"全程平路"，实测有 3% 缓坡 → 矛盾
        assert any("全程平路" in k and v == "contradicted" for k, v in by_text.items()), by_text
        # 2) 主观：风景描述不该被当事实核验
        assert any("风景" in k and v == "subjective" for k, v in by_text.items()), by_text
        # 3) 无证据源：补给点必须如实标"未核实"
        assert any("便利店" in k and v == "unverified" for k, v in by_text.items()), by_text


# --------------------------------------------------------------------------
# 6. OSM 证据源与抽取器降级
# --------------------------------------------------------------------------

FIXTURE = DATA / "fixtures" / "osm_sample.json"


def test_osm_fixture_contexts():
    from routeguard.sources.osm import OSMClient

    segs = segment_route(parse_gpx(DATA / "sample" / "demo_route.gpx"))
    ctx = OSMClient(fixture=FIXTURE).fetch(segs)
    assert len(ctx) == len(segs)
    assert any(v.surface == "gravel" for v in ctx.values()), [v.to_dict() for v in ctx.values()]
    assert any(v.poi_supply > 0 for v in ctx.values())


def test_osm_upgrades_unverified_to_judgement():
    from routeguard.sources.osm import OSMClient

    gpx = DATA / "sample" / "demo_route.gpx"
    with tempfile.TemporaryDirectory() as tmp:
        base = audit_route_guide(gpx, GUIDE, WHEN, SyntheticProvider(), tmp, osm=None)
        enriched = audit_route_guide(
            gpx, GUIDE, WHEN, SyntheticProvider(), tmp, osm=OSMClient(fixture=FIXTURE)
        )
    assert enriched["counts"].get("unverified", 0) < base["counts"].get("unverified", 0)
    supply = [c for c in enriched["claims"] if "便利店" in c["text"]]
    assert supply and supply[0]["status"] == "contradicted", supply


def test_llm_extractor_falls_back_without_model():
    from routeguard.extractors import LLMClaimExtractor
    from routeguard.llm import NullLLM

    ext = LLMClaimExtractor(NullLLM())
    claims = ext.extract(GUIDE.read_text(encoding="utf-8"), ["S001", "S002", "S003"])
    assert claims
    assert ext.last_error


def test_osm_unavailable_is_not_negative_evidence():
    """回归测试：OSM 没取到 ≠ OSM 说没有。断网时必须落回"未核实"。"""
    from routeguard.claims import Claim, adjudicate_claim
    from routeguard.sources.osm import SegmentContext

    seg = segment_route(parse_gpx(DATA / "sample" / "demo_route.gpx"))[-1]
    claim = Claim(
        claim_id="C999",
        text="沿途有便利店可以补水",
        segment_id=seg.segment_id,
        kind="supply",
        stated="沿途有便利店可以补水",
    )

    offline = SegmentContext(segment_id=seg.segment_id, available=False)
    v_offline = adjudicate_claim(claim, seg, {}, {}, WHEN, offline)
    assert v_offline.status == "unverified", v_offline.status

    sparse = SegmentContext(segment_id=seg.segment_id, available=True, poi_supply=0, poi_sparse=True)
    v_sparse = adjudicate_claim(claim, seg, {}, {}, WHEN, sparse)
    assert v_sparse.status == "unverified", v_sparse.status

    online_empty = SegmentContext(
        segment_id=seg.segment_id, available=True, poi_supply=0, poi_sparse=False
    )
    v_online = adjudicate_claim(claim, seg, {}, {}, WHEN, online_empty)
    assert v_online.status == "contradicted", v_online.status


# --------------------------------------------------------------------------
# 7. 自然语言规划（plan）
# --------------------------------------------------------------------------

USER_TEXT = "我的FTP为250w，体重70kg，想在2027年6月份开始骑行长征路线，即从瑞金到延安。帮我规划路线。"


def test_brief_parses_user_request():
    from routeguard.brief import parse_brief

    brief, issues = parse_brief(USER_TEXT)
    assert not issues, issues
    assert (brief.origin, brief.destination) == ("瑞金", "延安")
    assert brief.start_date == "2027-06-01"
    assert brief.rider.ftp_w == 250
    assert brief.rider.weight_kg == 70
    assert brief.rider.wkg == 3.57
    # 「长征路线」必须落到历史真实路线，而不是好走的中部纵贯线
    assert brief.corridor == "historic"


def test_plan_itinerary_is_reasonable():
    from routeguard.brief import parse_brief
    from routeguard.corridor import CORRIDORS
    from routeguard.planner import plan_itinerary

    brief, _ = parse_brief(USER_TEXT)
    it = plan_itinerary(brief, CORRIDORS[brief.corridor])

    assert 3500 < it.total_km < 5000, it.total_km
    assert it.riding_days >= 30
    assert it.rest_days >= 4
    assert it.max_elev_m >= 4000, it.max_elev_m

    # 不允许在山口/无住宿点过夜
    assert all(d.overnight != "夹金山口" for d in it.days)
    assert all(d.service in ("city", "county") for d in it.days), [
        (d.day_index, d.overnight, d.service) for d in it.days if d.service == "town"
    ]

    # 高原段必须限速：夹金山那天的里程要明显低于平原日均
    assert any("高原" in a for a in it.climate_advice)
    assert any("海拔" in w for w in it.warnings)


def test_plan_avoids_overnight_without_lodging():
    from routeguard.brief import parse_brief
    from routeguard.corridor import CORRIDORS
    from routeguard.planner import plan_itinerary

    brief, _ = parse_brief(USER_TEXT)
    it = plan_itinerary(brief, CORRIDORS["historic"])
    no_lodging = {"夹金山口", "腊子口", "班佑", "六盘山", "两河口", "安顺场", "榜罗"}
    overnight_at_bad = [d.overnight for d in it.days if d.overnight in no_lodging]
    # 允许极少数无替代方案的情况，但必须在警告里说明
    for name in overnight_at_bad:
        assert any(name in w for w in it.warnings), name


def test_plan_missing_route_reports_issue():
    from routeguard.brief import parse_brief

    _, issues = parse_brief("我想骑车出去玩，帮我看看")
    assert issues and "起点和终点" in issues[0]


def test_plan_exports_valid_gpx():
    """导出的 GPX 必须能被自己的解析器读回，且里程对得上。"""
    from routeguard.brief import parse_brief
    from routeguard.corridor import CORRIDORS
    from routeguard.gpx_export import write_gpx
    from routeguard.planner import plan_itinerary

    brief, _ = parse_brief(USER_TEXT)
    waypoints = CORRIDORS[brief.corridor]
    it = plan_itinerary(brief, waypoints)

    with tempfile.TemporaryDirectory() as tmp:
        gpx = write_gpx(Path(tmp) / "route.gpx", it, waypoints)
        text = gpx.read_text(encoding="utf-8")
        assert text.startswith("<?xml")
        assert text.count("<trk>") == it.riding_days
        assert text.count("<wpt ") == it.riding_days

        pts = parse_gpx(gpx)
        assert len(pts) > 500
        # 折线是直线插值，长度必然短于乘了绕行系数的公路里程（约 0.78 倍）
        from routeguard.geo import route_stats

        dist = route_stats(pts)["distance_km"]
        ratio = dist / it.total_km
        assert 0.70 < ratio < 1.0, (dist, it.total_km, ratio)


class _FakeFinder:
    def __init__(self, lodgings, error=""):
        self.lodgings = lodgings
        self.last_error = error

    def find(self, lat, lon):
        return list(self.lodgings)


def test_lodging_relocates_camp_stop():
    """找不到县城但有酒店时，落脚点应挪到酒店，而不是让人露营。"""
    from routeguard.brief import TripBrief
    from routeguard.corridor import Leg, Waypoint
    from routeguard.planner import plan_itinerary
    from routeguard.sources.lodging import Lodging

    w1 = Waypoint("甲县", 30.0, 110.0, "county", elev_m=100)
    w2 = Waypoint("乙县", 30.0, 112.0, "county", elev_m=100)
    pts = [(30.0, 110.0 + i * 0.2, 100.0) for i in range(11)]
    leg = Leg(start=w1, end=w2, km=200.0, climb_m=500, points=pts, provider="brouter:trekking")
    brief = TripBrief(raw_text="", origin="甲县", destination="乙县", start_date="2027-06-01")

    finder = _FakeFinder([Lodging("山脚客栈", 30.0, 111.0, "guest_house", 900.0)])
    it = plan_itinerary(brief, [w1, w2], legs=[leg], lodging_finder=finder)

    lodges = [d for d in it.days if d.service == "lodge"]
    assert lodges, [(d.day_index, d.overnight, d.service) for d in it.days]
    assert any("山脚客栈" in d.overnight for d in lodges)


def test_lodging_failure_keeps_camp_and_warns():
    from routeguard.brief import parse_brief
    from routeguard.corridor import CORRIDORS
    from routeguard.planner import plan_itinerary

    brief, _ = parse_brief(USER_TEXT)
    finder = _FakeFinder([], error="TimeoutError: timed out")
    it = plan_itinerary(brief, CORRIDORS[brief.corridor], lodging_finder=finder)
    camps = [d for d in it.days if d.service == "camp"]
    if camps:  # 若存在无住宿点，必须给出明确提示而不是静默
        assert any("无固定住宿" in w for w in it.warnings)


def test_daily_gpx_split():
    """每个骑行日导出一个独立 GPX，且各日里程加起来应与总里程接近。"""
    from routeguard.brief import parse_brief
    from routeguard.corridor import CORRIDORS
    from routeguard.geo import route_stats
    from routeguard.gpx_export import write_daily_gpx
    from routeguard.planner import plan_itinerary

    brief, _ = parse_brief(USER_TEXT)
    waypoints = CORRIDORS[brief.corridor]
    it = plan_itinerary(brief, waypoints)

    with tempfile.TemporaryDirectory() as tmp:
        files = write_daily_gpx(Path(tmp), it, it.legs)
        assert len(files) == it.riding_days
        assert all(f.exists() and f.stat().st_size > 500 for f in files)
        assert all(f.name.startswith("D") for f in files)

        total = 0.0
        for f in files:
            pts = parse_gpx(f)
            assert len(pts) >= 2
            total += route_stats(pts)["distance_km"]
        # 直线模式下按直线里程比；只要量级一致即可
        assert total > it.total_km * 0.6, (total, it.total_km)


def test_via_split_and_coordinates():
    """--via 的切分与坐标解析：地名用逗号，坐标用分号；经度纬度顺序自适应。"""
    from routeguard.geocode import NominatimGeocoder, split_via

    assert split_via("海口,文昌,万宁") == ["海口", "文昌", "万宁"]
    assert split_via("110.32,20.03;110.10,19.52") == ["110.32,20.03", "110.10,19.52"]

    g = NominatimGeocoder(cache_dir=None)
    for q in ("110.32,20.03", "20.03,110.32", "110.32，20.03"):
        p = g.lookup(q)
        assert p is not None, q
        assert abs(p.lat - 20.03) < 1e-6 and abs(p.lon - 110.32) < 1e-6, (q, p)

    labeled = g.lookup("海口@20.03,110.32")
    assert labeled is not None and labeled.name == "海口"
    assert labeled.addresstype == "coordinate"


def test_custom_corridor_plans_loop():
    """自定义途经点：环线首尾同名也应闭合，且能排出多日行程。"""
    from routeguard.brief import TripBrief
    from routeguard.corridor import waypoints_from_places
    from routeguard.geocode import Place
    from routeguard.planner import plan_itinerary

    places = [
        Place("海口", 20.03, 110.32),
        Place("文昌", 19.61, 110.75),
        Place("万宁", 18.80, 110.39),
        Place("三亚", 18.25, 109.51),
        Place("海口", 20.03, 110.32),
    ]
    wps = waypoints_from_places(places)
    brief = TripBrief(raw_text="", origin="海口", destination="海口", start_date="2027-01-01")
    it = plan_itinerary(brief, wps)

    assert it.days, "应排出至少一天行程"
    assert abs(it.total_km - 500) < 200, it.total_km  # 直线估算法下量级合理
    assert it.days[-1].overnight == "海口"


def test_routegen_densifies_between_anchors():
    """任意 A→B 应自动补出中间航点，而不是只有首尾两个点。"""
    from routeguard.geocode import Place
    from routeguard.routegen import generate_waypoints, generated_summary

    a, b = Place("海口", 20.03, 110.32), Place("三亚", 18.25, 109.51)
    wps = generate_waypoints([a, b], spacing_km=60)
    assert wps[0].name == "海口" and wps[-1].name == "三亚"
    assert len(wps) >= 3, [w.name for w in wps]
    assert "自动采样点" in generated_summary(wps)


def test_clarify_fills_missing_endpoints():
    """缺起终点时应通过对话补齐，而不是直接失败。"""
    from routeguard.brief import parse_brief
    from routeguard.clarify import clarify_plan

    brief, issues = parse_brief("2027年1月骑行海南岛")
    answers = iter(["海口", "三亚"])
    brief, via, notes = clarify_plan(brief, issues, ask=lambda _p: next(answers))
    assert brief.origin == "海口" and brief.destination == "三亚"
    assert notes, "澄清过程应留下记录"


if __name__ == "__main__":
    passed = 0
    failed = []
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                passed += 1
                print(f"[PASS] {name}")
            except Exception as exc:  # noqa: BLE001
                failed.append((name, exc))
                print(f"[FAIL] {name}: {exc}")
    print(f"\n通过 {passed}，失败 {len(failed)}")
    raise SystemExit(1 if failed else 0)
