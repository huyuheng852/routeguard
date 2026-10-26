"""OpenAI 兼容的 LLM 客户端（仅标准库）。

设计要点
--------
1. **失败必须降级**：调用失败抛 LLMError，由上层回退到规则抽取，绝不让整条流水线崩掉。
2. **只做 JSON 模式**：本项目的 LLM 只负责结构化抽取与解释，不产出自由文本。
3. 不把 key 写进日志、异常信息。
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Protocol


class LLMError(RuntimeError):
    """模型调用失败或返回内容无法解析为 JSON。"""


class LLMClient(Protocol):
    def chat_json(self, system_prompt: str, user_content: str, temperature: float = 0.0) -> Dict[str, Any]:
        ...


_JSON_FENCE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.S)


def extract_json(raw: str) -> Dict[str, Any]:
    """从模型输出里抠出 JSON 对象，兼容代码围栏与前后废话。"""
    text = raw.strip()
    m = _JSON_FENCE.search(text)
    if m:
        text = m.group(1).strip()
    if not text.startswith("{"):
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            text = text[start : end + 1]
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise LLMError(f"模型返回不是合法 JSON：{exc}") from exc
    if not isinstance(data, dict):
        raise LLMError("模型返回的 JSON 根节点不是对象")
    return data


class OpenAICompatClient:
    """适配 OpenAI / DeepSeek / Agnes 等兼容 Chat Completions 的服务。"""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        timeout: int = 90,
        retries: int = 2,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.retries = retries

    def _post(self, body: Dict[str, Any]) -> Dict[str, Any]:
        url = f"{self.base_url}/chat/completions"
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def chat_json(self, system_prompt: str, user_content: str, temperature: float = 0.0) -> Dict[str, Any]:
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            "temperature": temperature,
        }
        last: Optional[Exception] = None
        for attempt in range(1, self.retries + 1):
            try:
                payload = self._post(body)
                content = payload["choices"][0]["message"]["content"]
                return extract_json(content)
            except (urllib.error.URLError, TimeoutError, KeyError, json.JSONDecodeError) as exc:
                last = exc
                if attempt < self.retries:
                    time.sleep(2 * attempt)
            except Exception as exc:  # noqa: BLE001
                last = exc
                break
        raise LLMError(f"LLM 调用失败：{type(last).__name__}: {last}")


class NullLLM:
    """未配置模型时的占位实现：调用即失败，由上层回退。"""

    def chat_json(self, system_prompt: str, user_content: str, temperature: float = 0.0) -> Dict[str, Any]:
        raise LLMError("未配置 LLM（缺少 LLM_BASE_URL / LLM_API_KEY / LLM_MODEL）")


def build_llm(settings) -> LLMClient:
    if not settings.llm_ready:
        return NullLLM()
    return OpenAICompatClient(
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key,
        model=settings.llm_model,
        timeout=settings.llm_timeout,
    )
