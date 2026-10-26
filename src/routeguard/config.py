"""配置：从环境变量与 .env 读取，自动识别主流大模型厂商的 API Key。

优先级
------
1. 显式指定：`LLM_BASE_URL` + `LLM_API_KEY` + `LLM_MODEL`
2. 厂商专用变量：`DEEPSEEK_API_KEY` / `OPENAI_API_KEY` / …（见 PROVIDERS）
3. 本地 Ollama：设置 `OLLAMA_HOST`（无需 key）
4. 都没有 → LLM 功能关闭，断言抽取自动退回规则版

用户只要把 key 放进环境变量或 `.env`，不用改任何代码。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional, Tuple

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _parse_env_file(path: Path) -> Dict[str, str]:
    values: Dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


# 厂商预设：变量名 → (base_url, 默认模型)
# 注意：字典顺序即优先级。国内用户居多，DeepSeek 放第一位；
# README 的表格顺序必须与此保持一致。
PROVIDERS: Dict[str, Tuple[Tuple[str, ...], str, str]] = {
    "deepseek": (("DEEPSEEK_API_KEY",), "https://api.deepseek.com/v1", "deepseek-chat"),
    "openai": (("OPENAI_API_KEY",), "https://api.openai.com/v1", "gpt-4o-mini"),
    "moonshot": (("MOONSHOT_API_KEY",), "https://api.moonshot.cn/v1", "moonshot-v1-8k"),
    "dashscope": (
        ("DASHSCOPE_API_KEY", "QWEN_API_KEY"),
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "qwen-plus",
    ),
    "zhipu": (
        ("ZHIPUAI_API_KEY", "ZHIPU_API_KEY", "GLM_API_KEY"),
        "https://open.bigmodel.cn/api/paas/v4",
        "glm-4-flash",
    ),
    "siliconflow": (
        ("SILICONFLOW_API_KEY", "SF_API_KEY"),
        "https://api.siliconflow.cn/v1",
        "Qwen/Qwen2.5-7B-Instruct",
    ),
    "agnes": (
        ("AGNES_API_KEY",),
        "https://api.agnes-ai.cn/v1",
        "agnes-2.5-flash",
    ),
}

OLLAMA_DEFAULT = "http://127.0.0.1:11434/v1"


@dataclass
class Settings:
    """运行配置。所有外部依赖都可以关掉，关掉即降级。"""

    provider: str = "none"
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = ""
    llm_timeout: int = 90

    overpass_url: str = "https://overpass-api.de/api/interpreter"
    overpass_timeout: int = 25
    osm_enabled: bool = True
    osm_radius_m: int = 150

    cache_dir: Path = field(default_factory=lambda: PROJECT_ROOT / ".cache")

    @property
    def llm_ready(self) -> bool:
        return bool(self.llm_base_url and self.llm_model and (self.llm_api_key or self._is_local))

    @property
    def _is_local(self) -> bool:
        return "127.0.0.1" in self.llm_base_url or "localhost" in self.llm_base_url

    def masked_key(self) -> str:
        k = self.llm_api_key
        if not k:
            return "(未提供，本地模型可留空)"
        if len(k) <= 8:
            return k[:2] + "***"
        return f"{k[:6]}…{k[-4:]}"

    @classmethod
    def load(cls, env_file: Optional[Path] = None) -> "Settings":
        env_file = env_file or (PROJECT_ROOT / ".env")
        file_values = _parse_env_file(env_file)

        def get(key: str, default: str = "") -> str:
            return os.getenv(key) or file_values.get(key, default)

        provider = "none"
        base_url = get("LLM_BASE_URL")
        api_key = get("LLM_API_KEY")
        model = get("LLM_MODEL")

        if base_url and model:
            provider = "custom"
        else:
            for name, (env_names, preset_base, preset_model) in PROVIDERS.items():
                found = next((get(k) for k in env_names if get(k)), "")
                if found:
                    provider = name
                    base_url = base_url or preset_base
                    api_key = api_key or found
                    model = model or preset_model
                    break

        if provider == "none":
            host = get("OLLAMA_HOST")
            if host:
                provider = "ollama"
                base_url = host.rstrip("/")
                if not base_url.endswith("/v1"):
                    base_url += "/v1"
                model = model or get("OLLAMA_MODEL", "qwen2.5:7b")

        return cls(
            provider=provider,
            llm_base_url=base_url,
            llm_api_key=api_key,
            llm_model=model,
            llm_timeout=int(get("LLM_TIMEOUT", "90") or 90),
            overpass_url=get("OVERPASS_URL", "https://overpass-api.de/api/interpreter"),
            overpass_timeout=int(get("OVERPASS_TIMEOUT", "25") or 25),
            osm_enabled=(get("OSM_ENABLED", "1").lower() not in ("0", "false", "no")),
            osm_radius_m=int(get("OSM_RADIUS_M", "150") or 150),
            cache_dir=Path(get("ROUTE_GUARD_CACHE", str(PROJECT_ROOT / ".cache"))),
        )
