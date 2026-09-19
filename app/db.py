"""
db.py — 沿用 txf-sim 的模式: 有 DATABASE_URL 就用 Postgres,沒有就退回本地 SQLite。
存兩張表: suggestions(今日建議,尚未執行的) / trade_log(歷史下單紀錄)
"""
import os
import sqlite3
import datetime

DATABASE_URL = os.getenv("DATABASE_URL")
USE_PG = bool(DATABASE_URL)

if USE_PG:
    import psycopg2
    import psycopg2.extras


def get_conn():
    if USE_PG:
        return psycopg2.connect(DATABASE_URL)
    return sqlite3.connect("local.db")


def init_db():
    conn = get_conn()
    cur = conn.cursor()
    if USE_PG:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS suggestions (
                id SERIAL PRIMARY KEY,
                code TEXT NOT NULL,
                action TEXT NOT NULL,
                qty INTEGER NOT NULL,
                note TEXT,
                created_at TIMESTAMP DEFAULT NOW(),
                consumed BOOLEAN DEFAULT FALSE
            );
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS trade_log (
                id SERIAL PRIMARY KEY,
                ts TIMESTAMP DEFAULT NOW(),
                mode TEXT,
                code TEXT,
                action TEXT,
                qty INTEGER,
                held_qty INTEGER,
                status TEXT,
                error TEXT,
                note TEXT
            );
        """)
    else:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS suggestions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT NOT NULL,
                action TEXT NOT NULL,
                qty INTEGER NOT NULL,
                note TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                consumed INTEGER DEFAULT 0
            );
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS trade_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT DEFAULT CURRENT_TIMESTAMP,
                mode TEXT,
                code TEXT,
                action TEXT,
                qty INTEGER,
                held_qty INTEGER,
                status TEXT,
                error TEXT,
                note TEXT
            );
        """)
    conn.commit()
    conn.close()


def add_suggestion(code, action, qty, note=""):
    conn = get_conn()
    cur = conn.cursor()
    ph = "%s" if USE_PG else "?"
    cur.execute(
        f"INSERT INTO suggestions (code, action, qty, note) VALUES ({ph},{ph},{ph},{ph})",
        (code, action, qty, note),
    )
    conn.commit()
    conn.close()


def get_pending_suggestions():
    conn = get_conn()
    cur = conn.cursor()
    cond = "FALSE" if USE_PG else "0"
    cur.execute(f"SELECT id, code, action, qty, note FROM suggestions WHERE consumed = {cond} ORDER BY id")
    rows = cur.fetchall()
    conn.close()
    return [{"id": r[0], "code": r[1], "action": r[2], "qty": r[3], "note": r[4]} for r in rows]


def delete_suggestion(sid):
    conn = get_conn()
    cur = conn.cursor()
    ph = "%s" if USE_PG else "?"
    cur.execute(f"DELETE FROM suggestions WHERE id = {ph}", (sid,))
    conn.commit()
    conn.close()


def mark_consumed(ids):
    if not ids:
        return
    conn = get_conn()
    cur = conn.cursor()
    val = "TRUE" if USE_PG else "1"
    ph = "%s" if USE_PG else "?"
    placeholders = ",".join([ph] * len(ids))
    cur.execute(f"UPDATE suggestions SET consumed = {val} WHERE id IN ({placeholders})", ids)
    conn.commit()
    conn.close()


def write_log(entries):
    conn = get_conn()
    cur = conn.cursor()
    ph = "%s" if USE_PG else "?"
    for e in entries:
        cur.execute(
            f"""INSERT INTO trade_log (mode, code, action, qty, held_qty, status, error, note)
                VALUES ({ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph})""",
            (e["mode"], e["code"], e["action"], e["qty"], e["held_qty"], e["status"], e["error"], e["note"]),
        )
    conn.commit()
    conn.close()


def get_recent_logs(limit=50):
    conn = get_conn()
    cur = conn.cursor()
    ph = "%s" if USE_PG else "?"
    cur.execute(f"SELECT ts, mode, code, action, qty, held_qty, status, error, note FROM trade_log ORDER BY id DESC LIMIT {ph}", (limit,))
    rows = cur.fetchall()
    conn.close()
    cols = ["ts", "mode", "code", "action", "qty", "held_qty", "status", "error", "note"]
    return [dict(zip(cols, r)) for r in rows]
