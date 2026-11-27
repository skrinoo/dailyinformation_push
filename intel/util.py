"""Small stdlib-only helpers: HTTP, text, time, similarity."""
from __future__ import annotations

import gzip
import hashlib
import html
import json
import math
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

# 不少中文站（量子位、阮一峰）对非浏览器 UA 直接 403，RSS 抓取按浏览器 UA 走。
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


class HttpError(RuntimeError):
    pass


def http_get(url: str, headers: dict | None = None, timeout: int = 25) -> bytes:
    last: Exception | None = None
    for attempt in range(2):
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Encoding": "gzip", **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                return raw
        except urllib.error.HTTPError as exc:
            body = exc.read()[:300].decode("utf-8", "ignore")
            last = HttpError(f"HTTP {exc.code} for {url}: {body}")
            if exc.code < 500:
                break
        except (urllib.error.URLError, socket.timeout) as exc:
            last = HttpError(f"network error for {url}: {exc}")
        if attempt == 0:
            import time as _time

            _time.sleep(1.5)
    raise last or HttpError(f"failed to fetch {url}")


def http_get_text(url: str, headers: dict | None = None, timeout: int = 25) -> str:
    raw = http_get(url, headers=headers, timeout=timeout)
    return raw.decode("utf-8", "replace")


def http_post_json(url: str, payload: dict, headers: dict | None = None, timeout: int = 25) -> dict:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json", "User-Agent": UA, **(headers or {})},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
            return json.loads(body) if body.strip() else {}
    except urllib.error.HTTPError as exc:
        body = exc.read()[:400].decode("utf-8", "replace")
        raise HttpError(f"HTTP {exc.code} for {url}: {body}") from exc
    except (urllib.error.URLError, socket.timeout) as exc:
        raise HttpError(f"network error for {url}: {exc}") from exc


TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")


def strip_html(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", text)
    text = TAG_RE.sub(" ", text)
    text = html.unescape(text)
    return WS_RE.sub(" ", text).strip()


def clip(text: str, n: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def sha1(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    value = value.strip()
    try:
        dt = parsedate_to_datetime(value)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError, IndexError):
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(value, fmt)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def hours_since(dt: datetime | None) -> float:
    if dt is None:
        return 72.0
    return max(0.0, (utcnow() - dt).total_seconds() / 3600.0)


LATIN_RE = re.compile(r"[a-z0-9_+#.\-]+")
CJK_RE = re.compile(r"[\u4e00-\u9fff]")


def tokenize(text: str) -> set[str]:
    """Latin words + CJK bigrams (cheap, language-agnostic near-dup detection)."""
    text = (text or "").lower()
    tokens = set(LATIN_RE.findall(text))
    cjk = CJK_RE.findall(text)
    tokens.update("".join(cjk[i : i + 2]) for i in range(len(cjk) - 1))
    return {t for t in tokens if len(t) > 1}


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    return inter / float(len(a) + len(b) - inter)


def freshness(hours: float, half_life: float = 72.0) -> float:
    """1.0 at 0h, 0.5 at half_life, asymptotically 0."""
    return math.exp(-math.log(2) * hours / max(1.0, half_life))


TRACKING_PREFIXES = ("utm_", "spm", "ref_", "mc_", "pk_")
TRACKING_KEYS = {"ref", "source", "from", "share", "fbclid", "gclid", "igshid", "utm"}


def canonical_url(url: str) -> str:
    """Strip tracking params / fragments / trailing slash so the same article collapses to one key."""
    if not url:
        return ""
    url = url.strip()
    if url.startswith("//"):
        url = "https:" + url
    parsed = urllib.parse.urlsplit(url)
    host = (parsed.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    query = []
    for key, value in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True):
        low = key.lower()
        if low in TRACKING_KEYS or low.startswith(TRACKING_PREFIXES):
            continue
        query.append((key, value))
    path = parsed.path.rstrip("/") or "/"
    netloc = host
    if parsed.port and parsed.port not in (80, 443):
        netloc = f"{host}:{parsed.port}"
    return urllib.parse.urlunsplit((parsed.scheme.lower() or "https", netloc, path, urllib.parse.urlencode(query), ""))


def now_local(tz_name: str = "Asia/Shanghai") -> datetime:
    try:
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo(tz_name))
    except Exception:
        return datetime.now(timezone.utc).astimezone()


def sleep_until_next(times: list[str], tz_name: str) -> int:
    """Seconds until the next HH:MM slot (used by the schedule loop)."""
    now = now_local(tz_name)
    slots = []
    for value in times:
        try:
            hour, minute = (int(x) for x in value.split(":", 1))
        except ValueError:
            continue
        candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= now:
            candidate += timedelta(days=1)
        slots.append(candidate)
    if not slots:
        return 3600
    return max(30, int((min(slots) - now).total_seconds()))
