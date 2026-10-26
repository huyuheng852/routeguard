# 贡献指南

感谢关注。这是一个研究原型，欢迎提 issue 或 PR，尤其是以下几类：

## 特别欢迎

- **真实路线反馈**：你按这个规划骑过某一段，发现里程/爬升/住宿与实际不符；
- **路由服务补充**：除 BRouter / OSRM 以外的开源路由（如 Valhalla）接入；
- **住宿数据源**：OSM 在中国大陆的住宿覆盖有限，如果你知道更好的开源 POI 源；
- **断言类型扩展**：`claims.py` 里的六类断言之外，骑行路书里还有哪些值得核验的事实。

## 开发约定

1. **零第三方依赖**：运行时只用 Python 标准库（`pip install pytest` 仅用于可选测试）；
2. **失败必须降级**：任何外部服务（路由、OSM、LLM）不可用时，程序要能继续跑，
   并把"没拿到数据"如实传给用户，而不是抛异常或编一个结果；
3. **新增外部数据源**：先在 `schema.SOURCE_ALLOWLIST` 登记，否则证据会被门禁丢弃；
4. **测试**：新增功能请补 `tests/test_core.py` 里的用例，保持 `python tests/test_core.py` 全绿。

## 提交前

```powershell
python tests\test_core.py
python -m routeguard selftest
```

## 不要提交

- 真实的 `.env`、API Key；
- 个人骑行轨迹（GPX 含起终点位置信息）；
- `out/`、`.cache/` 等运行产物。
