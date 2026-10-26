"""地名解析：把"海口""五指山"这类地名换成经纬度。

数据源是 OpenStreetMap 的 Nominatim（免费、无需 key）。结果落盘缓存，
同一地名只查一次；查不到就把原因如实返回，由上层提示用户改用坐标。

也支持直接给坐标：`110.32,20.03` 这种写法不联网。
"""

from __future__ import annotations

import hashlib
import json
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional


@dataclass
class Place:
    name: str
    lat: float
    lon: float
    addresstype: str = ""

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "lat": self.lat,
            "lon": self.lon,
            "addresstype": self.addresstype,
        }


COORD_RE = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*[,，]\s*(-?\d+(?:\.\d+)?)\s*$")


def split_via(text: str) -> List[str]:
    """切分 --via 字符串。

    地名之间用逗号；坐标本身含逗号，所以出现 `;` `|` `、` 时以它们为分隔符，
    例如 "110.32,20.03;110.10,19.52"。
    """
    if any(sep in text for sep in ";；|、"):
        parts = re.split(r"[;；|、]", text)
    else:
        parts = re.split(r"[,，]", text)
    return [p.strip() for p in parts if p.strip()]


class NominatimGeocoder:
    def __init__(
        self,
        url: str = "https://nominatim.openstreetmap.org/search",
        timeout: int = 15,
        cache_dir: Optional[Path] = None,
        language: str = "zh",
    ) -> None:
        self.url = url
        self.timeout = timeout
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.language = language
        self.last_error = ""
        if self.cache_dir:
            self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _cache_path(self, query: str) -> Optional[Path]:
        if not self.cache_dir:
            return None
        key = hashlib.sha1(query.encode("utf-8")).hexdigest()[:16]
        return self.cache_dir / f"geo_{key}.json"

    def lookup(self, query: str) -> Optional[Place]:
        """返回第一个匹配地点；`名称@纬度,经度` 与纯坐标都不联网。"""
        query = (query or "").strip()
        if not query:
            self.last_error = "空地名"
            return None

        label = ""
        if "@" in query:
            label, _, query = query.partition("@")
            label = label.strip()
            query = query.strip()

        m = COORD_RE.match(query)
        if m:
            a, b = float(m.group(1)), float(m.group(2))
            # 国内习惯写「经度,纬度」，GIS 习惯写「纬度,经度」，靠取值范围判别
            if abs(a) > 90 and abs(b) <= 90:
                lat, lon = b, a
            elif abs(b) > 90 and abs(a) <= 90:
                lat, lon = a, b
            else:
                lat, lon = a, b
            if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                self.last_error = "坐标超出范围"
                return None
            return Place(
                name=label or f"{lat:.4f},{lon:.4f}",
                lat=lat,
                lon=lon,
                addresstype="coordinate",
            )

        cache = self._cache_path(query)
        if cache and cache.exists():
            data = json.loads(cache.read_text(encoding="utf-8"))
            return Place(**data) if data else None

        params = {
            "q": query,
            "format": "jsonv2",
            "limit": "1",
            "accept-language": self.language,
        }
        url = f"{self.url}?{urllib.parse.urlencode(params)}"
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "routeguard/0.4 (cycling route planner)"}
            )
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                items = json.loads(resp.read().decode("utf-8", "ignore"))
        except Exception as exc:  # noqa: BLE001 网络问题一律降级
            self.last_error = f"{type(exc).__name__}: {exc}"
            return None

        if not items:
            self.last_error = f"OSM 未收录「{query}」"
            if cache:
                cache.write_text("null", encoding="utf-8")
            return None

        top = items[0]
        place = Place(
            name=label or query,
            lat=float(top["lat"]),
            lon=float(top["lon"]),
            addresstype=top.get("addresstype") or top.get("type") or "",
        )
        if cache:
            cache.write_text(json.dumps(place.to_dict(), ensure_ascii=False), encoding="utf-8")
        self.last_error = ""
        return place

    def lookup_many(self, queries: List[str]) -> tuple[List[Place], List[str]]:
        """批量解析，返回 (成功列表, 失败原因列表)。"""
        places, errors = [], []
        for q in queries:
            p = self.lookup(q)
            if p is None:
                errors.append(f"{q}（{self.last_error}）")
            else:
                places.append(p)
        return places, errors
