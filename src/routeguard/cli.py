"""命令行入口。

    python -m routeguard audit                 # 核验示范路书（含 HTML 报告）
    python -m routeguard analyze --gpx ...     # 路段风险裁决
    python -m routeguard selftest              # 自检：不联网跑一遍核心链路
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .audit import summarize
from .claims import STATUS_ICONS
from .config import Settings
from .extractors import build_extractor
from .gpx_export import write_daily_gpx, write_gpx
from .indicators import SyntheticProvider
from .llm import build_llm
from .paths import default_out, sample_gpx, sample_guide
from .pipeline import audit_route_guide, run_pipeline
from .planner import plan_itinerary
from .router import BRouterClient, OSRMClient, Router, build_routed_legs
from .schema import RISK_LABELS
from .sources.lodging import OverpassLodgingFinder
from .sources.osm import OSMClient
from . import __version__


def _print_report(report) -> None:
    print("=" * 86)
    print(f"线路 {report.route_id}｜计划日期 {report.when}｜出发 {report.meta.get('departure_hour')} 时")
    print("=" * 86)
    print(f"{'路段':<6}{'长度m':<8}{'最大坡度':<10}{'遮荫':<8}{'风险':<6}{'状态':<22}{'置信':<6}")
    print("-" * 86)
    for seg in report.segments:
        print(
            f"{seg.segment_id:<6}{seg.length_m:<8.0f}{seg.max_slope_pct:<10.1f}"
            f"{seg.shaded_ratio * 100:<8.0f}{seg.label:<6}{seg.status:<22}{seg.confidence:<6.2f}"
        )
    print("-" * 86)
    s = summarize(report)
    print(f"风险分布：{s['risk_distribution']}")
    print(f"证据不足路段：{s['insufficient_evidence']}｜被拒证据：{s['rejected_evidence']}｜冲突：{s['conflicts']}")
    print(f"最高风险：{RISK_LABELS.get(s['max_risk'], '?')}")


def _print_audit(result) -> None:
    print("=" * 90)
    print(
        f"路书核验｜{Path(result['guide']).name}｜计划日期 {result['when']}"
        f"｜出发 {result['departure_hour']} 时｜抽取器 {result['extractor']}"
    )
    print("=" * 90)
    for item in result["claims"]:
        print(
            f"{item['claim_id']}  [{item['segment_id']:>3}]  {item['text'][:34]:<34} "
            f"{STATUS_ICONS.get(item['status'], item['status'])}"
        )
        print(f"         └ {item['note'][:76]}")
    print("-" * 90)
    counts = result["counts"]
    print(
        f"共 {result['total']} 条断言｜核验覆盖率 {result['coverage'] * 100:.0f}%｜"
        f"未核实率 {result['unverified_rate'] * 100:.0f}%｜"
        f"矛盾 {counts.get('contradicted', 0)}｜未核实 {counts.get('unverified', 0)}｜"
        f"主观 {counts.get('subjective', 0)}｜支持 {counts.get('supported', 0)}"
    )
    if result.get("extractor_fallback"):
        print(f"[提示] LLM 抽取回退：{result['extractor_fallback'][:80]}")
    if result.get("osm_error"):
        print(f"[提示] OSM 未取到证据：{result['osm_error'][:80]}")


def cmd_audit(args) -> int:
    settings = Settings.load()
    provider = SyntheticProvider(seed="demo")

    osm = None
    if args.osm:
        osm = OSMClient(
            url=settings.overpass_url,
            timeout=settings.overpass_timeout,
            cache_dir=settings.cache_dir,
            radius_m=settings.osm_radius_m,
            offline=False,
        )

    llm = build_llm(settings)
    if args.llm and not settings.llm_ready:
        print(
            "[提示] 加了 --llm 但没有检测到可用的模型配置，本次请用规则抽取。\n"
            "       配置方法见 README「配置 AI」，或先跑 `python -m routeguard config` 检查。",
            file=sys.stderr,
        )
    extractor = build_extractor(args.llm and settings.llm_ready, llm)

    result = audit_route_guide(
        args.gpx,
        args.guide,
        args.when,
        provider,
        args.out,
        departure_hour=args.hour,
        extractor=extractor,
        osm=osm,
    )
    _print_audit(result)
    print(f"\nMarkdown 报告：{Path(args.out) / 'route_audit.md'}")
    print(f"HTML 报告  ：{Path(args.out) / 'route_audit.html'}")
    return 0


def cmd_analyze(args) -> int:
    provider = SyntheticProvider(seed="demo")
    report = run_pipeline(
        args.gpx, args.when, provider, args.out, departure_hour=args.hour
    )
    _print_report(report)
    print(f"\n报告已写入：{Path(args.out) / 'report.json'}")
    return 0


def cmd_plan(args) -> int:
    from .brief import parse_brief
    from .corridor import CORRIDORS
    from .plan_report import print_console, render_html, render_markdown
    from .planner import plan_itinerary

    brief, issues = parse_brief(args.text)
    if issues:
        print("无法开始规划：")
        for i in issues:
            print(f"  · {i}")
        return 2

    if args.corridor:
        brief.corridor = args.corridor
    if args.start:
        brief.start_date = args.start
    if args.daily:
        brief.daily_km_pref = args.daily

    waypoints = CORRIDORS[brief.corridor]
    names = {w.name for w in waypoints}
    if brief.origin not in names or brief.destination not in names:
        print("暂不支持任意起终点。")
        print(f"  当前内置走廊：{' → '.join(w.name for w in waypoints)}")
        print(f"  你的请求：{brief.origin} → {brief.destination}")
        print("  可用 --corridor historic 切换为历史路线走廊。")
        return 2

    legs = None
    router_mode = getattr(args, "router", "auto")
    if router_mode != "none":
        settings = Settings.load()
        providers = []
        if router_mode in ("auto", "brouter"):
            providers.append(BRouterClient())
        if router_mode in ("auto", "osrm"):
            providers.append(OSRMClient())
        if providers:
            print(f"正在用开源路由服务规划 {len(waypoints) - 1} 段真实路线（首次较慢，之后走缓存）…")
            router = Router(providers, cache_dir=settings.cache_dir / "routes")
            legs = build_routed_legs(waypoints, router)
            st = router.stats
            print(
                f"路由完成：新请求 {st['routed']} 段，缓存命中 {st['cache_hit']} 段，"
                f"直线兜底 {st['fallback']} 段\n"
            )

    lodging_finder = None
    if getattr(args, "lodging", "auto") != "off":
        settings = Settings.load()
        lodging_finder = OverpassLodgingFinder(
            url=settings.overpass_url,
            timeout=min(15, settings.overpass_timeout),
            cache_dir=settings.cache_dir / "lodging",
        )

    it = plan_itinerary(brief, waypoints, legs, lodging_finder=lodging_finder)
    print_console(it)
    if brief.warnings:
        print("提示：")
        for w in brief.warnings:
            print(f"  · {w}")

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "plan.md").write_text(render_markdown(it), encoding="utf-8")
    (out / "plan.html").write_text(render_html(it), encoding="utf-8")

    gpx_path = Path(args.gpx_out) if args.gpx_out else (out / "route.gpx")
    write_gpx(gpx_path, it, waypoints, it.legs)
    (out / "plan.json").write_text(
        json.dumps(
            {
                "brief": {
                    "origin": brief.origin,
                    "destination": brief.destination,
                    "start_date": brief.start_date,
                    "corridor": brief.corridor,
                },
                "profile": it.profile_summary,
                "total_km": it.total_km,
                "total_climb": it.total_climb,
                "riding_days": it.riding_days,
                "rest_days": it.rest_days,
                "calendar_days": it.calendar_days,
                "days": [d.__dict__ for d in it.days],
                "warnings": it.warnings,
                "assumptions": it.assumptions,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\n规划报告：{out / 'plan.md'}｜{out / 'plan.html'}")
    legs_used = list(it.legs)
    total_legs = len(legs_used)
    routed_legs = sum(1 for l in legs_used if l.provider not in ("corridor", "straight-fallback"))
    if total_legs and routed_legs == total_legs:
        note = "全部真实路网几何"
    elif routed_legs:
        note = f"{routed_legs}/{total_legs} 段真实路网，其余为直线近似"
    else:
        note = "走廊折线，导航前需 snap 到路网"
    print(f"GPX 轨迹：{gpx_path}（{note}）")

    if not getattr(args, "no_daily_gpx", False):
        daily = write_daily_gpx(out / "daily", it, it.legs)
        print(f"逐日 GPX：{out / 'daily'}（{len(daily)} 个文件，每天一个）")
    return 0


def cmd_config(args) -> int:
    """显示当前识别到的模型配置，可选做一次连通性测试。"""
    settings = Settings.load()
    print("=" * 70)
    print("RouteGuard 配置检查")
    print("=" * 70)
    print(f"  识别到的厂商 : {settings.provider}")
    print(f"  Base URL     : {settings.llm_base_url or '(未配置)'}")
    print(f"  模型         : {settings.llm_model or '(未配置)'}")
    print(f"  API Key      : {settings.masked_key()}")
    print(f"  LLM 可用     : {'是' if settings.llm_ready else '否（将退回规则抽取）'}")
    print(f"  OSM 开关     : {'开' if settings.osm_enabled else '关'}")
    print(f"  Overpass     : {settings.overpass_url}")
    print(f"  缓存目录     : {settings.cache_dir}")

    if not args.check:
        print("\n加 --check 可发一次真实请求，验证 key 是否有效。")
        return 0

    if not settings.llm_ready:
        print("\n[跳过] 没有可用的模型配置，无法测试。")
        return 1

    from .llm import LLMError, build_llm

    client = build_llm(settings)
    try:
        reply = client.chat_json(
            "你是连通性探针，只输出 JSON。", '{"ping": 1}', 0.0
        )
        print(f"\n[成功] 模型返回：{str(reply)[:120]}")
        return 0
    except LLMError as exc:
        print(f"\n[失败] {exc}")
        return 1


def cmd_selftest(args) -> int:
    import tempfile

    from .pipeline import audit_route_guide as audit_fn

    settings = Settings.load()
    print(f"配置：LLM={'就绪' if settings.llm_ready else '未配置'}｜OSM={'开' if settings.osm_enabled else '关'}")
    with tempfile.TemporaryDirectory() as tmp:
        result = audit_fn(
            sample_gpx(),
            sample_guide(),
            "2026-08-15",
            SyntheticProvider(seed="demo"),
            tmp,
            extractor=build_extractor(False, build_llm(settings)),
            osm=None,
        )
        print(f"自检：{result['total']} 条断言，矛盾 {result['counts'].get('contradicted', 0)} 条")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser("routeguard", description="可核验的骑行路书")
    parser.add_argument(
        "--version", action="version", version=f"routeguard {__version__}"
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    def add_common(p):
        p.add_argument("--when", default="2026-08-15", help="计划出发日期 YYYY-MM-DD")
        p.add_argument("--hour", type=int, default=9, help="计划出发小时（本地时）")

    p_audit = sub.add_parser("audit", help="核验一份路书")
    p_audit.add_argument("--guide", default=str(sample_guide()))
    p_audit.add_argument("--gpx", default=str(sample_gpx()))
    p_audit.add_argument("--out", default=str(default_out("audit")))
    p_audit.add_argument("--osm", dest="osm", action="store_true", default=True, help="启用 OSM 证据源（默认开）")
    p_audit.add_argument("--no-osm", dest="osm", action="store_false", help="关闭 OSM，纯离线")
    p_audit.add_argument("--llm", dest="llm", action="store_true", default=False, help="用 LLM 抽取断言")
    add_common(p_audit)

    p_run = sub.add_parser("analyze", help="路段风险裁决")
    p_run.add_argument("--gpx", required=True)
    p_run.add_argument("--out", default=str(default_out("run")))
    add_common(p_run)

    p_demo = sub.add_parser("demo", help="用示范 GPX 跑风险裁决")
    p_demo.add_argument("--out", default=str(default_out("demo")))
    add_common(p_demo)

    p_plan = sub.add_parser("plan", help="从一句自然语言生成多日骑行规划")
    p_plan.add_argument("--text", required=True, help="例如：FTP 250W，体重 70kg，2027 年 6 月从瑞金到延安")
    p_plan.add_argument("--corridor", default=None, choices=["central", "historic"])
    p_plan.add_argument("--start", default=None, help="覆盖出发日期 YYYY-MM-DD")
    p_plan.add_argument("--daily", type=float, default=None, help="覆盖每日目标里程")
    p_plan.add_argument("--out", default=str(default_out("plan")))
    p_plan.add_argument("--gpx-out", default=None, help="GPX 输出路径（默认 <out>/route.gpx）")
    p_plan.add_argument(
        "--no-daily-gpx", action="store_true", help="不导出逐日 GPX（默认导出到 <out>/daily/）"
    )
    p_plan.add_argument(
        "--router",
        choices=["auto", "brouter", "osrm", "none"],
        default="auto",
        help="路由服务：auto=BRouter→OSRM→直线；none=只用走廊直线（快但不可导航）",
    )
    p_plan.add_argument(
        "--lodging",
        choices=["auto", "off"],
        default="auto",
        help="无城镇落脚点时是否用 OSM 检索附近酒店（默认开）",
    )

    sub.add_parser("selftest", help="离线自检")

    p_cfg = sub.add_parser("config", help="查看识别到的模型配置")
    p_cfg.add_argument("--check", action="store_true", help="发一次真实请求验证 key")
    return parser


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    args = build_parser().parse_args(argv)
    if args.cmd == "audit":
        return cmd_audit(args)
    if args.cmd == "analyze":
        return cmd_analyze(args)
    if args.cmd == "demo":
        args.gpx = str(sample_gpx())
        return cmd_analyze(args)
    if args.cmd == "selftest":
        return cmd_selftest(args)
    if args.cmd == "config":
        return cmd_config(args)
    if args.cmd == "plan":
        return cmd_plan(args)
    return 2


if __name__ == "__main__":
    sys.exit(main())
