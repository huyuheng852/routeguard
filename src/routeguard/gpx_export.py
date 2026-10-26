"""GPX 导出：把规划结果写成 GPX 1.1。

导出的是什么
------------
**走廊折线**，不是可导航的逐米路由：航点之间按直线插值（每 ~2 km 一个点）。
它的用途是给码表/Strava/行者一个"骨架"，让你看清楚每天从哪到哪、大概什么形状，
真正的道路上骑行前还需要用 BRouter/OSRM 把这条折线 snap 到路网上。

结构约定
--------
* 每个骑行日一条 `<trk>`，名字形如 `D3 赣州 → 遂川`，导入后按天分开；
* 每晚落脚点写成一个 `<wpt>`，带城镇名与住宿等级；
* 有海拔数据时写 `<ele>`，没有就省略（不写 0，避免海拔曲线贴地）。
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from .corridor import Waypoint, build_legs, haversine_km
from .planner import Itinerary

SERVICE_LABEL = {"city": "地级市", "county": "县城", "town": "乡镇", "lodge": "酒店/客栈", "camp": "露营"}


def _escape(text: str) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _interpolate(
    a: Waypoint, b: Waypoint, step_km: float
) -> List[Tuple[float, float, float]]:
    """两点之间按步长插值（含起点，不含终点，终点由下一段补上）。"""
    dist = haversine_km(a.lat, a.lon, b.lat, b.lon)
    n = max(1, int(dist / max(step_km, 0.5)))
    pts: List[Tuple[float, float, float]] = []
    for i in range(n):
        f = i / n
        pts.append(
            (
                a.lat + (b.lat - a.lat) * f,
                a.lon + (b.lon - a.lon) * f,
                a.elev_m + (b.elev_m - a.elev_m) * f,
            )
        )
    return pts


def _decimate(points: Sequence[Tuple], min_m: float) -> List[Tuple]:
    """按最小间距抽稀，避免 BRouter 的 2–5 m 点距把文件撑到十几 MB。"""
    if not points or min_m <= 0:
        return list(points)
    kept: List[Tuple] = [points[0]]
    for p in points[1:]:
        last = kept[-1]
        d = haversine_km(last[0], last[1], p[0], p[1]) * 1000
        if d >= min_m:
            kept.append(p)
    if kept[-1] is not points[-1]:
        kept.append(points[-1])
    return kept


def _track_lines(day, legs: Sequence, step_km: float, min_point_m: float) -> List[str]:
    """生成一天的 <trk> 片段。"""
    out: List[str] = []
    out.append("  <trk>")
    out.append(f"    <name>D{day.day_index} {_escape(day.start)} → {_escape(day.end)}</name>")
    out.append(f"    <desc>{day.km:.0f} km，爬升约 {day.climb_m} m</desc>")
    out.append("    <trkseg>")
    for leg_idx in range(day.leg_from, day.leg_to):
        if leg_idx >= len(legs):
            break
        leg = legs[leg_idx]
        if leg.points:
            pts = _decimate([(p[0], p[1], p[2]) for p in leg.points[:-1]], min_point_m)
        else:
            pts = _interpolate(leg.start, leg.end, step_km)
        for lat, lon, ele in pts:
            ele_tag = f"<ele>{ele:.0f}</ele>" if ele else ""
            out.append(f'      <trkpt lat="{lat:.6f}" lon="{lon:.6f}">{ele_tag}</trkpt>')
    last_leg = legs[min(day.leg_to, len(legs)) - 1]
    if last_leg.points:
        lat, lon, ele = last_leg.points[-1]
    else:
        lat, lon, ele = last_leg.end.lat, last_leg.end.lon, last_leg.end.elev_m or None
    ele_tag = f"<ele>{ele:.0f}</ele>" if ele else ""
    out.append(f'      <trkpt lat="{lat:.6f}" lon="{lon:.6f}">{ele_tag}</trkpt>')
    out.append("    </trkseg>")
    out.append("  </trk>")
    return out


def _safe_name(text: str) -> str:
    for ch in ('→', ' ', '/', '\\', ':', '*', '?', '"', '<', '>', '|'):
        text = text.replace(ch, "-")
    return text.replace("---", "-").replace("--", "-")


def _start_label(name: str) -> str:
    """取可读的起点名：'吴起→延安 途中' → '吴起'。"""
    return name.split("→")[0].strip() or name


def _end_label(name: str) -> str:
    """取可读的终点名：'吴起→延安 途中2' → '延安'。"""
    part = name.split("→")[-1].replace("途中", "")
    part = re.sub(r"[\d\s]+$", "", part).strip()
    return part or name


def build_day_gpx(
    it: Itinerary,
    day,
    legs: Sequence,
    step_km: float = 2.0,
    min_point_m: float = 40.0,
) -> str:
    """单日 GPX：一个文件一天，方便逐日导入码表/分享。"""
    lines: List[str] = ['<?xml version="1.0" encoding="UTF-8"?>']
    lines.append(
        '<gpx version="1.1" creator="RouteGuard" '
        'xmlns="http://www.topografix.com/GPX/1/1">'
    )
    lines.append("  <metadata>")
    lines.append(f"    <name>D{day.day_index} {_escape(day.start)} → {_escape(day.end)}</name>")
    lines.append(
        f"    <desc>{_escape(it.brief.origin)} → {_escape(it.brief.destination)} 第 {day.day_index} 天；"
        f"{day.km:.0f} km / 爬升 {day.climb_m} m；{day.date}</desc>"
    )
    lines.append(f"    <time>{datetime.now().isoformat(timespec='seconds')}</time>")
    lines.append("  </metadata>")

    last_leg = legs[min(day.leg_to, len(legs)) - 1]
    for label, pt in (("起点", (last_leg.start.lat, last_leg.start.lon, last_leg.start.elev_m)),
                      ("终点", (last_leg.end.lat, last_leg.end.lon, last_leg.end.elev_m))):
        ele = f"<ele>{pt[2]}</ele>" if pt[2] else ""
        lines.append(f'  <wpt lat="{pt[0]:.6f}" lon="{pt[1]:.6f}">{ele}')
        lines.append(f"    <name>{label} {_escape(day.start if label == '起点' else day.end)}</name>")
        lines.append("  </wpt>")

    lines.extend(_track_lines(day, legs, step_km, min_point_m))
    lines.append("</gpx>")
    return "\n".join(lines)


def write_daily_gpx(
    out_dir: str | Path,
    it: Itinerary,
    legs: Sequence,
    step_km: float = 2.0,
    min_point_m: float = 40.0,
) -> List[Path]:
    """把每个骑行日导出成独立 GPX：D01_瑞金-于都.gpx。"""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    # 清掉上一次留下的逐日文件，避免改了行程后新旧混在一起
    for stale in out_dir.glob("*.gpx"):
        stale.unlink()
    written: List[Path] = []
    for day in it.days:
        if day.kind != "ride":
            continue
        name = (
            f"D{day.day_index:02d}_{_safe_name(_start_label(day.start))}"
            f"-{_safe_name(_end_label(day.end))}_{day.km:.0f}km.gpx"
        )
        path = out_dir / name
        path.write_text(
            build_day_gpx(it, day, legs, step_km, min_point_m), encoding="utf-8"
        )
        written.append(path)
    return written


def build_gpx(
    it: Itinerary,
    waypoints: Sequence[Waypoint],
    legs: Optional[Sequence] = None,
    step_km: float = 2.0,
    min_point_m: float = 40.0,
) -> str:
    # 必须用规划时实际使用的 legs（可能是真实路由 + 自动切段后的结果），
    # 不能在这里重新 build_legs，否则会退化成走廊直线。
    legs = list(legs) if legs is not None else build_legs(waypoints)
    lines: List[str] = []
    lines.append('<?xml version="1.0" encoding="UTF-8"?>')
    lines.append(
        '<gpx version="1.1" creator="RouteGuard" '
        'xmlns="http://www.topografix.com/GPX/1/1">'
    )
    lines.append("  <metadata>")
    lines.append(f"    <name>{_escape(it.brief.origin)} → {_escape(it.brief.destination)}</name>")
    routed = any(l.provider not in ("corridor", "straight-fallback") for l in legs)
    if routed:
        providers = sorted({l.provider for l in legs if l.provider not in ("corridor", "straight-fallback")})
        desc_tail = f"几何来自真实路网（{', '.join(providers)}）。"
    else:
        desc_tail = "本文件是走廊折线（航点间直线插值），折线长度约为公路里程的 78%，仅作路线骨架。"
    lines.append(
        f"    <desc>{_escape(it.corridor_label)}；{it.total_km:.0f} km / "
        f"{it.riding_days} 骑行日。{desc_tail}</desc>"
    )
    lines.append(f"    <time>{datetime.now().isoformat(timespec='seconds')}</time>")
    lines.append("  </metadata>")

    # 每晚落脚点
    for day in it.days:
        if day.kind != "ride":
            continue
        wp = next((w for w in waypoints if w.name == day.overnight), None)
        if wp is None:
            continue
        ele = f"<ele>{wp.elev_m}</ele>" if wp.elev_m else ""
        lines.append(f'  <wpt lat="{wp.lat:.6f}" lon="{wp.lon:.6f}">{ele}')
        lines.append(f"    <name>D{day.day_index} {_escape(day.overnight)}</name>")
        lines.append(
            f"    <desc>{_escape(day.start)} → {_escape(day.end)}，{day.km:.0f} km，"
            f"住宿等级：{SERVICE_LABEL.get(day.service, day.service)}</desc>"
        )
        lines.append("  </wpt>")

    # 每天一条轨迹
    for day in it.days:
        if day.kind != "ride":
            continue
        lines.extend(_track_lines(day, legs, step_km, min_point_m))

    lines.append("</gpx>")
    return "\n".join(lines)


def write_gpx(
    path: str | Path,
    it: Itinerary,
    waypoints: Sequence[Waypoint],
    legs: Optional[Sequence] = None,
    step_km: float = 2.0,
    min_point_m: float = 40.0,
) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        build_gpx(it, waypoints, legs, step_km, min_point_m), encoding="utf-8"
    )
    return path
