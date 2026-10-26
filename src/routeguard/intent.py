"""结构化需求解析：让大模型把一句人话拆成可执行的行程需求。

为什么不用正则
--------------
"总里程 1290 公里、路况好、不爬坡、要安全" —— 正则只能抓日期和"从A到B"。
其余（想看什么、强度偏好、环线方向、是否带娃）都要靠理解，这正是 LLM 该干的活。

设计约束
--------
1. **只做理解，不做决策**：LLM 输出结构化字段，路线与日程仍由确定性代码算；
2. **缺字段就标 missing**，交给澄清对话去问，绝不让模型"猜一个";
3. **失败必须降级**：没有 key 或调用失败时退回正则版 `brief.parse_brief`，
   功能不断，只是理解得粗一些。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import List, Optional

from .brief import RiderProfile, TripBrief
from .llm import LLMError

SYSTEM_PROMPT = """你是骑行路线规划的需求解析器。把用户的话拆成结构化字段，只输出 JSON。

字段说明：
- origin / destination：起点、终点的地名（中文，尽量规范到市/县名）。没提到就填 null。
- via：用户明确提到的沿途城镇或区域，按顺序数组。没提到就 []。
- loop：是否环线（起点=终点，或用户说"环岛""环湖""骑一圈"）。
- start_date：出发日期，格式 YYYY-MM-DD。只说了年月就填该月 1 日，并在 notes 里注明。
- days：用户明确要求的骑行天数，整数。没提到就 null。
- ftp_w / weight_kg：功率与体重，数字。没提到就 null。
- level：beginner / intermediate / advanced。能从语境判断就填，否则 null。
- daily_km：用户明确要求的每日里程。没提到就 null。
- preferences：用户对路线的偏好关键词数组，例如 ["海边","雨林","避开国道","少爬坡","有补给","风景好"]。
- missing：**必填但用户没给** 的字段名数组，只允许出现 "origin"、"destination"、"start_date"。
- suggested：如果用户说的是知名线路（如"海南环岛""环青海湖""川藏线"），
  给出这条线路**常规的起终点**建议，形如
  {"origin":"海口","destination":"海口","reason":"海南环岛通常从海口出发并回到海口"}。
  这是建议不是事实；用户没提的线路就别填。
- notes：你的补充说明数组，例如"只说了 2027 年 1 月，按 1 月 1 日处理"。

规则：
1. 不要编造用户没说的地名；不确定就填 null。
2. "海南岛东线""环青海湖"这类**线路名不是地名**，不要把整串当地名；
   如果里面有明确城市就填城市，没有就留空。
3. 只输出 JSON，不要解释。"""

USER_TEMPLATE = """用户原话：
{text}

请输出 JSON。"""


@dataclass
class TripIntent:
    origin: str = ""
    destination: str = ""
    via: List[str] = field(default_factory=list)
    loop: bool = False
    start_date: str = ""
    days: Optional[int] = None
    ftp_w: Optional[float] = None
    weight_kg: Optional[float] = None
    level: str = ""
    daily_km: Optional[float] = None
    preferences: List[str] = field(default_factory=list)
    missing: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    suggested_origin: str = ""
    suggested_destination: str = ""
    suggestion_reason: str = ""
    source: str = "rules"  # rules | llm


VALID_LEVELS = {"beginner", "intermediate", "advanced"}


def _as_float(value) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(str(value).strip().rstrip("wW公斤kg "))
    except (TypeError, ValueError):
        return None


def _as_int(value) -> Optional[int]:
    f = _as_float(value)
    return int(f) if f is not None else None


def _as_str(value) -> str:
    if value is None:
        return ""
    s = str(value).strip()
    if s.lower() in ("null", "none", "未知", "未提及", ""):
        return ""
    return s


def normalize_date(value: str) -> str:
    """把 2027-1 / 2027年1月 / 2027.1 统一成 YYYY-MM-01。"""
    s = _as_str(value)
    if not s:
        return ""
    m = re.match(r"^(\d{4})[-/.年](\d{1,2})(?:[-/.月](\d{1,2}))?", s)
    if not m:
        return ""
    year, month = int(m.group(1)), int(m.group(2))
    day = int(m.group(3)) if m.group(3) else 1
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return ""
    return f"{year:04d}-{month:02d}-{day:02d}"


def intent_from_dict(data: dict) -> TripIntent:
    """把模型返回的 JSON 规范化成 TripIntent（防御式，字段缺失也不炸）。"""
    via_raw = data.get("via") or []
    if isinstance(via_raw, str):
        via_raw = [v for v in re.split(r"[,，、;；>]", via_raw) if v.strip()]
    via = [_as_str(v) for v in via_raw if _as_str(v)]

    level = _as_str(data.get("level")).lower()
    if level not in VALID_LEVELS:
        level = ""

    missing = [_as_str(x) for x in (data.get("missing") or []) if _as_str(x)]
    missing = [m for m in missing if m in ("origin", "destination", "start_date")]

    notes = [_as_str(x) for x in (data.get("notes") or []) if _as_str(x)]

    suggested = data.get("suggested") or {}
    if not isinstance(suggested, dict):
        suggested = {}

    intent = TripIntent(
        origin=_as_str(data.get("origin")),
        destination=_as_str(data.get("destination")),
        via=via,
        loop=bool(data.get("loop")),
        start_date=normalize_date(data.get("start_date")),
        days=_as_int(data.get("days")),
        ftp_w=_as_float(data.get("ftp_w")),
        weight_kg=_as_float(data.get("weight_kg")),
        level=level,
        daily_km=_as_float(data.get("daily_km")),
        preferences=[_as_str(p) for p in (data.get("preferences") or []) if _as_str(p)],
        notes=notes,
        suggested_origin=_as_str(suggested.get("origin")),
        suggested_destination=_as_str(suggested.get("destination")),
        suggestion_reason=_as_str(suggested.get("reason")),
        source="llm",
    )

    # 模型可能漏报 missing，这里再兜一层
    need = []
    if not intent.origin:
        need.append("origin")
    if not intent.destination:
        need.append("destination")
    if not intent.start_date:
        need.append("start_date")
    intent.missing = sorted(set(intent.missing) | set(need))
    return intent


def extract_intent(text: str, llm=None) -> TripIntent:
    """优先用 LLM；没有模型或失败则退回正则。"""
    if llm is not None:
        try:
            data = llm.chat_json(SYSTEM_PROMPT, USER_TEMPLATE.format(text=text), 0.0)
            if isinstance(data, dict):
                return intent_from_dict(data)
        except Exception:  # noqa: BLE001 任何模型异常都降级，不能让规划挂掉
            pass

    return rules_intent(text)


def rules_intent(text: str) -> TripIntent:
    """正则兜底：只抓得起终点、日期、功率与体重。"""
    from .brief import parse_brief

    brief, _issues = parse_brief(text)

    intent = TripIntent(
        origin=brief.origin,
        destination=brief.destination,
        start_date=brief.start_date,
        ftp_w=brief.rider.ftp_w,
        weight_kg=brief.rider.weight_kg,
        source="rules",
    )
    need = []
    if not intent.origin:
        need.append("origin")
    if not intent.destination:
        need.append("destination")
    if not intent.start_date:
        need.append("start_date")
    intent.missing = need
    if "环" in text and ("岛" in text or "湖" in text):
        intent.loop = True
    return intent


def intent_to_brief(intent: TripIntent, raw_text: str) -> TripBrief:
    """把结构化需求转成规划器认识的 TripBrief。"""
    assumptions: List[str] = list(intent.notes)
    if intent.start_date:
        assumptions.append(f"出发日期：{intent.start_date}")
    if intent.preferences:
        assumptions.append("路线偏好：" + "、".join(intent.preferences))

    brief = TripBrief(
        raw_text=raw_text,
        origin=intent.origin,
        destination=intent.destination,
        start_date=intent.start_date or "",
        daily_km_pref=intent.daily_km,
    )
    brief.rider = RiderProfile(
        ftp_w=intent.ftp_w,
        weight_kg=intent.weight_kg,
    )
    brief.assumptions = assumptions
    return brief


CITIES_PROMPT = """你是长途骑行路线规划师。给出从起点到终点**沿途经过的城镇**，按顺序排列。

要求：
1. 只给真实存在的县级以上地名（中文），按骑行顺序从起点到终点；
2. 相邻两城公路距离 80–130 km；
3. 优先走国道/省道干线，避开无人区；
4. 不要重复起点终点，只输出中间城镇；
5. 只输出 JSON：{"cities": ["...", "..."]}"""


def suggest_route_cities(llm, origin: str, destination: str, count: int = 8) -> List[str]:
    """让模型给出一条合理的沿途城市链，用于替代"大圆直线上撒点"。"""
    if llm is None or not origin or not destination:
        return []
    try:
        data = llm.chat_json(
            CITIES_PROMPT,
            f"起点：{origin}\n终点：{destination}\n沿途大约需要 {count} 个城镇。",
            0.0,
        )
    except Exception:  # noqa: BLE001
        return []
    raw = data.get("cities") if isinstance(data, dict) else None
    if not isinstance(raw, list):
        return []
    out = []
    for c in raw:
        s = _as_str(c)
        if s and s not in out and s not in (origin, destination):
            out.append(s)
    return out


def recompute_missing(intent: TripIntent) -> List[str]:
    need = []
    if not intent.origin:
        need.append("origin")
    if not intent.destination:
        need.append("destination")
    if not intent.start_date:
        need.append("start_date")
    intent.missing = need
    return need


def fill_missing(intent: TripIntent, ask) -> List[str]:
    """逐项追问缺失字段。ask 是 `input` 这类函数。返回澄清记录。"""
    notes: List[str] = []
    prompts = {
        "origin": "这次骑行从哪里出发？（例：海口）",
        "destination": "终点是哪里？（环线就和起点填一样）",
        "start_date": "计划哪天出发？（例：2027-01-01，或 2027-01）",
    }
    for field_name in list(intent.missing):
        suggestion = (
            intent.suggested_origin if field_name == "origin" else
            intent.suggested_destination if field_name == "destination" else ""
        )
        prompt = prompts[field_name]
        if suggestion:
            why = f"（{intent.suggestion_reason}）" if intent.suggestion_reason else ""
            prompt = f"{prompt[:-1]}？没有的话我建议用「{suggestion}」{why}，回车采用："
        try:
            answer = (ask(prompt) or "").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not answer and suggestion:
            answer = suggestion
            notes.append(f"{'起点' if field_name == 'origin' else '终点'}采用建议值：{suggestion}")
        if not answer:
            continue
        if field_name == "start_date":
            normalized = normalize_date(answer)
            if normalized:
                intent.start_date = normalized
                notes.append(f"出发日期由对话确认：{normalized}")
        else:
            setattr(intent, field_name, answer)
            notes.append(f"{'起点' if field_name == 'origin' else '终点'}由对话确认：{answer}")
    recompute_missing(intent)
    return notes


def dump_intent(intent: TripIntent) -> str:
    return json.dumps(intent.__dict__, ensure_ascii=False, indent=2)
