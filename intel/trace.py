"""可观测层：trace_id + span + JSONL。

不引入 OpenTelemetry：单机 CLI 场景下，一行一个 span 的 JSONL 已经能被
`jq` / `grep` 直接消费，接入成本远低于搭一套 collector。真上规模时再把
span 结构平移过去即可（字段名刻意对齐 OTel 的 trace_id / span / duration）。

脱敏：URL 的敏感 query（token/key/code/sig）与邮箱一律打码后再落盘。
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

_SENSITIVE_QS = re.compile(r"((?:access_token|token|key|api_key|code|sig|signature)=)[^&]+", re.I)
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


def new_trace_id() -> str:
    return uuid.uuid4().hex[:16]


def redact(text: str, limit: int = 500) -> str:
    if not text:
        return ""
    out = _SENSITIVE_QS.sub(r"\1***", text)
    out = _EMAIL.sub("***@***", out)
    return out[:limit]


class Tracer:
    def __init__(self, trace_dir: str | Path, trace_id: str, enabled: bool = True):
        self.dir = Path(trace_dir)
        self.trace_id = trace_id
        self.enabled = enabled
        self.spans: list[dict[str, Any]] = []
        self._t0 = time.perf_counter()
        if self.enabled:
            self.dir.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def span(self, name: str, item_id: str = "", **attrs: Any) -> Iterator[dict[str, Any]]:
        start = time.perf_counter()
        payload: dict[str, Any] = {}
        error = ""
        try:
            yield payload
        except Exception as exc:  # 记录后原样抛出，不吞异常
            error = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            rec = {
                "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                "trace_id": self.trace_id,
                "span": name,
                "item_id": item_id,
                "ms": round((time.perf_counter() - start) * 1000, 1),
                "attrs": {k: (redact(v) if isinstance(v, str) else v) for k, v in attrs.items()},
                "out": {k: (redact(v) if isinstance(v, str) else v) for k, v in payload.items()},
                "err": error,
            }
            self.spans.append(rec)
            if self.enabled:
                day = datetime.now().strftime("%Y-%m-%d")
                with (self.dir / f"{day}.jsonl").open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def event(self, name: str, **attrs: Any) -> None:
        with self.span(name, **attrs):
            pass

    def summary(self) -> dict[str, Any]:
        total_ms = round((time.perf_counter() - self._t0) * 1000, 1)
        by_span: dict[str, dict[str, Any]] = {}
        for s in self.spans:
            agg = by_span.setdefault(s["span"], {"count": 0, "ms": 0.0, "errors": 0})
            agg["count"] += 1
            agg["ms"] = round(agg["ms"] + s["ms"], 1)
            agg["errors"] += 1 if s["err"] else 0
        return {"trace_id": self.trace_id, "total_ms": total_ms, "spans": by_span,
                "errors": sum(v["errors"] for v in by_span.values())}

    def dump(self) -> Path | None:
        if not self.enabled:
            return None
        path = self.dir / f"run-{self.trace_id}.json"
        path.write_text(json.dumps({"summary": self.summary(), "spans": self.spans},
                                   ensure_ascii=False, indent=2), encoding="utf-8")
        return path


def env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}
