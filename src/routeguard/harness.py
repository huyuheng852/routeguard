"""把 RouteGuard 从"流水线"升级为"agent 工程"。

三件套
------
* **Tools**：能力注册表。每个工具声明名字、说明、参数，agent 按需调用；
* **Skills**：可插拔的领域知识（markdown 文件），命中关键词就注入提示词；
* **Memory**：跨会话记住骑手画像与偏好，下次不用重复说。

有了这三样，agent 的循环是：
    读记忆 → 匹配技能 → 让模型选工具 → 执行 → 观察 → 直到能出结果

没有模型时，`run_agent` 会退回确定性管线（`direct_legs`），功能不中断。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

# 工具算出的路线对象（含几何），供上层复用，避免二次请求路由服务
LAST_ROUTE: dict = {}


# --------------------------------------------------------------------------
# Tools
# --------------------------------------------------------------------------


@dataclass
class Tool:
    name: str
    description: str
    parameters: Dict[str, str]
    fn: Callable[..., Any]

    def describe(self) -> str:
        args = ", ".join(f"{k}({v})" for k, v in self.parameters.items()) or "无参数"
        return f"- {self.name}: {self.description}｜参数：{args}"


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: Dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def names(self) -> List[str]:
        return list(self._tools)

    def describe(self) -> str:
        return "\n".join(t.describe() for t in self._tools.values())

    def call(self, tool_name: str, **kwargs) -> dict:
        tool = self._tools.get(tool_name)
        if tool is None:
            return {"ok": False, "error": f"没有名为 {tool_name} 的工具，可用：{', '.join(self.names())}"}
        try:
            result = tool.fn(**kwargs)
            return {"ok": True, "data": result}
        except TypeError as exc:
            return {"ok": False, "error": f"参数不对：{exc}"}
        except Exception as exc:  # noqa: BLE001 工具失败不能让 agent 崩
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def default_tools(settings=None, geocoder=None, router=None, lodging=None) -> ToolRegistry:
    """把现有能力包装成工具。"""
    from .corridor import haversine_km
    from .planner import daily_target
    from .routegen import direct_legs

    reg = ToolRegistry()

    def geocode(name: str) -> list:
        g = geocoder
        if g is None:
            from .geocode import build_geocoder

            g = build_geocoder(settings.cache_dir / "geocode" if settings else None)
        place = g.lookup(name)
        if place is None:
            raise ValueError(f"地名「{name}」解析失败：{g.last_error}")
        return [{"name": place.name, "lat": place.lat, "lon": place.lon}]

    def plan_route(start: str, end: str, daily_km: float = 0) -> dict:
        """按地名规划。让模型自己传经纬度极易把 lat/lon 弄反，所以只收地名。"""
        from types import SimpleNamespace

        g = geocoder
        if g is None:
            from .geocode import build_geocoder

            g = build_geocoder(settings.cache_dir / "geocode" if settings else None)
        pa, pb = g.lookup(start), g.lookup(end)
        if pa is None or pb is None:
            missing = start if pa is None else end
            raise ValueError(f"地名「{missing}」解析失败")

        straight = haversine_km(pa.lat, pa.lon, pb.lat, pb.lon)
        budget = daily_km or 100.0

        r = router
        if r is None:
            from .router import BRouterClient, OSRMClient, Router

            r = Router(
                [BRouterClient(profile="fastbike"), OSRMClient()],
                cache_dir=settings.cache_dir / "routes" if settings else None,
            )
        a = SimpleNamespace(name=pa.name, lat=pa.lat, lon=pa.lon, elev_m=0)
        b = SimpleNamespace(name=pb.name, lat=pb.lat, lon=pb.lon, elev_m=0)
        wps, legs = direct_legs(r, a, b, budget, g)

        LAST_ROUTE.clear()
        LAST_ROUTE.update({"waypoints": wps, "legs": legs, "start": pa.name, "end": pb.name})

        km = round(sum(l.km for l in legs), 1)
        result = {
            "start": pa.name,
            "end": pb.name,
            "straight_km": round(straight, 1),
            "days": len(legs),
            "km": km,
            "climb": sum(l.climb_m for l in legs),
            "engine": legs[0].provider if legs else "",
            "stops": [w.name for w in wps],
        }
        # 合理性自检：路线长度应落在直线距离的 1.0–3.0 倍之间
        ratio = km / straight if straight else 0
        result["ratio"] = round(ratio, 2)
        if ratio and (ratio < 0.95 or ratio > 3.0):
            result["warning"] = (
                f"里程异常：{km} km 是直线 {straight:.0f} km 的 {ratio:.2f} 倍，"
                "可能路由失败或坐标不对，请复核"
            )
        return result

    def find_lodging(lat: float, lon: float) -> list:
        f = lodging
        if f is None:
            return []
        items = f.find(lat, lon)
        return [{"name": i.name, "dist_m": round(i.dist_m), "kind": i.kind} for i in items[:5]]

    def distance(lat1: float, lon1: float, lat2: float, lon2: float) -> dict:
        km = haversine_km(lat1, lon1, lat2, lon2)
        return {"km": round(km, 1)}

    reg.register(Tool("geocode", "把地名换成经纬度", {"name": "中文地名"}, geocode))
    reg.register(
        Tool(
            "plan_route",
            "按地名规划起终点之间的多日行程（真实路网），返回里程/天数/落脚点",
            {"start": "起点地名", "end": "终点地名", "daily_km": "float，可省略"},
            plan_route,
        )
    )
    reg.register(Tool("find_lodging", "查坐标附近的酒店/客栈", {"lat": "float", "lon": "float"}, find_lodging))
    reg.register(
        Tool(
            "distance",
            "两点之间的大圆距离，用来判断合理性",
            {"lat1": "float", "lon1": "float", "lat2": "float", "lon2": "float"},
            distance,
        )
    )
    return reg


# --------------------------------------------------------------------------
# Skills
# --------------------------------------------------------------------------


@dataclass
class Skill:
    name: str
    description: str
    triggers: List[str]
    body: str

    def matches(self, text: str) -> bool:
        return any(t in text for t in self.triggers)


class SkillLibrary:
    """从 skills/ 目录加载 markdown 技能；文件头用 `---` 写元信息。"""

    def __init__(self, root: Optional[Path] = None) -> None:
        self.skills: List[Skill] = []
        self.root = root or (Path(__file__).resolve().parent / "skills")
        self.reload()

    def reload(self) -> None:
        self.skills = []
        if not self.root.exists():
            return
        for path in sorted(self.root.glob("*.md")):
            text = path.read_text(encoding="utf-8")
            meta: Dict[str, str] = {}
            body = text
            if text.startswith("---"):
                parts = text.split("---", 2)
                if len(parts) >= 3:
                    for line in parts[1].strip().splitlines():
                        if ":" in line:
                            k, _, v = line.partition(":")
                            meta[k.strip()] = v.strip()
                    body = parts[2].strip()
            triggers = [t.strip() for t in meta.get("triggers", "").split(",") if t.strip()]
            self.skills.append(
                Skill(
                    name=meta.get("name") or path.stem,
                    description=meta.get("description", ""),
                    triggers=triggers,
                    body=body,
                )
            )

    def match(self, text: str) -> List[Skill]:
        return [s for s in self.skills if s.matches(text)]

    def prompt_for(self, text: str) -> str:
        hit = self.match(text)
        if not hit:
            return ""
        blocks = [f"### 技能：{s.name}\n{s.description}\n{s.body}" for s in hit]
        return "以下是本任务适用的领域技能，请遵守：\n\n" + "\n\n".join(blocks)


# --------------------------------------------------------------------------
# Memory
# --------------------------------------------------------------------------


class Memory:
    """极简跨会话记忆：键值 + 时间戳，存 JSON。"""

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = path or (Path.cwd() / ".memory" / "rider.json")
        self.data: Dict[str, Any] = {}
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                self.data = {}

    def remember(self, key: str, value: Any) -> None:
        self.data[key] = value
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")

    def recall(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def as_prompt(self) -> str:
        if not self.data:
            return ""
        facts = "；".join(f"{k}={v}" for k, v in self.data.items() if not k.startswith("_"))
        return f"已知的骑手信息（来自历史会话）：{facts}"


# --------------------------------------------------------------------------
# Agent loop
# --------------------------------------------------------------------------

AGENT_PROMPT = """你是骑行路线规划 agent。你可以调用工具来完成任务。

可用的工具：
@@TOOLS@@

@@SKILLS@@

@@MEMORY@@

工作方式：每一步只输出一个 JSON。
- 要调用工具：{"thought": "为什么", "tool": "工具名", "args": {...}}
- 已经能回答：{"thought": "为什么", "final": {"origin": "...", "destination": "...", "summary": "..."}}

规则：
1. 先用 geocode 把地名变成坐标，再用 plan_route 规划；
2. 不要编造坐标；地名解析失败就如实说；
3. 最多 6 步，够了就给 final；
4. 只输出 JSON，不要解释。"""


@dataclass
class AgentTrace:
    steps: List[dict] = field(default_factory=list)
    final: Optional[dict] = None
    used_skills: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"steps": self.steps, "final": self.final, "skills": self.used_skills}


def run_agent(
    text: str,
    llm=None,
    tools: Optional[ToolRegistry] = None,
    skills: Optional[SkillLibrary] = None,
    memory: Optional[Memory] = None,
    max_steps: int = 6,
) -> AgentTrace:
    """让模型自己决定调用哪些工具；没有模型时直接失败，由调用方降级。"""
    tools = tools or ToolRegistry()
    skills = skills or SkillLibrary()
    memory = memory or Memory()

    trace = AgentTrace()
    matched = skills.match(text)
    trace.used_skills = [s.name for s in matched]

    if llm is None:
        trace.steps.append({"error": "未配置模型，agent 模式不可用"})
        return trace

    system = (
        AGENT_PROMPT.replace("@@TOOLS@@", tools.describe())
        .replace("@@SKILLS@@", skills.prompt_for(text))
        .replace("@@MEMORY@@", memory.as_prompt())
    )
    history = [f"用户需求：{text}"]

    last_plan = None
    for step in range(max_steps):
        # 步数预算：剩最后一步时明确要求收尾，避免模型无限验证
        if step == max_steps - 1:
            history.append(
                "这是最后一步，必须输出 {\"thought\": \"...\", \"final\": {...}}，"
                "不要再调用工具。"
            )
        try:
            out = llm.chat_json(system, "\n".join(history), 0.0)
        except Exception as exc:  # noqa: BLE001
            trace.steps.append({"step": step, "error": f"{type(exc).__name__}: {exc}"})
            return trace

        if not isinstance(out, dict):
            trace.steps.append({"step": step, "error": "模型输出不是 JSON 对象"})
            return trace

        if "final" in out:
            trace.final = out.get("final")
            trace.steps.append({"step": step, "thought": out.get("thought", ""), "final": trace.final})
            return trace

        name = out.get("tool")
        args = out.get("args") or {}
        if not name:
            trace.steps.append({"step": step, "error": "既没有 tool 也没有 final", "raw": out})
            return trace

        result = tools.call(name, **args) if isinstance(args, dict) else {"ok": False, "error": "args 必须是对象"}
        trace.steps.append(
            {"step": step, "thought": out.get("thought", ""), "tool": name, "args": args, "result": result}
        )
        if name == "plan_route" and result.get("ok"):
            last_plan = result.get("data")
        brief = json.dumps(result, ensure_ascii=False)[:600]
        history.append(f"调用 {name} 的结果：{brief}")

    if last_plan:
        trace.final = {
            "origin": last_plan.get("start"),
            "destination": last_plan.get("end"),
            "summary": (
                f"共 {last_plan.get('days')} 天 / {last_plan.get('km')} km，"
                f"爬升 {last_plan.get('climb')} m（引擎 {last_plan.get('engine')}）"
            ),
            "source": "fallback_from_tools",
        }
        trace.steps.append({"step": max_steps, "final": trace.final,
                            "note": "模型未主动收尾，用已有工具结果兜底"})
        return trace

    trace.steps.append({"error": f"达到最大步数 {max_steps} 仍未给出结论"})
    return trace
