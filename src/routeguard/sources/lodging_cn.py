"""国内免费住宿检索：高德 / 天地图。

为什么不用 OSM
--------------
Overpass 在国内又慢又不全——OSM 的酒店 POI 在中国基本没人录入。
换镜像没用，得换数据源。

两个适配器都免费（申请 key 即可），差别：
* 高德：最快、酒店最全，商用需遵守其条款；
* 天地图：国家地理信息平台，授权最干净。

查不到时一律返回空列表，由上层写"未核实"——换了数据源也不许猜。
"""

from __future__ import annotations

import hashlib
import json
import math
import urllib.parse
import urllib.request
from pathlib import Path
from typing import List, Optional

from .lodging import Lodging

EARTH_R = 6371008.8


def _dist_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_R * math.asin(math.sqrt(a))


class _BaseFinder:
    name = "base"

    def __init__(self, key: str, cache_dir: Optional[Path] = None, radius_m: int = 8000,
                 timeout: int = 8) -> None:
        self.key = (key or "").strip()
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.radius_m = radius_m
        self.timeout = timeout
        self.last_error = ""
        if self.cache_dir:
            self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _cache_path(self, lat: float, lon: float) -> Optional[Path]:
        if not self.cache_dir:
            return None
        key = f"{self.name}:{lat:.4f},{lon:.4f},{self.radius_m}"
        return self.cache_dir / f"{self.name}_{hashlib.sha1(key.encode()).hexdigest()[:16]}.json"

    def _cached(self, lat: float, lon: float) -> Optional[List[Lodging]]:
        p = self._cache_path(lat, lon)
        if p and p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            return [Lodging(**d) for d in data]
        return None

    def _store(self, lat: float, lon: float, items: List[Lodging]) -> None:
        p = self._cache_path(lat, lon)
        if p:
            p.write_text(
                json.dumps([i.to_dict() for i in items], ensure_ascii=False),
                encoding="utf-8",
            )

    def find(self, lat: float, lon: float) -> List[Lodging]:
        if not self.key:
            self.last_error = f"未配置 {self.name} 的 key"
            return []
        cached = self._cached(lat, lon)
        if cached is not None:
            return cached
        try:
            items = self._fetch(lat, lon)
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"{type(exc).__name__}: {exc}"
            return []
        self.last_error = "" if items else (self.last_error or "接口返回 0 条结果")
        items.sort(key=lambda x: x.dist_m)
        self._store(lat, lon, items)
        return items

    def _fetch(self, lat: float, lon: float) -> List[Lodging]:
        raise NotImplementedError


class AmapLodgingFinder(_BaseFinder):
    """高德 Web 服务 API：周边搜索「酒店」。"""

    name = "amap"

    def _fetch(self, lat: float, lon: float) -> List[Lodging]:
        params = {
            "key": self.key,
            "location": f"{lon:.6f},{lat:.6f}",
            "keywords": "酒店",
            "radius": str(self.radius_m),
            "offset": "25",
            "page": "1",
            "sortrule": "distance",
            "extensions": "base",
        }
        url = "https://restapi.amap.com/v3/place/around?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={"User-Agent": "routeguard/1.0"})
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", "ignore"))

        if str(data.get("status")) != "1":
            self.last_error = f"高德返回错误：{data.get('info')}"
            return []

        out: List[Lodging] = []
        for poi in data.get("pois") or []:
            loc = str(poi.get("location", ""))
            if "," not in loc:
                continue
            slon, slat = loc.split(",")[:2]
            plat, plon = float(slat), float(slon)
            dist = float(poi.get("distance") or 0) or _dist_m(lat, lon, plat, plon)
            out.append(
                Lodging(
                    name=poi.get("name") or "未命名住宿",
                    lat=plat,
                    lon=plon,
                    kind=f"amap:{poi.get('type', '酒店').split(';')[-1]}",
                    dist_m=dist,
                )
            )
        return out


class TiandituLodgingFinder(_BaseFinder):
    """天地图 v2 搜索：国家平台，免费。"""

    name = "tianditu"

    def _fetch(self, lat: float, lon: float) -> List[Lodging]:
        dlat = self.radius_m / 111000.0
        dlon = self.radius_m / (111000.0 * max(math.cos(math.radians(lat)), 0.2))
        post_str = {
            "keyWord": "酒店",
            "level": 12,
            "mapBound": f"{lon - dlon:.6f},{lat - dlat:.6f},{lon + dlon:.6f},{lat + dlat:.6f}",
            "queryType": 1,
            "start": 0,
            "count": 20,
            "show": 1,
        }
        url = (
            "http://api.tianditu.gov.cn/v2/search?"
            + urllib.parse.urlencode(
                {"postStr": json.dumps(post_str, ensure_ascii=False), "type": "query", "tk": self.key}
            )
        )
        req = urllib.request.Request(url, headers={"User-Agent": "routeguard/1.0"})
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", "ignore"))

        pois = data.get("pois") or []
        out: List[Lodging] = []
        for poi in pois:
            lonlat = str(poi.get("lonlat", ""))
            if "," not in lonlat:
                continue
            slon, slat = lonlat.split(",")[:2]
            plat, plon = float(slat), float(slon)
            out.append(
                Lodging(
                    name=poi.get("name") or "未命名住宿",
                    lat=plat,
                    lon=plon,
                    kind="tianditu:酒店",
                    dist_m=_dist_m(lat, lon, plat, plon),
                )
            )
        return out


def build_cn_finder(provider: str, settings, cache_dir: Optional[Path] = None):
    """按配置构造国内住宿检索器；key 缺失时返回 None。"""
    provider = (provider or "auto").lower()
    cache_dir = cache_dir or (Path(settings.cache_dir) / "lodging")

    if provider in ("auto", "amap"):
        key = getattr(settings, "amap_key", "")
        if key:
            return AmapLodgingFinder(key, cache_dir=cache_dir, radius_m=8000)
        if provider == "amap":
            return None

    if provider in ("auto", "tianditu"):
        key = getattr(settings, "tianditu_key", "")
        if key:
            return TiandituLodgingFinder(key, cache_dir=cache_dir, radius_m=8000)

    return None
