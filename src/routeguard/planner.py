"""规划器：把出行简报 + 路线走廊变成逐日行程。

核心是三个模型：
1. **能力模型**：由 W/kg 推出日行基准，再按行李、季节、爬升折算；
2. **拆分模型**：贪心累加航段，达到当日预算且落脚点有住宿时收工；
3. **风险模型**：单段超能力、住宿缺失、连续高强度、季节气候，逐条给出提示。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import List, Optional, Sequence, Tuple

from .brief import TripBrief
from .corridor import CORRIDOR_LABELS, Leg, Waypoint, build_legs
from .sources.lodging import nearest_lodging

CLIMB_PENALTY_KM_PER_1000M = 20.0
LUGGAGE_FACTOR = {"轻装": 1.0, "驮包（约 8kg）": 0.9, "重装（露营）": 0.8}
SEASON_FACTOR = {6: 0.95, 7: 0.9, 8: 0.9, 9: 1.0, 10: 1.0, 4: 0.95, 5: 1.0}


@dataclass
class DayPlan:
    day_index: int
    date: str
    kind: str  # ride / rest
    start: str
    end: str
    km: float
    climb_m: int
    overnight: str
    service: str
    end_lat: float = 0.0
    end_lon: float = 0.0
    max_elev_m: int = 0
    towns: int = 1
    leg_from: int = 0
    leg_to: int = 0
    legs: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


@dataclass
class Itinerary:
    brief: TripBrief
    corridor: str
    corridor_label: str
    total_km: float
    total_climb: int
    riding_days: int
    rest_days: int
    calendar_days: int
    daily_target_km: float
    max_gap_km: float = 0.0
    max_gap_label: str = ""
    max_elev_m: int = 0
    days: List[DayPlan] = field(default_factory=list)
    legs: List[Leg] = field(default_factory=list)
    camp_points: List[dict] = field(default_factory=list)
    climate_advice: List[str] = field(default_factory=list)
    prep_tips: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    assumptions: List[str] = field(default_factory=list)
    profile_summary: dict = field(default_factory=dict)


def _safe_int(value, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _safe_month(start_date) -> int:
    """从 YYYY-MM-DD 里取月份；缺失或非法时退回 7 月（中性季节）。

    agent 交接过来的行程可能没有日期，直接 int(start_date[5:7]) 会抛
    ValueError: invalid literal for int() with base 10: ''
    """
    try:
        m = int(str(start_date)[5:7])
        return m if 1 <= m <= 12 else 7
    except (ValueError, IndexError, TypeError):
        return 7


def _altitude_factor(elev_m: float) -> float:
    """海拔越高，可承受的日里程越低。"""
    if elev_m < 2000:
        return 1.0
    if elev_m < 2500:
        return 0.9
    if elev_m < 3000:
        return 0.8
    if elev_m < 3500:
        return 0.65
    return 0.5


def split_long_legs(
    legs: List[Leg], target_km: float, factor_fn=None
) -> Tuple[List[Leg], int]:
    """把「单腿超过该海拔下的日能力」的腿按几何切开。

    航点之间常常相隔 150 km 以上，真实路网更长；不切就只能排出"一天 180 km"。
    切点标为 `camp`（无固定住宿），由上层提示需自备露营装备。
    注意：只有**真正超限**才切，105 km 的腿在 109 km 预算下不该被拆成两天。
    """
    factor_fn = factor_fn or _altitude_factor
    if target_km <= 0:
        return legs, 0
    out: List[Leg] = []
    added = 0
    for leg in legs:
        allowed = target_km * factor_fn(max(leg.start.elev_m, leg.end.elev_m))
        # 15% 容差不拆：110 km 的腿在 109 km 预算下不值得拆成两天
        if leg.km <= allowed * 1.15 or not leg.points or len(leg.points) < 4:
            out.append(leg)
            continue
        n = max(2, int(math.ceil(leg.km / (allowed * 1.05))))
        pts = list(leg.points)
        prev_wp = leg.start
        for k in range(n):
            a = round(len(pts) * k / n)
            b = round(len(pts) * (k + 1) / n)
            sub = pts[a : b + 1]
            if len(sub) < 2:
                continue
            is_last = k == n - 1
            ele = _safe_int(sub[-1][2])
            if is_last:
                name = leg.end.name
            else:
                # 名字太长会把表格撑乱；嵌套的"途中"也要去掉
                base = f"{leg.start.name.split('–')[0]}-{leg.end.name.split('–')[-1]}"
                base = base.replace("途中", "").replace("  ", " ").strip(" -")
                suffix = "途中" if n == 2 else f"途中{k + 1}"
                name = f"{base} {suffix}"
                if len(name) > 14:
                    name = f"{base[:9]}… {suffix}"
            wp = Waypoint(
                name=name,
                lat=sub[-1][0],
                lon=sub[-1][1],
                service="camp" if not is_last else leg.end.service,
                climb_from_prev_m=0,
                note="" if is_last else "途中无固定住宿",
                elev_m=ele,
                terrain=leg.end.terrain,
            )
            sub_km = leg.km * len(sub) / len(pts) * 1.02
            out.append(
                Leg(
                    start=prev_wp,
                    end=wp,
                    km=round(sub_km, 1),
                    climb_m=int(leg.climb_m * (len(sub) / len(pts))),
                    points=sub,
                    provider=leg.provider,
                )
            )
            prev_wp = wp
            if not is_last:
                added += 1
    return out, added


def daily_target(brief: TripBrief) -> float:
    base = brief.rider.base_daily_km
    factor = LUGGAGE_FACTOR.get(brief.luggage, 0.9)
    month = _safe_month(getattr(brief, 'start_date', ''))
    season = SEASON_FACTOR.get(month, 1.0)
    target = base * factor * season
    if brief.daily_km_pref:
        target = brief.daily_km_pref
    return round(target, 1)


def _season_advisory(lat: float, month: int) -> str:
    if month in (6, 7, 8):
        if lat < 30:
            return "夏季高温多雨，建议 6–10 点、16–19 点骑行，正午避暑"
        if lat < 34:
            return "初夏闷热，午后易雷阵雨，注意补水与雷电"
        return "昼夜温差大、日晒强，早晚偏凉需带风衣"
    if month in (11, 12, 1, 2):
        return "冬季寒冷，北方路段可能结冰，需防滑装备"
    return "气候相对适宜，仍需关注逐日预报"


def _region(lat: float, elev: int = 0) -> str:
    """按纬度与海拔粗略划分气候区。

    早期版本按长征线的城市写死，导致海南被标成"江西—湖南"。
    现在改为纯纬度分带，任何航线都能落到合理区间。
    """
    if elev >= 3000:
        return "高原段（海拔 3000m 以上）"
    if lat < 21.0:
        return "热带段（海南—雷州半岛）"
    if lat < 25.0:
        return "华南段（两广—闽南）"
    if lat < 29.5:
        return "中南段（湘赣—桂北—黔东南）"
    if lat < 34.2:
        return "中段（四川盆地—陕南—陇南）"
    return "北段（关中—陕北—陇东）"


def _region_advice(region: str, month: int) -> str:
    summer = month in (6, 7, 8)
    if region.startswith("高原"):
        return (
            "单日控制在 60–80km，午后山口风大；6–8 月是雨季，注意塌方、雷电与失温；"
            "每天补水 3L 以上，出现头痛/呕吐立即下撤到低海拔"
            if summer
            else "气温低、可能积雪结冰，需防滑与保暖装备"
        )
    if region.startswith("热带"):
        return (
            "11:00–15:00 是湿热高峰，尽量早晚骑；紫外线强，长袖+防晒霜；"
            "台风季关注预警，单日控制在 80–100km"
            if summer
            else "1–2 月是最佳季节，但仍有台风尾巴与强降雨，随身雨衣；正午紫外线依旧强"
        )
    if region.startswith("华南"):
        return (
            "6:00 出发、正午避暑；前汛期暴雨集中，注意城市内涝与山路塌方"
            if summer
            else "早晚骑行，湿冷要注意保暖；雨后路面湿滑"
        )
    if region.startswith("中南"):
        return (
            "6:00 出发、11–15 点避暑；6 月正值梅雨，随身雨衣并每天看暴雨预警；"
            "湿度大，注意电解质与刹车防滑"
            if summer
            else "早晚骑行，雨后路面湿滑注意刹车距离"
        )
    if region.startswith("中段"):
        return (
            "6:30 出发，尽量上午完成 2/3 里程；午后雷阵雨多，避免在空旷路段遇雷"
            if summer
            else "山区雾多，注意能见度与保暖"
        )
    return (
        "7:00 出发；日晒强、昼夜温差大，带防晒与薄风衣；黄土沟壑区补给点稀疏，出门带足水"
        if summer
        else "气温低，注意保暖与路面结冰"
    )


def _build_climate_advice(days: List[DayPlan], month: int) -> List[str]:
    """按连续路段归并成 2–4 条区域建议，而不是逐日重复。"""
    groups: List[dict] = []
    for d in days:
        if d.kind != "ride":
            continue
        region = _region(d.end_lat, d.max_elev_m)
        if groups and groups[-1]["region"] == region:
            groups[-1]["end"] = d
        else:
            groups.append({"region": region, "start": d, "end": d})
    return [
        f"D{g['start'].day_index}–D{g['end'].day_index}"
        f"（{g['start'].date} ~ {g['end'].date}）{g['region']}：{_region_advice(g['region'], month)}"
        for g in groups
    ]


def _build_prep_tips(brief: TripBrief, total_km: float) -> List[str]:
    return [
        f"行前 4–6 周：连续两天 100 km+ 的背靠背骑行至少 2 次，检验恢复能力（本次总里程 {total_km:.0f} km）",
        "车况：链条、刹车皮、外胎重点检查；带 2 条备胎、补胎片、便携气筒、截链器",
        "电子：码表 + 20000mAh 充电宝；离线地图提前下载（山区信号差）",
        "证件与保险：身份证、骑行意外险；长途建议随身带应急联系卡",
        "住宿：县城以上提前 1 天预订；节假日避开景区周边",
        "返程：延安有动车与航班，自行车可随车托运或当地车店打包，出发前确认政策",
        "补给策略：单日超过 100 km 的路段，出发时带足 2L 水与一顿备用粮",
    ]


def plan_itinerary(
    brief: TripBrief,
    waypoints: Sequence[Waypoint],
    legs: Optional[Sequence[Leg]] = None,
    lodging_finder: Optional[object] = None,
    split_legs: bool = True,
    max_walk_m: float = 6000.0,
) -> Itinerary:
    """legs 为空时用走廊直线；传入真实路由结果时按真实里程与爬升排程。

    lodging_finder 用于在无城镇落脚点附近检索酒店（OSM），找不到才提示露营。
    """
    legs = list(legs) if legs is not None else build_legs(waypoints)
    target = daily_target(brief)
    # 单腿普遍超过日预算时自动切段（切点标 camp，需自备露营装备）
    legs, _split_count = split_long_legs(legs, target) if split_legs else (legs, 0)
    warnings: List[str] = []

    days: List[DayPlan] = []
    try:
        start_day = date.fromisoformat(str(brief.start_date))
    except (ValueError, TypeError):
        # agent 交接过来的行程可能没有日期，退回今天并记进假设
        start_day = date.today()
        brief.start_date = start_day.isoformat()
        brief.assumptions.append(f"未给出出发日期，按今天 {brief.start_date} 计算")
    cal = start_day
    riding_count = 0
    acclimatized = False
    i = 0

    while i < len(legs):
        km = 0.0
        climb = 0
        day_max_elev = 0
        labels: List[str] = []
        j = i
        day_warn: List[str] = []

        best_j, best_km, best_climb, best_elev = i, 0.0, 0, 0
        while j < len(legs):
            leg = legs[j]
            nxt_km = km + leg.km
            nxt_climb = climb + leg.climb_m
            nxt_elev = max(day_max_elev, leg.start.elev_m, leg.end.elev_m)
            allowed = target * _altitude_factor(nxt_elev)
            cost = nxt_km + nxt_climb / 1000.0 * CLIMB_PENALTY_KM_PER_1000M
            if cost > allowed:
                break
            km, climb, day_max_elev = nxt_km, nxt_climb, nxt_elev
            labels.append(leg.label)
            j += 1
            if leg.end.has_hotel or leg.end.service == "camp":
                best_j, best_km, best_climb, best_elev = j, km, climb, day_max_elev

        if best_j > i:
            # 回退到最后一个「有住宿」的落脚点
            while j > best_j:
                j -= 1
                labels.pop()
            km, climb, day_max_elev = best_km, best_climb, best_elev
        else:
            # 沿途没有可停点：至少允许停在下一个"途中点"，避免连锁拉长成 200+ km
            hotel_idx = None
            k = i
            while k < len(legs):
                k += 1
                if legs[k - 1].end.has_hotel or legs[k - 1].end.service == "camp":
                    hotel_idx = k
                    break
            j = hotel_idx if hotel_idx is not None else len(legs)
            km = sum(legs[t].km for t in range(i, j))
            climb = sum(legs[t].climb_m for t in range(i, j))
            day_max_elev = max(legs[t].end.elev_m for t in range(i, j))
            labels = [legs[t].label for t in range(i, j)]
            passed = [legs[t].end.name for t in range(i, j - 1) if not legs[t].end.has_hotel]
            if passed:
                day_warn.append(
                    f"途经 {'、'.join(passed)} 均无住宿：本日被迫一次骑到 {legs[j - 1].end.name}"
                    f"（{km:.0f} km）；想分两天只能自备露营装备"
                )

        end_wp = legs[j - 1].end
        if end_wp.service == "camp":
            lodging, note = nearest_lodging(lodging_finder, end_wp.lat, end_wp.lon, max_walk_m)
            if lodging is not None:
                end_wp = Waypoint(
                    name=f"{end_wp.name}（{lodging.name}）",
                    lat=lodging.lat,
                    lon=lodging.lon,
                    service="lodge",
                    climb_from_prev_m=0,
                    note=f"OSM 住宿：{lodging.kind}",
                    elev_m=end_wp.elev_m,
                    terrain=end_wp.terrain,
                )
                day_warn.append(f"{note}，已把落脚点移到该住宿处")
            else:
                day_warn.append(f"{end_wp.name} 无固定住宿：{note}，需自备露营装备")

        high_points = [legs[k].end for k in range(i, j) if legs[k].end.elev_m >= 3500]
        if high_points:
            day_warn.append(
                "翻越 " + "、".join(f"{p.name}({p.elev_m}m)" for p in high_points)
                + "：早出发、备保暖层，山口段可能需要推车"
            )

        if day_max_elev >= 3500:
            day_warn.append(
                f"当日最高海拔 {day_max_elev}m：高原反应风险高，控制强度、缓慢爬升，出现头痛/呕吐立即下撤"
            )
        elif day_max_elev >= 3000:
            day_warn.append(f"当日最高海拔 {day_max_elev}m：注意保暖与补水，避免剧烈冲刺")

        # 单段明显超出"该海拔下的能力"时才提示，避免噪音
        long_legs = []
        for k in range(i, j):
            allowed_k = target * _altitude_factor(max(legs[k].start.elev_m, legs[k].end.elev_m))
            if legs[k].km > allowed_k * 1.15:
                long_legs.append((legs[k], allowed_k))
        if long_legs:
            leg, allowed_k = max(long_legs, key=lambda x: x[0].km)
            day_warn.append(
                f"{leg.label} 单段 {leg.km:.0f} km，该海拔下日行能力约 {allowed_k:.0f} km，"
                f"超出 {leg.km - allowed_k:.0f} km；中途无落脚点，建议早出发或搭车通过"
            )
        elif km > target * _altitude_factor(day_max_elev) * 1.15:
            allowed_day = target * _altitude_factor(day_max_elev)
            day_warn.append(
                f"当日 {km:.0f} km 高于该海拔下的日行能力 {allowed_day:.0f} km，注意体力分配"
            )

        riding_count += 1
        days.append(
            DayPlan(
                day_index=len(days) + 1,
                date=cal.isoformat(),
                kind="ride",
                start=legs[i].start.name,
                end=end_wp.name,
                km=round(km, 1),
                climb_m=climb,
                overnight=end_wp.name,
                service=end_wp.service,
                end_lat=end_wp.lat,
                end_lon=end_wp.lon,
                max_elev_m=day_max_elev,
                towns=j - i + 1,
                leg_from=i,
                leg_to=j,
                legs=labels,
                warnings=day_warn,
            )
        )
        cal += timedelta(days=1)
        warnings.extend(f"D{days[-1].day_index}: {w}" for w in day_warn)

        # 首次上到 3000m 以上：强制插一天适应日
        if day_max_elev >= 3000 and not acclimatized and end_wp.has_hotel:
            days.append(
                DayPlan(
                    day_index=len(days) + 1,
                    date=cal.isoformat(),
                    kind="rest",
                    start=end_wp.name,
                    end=end_wp.name,
                    km=0.0,
                    climb_m=0,
                    overnight=end_wp.name,
                    service=end_wp.service,
                    end_lat=end_wp.lat,
                    end_lon=end_wp.lon,
                    max_elev_m=day_max_elev,
                )
            )
            warnings.append(
                f"D{days[-1].day_index}: 首次抵达 3000m 以上（{end_wp.name}），安排一天适应海拔，不要赶路"
            )
            cal += timedelta(days=1)
            acclimatized = True

        i = j

        # 休整：每骑满 rest_every 天，且当前落脚点有住宿（县城及以上）
        if brief.rest_every and riding_count % brief.rest_every == 0 and i < len(legs):
            if end_wp.has_hotel and days and days[-1].kind != "rest":
                days.append(
                    DayPlan(
                        day_index=len(days) + 1,
                        date=cal.isoformat(),
                        kind="rest",
                        start=end_wp.name,
                        end=end_wp.name,
                        km=0.0,
                        climb_m=0,
                        overnight=end_wp.name,
                        service=end_wp.service,
                        warnings=[],
                    )
                )
                cal += timedelta(days=1)

    month = _safe_month(getattr(brief, 'start_date', ''))
    mid_lat = waypoints[len(waypoints) // 2].lat

    total_km = round(sum(d.km for d in days), 1)
    total_climb = sum(d.climb_m for d in days)
    riding_days = sum(1 for d in days if d.kind == "ride")
    rest_days = sum(1 for d in days if d.kind == "rest")

    longest = max(legs, key=lambda x: x.km)
    if longest.km >= 100:
        warnings.append(
            f"最长无城镇间隔 {longest.km:.0f} km（{longest.label}），建议当天带足 2L 水与一顿备用粮"
        )

    max_elev = max((d.max_elev_m for d in days), default=0)
    camp_nights = [d for d in days if d.service == "camp"]
    camp_points = [
        {
            "day": d.day_index,
            "name": d.overnight,
            "lat": round(d.end_lat, 6),
            "lon": round(d.end_lon, 6),
        }
        for d in camp_nights
    ]
    if camp_nights:
        names = "、".join(sorted({d.overnight for d in camp_nights}))[:120]
        warnings.append(
            f"有 {len(camp_nights)} 晚落在「无固定住宿」的途中点（{names}）："
            "OSM 未检索到酒店或检索失败；出发前务必电话确认当地农家/道班能否借宿，否则需带露营装备"
        )
    passes = [w for w in waypoints if w.elev_m >= 3500]
    if max_elev >= 3500:
        warnings.append(
            f"全线最高海拔约 {max_elev} m"
            + (f"（{'、'.join(p.name for p in passes)}）" if passes else "")
            + "：需预留适应时间，备保暖层，出现高反立即下撤；山口段可能需要推车"
        )
    sparse = [leg for leg in legs if not leg.end.has_hotel]
    if sparse:
        warnings.append(
            "住宿薄弱点：" + "、".join(f"{l.end.name}({l.km:.0f}km)" for l in sparse)
            + "，这些点没有县城级住宿，需提前确认或调整行程"
        )

    # ---- 分段气候与作息建议 ----
    climate_advice = _build_climate_advice(days, month)
    prep_tips = _build_prep_tips(brief, total_km)

    if brief.rider.wkg and brief.rider.wkg < 2.5 and total_km > 1500:
        warnings.append(
            f"当前体能 {brief.rider.level}（{brief.rider.wkg} W/kg）骑 {total_km:.0f} km 长线偏吃力，"
            "建议提前 8–12 周做长距离适应性训练"
        )

    return Itinerary(
        brief=brief,
        corridor=brief.corridor,
        corridor_label=CORRIDOR_LABELS.get(brief.corridor, brief.corridor),
        total_km=total_km,
        total_climb=total_climb,
        riding_days=riding_days,
        rest_days=rest_days,
        calendar_days=len(days),
        daily_target_km=target,
        max_gap_km=round(longest.km, 1),
        max_gap_label=longest.label,
        max_elev_m=max_elev,
        days=days,
        legs=legs,
        camp_points=camp_points,
        climate_advice=climate_advice,
        prep_tips=prep_tips,
        warnings=warnings,
        assumptions=brief.assumptions,
        profile_summary={
            "FTP": f"{brief.rider.ftp_w:.0f} W" if brief.rider.ftp_w else "未提供",
            "体重": f"{brief.rider.weight_kg:.0f} kg" if brief.rider.weight_kg else "未提供",
            "功重比": f"{brief.rider.wkg} W/kg" if brief.rider.wkg else "未提供",
            "水平": brief.rider.level,
            "日行基准": f"{brief.rider.base_daily_km:.0f} km（轻装平路）",
            "规划目标": f"{target:.0f} km/天（含行李与季节修正）",
        },
    )
