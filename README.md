# RouteGuard · 可核验的骑行路书

[![tests](https://img.shields.io/badge/tests-29%20passed-brightgreen)](#运行测试)
[![python](https://img.shields.io/badge/python-3.10%2B-blue)](#环境要求)
[![license](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![deps](https://img.shields.io/badge/dependencies-stdlib%20only-success)](#环境要求)

> 骑友最大的痛点不是"没有路书"，而是**路书不可信**：
> 说"全程平路"实际爬升 900 米，标的补给点三个月前就关门了。
> 而 AI 写的路书更糟——它会把空白补成"沿途风景优美、补给充足"。

RouteGuard 做两件事：

| 能力 | 命令 | 说明 |
|---|---|---|
| **核验路书** | `routeguard audit` | 把路书拆成原子断言，逐条判 **支持 / 矛盾 / 未核实 / 主观** |
| **规划路线** | `routeguard plan` | 从一句话（FTP、体重、起终点、日期）生成多日行程 + 真实路网 GPX |

---

## 目录

- [30 秒上手](#30-秒上手)
- [安装](#安装)
- [配置 AI（支持 7 家厂商 + 本地模型）](#配置-ai支持-7-家厂商--本地模型)
- [命令详解](#命令详解)
- [输出文件](#输出文件)
- [离线与降级](#离线与降级)
- [常见问题](#常见问题)
- [已知限制](#已知限制)
- [项目结构](#项目结构)
- [许可与数据来源](#许可与数据来源)

---

## 30 秒上手

```powershell
# 1) 进目录
cd routeguard

# 2) 让 Python 找到包（Windows PowerShell）
$env:PYTHONPATH = "$PWD\src"

# 3) 自检（不需要网络、不需要 API Key）
python -m routeguard selftest

# 4) 核验一份示范路书
python -m routeguard audit --no-osm

# 5) 规划一条线路
python -m routeguard plan --text "我的FTP为250w，体重70kg，2027年6月从瑞金到延安" --router none
```

第 4 步会输出 10 条断言的核验结果，第 5 步会在 `out/plan/` 生成 Markdown / HTML / GPX。

> **没有 API Key 也能跑**。AI 只是"锦上添花"：配了就用大模型抽取断言、写解释；
> 没配就退回规则引擎，功能完整、结果可复现。

---

## 安装

### 环境要求

| 项目 | 要求 |
|---|---|
| Python | **3.10 及以上**（用到 `X | Y` 类型语法） |
| 第三方依赖 | **零依赖**，只用标准库 |
| 网络 | 可选。规划路线需要访问 BRouter/OSRM；核验 OSM 需要 Overpass |

### 方式 A：解压即用（推荐新手）

1. 下载 Release 里的 `routeguard-v0.3.0.zip`
2. 解压到任意目录（路径**不要有空格**最省事）
3. 打开终端进入该目录：

```powershell
cd routeguard-v0.3.0
$env:PYTHONPATH = "$PWD\src"
python -m routeguard selftest
```

看到 `自检：10 条断言，矛盾 2 条` 就成了。

### 方式 B：从源码安装（推荐开发者）

```powershell
git clone https://github.com/<你的用户名>/routeguard.git
cd routeguard
pip install -e .
routeguard selftest
```

装完可以直接用 `routeguard` 命令，不必再设 `PYTHONPATH`。

> **国内网络提示**：`pip install -e .` 会创建一个隔离构建环境去下载 setuptools，
> 如果卡在 `files.pythonhosted.org` 超时，用下面任一方式：
>
> ```powershell
> # 1) 用国内镜像
> pip install -e . -i https://pypi.tuna.tsinghua.edu.cn/simple
>
> # 2) 或跳过构建隔离（环境里已有 setuptools 即可）
> pip install -e . --no-build-isolation
> ```
>
> 完全不想装也行——用方式 A，设 `PYTHONPATH` 就能跑。

### Linux / macOS

```bash
cd routeguard
export PYTHONPATH="$PWD/src"
python3 -m routeguard selftest
```

---

## 配置 AI（支持 7 家厂商 + 本地模型）

RouteGuard 用 **OpenAI 兼容接口**，所以只要厂商支持 `/chat/completions`，就能直接用。

### 支持的厂商

| 厂商 | 环境变量 | 默认 Base URL | 默认模型 |
|---|---|---|---|
| **DeepSeek** | `DEEPSEEK_API_KEY` | `https://api.deepseek.com/v1` | `deepseek-chat` |
| **OpenAI** | `OPENAI_API_KEY` | `https://api.openai.com/v1` | `gpt-4o-mini` |
| **月之暗面 Kimi** | `MOONSHOT_API_KEY` | `https://api.moonshot.cn/v1` | `moonshot-v1-8k` |
| **通义千问** | `DASHSCOPE_API_KEY` / `QWEN_API_KEY` | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `qwen-plus` |
| **智谱 GLM** | `ZHIPUAI_API_KEY` / `ZHIPU_API_KEY` | `https://open.bigmodel.cn/api/paas/v4` | `glm-4-flash` |
| **硅基流动** | `SILICONFLOW_API_KEY` | `https://api.siliconflow.cn/v1` | `Qwen/Qwen2.5-7B-Instruct` |
| **Agnes** | `AGNES_API_KEY` | `https://api.agnes-ai.cn/v1` | `agnes-2.5-flash` |
| **Ollama 本地** | `OLLAMA_HOST` | `http://127.0.0.1:11434/v1` | `qwen2.5:7b` |

> 顺序即优先级：同时配了多个，按表中顺序取第一个命中的。

### 三种配置方式，任选其一

**方式 1：`.env` 文件（推荐，最不容易泄漏）**

在项目根目录新建 `.env`（可复制 `.env.example`）：

```bash
# 只写一行就够了，其他全用默认值
DEEPSEEK_API_KEY=sk-xxxxxxxxxxxxxxxx
```

**方式 2：系统环境变量**

```powershell
# Windows PowerShell（当前窗口有效）
$env:DEEPSEEK_API_KEY = "sk-xxxxxxxxxxxxxxxx"

# 永久生效
setx DEEPSEEK_API_KEY "sk-xxxxxxxxxxxxxxxx"
```

```bash
# Linux / macOS
export DEEPSEEK_API_KEY="sk-xxxxxxxxxxxxxxxx"
```

**方式 3：完全手动指定（任意兼容服务）**

```bash
LLM_BASE_URL=https://your-endpoint/v1
LLM_API_KEY=sk-xxxx
LLM_MODEL=your-model-name
```

### 验证配置

```powershell
python -m routeguard config           # 只看识别结果
python -m routeguard config --check   # 发一次真实请求验证 key
```

正常输出：

```
识别到的厂商 : deepseek
Base URL     : https://api.deepseek.com/v1
模型         : deepseek-chat
API Key      : sk-abc…wxyz
LLM 可用     : 是
```

`--check` 成功会打印模型的返回内容；失败会给出具体原因（401 是 key 错，429 是限流）。

### 用本地 Ollama（零成本、数据不出本机）

```powershell
ollama pull qwen2.5:7b
$env:OLLAMA_HOST = "http://127.0.0.1:11434"
python -m routeguard config --check
```

本地模型不需要 key，RouteGuard 会自动识别。

### AI 用在哪

| 环节 | 配了 AI | 没配 AI |
|---|---|---|
| 断言抽取（`audit --llm`） | 大模型把自然语言拆成原子断言 | 关键词 + 行格式规则 |
| 其余全部 | — | 完全一致（核算、裁决、规划都是确定性代码） |

**AI 只负责"读懂人话"，判断权始终在代码手里**——这是本项目的核心设计。

---

## 命令详解

### `routeguard config`

```powershell
python -m routeguard config            # 查看当前配置
python -m routeguard config --check    # 验证 API Key 是否有效
```

### `routeguard selftest`

不联网跑一遍核心链路，用来确认安装没问题。

```powershell
python -m routeguard selftest
# 配置：LLM=就绪｜OSM=开
# 自检：10 条断言，矛盾 2 条
```

### `routeguard audit` —— 核验路书

```powershell
python -m routeguard audit                    # 核验内置示范路书
python -m routeguard audit --no-osm           # 纯离线
python -m routeguard audit --llm              # 用大模型抽取断言
python -m routeguard audit --guide 我的路书.md --gpx 我的轨迹.gpx --when 2027-06-01
```

| 参数 | 默认 | 说明 |
|---|---|---|
| `--guide` | 示范路书 | 路书文件（Markdown） |
| `--gpx` | 示范轨迹 | 对应轨迹 |
| `--when` | `2026-08-15` | 出发日期（决定遮荫、天气） |
| `--hour` | `9` | 出发小时 |
| `--osm/--no-osm` | 开 | 是否查 OSM 核验路面/补给/隧道 |
| `--llm` | 关 | 是否用大模型抽取断言 |
| `--out` | `out/audit` | 输出目录 |

**路书格式**：每行一条断言，`[路段]` 标注对应轨迹段：

```markdown
- [S001] 坡度平缓，适合新手
- [S003] 沿途有便利店可以补水
- [全程] 全程平路，几乎没有爬升
- [全程] 风景绝美，值得一去
```

输出示例：

```
C001  [全程] 全程平路，几乎没有爬升        ⚠️ 矛盾
         └ 全程展开 → S001:contradicted；S002:contradicted；S003:contradicted
C004  [S003] 沿途有便利店可以补水          ❔ 未核实
         └ OSM 在该区域 POI 覆盖稀疏，检索不到不足以否定该断言
C010  [全程] 风景绝美，值得一去            💬 主观
         └ 主观描述，不纳入核验范围
共 10 条断言｜核验覆盖率 56%｜未核实率 44%｜矛盾 2｜未核实 4｜主观 1｜支持 3
```

### `routeguard plan` —— 规划路线

```powershell
python -m routeguard plan --text "我的FTP为250w，体重70kg，想在2027年6月份开始骑行长征路线，即从瑞金到延安。帮我规划路线。"
```

一句话里能识别：**FTP、体重、日期、起终点、路线类型**。缺什么会自己假设并写进报告。

| 参数 | 默认 | 说明 |
|---|---|---|
| `--text` | 必填 | 自然语言需求 |
| `--router` | `auto` | `auto`=BRouter→OSRM→直线；`none`=只算直线（秒出，不可导航） |
| `--lodging` | `auto` | 无城镇落脚点时是否用 OSM 检索酒店 |
| `--daily` | 按体能算 | 覆盖每日目标里程 |
| `--corridor` | 自动 | `historic`（历史长征线）/ `central`（中部纵贯线） |
| `--start` | 从文本解析 | 覆盖出发日期 |
| `--gpx-out` | `<out>/route.gpx` | 全程 GPX 路径 |
| `--no-daily-gpx` | 关 | 不生成逐日 GPX |

输出示例：

```
路线规划：瑞金 → 延安｜历史长征线｜出发 2027-06-01
  FTP：250 W｜体重：70 kg｜功重比：3.57 W/kg｜水平：较强
  规划目标：109 km/天（含行李与季节修正）
总里程 6699 km｜累计爬升 110648 m｜骑行 79 天｜休整 7 天｜日历 86 天
```

**规划的四条硬规则**：

1. 日里程 = W/kg 基准 × 行李系数 × 季节系数；
2. 海拔 >3000 m 自动降额（3000–3500 打 0.65 折，>3500 打 0.5 折），首次上高原插一天适应日；
3. **只在有住宿的城镇收工**，绝不把人安排在山口过夜；
4. 无住宿路段强制合并成一天，并明确告知"要么硬骑，要么自备露营装备"。

---

## 输出文件

### `audit` 的产物

```
out/audit/
├─ route_audit.md      ← 逐条核验报告（Markdown）
├─ route_audit.html    ← 自包含 HTML，双击可看
└─ claims.json         ← 结构化结果，便于二次开发
```

### `plan` 的产物

```
out/plan/
├─ plan.md             ← 行程报告
├─ plan.html           ← 可视化版
├─ plan.json           ← 结构化行程（每天里程/爬升/住宿）
├─ route.gpx           ← 全程轨迹（真实路网）
└─ daily/
   ├─ D01_瑞金-于都_80km.gpx
   └─ …                ← 每个骑行日一个，可直接导入码表
```

---

## 离线与降级

所有外部服务都是**可选**的，挂了就降级，不会让程序崩：

| 服务 | 用途 | 不可用时 |
|---|---|---|
| BRouter / OSRM | 真实路网 | 退回走廊直线，并在报告标注"非真实路线" |
| Overpass（OSM） | 路面/补给/住宿 | 相关断言标"未核实"，不猜 |
| 大模型 API | 断言抽取 | 回退规则引擎 |

想完全离线：加 `--no-osm` 和 `--router none`。

**缓存**：路由结果按航点对缓存在 `.cache/routes/`，住宿查询在 `.cache/lodging/`。
第二次跑同样的路线是秒出。改路线或换城市会重新请求。

---

## 常见问题

**Q：没有 API Key 能用吗？**
能，而且是完整功能。AI 只影响"断言的抽取方式"，不影响核验、规划和报告。

**Q：API Key 会不会被上传？**
不会。`.gitignore` 排除了 `.env`，代码里没有任何硬编码的 key。`config` 命令只显示打码后的片段。

**Q：规划很慢 / 卡住？**
首次规划几十段真实路线需要 1–2 分钟（每段一次请求）。之后走缓存。
如果网络不通，`--router auto` 会在几次失败后退回直线，不会无限等。

**Q：报 `Failed to connect` 或 `TimeoutError`**
检查能否访问 `brouter.de` / `overpass-api.de`。国内网络可能间歇性不通，
可以设代理：`$env:HTTPS_PROXY = "http://127.0.0.1:7890"`。

**Q：Windows 控制台中文乱码？**
用 Windows Terminal 或 PowerShell 7；代码里已强制 UTF-8 输出。

**Q：`ModuleNotFoundError: No module named 'routeguard'`**
没设 `PYTHONPATH`。要么 `$env:PYTHONPATH = "$PWD\src"`，要么 `pip install -e .`。

**Q：`.gpx` 导入码表后距离比报告少？**
报告里是公路里程（含爬坡绕行），GPX 抽稀到 40 m 点距，会有约 1.4% 的损失，属正常。

**Q：用 AI 要花多少钱？**
一次 `audit --llm` 只调用 1 次模型，处理一份路书约几千 token，成本不到一分钱。
`plan` 默认不调用模型。

---

## 已知限制

1. **走廊不是逐米路由**：规划基于城市航点，实际骑行前建议用 BRouter / RideWithGPS 复核；
2. **爬升是估算**：BRouter 返回真实高程时按高程算，否则按地形系数（误差约 ±30%）；
3. **住宿只到城镇级**：判断"有没有县城/酒店"，不判断"有没有空房"；
4. **大陆 OSM 覆盖不全**：查不到住宿 ≠ 没有，报告会列出待确认清单；
5. **只内置两条走廊**：瑞金—延安（历史 / 纵贯），换起终点会明确拒绝而不是瞎编；
6. **`pip install routeguard` 尚未支持**：`data/` 没打进 wheel，目前请用 `pip install -e .`。

---

## 项目结构

```
routeguard/
├─ src/routeguard/
│  ├─ config.py       配置与厂商识别（7 家 + Ollama）
│  ├─ llm.py          OpenAI 兼容客户端（含失败降级）
│  ├─ brief.py        自然语言 → 出行简报
│  ├─ corridor.py     路线走廊数据（含海拔）
│  ├─ router.py       BRouter / OSRM 真实路由
│  ├─ planner.py      体能模型、逐日拆分、海拔降额
│  ├─ sources/
│  │  ├─ osm.py       OSM 路面/补给/隧道
│  │  └─ lodging.py   OSM 住宿检索
│  ├─ claims.py       断言模型与三态裁定
│  ├─ extractors.py   规则抽取 / LLM 抽取
│  ├─ consensus.py    证据门禁 + 安全单调裁决
│  ├─ gpx_export.py   GPX 导出（全程 + 逐日）
│  ├─ plan_report.py  行程渲染
│  └─ cli.py          命令行入口
├─ tests/test_core.py 29 条测试
├─ docs/技术方案.md   研究设计（问题、方法、实验）
├─ data/sample/       示范 GPX 与"故意写砸"的路书
└─ .github/workflows/  CI
```

---

## 运行测试

```powershell
python tests\test_core.py
```

覆盖：遮荫几何、证据门禁、安全单调裁决、断言核验、OSM 降级、路线规划、GPX 导出。

---

## 许可与数据来源

- 代码：[MIT](LICENSE)
- 路网：[OpenStreetMap](https://www.openstreetmap.org/copyright)（ODbL），路由由 [BRouter](https://brouter.de) 提供
- 数字高程：BRouter 内置 SRTM
- 轨迹数据：示范文件为合成数据，不含任何真实个人位置

> 如果你要提交自己骑过的 GPX，请先删除起终点 500 m 范围，并征得同行者同意。
