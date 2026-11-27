"""反馈原因：一个「没用」要能说清是哪种没用。

只记 up/down 的二元反馈，学到的只有一个数：这个源 0.9 或 1.08。
但「没用」至少有六种成因，惩罚的对象并不相同——

  跑题、重复        → 是选题和去重的问题，不该重罚来源
  太浅、标题党      → 是内容质量问题，该压这个源
  来源不可信        → 最该压，一次就够狠
  太旧              → 是时效问题，轻微

所以原因不只是标签，它决定权重乘数。控制台选原因，库里落 reason 码，
粗排下一轮就用新的权重。
"""

from __future__ import annotations

# code, 中文, English, 权重乘数, 说明（控制台 tooltip）
REASONS: tuple[tuple[str, str, str, float, str], ...] = (
    ("off_topic", "跑题、不相关", "Off-topic", 0.96,
     "主题没对上，来源本身没错，轻微下调"),
    ("duplicate", "已经看过、重复", "Already seen", 1.00,
     "去重的问题，只记录不扣来源"),
    ("shallow", "太浅、没信息量", "Too shallow", 0.92,
     "内容质量问题，压低这个来源"),
    ("clickbait", "标题党、营销稿", "Clickbait", 0.88,
     "标题与正文不符，明显下调"),
    ("unreliable", "来源不可信", "Unreliable", 0.82,
     "最重的一档，一次就压下去"),
    ("stale", "太旧、不是新闻", "Too old", 0.97,
     "时效问题，轻微下调"),
)

_BY_CODE = {r[0]: r for r in REASONS}
DEFAULT_DOWN_FACTOR = 0.90          # 不选原因时的旧行为，保持不变


def codes() -> list[str]:
    return [r[0] for r in REASONS]


def exists(code: str) -> bool:
    return (code or "") in _BY_CODE


def label(code: str, lang: str = "zh") -> str:
    row = _BY_CODE.get(code or "")
    if not row:
        return code or ""
    return row[2] if lang == "en" else row[1]


def hint(code: str) -> str:
    row = _BY_CODE.get(code or "")
    return row[4] if row else ""


def down_factor(reason: str = "") -> float:
    """没选原因 → 老的 0.90；选了就按原因分档。"""
    if not reason:
        return DEFAULT_DOWN_FACTOR
    row = _BY_CODE.get(reason)
    if not row:
        raise ValueError(f"未知反馈原因：{reason}（可选 {', '.join(codes())}）")
    return row[3]


def options() -> list[dict[str, object]]:
    """给控制台和 CLI 的选项列表。"""
    return [{"code": c, "label": zh, "label_en": en, "factor": f, "hint": h}
            for c, zh, en, f, h in REASONS]
