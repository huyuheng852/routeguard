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
    )
    if args.lodging in ("on", True):
        args.lodging = "auto"

    _update(job_id, status="running")
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            code = cmd_plan(args)
        log = buf.getvalue()
        if code != 0:
            _update(job_id, status="failed", log=log, error="规划未完成，请检查上面的输出")
            return
        trip = _extract_result(out_dir)
        _update(job_id, status="done", log=log, trip=trip)
    except Exception as exc:  # noqa: BLE001
        _update(
            job_id,
            status="failed",
            log=buf.getvalue(),
            error=f"{type(exc).__name__}: {exc}",
        )


DAY_RE = re.compile(
    r"^D(\d+)\s+(\d{4}-\d{2}-\d{2})\s+(.+?)\s{2,}(\d+)\s+(\d+)\s+(\S+)\s+(\S+)"
)


def _extract_result(out_dir: Path) -> dict:
    """从规划产物里抽出页面要展示的结构。"""
    trip = {"summary": "", "quality": "", "days": [], "warnings": [], "files": []}
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
  :root { --fg:#1b1f24; --muted:#6b7280; --line:#e5e7eb; --accent:#0f766e; --bg:#f7f8fa; }
  * { box-sizing: border-box; }
  body { margin:0; font:15px/1.65 -apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
         color:var(--fg); background:var(--bg); }
  header { background:#fff; border-bottom:1px solid var(--line); padding:18px 24px; }
  h1 { margin:0; font-size:19px; letter-spacing:.2px; }
  h1 small { font-weight:400; color:var(--muted); font-size:13px; margin-left:8px; }
  main { max-width:980px; margin:22px auto 60px; padding:0 20px; }
  .card { background:#fff; border:1px solid var(--line); border-radius:10px; padding:18px 20px; margin-bottom:16px; }
  label { display:block; font-size:13px; color:var(--muted); margin:12px 0 6px; }
  textarea { width:100%; min-height:86px; padding:11px 12px; font:inherit; border:1px solid var(--line);
             border-radius:8px; resize:vertical; }
  input[type=text], select { padding:9px 10px; font:inherit; border:1px solid var(--line); border-radius:8px; width:100%; }
  .row { display:flex; gap:14px; flex-wrap:wrap; }
  .row > div { flex:1 1 200px; }
  .actions { display:flex; align-items:center; gap:12px; margin-top:16px; }
  button { background:var(--accent); color:#fff; border:0; border-radius:8px; padding:11px 22px;
           font:inherit; font-weight:600; cursor:pointer; }
  button:disabled { opacity:.55; cursor:default; }
  .hint { color:var(--muted); font-size:13px; }
  pre { background:#0f172a; color:#e2e8f0; padding:14px; border-radius:8px; overflow:auto;
        font-size:12.5px; line-height:1.55; max-height:340px; margin:0; }
  table { width:100%; border-collapse:collapse; font-size:14px; }
  th, td { text-align:left; padding:9px 10px; border-bottom:1px solid var(--line); }
  th { color:var(--muted); font-weight:600; font-size:13px; }
  .quality { background:#f0fdf4; border:1px solid #bbf7d0; color:#166534; padding:10px 12px;
             border-radius:8px; font-size:13.5px; margin-bottom:12px; }
  .quality.warn { background:#fff7ed; border-color:#fed7aa; color:#9a3412; }
  .warn-list { margin:10px 0 0; padding-left:18px; color:#9a3412; font-size:13.5px; }
  .files a { display:inline-block; margin:4px 10px 4px 0; color:var(--accent); text-decoration:none;
             border:1px solid var(--line); padding:7px 12px; border-radius:7px; font-size:13.5px; }
  .files a:hover { border-color:var(--accent); }
  .badge { display:inline-block; padding:2px 9px; border-radius:99px; font-size:12px; font-weight:600; }
  .badge.run { background:#fef3c7; color:#92400e; }
  .badge.ok { background:#dcfce7; color:#166534; }
  .badge.err { background:#fee2e2; color:#991b1b; }
  .spin { display:inline-block; width:13px; height:13px; border:2px solid #d1d5db; border-top-color:var(--accent);
          border-radius:50%; animation:r .8s linear infinite; vertical-align:-2px; margin-right:6px; }
  @keyframes r { to { transform:rotate(360deg); } }
</style>
</head>
<body>
<header><h1>RouteGuard <small>说一句话，生成可核验的骑行路书</small></h1></header>
<main>
  <div class="card">
    <label for="text">你想怎么骑？</label>
    <textarea id="text" placeholder="例：我的FTP为250w，体重70kg，2027年6月从武汉到成都。"></textarea>
    <div class="row">
      <div>
        <label for="via">沿途城镇（可选，逗号分隔）</label>
        <input id="via" type="text" placeholder="武汉,仙桃,荆州,宜昌,恩施,重庆,成都">
      </div>
      <div>
        <label for="start">出发日期（可选）</label>
        <input id="start" type="text" placeholder="2027-06-01">
      </div>
    </div>
    <div class="row">
      <div>
        <label for="router">路线引擎</label>
        <select id="router">
          <option value="auto">自动（OSRM 优先，失败换 BRouter）</option>
          <option value="brouter">BRouter（带高程，慢约 1 分钟）</option>
          <option value="none">不联网（直线近似，仅看里程）</option>
        </select>
      </div>
      <div>
        <label for="lodging">住宿核实</label>
        <select id="lodging">
          <option value="on">查 OSM 上的酒店/客栈</option>
          <option value="off">不查（快）</option>
        </select>
      </div>
    </div>
    <div class="actions">
      <button id="go">开始规划</button>
      <span id="status" class="hint"></span>
    </div>
    <p class="hint">首次规划约 10–30 秒（要访问路由服务）。结果会保留在本机 out/web/ 下。</p>
  </div>

  <div class="card" id="result" style="display:none">
    <div id="quality" class="quality"></div>
    <div id="summary" style="font-weight:600;margin-bottom:12px"></div>
    <table id="days"><thead><tr><th>#</th><th>当天路线</th><th>轨迹点</th></tr></thead><tbody></tbody></table>
    <div id="warns"></div>
    <h3 style="font-size:15px;margin:18px 0 8px">下载</h3>
    <div class="files" id="files"></div>
  </div>

  <div class="card" id="logcard" style="display:none">
    <h3 style="font-size:15px;margin:0 0 10px">运行日志</h3>
    <pre id="log"></pre>
  </div>
</main>
<script>
const $ = id => document.getElementById(id);
let timer = null;

async function start() {
  const text = $('text').value.trim();
  if (!text) { alert('先写一句你想怎么骑'); return; }
  $('go').disabled = true;
  $('result').style.display = 'none';
  $('status').innerHTML = '<span class="spin"></span>请求中…';

  const res = await fetch('/api/plan', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      text,
      via: $('via').value.trim(),
      start: $('start').value.trim(),
      router: $('router').value,
      lodging: $('lodging').value,
    }),
  });
  const {job_id} = await res.json();
  poll(job_id);
}

function poll(id) {
  clearInterval(timer);
  timer = setInterval(async () => {
    const r = await fetch('/api/job/' + id);
    const job = await r.json();
    $('logcard').style.display = 'block';
    $('log').textContent = job.log || '（等待输出…）';

    if (job.status === 'running' || job.status === 'queued') {
      $('status').innerHTML = '<span class="spin"></span>规划中…';
      return;
    }
    clearInterval(timer);
    $('go').disabled = false;
    if (job.status === 'done') {
      $('status').innerHTML = '<span class="badge ok">完成</span>';
      render(job);
    } else {
      $('status').innerHTML = '<span class="badge err">失败</span>';
      if (job.error) $('log').textContent += '\\n\\n' + job.error;
    }
  }, 1200);
}

function render(job) {
  const t = job.trip || {};
  const q = $('quality');
  q.textContent = t.quality || '';
  q.className = 'quality' + ((t.quality || '').includes('不可用') || (t.quality || '').includes('直线') ? ' warn' : '');
  $('summary').textContent = t.summary || '';

  const tb = $('days').querySelector('tbody');
  tb.innerHTML = '';
  (t.days || []).forEach((d, i) => {
    const tr = document.createElement('tr');
    tr.innerHTML = `<td>${i + 1}</td><td>${d.name}</td><td>${d.points}</td>`;
    tb.appendChild(tr);
  });

  const w = $('warns');
  w.innerHTML = (t.warnings && t.warnings.length)
    ? '<ul class="warn-list">' + t.warnings.map(x => `<li>${x}</li>`).join('') + '</ul>' : '';

  const f = $('files');
  f.innerHTML = '';
  (t.files || []).forEach(file => {
    const a = document.createElement('a');
    a.href = `/files/${job.id}/${encodeURIComponent(file.name)}`;
    a.textContent = file.name === '全部打包.zip'
      ? `全部打包.zip（含 ${t.daily_count} 个逐日 GPX）`
      : file.name;
    f.appendChild(a);
  });
  $('result').style.display = 'block';
  $('result').scrollIntoView({behavior: 'smooth', block: 'start'});
}

$('go').onclick = start;
$('text').addEventListener('keydown', e => {
  if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) start();
});
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
