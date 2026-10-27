# Changelog

本项目版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)，
变更记录格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)。

## [1.1.0] - 2026-10-26

全部数据源改为**免费、无需 key**，并修掉 agent 的重复路由。

### 新增

- **Photon 住宿检索**（`sources/elevation.py`）：komoot 的 OSM 检索，无需 key，
  1 秒返回。实测上海 9 家、杭州 18 家真实酒店，取代常年超时的 Overpass。
- **OpenTopoData 高程兜底**：路由引擎不返回高程时（如 OSRM），自动用 SRTM 30m
  补齐并重算爬升，报告不再出现"爬升未计入"。
- **agent 结果复用**：`plan_route` 工具算出的航点与几何存入 `LAST_ROUTE`，
  交接时直接出报告，不再二次请求路由（原先要算两遍，第二遍极易失败）。
- **harness 工程**：工具注册表 / 技能库（`skills/*.md`）/ 跨会话记忆。
- **网页版**：阶段进度条、子 agent 卡片、日志分级上色、失败时展示完整堆栈。

### 修复

- **严格路由**：拿不到真实路网时直接报错，绝不静默退回直线（此前会产出 4 个点的假 GPX）。
- **缓存键带引擎签名**：换了引擎不再复用旧结果。
- **空日期崩溃**：`date.fromisoformat('')` 与 `int('')` 两处已容错。
- **skills 未打包**：`pyproject` 补 `skills/*.md`，否则 pip 安装后技能为空。
- **测试补强**：工具注册表、技能匹配、记忆持久化、路由降级与熔断、缓存签名。

### 已知限制

- `supply` agent 复用的是住宿检索器，统计的是住宿 POI，不是便利店；
- 自动落脚点若落在乡镇，Photon 多半查不到住宿，会标"待核实"。

---

## [1.0.0]

- 单次 OSRM 请求整条路线，按日预算切分（不再按 120km 采样）
- 落脚点按行政等级选取：地级市 > 县级市 > 乡镇

## [0.9.0]

- 网页版 `routeguard web`，后台线程跑规划、页面轮询进度
- 下载区：报告 / 全程 GPX / 逐日 GPX / 打包 zip

## [0.8.0]

- 改为单次请求整条路线，修复 91 段 / 10073 km 的累积误差
## [0.1.0 – 0.7.1] 内部迭代 · 2026-10

公开版本从 0.8.0 起算，之前是设计与原型期，主要沉淀：

- 三角色（生产者 / 溯源 / 仲裁）与证据门禁
- 断言级核验与三态裁定（支持 / 矛盾 / 未核实 / 主观）
- 动态遮荫模型（冠层高度 + 太阳几何）
- 多厂商 Key 适配（7 家 + Ollama 本地）
- 路线走廊与体能模型（瑞金—延安 历史线 / 纵贯线）
- 多 agent 编排原型（geo / route / terrain / weather / lodging / safety / critic）
- 网页版与逐日 GPX 导出

---

[Unreleased]: https://github.com/huyuheng852/routeguard/compare/v0.7.1...HEAD
[0.7.1]: https://github.com/huyuheng852/routeguard/releases/tag/v0.7.1
[0.7.0]: https://github.com/huyuheng852/routeguard/releases/tag/v0.7.0
[0.6.0]: https://github.com/huyuheng852/routeguard/releases/tag/v0.6.0
[0.5.0]: https://github.com/huyuheng852/routeguard/releases/tag/v0.5.0
[0.4.0]: https://github.com/huyuheng852/routeguard/releases/tag/v0.4.0
[0.3.1]: https://github.com/huyuheng852/routeguard/releases/tag/v0.3.1
[0.3.0]: https://github.com/huyuheng852/routeguard/releases/tag/v0.3.0
