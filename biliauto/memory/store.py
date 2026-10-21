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
"""


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
            self._conn.commit()

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
        assert field in {"danmaku_sent", "llm_calls", "speech_made"}
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
            return {"danmaku_sent": 0, "llm_calls": 0, "speech_made": 0}
        return {
            "danmaku_sent": row["danmaku_sent"],
            "llm_calls": row["llm_calls"],
            "speech_made": row["speech_made"],
        }

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
