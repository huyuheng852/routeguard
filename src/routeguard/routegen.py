"""通用路线生成：把任意 A→B（或 A→…→A 环线）变成可规划的航点序列。

为什么需要
----------
内置走廊只是**两个特例**（瑞金—延安的历史线与纵贯线），带人工核对过的海拔和
住宿等级。用户说"从海口到三亚"时，程序不该回一句"不支持"，而应该自己把路线
造出来——哪怕精度不如人工走廊，也必须能跑、并且说清哪里是估的。

做法
----
1. 起终点（及用户给的途经点）先解析成坐标；
2. 相邻锚点之间沿大圆等距插值，每 ~120 km 生成一个采样点，避免单段几千公里
   一次性丢给路由服务；
3. 每个采样点交给真实路由逐段求解，海拔从几何回填；
4. 采样点是**自动生成**的，不一定有城镇，报告里会标注"住宿未核实"。
"""

from __future__ import annotations

import math
from typing import List, Optional, Sequence

from .corridor import Waypoint, haversine_km


def _interpolate(a, b, spacing_km: float) -> List[dict]:
    """在 a、b 之间等距取中间点（不含两端）。"""
    dist = haversine_km(a.lat, a.lon, b.lat, b.lon)
    n = max(1, int(math.ceil(dist / spacing_km)))
    if n <= 1:
        return []
    out = []
    for i in range(1, n):
        f = i / n
        out.append(
            {
                "lat": a.lat + (b.lat - a.lat) * f,
                "lon": a.lon + (b.lon - a.lon) * f,
                "name": f"{a.name}–{b.name} 途中{i}",
            }
        )
    return out


def generate_waypoints(
    anchors: Sequence[object],
    spacing_km: float = 120.0,
    close_loop: bool = False,
) -> List[Waypoint]:
    """把锚点序列扩展成航点序列。

    anchors 需带 name/lat/lon（`geocode.Place`）。close_loop=True 时
    最后一个锚点会回到第一个，形成环线。
    """
    if not anchors:
        return []

    pts: List[dict] = []
    for idx, a in enumerate(anchors):
        pts.append({"lat": a.lat, "lon": a.lon, "name": a.name})
        nxt = anchors[idx + 1] if idx + 1 < len(anchors) else None
        if nxt is not None:
            pts.extend(_interpolate(a, nxt, spacing_km))
        elif close_loop and len(anchors) > 1:
            last = anchors[-1]
            first = anchors[0]
            pts.extend(_interpolate(last, first, spacing_km))

    if close_loop and len(anchors) > 1:
        first = anchors[0]
        pts.append({"lat": first.lat, "lon": first.lon, "name": first.name})

    # 去掉相邻重复（同一个点连续出现）
    cleaned: List[dict] = []
    for p in pts:
        if not cleaned or cleaned[-1]["name"] != p["name"]:
            cleaned.append(p)

    auto_names = {p["name"] for p in pts if "途中" in p["name"]}
    out: List[Waypoint] = []
    for p in cleaned:
        is_auto = p["name"] in auto_names
        out.append(
            Waypoint(
                name=p["name"],
                lat=p["lat"],
                lon=p["lon"],
                service="county",
                climb_from_prev_m=0,
                note="自动采样点，住宿未核实" if is_auto else "用户指定锚点",
                elev_m=0,
                terrain="rolling",
            )
        )
    return out


def generated_summary(waypoints: Sequence[Waypoint]) -> str:
    """给报告用的一句话说明。"""
    auto = sum(1 for w in waypoints if "自动采样点" in (w.note or ""))
    if not auto:
        return ""
    return (
        f"路线由 {len(waypoints)} 个航点组成，其中 {auto} 个是自动采样点（按约 120 km 生成），"
        "住宿与路况未逐个核实；出发前请用地图复核"
    )
