"""环境指标提供者。

本文件定义"数据层 → 指标层"的接口。骨架阶段用合成数据保证全链路可跑；
接真实数据时，实现同一个 `IndicatorProvider` 协议即可替换，无需改上层。
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, List, Protocol, Sequence

from .schema import Evidence, Segment


class IndicatorProvider(Protocol):
    """所有数据源都必须实现这个协议。"""

    name: str

    def canopy_height_at(self, lat: float, lon: float) -> float:
        """返回该点的树冠高度（米）。供遮荫模型使用。"""
        ...

    def evidences(self, segment: Segment, when: str) -> List[Evidence]:
        """返回该路段在指定日期可用的环境证据。"""
        ...


def _hash_unit(*parts: object) -> float:
    """把任意参数映射到 [0,1) 的确定性伪随机数。"""
    raw = "|".join(str(p) for p in parts).encode("utf-8")
    digest = hashlib.sha256(raw).digest()
    return int.from_bytes(digest[:6], "big") / float(1 << 48)


@dataclass
class SyntheticProvider:
    """合成数据源：用于算法联调与单元测试，**不得用于论文实验结论**。"""

    seed: str = "demo"
    name: str = "synthetic:v1"

    def _center(self, segment: Segment) -> tuple[float, float]:
        pts = segment.points
        return pts[len(pts) // 2].lat, pts[len(pts) // 2].lon

    def canopy_height_at(self, lat: float, lon: float) -> float:
        """平滑的树冠高度场：沿经度做正弦起伏，模拟林荫道与开阔段交替。"""
        base = 6.0 + 5.0 * math.sin((lon * 900.0) + (_hash_unit(self.seed, round(lat, 4)) * 0.7))
        noise = _hash_unit(self.seed, round(lat, 5), round(lon, 5)) * 2.0
        return max(0.0, base + noise)

    def _value(self, segment: Segment, key: str) -> float:
        lat, lon = self._center(segment)
        return _hash_unit(self.seed, segment.segment_id, key, round(lat, 4), round(lon, 4))

    def evidences(self, segment: Segment, when: str) -> List[Evidence]:
        u_lst = self._value(segment, "lst")
        u_ndvi = self._value(segment, "ndvi")
        u_imp = self._value(segment, "impervious")
        u_hum = self._value(segment, "humidity")

        day = datetime.strptime(when, "%Y-%m-%d")
        img_from = (day - timedelta(days=12)).strftime("%Y-%m-%d")
        img_to = (day + timedelta(days=3)).strftime("%Y-%m-%d")
        met_from = when
        met_to = (day + timedelta(days=1)).strftime("%Y-%m-%d")

        lat, lon = self._center(segment)
        canopy = self.canopy_height_at(lat, lon)
        lst = 32.0 + 10.0 * u_lst  # 32~42 ℃
        ndvi = 0.25 + 0.55 * u_ndvi  # 0.25~0.80
        impervious = 0.15 + 0.7 * u_imp
        humidity = 40.0 + 45.0 * u_hum

        return [
            Evidence(
                indicator="lst_c",
                value=round(lst, 1),
                unit="℃",
                source="sentinel-2:L2A",
                valid_from=img_from,
                valid_to=img_to,
                spatial_scale_m=30.0,
                quality=0.72,
                note="地表温度，晴空像元中位数（骨架阶段为合成值）",
            ),
            Evidence(
                indicator="ndvi",
                value=round(ndvi, 3),
                unit="",
                source="sentinel-2:L2A",
                valid_from=img_from,
                valid_to=img_to,
                spatial_scale_m=10.0,
                quality=0.80,
                note="归一化植被指数",
            ),
            Evidence(
                indicator="impervious_ratio",
                value=round(impervious, 3),
                unit="",
                source="sentinel-2:L2A",
                valid_from=img_from,
                valid_to=img_to,
                spatial_scale_m=10.0,
                quality=0.68,
                note="不透水面比例（NDBI 估算）",
            ),
            Evidence(
                indicator="canopy_height_m",
                value=round(canopy, 2),
                unit="m",
                source="meta:canopy-height:1m",
                valid_from="2026-01-01",
                valid_to="2027-12-31",
                spatial_scale_m=1.0,
                quality=0.65,
                note="树冠高度（骨架阶段为合成场）",
            ),
            Evidence(
                indicator="air_temp_c",
                value=round(24.0 + 8.0 * u_lst, 1),
                unit="℃",
                source="open-meteo:forecast",
                valid_from=met_from,
                valid_to=met_to,
                spatial_scale_m=3000.0,
                quality=0.85,
                note="2 米气温预报",
            ),
            Evidence(
                indicator="humidity_pct",
                value=round(humidity, 1),
                unit="%",
                source="open-meteo:forecast",
                valid_from=met_from,
                valid_to=met_to,
                spatial_scale_m=3000.0,
                quality=0.85,
                note="相对湿度预报",
            ),
            Evidence(
                indicator="road_class",
                value="secondary" if u_imp > 0.5 else "residential",
                unit="",
                source="osm:highway",
                valid_from="2026-01-01",
                valid_to="2027-12-31",
                spatial_scale_m=10.0,
                quality=0.95,
                note="OSM 道路等级",
            ),
        ]


def index_by_indicator(evidences: Sequence[Evidence]) -> Dict[str, Evidence]:
    """同名指标保留质量最高（quality 最大）的一条。"""
    best: Dict[str, Evidence] = {}
    for ev in evidences:
        cur = best.get(ev.indicator)
        if cur is None or ev.quality > cur.quality:
            best[ev.indicator] = ev
    return best
