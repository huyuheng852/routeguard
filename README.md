# RouteGuard · 可核验的骑行路书

[![tests](https://github.com/huyuheng852/routeguard/actions/workflows/ci.yml/badge.svg)](https://github.com/huyuheng852/routeguard/actions/workflows/ci.yml)
[![python](https://img.shields.io/badge/python-3.10%2B-blue)](#环境要求)
[![license](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![deps](https://img.shields.io/badge/dependencies-stdlib%20only-success)](#环境要求)

> 骑友最大的痛点不是"没有路书"，而是**路书不可信**：
> 说"全程平路"实际爬升 900 米，标的补给点三个月前就关门了。
> 而 AI 写的路书更糟——它会把空白补成"沿途风景优美、补给充足"。

RouteGuard 做两件事：

| 能力 | 命令 | 说明 |
|---|---|---|
| **规划路线** | `web` / `plan` | 一句话（FTP、体重、起终点、日期）→ 多日行程 + 真实路网 GPX |
| **核验路书** | `audit` | 把路书拆成原子断言，逐条判 **支持 / 矛盾 / 未核实 / 主观** |

---

## 目录

- [30 秒上手](#30-秒上手)
- [网页版（推荐）](#网页版推荐)
- [命令行](#命令行)
- [配置 AI（7 家厂商 + 本地模型）](#配置-ai7-家厂商--本地模型)
- [住宿数据源（国内免费）](#住宿数据源国内免费)
- [输出文件](#输出文件)
- [常见问题](#常见问题)
- [已知限制](#已知限制)
- [项目结构](#项目结构)
- [许可与数据来源](#许可与数据来源)

---

## 30 秒上手

**Windows 用户**：双击 `start.bat`，浏览器自动打开 <http://127.0.0.1:8765>。

**其他方式**：

```powershell
cd routeguard
$env:PYTHONPATH = "$PWD\src"     # Windows PowerShell
python -m routeguard web          # 启动网页版
```

> 不需要 API Key，不需要联网也能跑（只是路线会退化成直线近似）。
> 不需要 `pip install`，全部用标准库。

---

## 网页版（推荐）

```powershell
python -m routeguard web          # 或双击 start.bat
```

打开 <http://127.0.0.1:8765>：

| 区域 | 内容 |
|---|---|
| 输入 | 一句话写需求：`FTP 250w，体重 70kg，2027年4月从上海到福州` |
| 可选 | 沿途城镇、出发日期 |
| 选择 | 路线引擎、住宿数据源 |
| 结果 | 逐日行程表、风险提示 |
| 下载 | `plan.md`、`plan.html`、`route.gpx`、逐日 GPX、**全部打包.zip** |
| 日志 | 实时滚动，卡在哪一步一眼可见 |

只监听 `127.0.0.1`，不对外网开放。换端口：`--port 9000`。

**为什么不用命令行**：规划要访问路由服务，首次约 1 分钟。网页版把任务丢到后台线程、页面轮询进度，不会让浏览器转圈到超时。

---

## 命令行

### `plan` —— 规划路线

```powershell
python -m routeguard plan --text "我的FTP为250w，体重70kg，2027年4月从上海到福州"
```

| 参数 | 默认 | 说明 |
|---|---|---|
| `--text` | 必填 | 自然语言需求 |
| `--via` | 无 | 指定途经城镇：`"上海,宁波,温州,福州"`；也可写坐标 `名称@纬度,经度`，点之间用分号 |
| `--router` | `auto` | `auto`=BRouter→OSRM→直线；`brouter`=强制带高程；`none`=不联网（直线） |
| `--lodging` | `auto` | 是否核实住宿；`off` 关闭（快很多） |
| `--lodging-provider` | `auto` | `auto`=高德→天地图→OSM；也可强制 `amap` / `tianditu` / `osm` |
| `--start` | 从文本解析 | 覆盖出发日期 `2027-06-01` |
| `--out` | `out/plan` | 输出目录 |
| `--crew` | 关 | 多 agent 编排模式（见下） |
| `--no-ask` | 关 | 缺信息时不追问，直接报错 |

**它会自己处理这些**：

- 只有起终点也能规划（一次请求整条路线，再按日切分）
- 环线（起终点同名）需要用 `--via` 指明方向
- 落脚点按行政等级挑：**地级市 > 县级市 > 乡镇**，挑不到就标"待核实"
- 海拔 >3000 m 自动降低日里程，首次上高原插适应日
- 缺起点/终点/日期时逐条追问，不猜

### `audit` —— 核验路书

```powershell
python -m routeguard audit --no-osm        # 核验内置示范路书，纯离线
python -m routeguard audit --guide 我的路书.md --gpx 我的轨迹.gpx --when 2027-06-01
```

输出逐条判定 + 证据来源 + 未核实原因，同时生成 `route_audit.md` 与自包含 `route_audit.html`。

### `config` / `selftest`

```powershell
python -m routeguard config --check   # 检查 AI 配置并发一次真实请求
python -m routeguard selftest         # 不联网跑一遍核心链路
```

### 多 agent 编排（`--crew`）

中心 agent 只负责"理解 → 派活 → 汇总"，实际工作分给子 agent：

| Agent | 职责 |
|---|---|
| `geo` | 地名解析、航点生成 |
| `route` | 真实路由与海拔回填 |
| `terrain` | 爬升、最高点、最陡段 |
| `weather` | 按月份与气候区给天气风险 |
| `lodging` | 每晚落脚点住宿核查 |
| `supply` | 沿途补给点抽样 |
| `safety` | 硬约束冲突（超长日、高海拔） |
| `critic` | 自检行程内部一致性 |

任何子 agent 抛异常都记为 `FAIL`，依赖它的下游自动 `SKIP`，编排继续跑完。

---

## 配置 AI（7 家厂商 + 本地模型）

AI 只用于**理解人话**（抽取断言、解析需求），路线计算、核验裁决始终是确定性代码。
**没配 AI 也能用**，会退回规则引擎。

| 厂商 | 环境变量 | 默认模型 |
|---|---|---|
| **DeepSeek** | `DEEPSEEK_API_KEY` | `deepseek-chat` |
| OpenAI | `OPENAI_API_KEY` | `gpt-4o-mini` |
| 月之暗面 Kimi | `MOONSHOT_API_KEY` | `moonshot-v1-8k` |
| 通义千问 | `DASHSCOPE_API_KEY` | `qwen-plus` |
| 智谱 GLM | `ZHIPUAI_API_KEY` | `glm-4-flash` |
| 硅基流动 | `SILICONFLOW_API_KEY` | `Qwen/Qwen2.5-7B-Instruct` |
| Ollama 本地 | `OLLAMA_HOST` | `qwen2.5:7b` |

写在项目根目录 `.env` 里最省事（已被 `.gitignore` 排除）：

```bash
DEEPSEEK_API_KEY=sk-xxxxxxxxxxxxxxxx
```

验证：

```powershell
python -m routeguard config --check
```

---

## 住宿数据源（默认无需 key）

| 数据源 | 速度 | 覆盖 | Key | 何时使用 |
|---|---|---|---|---|
| **高德** | <100 ms | 最好 | 免费申请 | 配了 key 优先 |
| **天地图** | 快 | 好 | 免费申请 | 没有高德 key 时 |
| **Photon** | ~1 s | 城市好、乡镇差 | **无需** | 默认 |
| OSM Overpass | 慢、常超时 | 差 | 无需 | 最后兜底 |

**默认不需要任何 key**：没有高德/天地图 key 时自动用 Photon（komoot 的 OSM 检索），
实测上海 9 家、杭州 18 家真实酒店。乡镇级点位仍可能查不到，会明确标待核实。

想更全可以免费申请 key 写进 `.env`：

```bash
AMAP_KEY=你的高德key          # lbs.amap.com → 应用管理 → Web服务
TIANDITU_KEY=你的天地图tk      # console.tianditu.gov.cn
```

## 高程数据

| 来源 | Key | 说明 |
|---|---|---|
| **BRouter** | 无需 | 默认引擎自带高程与爬升 |
| **OpenTopoData**（SRTM 30m） | 无需 | 路由引擎不含高程时自动补齐 |

两层保障，报告里不会再出现爬升未计入而不给替代方案。

## 输出文件

```
out/plan/
├─ plan.md / plan.html     ← 行程报告
├─ plan.json               ← 结构化数据
├─ route.gpx               ← 全程轨迹（真实路网）
└─ daily/
   └─ D01_上海-途中_81km.gpx  ← 每个骑行日一个，可直接导入码表
```

网页版每次运行独立存放在 `out/web/<任务号>/`，互不覆盖。

GPX 说明：

- 点距抽稀到 40 m（原始 2–5 m 会让文件到 10 MB+）
- 用 BRouter 时带高程；用 OSRM 时**不含高程，报告会注明"爬升未计入"**

---

## 常见问题

**Q：双击 `start.bat` 报 `python 不是内部或外部命令`**
系统 PATH 里没有 Python。`start.ps1` 会自动在常见安装位置（conda、Program Files、`%LOCALAPPDATA%`）里找 3.10+ 的版本；找不到会明确提示你装或激活环境。

**Q：`ModuleNotFoundError: No module named 'routeguard'`**
没设 `PYTHONPATH`。用 `start.bat`，或手动：

```powershell
$env:PYTHONPATH = "$PWD\src"
```

**Q：规划要等多久**
首次 40–90 秒（BRouter 计算整条路线），之后走缓存秒出。
想快就用 `--router none`（直线近似，不适合导航）。

**Q：为什么里程比地图软件长**
BRouter 的自行车 profile 在中国会绕开主干道，实测比真实公路长 1.7–1.8 倍。
这是 OSM 中国数据里缺少自行车通行标记导致的，不是算错。

**Q：没有 API Key 能用吗**
能，完整功能。AI 只影响"怎么理解你的话"，不影响核验与规划。

**Q：Key 会不会被上传**
不会。`.env` 在 `.gitignore` 里，发布包也显式排除；`config` 只显示打码片段。

---

## 已知限制

1. **路线绕行**：BRouter 在中国比真实公路长 1.7–1.8 倍（实测 上海→福州 1050 km vs 实际约 950 km）；
2. **偏好未生效**：模型能解析出"沿海、少爬坡"，但**代码还没用它约束路线**；
3. **爬升是估算**：BRouter 的高程累积有噪声（长距离可能偏高），OSRM 干脆没有；
4. **住宿只判断"有没有"**：不查空房、不查价格，也不保证营业；
5. **自动落脚点可能是乡镇**：切点 ±16 km 内没有地级市时只能选乡镇，报告标"待核实"；
6. **只支持两类路线**：内置走廊（瑞金—延安 历史线/纵贯线，海拔住宿人工核对）与
   任意 A→B 自动生成；环线必须用 `--via` 指明方向。

---

## 项目结构

```
routeguard/
├─ start.bat / start.ps1   一键启动（自动找 Python、设 PYTHONPATH）
├─ src/routeguard/
│  ├─ web.py               网页版（http.server，零依赖）
│  ├─ cli.py               命令行入口
│  ├─ intent.py            自然语言 → 结构化需求（LLM + 规则兜底）
│  ├─ clarify.py           缺信息时追问
│  ├─ geocode.py           地名解析（Photon → Nominatim，含反向解析）
│  ├─ routegen.py          整条路线请求 + 按日切分 + 落脚点选级
│  ├─ router.py            BRouter / OSRM 适配层（含绕行闸门与熔断）
│  ├─ planner.py           体能模型、逐日拆分、海拔降额
│  ├─ crew.py              多 agent 编排
│  ├─ sources/
│  │  ├─ lodging.py        OSM 住宿检索
│  │  ├─ lodging_cn.py     高德 / 天地图住宿检索
│  │  └─ osm.py            路面、补给、隧道
│  ├─ claims.py            断言模型与三态裁定
│  ├─ consensus.py         证据门禁 + 安全单调裁决
│  ├─ gpx_export.py        GPX 导出（全程 + 逐日）
│  └─ plan_report.py       行程渲染
├─ tests/test_core.py      36 条测试
├─ docs/技术方案.md         研究设计（问题、方法、实验）
└─ .github/workflows/      CI（Python 3.10 / 3.11 / 3.12）
```

---

## 运行测试

```powershell
python tests\test_core.py
```

覆盖：遮荫几何、证据门禁、安全单调裁决、断言核验、OSM 降级、
路线生成、落脚点选级、GPX 导出、多 agent 失败隔离。

---

## 许可与数据来源

- 代码：[MIT](LICENSE)
- 路网：[OpenStreetMap](https://www.openstreetmap.org/copyright)（ODbL），路由由 [BRouter](https://brouter.de) 提供
- 住宿：高德 / 天地图 / OSM（各自遵守其条款）
- 示范轨迹：合成数据，不含任何真实个人位置

> 提交自己骑过的 GPX 前，请删除起终点 500 m 范围，并征得同行者同意。
