"""Sao lưu dữ liệu nhập tay sang Postgres (DATABASE_URL) và nạp lại khi khởi động.

SQLite vẫn là nơi làm việc chính; Postgres chỉ giữ bản sao bền của các bảng nhập tay,
để Render gói Free (ổ đĩa tạm) không làm mất KPI / việc / lead / ghi chú / tài khoản.
"""
import json
import logging
import os
import threading
from . import db

log = logging.getLogger("backup")
DATABASE_URL = os.getenv("DATABASE_URL", "")
TABLES = ["crm_users", "sale_aliases", "notes", "kpi_targets", "tasks", "leads"]
_lock = threading.Lock()


def enabled():
    return bool(DATABASE_URL)


def _pg():
    import psycopg2
    return psycopg2.connect(DATABASE_URL, connect_timeout=15)


def _ensure(cur):
    cur.execute("""CREATE TABLE IF NOT EXISTS crm_backup (
        tbl TEXT NOT NULL, rows JSONB NOT NULL, updated TIMESTAMPTZ DEFAULT now(), PRIMARY KEY (tbl))""")


def save(tables=None):
    """Chép toàn bộ các bảng nhập tay từ SQLite lên Postgres (chạy nền)."""
    if not enabled():
        return
    tables = tables or TABLES

    def run():
        with _lock:
            try:
                with db.cursor() as con:
                    payload = {t: [dict(r) for r in con.execute(f"SELECT * FROM {t}").fetchall()] for t in tables}
                    payload["customer_manual"] = [dict(r) for r in con.execute(
                        "SELECT id, sale_manual FROM customers WHERE sale_manual IS NOT NULL AND sale_manual<>''").fetchall()]
                pg = _pg()
                cur = pg.cursor()
                _ensure(cur)
                for t, rows in payload.items():
                    cur.execute("""INSERT INTO crm_backup(tbl, rows, updated) VALUES (%s, %s, now())
                                   ON CONFLICT (tbl) DO UPDATE SET rows=EXCLUDED.rows, updated=now()""", (t, json.dumps(rows, ensure_ascii=False)))
                pg.commit()
                pg.close()
                log.info("backup saved: %s", {t: len(r) for t, r in payload.items()})
            except Exception:
                log.exception("backup save failed")

    threading.Thread(target=run, daemon=True).start()


def restore():
    """Khi khởi động: nếu SQLite trống (ổ đĩa mới) thì nạp lại từ Postgres."""
    if not enabled():
        return
    try:
        pg = _pg()
        cur = pg.cursor()
        _ensure(cur)
        cur.execute("SELECT tbl, rows FROM crm_backup")
        data = {t: r for t, r in cur.fetchall()}
        pg.close()
    except Exception:
        log.exception("backup restore failed (bỏ qua)")
        return
    if not data:
        return
    with db.cursor() as con:
        for t in TABLES:
            rows = data.get(t) or []
            if isinstance(rows, str):
                rows = json.loads(rows)
            have = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            if have or not rows:
                continue  # SQLite đã có dữ liệu -> giữ nguyên, không ghi đè
            cols = [r[1] for r in con.execute(f"PRAGMA table_info({t})")]
            for r in rows:
                keys = [k for k in r if k in cols]
                con.execute(f"INSERT OR IGNORE INTO {t}({','.join(keys)}) VALUES({','.join('?' * len(keys))})", [r[k] for k in keys])
            log.info("restored %s: %d rows", t, len(rows))
        cm = data.get("customer_manual") or []
        if isinstance(cm, str):
            cm = json.loads(cm)
        for r in cm:
            con.execute("UPDATE customers SET sale_manual=? WHERE id=? AND (sale_manual IS NULL OR sale_manual='')", (r["sale_manual"], r["id"]))


def restore_customer_manual():
    """Sau khi đồng bộ KiotViet xong (bảng customers vừa được tạo), gán lại sale_manual từ bản sao."""
    if not enabled():
        return
    try:
        pg = _pg()
        cur = pg.cursor()
        _ensure(cur)
        cur.execute("SELECT rows FROM crm_backup WHERE tbl='customer_manual'")
        row = cur.fetchone()
        pg.close()
        if not row:
            return
        cm = row[0] if not isinstance(row[0], str) else json.loads(row[0])
        with db.cursor() as con:
            for r in cm:
                con.execute("UPDATE customers SET sale_manual=? WHERE id=? AND (sale_manual IS NULL OR sale_manual='')", (r["sale_manual"], r["id"]))
    except Exception:
        log.exception("restore customer_manual failed")
