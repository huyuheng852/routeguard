"""意图解析：把一句自然语言变成结构化的出行简报。

原则
----
1. **能解析就解析，解析不到就假设并声明**，不要一上来问十个问题。
2. 所有假设都写进 `assumptions`，用户可以一眼看到系统替他决定了什么。
3. 缺少致命信息（起终点）才报错。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional, Tuple


@dataclass
class RiderProfile:
    ftp_w: Optional[float] = None
    weight_kg: Optional[float] = None
    bike: str = "公路车"

    @property
    def wkg(self) -> Optional[float]:
        if self.ftp_w and self.weight_kg:
            return round(self.ftp_w / self.weight_kg, 2)
        return None

    @property
    def level(self) -> str:
        w = self.wkg
        if w is None:
            return "未知"
        if w < 2.0:
            return "新手"
        if w < 2.8:
            return "休闲"
        if w < 3.5:
            return "进阶"
        if w < 4.5:
            return "较强"
        return "竞技"

    @property
    def base_daily_km(self) -> float:
        """按 W/kg 插值的日行基准（轻装、平路）。"""
        w = self.wkg or 2.5
        table = [(1.5, 60), (2.0, 80), (2.5, 95), (3.0, 110), (3.5, 125), (4.0, 140), (4.5, 150)]
        if w <= table[0][0]:
            return table[0][1]
        for (x1, y1), (x2, y2) in zip(table, table[1:]):
            if w <= x2:
                return y1 + (y2 - y1) * (w - x1) / (x2 - x1)
        return table[-1][1]


@dataclass
class TripBrief:
    raw_text: str
    origin: str
    destination: str
    start_date: str  # YYYY-MM-DD
    corridor: str = "central"
    rider: RiderProfile = field(default_factory=RiderProfile)
    luggage: str = "驮包（约 8kg）"
    rest_every: int = 7  # 每骑几天安排一天休整
    assumptions: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    daily_km_pref: Optional[float] = None


FTP_RE = re.compile(r"FTP\s*(?:为|是|[:：])?\s*(\d{2,3})\s*(?:w|W|瓦)")
WEIGHT_RE = re.compile(r"体重\s*(?:为|是|[:：])?\s*(\d{2,3})\s*(?:kg|KG|公斤|千克)")
DATE_RE = re.compile(r"(\d{4})\s*年\s*(\d{1,2})\s*月(?:\s*(\d{1,2})\s*日)?")
ROUTE_RE = re.compile(
    r"从\s*([\u4e00-\u9fa5A-Za-z]{2,10}?)\s*(?:到|至|骑到|出发到|—|-|→)\s*([\u4e00-\u9fa5A-Za-z]{2,10})"
)
DAILY_RE = re.compile(r"每天\s*(\d{2,3})\s*(?:公里|km|千米)")


def parse_brief(text: str) -> Tuple[TripBrief, List[str]]:
    """解析出行需求。返回 (简报, 致命问题列表)。"""
    text = (text or "").strip()
    issues: List[str] = []
    assumptions: List[str] = []
    warnings: List[str] = []

    rider = RiderProfile()
    m = FTP_RE.search(text)
    if m:
        rider.ftp_w = float(m.group(1))
    else:
        assumptions.append("未识别到 FTP，按 200W / 70kg（约 2.9 W/kg）估算体能")

    m = WEIGHT_RE.search(text)
    if m:
        rider.weight_kg = float(m.group(1))
    else:
        assumptions.append("未识别到体重，按 70kg 估算")

    if rider.ftp_w and not (60 <= rider.ftp_w <= 600):
        warnings.append(f"FTP {rider.ftp_w:.0f}W 不在常见范围（60–600W），请核对")
    if rider.weight_kg and not (35 <= rider.weight_kg <= 150):
        warnings.append(f"体重 {rider.weight_kg:.0f}kg 不在常见范围（35–150kg），请核对")

    m = DATE_RE.search(text)
    if m:
        year, month = int(m.group(1)), int(m.group(2))
        day = int(m.group(3)) if m.group(3) else 1
        start_date = f"{year:04d}-{month:02d}-{day:02d}"
    else:
        start_date = "2027-06-01"
        assumptions.append("未识别到出发时间，按 2027-06-01 起算")

    m = ROUTE_RE.search(text)
    if m:
        origin, destination = m.group(1), m.group(2)
    else:
        origin, destination = "", ""
        issues.append("没有识别出起点和终点。请用「从 A 到 B」的句式，例如：从瑞金到延安")

    corridor = "historic" if "长征" in text else "central"
    if "长征" in text:
        warnings.append(
            "按「历史长征线」规划（中央红军真实路线，含 4114m 夹金山口、松潘草地与多处无住宿段）。"
            "若你想走的是难度可控的 2300km 纵贯线，请加 --corridor central。"
        )

    m = DAILY_RE.search(text)
    daily_pref = float(m.group(1)) if m else None

    brief = TripBrief(
        raw_text=text,
        origin=origin,
        destination=destination,
        start_date=start_date,
        corridor=corridor,
        rider=rider,
        daily_km_pref=daily_pref,
        assumptions=assumptions,
        warnings=warnings,
    )
    return brief, issues
