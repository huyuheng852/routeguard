"""三个角色的判断逻辑。

骨架阶段先用**确定性规则**实现，保证可复现、可单测；
接大模型时，把 evaluate() 内部换成"LLM 读指标卡 + 输出 JSON"，接口不变。

角色边界（对应教案 p13 的四要素）：
* RouteAnalyst      —— 只看轨迹与道路属性，不看天气
* EnvironmentAnalyst —— 只看环境指标与遮荫，不看坡度
* （裁决由 consensus 承担，不在此文件）
"""

from __future__ import annotations

import math
from typing import Dict

from .schema import Evidence, Finding, Segment


def apparent_temperature_c(temp_c: float, humidity_pct: float, wind_ms: float = 1.0) -> float:
    """Steadman 体感温度，用于把"气温 + 湿度"折算成热风险。"""
    e = (humidity_pct / 100.0) * 6.105 * math.exp(17.27 * temp_c / (237.7 + temp_c))
    return temp_c + 0.33 * e - 0.70 * wind_ms - 4.0


def _ev(cards: Dict[str, Evidence], name: str) -> Evidence | None:
    return cards.get(name)


class RouteAnalyst:
    role = "RouteAnalyst"

    def evaluate(self, segment: Segment, cards: Dict[str, Evidence], shade: Dict[str, float]) -> Finding:
        slope = abs(segment.max_slope_pct)
        road = segment.road_class
        used = []

        if slope >= 10:
            risk, why = 3, f"最大坡度 {slope:.1f}%，超过 10%（长陡坡）"
        elif slope >= 6:
            risk, why = 2, f"最大坡度 {slope:.1f}%，属于较陡路段"
        elif slope >= 3:
            risk, why = 1, f"最大坡度 {slope:.1f}%，缓坡"
        else:
            risk, why = 0, f"最大坡度 {slope:.1f}%，基本平路"

        traffic_bump = {"trunk": 1, "primary": 1, "secondary": 0, "residential": 0}.get(road, 0)
        if traffic_bump:
            risk = min(3, risk + traffic_bump)
            why += f"；道路等级为 {road}，机动车干扰大"

        road_ev = _ev(cards, "road_class")
        if road_ev:
            used.append(road_ev)

        confidence = 0.9 if segment.length_m >= 200 else 0.6
        return Finding(
            analyst=self.role,
            segment_id=segment.segment_id,
            risk=risk,
            confidence=confidence,
            rationale=why,
            evidences=used,
        )


class EnvironmentAnalyst:
    role = "EnvironmentAnalyst"

    def evaluate(self, segment: Segment, cards: Dict[str, Evidence], shade: Dict[str, float]) -> Finding:
        used = []
        t_ev = _ev(cards, "air_temp_c")
        h_ev = _ev(cards, "humidity_pct")
        lst_ev = _ev(cards, "lst_c")
        for ev in (t_ev, h_ev, lst_ev):
            if ev:
                used.append(ev)

        temp = float(t_ev.value) if t_ev else 25.0
        hum = float(h_ev.value) if h_ev else 55.0
        lst = float(lst_ev.value) if lst_ev else temp + 8.0

        at = apparent_temperature_c(temp, hum)
        shaded = shade.get("shaded_ratio", 0.0)

        if at >= 38:
            risk = 3
            why = f"体感温度 {at:.1f}℃，属于极端热应激"
        elif at >= 33:
            risk = 2
            why = f"体感温度 {at:.1f}℃，热应激明显"
        elif at >= 29:
            risk = 1
            why = f"体感温度 {at:.1f}℃，轻度热负荷"
        else:
            risk = 0
            why = f"体感温度 {at:.1f}℃，热负荷可接受"

        # 遮荫是"调节项"：热 + 无荫 = 加一成风险；凉爽时有荫不加分（避免把舒适度当成安全）
        if risk >= 1 and shaded < 0.2:
            risk = min(3, risk + 1)
            why += f"；遮荫率仅 {shaded * 100:.0f}%，全程暴晒"
        elif shaded >= 0.6:
            why += f"；遮荫率 {shaded * 100:.0f}%，体感更友好"

        if lst >= 45:
            why += f"；地表温度 {lst:.0f}℃，辐射热强"

        return Finding(
            analyst=self.role,
            segment_id=segment.segment_id,
            risk=risk,
            confidence=0.85,
            rationale=why,
            evidences=used,
        )
