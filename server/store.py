"""SQLite 持久化层：会话 / 消息 / 评分 / 报告与复盘。

设计原则：
- 单文件 DB，一期本地自用，无多用户。
- 所有写操作立即落盘：进程重启、刷新页面都能完整恢复一场面试。
- 报告（含 LLM 复盘）生成一次即入库，之后只读，不重复花钱。
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "data" / "interview.db"
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id           TEXT PRIMARY KEY,
    type         TEXT NOT NULL,
    total        INTEGER NOT NULL,
    resume       TEXT DEFAULT '',
    jd           TEXT DEFAULT '',
    main_q       INTEGER DEFAULT 0,
    followup_used INTEGER DEFAULT 0,
    awaiting_followup INTEGER DEFAULT 0,
    status       TEXT DEFAULT 'active',
    current_json TEXT DEFAULT '{}',
    title        TEXT DEFAULT '',
    created_at   REAL NOT NULL,
    updated_at   REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    role       TEXT NOT NULL,
    kind       TEXT DEFAULT '',
    qno        INTEGER DEFAULT 0,
    content    TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_msg_session ON messages(session_id);
CREATE TABLE IF NOT EXISTS evaluations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL,
    qno         INTEGER NOT NULL,
    is_followup INTEGER DEFAULT 0,
    question    TEXT DEFAULT '',
    hints_json  TEXT DEFAULT '[]',
    answer      TEXT DEFAULT '',
    dims_json   TEXT NOT NULL,
    band        TEXT DEFAULT '',
    confidence  REAL DEFAULT 0,
    band_confidence REAL DEFAULT 0,
    low         INTEGER DEFAULT 0,
    band_low    INTEGER DEFAULT 0,
    engine      TEXT DEFAULT 'jev',
    cost        REAL DEFAULT 0,
    created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ev_session ON evaluations(session_id);
CREATE TABLE IF NOT EXISTS reports (
    session_id   TEXT PRIMARY KEY,
    overall      REAL DEFAULT 0,
    dims_json    TEXT NOT NULL,
    main_count   INTEGER DEFAULT 0,
    followup_count INTEGER DEFAULT 0,
    duration_s   INTEGER DEFAULT 0,
    summary      TEXT DEFAULT '',
    highlights_json TEXT DEFAULT '[]',
    improvements_json TEXT DEFAULT '[]',
    engines_json TEXT DEFAULT '[]',
    created_at   REAL NOT NULL
);
"""


def conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def init():
    with conn() as c:
        c.executescript(SCHEMA)
        # 迁移：reports 表加 review_status（generating/ready/failed/pending）
        cols = [r[1] for r in c.execute("PRAGMA table_info(reports)")]
        if "review_status" not in cols:
            c.execute("ALTER TABLE reports ADD COLUMN review_status TEXT DEFAULT ''")


# ---------------- 会话 ----------------

def create_session(sid: str, type_: str, total: int, resume: str, jd: str, title: str) -> None:
    now = time.time()
    with conn() as c:
        c.execute(
            """INSERT INTO sessions(id,type,total,resume,jd,main_q,followup_used,awaiting_followup,
               status,current_json,title,created_at,updated_at)
               VALUES(?,?,?,?,?,0,0,0,'active','{}',?,?,?)""",
            (sid, type_, total, resume, jd, title, now, now))


def get_session(sid: str) -> dict | None:
    with conn() as c:
        r = c.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
        return dict(r) if r else None


def update_session(sid: str, **fields) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k}=?" for k in fields)
    vals = list(fields.values()) + [time.time(), sid]
    with conn() as c:
        c.execute(f"UPDATE sessions SET {cols}, updated_at=? WHERE id=?", vals)


def list_sessions(limit: int = 50) -> list[dict]:
    with conn() as c:
        rows = c.execute("SELECT * FROM sessions ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        rep = get_report(d["id"])
        d["overall"] = rep["overall"] if rep else None
        d["has_report"] = bool(rep)
        out.append(d)
    return out


def delete_session(sid: str) -> None:
    with conn() as c:
        c.execute("DELETE FROM messages WHERE session_id=?", (sid,))
        c.execute("DELETE FROM evaluations WHERE session_id=?", (sid,))
        c.execute("DELETE FROM reports WHERE session_id=?", (sid,))
        c.execute("DELETE FROM sessions WHERE id=?", (sid,))


# ---------------- 消息 ----------------

def add_message(sid: str, role: str, content: str, kind: str = "", qno: int = 0) -> None:
    with conn() as c:
        c.execute("INSERT INTO messages(session_id,role,kind,qno,content,created_at) VALUES(?,?,?,?,?,?)",
                  (sid, role, kind, qno, content, time.time()))


def list_messages(sid: str) -> list[dict]:
    with conn() as c:
        rows = c.execute("SELECT * FROM messages WHERE session_id=? ORDER BY id", (sid,)).fetchall()
    return [dict(r) for r in rows]


# ---------------- 评分 ----------------

def add_evaluation(sid: str, ev: dict) -> None:
    with conn() as c:
        c.execute(
            """INSERT INTO evaluations(session_id,qno,is_followup,question,hints_json,answer,dims_json,
               band,confidence,band_confidence,low,band_low,engine,cost,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (sid, ev.get("qno", 0), 1 if ev.get("is_followup") else 0, ev.get("question", ""),
             json.dumps(ev.get("hints", []), ensure_ascii=False), ev.get("answer", ""),
             json.dumps(ev.get("dims", [0] * 5), ensure_ascii=False), ev.get("band", ""),
             ev.get("confidence", 0), ev.get("band_confidence", 0),
             1 if ev.get("low") else 0, 1 if ev.get("band_low") else 0,
             ev.get("engine", "jev"), ev.get("cost") or 0.0, time.time()))


def list_evaluations(sid: str) -> list[dict]:
    with conn() as c:
        rows = c.execute("SELECT * FROM evaluations WHERE session_id=? ORDER BY id", (sid,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["dims"] = json.loads(d["dims_json"])
        d["hints"] = json.loads(d["hints_json"])
        d["is_followup"] = bool(d["is_followup"])
        d["low"] = bool(d["low"])
        d["band_low"] = bool(d["band_low"])
        out.append(d)
    return out


# ---------------- 报告 ----------------

def save_report(sid: str, rep: dict) -> None:
    with conn() as c:
        c.execute("DELETE FROM reports WHERE session_id=?", (sid,))
        c.execute(
            """INSERT INTO reports(session_id,overall,dims_json,main_count,followup_count,duration_s,
               summary,highlights_json,improvements_json,engines_json,review_status,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (sid, rep["overall"], json.dumps(rep["dims"], ensure_ascii=False),
             rep.get("main_count", 0), rep.get("followup_count", 0), rep.get("duration_s", 0),
             rep.get("summary", ""), json.dumps(rep.get("highlights", []), ensure_ascii=False),
             json.dumps(rep.get("improvements", []), ensure_ascii=False),
             json.dumps(rep.get("engines", []), ensure_ascii=False),
             rep.get("review_status", "ready"), time.time()))


def get_report(sid: str) -> dict | None:
    with conn() as c:
        r = c.execute("SELECT * FROM reports WHERE session_id=?", (sid,)).fetchone()
        if not r:
            return None
    d = dict(r)
    d["dims"] = json.loads(d["dims_json"])
    d["highlights"] = json.loads(d["highlights_json"])
    d["improvements"] = json.loads(d["improvements_json"])
    d["engines"] = json.loads(d["engines_json"])
    return d


init()
