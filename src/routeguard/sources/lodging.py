"""住宿检索：在开源地图上找附近的酒店/客栈。

为什么需要
----------
走廊航点只有县城以上才保证有住宿；自动切段产生的"途中"停靠点没有。
默认让人露营是不负责任的——先用 OSM 查一遍周边有没有酒店，
查到就把落脚点挪过去，查不到才提示露营。

数据来源：OpenStreetMap（Overpass API），`tourism=hotel/guest_house/hostel/motel/apartment`。
中国大陆的 OSM 住宿覆盖不完整，**查不到 ≠ 没有**，所以结果要如实说明。
"""

from __future__ import annotations

import hashlib
import json
import math
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

EARTH_R = 6371008.8
LODGING_TAGS = ("hotel", "guest_house", "hostel", "motel", "apartment", "resort")


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_R * math.asin(math.sqrt(a))


@dataclass
class Lodging:
    name: str
    lat: float
    lon: float
    kind: str
    dist_m: float

    def to_dict(self) -> Dict[str, object]:
        return {
            "name": self.name,
            "lat": self.lat,
            "lon": self.lon,
            "kind": self.kind,
            "dist_m": round(self.dist_m),
        }


class OverpassLodgingFinder:
    """按点查询周边住宿，带缓存与失败降级。"""

    def __init__(
        self,
        url: str = "https://overpass-api.de/api/interpreter",
        timeout: int = 25,
        cache_dir: Optional[Path] = None,
        radius_m: int = 8000,
    ) -> None:
        self.url = url
        self.timeout = timeout
        self.radius_m = radius_m
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.last_error: str = ""
        self.disabled: bool = False
        self._fail_streak = 0
        if self.cache_dir:
            self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _cache_path(self, lat: float, lon: float) -> Optional[Path]:
        if not self.cache_dir:
            return None
        key = f"{lat:.4f},{lon:.4f},{self.radius_m}"
        return self.cache_dir / f"lodging_{hashlib.sha1(key.encode()).hexdigest()[:16]}.json"

    def _query(self, lat: float, lon: float) -> str:
        d = self.radius_m / 111000.0
        bbox = f"{lat - d:.5f},{lon - d:.5f},{lat + d:.5f},{lon + d:.5f}"
        return (
            f"[out:json][timeout:{self.timeout}];"
            f'node["tourism"]({bbox});'
            f"out;"
        )

    def find(self, lat: float, lon: float) -> List[Lodging]:
        """返回按距离升序的住宿列表；失败返回空列表并记录 last_error。"""
        if self.disabled:
            self.last_error = "住宿检索已停用（连续失败，避免逐个超时拖慢规划）"
            return []
        cache = self._cache_path(lat, lon)
        payload: Optional[dict] = None
        if cache and cache.exists():
            payload = json.loads(cache.read_text(encoding="utf-8"))
        else:
            try:
                req = urllib.request.Request(
                    self.url,
                    data=self._query(lat, lon).encode("utf-8"),
                    headers={"User-Agent": "routeguard/0.3 (research prototype)"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))
                if cache:
                    cache.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
                self._fail_streak = 0
            except Exception as exc:  # noqa: BLE001 网络问题一律降级
                self.last_error = f"{type(exc).__name__}: {exc}"
                self._fail_streak += 1
                # 连续失败两次就熔断：否则几十个点逐个超时，规划要跑十几分钟
                if self._fail_streak >= 2:
                    self.disabled = True
                    self.last_error = "网络/接口连续失败，本次规划已停用住宿检索"
                return []

        out: List[Lodging] = []
        for el in payload.get("elements", []):
            tags = el.get("tags", {}) or {}
            kind = tags.get("tourism")
            if kind not in LODGING_TAGS:
                continue
            plat, plon = el.get("lat"), el.get("lon")
            if plat is None or plon is None:
                continue
            dist = _haversine_m(lat, lon, plat, plon)
            if dist > self.radius_m:
                continue
            out.append(
                Lodging(
                    name=tags.get("name") or f"{kind}（未命名）",
                    lat=plat,
                    lon=plon,
                    kind=kind,
                    dist_m=dist,
                )
            )
        out.sort(key=lambda x: x.dist_m)
        self.last_error = ""
        return out


def nearest_lodging(
    finder: Optional[OverpassLodgingFinder],
    lat: float,
    lon: float,
    max_walk_m: float = 6000.0,
) -> tuple[Optional[Lodging], str]:
    """找最近住宿。返回 (住宿, 说明)。说明用于写进报告，哪怕是失败原因。"""
    if finder is None:
        return None, "未启用住宿检索"
    found = finder.find(lat, lon)
    if not found:
        if finder.last_error:
            return None, f"住宿检索失败（{finder.last_error[:60]}），未能确认"
        return None, "周边未检索到酒店/客栈（OSM 数据可能不全）"
    best = found[0]
    if best.dist_m > max_walk_m:
        return None, (
            f"最近的住宿「{best.name}」在 {best.dist_m / 1000:.1f} km 外，超出步行范围，"
            f"建议调整行程"
        )
    return best, f"最近住宿「{best.name}」（{best.dist_m / 1000:.1f} km，{best.kind}）"
