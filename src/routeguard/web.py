"""本地网页版：不用命令行也能规划路线。

启动：`python -m routeguard web`，浏览器打开 http://127.0.0.1:8765

设计
----
* 只用标准库（http.server + 线程），不引入 Flask/FastAPI；
* 规划在后台线程跑，页面轮询进度，避免浏览器转圈到超时；
* 每次规划输出到 `out/web/<job_id>/`，页面直接给下载链接；
* 全程只监听 127.0.0.1，不对外网暴露。
"""

from __future__ import annotations

import io
import json
import re
import shutil
import threading
import time
import uuid
import zipfile
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Dict, Optional

BUILD_TAG = "1026-1901"
HOST = "127.0.0.1"
PORT = 8765

JOBS: Dict[str, dict] = {}
LOCK = threading.Lock()
ROOT_OUT = Path.cwd() / "out" / "web"


def _new_job() -> str:
    job_id = uuid.uuid4().hex[:12]
    with LOCK:
        JOBS[job_id] = {
            "id": job_id,
            "status": "queued",
            "log": "",
            "started": time.time(),
            "dir": str(ROOT_OUT / job_id),
            "trip": None,
            "error": "",
        }
    return job_id


def _update(job_id: str, **kw) -> None:
    with LOCK:
        JOBS[job_id].update(kw)


def _snapshot(job_id: str) -> Optional[dict]:
    with LOCK:
        job = JOBS.get(job_id)
        return dict(job) if job else None


def _run_plan(job_id: str, payload: dict) -> None:
    from .cli import cmd_plan

    out_dir = Path(ROOT_OUT / job_id)
    out_dir.mkdir(parents=True, exist_ok=True)

    use_agent = bool(payload.get("agent"))
    args = SimpleNamespace(
        text=payload.get("text", ""),
        corridor=None,
        start=payload.get("start") or None,
        daily=payload.get("daily") or None,
        out=str(out_dir),
        gpx_out=None,
        router=payload.get("router", "auto"),
        lodging=payload.get("lodging", "on"),
        no_daily_gpx=False,
        spacing=120.0,
        no_ask=True,
        crew=bool(payload.get("crew")),
        cities=False,
        no_llm=bool(payload.get("no_llm")),
        llm=False,
        via=payload.get("via") or None,
        lodging_provider=payload.get("lodging_provider", "auto"),
    )
    if args.lodging in ("on", True):
        args.lodging = "auto"

    _update(job_id, status="running")
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            if use_agent:
                from .cli import cmd_agent

                code = cmd_agent(args)
            else:
                code = cmd_plan(args)
        log = buf.getvalue()
        if code != 0:
            _update(
                job_id,
                status="failed",
                log=log + "\n\n===== 规划未完成（退出码 %s）=====" % code,
                error="规划未完成，请检查日志",
            )
            return
        trip = _extract_result(out_dir, log)
        _update(job_id, status="done", log=log, trip=trip)
    except Exception as exc:  # noqa: BLE001
        import traceback

        tb = traceback.format_exc().strip()
        log_text = buf.getvalue() + "\n\n===== 失败详情 =====\n" + tb
        _update(
            job_id,
            status="failed",
            log=log_text,
            error=f"{type(exc).__name__}: {exc}",
        )


AGENT_RE = re.compile(r"^\s*\[(OK|WARN|FAIL|SKIP)\]\s+(\w+)\s+(.*)$")
TOOL_RE = re.compile(r"^\s*\[\d+\]\s*(.*?)\s*→\s*(\w+)\s*\((OK|FAIL)\)")

DAY_RE = re.compile(
    r"^D(\d+)\s+(\d{4}-\d{2}-\d{2})\s+(.+?)\s{2,}(\d+)\s+(\d+)\s+(\S+)\s+(\S+)"
)


def _extract_result(out_dir: Path, log: str = "") -> dict:
    """从规划产物里抽出页面要展示的结构。"""
    trip = {"summary": "", "quality": "", "days": [], "warnings": [], "files": [], "_log": log}
    md = out_dir / "plan.md"
    if md.exists():
        text = md.read_text(encoding="utf-8")
        for line in text.splitlines():
            if line.startswith("- 总里程"):
                trip["summary"] = line.lstrip("- ").replace("**", "")
            elif line.startswith("- 路线数据"):
                trip["quality"] = line.lstrip("- ")
        in_warn = False
        for line in text.splitlines():
            if line.startswith("## 七") or line.startswith("## 八"):
                in_warn = line.startswith("## 七")
                continue
            if in_warn and line.startswith("- "):
                trip["warnings"].append(line[2:].strip())

    import xml.etree.ElementTree as ET

    gpx = out_dir / "route.gpx"
    if gpx.exists():
        try:
            root = ET.parse(str(gpx)).getroot()
            ns = {"g": "http://www.topografix.com/GPX/1/1"}
            for trk in root.findall("g:trk", ns):
                name = (trk.findtext("g:name", "", ns) or "").strip()
                pts = trk.findall(".//g:trkpt", ns)
                trip["days"].append({"name": name, "points": len(pts)})
        except ET.ParseError:
            pass

    for f in sorted(out_dir.glob("*.gpx")) + sorted(out_dir.glob("plan.*")):
        trip["files"].append({"name": f.name, "size": f.stat().st_size})
    daily = sorted((out_dir / "daily").glob("*.gpx")) if (out_dir / "daily").exists() else []
    trip["daily_count"] = len(daily)
    trip["files"].append({"name": "全部打包.zip", "size": 0})

    # agent 行形如：  [OK  ] geo      2 个锚点 → 3 个航点
    agents = []
    for line in trip.pop("_log", "").splitlines():
        m = AGENT_RE.match(line)
        if m:
            agents.append({"status": m.group(1), "name": m.group(2), "summary": m.group(3).strip()})
    trip["agents"] = agents
    return trip


def _zip_dir(src: Path, dest: Path) -> None:
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
        for item in src.rglob("*"):
            if item.is_file() and item != dest:
                zf.write(item, item.relative_to(src))


INDEX_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>RouteGuard · 骑行路书规划</title>
<style>
 :root{--fg:#111827;--muted:#6b7280;--line:#e5e7eb;--accent:#0f766e;--bg:#f6f8fa;--ok:#16a34a;--warn:#d97706;--err:#dc2626;--skip:#9ca3af}
 *{box-sizing:border-box}
 body{margin:0;font:15px/1.65 -apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;color:var(--fg);background:var(--bg)}
 header{background:linear-gradient(135deg,#0f766e,#134e4a);color:#fff;padding:24px}
 header h1{margin:0;font-size:20px} header p{margin:6px 0 0;opacity:.85;font-size:13.5px}
 main{max-width:1040px;margin:22px auto 80px;padding:0 18px}
 .card{background:#fff;border:1px solid var(--line);border-radius:12px;padding:20px 22px;margin-bottom:16px}
 label{display:block;font-size:13px;color:var(--muted);margin:14px 0 6px;font-weight:600}
 textarea{width:100%;min-height:90px;padding:12px 14px;font:inherit;border:1px solid var(--line);border-radius:10px;resize:vertical}
 textarea:focus,input:focus,select:focus{outline:none;border-color:#0d9488;box-shadow:0 0 0 3px rgba(13,148,136,.12)}
 input[type=text],select{padding:10px 12px;font:inherit;border:1px solid var(--line);border-radius:10px;width:100%;background:#fff}
 .row{display:flex;gap:14px;flex-wrap:wrap} .row>div{flex:1 1 210px}
 .actions{display:flex;align-items:center;gap:14px;margin-top:18px;flex-wrap:wrap}
 button{background:var(--accent);color:#fff;border:0;border-radius:10px;padding:12px 26px;font:inherit;font-weight:600;cursor:pointer}
 button:hover:not(:disabled){background:#115e56} button:disabled{opacity:.5;cursor:default}
 .hint{color:var(--muted);font-size:13px;margin:10px 0 0}
 kbd{background:#eef2f7;border:1px solid var(--line);border-radius:5px;padding:1px 5px;font-size:12px}
 .stages{display:flex;gap:8px;margin-top:16px;flex-wrap:wrap}
 .stage{flex:1 1 120px;font-size:12px;color:var(--muted);border-top:3px solid var(--line);padding-top:7px}
 .stage.active{color:var(--accent);border-top-color:var(--accent);font-weight:600}
 .stage.done{color:var(--ok);border-top-color:var(--ok)}
 .metrics{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:12px;margin-bottom:16px}
 .metric{background:#f9fafb;border:1px solid var(--line);border-radius:10px;padding:12px 14px}
 .metric .k{font-size:12px;color:var(--muted)} .metric .v{font-size:20px;font-weight:700;margin-top:2px}
 .agents{display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:12px}
 .agent{border:1px solid var(--line);border-radius:10px;padding:12px 14px}
 .agent .n{font-weight:700;font-size:14px} .agent .d{font-size:12px;color:var(--muted);margin-top:4px}
 .agent .s{font-size:13px;margin-top:6px;color:#374151}
 .dot{width:8px;height:8px;border-radius:50%;display:inline-block;margin-right:6px}
 .dot.OK{background:var(--ok)} .dot.WARN{background:var(--warn)} .dot.FAIL{background:var(--err)} .dot.SKIP{background:var(--skip)}
 .badge{display:inline-block;padding:2px 9px;border-radius:99px;font-size:12px;font-weight:600}
 .badge.run{background:#fef3c7;color:#92400e} .badge.ok{background:#dcfce7;color:#166534} .badge.err{background:#fee2e2;color:#991b1b}
 h3.sec{font-size:15px;margin:22px 0 10px}
 table{width:100%;border-collapse:collapse;font-size:14px}
 th,td{text-align:left;padding:9px 10px;border-bottom:1px solid var(--line)}
 th{color:var(--muted);font-weight:600;font-size:12.5px}
 tbody tr:hover{background:#f9fafb}
 .quality{background:#f0fdf4;border:1px solid #bbf7d0;color:#166534;padding:11px 13px;border-radius:10px;font-size:13.5px;margin-bottom:14px}
 .quality.warn{background:#fff7ed;border-color:#fed7aa;color:#9a3412}
 .warn-list{margin:12px 0 0;padding-left:18px;color:#9a3412;font-size:13.5px}
 .files{display:flex;flex-wrap:wrap;gap:10px;margin-top:8px}
 .files a{color:var(--accent);text-decoration:none;border:1px solid var(--line);padding:8px 14px;border-radius:8px;font-size:13.5px}
 .files a:hover{border-color:var(--accent);background:#f0fdfa}
 .logbox{background:#0b1220;border-radius:10px;padding:12px 14px;max-height:300px;overflow:auto;font:12.5px/1.6 ui-monospace,Consolas,monospace;color:#cbd5e1}
 .logbox .l{white-space:pre-wrap;word-break:break-word}
 .logbox .ok{color:#86efac} .logbox .warn{color:#fcd34d} .logbox .err{color:#fca5a5} .logbox .dim{color:#64748b}
 .toast{position:fixed;right:20px;bottom:20px;background:#111827;color:#fff;padding:12px 16px;border-radius:10px;font-size:13.5px;opacity:0;transition:.25s}
 .toast.show{opacity:1}
 @media(max-width:640px){main{padding:0 12px}.card{padding:16px}}
</style>
</head>
<body>
<header><h1>RouteGuard <span style="font-size:13px;opacity:.7">BUILD " + BUILD_TAG + "</span></h1><p>说一句话，生成可核验的骑行路书 · 真实路网 · 逐日行程 · GPX 导出</p></header>
<main>
 <div class="card">
  <label for="text">你想怎么骑？</label>
  <textarea id="text" placeholder="例：我的FTP为250w，体重70kg，2027年4月从上海到福州，沿海骑。"></textarea>
  <div class="row">
   <div><label for="via">沿途城镇（可选）</label><input id="via" type="text" placeholder="上海,宁波,温州,福州"></div>
   <div><label for="start">出发日期（可选）</label><input id="start" type="text" placeholder="2027-04-01"></div>
  </div>
  <div class="row">
   <div><label for="router">路线引擎</label>
    <select id="router">
     <option value="auto">自动 · BRouter 优先（约 1 分钟）</option>
     <option value="brouter">BRouter · 带高程</option>
     <option value="none">不联网 · 直线近似（秒出）</option>
    </select></div>
   <div><label for="lodging">住宿核实</label>
    <select id="lodging"><option value="on">查酒店</option><option value="off">不查（更快）</option></select></div>
   <div><label for="mode">规划方式</label>
    <select id="mode"><option value="0">标准流程 · 快且稳</option><option value="1">Agent 模式 · 实验</option></select></div>
  </div>
  <div class="actions"><button id="go">开始规划</button><span id="status" class="hint"></span></div>
  <p class="hint">快捷键 <kbd>Ctrl</kbd>+<kbd>Enter</kbd> 提交。</p>
  <div class="stages" id="stages">
   <div class="stage">1 · 理解需求</div><div class="stage">2 · 解析地名</div><div class="stage">3 · 请求路线</div>
   <div class="stage">4 · 按日切分</div><div class="stage">5 · 住宿核实</div><div class="stage">6 · 生成文件</div>
  </div>
 </div>

 <div class="card" id="result" style="display:none">
  <div id="quality" class="quality"></div>
  <div class="metrics" id="metrics"></div>
  <h3 class="sec">子 Agent 执行情况</h3>
  <div class="agents" id="agents"></div>
  <h3 class="sec">逐日行程</h3>
  <table id="days"><thead><tr><th>#</th><th>当天路线</th><th>轨迹点数</th></tr></thead><tbody></tbody></table>
  <div id="warns"></div>
  <h3 class="sec">下载</h3><div class="files" id="files"></div>
 </div>

 <div class="card" id="logcard" style="display:none">
  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px">
   <h3 class="sec" style="margin:0">运行日志</h3>
   <label style="margin:0;font-weight:400"><input type="checkbox" id="autoscroll" checked> 自动滚动</label>
  </div>
  <div class="logbox" id="log"></div>
 </div>
</main>
<div class="toast" id="toast"></div>
<script>
var ROLE = {geo:'地名解析与航点生成',route:'真实路网路由与海拔回填',terrain:'爬升、最高点、最陡段',
 weather:'按月份与气候区给天气风险',lodging:'每晚落脚点住宿核查',supply:'沿途补给点抽样',
 safety:'硬约束冲突检查',critic:'行程内部一致性自检'};
var NL = String.fromCharCode(10);
var timer = null;

function $(id){ return document.getElementById(id); }
function toast(m){ var t=$('toast'); t.textContent=m; t.className='toast show'; setTimeout(function(){t.className='toast';},2200); }
function setStage(i){ var els=document.querySelectorAll('.stage');
  for(var k=0;k<els.length;k++){ els[k].className='stage'+(k<i?' done':(k===i?' active':'')); } }

function renderLog(text){
  var box=$('log'); var atBottom=box.scrollTop+box.clientHeight>=box.scrollHeight-20;
  var lines=(text||'（等待输出…）').split(NL); var html='';
  for(var i=0;i<lines.length;i++){
    var l=lines[i]; var c='';
    if(l.indexOf('[OK')>=0||l.indexOf('完成')>=0) c='ok';
    else if(l.indexOf('[WARN')>=0||l.indexOf('提示')>=0||l.indexOf('警告')>=0) c='warn';
    else if(l.indexOf('FAIL')>=0||l.indexOf('错误')>=0||l.indexOf('失败')>=0||l.indexOf('Traceback')>=0) c='err';
    else if(l.indexOf('  -')===0||l.trim()==='') c='dim';
    html += '<div class="l '+c+'">'+l.replace(/</g,'&lt;')+'</div>';
  }
  box.innerHTML=html;
  if($('autoscroll').checked && atBottom) box.scrollTop=box.scrollHeight;
}

function stageFromLog(text){
  var order=['理解','地名','请求整条','切成','住宿','规划报告']; var idx=0;
  for(var i=0;i<order.length;i++){ if(text && text.indexOf(order[i])>=0) idx=i+1; }
  return idx;
}

function start(){
  var text=$('text').value.trim();
  if(!text){ toast('先写一句你想怎么骑'); $('text').focus(); return; }
  $('go').disabled=true;
  $('result').style.display='none';
  $('logcard').style.display='none';
  $('status').innerHTML='<span class="badge run">提交中…</span>';
  setStage(0);
  fetch('/api/plan',{method:'POST',headers:{'Content-Type':'application/json'},
    body: JSON.stringify({text:text, via:$('via').value.trim(), start:$('start').value.trim(),
      router:$('router').value, lodging:$('lodging').value, agent:$('mode').value==='1'})})
   .then(function(r){ return r.json(); })
   .then(function(d){ poll(d.job_id); })
   .catch(function(e){ $('go').disabled=false; toast('请求失败：'+e); });
}

function poll(id){
  clearInterval(timer);
  timer=setInterval(function(){
    fetch('/api/job/'+id).then(function(r){return r.json();}).then(function(job){
      $('logcard').style.display='block';
      renderLog(job.log);
      setStage(stageFromLog(job.log||''));
      if(job.status==='running'||job.status==='queued'){
        $('status').innerHTML='<span class="badge run">规划中…</span>'; return;
      }
      clearInterval(timer); $('go').disabled=false;
      if(job.status==='done'){ $('status').innerHTML='<span class="badge ok">完成</span>'; setStage(6); render(job); }
      else{ $('status').innerHTML='<span class="badge err">失败</span>'; toast(job.error||'规划失败'); }
    });
  },1200);
}

function metric(k,v){ return '<div class="metric"><div class="k">'+k+'</div><div class="v">'+v+'</div></div>'; }

function render(job){
  var t=job.trip||{};
  var q=$('quality'); q.textContent=t.quality||'（无路线数据说明）';
  q.className='quality'+((t.quality||'').indexOf('不可用')>=0||(t.quality||'').indexOf('直线')>=0?' warn':'');
  var s=t.summary||'';
  var km=(s.match(/([0-9.]+) km/)||[])[1];
  var climb=(s.match(/爬升 ([0-9.]+)/)||[])[1];
  var days=(t.days||[]).length;
  $('metrics').innerHTML=metric('总里程',km?km+' km':'—')+metric('累计爬升',climb?climb+' m':'—')
    +metric('骑行天数',days||'—')+metric('逐日 GPX',(t.daily_count||0)+' 个');

  var box=$('agents'), ag=t.agents||[];
  if(ag.length){
    var h='';
    for(var i=0;i<ag.length;i++){
      h+='<div class="agent"><div><span class="dot '+ag[i].status+'"></span><span class="n">'+ag[i].name+'</span></div>'
       +'<div class="d">'+(ROLE[ag[i].name]||'执行子任务')+'</div><div class="s">'+ag[i].summary+'</div></div>';
    }
    box.innerHTML=h;
  } else {
    box.innerHTML='<div class="agent"><div><span class="dot OK"></span><span class="n">pipeline</span></div>'
      +'<div class="d">标准流程（未启用多 agent）</div><div class="s">'+s+'</div></div>';
  }

  var tb=$('days').getElementsByTagName('tbody')[0]; tb.innerHTML='';
  var ds=t.days||[];
  for(var i=0;i<ds.length;i++){
    var tr=document.createElement('tr');
    tr.innerHTML='<td>'+(i+1)+'</td><td>'+ds[i].name+'</td><td>'+ds[i].points+'</td>';
    tb.appendChild(tr);
  }
  var w=$('warns');
  if((t.warnings||[]).length){
    var wh='<ul class="warn-list">';
    for(var j=0;j<t.warnings.length;j++){ wh+='<li>'+t.warnings[j]+'</li>'; }
    w.innerHTML=wh+'</ul>';
  } else { w.innerHTML=''; }

  var f=$('files'); f.innerHTML='';
  var fs=t.files||[];
  for(var k=0;k<fs.length;k++){
    var a=document.createElement('a');
    a.href='/files/'+job.id+'/'+encodeURIComponent(fs[k].name);
    a.textContent=fs[k].name==='全部打包.zip'?('全部打包.zip（含 '+(t.daily_count||0)+' 个逐日 GPX）'):fs[k].name;
    f.appendChild(a);
  }
  $('result').style.display='block';
  $('result').scrollIntoView({behavior:'smooth',block:'start'});
}

$('go').onclick=start;
$('text').addEventListener('keydown', function(e){ if(e.key==='Enter'&&(e.ctrlKey||e.metaKey)) start(); });
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    server_version = "RouteGuardWeb/0.9"

    def log_message(self, fmt, *args):  # 静音默认访问日志
        pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            self._send(200, INDEX_HTML.encode("utf-8"), "text/html; charset=utf-8")
            return
        if path.startswith("/api/job/"):
            job = _snapshot(path.rsplit("/", 1)[-1])
            self._json(job or {"status": "missing"}, 200 if job else 404)
            return
        if path.startswith("/files/"):
            self._serve_file(path)
            return
        self._json({"error": "not found"}, 404)

    def _serve_file(self, path: str) -> None:
        parts = path.split("/")
        if len(parts) < 4:
            self._json({"error": "bad path"}, 400)
            return
        job_id, name = parts[2], parts[3]
        from urllib.parse import unquote

        name = unquote(name)
        job_dir = ROOT_OUT / job_id
        if not job_dir.exists():
            self._json({"error": "job gone"}, 404)
            return
        if name == "全部打包.zip":
            target = job_dir / "routeguard-results.zip"
            _zip_dir(job_dir, target)
        else:
            target = job_dir / name
            if not target.exists():
                target = job_dir / "daily" / name
        if not target.exists() or not target.is_file():
            self._json({"error": "file not found"}, 404)
            return
        ctype = "application/octet-stream"
        if target.suffix == ".gpx":
            ctype = "application/gpx+xml; charset=utf-8"
        elif target.suffix == ".md":
            ctype = "text/markdown; charset=utf-8"
        elif target.suffix == ".html":
            ctype = "text/html; charset=utf-8"
        data = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header(
            "Content-Disposition", f"attachment; filename*=UTF-8''{name}"
        )
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/api/plan":
            self._json({"error": "not found"}, 404)
            return
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            self._json({"error": "bad json"}, 400)
            return
        if not (payload.get("text") or "").strip():
            self._json({"error": "缺少需求描述"}, 400)
            return

        job_id = _new_job()
        threading.Thread(target=_run_plan, args=(job_id, payload), daemon=True).start()
        self._json({"job_id": job_id})


def serve(host: str = HOST, port: int = PORT, open_browser: bool = True) -> int:
    ROOT_OUT.mkdir(parents=True, exist_ok=True)
    url = f"http://{host}:{port}"
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"RouteGuard 网页版已启动：{url}")
    print("按 Ctrl+C 停止。")
    if open_browser:
        try:
            import webbrowser

            threading.Timer(0.8, lambda: webbrowser.open(url)).start()
        except Exception:  # noqa: BLE001
            pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        server.server_close()
    return 0
