"""命令行入口。

    python -m routeguard audit                 # 核验示范路书（含 HTML 报告）
    python -m routeguard analyze --gpx ...     # 路段风险裁决
    python -m routeguard selftest              # 自检：不联网跑一遍核心链路
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace

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


def cmd_agent(args) -> int:
    """harness 模式：模型自己选工具、调技能、读记忆。"""
    from .geocode import build_geocoder
    from .harness import Memory, SkillLibrary, default_tools, run_agent
    from .llm import build_llm
    from .router import BRouterClient, OSRMClient, Router
    from .sources.lodging import OverpassLodgingFinder

    settings = Settings.load()
    if not settings.llm_ready:
        print("agent 模式需要大模型：请先配置 DEEPSEEK_API_KEY 等，或运行 `routeguard config --check`。")
        return 2

    geocoder = build_geocoder(settings.cache_dir / "geocode")
    router = Router(
        [BRouterClient(profile="fastbike"), OSRMClient()],
        cache_dir=settings.cache_dir / "routes",
    )
    finder = _make_finder(args, settings)
    tools = default_tools(settings, geocoder=geocoder, router=router, lodging=finder)
    skills = SkillLibrary()
    memory = Memory()

    print(f"工具 {len(tools.names())} 个：{', '.join(tools.names())}")
    print(f"技能 {len(skills.skills)} 个：{', '.join(s.name for s in skills.skills)}")
    trace = run_agent(args.text, build_llm(settings), tools, skills, memory)

    for step in trace.steps:
        if "tool" in step:
            ok = step["result"].get("ok")
            print(f"  [{step['step'] + 1}] {step.get('thought', '')[:40]} → {step['tool']} "
                  f"({'OK' if ok else 'FAIL'})")
        elif "error" in step:
            print(f"  [{step.get('step', 0) + 1}] 出错：{step['error'][:120]}")
            if "raw" in step:
                print(f"       模型原始输出：{str(step['raw'])[:160]}")
        elif "final" in step:
            print(f"  [{step['step'] + 1}] 结论")

    if trace.used_skills:
        print(f"命中技能：{', '.join(trace.used_skills)}")
    if not trace.final:
        print("agent 没有给出结论，可用 `plan` 走确定性流程。")
        return 2

    origin = trace.final.get("origin")
    destination = trace.final.get("destination")
    print(f"\n结论：{origin} → {destination}")
    if trace.final.get("summary"):
        print(trace.final["summary"])

    if origin and destination:
        memory.remember("last_origin", origin)
        memory.remember("last_destination", destination)
        print("\n接着用确定性流程出具体行程：")
        # 复用 agent 已经算好的路线，避免再请求一次（也避免第二次失败）
        from . import harness

        cached = dict(harness.LAST_ROUTE or {})
        if cached.get("legs") and cached.get("waypoints"):
            print("复用 agent 已算出的路线，不再重复请求。")
            from .brief import parse_brief

            brief2, _ = parse_brief(args.text)
            brief2.origin = cached.get("start") or brief2.origin
            brief2.destination = cached.get("end") or brief2.destination
            brief2.start_date = brief2.start_date or ""
            return _finish_from_legs(
                args, brief2, cached["waypoints"], cached["legs"], Settings.load()
            )

        args2 = SimpleNamespace(
            text=f"从{origin}到{destination}",
            corridor=None,
            start=None,
            daily=None,
            out=args.out,
            gpx_out=None,
            router=getattr(args, "router", "auto"),
            lodging=getattr(args, "lodging", "auto"),
            lodging_provider=getattr(args, "lodging_provider", "auto"),
            no_daily_gpx=getattr(args, "no_daily_gpx", False),
            spacing=getattr(args, "spacing", 120.0),
            no_ask=True,
            crew=False,
            cities=False,
            no_llm=False,
            llm=False,
            via=None,
        )
        return cmd_plan(args2)
    return 0


def _gpx_filename(args) -> str:
    """直线近似模式下文件名要自曝身份，防止误拖进码表。"""
    if getattr(args, "router", "auto") == "none":
        return "route_直线近似_不可导航.gpx"
    return "route.gpx"


def _make_finder(args, settings):
    """优先用国内免费数据源（高德/天地图），没有 key 时退回 OSM。"""
    from .sources.lodging import OverpassLodgingFinder
    from .sources.lodging_cn import build_cn_finder

    provider = getattr(args, "lodging_provider", "auto") or "auto"
    if provider != "osm":
        finder = build_cn_finder(provider, settings, settings.cache_dir / "lodging")
        if finder is not None:
            return finder
        if provider in ("amap", "tianditu"):
            print(f"[提示] 未配置 {provider} 的 key，改用 Photon（无需 key，OSM 数据）")

    # 没有商业 key：Photon 优先（快），查不到再退 Overpass（全但慢）
    from .sources.elevation import ChainFinder, PhotonLodgingFinder
    from .sources.lodging import OverpassLodgingFinder

    print("住宿数据源：Photon（无需 key）→ Overpass 兜底")
    return ChainFinder(
        [
            PhotonLodgingFinder(cache_dir=settings.cache_dir / "lodging"),
            OverpassLodgingFinder(
                url=settings.overpass_url,
                timeout=min(15, settings.overpass_timeout),
                cache_dir=settings.cache_dir / "lodging",
            ),
        ]
    )


def _finish_from_legs(args, brief, waypoints, legs, settings) -> int:
    """已有航点与几何时，直接出报告，不再请求路由服务。"""
    from .plan_report import print_console, render_html
    from .plan_report import render_markdown as render_itinerary
    from .planner import plan_itinerary

    finder = None
    if getattr(args, "lodging", "auto") != "off":
        finder = _make_finder(args, settings)

    brief.corridor = "generated"
    it = plan_itinerary(brief, waypoints, legs, lodging_finder=finder, split_legs=False)
    print()
    print_console(it)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "plan.md").write_text(render_itinerary(it), encoding="utf-8")
    (out / "plan.html").write_text(render_html(it), encoding="utf-8")
    write_gpx(out / _gpx_filename(args), it, waypoints, it.legs)
    if not getattr(args, "no_daily_gpx", False):
        daily = write_daily_gpx(out / "daily", it, it.legs)
        print(f"逐日 GPX：{out / 'daily'}（{len(daily)} 个文件）")
    print(f"\n规划报告：{out / 'plan.md'}｜{out / 'plan.html'}")
    return 0


def _plan_direct(args, brief, places, settings, geocoder) -> int:
    """最省事的路径：一次 OSRM 请求拿整条路线，按日切分，出报告。"""
    from .plan_report import print_console, render_html
    from .plan_report import render_markdown as render_itinerary
    from .planner import daily_target
    from .routegen import direct_legs

    # BRouter bike profiles detour far less than OSRM-bike in China
    # (measured Shanghai->Fuzhou: 1052 km vs 1698 km; real road ~950 km)
    router = Router(
        [BRouterClient(profile="fastbike"), BRouterClient(profile="trekking"), OSRMClient()],
        cache_dir=settings.cache_dir / "routes",
    )
    print("正在请求整条路线（BRouter 优先，首次约 1 分钟）…")
    waypoints, legs = direct_legs(router, places[0], places[-1], daily_target(brief), geocoder)
    if legs and sum(l.climb_m for l in legs) == 0:
        from .routegen import fill_missing_elevation

        legs, filled = fill_missing_elevation(legs, settings.cache_dir / "elevation")
        print(f"路由未返回高程，已用 OpenTopoData 补齐 {filled} 个点")
    if not waypoints:
        print("路由服务没有返回可用路线。")
        return 2

    brief.origin = places[0].name
    brief.destination = places[-1].name
    brief.corridor = "generated"

    total = sum(l.km for l in legs)
    print(f"整条路线 {total:.0f} km，切成 {len(legs)} 天（引擎：{legs[0].provider}）")

    finder = None
    if getattr(args, "lodging", "auto") != "off":
        finder = _make_finder(args, settings)

    it = plan_itinerary(brief, waypoints, legs, lodging_finder=finder, split_legs=False)
    print()
    print_console(it)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "plan.md").write_text(render_itinerary(it), encoding="utf-8")
    (out / "plan.html").write_text(render_html(it), encoding="utf-8")
    write_gpx(out / _gpx_filename(args), it, waypoints, it.legs)
    if not getattr(args, "no_daily_gpx", False):
        daily = write_daily_gpx(out / "daily", it, it.legs)
        print(f"逐日 GPX：{out / 'daily'}（{len(daily)} 个文件）")
    print(f"\n规划报告：{out / 'plan.md'}｜{out / 'plan.html'}")
    return 0


def _plan_with_crew(args, brief, places, settings) -> int:
    """中心 agent 编排模式：子 agent 分头干活，最后汇总。"""
    from .crew import (
        Context,
        CriticAgent,
        GeoAgent,
        LodgingAgent,
        Orchestrator,
        RouteAgent,
        SafetyAgent,
        SupplyAgent,
        TerrainAgent,
        WeatherAgent,
        render_markdown as render_agents,
        summarize,
    )
    from .plan_report import print_console, render_html
    from .plan_report import render_markdown as render_itinerary
    from .planner import plan_itinerary
    from .sources.lodging import OverpassLodgingFinder

    router = None
    if getattr(args, "router", "auto") != "none":
        router = Router(
            [BRouterClient(), OSRMClient()], cache_dir=settings.cache_dir / "routes"
        )
    finder = None
    if getattr(args, "lodging", "auto") != "off":
        finder = _make_finder(args, settings)

    ctx = Context(brief=brief, settings=settings, logger=lambda m: print(f"  {m}"))
    ctx.artifacts["places"] = places
    from .geocode import build_geocoder as _bg

    ctx.artifacts["geocoder"] = _bg(settings.cache_dir / "geocode")

    print("中心 agent 分派任务：")
    from .planner import daily_target as _dt
    _spacing = min(float(getattr(args, "spacing", 120) or 120), _dt(brief) * 0.8)
    agents = [GeoAgent(None, spacing_km=_spacing)]
    if router is not None:
        agents.append(RouteAgent(router))
    agents += [TerrainAgent(), WeatherAgent()]
    reports = Orchestrator(agents, logger=ctx.logger).run(ctx)

    if not ctx.waypoints:
        print("子 agent 未能生成航点，流程中止。")
        return 2

    brief.corridor = "generated"
    it = plan_itinerary(brief, ctx.waypoints, ctx.legs or None, lodging_finder=finder)
    ctx.itinerary = it

    print("结果核验：")
    reports += Orchestrator(
        [LodgingAgent(), SupplyAgent(finder), SafetyAgent(), CriticAgent()],
        logger=ctx.logger,
    ).run(ctx)

    print()
    print_console(it)
    print(f"\n{summarize(reports)}")

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    md = render_itinerary(it)
    md = md.replace(
        "## 九、方法与局限", "\n".join(render_agents(reports)) + "\n\n## 九、方法与局限"
    )
    (out / "plan.md").write_text(md, encoding="utf-8")
    (out / "plan.html").write_text(render_html(it), encoding="utf-8")
    write_gpx(out / "route.gpx", it, ctx.waypoints, it.legs)
    if not getattr(args, "no_daily_gpx", False):
        daily = write_daily_gpx(out / "daily", it, it.legs)
        print(f"逐日 GPX：{out / 'daily'}（{len(daily)} 个文件）")
    print(f"\n规划报告：{out / 'plan.md'}｜{out / 'plan.html'}")
    return 0


def cmd_plan(args) -> int:
    from .clarify import non_interactive_hint
    from .corridor import CORRIDORS, waypoints_from_places
    from .geocode import build_geocoder, split_via
    from .intent import extract_intent, fill_missing, intent_to_brief
    from .plan_report import print_console, render_html, render_markdown
    from .planner import plan_itinerary
    from .routegen import generate_waypoints, generated_summary, snap_waypoints

    settings = Settings.load()
    use_llm = (not getattr(args, "no_llm", False)) and settings.llm_ready
    llm_client = build_llm(settings) if use_llm else None
    intent = extract_intent(args.text, llm_client)
    if intent.source == "llm":
        print("[理解] 由大模型解析需求")
    elif use_llm:
        print("[理解] 模型调用失败，已退回规则解析")

    interactive = sys.stdin.isatty() and not getattr(args, "no_ask", False)
    notes: list[str] = []
    if intent.missing and interactive:
        notes = fill_missing(intent, input)

    # 非交互场景：模型对知名线路给过建议时，先采用并明确标注，而不是直接失败
    adopted: list[str] = []
    if not interactive:
        if not intent.origin and intent.suggested_origin:
            intent.origin = intent.suggested_origin
            adopted.append(f"起点用模型建议：{intent.suggested_origin}")
        if not intent.destination and intent.suggested_destination:
            intent.destination = intent.suggested_destination
            adopted.append(f"终点用模型建议：{intent.suggested_destination}")
        if adopted:
            reason = f"（{intent.suggestion_reason}）" if intent.suggestion_reason else ""
            print(f"[建议] 模型推测这条线路的常规起终点{reason}")
            for a in adopted:
                print(f"       {a}　如果不对，请加 --via 或写明「从 A 到 B」")

    via = getattr(args, "via", None) or ",".join(intent.via) or ""
    via_names = split_via(via) if via else []
    if via_names:
        intent.origin = intent.origin or via_names[0]
        intent.destination = intent.destination or via_names[-1]

    brief = intent_to_brief(intent, args.text)

    blocking = []
    if not intent.origin:
        blocking.append("缺起点")
    if not intent.destination:
        blocking.append("缺终点")
    if blocking:
        print("无法开始规划：")
        for i in blocking:
            print(f"  · {i}")
        for tip in non_interactive_hint(brief, ["起点和终点"]):
            print(f"  → {tip}")
        return 2

    for n in notes:
        print(f"[澄清] {n}")
    if intent.preferences:
        print(f"[理解] 偏好：{'、'.join(intent.preferences)}")

    if args.corridor:
        brief.corridor = args.corridor
    if args.start:
        brief.start_date = args.start
    if args.daily:
        brief.daily_km_pref = args.daily

    loop_requested = brief.origin == brief.destination and brief.destination != ""
    builtin = CORRIDORS.get(brief.corridor, [])
    builtin_names = {w.name for w in builtin}
    use_builtin = (
        not via
        and not args.corridor
        and brief.origin in builtin_names
        and brief.destination in builtin_names
    )

    if use_builtin:
        waypoints = builtin
        print(f"使用内置走廊：{brief.corridor}（人工核对过海拔与住宿）")
    elif loop_requested and not via_names:
        print("起点和终点相同，但没给途经点——环线可以是任意形状，我猜不出来。")
        print('  请用 --via 指明方向，例如：')
        print('  --via "海口,文昌,万宁,三亚,五指山,琼中"')
        return 2
    else:
        settings = Settings.load()
        geocoder = build_geocoder(cache_dir=settings.cache_dir / "geocode")
        queries = list(via_names) if via_names else [brief.origin, brief.destination]
        densify = True

        # 没给途经点时，先让模型给出沿途城市链；否则只能在大圆直线上撒点，
        # 结果就是一条贯穿荒野的直线，看着不像真实路线。
        if not via_names and llm_client is not None and getattr(args, "cities", False):
            from .intent import suggest_route_cities

            cities = suggest_route_cities(llm_client, brief.origin, brief.destination)
            if cities:
                print(f"[路线] 模型建议沿途经过：{'、'.join(cities)}")
                queries = [brief.origin] + cities + [brief.destination]
                densify = False  # 城市已足够密，不再插值

        def _bare(q: str) -> str:
            return q.split("@")[0].strip()

        # 途经点里已包含起点/终点时别再塞一遍，否则会拿裸地名去联网重查
        if _bare(queries[0]) != _bare(brief.origin):
            queries.insert(0, brief.origin)
        if _bare(queries[-1]) != _bare(brief.destination):
            queries.append(brief.destination)

        deduped: list[str] = []
        for q in queries:
            if q and (not deduped or deduped[-1] != q):
                deduped.append(q)

        print(f"正在解析 {len(deduped)} 个地名（OSM Nominatim，首次较慢）…")
        places, errors = geocoder.lookup_many(deduped)
        if errors:
            print("以下地名没能定位：")
            for e in errors:
                print(f"  · {e}")
            print('改用坐标写法：--via "海口@20.03,110.32;三亚@18.25,109.51"')
            return 2
        for p in places:
            print(f"  {p.name:<14} {p.lat:.4f}, {p.lon:.4f}")

        if getattr(args, "crew", False):
            return _plan_with_crew(args, brief, places, settings)

        if (not via_names and not getattr(args, "cities", False)
                and getattr(args, "router", "auto") != "none"
                and len(places) >= 2):
            return _plan_direct(args, brief, places, settings, geocoder)

        from .planner import daily_target

        # 采样间距跟着当日能力走，否则每个航点都超过日预算，
        # 一天只能走一段，被迫排出"每天 133 km"这种假行程。
        spacing = min(
            float(getattr(args, "spacing", 120) or 120),
            daily_target(brief) * 0.8,
        )
        if len(places) >= 2:
            waypoints = generate_waypoints(places, spacing_km=spacing if densify else 1e9)
            waypoints, snapped = snap_waypoints(waypoints, geocoder)
            if snapped:
                print(f"已把 {snapped} 个采样点吸附到最近城镇")
        else:
            waypoints = waypoints_from_places(places)
        brief.origin = places[0].name
        brief.destination = places[-1].name
        brief.corridor = "generated"
        print(f"自动生成 {len(waypoints)} 个航点（采样间距约 {spacing:.0f} km）")

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
    auto_note = generated_summary(waypoints)
    if auto_note:
        it.warnings.insert(0, auto_note)
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


def cmd_clean(args) -> int:
    """清理缓存（默认只删 .cache，--all 连 out 一起删）。"""
    import shutil

    settings = Settings.load()
    targets = [settings.cache_dir]
    if getattr(args, "all", False):
        targets.append(Path.cwd() / "out")

    for path in targets:
        if not path.exists():
            print(f"跳过（不存在）：{path}")
            continue
        files = sum(1 for _ in path.rglob("*") if _.is_file())
        size = sum(f.stat().st_size for f in path.rglob("*") if f.is_file()) / 1e6
        shutil.rmtree(path, ignore_errors=True)
        print(f"已删除 {path}（{files} 个文件，{size:.1f} MB）")
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
    p_plan.add_argument(
        "--via",
        default=None,
        help='自定义途经点，逗号分隔，如 "海口,文昌,万宁,三亚"；也可写坐标 "110.32,20.03"',
    )
    p_plan.add_argument("--start", default=None, help="覆盖出发日期 YYYY-MM-DD")
    p_plan.add_argument("--daily", type=float, default=None, help="覆盖每日目标里程")
    p_plan.add_argument("--spacing", type=float, default=120.0, help="自定义路线的采样间距（km）")
    p_plan.add_argument("--cities", action="store_true", help="实验：让模型给出沿途城市链（尚未稳定）")
    p_plan.add_argument("--crew", action="store_true", help="多 agent 编排：地理/路由/地形/天气/住宿/补给/风险/自检")
    p_plan.add_argument(
        "--lodging-provider",
        choices=["auto", "amap", "tianditu", "osm"],
        default="auto",
        help="住宿数据源：auto=高德→天地图→OSM",
    )
    p_plan.add_argument("--no-llm", action="store_true", help="不用大模型解析需求，强制走规则")
    p_plan.add_argument("--llm", action="store_true", help="强制使用大模型解析需求（未配置会提示）")
    p_plan.add_argument("--no-ask", action="store_true", help="不进入澄清对话，缺信息直接报错")
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

    p_web = sub.add_parser("web", help="启动本地网页版（浏览器操作）")
    p_web.add_argument("--port", type=int, default=8765)
    p_web.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")

    p_ag = sub.add_parser("agent", help="harness 模式：agent 自主调用工具与技能")
    p_ag.add_argument("--text", required=True)
    p_ag.add_argument("--lodging-provider", choices=["auto", "amap", "tianditu", "osm"], default="auto")
    p_ag.add_argument("--lodging", choices=["auto", "off"], default="auto")
    p_ag.add_argument("--router", choices=["auto", "brouter", "osrm", "none"], default="auto")
    p_ag.add_argument("--spacing", type=float, default=120.0)
    p_ag.add_argument("--no-daily-gpx", action="store_true")
    p_ag.add_argument("--out", default=str(default_out("plan")))
    p_clean = sub.add_parser("clean", help="清理缓存")
    p_clean.add_argument("--all", action="store_true", help="连 out/ 一起删")

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
    if args.cmd == "agent":
        return cmd_agent(args)
    if args.cmd == "web":
        from .web import serve
        return serve(port=args.port, open_browser=not args.no_browser)
    if args.cmd == "clean":
        return cmd_clean(args)
    if args.cmd == "config":
        return cmd_config(args)
    if args.cmd == "plan":
        return cmd_plan(args)
    return 2


if __name__ == "__main__":
    sys.exit(main())
