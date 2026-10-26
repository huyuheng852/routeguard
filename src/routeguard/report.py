"""报告渲染：Markdown 与自包含 HTML（无外部依赖、可直接双击打开）。"""

from __future__ import annotations

import html
from typing import Any, Dict, Sequence

from .claims import ClaimVerdict, summarize_verdicts

STATUS_STYLE = {
    "supported": ("支持", "#1a7f37", "#dafbe1"),
    "contradicted": ("矛盾", "#b42318", "#ffebe9"),
    "unverified": ("未核实", "#6e7781", "#f6f8fa"),
    "conflict": ("冲突", "#9a6700", "#fff8c5"),
    "subjective": ("主观", "#6639ba", "#fbefff"),
}


def render_html(
    verdicts: Sequence[ClaimVerdict],
    title: str,
    meta: Dict[str, Any] | None = None,
) -> str:
    meta = meta or {}
    s = summarize_verdicts(verdicts)

    rows = []
    for v in verdicts:
        label, color, bg = STATUS_STYLE.get(v.status, (v.status, "#333", "#eee"))
        ev_html = "".join(
            f"<li><code>{html.escape(ev.indicator)}</code> = {html.escape(str(ev.value))}{html.escape(ev.unit)}"
            f" <span class='muted'>· {html.escape(ev.source)} · {ev.valid_from}~{ev.valid_to}"
            f" · q={ev.quality:.2f}</span></li>"
            for ev in v.evidences
        )
        rows.append(
            f"""
      <tr>
        <td class="seg">{html.escape(v.claim.segment_id)}</td>
        <td>
          <div class="claim">{html.escape(v.claim.text)}</div>
          <div class="note">{html.escape(v.note)}</div>
          {f'<ul class="ev">{ev_html}</ul>' if ev_html else ''}
        </td>
        <td><span class="badge" style="color:{color};background:{bg};border-color:{color}22">{label}</span></td>
      </tr>"""
        )

    cards = "".join(
        f"<div class='card'><div class='num'>{value}</div><div class='lbl'>{name}</div></div>"
        for name, value in (
            ("断言总数", s["total"]),
            ("已核验", s["verified"]),
            ("覆盖率", f"{s['coverage'] * 100:.0f}%"),
            ("未核实率", f"{s['unverified_rate'] * 100:.0f}%"),
            ("矛盾", s["counts"].get("contradicted", 0)),
        )
    )

    meta_line = " · ".join(f"{k}: {v}" for k, v in meta.items() if v not in (None, ""))

    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>{html.escape(title)}</title>
<style>
  :root {{ color-scheme: light; }}
  body {{ font-family: -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif;
         margin: 0; padding: 32px; background: #fafbfc; color: #1f2328; }}
  h1 {{ font-size: 22px; margin: 0 0 6px; }}
  .meta {{ color: #6e7781; font-size: 13px; margin-bottom: 20px; }}
  .cards {{ display: flex; gap: 12px; margin-bottom: 22px; flex-wrap: wrap; }}
  .card {{ background: #fff; border: 1px solid #d8dee4; border-radius: 10px;
           padding: 12px 18px; min-width: 96px; }}
  .num {{ font-size: 22px; font-weight: 600; }}
  .lbl {{ font-size: 12px; color: #6e7781; margin-top: 2px; }}
  table {{ width: 100%; border-collapse: collapse; background: #fff;
           border: 1px solid #d8dee4; border-radius: 10px; overflow: hidden; }}
  th {{ text-align: left; font-size: 12px; color: #6e7781; font-weight: 500;
        padding: 10px 14px; background: #f6f8fa; border-bottom: 1px solid #d8dee4; }}
  td {{ padding: 12px 14px; border-bottom: 1px solid #eaeef2; vertical-align: top; font-size: 14px; }}
  tr:last-child td {{ border-bottom: none; }}
  .seg {{ font-family: ui-monospace, Consolas, monospace; color: #57606a; width: 72px; }}
  .claim {{ font-weight: 600; }}
  .note {{ color: #57606a; font-size: 13px; margin-top: 4px; }}
  .ev {{ margin: 6px 0 0 0; padding-left: 18px; color: #57606a; font-size: 12px; }}
  .muted {{ color: #8c959f; }}
  .badge {{ display: inline-block; padding: 3px 10px; border-radius: 999px;
            font-size: 12px; border: 1px solid; white-space: nowrap; }}
</style></head>
<body>
  <h1>{html.escape(title)}</h1>
  <div class="meta">{html.escape(meta_line)}</div>
  <div class="cards">{cards}</div>
  <table>
    <thead><tr><th>路段</th><th>断言与依据</th><th>结论</th></tr></thead>
    <tbody>{''.join(rows)}</tbody>
  </table>
  <p class="meta" style="margin-top:18px">
    说明：未核实 ≠ 错误，只表示当前证据源无法支持或反驳；主观描述不参与核验。
  </p>
</body></html>"""
