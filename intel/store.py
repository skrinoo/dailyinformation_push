"""SQLite 状态库。

各表分工，不要混：
  items      — 候选与判定结果（全局去重缓存 + 最近一次判定，可回查「为什么没推」）
  topic_items— 按关注项记录「这一项今天收过什么」（简报与历史都读它）
  events     — L2 事件簇（跨天去重的依据）
  watermark  — 每源水位线；多关注项时键名带 topic 前缀，互不推进
  seen_urls  — 按关注项的「URL 已知」集合。全局 items 只保留最近一次判定，
               不能拿来判断「这个关注项见过没有」，否则 A 项看过的东西 B 项永远收不到
  pushes     — 投递幂等（同一 URL 30 天内不重复推，跨关注项也去重）
  runs       — 每轮运行；带 topic 列，历史按关注项查
  tasks_out  — 已写入飞书待办的条目（幂等，避免重复建任务）

WAL 模式是为了让 CLI 反馈写入与定时任务读取不互相阻塞。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import feedback

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id            TEXT PRIMARY KEY,
    canonical_url TEXT UNIQUE,
    topic         TEXT DEFAULT '',
    title         TEXT NOT NULL,
    url           TEXT NOT NULL,
    source        TEXT,
    published_at  TEXT,
    headline      TEXT,
    summary       TEXT,
    points        TEXT,
    reason        TEXT,
    change_type   TEXT,
    gates         TEXT,
    score         REAL,
    confidence    REAL,
    verdict       TEXT,
    reject_reason TEXT,
    cluster_id    TEXT,
    channel       TEXT,
    tags          TEXT,
    entities      TEXT,
    first_seen    TEXT,
    pushed_at     TEXT,
    feedback      INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_items_cluster ON items(cluster_id);
CREATE INDEX IF NOT EXISTS idx_items_first_seen ON items(first_seen);

CREATE TABLE IF NOT EXISTS events (
    id         TEXT PRIMARY KEY,
    entity_key TEXT NOT NULL,
    title      TEXT,
    first_seen TEXT,
    last_seen  TEXT,
    item_count INTEGER DEFAULT 0,
    pushed_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_key ON events(entity_key, last_seen);

CREATE TABLE IF NOT EXISTS watermark (
    source       TEXT PRIMARY KEY,
    last_seen_at TEXT,
    last_run_at  TEXT,
    cursor       TEXT
);

CREATE TABLE IF NOT EXISTS pushes (
    canonical_url TEXT NOT NULL,
    channel       TEXT,
    pushed_at     TEXT,
    run_id        TEXT,
    PRIMARY KEY (canonical_url, channel)
);
CREATE INDEX IF NOT EXISTS idx_pushes_time ON pushes(pushed_at);

CREATE TABLE IF NOT EXISTS runs (
    id          TEXT PRIMARY KEY,
    topic       TEXT DEFAULT '',
    started_at  TEXT,
    finished_at TEXT,
    fetched     INTEGER,
    after_dedupe INTEGER,
    clustered   INTEGER,
    gated       INTEGER,
    kept        INTEGER,
    pushed      INTEGER,
    status      TEXT,
    note        TEXT,
    cost_usd    REAL DEFAULT 0,
    tokens      INTEGER DEFAULT 0
);

-- 关注项维度的收录记录。items 全局唯一（同一 URL 只留最近一次判定），
-- 想知道「A 项今天收过哪几条」必须查这张表。
CREATE TABLE IF NOT EXISTS topic_items (
    topic         TEXT NOT NULL,
    canonical_url TEXT NOT NULL,
    item_id       TEXT,
    title         TEXT,
    url           TEXT,
    source        TEXT,
    headline      TEXT,
    summary       TEXT,
    points        TEXT,
    reason        TEXT,
    score         REAL,
    confidence    REAL,
    verdict       TEXT,
    channel       TEXT,
    run_id        TEXT,
    first_seen    TEXT,
    PRIMARY KEY (topic, canonical_url)
);
CREATE INDEX IF NOT EXISTS idx_topic_items_time ON topic_items(topic, first_seen);

-- 关注项维度的「URL 已知」。没有它，多关注项会互相截胡。
CREATE TABLE IF NOT EXISTS seen_urls (
    topic         TEXT NOT NULL,
    canonical_url TEXT NOT NULL,
    first_seen    TEXT,
    PRIMARY KEY (topic, canonical_url)
);

-- 飞书待办幂等：同一 URL 只建一次；重跑、补推都不会重复建。
CREATE TABLE IF NOT EXISTS tasks_out (
    canonical_url TEXT NOT NULL,
    channel       TEXT NOT NULL,
    task_guid     TEXT,
    summary       TEXT,
    created_at    TEXT,
    PRIMARY KEY (canonical_url, channel)
);

CREATE TABLE IF NOT EXISTS ai_plans (
    key        TEXT PRIMARY KEY,
    payload    TEXT,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS source_stats (
    source TEXT PRIMARY KEY,
    shown  INTEGER DEFAULT 0,
    up     INTEGER DEFAULT 0,
    down   INTEGER DEFAULT 0,
    opens  INTEGER DEFAULT 0,
    weight REAL DEFAULT 1.0
);

CREATE TABLE IF NOT EXISTS feedback_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id    TEXT,
    action     TEXT,
    topic      TEXT DEFAULT '',
    reason     TEXT DEFAULT '',
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS topic_source_stats (
    topic  TEXT,
    source TEXT,
    shown  INTEGER DEFAULT 0,
    up     INTEGER DEFAULT 0,
    down   INTEGER DEFAULT 0,
    opens  INTEGER DEFAULT 0,
    weight REAL DEFAULT 1.0,
    PRIMARY KEY (topic, source)
);

CREATE TABLE IF NOT EXISTS page_baselines (
    url        TEXT PRIMARY KEY,
    digest     TEXT,
    text       TEXT,
    updated_at TEXT
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, db_path: str | Path):
        path = Path(db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), timeout=15)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        """老库缺列时补上。SQLite 不支持 IF NOT EXISTS 加列，只能先看表结构。"""
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(runs)").fetchall()}
        if "tokens" not in cols:
            self.conn.execute("ALTER TABLE runs ADD COLUMN tokens INTEGER DEFAULT 0")
        if "topic" not in cols:
            self.conn.execute("ALTER TABLE runs ADD COLUMN topic TEXT DEFAULT ''")
        item_cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(items)").fetchall()}
        if "topic" not in item_cols:
            self.conn.execute("ALTER TABLE items ADD COLUMN topic TEXT DEFAULT ''")
        for col, ddl in (("headline", "TEXT"), ("summary", "TEXT"), ("points", "TEXT")):
            if col not in item_cols:
                self.conn.execute(f"ALTER TABLE items ADD COLUMN {col} {ddl}")
        topic_cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(topic_items)").fetchall()}
        if topic_cols and "reason" not in topic_cols:
            self.conn.execute("ALTER TABLE topic_items ADD COLUMN reason TEXT")
        for col in ("headline", "summary", "points"):
            if topic_cols and col not in topic_cols:
                self.conn.execute(f"ALTER TABLE topic_items ADD COLUMN {col} TEXT")
        fb_cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(feedback_log)").fetchall()}
        if fb_cols and "topic" not in fb_cols:
            self.conn.execute("ALTER TABLE feedback_log ADD COLUMN topic TEXT DEFAULT ''")
        if fb_cols and "reason" not in fb_cols:
            self.conn.execute("ALTER TABLE feedback_log ADD COLUMN reason TEXT DEFAULT ''")
        # 老库升级：把已有条目补进 seen_urls 的空关注项分区，保持「已知」语义不丢
        self.conn.execute(
            """INSERT OR IGNORE INTO seen_urls (topic, canonical_url, first_seen)
               SELECT '', canonical_url, first_seen FROM items WHERE canonical_url != ''"""
        )

    # ---------------- 关注项维度 ----------------
    def knows_url(self, canonical_url: str, topic: str = "") -> bool:
        """topic 为空时看全局 items（单关注项的老路径，行为不变）；
        指定 topic 时只看该关注项自己的已知集合。"""
        if not canonical_url:
            return False
        if topic:
            row = self.conn.execute(
                "SELECT 1 FROM seen_urls WHERE topic = ? AND canonical_url = ? LIMIT 1",
                (topic, canonical_url),
            ).fetchone()
            return row is not None
        row = self.conn.execute(
            "SELECT 1 FROM items WHERE canonical_url = ?", (canonical_url,)
        ).fetchone()
        return row is not None

    def mark_seen(self, urls: list[str], topic: str) -> None:
        """本轮处理过的 URL 全部登记。被淘汰的也要登记，否则下一轮还会重跑完整链路。"""
        now = _now()
        self.conn.executemany(
            "INSERT OR IGNORE INTO seen_urls (topic, canonical_url, first_seen) VALUES (?,?,?)",
            [(topic, u, now) for u in urls if u],
        )
        self.conn.commit()

    def save_topic_item(self, topic: str, item: Any, run_id: str) -> None:
        if not topic:
            return
        self.conn.execute(
            """INSERT INTO topic_items (topic, canonical_url, item_id, title, url, source,
                                        headline, summary, points, score, confidence, verdict,
                                        channel, run_id, first_seen)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(topic, canonical_url) DO UPDATE SET
                 item_id = excluded.item_id,
                 title = excluded.title,
                 headline = excluded.headline,
                 summary = excluded.summary,
                 points = excluded.points,
                 score = excluded.score,
                 confidence = excluded.confidence,
                 verdict = excluded.verdict,
                 channel = excluded.channel,
                 run_id = excluded.run_id""",
            (
                topic, item.canonical_url, item.id, item.shown_title, item.url, item.source,
                item.headline, item.summary,
                json.dumps(item.points or [], ensure_ascii=False),
                float(item.score or 0), float(item.confidence or 0), item.verdict,
                item.channel, run_id, _now(),
            ),
        )
        self.conn.commit()

    def topic_items(self, start_iso: str, end_iso: str = "", topic: str = "",
                    only_picked: bool = True, limit: int = 200) -> list[dict[str, Any]]:
        """按时间窗取关注项收录记录，简报用。"""
        sql = ("SELECT * FROM topic_items WHERE first_seen >= ?")
        args: list[Any] = [start_iso]
        if end_iso:
            sql += " AND first_seen < ?"
            args.append(end_iso)
        if topic:
            sql += " AND topic = ?"
            args.append(topic)
        if only_picked:
            sql += " AND verdict = 'selected'"
        sql += " ORDER BY score DESC, first_seen DESC LIMIT ?"
        args.append(int(limit))
        return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def topic_runs(self, topic: str, limit: int = 10) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM runs WHERE topic = ? AND status != 'running' "
            "ORDER BY started_at DESC, rowid DESC LIMIT ?", (topic, limit)
        ).fetchall()
        return [dict(r) for r in rows]

    def runs_between(self, start_iso: str, end_iso: str = "") -> list[dict[str, Any]]:
        sql = "SELECT * FROM runs WHERE started_at >= ?"
        args: list[Any] = [start_iso]
        if end_iso:
            sql += " AND started_at < ?"
            args.append(end_iso)
        sql += " ORDER BY started_at ASC, rowid ASC"
        return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    # ---------------- 飞书待办幂等 ----------------
    def task_exists(self, canonical_url: str, channel: str = "feishu_task") -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM tasks_out WHERE canonical_url = ? AND channel = ? LIMIT 1",
            (canonical_url, channel),
        ).fetchone()
        return row is not None

    def log_task(self, canonical_url: str, task_guid: str, summary: str,
                 channel: str = "feishu_task") -> None:
        self.conn.execute(
            """INSERT INTO tasks_out (canonical_url, channel, task_guid, summary, created_at)
               VALUES (?,?,?,?,?)
               ON CONFLICT(canonical_url, channel) DO UPDATE SET
                 task_guid = excluded.task_guid, summary = excluded.summary""",
            (canonical_url, channel, task_guid, summary, _now()),
        )
        self.conn.commit()

    def tasks_between(self, start_iso: str, end_iso: str = "") -> list[dict[str, Any]]:
        sql = "SELECT * FROM tasks_out WHERE created_at >= ?"
        args: list[Any] = [start_iso]
        if end_iso:
            sql += " AND created_at < ?"
            args.append(end_iso)
        sql += " ORDER BY created_at ASC"
        return [dict(r) for r in self.conn.execute(sql, args).fetchall()]


    # ---------------- 水位线 ----------------
    def get_watermark(self, source: str) -> dict[str, str]:
        row = self.conn.execute(
            "SELECT last_seen_at, last_run_at, cursor FROM watermark WHERE source = ?", (source,)
        ).fetchone()
        return dict(row) if row else {"last_seen_at": "", "last_run_at": "", "cursor": ""}

    def set_watermark(self, source: str, last_seen_at: str, cursor: str = "") -> None:
        self.conn.execute(
            """INSERT INTO watermark (source, last_seen_at, last_run_at, cursor) VALUES (?,?,?,?)
               ON CONFLICT(source) DO UPDATE SET
                 last_seen_at = CASE
                    WHEN excluded.last_seen_at > watermark.last_seen_at THEN excluded.last_seen_at
                    ELSE watermark.last_seen_at END,
                 last_run_at = excluded.last_run_at,
                 cursor = COALESCE(NULLIF(excluded.cursor,''), watermark.cursor)""",
            (source, last_seen_at or "", _now(), cursor),
        )
        self.conn.commit()

    # ---------------- items ----------------
    def pushed_within(self, canonical_url: str, hours: int) -> bool:
        since = _iso(datetime.now(timezone.utc) - timedelta(hours=hours))
        row = self.conn.execute(
            "SELECT 1 FROM pushes WHERE canonical_url = ? AND pushed_at >= ? LIMIT 1",
            (canonical_url, since),
        ).fetchone()
        return row is not None

    def save_item(self, item: dict[str, Any], topic: str = "") -> None:
        self.conn.execute(
            """INSERT INTO items (id, canonical_url, topic, title, url, source, published_at,
                                  headline, summary, points, reason, change_type, gates, score,
                                  confidence, verdict, reject_reason, cluster_id, channel, tags,
                                  entities, first_seen)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(canonical_url) DO UPDATE SET
                 topic=COALESCE(NULLIF(items.topic,''), excluded.topic),
                 headline=COALESCE(NULLIF(excluded.headline,''), items.headline),
                 summary=COALESCE(NULLIF(excluded.summary,''), items.summary),
                 points=COALESCE(NULLIF(excluded.points,''), items.points),
                 change_type=excluded.change_type,
                 gates=excluded.gates,
                 score=excluded.score,
                 confidence=excluded.confidence,
                 verdict=excluded.verdict,
                 reject_reason=excluded.reject_reason,
                 cluster_id=excluded.cluster_id,
                 channel=excluded.channel,
                 tags=excluded.tags,
                 entities=excluded.entities""",
            (
                item["id"],
                item["canonical_url"],
                topic,
                item["title"],
                item["url"],
                item.get("source", ""),
                item.get("published_at", ""),
                item.get("headline", ""),
                item.get("summary", ""),
                json.dumps(item.get("points") or [], ensure_ascii=False),
                item.get("reason", ""),
                item.get("change_type", ""),
                json.dumps(item.get("gates") or {}, ensure_ascii=False),
                float(item.get("score") or 0),
                float(item.get("confidence") or 0),
                item.get("verdict", ""),
                item.get("reject_reason", ""),
                item.get("cluster_id", ""),
                item.get("channel", ""),
                ",".join(item.get("tags") or []),
                json.dumps(item.get("entities") or [], ensure_ascii=False),
                _now(),
            ),
        )
        self.conn.commit()

    def mark_pushed(self, canonical_urls: list[str], channel: str, run_id: str) -> None:
        now = _now()
        self.conn.executemany(
            "INSERT OR REPLACE INTO pushes (canonical_url, channel, pushed_at, run_id) VALUES (?,?,?,?)",
            [(u, channel, now, run_id) for u in canonical_urls],
        )
        self.conn.execute(
            "UPDATE items SET pushed_at = ? WHERE canonical_url IN (%s)"
            % ",".join("?" * len(canonical_urls)),
            (now, *canonical_urls),
        )
        self.conn.commit()

    # ---------------- events ----------------
    def find_recent_event(self, entity_key: str, window_hours: int) -> dict[str, Any] | None:
        since = _iso(datetime.now(timezone.utc) - timedelta(hours=window_hours))
        row = self.conn.execute(
            "SELECT * FROM events WHERE entity_key = ? AND last_seen >= ? ORDER BY last_seen DESC LIMIT 1",
            (entity_key, since),
        ).fetchone()
        return dict(row) if row else None

    def upsert_event(self, event_id: str, entity_key: str, title: str, item_count: int) -> None:
        now = _now()
        self.conn.execute(
            """INSERT INTO events (id, entity_key, title, first_seen, last_seen, item_count)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                 last_seen = excluded.last_seen,
                 item_count = events.item_count + excluded.item_count,
                 title = COALESCE(NULLIF(excluded.title,''), events.title)""",
            (event_id, entity_key, title, now, now, item_count),
        )
        self.conn.commit()

    def event_history(self, entity_key: str, limit: int = 3) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM events WHERE entity_key = ? ORDER BY last_seen DESC LIMIT ?",
            (entity_key, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    # ---------------- runs ----------------
    def start_run(self, run_id: str, topic: str = "") -> None:
        self.conn.execute(
            "INSERT INTO runs (id, topic, started_at, status) VALUES (?,?,?,?)",
            (run_id, topic, _now(), "running"),
        )
        self.conn.commit()

    def finish_run(self, run_id: str, **fields: Any) -> None:
        allowed = {
            "fetched", "after_dedupe", "clustered", "gated", "kept",
            "pushed", "status", "note", "cost_usd", "tokens",
        }
        cols = [k for k in fields if k in allowed]
        sets = ", ".join(f"{c} = ?" for c in cols + ["finished_at"])
        vals = [fields[c] for c in cols] + [_now(), run_id]
        self.conn.execute(f"UPDATE runs SET {sets} WHERE id = ?", vals)
        self.conn.commit()

    def set_run_tokens(self, run_id: str, tokens: int) -> None:
        """运行中途也要落一次：进程被杀时，已烧掉的 token 不能从账上消失。"""
        self.conn.execute("UPDATE runs SET tokens = ? WHERE id = ?", (int(tokens), run_id))
        self.conn.commit()

    def tokens_today(self, tz_name: str = "Asia/Shanghai") -> int:
        """今日已用 token（含正在跑的）。跨时区按用户本地日切。"""
        from .util import now_local

        local_now = now_local(tz_name)
        start_local = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        since = _iso(start_local)
        row = self.conn.execute(
            "SELECT COALESCE(SUM(tokens),0) AS t FROM runs WHERE started_at >= ?", (since,)
        ).fetchone()
        return int(row["t"] or 0)

    # ---------------- AI 检索策略缓存 ----------------
    def get_plan_cache(self, key: str, ttl_hours: int = 24) -> dict[str, Any] | None:
        since = _iso(datetime.now(timezone.utc) - timedelta(hours=max(1, ttl_hours)))
        row = self.conn.execute(
            "SELECT payload, created_at FROM ai_plans WHERE key = ? AND created_at >= ?",
            (key, since),
        ).fetchone()
        if not row:
            return None
        try:
            data = json.loads(row["payload"])
        except (TypeError, json.JSONDecodeError):
            return None
        data["cached_at"] = row["created_at"]
        return data

    def save_plan_cache(self, key: str, plan: dict[str, Any]) -> None:
        self.conn.execute(
            """INSERT INTO ai_plans (key, payload, created_at) VALUES (?,?,?)
               ON CONFLICT(key) DO UPDATE SET
                 payload = excluded.payload, created_at = excluded.created_at""",
            (key, json.dumps(plan, ensure_ascii=False), _now()),
        )
        self.conn.commit()

    def recent_runs(self, limit: int = 5) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            # rowid 是次级键：同一秒内多次运行（测试、手动重跑）时，
            # 只按 started_at 排序会让顺序不稳定，心跳计数就会读错。
            "SELECT * FROM runs WHERE status != 'running' "
            "ORDER BY started_at DESC, rowid DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    def consecutive_empty_runs(self) -> int:
        """从最近一次往回数，连续 kept=0 的次数。用于零产出心跳。"""
        count = 0
        for run in self.recent_runs(limit=10):
            if int(run.get("kept") or 0) == 0:
                count += 1
            else:
                break
        return count

    # ---------------- 反馈 ----------------
    def add_feedback(self, item_id: str, action: str, topic: str = "",
                     reason: str = "") -> dict[str, Any]:
        """记录一次反馈并调整来源权重。

        作用域：显式 topic > 条目自身所属关注项 > 全局。
        按关注项记录时只动 topic_source_stats，全局 weight 不受污染——
        否则「投资人爱看融资」会把所有主题的融资权重一起抬上去。

        reason 只在 down 时生效，且不是纯标签：它决定乘数（见 feedback.py）。
        「重复」不扣来源，「来源不可信」扣得最狠——把六种「没用」压成同一个
        0.90，等于把选题问题和内容质量问题算成同一笔账。
        """
        factor = {"up": 1.08, "down": None, "save": 1.04, "open": 1.02}.get(action)
        if action not in ("up", "down", "save", "open"):
            raise ValueError("action 必须是 up / down / save / open 之一")
        reason = (reason or "").strip()
        if reason and action != "down":
            raise ValueError("只有「没用」需要选原因")
        if action == "down":
            factor = feedback.down_factor(reason)   # 未选原因时仍是 0.90
        row = self.conn.execute("SELECT source, topic FROM items WHERE id = ?", (item_id,)).fetchone()
        if not row:
            raise KeyError(f"未知条目 id：{item_id}")
        source = row["source"] or "unknown"
        scope = (topic or row["topic"] or "").strip()

        self.conn.execute(
            "UPDATE items SET feedback = ? WHERE id = ?",
            ({"up": 1, "down": -1, "save": 2, "open": 0}[action], item_id),
        )
        self.conn.execute(
            "INSERT INTO feedback_log (item_id, action, topic, reason, created_at) VALUES (?,?,?,?,?)",
            (item_id, action, scope, reason, _now()),
        )

        up = 1 if action == "up" else 0
        down = 1 if action == "down" else 0
        opens = 1 if action == "open" else 0
        if scope:
            # 隐式信号只做弱更新（open 是点击，不等于认可）
            self.conn.execute(
                "INSERT INTO topic_source_stats (topic, source) VALUES (?,?) "
                "ON CONFLICT(topic, source) DO NOTHING", (scope, source))
            self.conn.execute(
                """UPDATE topic_source_stats SET
                     up = up + ?, down = down + ?, opens = opens + ?,
                     weight = MIN(2.0, MAX(0.5, weight * ?))
                   WHERE topic = ? AND source = ?""",
                (up, down, opens, factor, scope, source),
            )
            self.conn.commit()
            w = self.conn.execute(
                "SELECT weight FROM topic_source_stats WHERE topic = ? AND source = ?",
                (scope, source)).fetchone()
            return {"item_id": item_id, "source": source, "action": action, "scope": scope,
                    "reason": reason, "factor": round(float(factor), 3),
                    "reason_label": feedback.label(reason) if reason else "",
                    "topic_weight": round(float(w["weight"]), 3),
                    "effective_weight": round(self.source_weights(scope).get(source, 1.0), 3)}

        self.conn.execute(
            "INSERT INTO source_stats (source) VALUES (?) ON CONFLICT(source) DO NOTHING", (source,)
        )
        self.conn.execute(
            """UPDATE source_stats SET
                 up    = up    + ?,
                 down  = down  + ?,
                 opens = opens + ?,
                 weight = MIN(2.0, MAX(0.5, weight * ?))
               WHERE source = ?""",
            (up, down, opens, factor, source),
        )
        self.conn.commit()
        w = self.conn.execute("SELECT weight FROM source_stats WHERE source = ?", (source,)).fetchone()
        return {"item_id": item_id, "source": source, "action": action, "scope": "",
                "reason": reason, "factor": round(float(factor), 3),
                "reason_label": feedback.label(reason) if reason else "",
                "source_weight": round(float(w["weight"]), 3),
                "effective_weight": round(float(w["weight"]), 3)}

    def recent_items(self, limit: int = 30, topic: str = "") -> list[dict[str, Any]]:
        """控制台「反馈」页用：最近收录过的条目 + 最近一次反馈动作。"""
        sql = """SELECT i.id, i.title, i.url, i.source, i.topic, i.channel, i.verdict,
                        i.confidence, i.first_seen,
                        (SELECT f.action FROM feedback_log f WHERE f.item_id = i.id
                          ORDER BY f.id DESC LIMIT 1) AS last_action,
                        (SELECT f.topic FROM feedback_log f WHERE f.item_id = i.id
                          ORDER BY f.id DESC LIMIT 1) AS last_topic,
                        (SELECT f.reason FROM feedback_log f WHERE f.item_id = i.id
                          ORDER BY f.id DESC LIMIT 1) AS last_reason
                   FROM items i"""
        args: list[Any] = []
        if topic:
            sql += " WHERE i.topic = ?"
            args.append(topic)
        sql += " ORDER BY i.first_seen DESC LIMIT ?"
        args.append(max(1, min(200, int(limit))))
        weights = self.source_weights(topic)
        per_topic: dict[str, dict[str, float]] = {}
        out = []
        for r in self.conn.execute(sql, args).fetchall():
            row = dict(r)
            own = row["topic"] or ""
            if own not in per_topic:
                per_topic[own] = weights if own == topic else self.source_weights(own)
            row["source_weight"] = round(per_topic[own].get(row["source"], 1.0), 3)
            out.append(row)
        return out

    def source_weights(self, topic: str = "") -> dict[str, float]:
        """全局权重；给定 topic 时再乘上该关注项自己的系数（夹在 0.5–2.5）。"""
        out = {
            r["source"]: float(r["weight"])
            for r in self.conn.execute("SELECT source, weight FROM source_stats").fetchall()
        }
        if not topic:
            return out
        for r in self.conn.execute(
                "SELECT source, weight FROM topic_source_stats WHERE topic = ?", (topic,)).fetchall():
            eff = out.get(r["source"], 1.0) * float(r["weight"])
            out[r["source"]] = min(2.5, max(0.5, eff))
        return out

    def topic_weights(self, topic: str) -> list[dict[str, Any]]:
        """该关注项下每个来源的权重明细，控制台展示用。"""
        base = {r["source"]: float(r["weight"])
                for r in self.conn.execute("SELECT source, weight FROM source_stats")}
        rows = self.conn.execute(
            "SELECT source, up, down, weight FROM topic_source_stats WHERE topic = ? "
            "ORDER BY weight DESC", (topic,)).fetchall()
        return [{"source": r["source"], "up": r["up"], "down": r["down"],
                 "topic_weight": round(float(r["weight"]), 3),
                 "global_weight": round(base.get(r["source"], 1.0), 3),
                 "effective_weight": round(min(2.5, max(0.5, base.get(r["source"], 1.0)
                                                      * float(r["weight"]))), 3)}
                for r in rows]

    def reason_stats(self, topic: str = "", days: int = 30) -> list[dict[str, Any]]:
        """「为什么没用」的分布。反馈入口只剩控制台，这张表是它唯一的产出。

        只统计 down；老库没有 reason 的历史行算进「没选原因」。
        """
        since = (datetime.now(timezone.utc) - timedelta(days=max(1, int(days)))).isoformat()
        sql = ("SELECT reason, COUNT(*) AS n FROM feedback_log "
               "WHERE action = 'down' AND created_at >= ?")
        args: list[Any] = [since]
        if topic:
            sql += " AND topic = ?"
            args.append(topic)
        sql += " GROUP BY reason ORDER BY n DESC"
        rows = self.conn.execute(sql, args).fetchall()
        total = sum(int(r["n"]) for r in rows)
        out = []
        for r in rows:
            code = r["reason"] or ""
            out.append({"code": code,
                        "label": feedback.label(code) if code else "没选原因",
                        "count": int(r["n"]),
                        "share": round(int(r["n"]) / total, 3) if total else 0.0})
        return out

    def record_shown(self, sources: list[str]) -> None:
        for source in {s for s in sources if s}:
            self.conn.execute(
                "INSERT INTO source_stats (source, shown) VALUES (?,1) "
                "ON CONFLICT(source) DO UPDATE SET shown = shown + 1",
                (source,),
            )
        self.conn.commit()

    def stats(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT source, shown, up, down, opens, weight FROM source_stats ORDER BY weight DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    # ---------------- Page Monitor 基线 ----------------
    def get_page_baseline(self, url: str) -> dict[str, str] | None:
        row = self.conn.execute(
            "SELECT url, digest, text, updated_at FROM page_baselines WHERE url = ?", (url,)
        ).fetchone()
        return dict(row) if row else None

    def save_page_baseline(self, url: str, digest: str, text: str) -> None:
        self.conn.execute(
            """INSERT INTO page_baselines (url, digest, text, updated_at) VALUES (?,?,?,?)
               ON CONFLICT(url) DO UPDATE SET
                 digest = excluded.digest, text = excluded.text, updated_at = excluded.updated_at""",
            (url, digest, text, _now()),
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()
