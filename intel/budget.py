"""Token 预算。

用户按 token 设上限，而不是按美元——因为价格会变、供应商会换，但「我每天愿意让
它读多少字」是稳定意图。所以预算闸只认 token，钱只在报表里按需换算。

两道上限同时生效：
  tokens_per_run —— 单轮天花板，防止一次跑飞
  tokens_per_day —— 日累计天花板，防止「一天跑 20 次」

超限行为可配：
  degrade（默认）—— 停掉模型，降级到规则/确定性方案，继续把这一轮跑完
  stop          —— 直接抛错中止，宁可没有简报也不超预算

预算按「预估 + 实扣」两段记账：调用前用字符估算做准入，调用后用 API 返回的
usage 做真实扣减。估算只用于「准不准进」，不用于「花了多少」。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class BudgetExceeded(RuntimeError):
    """预算不足以完成本次调用。on_exceed=stop 时向上抛，否则由调用方降级。"""


# 一次调用的输出预留：判定批次通常 300-700 token，留 900 足够，且不会让
# 「估算通过、实际超支」成为常态。
DEFAULT_OUTPUT_RESERVE = 900


@dataclass
class TokenBudget:
    per_run: int = 0
    per_day: int = 0
    used_run: int = 0
    used_today: int = 0
    on_exceed: str = "degrade"
    output_reserve: int = DEFAULT_OUTPUT_RESERVE
    calls: int = 0
    skipped: int = 0
    events: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def from_config(
        cls,
        cfg: dict[str, Any],
        used_today: int = 0,
        on_exceed: str | None = None,
    ) -> "TokenBudget":
        budget = (cfg or {}).get("budget") or {}
        return cls(
            per_run=int(budget.get("tokens_per_run") or 0),
            per_day=int(budget.get("tokens_per_day") or 0),
            used_today=max(0, int(used_today or 0)),
            on_exceed=str(on_exceed or budget.get("on_exceed") or "degrade"),
            output_reserve=int(budget.get("output_reserve_tokens") or DEFAULT_OUTPUT_RESERVE),
        )

    # ---------------- 额度 ----------------
    def remaining(self) -> int | None:
        """返回剩余 token；None 表示不设上限。"""
        caps: list[int] = []
        if self.per_run > 0:
            caps.append(self.per_run - self.used_run)
        if self.per_day > 0:
            caps.append(self.per_day - self.used_today)
        if not caps:
            return None
        return max(0, min(caps))

    def can(self, estimate: int) -> bool:
        left = self.remaining()
        if left is None:
            return True
        return left >= max(0, int(estimate))

    def guard(self, stage: str, estimate: int) -> bool:
        """调用前的准入判断。stop 模式直接抛错，degrade 模式返回 False。"""
        need = int(estimate) + self.output_reserve
        if self.can(need):
            return True
        self.note_skip(stage, need)
        if self.on_exceed == "stop":
            raise BudgetExceeded(
                f"预算不足（{stage}）：需要约 {need} token，剩余 {self.remaining()}；"
                f"本轮已用 {self.used_run}，今日已用 {self.used_today}"
            )
        return False

    def note_skip(self, stage: str, need: int) -> None:
        self.skipped += 1
        self.events.append({
            "stage": stage,
            "action": "skip",
            "need_tokens": int(need),
            "remaining": self.remaining(),
        })

    # ---------------- 记账 ----------------
    def charge(self, prompt_tokens: int, completion_tokens: int) -> int:
        total = max(0, int(prompt_tokens or 0)) + max(0, int(completion_tokens or 0))
        self.used_run += total
        self.used_today += total
        self.calls += 1
        return total

    def rollback(self, tokens: int) -> None:
        """调用失败但已记账时回滚，避免「失败了也算钱」。"""
        tokens = max(0, int(tokens or 0))
        self.used_run = max(0, self.used_run - tokens)
        self.used_today = max(0, self.used_today - tokens)

    # ---------------- 观测 ----------------
    def summary(self) -> dict[str, Any]:
        return {
            "used_run": self.used_run,
            "used_today": self.used_today,
            "per_run": self.per_run,
            "per_day": self.per_day,
            "remaining": self.remaining(),
            "calls": self.calls,
            "skipped": self.skipped,
            "on_exceed": self.on_exceed,
            "exhausted": self.remaining() == 0,
        }

    def describe(self) -> str:
        left = self.remaining()
        cap = f"/{self.per_run}" if self.per_run else ""
        day = f"（今日 {self.used_today}/{self.per_day}）" if self.per_day else ""
        left_s = "不限" if left is None else str(left)
        return f"本轮 {self.used_run}{cap} token，剩余 {left_s}{day}"
