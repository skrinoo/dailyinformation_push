"""LLM 客户端：OpenAI 兼容 /chat/completions。

三条硬约束：
1. 所有网络调用都有显式超时与重试（指数退避 + 抖动），失败必须向上抛，不静默；
2. 预算只认 token（见 budget.py）；价格可选，缺省为 0 时只报 token 不报钱，
   避免把过时价目表当成事实；
3. 返回结构化 usage，调用方据此记账，预算闸才能知道「这一轮还烧不烧得起」。
"""

from __future__ import annotations

import json
import random
import re
import time
from dataclasses import dataclass
from typing import Any

from .budget import BudgetExceeded, TokenBudget
from .util import HttpError, http_post_json

__all__ = [
    "LLMError", "BudgetExceeded", "LLMResult", "chat",
    "estimate_messages_tokens", "parse_json_loose",
]


class LLMError(RuntimeError):
    pass


@dataclass
class LLMResult:
    text: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0
    attempts: int = 1


def _estimate_tokens(text: str) -> int:
    """粗估：中文约 1 token/字，英文约 4 字符/token。仅用于预算守卫，不用于计费。"""
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    return cjk + max(0, (len(text) - cjk) // 4)


def estimate_messages_tokens(messages: list[dict[str, str]]) -> int:
    return sum(_estimate_tokens(m.get("content", "")) + 4 for m in messages)


def chat(
    cfg: dict[str, Any],
    api_key: str,
    messages: list[dict[str, str]],
    json_mode: bool = True,
    budget: TokenBudget | None = None,
    stage: str = "llm",
) -> LLMResult:
    llm = cfg["llm"]
    if not api_key:
        raise LLMError(f"缺少 API key：请设置环境变量 {llm.get('api_key_env')}")
    base = (llm.get("base_url") or "").rstrip("/")
    if not base:
        raise LLMError("llm.base_url 为空")

    # 预算准入：估算只回答「准不准进」，真实花费以 usage 为准。
    estimate = estimate_messages_tokens(messages)
    if budget is not None and not budget.guard(stage, estimate):
        raise BudgetExceeded(
            f"预算不足（{stage}）：预估 {estimate} token，剩余 {budget.remaining()}"
        )

    price_in = float(llm.get("price_per_1k_in") or 0)
    price_out = float(llm.get("price_per_1k_out") or 0)

    payload: dict[str, Any] = {
        "model": llm.get("model", ""),
        "messages": messages,
        "temperature": float(llm.get("temperature", 0.0)),
    }
    if llm.get("max_output_tokens"):
        payload["max_tokens"] = int(llm["max_output_tokens"])
    if json_mode:
        payload["response_format"] = {"type": "json_object"}

    retries = int(llm.get("max_retries", 2))
    timeout = int(llm.get("timeout", 90))
    last_err: Exception | None = None

    for attempt in range(1, retries + 2):
        try:
            data = http_post_json(
                f"{base}/chat/completions",
                payload,
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=timeout,
            )
            content = data["choices"][0]["message"]["content"]
            usage = data.get("usage") or {}
            pt = int(usage.get("prompt_tokens") or 0)
            ct = int(usage.get("completion_tokens") or 0)
            if not pt:  # 某些兼容端点不返回 usage，退回估算，否则预算会永远为 0
                pt = estimate
            if not ct:
                ct = _estimate_tokens(content or "")
            total = pt + ct
            if budget is not None:
                budget.charge(pt, ct)
            cost = pt / 1000 * price_in + ct / 1000 * price_out
            return LLMResult(text=content or "", prompt_tokens=pt, completion_tokens=ct,
                             total_tokens=total, cost_usd=round(cost, 6), attempts=attempt)
        except (HttpError, KeyError, IndexError, TypeError) as exc:
            last_err = exc
            if attempt > retries:
                break
            backoff = float(llm.get("retry_backoff_base", 0.8)) * (2 ** (attempt - 1))
            jitter = random.uniform(0, float(llm.get("retry_jitter", 0.3)))
            time.sleep(min(8.0, backoff + jitter))

    raise LLMError(f"LLM 调用失败（{retries + 1} 次尝试）：{last_err}")


_FENCE = re.compile(r"```(?:json)?\s*([\s\S]*?)```")
_ARRAY = re.compile(r"\[[\s\S]*\]")
_OBJECT = re.compile(r"\{[\s\S]*\}")


def parse_json_loose(text: str) -> Any:
    """尽量从模型输出里捞出 JSON。宁可从一段废话里捞出数组，也不要整批失败。"""
    raw = (text or "").strip()
    if not raw:
        raise LLMError("模型返回空内容")

    fenced = _FENCE.search(raw)
    if fenced:
        raw = fenced.group(1).strip()

    for candidate in (raw,):
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass

    for pattern in (_OBJECT, _ARRAY):
        m = pattern.search(raw)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                continue
    raise LLMError(f"无法解析 JSON：{raw[:200]}")
