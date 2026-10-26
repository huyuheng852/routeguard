"""路线走廊：瑞金 → 延安。

说明
----
本模块提供的是**走廊（corridor）**，不是逐米可骑的路由。每个航点是一座城市/县城，
两两之间用大圆距离 × 绕行系数近似公路里程。这样做的原因：

1. 2000+ km 的逐段路由需要专业路网引擎（OSRM/GraphHopper），且中国区数据可用性不稳；
2. 规划阶段真正需要的是"每晚住哪、哪天多远"，而不是每个路口的转向；
3. 把"走廊"和"实际路由"分开，误差可量化：用系数 1.22 近似公路绕行。

爬升为**人工估算**（按地形分区），精度约 ±30%，报告中必须标注为估算值。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

EARTH_R = 6371008.8
DETOUR_FACTOR = 1.22  # 公路里程 ≈ 大圆距离 × 绕行系数


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_R * math.asin(math.sqrt(a)) / 1000.0


@dataclass(frozen=True)
class Waypoint:
    name: str
    lat: float
    lon: float
    service: str  # city / county / town
    climb_from_prev_m: int = 0
    note: str = ""
    elev_m: int = 0  # 海拔（米）
    terrain: str = "rolling"  # flat / rolling / mountain / plateau

    @property
    def has_hotel(self) -> bool:
        return self.service in ("city", "county", "lodge")

    @property
    def has_food(self) -> bool:
        return True


TERRAIN_K = {"flat": 2, "rolling": 5, "mountain": 8, "plateau": 6}


@dataclass(frozen=True)
class Leg:
    start: Waypoint
    end: Waypoint
    km: float
    climb_m: int
    points: Optional[Sequence[Tuple[float, float, Optional[float]]]] = None
    provider: str = "corridor"

    @property
    def label(self) -> str:
        return f"{self.start.name} → {self.end.name}"

    @property
    def routed(self) -> bool:
        """是否来自真实路网（而不是走廊直线）。"""
        return self.provider != "corridor"


# ---------------------------------------------------------------------------
# 中部纵贯线（推荐）：约 2300 km，全程有县城以上城镇，补给与住宿可控
# ---------------------------------------------------------------------------
CENTRAL: List[Waypoint] = [
    Waypoint("瑞金", 25.885, 115.973, "city", 0, "起点，长征出发地"),
    Waypoint("于都", 25.952, 115.415, "county", 345, "长征第一渡"),
    Waypoint("赣州", 25.831, 114.935, "city", 244),
    Waypoint("遂川", 26.323, 114.168, "county", 1380, "进入罗霄山脉"),
    Waypoint("井冈山市", 26.748, 114.289, "city", 900, "山区，注意爬升"),
    Waypoint("莲花", 27.128, 113.961, "county", 650),
    Waypoint("萍乡", 27.622, 113.854, "city", 612),
    Waypoint("醴陵", 27.646, 113.497, "city", 215),
    Waypoint("长沙", 28.228, 112.939, "city", 416),
    Waypoint("湘阴", 28.678, 112.909, "county", 122),
    Waypoint("岳阳", 29.357, 113.129, "city", 288),
    Waypoint("临湘", 29.476, 113.450, "county", 123),
    Waypoint("赤壁", 29.725, 113.900, "city", 208),
    Waypoint("咸宁", 29.841, 114.322, "city", 260),
    Waypoint("武汉", 30.593, 114.305, "city", 306),
    Waypoint("孝感", 30.924, 113.916, "city", 128),
    Waypoint("广水", 31.617, 113.826, "county", 570),
    Waypoint("信阳", 32.147, 114.091, "city", 390),
    Waypoint("驻马店", 32.983, 114.022, "city", 456),
    Waypoint("漯河", 33.581, 114.017, "city", 162),
    Waypoint("许昌", 34.036, 113.852, "city", 192),
    Waypoint("郑州", 34.746, 113.625, "city", 400),
    Waypoint("洛阳", 34.619, 112.454, "city", 792),
    Waypoint("三门峡", 34.773, 111.200, "city", 1128, "进入黄土台塬边缘"),
    Waypoint("灵宝", 34.520, 110.880, "county", 300),
    Waypoint("潼关", 34.544, 110.247, "county", 355),
    Waypoint("渭南", 34.499, 109.510, "city", 498),
    Waypoint("西安", 34.341, 108.940, "city", 268),
    Waypoint("铜川", 34.897, 108.945, "city", 750, "进入黄土高原"),
    Waypoint("黄陵", 35.580, 109.260, "county", 1188, "黄帝陵，海拔抬升明显"),
    Waypoint("富县", 35.988, 109.380, "county", 798),
    Waypoint("延安", 36.585, 109.490, "city", 1215, "终点，黄土丘陵沟壑区"),
]

# ---------------------------------------------------------------------------
# 历史真实路线（默认）：沿中央红军 1934–1935 年行军路线，约 5000 km
# 含 4000m 级山口、高原草甸与长距离无人区，难度远高于中部纵贯线
# ---------------------------------------------------------------------------
HISTORIC: List[Waypoint] = [
    # —— 江西突围 ——
    Waypoint("瑞金", 25.885, 115.973, "city", 0, "长征出发地", 200, "rolling"),
    Waypoint("于都", 25.952, 115.415, "county", 0, "长征第一渡", 150, "rolling"),
    Waypoint("信丰", 25.386, 114.923, "county", 0, "突破第一道封锁线", 200, "rolling"),
    Waypoint("崇义", 25.687, 114.307, "county", 0, "", 300, "mountain"),
    # —— 湘南 ——
    Waypoint("汝城", 25.550, 113.685, "county", 0, "第二道封锁线", 450, "mountain"),
    Waypoint("宜章", 25.400, 112.948, "county", 0, "第三道封锁线", 350, "mountain"),
    Waypoint("宁远", 25.600, 111.940, "county", 0, "", 300, "rolling"),
    Waypoint("道县", 25.527, 111.600, "county", 0, "", 200, "rolling"),
    # —— 广西：湘江战役 ——
    Waypoint("全州", 25.929, 111.073, "county", 0, "湘江战役·脚山铺", 200, "rolling"),
    Waypoint("兴安", 25.611, 110.671, "county", 0, "湘江战役纪念地", 250, "rolling"),
    Waypoint("龙胜", 25.797, 110.011, "county", 0, "越城岭山区", 400, "mountain"),
    # —— 贵州 ——
    Waypoint("通道", 26.158, 109.786, "county", 0, "通道转兵", 400, "mountain"),
    Waypoint("黎平", 26.231, 109.137, "county", 0, "黎平会议", 450, "mountain"),
    Waypoint("剑河", 26.727, 108.441, "county", 0, "", 500, "mountain"),
    Waypoint("台江", 26.667, 108.317, "county", 0, "", 600, "mountain"),
    Waypoint("镇远", 27.049, 108.425, "county", 0, "", 500, "mountain"),
    Waypoint("施秉", 27.033, 108.126, "county", 0, "", 600, "mountain"),
    Waypoint("余庆", 27.221, 107.888, "county", 0, "", 700, "mountain"),
    Waypoint("遵义", 27.725, 106.927, "city", 0, "遵义会议", 850, "mountain"),
    Waypoint("桐梓", 28.133, 106.825, "county", 0, "娄山关", 950, "mountain"),
    Waypoint("习水", 28.327, 106.200, "county", 0, "四渡赤水", 1100, "mountain"),
    Waypoint("赤水", 28.590, 105.697, "city", 0, "", 300, "rolling"),
    # —— 云南：扎西整编、金沙江 ——
    Waypoint("叙永", 28.167, 105.444, "county", 0, "川滇通道", 400, "rolling"),
    Waypoint("威信", 27.843, 105.048, "county", 0, "扎西会议", 1200, "mountain"),
    Waypoint("镇雄", 27.441, 104.874, "county", 0, "", 1600, "mountain"),
    Waypoint("威宁", 26.856, 104.278, "county", 0, "乌蒙山区", 2100, "mountain"),
    Waypoint("会泽", 26.410, 103.300, "county", 0, "", 2100, "mountain"),
    Waypoint("东川", 26.083, 103.182, "county", 0, "泥石流区，雨季注意", 1200, "mountain"),
    Waypoint("禄劝", 25.552, 102.470, "county", 0, "皎平渡·巧渡金沙江", 1600, "mountain"),
    # —— 川滇：会理 ——
    Waypoint("会理", 26.655, 102.245, "county", 0, "会理会议", 1800, "mountain"),
    Waypoint("德昌", 27.403, 102.180, "county", 0, "", 1400, "mountain"),
    Waypoint("西昌", 27.894, 102.264, "city", 0, "彝海结盟方向", 1500, "mountain"),
    Waypoint("冕宁", 28.549, 102.171, "county", 0, "彝海结盟", 1700, "mountain"),
    # —— 川西：大渡河、泸定桥、雪山 ——
    Waypoint("石棉", 29.234, 102.360, "county", 0, "强渡大渡河·安顺场", 1000, "mountain"),
    Waypoint("泸定", 29.914, 102.235, "county", 0, "飞夺泸定桥", 1330, "mountain"),
    Waypoint("天全", 30.066, 102.758, "county", 0, "", 700, "mountain"),
    Waypoint("宝兴", 30.368, 102.814, "county", 0, "夹金山南麓", 1000, "mountain"),
    Waypoint("夹金山口", 30.750, 102.650, "town", 0, "海拔 4114m，需推车/扛车，注意失温", 4114, "mountain"),
    Waypoint("小金", 30.999, 102.364, "county", 0, "懋功会师", 2400, "mountain"),
    Waypoint("两河口", 31.320, 102.440, "town", 0, "两河口会议，住宿少", 3000, "mountain"),
    Waypoint("马尔康", 31.899, 102.221, "city", 0, "卓克基土司官寨", 2600, "mountain"),
    Waypoint("黑水", 32.060, 102.990, "county", 0, "毛儿盖会议方向", 2800, "mountain"),
    Waypoint("松潘", 32.655, 103.596, "county", 0, "", 2900, "plateau"),
    # —— 草地 ——
    Waypoint("若尔盖", 33.578, 102.963, "county", 0, "松潘草地，海拔 3400m，雨季泥泞", 3400, "plateau"),
    Waypoint("班佑", 33.600, 102.850, "town", 0, "草地出口，几乎无住宿", 3450, "plateau"),
    # —— 甘南：腊子口 ——
    Waypoint("腊子口", 34.098, 103.696, "town", 0, "腊子口战役，无住宿", 2200, "mountain"),
    Waypoint("哈达铺", 34.200, 104.300, "county", 0, "翻越岷山", 2000, "mountain"),
    Waypoint("榜罗", 35.000, 104.900, "town", 0, "榜罗镇会议", 1900, "rolling"),
    Waypoint("通渭", 35.210, 105.240, "county", 0, "", 1800, "rolling"),
    Waypoint("静宁", 35.520, 105.730, "county", 0, "", 1700, "rolling"),
    # —— 六盘山、陕北 ——
    Waypoint("隆德", 35.620, 106.110, "county", 0, "六盘山北麓", 2000, "mountain"),
    Waypoint("六盘山", 35.650, 106.200, "town", 0, "翻越六盘山，无住宿", 2500, "mountain"),
    Waypoint("固原", 36.000, 106.280, "city", 0, "", 1750, "rolling"),
    Waypoint("环县", 36.570, 107.310, "county", 0, "", 1400, "rolling"),
    Waypoint("吴起", 36.927, 108.176, "county", 0, "中央红军长征终点", 1300, "rolling"),
    Waypoint("延安", 36.585, 109.490, "city", 0, "终点", 1000, "rolling"),
]

CORRIDORS = {"central": CENTRAL, "historic": HISTORIC}

CORRIDOR_LABELS = {
    "central": "中部纵贯线（备选，难度低）",
    "historic": "历史长征线（中央红军真实路线）",
}


def _leg_climb(a: Waypoint, b: Waypoint, km: float) -> int:
    """爬升：优先用显式值；否则由海拔差 + 地形起伏估算。"""
    if b.climb_from_prev_m:
        return b.climb_from_prev_m
    if b.elev_m:
        gain = max(0, b.elev_m - a.elev_m)
        return int(gain + km * TERRAIN_K.get(b.terrain, 8))
    return 0


def build_legs(waypoints: Sequence[Waypoint]) -> List[Leg]:
    legs: List[Leg] = []
    for a, b in zip(waypoints, waypoints[1:]):
        straight = haversine_km(a.lat, a.lon, b.lat, b.lon)
        km = round(straight * DETOUR_FACTOR, 1)
        legs.append(Leg(start=a, end=b, km=km, climb_m=_leg_climb(a, b, km)))
    return legs


def corridor_stats(waypoints: Sequence[Waypoint]) -> dict:
    legs = build_legs(waypoints)
    return {
        "legs": len(legs),
        "km": round(sum(l.km for l in legs), 1),
        "climb_m": sum(l.climb_m for l in legs),
        "max_leg_km": max((l.km for l in legs), default=0),
    }
