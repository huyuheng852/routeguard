"""动态遮荫模型（核心创新点 1）。

思路
----
骑行者在某时刻是否被树荫覆盖，取决于三件事：
树有多高（冠层高度）、太阳在哪（高度角/方位角）、树长在什么方位。

静态 NDVI 只能回答"这里绿不绿"，回答不了"下午三点这条路上有没有阴凉"。
本模块用「冠层高度 + 太阳位置 + 阴影投射几何」计算**沿轨迹的实际遮荫比例**。

几何：太阳高度角 h 时，高度 H 的树在背光方向投出影长 L = H / tan(h)；
若骑行点沿背光方向 d 米处有一棵高度 >= d*tan(h) 的树，则该点处于阴影中。
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Callable, Dict, Sequence

from .schema import Point

METERS_PER_DEG_LAT = 111320.0


def _day_of_year(dt: datetime) -> int:
    return dt.timetuple().tm_yday


def solar_position(
    lat: float,
    lon: float,
    dt_local: datetime,
    tz_hours: float = 8.0,
) -> Dict[str, float]:
    """NOAA 近似算法。返回太阳高度角与方位角（度，方位角自北顺时针）。"""
    gamma = 2 * math.pi / 365.0 * (_day_of_year(dt_local) - 1 + (dt_local.hour - 12) / 24.0)

    eqtime = 229.18 * (
        0.000075
        + 0.001868 * math.cos(gamma)
        - 0.032077 * math.sin(gamma)
        - 0.014615 * math.cos(2 * gamma)
        - 0.040849 * math.sin(2 * gamma)
    )
    decl = (
        0.006918
        - 0.399912 * math.cos(gamma)
        + 0.070257 * math.sin(gamma)
        - 0.006758 * math.cos(2 * gamma)
        + 0.000907 * math.sin(2 * gamma)
        - 0.002697 * math.cos(3 * gamma)
        + 0.00148 * math.sin(3 * gamma)
    )

    minutes = dt_local.hour * 60 + dt_local.minute + dt_local.second / 60.0
    true_solar_time = (minutes + eqtime + 4.0 * lon - 60.0 * tz_hours) % 1440.0
    hour_angle = math.radians(true_solar_time / 4.0 - 180.0)
    phi = math.radians(lat)

    cos_zenith = math.sin(phi) * math.sin(decl) + math.cos(phi) * math.cos(decl) * math.cos(hour_angle)
    cos_zenith = max(-1.0, min(1.0, cos_zenith))
    zenith = math.acos(cos_zenith)
    elevation = 90.0 - math.degrees(zenith)

    azimuth = math.degrees(
        math.atan2(
            math.sin(hour_angle),
            math.cos(hour_angle) * math.sin(phi) - math.tan(decl) * math.cos(phi),
        )
    ) + 180.0
    azimuth %= 360.0

    return {"elevation_deg": elevation, "azimuth_deg": azimuth}


def _offset(lat: float, lon: float, bearing_deg: float, distance_m: float) -> tuple[float, float]:
    """沿方位角移动一段距离后的经纬度（小距离平面近似）。"""
    b = math.radians(bearing_deg)
    dlat = distance_m * math.cos(b) / METERS_PER_DEG_LAT
    dlon = distance_m * math.sin(b) / (METERS_PER_DEG_LAT * max(math.cos(math.radians(lat)), 1e-6))
    return lat + dlat, lon + dlon


def point_shaded(
    point: Point,
    sun_elev_deg: float,
    sun_az_deg: float,
    canopy_at: Callable[[float, float], float],
    search_m: float = 30.0,
    step_m: float = 3.0,
) -> bool:
    """判断一个轨迹点是否处于树荫中。"""
    if sun_elev_deg <= 3.0:
        # 太阳贴地：影子极长，且在骑行安全上属于眩光高风险时段
        return True
    tan_h = math.tan(math.radians(sun_elev_deg))
    bearing = (sun_az_deg + 180.0) % 360.0  # 阴影投向

    d = step_m
    while d <= search_m:
        need_height = d * tan_h
        lat, lon = _offset(point.lat, point.lon, bearing, d)
        if canopy_at(lat, lon) >= need_height:
            return True
        d += step_m
    return False


def shadow_profile(
    points: Sequence[Point],
    canopy_at: Callable[[float, float], float],
    when: datetime,
    tz_hours: float = 8.0,
    search_m: float = 30.0,
) -> Dict[str, float]:
    """沿轨迹计算遮荫比例等指标。"""
    if not points:
        return {"shaded_ratio": 0.0, "sun_elevation_deg": 0.0, "sun_azimuth_deg": 0.0, "mean_canopy_m": 0.0}

    mid = points[len(points) // 2]
    sun = solar_position(mid.lat, mid.lon, when, tz_hours=tz_hours)
    shaded = 0
    for p in points:
        if point_shaded(p, sun["elevation_deg"], sun["azimuth_deg"], canopy_at, search_m=search_m):
            shaded += 1

    mean_canopy = sum(canopy_at(p.lat, p.lon) for p in points) / len(points)
    return {
        "shaded_ratio": shaded / len(points),
        "sun_elevation_deg": round(sun["elevation_deg"], 1),
        "sun_azimuth_deg": round(sun["azimuth_deg"], 1),
        "mean_canopy_m": round(mean_canopy, 2),
    }
