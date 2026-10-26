"""真实道路路由：把航点之间的直线换成真正的公路。

为什么需要这一层
----------------
走廊折线（大圆距离 × 绕行系数）只能回答"大概多远"，不能回答"走哪条路"。
本模块接入开源路由服务，拿到真实路网几何、实际里程与真实爬升。

服务选择
--------
1. **BRouter**（默认）：免费、无需 key、有自行车 profile（trekking / fastbike），
   返回高程，能算真实爬升。中国大陆路网可用。
2. **OSRM**（备选）：`routing.openstreetmap.de` 的 bike profile，无高程。
3. **直线**（最后兜底）：全部失败时退回走廊直线，并**明确标注**这不是真实路线。

所有结果都落盘缓存，同一条腿只请求一次。
"""

from __future__ import annotations

import hashlib
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from .corridor import Leg, Waypoint, build_legs, haversine_km


@dataclass
class LegRoute:
    """一段真实路线。"""

    distance_km: float
    climb_m: Optional[int]
    points: List[Tuple[float, float, Optional[float]]]  # (lat, lon, ele)
    provider: str
    note: str = ""


class RoutingError(RuntimeError):
    pass


def _http_get(url: str, timeout: int) -> str:
    req = urllib.request.Request(
        url, headers={"User-Agent": "routeguard/0.3 (research prototype)"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "ignore")


def _climb_from_points(points: Sequence[Tuple[float, float, Optional[float]]]) -> Optional[int]:
    eles = [p[2] for p in points]
    if not eles or any(e is None for e in eles):
        return None
    gain = 0.0
    for a, b in zip(eles, eles[1:]):
        if b > a:
            gain += b - a
    return int(round(gain))


class BRouterClient:
    """BRouter：免费、带高程的自行车路由。"""

    name = "brouter"

    def __init__(
        self,
        base_url: str = "https://brouter.de/brouter",
        profile: str = "trekking",
        timeout: int = 40,
    ) -> None:
        self.base_url = base_url
        self.profile = profile
        self.timeout = timeout

    def route(self, a: Waypoint, b: Waypoint) -> LegRoute:
        params = {
            "lonlats": f"{a.lon},{a.lat}|{b.lon},{b.lat}",
            "profile": self.profile,
            "alternativeidx": "0",
            "format": "geojson",
        }
        url = f"{self.base_url}?{urllib.parse.urlencode(params)}"
        raw = _http_get(url, self.timeout)
        data = json.loads(raw)
        feats = data.get("features") or []
        if not feats:
            raise RoutingError(f"BRouter 无返回：{a.name} → {b.name}")
        feat = feats[0]
        coords = feat["geometry"]["coordinates"]
        props = feat.get("properties", {})
        points = [(float(c[1]), float(c[0]), float(c[2]) if len(c) > 2 else None) for c in coords]
        dist_km = float(props.get("track-length", 0)) / 1000.0
        if dist_km <= 0:
            raise RoutingError("BRouter 里程为 0")
        ascend = props.get("filtered ascend")
        climb = int(float(ascend)) if ascend not in (None, "") else _climb_from_points(points)
        return LegRoute(
            distance_km=round(dist_km, 2),
            climb_m=climb,
            points=points,
            provider=f"brouter:{self.profile}",
        )


class OSRMClient:
    """OSRM bike profile（无高程，爬升用走廊估算兜底）。"""

    name = "osrm"

    def __init__(
        self,
        base_url: str = "https://routing.openstreetmap.de/routed-bike",
        timeout: int = 30,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def route(self, a: Waypoint, b: Waypoint) -> LegRoute:
        url = (
            f"{self.base_url}/route/v1/driving/"
            f"{a.lon},{a.lat};{b.lon},{b.lat}"
            "?overview=full&geometries=geojson&steps=false"
        )
        raw = _http_get(url, self.timeout)
        data = json.loads(raw)
        routes = data.get("routes") or []
        if not routes:
            raise RoutingError(f"OSRM 无返回：{a.name} → {b.name}")
        route = routes[0]
        coords = route["geometry"]["coordinates"]
        points = [(float(c[1]), float(c[0]), None) for c in coords]
        return LegRoute(
            distance_km=round(float(route["distance"]) / 1000.0, 2),
            climb_m=None,
            points=points,
            provider="osrm:bike",
        )


class Router:
    """带缓存与降级的组合路由器。"""

    def __init__(
        self,
        providers: Sequence[object],
        cache_dir: Optional[Path] = None,
        delay_s: float = 0.25,
        max_consecutive_failures: int = 2,
    ) -> None:
        self.providers = list(providers)
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.delay_s = delay_s
        self.max_fail = max_consecutive_failures
        self.stats = {"cache_hit": 0, "routed": 0, "fallback": 0}
        self.last_provider = ""
        # 每个 provider 的连续失败计数；连续失败超过阈值就本次运行停用，
        # 否则 50+ 段每段都等一次超时，规划会卡到一个小时。
        self._fail_streak = {getattr(p, "name", str(i)): 0 for i, p in enumerate(self.providers)}
        self.disabled: set[str] = set()
        if self.cache_dir:
            self.cache_dir.mkdir(parents=True, exist_ok=True)

    @property
    def all_disabled(self) -> bool:
        return bool(self.providers) and len(self.disabled) >= len(self.providers)

    def _cache_path(self, a: Waypoint, b: Waypoint) -> Optional[Path]:
        if not self.cache_dir:
            return None
        key = f"{a.name}-{b.name}-{a.lat:.4f}{a.lon:.4f}-{b.lat:.4f}{b.lon:.4f}"
        digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
        return self.cache_dir / f"leg_{digest}.json"

    def route(self, a: Waypoint, b: Waypoint) -> LegRoute:
        cache = self._cache_path(a, b)
        if cache and cache.exists():
            data = json.loads(cache.read_text(encoding="utf-8"))
            self.stats["cache_hit"] += 1
            self.last_provider = data["provider"]
            return LegRoute(
                distance_km=data["distance_km"],
                climb_m=data["climb_m"],
                points=[(p[0], p[1], p[2]) for p in data["points"]],
                provider=data["provider"],
            )

        for provider in self.providers:
            name = getattr(provider, "name", "?")
            if name in self.disabled:
                continue
            try:
                result = provider.route(a, b)
                self.stats["routed"] += 1
                self.last_provider = result.provider
                self._fail_streak[name] = 0
                if cache:
                    cache.write_text(
                        json.dumps(
                            {
                                "distance_km": result.distance_km,
                                "climb_m": result.climb_m,
                                "provider": result.provider,
                                "points": result.points,
                            },
                            ensure_ascii=False,
                        ),
                        encoding="utf-8",
                    )
                if self.delay_s:
                    time.sleep(self.delay_s)
                return result
            except (urllib.error.URLError, TimeoutError, KeyError, ValueError, RoutingError):
                self.last_provider = f"{name}失败"
                self._fail_streak[name] = self._fail_streak.get(name, 0) + 1
                if self._fail_streak[name] >= self.max_fail:
                    self.disabled.add(name)
                continue

        # 全部失败：退回直线，并明确标注
        self.stats["fallback"] += 1
        self.last_provider = "straight"
        mid = ((a.lat + b.lat) / 2, (a.lon + b.lon) / 2, None)
        return LegRoute(
            distance_km=round(haversine_km(a.lat, a.lon, b.lat, b.lon) * 1.22, 2),
            climb_m=None,
            points=[(a.lat, a.lon, None), mid, (b.lat, b.lon, None)],
            provider="straight-fallback",
            note="路由服务不可用，已退回直线近似",
        )


def build_routed_legs(
    waypoints: Sequence[Waypoint],
    router: Router,
    verbose: bool = True,
) -> List[Leg]:
    """用真实路由替换走廊直线，返回带几何的 Leg 列表。"""
    base = build_legs(waypoints)
    legs: List[Leg] = []
    for idx, leg in enumerate(base):
        route = router.route(leg.start, leg.end)
        climb = route.climb_m if route.climb_m is not None else leg.climb_m
        legs.append(
            Leg(
                start=leg.start,
                end=leg.end,
                km=route.distance_km,
                climb_m=climb,
                points=route.points,
                provider=route.provider,
            )
        )
        if verbose:
            print(
                f"  [{idx + 1:2d}/{len(base)}] {leg.start.name} → {leg.end.name}  "
                f"{route.distance_km:6.1f} km  +{climb:5d} m  ({route.provider})"
            )
    return legs
