"""规划报告渲染：控制台、Markdown、HTML。"""

from __future__ import annotations

import html
from typing import List

from .planner import Itinerary

KIND_LABEL = {"ride": "骑行", "rest": "休整"}
SERVICE_LABEL = {"city": "地级市", "county": "县城", "town": "乡镇", "lodge": "酒店/客栈", "camp": "露营"}


def print_console(it: Itinerary) -> None:
    b = it.brief
    print("=" * 96)
    print(f"路线规划：{b.origin} → {b.destination}｜{it.corridor_label}｜出发 {b.start_date}")
    print("=" * 96)
    for k, v in it.profile_summary.items():
        print(f"  {k}：{v}")
    print("-" * 96)
    print(f"总里程 {it.total_km:.0f} km｜累计爬升 {it.total_climb:.0f} m｜"
          f"骑行 {it.riding_days} 天｜休整 {it.rest_days} 天｜日历 {it.calendar_days} 天")
    print("-" * 96)
    print(f"{'天数':<6}{'日期':<12}{'行程':<26}{'km':<7}{'爬升':<7}{'住宿':<10}{'类型':<8}")
    for d in it.days:
        route = f"{d.start} → {d.end}" if d.kind == "ride" else f"{d.start} 休整"
        print(f"D{d.day_index:<5}{d.date:<12}{route:<26}{d.km:<7.0f}{d.climb_m:<7}{d.overnight:<10}"
              f"{KIND_LABEL[d.kind]:<8}")
    print("-" * 96)
    if it.climate_advice:
        print("分段气候与作息：")
        for a in it.climate_advice:
            print(f"  · {a}")
    if it.prep_tips:
        print("行前准备：")
        for t in it.prep_tips:
            print(f"  · {t}")
    if it.warnings:
        day_warn = [w for w in it.warnings if w.startswith("D") and "单段" in w]
        camp_warn = [w for w in it.warnings if "无固定住宿" in w and w.startswith("D")]
        others = [w for w in it.warnings if w not in day_warn and w not in camp_warn]
        print("风险提示：")
        for w in others:
            print(f"  · {w}")
        if camp_warn:
            print(f"  · {len(camp_warn)} 晚落脚点无确认住宿（逐日明细见报告）")
        if day_warn:
            print(f"  · 有 {len(day_warn)} 段单日里程超出该海拔下的能力（完整清单见报告）：")
            for w in day_warn[:3]:
                print(f"      - {w}")
            if len(day_warn) > 3:
                print(f"      - …… 另有 {len(day_warn) - 3} 段")
    if it.assumptions:
        print("系统假设：")
        for a in it.assumptions:
            print(f"  · {a}")


def render_markdown(it: Itinerary) -> str:
    b = it.brief
    lines: List[str] = []
    lines.append(f"# 骑行规划：{b.origin} → {b.destination}")
    lines.append("")
    lines.append(f"> 出发日期：{b.start_date}｜路线走廊：{it.corridor_label}")
    lines.append("")
    lines.append("## 一、你的条件与规划参数")
    lines.append("")
    lines.append("| 项目 | 数值 |")
    lines.append("| --- | --- |")
    for k, v in it.profile_summary.items():
        lines.append(f"| {k} | {v} |")
    lines.append("")
    lines.append("## 二、行程总览")
    lines.append("")
    lines.append(f"- 总里程：**{it.total_km:.0f} km**")
    lines.append(f"- 累计爬升：**{it.total_climb:.0f} m**（估算）")
    lines.append(f"- 骑行 {it.riding_days} 天 + 休整 {it.rest_days} 天 = 日历 **{it.calendar_days} 天**")
    lines.append("")
    lines.append("## 三、逐日行程")
    lines.append("")
    lines.append("| 天 | 日期 | 行程 | 距离 | 爬升 | 住宿 |")
    lines.append("| --- | --- | --- | --- | --- | --- |")
    for d in it.days:
        route = f"{d.start} → {d.end}" if d.kind == "ride" else f"{d.start}（休整）"
        stay = f"{d.overnight}（{SERVICE_LABEL.get(d.service, d.service)}）"
        lines.append(f"| D{d.day_index} | {d.date} | {route} | {d.km:.0f} km | {d.climb_m} m | {stay} |")
    lines.append("")
    if it.climate_advice:
        lines.append("## 四、分段气候与作息")
        lines.append("")
        for a in it.climate_advice:
            lines.append(f"- {a}")
        lines.append("")
    if it.prep_tips:
        lines.append("## 五、行前准备")
        lines.append("")
        for t in it.prep_tips:
            lines.append(f"- {t}")
        lines.append("")
    if it.camp_points:
        lines.append("## 六、待确认住宿点（含坐标与 OSM 链接）")
        lines.append("")
        lines.append("这些落脚点 OSM 没有检索到酒店（或检索失败），出发前请逐个确认：")
        lines.append("")
        lines.append("| 天 | 位置 | 坐标 | 查看 |")
        lines.append("| --- | --- | --- | --- |")
        for p in it.camp_points:
            link = (
                f"https://www.openstreetmap.org/?mlat={p['lat']}&mlon={p['lon']}#map=16/"
                f"{p['lat']}/{p['lon']}"
            )
            lines.append(
                f"| D{p['day']} | {p['name']} | {p['lat']:.4f}, {p['lon']:.4f} | [OSM]({link}) |"
            )
        lines.append("")
    if it.warnings:
        lines.append("## 七、风险提示")
        lines.append("")
        for w in it.warnings:
            lines.append(f"- {w}")
        lines.append("")
    if it.assumptions:
        lines.append("## 八、系统做的假设（可修改）")
        lines.append("")
        for a in it.assumptions:
            lines.append(f"- {a}")
        lines.append("")
    lines.append("## 九、方法与局限")
    lines.append("")
    lines.append("- 路线为**走廊近似**：相邻城市间按大圆距离 × 1.22 估算公路里程，不是逐米路由；")
    lines.append("- 爬升为**地形分区估算**，误差约 ±30%；")
    lines.append("- 住宿与补给按城镇等级判断，未接入实时酒店库存；")
    lines.append("- 气候按月份给出一般性建议，未接入逐日预报。")
    return "\n".join(lines)


def render_html(it: Itinerary) -> str:
    b = it.brief
    rows = []
    for d in it.days:
        route = f"{d.start} → {d.end}" if d.kind == "ride" else f"{d.start}（休整）"
        cls = "rest" if d.kind == "rest" else ""
        rows.append(
            f"<tr class='{cls}'><td>D{d.day_index}</td><td>{html.escape(d.date)}</td>"
            f"<td>{html.escape(route)}</td><td>{d.km:.0f}</td><td>{d.climb_m}</td>"
            f"<td>{html.escape(d.overnight)}<span class='muted'>{SERVICE_LABEL.get(d.service, '')}</span></td></tr>"
        )
    profile = "".join(
        f"<div class='card'><div class='num'>{html.escape(str(v))}</div>"
        f"<div class='lbl'>{html.escape(k)}</div></div>"
        for k, v in it.profile_summary.items()
    )
    warnings = "".join(f"<li>{html.escape(w)}</li>" for w in it.warnings)
    assumptions = "".join(f"<li>{html.escape(a)}</li>" for a in it.assumptions)
    climate = "".join(f"<li>{html.escape(a)}</li>" for a in it.climate_advice)
    prep = "".join(f"<li>{html.escape(t)}</li>" for t in it.prep_tips)

    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>{html.escape(b.origin)} → {html.escape(b.destination)} 骑行规划</title>
<style>
 body{{font-family:-apple-system,"Segoe UI","Microsoft YaHei",sans-serif;margin:0;padding:32px;background:#fafbfc;color:#1f2328}}
 h1{{font-size:22px;margin:0 0 4px}} .meta{{color:#6e7781;font-size:13px;margin-bottom:20px}}
 .cards{{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:22px}}
 .card{{background:#fff;border:1px solid #d8dee4;border-radius:10px;padding:12px 16px;min-width:110px}}
 .num{{font-size:18px;font-weight:600}} .lbl{{font-size:12px;color:#6e7781;margin-top:2px}}
 table{{width:100%;border-collapse:collapse;background:#fff;border:1px solid #d8dee4;border-radius:10px;overflow:hidden}}
 th{{text-align:left;font-size:12px;color:#6e7781;font-weight:500;padding:10px 12px;background:#f6f8fa;border-bottom:1px solid #d8dee4}}
 td{{padding:9px 12px;border-bottom:1px solid #eaeef2;font-size:13px}}
 tr.rest td{{background:#fbfaf3;color:#8a6d3b}} .muted{{color:#8c959f;font-size:11px;margin-left:6px}}
 h2{{font-size:15px;margin:24px 0 8px}} ul{{margin:6px 0 0 18px;color:#57606a;font-size:13px}}
</style></head><body>
<h1>{html.escape(b.origin)} → {html.escape(b.destination)} 骑行规划</h1>
<div class="meta">{html.escape(it.corridor_label)} · 出发 {html.escape(b.start_date)} · 总里程 {it.total_km:.0f} km ·
骑行 {it.riding_days} 天 + 休整 {it.rest_days} 天 = {it.calendar_days} 天</div>
<div class="cards">{profile}</div>
<table><thead><tr><th>天</th><th>日期</th><th>行程</th><th>km</th><th>爬升 m</th><th>住宿</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table>
<h2>分段气候与作息</h2><ul>{climate or '<li>无</li>'}</ul>
<h2>行前准备</h2><ul>{prep or '<li>无</li>'}</ul>
<h2>风险提示</h2><ul>{warnings or '<li>无</li>'}</ul>
<h2>系统做的假设</h2><ul>{assumptions or '<li>无</li>'}</ul>
<p class="meta">路线为走廊近似（大圆距离 ×1.22），爬升为地形估算（±30%），住宿未接实时库存，气候未接逐日预报。</p>
</body></html>"""
