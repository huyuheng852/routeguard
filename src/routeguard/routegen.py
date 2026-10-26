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


def _interpolate(a, b, spacing_km: float, start_index: int = 1) -> List[dict]:
    """在 a、b 之间等距取中间点（不含两端），用全局编号命名。"""
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
                "name": f"途经{start_index + i - 1}",
                "auto": True,
                "hint": f"{a.name}–{b.name}",
            }
        )
    return out


MAX_REVERSE_CALLS = 40
RANK = {"city": 4, "municipality": 4, "town": 3, "village": 2, "hamlet": 2, "suburb": 1}
RANK_LABEL = {4: "city", 3: "county", 2: "town", 1: "town", 0: "camp"}


def _rank(addresstype: str) -> int:
    return RANK.get((addresstype or "").lower(), 0)


def _pick_overnight(points, idx, geocoder, window_km=(8.0, 20.0), budget=None):
    """在切点附近找落脚点，优先级：地级市 > 县级市 > 乡镇。

    允许比预算多骑 20 km、少骑 8 km，在这段里挑行政等级最高的城镇；
    等级相同则挑最接近预算的。
    """
    if geocoder is None or not hasattr(geocoder, "reverse"):
        return None

    # 候选：切点前 8 km 到后 20 km，每 4 km 取一个
    cands, acc, behind = [], 0.0, []
    for i in range(idx, 0, -1):
        step = haversine_km(points[i - 1][0], points[i - 1][1], points[i][0], points[i][1])
        acc += step
        if acc > window_km[0]:
            break
        behind.append(i - 1)
    ahead, acc = [], 0.0
    for i in range(idx, len(points) - 1):
        step = haversine_km(points[i][0], points[i][1], points[i + 1][0], points[i + 1][1])
        acc += step
        if acc > window_km[1]:
            break
        ahead.append(i + 1)

    best = None
    cand_idx = [idx]
    if behind:
        cand_idx.append(behind[-1])
    if ahead:
        cand_idx.extend(ahead[::10][:2])
    for i in cand_idx:
        if budget is not None and budget[0] <= 0:
            break
        if budget is not None:
            budget[0] -= 1
        pt = points[i]
        place = geocoder.reverse(pt[0], pt[1])
        if place is None:
            continue
        rank = _rank(getattr(place, "addresstype", ""))
        if rank == 0:
            continue
        key = (rank, -abs(i - idx))
        if best is None or key > best[0]:
            best = (key, i, place)
    return None if best is None else (best[1], best[2], _rank(best[2].addresstype))


def _nearest_idx(cum, dist):
    """累计距离数组里最接近 dist 的下标。"""
    lo, hi = 0, len(cum) - 1
    while lo < hi:
        mid = (lo + hi) // 2
        if cum[mid] < dist:
            lo = mid + 1
        else:
            hi = mid
    if lo > 0 and abs(cum[lo - 1] - dist) < abs(cum[lo] - dist):
        return lo - 1
    return lo


def direct_legs(router, start, end, daily_km: float, geocoder=None):
    """一次请求整条 A→B，再按日预算切分。

    切点选择：在「预算点 -8 km ~ +20 km」这段里找居民点，
    优先级 地级市 > 县级市 > 乡镇；找不到就退回预算点本身。
    """
    from .corridor import Leg

    route = router.route(start, end)
    pts = list(route.points)
    if len(pts) < 2:
        return [], []

    budget = max(40.0, daily_km)
    cum = [0.0]
    for a, b in zip(pts, pts[1:]):
        cum.append(cum[-1] + haversine_km(a[0], a[1], b[0], b[1]))
    total = cum[-1]

    targets, d = [], budget
    while d < total - budget * 0.9:
        targets.append(d)
        d += budget
    targets.append(total)

    calls = [MAX_REVERSE_CALLS]
    picks = []
    prev_idx = 0
    for tdist in targets:
        if tdist >= total:
            picks.append((len(pts) - 1, getattr(end, "name", "终点"), "city"))
            break

        base = _nearest_idx(cum, tdist)
        lo = _nearest_idx(cum, tdist - 8.0)
        hi = _nearest_idx(cum, tdist + min(25.0, budget * 0.12))
        lo = max(lo, prev_idx + 1)
        hi = max(hi, lo)

        best = None
        step = max(1, (hi - lo) // 3)
        for i in range(lo, hi + 1, step):
            if calls[0] <= 0 or geocoder is None:
                break
            calls[0] -= 1
            place = geocoder.reverse(pts[i][0], pts[i][1])
            if place is None:
                continue
            rank = _rank(getattr(place, "addresstype", ""))
            if rank == 0:
                continue
            score = (rank, -abs(i - base))
            if best is None or score > best[0]:
                best = (score, i, place, rank)

        if best is not None:
            idx, place, rank = best[1], best[2], best[3]
            service = "camp"
            name = place.name
        else:
            idx, service, name = max(base, prev_idx + 1), "camp", f"第{len(picks) + 1}天终点"
        idx = min(idx, len(pts) - 2) if len(pts) > 2 else idx
        picks.append((idx, name, service))
        prev_idx = idx

    waypoints, legs = [], []
    start_wp = Waypoint(
        name=getattr(start, "name", "起点"),
        lat=pts[0][0],
        lon=pts[0][1],
        service="city",
        climb_from_prev_m=0,
        note="起点",
        elev_m=int(pts[0][2]) if pts[0][2] is not None else 0,
        terrain="rolling",
    )
    prev_wp, prev_idx = start_wp, 0
    for n, (idx, name, service) in enumerate(picks, start=1):
        chunk = pts[prev_idx : idx + 1]
        if len(chunk) < 2:
            prev_idx = idx
            continue
        wp = Waypoint(
            name=name,
            lat=chunk[-1][0],
            lon=chunk[-1][1],
            service=service,
            climb_from_prev_m=0,
            note="终点" if idx >= len(pts) - 1 else f"按行政等级选取（{service}）",
            elev_m=int(chunk[-1][2]) if chunk[-1][2] is not None else 0,
            terrain="rolling",
        )
        km = sum(haversine_km(a[0], a[1], b[0], b[1]) for a, b in zip(chunk, chunk[1:]))
        eles = [q[2] for q in chunk if q[2] is not None]
        climb = (
            int(sum(max(0.0, b - a) for a, b in zip(eles, eles[1:])))
            if len(eles) == len(chunk)
            else 0
        )
        legs.append(
            Leg(start=prev_wp, end=wp, km=round(km, 1), climb_m=climb, points=chunk,
                provider=route.provider)
        )
        waypoints.append(wp)
        prev_wp, prev_idx = wp, idx
    return waypoints, legs


def generate_waypoints(
    anchors: Sequence[object],
    spacing_km: float = 120.0,
    close_loop: bool = False,
) -> List[Waypoint]:
    """把锚点序列扩展成航点序列。

    anchors 需带 name/lat/lon（`geocode.Place`）。close_loop=True 时
    最后一个锚点会回到第一个，形成环线。

    自动采样点的住宿状态是**未知**，因此标成 camp（可停、待核实），
    由 lodging 环节去查；查不到就明确写"无固定住宿"，不能默认成县城。
    """
    if not anchors:
        return []

    pts: List[dict] = []
    counter = 1
    for idx, a in enumerate(anchors):
        pts.append({"lat": a.lat, "lon": a.lon, "name": a.name, "auto": False})
        nxt = anchors[idx + 1] if idx + 1 < len(anchors) else None
        if nxt is not None:
            seg = _interpolate(a, nxt, spacing_km, counter)
            counter += len(seg)
            pts.extend(seg)
        elif close_loop and len(anchors) > 1:
            seg = _interpolate(anchors[-1], anchors[0], spacing_km, counter)
            counter += len(seg)
            pts.extend(seg)

    if close_loop and len(anchors) > 1:
        first = anchors[0]
        pts.append({"lat": first.lat, "lon": first.lon, "name": first.name, "auto": False})

    # 去掉相邻重复（同一个点连续出现）
    cleaned: List[dict] = []
    for p in pts:
        if not cleaned or cleaned[-1]["name"] != p["name"]:
            cleaned.append(p)

    out: List[Waypoint] = []
    for p in cleaned:
        out.append(
            Waypoint(
                name=p["name"],
                lat=p["lat"],
                lon=p["lon"],
                service="camp" if p["auto"] else "county",
                climb_from_prev_m=0,
                note="自动采样点，住宿未核实" if p["auto"] else "用户指定锚点",
                elev_m=0,
                terrain="rolling",
            )
        )
    return out


def snap_waypoints(waypoints, geocoder, max_km: float = 30.0):
    """把自动采样点吸附到最近的真实城镇。

    为什么必须这么做：采样点在大圆直线上，往往落在荒野中间；直接拿它去路由，
    整体形状就是一条贯穿荒野的直线。吸附到村镇后，路线才会沿着人走的路走。
    """
    if geocoder is None or not hasattr(geocoder, "reverse"):
        return waypoints, 0

    out = []
    snapped = 0
    for w in waypoints:
        if "自动采样点" in (w.note or ""):
            place = geocoder.reverse(w.lat, w.lon)
            if place is not None and haversine_km(w.lat, w.lon, place.lat, place.lon) <= max_km:
                out.append(
                    Waypoint(
                        name=place.name,
                        lat=place.lat,
                        lon=place.lon,
                        service="county",
                        climb_from_prev_m=0,
                        note="已吸附到最近城镇，住宿需核实",
                        elev_m=0,
                        terrain=w.terrain,
                    )
                )
                snapped += 1
                continue
        out.append(w)

    dedup = []
    for w in out:
        if not dedup or dedup[-1].name != w.name:
            dedup.append(w)
    return dedup, snapped


def generated_summary(waypoints: Sequence[Waypoint]) -> str:
    """给报告用的一句话说明。"""
    auto = sum(1 for w in waypoints if "自动采样点" in (w.note or ""))
    if not auto:
        return ""
    return (
        f"路线由 {len(waypoints)} 个航点组成，其中 {auto} 个是自动采样点（按约 120 km 生成），"
        "住宿与路况未逐个核实；出发前请用地图复核"
    )
