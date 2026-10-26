"""澄清对话：信息不足或意图有歧义时，问用户，而不是直接报错退出。

原则
----
* 只问**必要**的问题（起点、终点、环线方向），一次问一个，带示例；
* 用户回车跳过就用默认值，并把这个默认写进报告，不能悄悄替他决定；
* 非交互环境（管道、CI）不提问，直接给出可复制的修正命令。
"""

from __future__ import annotations

from typing import Callable, Optional, Tuple

Ask = Callable[[str], str]


def _safe_ask(ask: Ask, prompt: str) -> str:
    try:
        return (ask(prompt) or "").strip()
    except (EOFError, KeyboardInterrupt):
        return ""


def clarify_plan(brief, issues, ask: Optional[Ask] = None) -> Tuple[object, str, list]:
    """补全缺失信息。返回 (brief, via, 说明列表)。

    ask 为 None 时表示非交互，只做能自动推断的补全。
    """
    notes: list[str] = []
    via = getattr(brief, "via_hint", "") or ""

    missing_ends = any("起点和终点" in i for i in issues)
    if missing_ends and ask is not None:
        origin = _safe_ask(ask, "这次骑行从哪里出发？（例：海口）")
        dest = _safe_ask(ask, "终点是哪里？（环线就和起点填一样）")
        if origin:
            brief.origin = origin
            notes.append(f"起点由对话确认：{origin}")
        if dest:
            brief.destination = dest
            notes.append(f"终点由对话确认：{dest}")

    # 环线：起终点同名，但没给途经点 → 问一句方向
    if (
        ask is not None
        and brief.origin
        and brief.destination
        and brief.origin == brief.destination
        and not via
    ):
        ans = _safe_ask(
            ask,
            "你给的是环线（起点=终点）。沿途想经过哪些地方？逗号分隔，直接回车我按最短环线生成：",
        )
        if ans:
            via = ans
            notes.append(f"途经点由对话确认：{ans}")
        else:
            notes.append("未指定途经点，按起终点直接生成（可能不是你想走的那条线）")

    return brief, via, notes


def non_interactive_hint(brief, issues) -> list:
    """非交互时的补救建议，用户可以直接复制执行。"""
    tips = []
    if any("起点和终点" in i for i in issues):
        tips.append('补一句「从 A 到 B」，例如：--text "...从海口到三亚"')
        tips.append('或直接给途经点：--via "海口,文昌,三亚"')
    if brief.origin and brief.destination and brief.origin == brief.destination:
        tips.append("起终点相同会被当作环线；想走特定线路请用 --via 指定沿途城镇")
    return tips
