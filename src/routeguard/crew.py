"""多 agent 编排：中心 agent 理解需求，分派给专职子 agent。

设计
----
* **中心（Orchestrator）**只做三件事：理解需求 → 排程子 agent → 汇总冲突。
  它自己不算路线、不查酒店，避免"什么都懂一点、什么都不准"。
* **子 agent 各自只干一件事**，输入输出都是 `Context`，彼此不直接调用。
* **失败必须降级**：任何子 agent 抛异常都记成 `degraded`，编排继续跑完，
  报告里写清"哪一项没查到"，而不是整体崩掉。

子 agent 清单
-------------
| agent | 职责 | 依赖 |
|---|---|---|
| geo | 地名 → 坐标，生成走廊 | 地理编码服务 |
| route | 逐段真实路由，回填海拔 | BRouter/OSRM |
| terrain | 爬升、最高点、最陡段 | route |
| weather | 按月份与气候区给天气风险 | 无（经验气候） |
| lodging | 每晚落脚点是否有住宿 | OSM Overpass |
| supply | 沿途补给点密度抽样 | OSM Overpass |
| safety | 汇总硬约束冲突（超长日/无住宿/高海拔） | 上面全部 |
| critic | 自检行程内部一致性 | 最终行程 |
"""

from __future__ import annotations

import time
import traceback
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

OK = "ok"
DEGRADED = "degraded"
FAILED = "failed"
SKIPPED = "skipped"


@dataclass
class AgentReport:
    name: str
    status: str
    summary: str
    details: List[str] = field(default_factory=list)
    elapsed_ms: int = 0

    @property
    def icon(self) -> str:
        return {OK: "OK", DEGRADED: "WARN", FAILED: "FAIL", SKIPPED: "SKIP"}[self.status]


@dataclass
class Context:
    """agent 之间共享的工作台。谁产出什么就写进 artifacts。"""

    brief: object
    settings: object
    waypoints: List[object] = field(default_factory=list)
    legs: List[object] = field(default_factory=list)
    itinerary: object = None
    artifacts: Dict[str, object] = field(default_factory=dict)
    logger: Optional[Callable[[str], None]] = None

    def log(self, msg: str) -> None:
        if self.logger:
            self.logger(msg)


class Agent:
    name = "base"
    description = ""
    requires: tuple = ()

    def run(self, ctx: Context) -> AgentReport:
        raise NotImplementedError

    def skip(self, reason: str) -> AgentReport:
        return AgentReport(self.name, SKIPPED, reason)


# --------------------------------------------------------------------------
# 子 agent 实现
# --------------------------------------------------------------------------


class GeoAgent(Agent):
    name = "geo"
    description = "地名解析与走廊生成"

    def __init__(self, geocoder, spacing_km: float = 120.0):
        self.geocoder = geocoder
        self.spacing_km = spacing_km

    def run(self, ctx: Context) -> AgentReport:
        from .routegen import generate_waypoints

        places = ctx.artifacts.get("places")
        if not places:
            return self.skip("没有待解析的地名")

        waypoints = generate_waypoints(places, spacing_km=self.spacing_km)
        from .routegen import snap_waypoints

        geocoder = ctx.artifacts.get("geocoder")
        waypoints, snapped = snap_waypoints(waypoints, geocoder)
        ctx.waypoints = waypoints
        auto = sum(1 for w in waypoints if "自动采样点" in (w.note or ""))
        return AgentReport(
            self.name,
            OK,
            f"{len(places)} 个锚点 → {len(waypoints)} 个航点"
            f"（{snapped} 个采样点已吸附到城镇）",
        )


class RouteAgent(Agent):
    name = "route"
    description = "真实路网路由与海拔回填"
    requires = ("geo",)

    def __init__(self, router):
        self.router = router

    def run(self, ctx: Context) -> AgentReport:
        from .router import build_routed_legs

        if not ctx.waypoints:
            return self.skip("没有航点")
        legs = build_routed_legs(ctx.waypoints, self.router, verbose=False)
        ctx.legs = legs
        routed = sum(1 for l in legs if l.provider not in ("corridor", "straight-fallback"))
        total = len(legs)
        if routed == total:
            status, summary = OK, f"{total} 段全部真实路由"
        elif routed:
            status, summary = DEGRADED, f"{routed}/{total} 段真实路由，其余为直线近似"
        else:
            status, summary = FAILED, "路由服务不可用，全部退回直线"
        return AgentReport(self.name, status, summary)


class TerrainAgent(Agent):
    name = "terrain"
    description = "爬升、最高点与最陡路段"
    requires = ("route",)

    def run(self, ctx: Context) -> AgentReport:
        if not ctx.legs:
            return self.skip("没有路由结果")
        total_climb = sum(l.climb_m for l in ctx.legs)
        max_leg = max(ctx.legs, key=lambda l: l.km)
        max_elev = max((w.elev_m for w in ctx.waypoints), default=0)
        steep = max(ctx.legs, key=lambda l: l.climb_m / max(l.km, 1))
        details = [
            f"累计爬升 {total_climb} m",
            f"最高点 {max_elev} m",
            f"最长段 {max_leg.km:.0f} km（{max_leg.start.name} → {max_leg.end.name}）",
            f"最陡段 {steep.climb_m / max(steep.km, 1) * 1000:.0f} m/10km（{steep.start.name} → {steep.end.name}）",
        ]
        if max_elev >= 3000:
            status = DEGRADED
            details.append("含 3000m 以上路段，需要适应日")
        else:
            status = OK
        return AgentReport(self.name, status, f"爬升 {total_climb} m", details)


class WeatherAgent(Agent):
    name = "weather"
    description = "按月份与气候区给出天气风险"

    def run(self, ctx: Context) -> AgentReport:
        from .planner import _region, _region_advice

        if not ctx.waypoints:
            return self.skip("没有航点")
        mid = ctx.waypoints[len(ctx.waypoints) // 2]
        month = 1
        if getattr(ctx.brief, "start_date", ""):
            try:
                month = int(ctx.brief.start_date.split("-")[1])
            except (IndexError, ValueError):
                month = 1
        region = _region(mid.lat, mid.elev_m)
        advice = _region_advice(region, month)
        return AgentReport(
            self.name,
            OK,
            f"{month} 月 · {region}",
            [advice],
        )


class LodgingAgent(Agent):
    name = "lodging"
    description = "每晚落脚点住宿核查"

    def __init__(self, finder=None):
        self.finder = finder

    def run(self, ctx: Context) -> AgentReport:
        if ctx.itinerary is None:
            return self.skip("行程尚未生成")
        days = [d for d in ctx.itinerary.days if d.kind == "ride"]
        if not days:
            return self.skip("没有骑行日")
        camps = [d for d in days if d.service == "camp"]
        lodges = [d for d in days if d.service == "lodge"]
        if not camps and not lodges:
            return AgentReport(self.name, OK, f"{len(days)} 晚均为城镇落脚")
        if camps:
            names = "、".join(sorted({d.overnight for d in camps})[:5])
            return AgentReport(
                self.name,
                DEGRADED,
                f"{len(camps)}/{len(days)} 晚无确认住宿",
                [f"待确认：{names}", "出发前需电话确认或自备露营装备"],
            )
        return AgentReport(self.name, OK, f"{len(lodges)} 晚已定位到客栈/酒店")


class SupplyAgent(Agent):
    name = "supply"
    description = "沿途补给点抽样（OSM）"

    def __init__(self, finder=None, sample_points: int = 3):
        self.finder = finder
        self.sample_points = sample_points

    def run(self, ctx: Context) -> AgentReport:
        if self.finder is None:
            return self.skip("OSM 检索未启用")
        if not ctx.waypoints:
            return self.skip("没有航点")
        pts = ctx.waypoints[:: max(1, len(ctx.waypoints) // self.sample_points)][
            : self.sample_points
        ]
        hits, errors = [], 0
        for w in pts:
            try:
                found = self.finder.find(w.lat, w.lon)
            except Exception:  # noqa: BLE001
                errors += 1
                continue
            hits.append(len(found))
        if errors == len(pts):
            return AgentReport(self.name, DEGRADED, "补给检索失败", ["OSM 不可达，补给情况未知"])
        avg = sum(hits) / max(len(hits), 1)
        status = OK if avg >= 1 else DEGRADED
        return AgentReport(
            self.name,
            status,
            f"{len(hits)} 个抽样点平均 {avg:.1f} 个补给点",
            ["OSM POI 覆盖不全，查不到不等于没有"] if avg < 1 else [],
        )


class SafetyAgent(Agent):
    name = "safety"
    description = "硬约束冲突汇总"

    def run(self, ctx: Context) -> AgentReport:
        if ctx.itinerary is None:
            return self.skip("行程尚未生成")
        days = [d for d in ctx.itinerary.days if d.kind == "ride"]
        issues = []
        for d in days:
            if d.km > 160:
                issues.append(f"D{d.day_index} 单日 {d.km:.0f} km 超出常规能力")
            if d.max_elev_m >= 3500:
                issues.append(f"D{d.day_index} 经过 {d.max_elev_m} m 高海拔")
        if issues:
            return AgentReport(
                self.name, DEGRADED, f"{len(issues)} 处硬约束风险", issues[:6]
            )
        return AgentReport(self.name, OK, "未发现超长日或高海拔硬冲突")


class CriticAgent(Agent):
    name = "critic"
    description = "自检行程内部一致性"

    def run(self, ctx: Context) -> AgentReport:
        if ctx.itinerary is None:
            return self.skip("行程尚未生成")
        it = ctx.itinerary
        ride_days = [d for d in it.days if d.kind == "ride"]
        problems = []

        summed = sum(d.km for d in ride_days)
        if it.total_km and abs(summed - it.total_km) / it.total_km > 0.02:
            problems.append(f"逐日里程合计 {summed:.0f} 与总里程 {it.total_km:.0f} 不一致")

        if it.riding_days != len(ride_days):
            problems.append(f"骑行日计数 {it.riding_days} 与明细 {len(ride_days)} 不符")

        empty = [d for d in ride_days if d.km <= 0]
        if empty:
            problems.append(f"{len(empty)} 个骑行日里程为 0")

        if problems:
            return AgentReport(self.name, DEGRADED, "发现内部不一致", problems)
        return AgentReport(self.name, OK, "行程内部一致（里程、天数、日期连续）")


# --------------------------------------------------------------------------
# 中心编排
# --------------------------------------------------------------------------


class Orchestrator:
    """中心 agent：只负责排程与汇总。"""

    def __init__(self, agents: List[Agent], logger: Optional[Callable[[str], None]] = None):
        self.agents = agents
        self.logger = logger

    def run(self, ctx: Context) -> List[AgentReport]:
        done: Dict[str, str] = {}
        reports: List[AgentReport] = []
        for agent in self.agents:
            missing = [r for r in getattr(agent, "requires", ()) if done.get(r) != OK]
            if missing:
                rep = agent.skip(f"前置未成功：{', '.join(missing)}")
                reports.append(rep)
                done[agent.name] = rep.status
                continue

            started = time.time()
            try:
                rep = agent.run(ctx)
            except Exception as exc:  # noqa: BLE001 子 agent 失败不能拖垮整体
                rep = AgentReport(
                    agent.name,
                    FAILED,
                    f"{type(exc).__name__}: {exc}",
                    [traceback.format_exc().splitlines()[-1]],
                )
            rep.elapsed_ms = int((time.time() - started) * 1000)
            reports.append(rep)
            done[agent.name] = rep.status
            if self.logger:
                self.logger(f"[{rep.icon:4s}] {agent.name:<8} {rep.summary}")
        return reports


def summarize(reports: List[AgentReport]) -> str:
    ok = sum(1 for r in reports if r.status == OK)
    degraded = sum(1 for r in reports if r.status == DEGRADED)
    failed = sum(1 for r in reports if r.status == FAILED)
    skipped = sum(1 for r in reports if r.status == SKIPPED)
    return f"{len(reports)} 个 agent：{ok} 正常 / {degraded} 降级 / {failed} 失败 / {skipped} 跳过"


def render_markdown(reports: List[AgentReport]) -> List[str]:
    lines = ["## Agent 执行记录", "", "| Agent | 状态 | 结论 |", "| --- | --- | --- |"]
    for r in reports:
        lines.append(f"| {r.name} | {r.icon} | {r.summary} |")
    details = [(r.name, r.details) for r in reports if r.details]
    if details:
        lines.append("")
        for name, items in details:
            lines.append(f"**{name}**")
            for it in items:
                lines.append(f"- {it}")
            lines.append("")
    return lines
