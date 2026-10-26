"""补齐两样硬需求：高程与酒店，全部免费、无需 key。

高程：OpenTopoData（开放 SRTM 30m）
    一次最多 100 个点，1 次/秒。给整条路线的抽样点补高程，
    再算累计爬升——不依赖 BRouter 是否返回高程。

酒店：Photon（komoot 的 OSM 检索，带空间偏置）
    OSM 的 tourism 数据在中国稀疏，但比 Overpass 快得多、也稳得多；
    查不到仍然返回空，由上层标"未核实"。
"""

from __future__ import annotations

import hashlib
import json
import math
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from .lodging import Lodging

EARTH_R = 6371008.8


def _dist_m(a, b):
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    dl = math.radians(b[1] - a[1])
    dp = p2 - p1
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_R * math.asin(math.sqrt(h))


class OpenTopoElevation:
    """用 OpenTopoData 给轨迹补高程。"""

    name = "opentopodata"

    def __init__(
        self,
        url: str = "https://api.opentopodata.org/v1/srtm30m",
        cache_dir: Optional[Path] = None,
        timeout: int = 20,
        chunk: int = 100,
        pause: float = 1.1,
    ) -> None:
        self.url = url
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.timeout = timeout
        self.chunk = chunk
        self.pause = pause
        self.last_error = ""
        if self.cache_dir:
            self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _cache_path(self, points) -> Optional[Path]:
        if not self.cache_dir:
            return None
        key = ";".join(f"{p[0]:.4f},{p[1]:.4f}" for p in points[:50])
        h = hashlib.sha1(key.encode()).hexdigest()[:16]
        return self.cache_dir / f"elev_{h}.json"

    def fill(self, points: Sequence[Tuple[float, float, Optional[float]]]):
        """返回带高程的点列；失败时原样返回。"""
        pts = [(float(p[0]), float(p[1]), p[2]) for p in points]
        if not pts:
            return pts

        # 抽样：最多 100 个点，其余用线性插值
        step = max(1, len(pts) // self.chunk)
        idx = list(range(0, len(pts), step))[: self.chunk]
        sample = [pts[i] for i in idx]

        cache = self._cache_path(sample)
        elevs = None
        if cache and cache.exists():
            elevs = json.loads(cache.read_text(encoding="utf-8"))
        else:
            try:
                locs = "|".join(f"{p[0]:.6f},{p[1]:.6f}" for p in sample)
                url = f"{self.url}?locations={urllib.parse.quote(locs)}"
                req = urllib.request.Request(
                    url, headers={"User-Agent": "routeguard/1.0"}
                )
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    data = json.loads(resp.read().decode("utf-8", "ignore"))
                elevs = [r.get("elevation") for r in data.get("results", [])]
                if len(elevs) != len(sample):
                    raise ValueError("返回点数不匹配")
                if cache:
                    cache.write_text(json.dumps(elevs), encoding="utf-8")
                time.sleep(self.pause)
            except Exception as exc:  # noqa: BLE001
                self.last_error = f"{type(exc).__name__}: {exc}"
                return pts

        # 把抽样高程插回全部点
        out = list(pts)
        for k, i in enumerate(idx):
            e = elevs[k] if elevs and k < len(elevs) else None
            out[i] = (pts[i][0], pts[i][1], e)
        # 线性插值填补未抽样的点
        known = [i for i in idx if out[i][2] is not None]
        if len(known) >= 2:
            for i in range(len(out)):
                if out[i][2] is not None:
                    continue
                prev = max([k for k in known if k < i], default=None)
                nxt = min([k for k in known if k > i], default=None)
                if prev is None or nxt is None:
                    continue
                f = (i - prev) / (nxt - prev)
                e = out[prev][2] + (out[nxt][2] - out[prev][2]) * f
                out[i] = (out[i][0], out[i][1], e)
        return out

    @staticmethod
    def climb(points) -> int:
        eles = [p[2] for p in points]
        if not eles or any(e is None for e in eles):
            return 0
        # 平滑：忽略 <3 m 的抖动，避免噪声累积成假爬升
        gain = 0.0
        for a, b in zip(eles, eles[1:]):
            if b - a > 3:
                gain += b - a
        return int(round(gain))


class PhotonLodgingFinder:
    """用 Photon 的空间偏置检索附近酒店，无需 key。"""

    name = "photon"

    def __init__(
        self,
        url: str = "https://photon.komoot.io/api/",
        cache_dir: Optional[Path] = None,
        radius_m: int = 8000,
        timeout: int = 10,
    ) -> None:
        self.url = url
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.radius_m = radius_m
        self.timeout = timeout
        self.last_error = ""
        if self.cache_dir:
            self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _cache_path(self, lat: float, lon: float) -> Optional[Path]:
        if not self.cache_dir:
            return None
        key = f"photon-hotel:{lat:.4f},{lon:.4f}:{self.radius_m}"
        return self.cache_dir / f"ph_{hashlib.sha1(key.encode()).hexdigest()[:16]}.json"

    def find(self, lat: float, lon: float) -> List[Lodging]:
        cache = self._cache_path(lat, lon)
        if cache and cache.exists():
            return [Lodging(**d) for d in json.loads(cache.read_text(encoding="utf-8"))]

        out: List[Lodging] = []
        seen = set()
        for query in ("hotel", "酒店"):
            params = {
                "q": query,
                "lat": f"{lat:.5f}",
                "lon": f"{lon:.5f}",
                "limit": "20",
            }
            url = f"{self.url}?{urllib.parse.urlencode(params)}"
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "routeguard/1.0"})
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    data = json.loads(resp.read().decode("utf-8", "ignore"))
            except Exception as exc:  # noqa: BLE001
                self.last_error = f"{type(exc).__name__}: {exc}"
                continue

            for feat in data.get("features") or []:
                props = feat.get("properties") or {}
                if props.get("osm_key") not in ("tourism", "amenity"):
                    continue
                value = (props.get("osm_value") or "").lower()
                if value not in ("hotel", "guest_house", "hostel", "motel", "apartment", "resort"):
                    continue
                coords = feat.get("geometry", {}).get("coordinates") or []
                if len(coords) < 2:
                    continue
                plat, plon = float(coords[1]), float(coords[0])
                d = _dist_m((lat, lon), (plat, plon))
                if d > self.radius_m:
                    continue
                name = props.get("name") or "未命名住宿"
                if (name, round(plat, 4)) in seen:
                    continue
                seen.add((name, round(plat, 4)))
                out.append(
                    Lodging(name=name, lat=plat, lon=plon,
                            kind=f"osm:{value}", dist_m=d)
                )
        out.sort(key=lambda x: x.dist_m)
        if cache:
            cache.write_text(
                json.dumps([i.to_dict() for i in out], ensure_ascii=False), encoding="utf-8"
            )
        return out


class ChainFinder:
    """依次尝试多个住宿检索器；前一个查不到再退下一个。"""

    def __init__(self, finders, name: str = "chain") -> None:
        self.finders = list(finders)
        self.name = name
        self.last_error = ""

    def find(self, lat: float, lon: float):
        errors = []
        for f in self.finders:
            try:
                items = f.find(lat, lon)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{type(exc).__name__}: {exc}")
                continue
            if items:
                return items
        self.last_error = "；".join(errors) if errors else "所有数据源都没查到"
        return []
