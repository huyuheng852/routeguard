"""断言抽取器：规则版与 LLM 版。

两者接口一致：`extract(guide_md, segment_ids) -> List[Claim]`。
LLM 版失败时**自动回退**到规则版，保证流水线永不因模型不可用而中断。
"""

from __future__ import annotations

from typing import Any, Dict, List, Sequence

from .claims import Claim, class_kind_list, classify, extract_claims as rule_extract, normalize_slope
from .llm import LLMClient, LLMError

EXTRACT_SYSTEM_PROMPT = r"""你是「骑行路书断言抽取器」。

任务：把路书里的事实性描述，拆成一条条**可核验的原子断言**。

硬性要求：
1. 只抽取路书中**确实写过**的内容，不得补充、不得改写事实、不得推断。
2. 一句话含多个事实就拆成多条。
3. 主观描述（风景、感受）也要抽出来，标 kind=scenery，不要丢弃。
4. kind 只能取以下值：slope / surface / traffic / supply / shade / passable / scenery
5. segment_id 只能取给定的路段编号之一，或 "全程"。
6. slope 类断言的 stated 必须是 平 / 缓 / 陡 / 很陡 之一。

只输出 JSON：
{"claims": [{"segment_id": "S001", "kind": "slope", "stated": "平", "text": "原文这句"}]}
"""


class RuleExtractor:
    """关键词 + 行格式的规则抽取，零依赖，可离线。"""

    name = "rule"

    def extract(self, guide_md: str, segment_ids: Sequence[str] = ()) -> List[Claim]:
        return rule_extract(guide_md)


class LLMClaimExtractor:
    """大模型抽取，失败自动回退规则版。"""

    name = "llm"

    def __init__(self, llm: LLMClient, temperature: float = 0.0) -> None:
        self.llm = llm
        self.temperature = temperature
        self.last_error: str = ""

    def extract(self, guide_md: str, segment_ids: Sequence[str] = ()) -> List[Claim]:
        import json

        user = json.dumps(
            {
                "segments": list(segment_ids) or ["全程"],
                "kinds": class_kind_list(),
                "route_guide": guide_md,
            },
            ensure_ascii=False,
        )
        try:
            data = self.llm.chat_json(EXTRACT_SYSTEM_PROMPT, user, self.temperature)
        except LLMError as exc:
            self.last_error = str(exc)
            return rule_extract(guide_md)

        raw_claims = data.get("claims")
        if not isinstance(raw_claims, list) or not raw_claims:
            self.last_error = "模型返回的 claims 为空或格式错误，已回退规则抽取"
            return rule_extract(guide_md)

        allowed_segments = set(segment_ids) | {"全程"}
        allowed_kinds = set(class_kind_list())
        claims: List[Claim] = []
        for item in raw_claims:
            if not isinstance(item, dict):
                continue
            kind = str(item.get("kind", "")).strip()
            seg = str(item.get("segment_id", "全程")).strip()
            text = str(item.get("text", "")).strip()
            if kind not in allowed_kinds or not text:
                continue
            if seg not in allowed_segments:
                seg = "全程"
            stated = str(item.get("stated", text)).strip()
            if kind == "slope":
                stated = normalize_slope(stated) if stated in ("平", "缓", "陡", "很陡") else normalize_slope(text)
            claims.append(
                Claim(
                    claim_id=f"C{len(claims) + 1:03d}",
                    text=text,
                    segment_id=seg,
                    kind=kind,
                    stated=stated,
                )
            )
        if not claims:
            self.last_error = "模型输出未通过校验，已回退规则抽取"
            return rule_extract(guide_md)
        return claims


def build_extractor(use_llm: bool, llm: LLMClient) -> Any:
    if use_llm:
        return LLMClaimExtractor(llm)
    return RuleExtractor()
