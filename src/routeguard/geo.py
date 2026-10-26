"""轨迹处理：GPX 解析、距离计算、分段与坡度统计。"""

from __future__ import annotations

import math
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from .schema import Point, Segment

EARTH_RADIUS_M = 6371008.8


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = p2 - p1
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


def _local_tag(element: ET.Element) -> str:
    return element.tag.split("}")[-1]


def parse_gpx(path: str | Path) -> List[Point]:
    """解析 GPX 轨迹点。兼容带/不带命名空间的 gpx 文件。"""
    root = ET.parse(str(path)).getroot()
    points: List[Point] = []
    for node in root.iter():
        if _local_tag(node) != "trkpt":
            continue
        lat = float(node.attrib["lat"])
        lon = float(node.attrib["lon"])
        ele = 0.0
        for child in node:
            if _local_tag(child) == "ele" and child.text:
                ele = float(child.text)
                break
        points.append(Point(lat=lat, lon=lon, ele=ele))
    if len(points) < 2:
        raise ValueError(f"GPX 轨迹点不足：{path}")
    return points


def with_distance(points: Sequence[Point]) -> List[Point]:
    """补上累计距离。"""
    out: List[Point] = []
    acc = 0.0
    prev: Optional[Point] = None
    for p in points:
        if prev is not None:
            acc += haversine_m(prev.lat, prev.lon, p.lat, p.lon)
        out.append(Point(lat=p.lat, lon=p.lon, ele=p.ele, dist_m=acc))
        prev = p
    return out


def smooth_elevation(points: Sequence[Point], window: int = 3) -> List[Point]:
    """三点滑动平均，抑制 GPS 高程噪声；坡度对噪声极其敏感。"""
    if window < 3:
        return list(points)
    half = window // 2
    out: List[Point] = []
    for i, p in enumerate(points):
        lo = max(0, i - half)
        hi = min(len(points), i + half + 1)
        ele = sum(q.ele for q in points[lo:hi]) / (hi - lo)
        out.append(Point(lat=p.lat, lon=p.lon, ele=ele, dist_m=p.dist_m))
    return out


def _slope_pct(ele_a: float, ele_b: float, dist_m: float) -> float:
    if dist_m <= 1e-6:
        return 0.0
    return (ele_b - ele_a) / dist_m * 100.0


def segment_route(
    points: Sequence[Point],
    segment_len_m: float = 500.0,
    prefix: str = "S",
) -> List[Segment]:
    """按距离切段。

    相邻段**共享边界点**，保证各段长度之和等于总里程（否则会丢掉段间空隙）。
    最后一段不足半段时并入前一段，避免碎片。
    """
    pts = smooth_elevation(with_distance(points))

    total = pts[-1].dist_m
    if total <= 0:
        raise ValueError("轨迹总长度为 0，检查 GPX 是否重复点。")

    split_idx: List[int] = [0]
    target = segment_len_m
    for i, p in enumerate(pts):
        if p.dist_m >= target and i > split_idx[-1]:
            split_idx.append(i)
            target += segment_len_m
    if split_idx[-1] != len(pts) - 1:
        split_idx.append(len(pts) - 1)

    # 末段过短则与前一段合并
    if len(split_idx) >= 3:
        last_len = pts[split_idx[-1]].dist_m - pts[split_idx[-2]].dist_m
        if last_len < segment_len_m * 0.5:
            split_idx.pop(-2)

    segments: List[Segment] = []
    for idx in range(len(split_idx) - 1):
        a, b = split_idx[idx], split_idx[idx + 1]
        chunk = pts[a : b + 1]
        slopes = [
            _slope_pct(chunk[i].ele, chunk[i + 1].ele, chunk[i + 1].dist_m - chunk[i].dist_m)
            for i in range(len(chunk) - 1)
        ]
        length = chunk[-1].dist_m - chunk[0].dist_m
        segments.append(
            Segment(
                segment_id=f"{prefix}{idx + 1:03d}",
                index=idx,
                points=list(chunk),
                length_m=length,
                mean_slope_pct=(sum(slopes) / len(slopes)) if slopes else 0.0,
                max_slope_pct=max(slopes, key=abs) if slopes else 0.0,
            )
        )
    return segments


def route_stats(points: Sequence[Point]) -> dict:
    """总里程、累计爬升、最大海拔。"""
    pts = smooth_elevation(with_distance(points))
    climb = sum(max(0.0, pts[i + 1].ele - pts[i].ele) for i in range(len(pts) - 1))
    return {
        "distance_km": round(pts[-1].dist_m / 1000, 2),
        "climb_m": round(climb, 1),
        "max_ele_m": round(max(p.ele for p in pts), 1),
    }
