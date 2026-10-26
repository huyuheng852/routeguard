"""OSM 证据源：路面、道路等级、隧道、补给点。

工程约束
--------
1. **能连就接，连不上就降级**：网络失败返回空上下文，让断言自然落到"未核实"，
   而不是抛异常把整条流水线带崩。
2. **结果缓存**：同一个 bbox 只查一次，避免 Overpass 限流（也方便离线复现）。
3. **可注入夹具**：`fixture=` 直接读本地 JSON，单元测试不依赖网络。
"""

from __future__ import annotations

import hashlib
import json
import math
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

EARTH_R = 6371008.8


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_R * math.asin(math.sqrt(a))


@dataclass
class SegmentContext:
    """某路段周边可用的 OSM 事实。

    `available=False` 表示**没有取到数据**（断网/超时/离线），
    与"取到了、但该字段为空"是两件完全不同的事——前者必须落回"未核实"。
    """

    segment_id: str
    road_class: Optional[str] = None
    surface: Optional[str] = None
    tunnel: Optional[bool] = None
    poi_supply: int = 0
    poi_bike_shop: int = 0
    available: bool = True
    poi_sparse: bool = False  # 区域 POI 覆盖稀疏时，不能把"查不到"当"没有"
    source: str = "osm"
    note: str = ""

    @property
    def empty(self) -> bool:
        return (
            self.road_class is None
            and self.surface is None
            and self.tunnel is None
            and not self.poi_supply
            and not self.poi_bike_shop
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "segment_id": self.segment_id,
            "available": self.available,
            "road_class": self.road_class,
            "surface": self.surface,
            "tunnel": self.tunnel,
            "poi_supply": self.poi_supply,
            "poi_bike_shop": self.poi_bike_shop,
            "poi_sparse": self.poi_sparse,
            "note": self.note,
        }


class OSMClient:
    def __init__(
        self,
        url: str = "https://overpass-api.de/api/interpreter",
        timeout: int = 25,
        cache_dir: Optional[Path] = None,
        radius_m: int = 150,
        offline: bool = False,
        fixture: Optional[Path] = None,
    ) -> None:
        self.url = url
        self.timeout = timeout
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.radius_m = radius_m
        self.offline = offline
        self.fixture = Path(fixture) if fixture else None
        self.last_error: str = ""
        if self.cache_dir:
            self.cache_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # 对外
    # ------------------------------------------------------------------
    def fetch(self, segments: Sequence[Any]) -> Dict[str, SegmentContext]:
        """给每个路段取 OSM 上下文。任何异常都降级为空上下文（available=False）。"""
        empty = {
            s.segment_id: SegmentContext(segment_id=s.segment_id, note="未获取", available=False)
            for s in segments
        }
        if self.offline:
            self.last_error = "offline 模式，跳过 OSM"
            for ctx in empty.values():
                ctx.note = self.last_error
            return empty
        try:
            payload = self._load_payload(segments)
        except Exception as exc:  # noqa: BLE001  网络问题一律降级
            self.last_error = f"{type(exc).__name__}: {exc}"
            for ctx in empty.values():
                ctx.note = self.last_error
            return empty
        return self._assign(segments, payload)

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    def _bbox(self, segments: Sequence[Any]) -> tuple[float, float, float, float]:
        lats = [p.lat for s in segments for p in s.points]
        lons = [p.lon for s in segments for p in s.points]
        pad = 0.003
        return min(lats) - pad, min(lons) - pad, max(lats) + pad, max(lons) + pad

    def _build_query(self, segments: Sequence[Any]) -> str:
        s, w, n, e = self._bbox(segments)
        bbox = f"{s:.5f},{w:.5f},{n:.5f},{e:.5f}"
        return (
            f"[out:json][timeout:{self.timeout}];"
            f"(way[\"highway\"]({bbox});"
            f"node[\"shop\"]({bbox});"
            f"node[\"amenity\"=\"bicycle_repair_station\"]({bbox}););"
            f"out tags center;"
        )

    def _cache_path(self, query: str) -> Optional[Path]:
        if not self.cache_dir:
            return None
        digest = hashlib.sha1(query.encode("utf-8")).hexdigest()[:16]
        return self.cache_dir / f"osm_{digest}.json"

    def _load_payload(self, segments: Sequence[Any]) -> Dict[str, Any]:
        if self.fixture:
            return json.loads(self.fixture.read_text(encoding="utf-8"))

        query = self._build_query(segments)
        cache = self._cache_path(query)
        if cache and cache.exists():
            return json.loads(cache.read_text(encoding="utf-8"))

        req = urllib.request.Request(
            self.url,
            data=query.encode("utf-8"),
            headers={"User-Agent": "routeguard/0.2 (research prototype)"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        if cache:
            cache.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return payload

    def _assign(self, segments: Sequence[Any], payload: Dict[str, Any]) -> Dict[str, SegmentContext]:
        ways: List[Dict[str, Any]] = []
        supply_nodes: List[tuple[float, float]] = []
        bike_nodes: List[tuple[float, float]] = []

        for el in payload.get("elements", []):
            tags = el.get("tags", {}) or {}
            if el.get("type") == "way" and "highway" in tags:
                center = el.get("center") or {}
                lat, lon = center.get("lat"), center.get("lon")
                if lat is None or lon is None:
                    continue
                ways.append(
                    {
                        "lat": lat,
                        "lon": lon,
                        "highway": tags.get("highway"),
                        "surface": tags.get("surface"),
                        "tunnel": tags.get("tunnel"),
                    }
                )
            elif el.get("type") == "node":
                lat, lon = el.get("lat"), el.get("lon")
                if lat is None or lon is None:
                    continue
                if "shop" in tags:
                    supply_nodes.append((lat, lon))
                if tags.get("amenity") == "bicycle_repair_station":
                    bike_nodes.append((lat, lon))

        result: Dict[str, SegmentContext] = {}
        # 区域 POI 覆盖度：整条线路的 bbox 里 POI 太少时，不能拿"0 个"当反证
        poi_sparse = len(supply_nodes) < 3
        for seg in segments:
            mid = seg.points[len(seg.points) // 2]
            ctx = SegmentContext(segment_id=seg.segment_id, poi_sparse=poi_sparse)

            best = None
            best_d = float("inf")
            for way in ways:
                d = haversine_m(mid.lat, mid.lon, way["lat"], way["lon"])
                if d < best_d:
                    best_d, best = d, way
            if best is not None and best_d <= self.radius_m:
                ctx.road_class = best["highway"]
                ctx.surface = best["surface"]
                if best["tunnel"] is not None:
                    ctx.tunnel = str(best["tunnel"]).lower() not in ("no", "false", "0")

            ctx.poi_supply = sum(
                1 for lat, lon in supply_nodes if haversine_m(mid.lat, mid.lon, lat, lon) <= 300
            )
            ctx.poi_bike_shop = sum(
                1 for lat, lon in bike_nodes if haversine_m(mid.lat, mid.lon, lat, lon) <= 300
            )
            if ctx.empty:
                ctx.note = "OSM 周边无匹配要素"
            result[seg.segment_id] = ctx
        return result
