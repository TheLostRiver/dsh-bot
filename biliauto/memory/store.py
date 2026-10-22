"""持久化：SQLite 存事件流、回复、观众画像、每日用量。

为什么需要它：
1. 配额必须跨进程重启保留，否则重启即绕过 SEND_PER_DAY —— 等于没有限流。
2. 观众画像（谁常来、谁爱聊什么）让回复不像随机复读机。
3. 出事后要能复盘：哪条弹幕、哪次回复、成功还是被风控拒绝。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        REAL NOT NULL,
    kind      TEXT NOT NULL,
    uid       INTEGER,
    uname     TEXT,
    text      TEXT,
    decision  TEXT,
    reason    TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);

CREATE TABLE IF NOT EXISTS replies (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        REAL NOT NULL,
    event_id  INTEGER,
    uid       INTEGER,
    danmaku   TEXT,
    speech    TEXT,
    sent_ok   INTEGER NOT NULL DEFAULT 0,
    error     TEXT
);

CREATE TABLE IF NOT EXISTS viewers (
    uid        INTEGER PRIMARY KEY,
    uname      TEXT,
    first_seen REAL,
    last_seen  REAL,
    msg_count  INTEGER NOT NULL DEFAULT 0,
    replied    INTEGER NOT NULL DEFAULT 0,
    note       TEXT
);

CREATE TABLE IF NOT EXISTS usage (
    day            TEXT PRIMARY KEY,
    danmaku_sent   INTEGER NOT NULL DEFAULT 0,
    llm_calls      INTEGER NOT NULL DEFAULT 0,
    speech_made    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- 评论区值班：见过的评论必须有记录，否则每轮扫描会重复回复
CREATE TABLE IF NOT EXISTS comments_seen (
    rpid    INTEGER PRIMARY KEY,
    oid     INTEGER NOT NULL,
    uid     INTEGER,
    uname   TEXT,
    text    TEXT,
    ctime   REAL,
    action  TEXT,
    reason  TEXT,
    seen_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_comments_oid ON comments_seen(oid);

CREATE TABLE IF NOT EXISTS comment_replies (
    rpid     INTEGER PRIMARY KEY,
    oid      INTEGER NOT NULL,
    uid      INTEGER,
    incoming TEXT,
    reply    TEXT,
    ok       INTEGER NOT NULL DEFAULT 0,
    error    TEXT,
    ts       REAL NOT NULL
);

-- 自动逛 B 站：看过哪些视频
CREATE TABLE IF NOT EXISTS browse_seen (
    bvid     TEXT PRIMARY KEY,
    aid      INTEGER,
    title    TEXT,
    up_mid   INTEGER,
    up_name  TEXT,
    duration INTEGER,
    play     INTEGER,
    pubdate  REAL,
    seen_at  REAL NOT NULL,
    verdict  TEXT,
    reason   TEXT
);

CREATE TABLE IF NOT EXISTS browse_actions (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    ts     REAL NOT NULL,
    bvid   TEXT,
    action TEXT,
    ok     INTEGER NOT NULL DEFAULT 0,
    error  TEXT
);
CREATE INDEX IF NOT EXISTS idx_browse_actions_ts ON browse_actions(ts);

-- 通用动作用量：以后加动作不用改表结构
CREATE TABLE IF NOT EXISTS action_usage (
    day    TEXT NOT NULL,
    action TEXT NOT NULL,
    count  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, action)
);
"""

# 老库升级：usage 表后加的列
USAGE_COLUMNS = ("danmaku_sent", "llm_calls", "speech_made", "comment_sent")


@dataclass(frozen=True)
class EventRecord:
    id: int
    ts: float
    kind: str
    uid: int
    uname: str
    text: str


class Store:
    """线程安全的小型 SQLite 封装（单连接 + 锁，够用且不引依赖）。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._migrate()
            self._conn.commit()

    def _migrate(self) -> None:
        """给老数据库补列。CREATE TABLE IF NOT EXISTS 不会改已存在的表。"""
        for col in USAGE_COLUMNS:
            try:
                self._conn.execute(
                    f"ALTER TABLE usage ADD COLUMN {col} INTEGER NOT NULL DEFAULT 0"
                )
            except sqlite3.OperationalError:
                pass  # 列已存在
        try:
            self._conn.execute("ALTER TABLE browse_seen ADD COLUMN source TEXT")
        except sqlite3.OperationalError:
            pass

    # ---------- 事件 ----------
    def log_event(
        self, kind: str, uid: int, uname: str, text: str, decision: str = "", reason: str = ""
    ) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO events (ts, kind, uid, uname, text, decision, reason)"
                " VALUES (?,?,?,?,?,?,?)",
                (time.time(), kind, uid, uname, text, decision, reason),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def set_event_decision(self, event_id: int, decision: str, reason: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE events SET decision=?, reason=? WHERE id=?",
                (decision, reason, event_id),
            )
            self._conn.commit()

    def recent_dialogue(self, limit: int = 12) -> list[EventRecord]:
        """最近参与对话的消息，用于给 LLM 提供上下文。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, ts, kind, uid, uname, text FROM events"
                " WHERE decision IN ('replied','considered') ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            EventRecord(
                id=r["id"], ts=r["ts"], kind=r["kind"],
                uid=r["uid"] or 0, uname=r["uname"] or "", text=r["text"] or "",
            )
            for r in reversed(rows)
        ]

    # ---------- 回复 ----------
    def log_reply(
        self, event_id: int, uid: int, danmaku: str, speech: str, sent_ok: bool, error: str = ""
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO replies (ts, event_id, uid, danmaku, speech, sent_ok, error)"
                " VALUES (?,?,?,?,?,?,?)",
                (time.time(), event_id, uid, danmaku, speech, int(sent_ok), error),
            )
            self._conn.commit()

    def reply_stats(self) -> dict:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS total,"
                " SUM(sent_ok) AS ok FROM replies"
            ).fetchone()
        return {"total": row["total"] or 0, "ok": row["ok"] or 0}

    def recent_replies(self, limit: int = 30) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT r.ts, r.uid, r.danmaku, r.speech, r.sent_ok, r.error,"
                "       e.uname, e.text"
                " FROM replies r LEFT JOIN events e ON e.id = r.event_id"
                " ORDER BY r.id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            {
                "ts": r["ts"],
                "uid": r["uid"],
                "uname": r["uname"] or "",
                "incoming": r["text"] or "",
                "danmaku": r["danmaku"] or "",
                "speech": r["speech"] or "",
                "ok": bool(r["sent_ok"]),
                "error": r["error"] or "",
            }
            for r in rows
        ]

    # ---------- 观众 ----------
    def touch_viewer(self, uid: int, uname: str) -> int:
        """记录一次发言，返回该观众累计发言数。"""
        now = time.time()
        with self._lock:
            self._conn.execute(
                "INSERT INTO viewers (uid, uname, first_seen, last_seen, msg_count)"
                " VALUES (?,?,?,?,1)"
                " ON CONFLICT(uid) DO UPDATE SET"
                "   uname=excluded.uname, last_seen=excluded.last_seen,"
                "   msg_count=viewers.msg_count+1",
                (uid, uname, now, now),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT msg_count FROM viewers WHERE uid=?", (uid,)
            ).fetchone()
        return int(row["msg_count"]) if row else 1

    def mark_replied(self, uid: int) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE viewers SET replied=replied+1 WHERE uid=?", (uid,)
            )
            self._conn.commit()

    def viewer_profile(self, uid: int) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM viewers WHERE uid=?", (uid,)
            ).fetchone()
        if not row:
            return None
        note = row["note"]
        try:
            extra = json.loads(note) if note else {}
        except (json.JSONDecodeError, TypeError):
            extra = {"raw": note}
        return {
            "uname": row["uname"],
            "msg_count": row["msg_count"],
            "replied": row["replied"],
            "extra": extra,
        }

    def set_viewer_note(self, uid: int, note: dict) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE viewers SET note=? WHERE uid=?",
                (json.dumps(note, ensure_ascii=False), uid),
            )
            self._conn.commit()

    # ---------- 用量 ----------
    @staticmethod
    def _today() -> str:
        return time.strftime("%Y-%m-%d")

    def incr_usage(self, field: str, delta: int = 1) -> int:
        assert field in set(USAGE_COLUMNS), field
        day = self._today()
        with self._lock:
            self._conn.execute(
                "INSERT INTO usage (day, %s) VALUES (?, ?)"
                " ON CONFLICT(day) DO UPDATE SET %s = %s + ?" % (field, field, field),
                (day, delta, delta),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT %s AS v FROM usage WHERE day=?" % field, (day,)
            ).fetchone()
        return int(row["v"]) if row else delta

    def today_usage(self) -> dict:
        day = self._today()
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM usage WHERE day=?", (day,)
            ).fetchone()
        if not row:
            return {c: 0 for c in USAGE_COLUMNS}
        return {c: row[c] for c in USAGE_COLUMNS}

    # ---------- 评论区 ----------
    #: 只有这些结论才算"处理完了"。considered/deferred 是待定状态，
    #: 下一轮扫描还要再看一次，否则被配额挤掉的评论就永久丢了。
    COMMENT_TERMINAL = ("replied", "ignored", "failed")

    def seen_comment(self, rpid: int) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT action FROM comments_seen WHERE rpid=?", (rpid,)
            ).fetchone()
        return bool(row) and row["action"] in self.COMMENT_TERMINAL

    def record_comment(
        self,
        rpid: int,
        oid: int,
        uid: int,
        uname: str,
        text: str,
        ctime: float,
        action: str,
        reason: str = "",
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO comments_seen"
                " (rpid, oid, uid, uname, text, ctime, action, reason, seen_at)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (rpid, oid, uid, uname, text, ctime, action, reason, time.time()),
            )
            self._conn.commit()

    def set_comment_action(self, rpid: int, action: str, reason: str = "") -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE comments_seen SET action=?, reason=? WHERE rpid=?",
                (action, reason, rpid),
            )
            self._conn.commit()

    def log_comment_reply(
        self,
        rpid: int,
        oid: int,
        uid: int,
        incoming: str,
        reply: str,
        ok: bool,
        error: str = "",
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO comment_replies"
                " (rpid, oid, uid, incoming, reply, ok, error, ts)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (rpid, oid, uid, incoming, reply, int(ok), error, time.time()),
            )
            self._conn.commit()

    def recent_comment_replies(self, limit: int = 30) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT rpid, oid, uid, incoming, reply, ok, error, ts"
                " FROM comment_replies ORDER BY ts DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def comment_stats(self) -> dict:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS total, SUM(ok) AS ok FROM comment_replies"
            ).fetchone()
            seen = self._conn.execute(
                "SELECT COUNT(*) AS c FROM comments_seen"
            ).fetchone()
        return {
            "replied": row["ok"] or 0,
            "attempts": row["total"] or 0,
            "seen": seen["c"] or 0,
        }

    # ---------- 自动逛 B 站 ----------
    def browse_seen(self, bvid: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM browse_seen WHERE bvid=?", (bvid,)
            ).fetchone()
        return row is not None

    def record_browse(
        self,
        bvid: str,
        aid: int,
        title: str,
        up_mid: int,
        up_name: str,
        duration: int,
        play: int,
        pubdate: float,
        verdict: str,
        reason: str = "",
        source: str = "",
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO browse_seen"
                " (bvid, aid, title, up_mid, up_name, duration, play, pubdate,"
                "  seen_at, verdict, reason, source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    bvid, aid, title, up_mid, up_name, duration, play,
                    pubdate, time.time(), verdict, reason, source,
                ),
            )
            self._conn.commit()

    def log_action(self, bvid: str, action: str, ok: bool, error: str = "") -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO browse_actions (ts, bvid, action, ok, error)"
                " VALUES (?,?,?,?,?)",
                (time.time(), bvid, action, int(ok), error),
            )
            self._conn.commit()

    def recent_actions(self, limit: int = 40) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT a.ts, a.bvid, a.action, a.ok, a.error, b.title"
                " FROM browse_actions a LEFT JOIN browse_seen b ON b.bvid = a.bvid"
                " ORDER BY a.id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def incr_action(self, action: str, delta: int = 1) -> int:
        day = self._today()
        with self._lock:
            self._conn.execute(
                "INSERT INTO action_usage (day, action, count) VALUES (?,?,?)"
                " ON CONFLICT(day, action) DO UPDATE SET count = count + ?",
                (day, action, delta, delta),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT count FROM action_usage WHERE day=? AND action=?", (day, action)
            ).fetchone()
        return int(row["count"]) if row else delta

    def action_used(self, action: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT count FROM action_usage WHERE day=? AND action=?",
                (self._today(), action),
            ).fetchone()
        return int(row["count"]) if row else 0

    def actions_today(self) -> dict:
        with self._lock:
            rows = self._conn.execute(
                "SELECT action, count FROM action_usage WHERE day=?", (self._today(),)
            ).fetchall()
        return {r["action"]: r["count"] for r in rows}

    def browse_stats(self) -> dict:
        with self._lock:
            seen = self._conn.execute(
                "SELECT COUNT(*) AS c FROM browse_seen"
            ).fetchone()
            # dry_run 的假动作不算"真的做过"
            acted = self._conn.execute(
                "SELECT COUNT(*) AS c,"
                " SUM(CASE WHEN ok=1 AND IFNULL(error,'')!='dry_run' THEN 1 ELSE 0 END) AS ok"
                " FROM browse_actions"
            ).fetchone()
        return {
            "seen": seen["c"] or 0,
            "actions": acted["c"] or 0,
            "actions_ok": acted["ok"] or 0,
        }

    def browse_history(
        self,
        verdict: str = "",
        action: str = "",
        query: str = "",
        limit: int = 40,
        offset: int = 0,
    ) -> dict:
        """逛站记录分页查询。

        verdict: "" 看过并判断 | "acted" 互动过 | "judged" 看过没动 | "skip" 跳过
        action : 只保留某个动作成功的条目（like/favorite/coin/follow/comment）
        """
        # SQLite 里 LIMIT -1 等于"不限量"，负 offset 也会被当成 0，
        # 前端传个 -1 就能把整张表拉出来，这里先夹住
        limit = max(1, min(int(limit), 200))
        offset = max(0, int(offset))

        where = ["1=1"]
        params: list = []

        if verdict == "acted":
            where.append(
                "EXISTS (SELECT 1 FROM browse_actions a WHERE a.bvid=b.bvid AND a.ok=1)"
            )
        elif verdict == "judged":
            where.append("b.verdict='judged'")
        elif verdict == "skip":
            where.append("b.verdict='skip'")
        else:
            where.append("b.verdict!='skip'")

        if action:
            where.append(
                "EXISTS (SELECT 1 FROM browse_actions a WHERE a.bvid=b.bvid"
                " AND a.action=? AND a.ok=1)"
            )
            params.append(action)

        if query:
            where.append("(b.title LIKE ? OR b.bvid = ? OR b.up_name LIKE ?)")
            like = f"%{query}%"
            params.extend([like, query, like])

        cond = " AND ".join(where)
        with self._lock:
            total = self._conn.execute(
                f"SELECT COUNT(*) AS c FROM browse_seen b WHERE {cond}", params
            ).fetchone()["c"]
            rows = self._conn.execute(
                "SELECT b.bvid, b.title, b.up_name, b.source, b.play, b.duration,"
                "       b.verdict, b.reason, b.seen_at, b.aid"
                f" FROM browse_seen b WHERE {cond}"
                " ORDER BY b.seen_at DESC LIMIT ? OFFSET ?",
                [*params, limit, offset],
            ).fetchall()

            bvids = [r["bvid"] for r in rows]
            acts: dict[str, list[dict]] = {}
            if bvids:
                marks = ",".join("?" * len(bvids))
                for a in self._conn.execute(
                    "SELECT bvid, action, ok, error, ts FROM browse_actions"
                    f" WHERE bvid IN ({marks}) ORDER BY id DESC",
                    bvids,
                ):
                    acts.setdefault(a["bvid"], []).append(
                        {
                            "action": a["action"],
                            "ok": bool(a["ok"]),
                            "error": a["error"] or "",
                            "ts": a["ts"],
                        }
                    )

        return {
            "total": total or 0,
            "items": [
                {
                    "bvid": r["bvid"],
                    "aid": r["aid"] or 0,
                    "title": r["title"] or "",
                    "up_name": r["up_name"] or "",
                    "source": r["source"] or "",
                    "play": r["play"] or 0,
                    "duration": r["duration"] or 0,
                    "verdict": r["verdict"] or "",
                    "reason": r["reason"] or "",
                    "seen_at": r["seen_at"] or 0,
                    "actions": acts.get(r["bvid"], []),
                }
                for r in rows
            ],
        }

    def browse_report(self, limit: int = 40) -> list[dict]:
        """今天真正"看过并判断过"的视频，连同它在上面做过的动作。

        比读 Markdown 报告可靠：Markdown 是给人看的产物，这里给 UI 用。
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT bvid, title, up_name, source, play, duration,"
                "       verdict, reason, seen_at, pubdate"
                " FROM browse_seen"
                " WHERE verdict != 'skip'"
                " ORDER BY seen_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
            acts = self._conn.execute(
                "SELECT bvid, action, ok, error, ts FROM browse_actions"
                " ORDER BY id DESC LIMIT 500"
            ).fetchall()

        by_bvid: dict[str, list[dict]] = {}
        for a in acts:
            by_bvid.setdefault(a["bvid"], []).append(
                {
                    "action": a["action"],
                    "ok": bool(a["ok"]),
                    "error": a["error"] or "",
                    "ts": a["ts"],
                }
            )

        return [
            {
                "bvid": r["bvid"],
                "title": r["title"] or "",
                "up_name": r["up_name"] or "",
                "source": r["source"] or "",
                "play": r["play"] or 0,
                "duration": r["duration"] or 0,
                "verdict": r["verdict"] or "",
                "reason": r["reason"] or "",
                "seen_at": r["seen_at"] or 0,
                "actions": by_bvid.get(r["bvid"], []),
            }
            for r in rows
        ]

    # ---------- KV ----------
    def kv_get(self, key: str, default: str = "") -> str:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM kv WHERE key=?", (key,)
            ).fetchone()
        return row["value"] if row else default

    def kv_set(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO kv (key, value) VALUES (?,?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()
