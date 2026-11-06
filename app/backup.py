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
TABLES = ["crm_users", "sale_aliases", "notes", "kpi_targets", "tasks", "leads", "teams"]
SETTING_KEYS = ["sale_source", "churn_days", "cat_map", "kpi_alloc", "report_basis"]
SNAPSHOT_EVERY_HOURS = int(os.getenv("SNAPSHOT_EVERY_HOURS", "6"))
_lock = threading.Lock()
_db_lock = threading.Lock()


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
                    payload["customer_boss"] = [dict(r) for r in con.execute(
                        "SELECT id, COALESCE(boss_no_kpi,0) boss_no_kpi FROM customers WHERE COALESCE(is_boss,0)=1").fetchall()]
                    q = ",".join("?" * len(SETTING_KEYS))
                    payload["settings"] = [dict(r) for r in con.execute(
                        f"SELECT key, value FROM meta WHERE key IN ({q})", SETTING_KEYS).fetchall()]
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
        for r in (json.loads(data["settings"]) if isinstance(data.get("settings"), str) else (data.get("settings") or [])):
            if not con.execute("SELECT 1 FROM meta WHERE key=?", (r["key"],)).fetchone():
                con.execute("INSERT INTO meta(key,value) VALUES(?,?)", (r["key"], r["value"]))
        for r in (json.loads(data["customer_boss"]) if isinstance(data.get("customer_boss"), str) else (data.get("customer_boss") or [])):
            con.execute("UPDATE customers SET is_boss=1, boss_no_kpi=? WHERE id=?", (r.get("boss_no_kpi", 0), r["id"]))
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


# ===========================================================================
# Bản sao toàn bộ cơ sở dữ liệu (để khởi động lại không phải tải lại KiotViet)
# ===========================================================================
import gzip
import tempfile
from datetime import datetime, timedelta
from . import config


def _ensure_db(cur):
    cur.execute("""CREATE TABLE IF NOT EXISTS crm_db_snapshot (
        id INT PRIMARY KEY, data BYTEA NOT NULL, bytes INT, note TEXT, updated TIMESTAMPTZ DEFAULT now())""")


def snapshot_info():
    if not enabled():
        return None
    return {"at": db.get_meta("db_snapshot_at"), "bytes": db.get_meta("db_snapshot_bytes"),
            "every_hours": SNAPSHOT_EVERY_HOURS}


def snapshot_db(force=False, background=True):
    """Nén toàn bộ file SQLite và cất lên Postgres (giới hạn tần suất)."""
    if not enabled():
        return "off"
    last = db.get_meta("db_snapshot_at")
    if not force and last:
        try:
            if datetime.now() - datetime.fromisoformat(last) < timedelta(hours=SNAPSHOT_EVERY_HOURS):
                return "skip"
        except Exception:
            pass

    def run():
        if not _db_lock.acquire(blocking=False):
            return
        tmp = None
        try:
            import psycopg2
            fd, tmp = tempfile.mkstemp(suffix=".db")
            os.close(fd)
            os.unlink(tmp)
            con = db.connect()
            con.execute("VACUUM INTO ?", (tmp,))
            con.close()
            with open(tmp, "rb") as f:
                blob = gzip.compress(f.read(), 6)
            counts = {}
            with db.cursor() as c:
                for t in ("customers", "invoices", "invoice_details", "products"):
                    counts[t] = c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            pg = _pg()
            cur = pg.cursor()
            _ensure_db(cur)
            cur.execute("""INSERT INTO crm_db_snapshot(id, data, bytes, note, updated) VALUES (1, %s, %s, %s, now())
                           ON CONFLICT (id) DO UPDATE SET data=EXCLUDED.data, bytes=EXCLUDED.bytes,
                           note=EXCLUDED.note, updated=now()""",
                        (psycopg2.Binary(blob), len(blob), json.dumps(counts)))
            pg.commit()
            pg.close()
            db.set_meta("db_snapshot_at", datetime.now().isoformat(timespec="seconds"))
            db.set_meta("db_snapshot_bytes", str(len(blob)))
            log.info("db snapshot saved: %.1f MB %s", len(blob) / 1e6, counts)
        except Exception:
            log.exception("db snapshot failed")
        finally:
            if tmp and os.path.exists(tmp):
                os.unlink(tmp)
            _db_lock.release()

    if background:
        threading.Thread(target=run, daemon=True).start()
        return "started"
    run()
    return "done"


def restore_db():
    """Khi khởi động: nếu chưa có dữ liệu KiotViet thì lấy bản sao gần nhất về."""
    if not enabled():
        return False
    path = config.DB_PATH
    try:
        if os.path.exists(path) and os.path.getsize(path) > 100_000:
            con = db.connect()
            try:
                n = con.execute("SELECT COUNT(*) FROM invoices").fetchone()[0]
                if n > 0:
                    return False      # đã có dữ liệu, không cần khôi phục
            except Exception:
                pass
            finally:
                con.close()
    except Exception:
        pass
    try:
        pg = _pg()
        cur = pg.cursor()
        _ensure_db(cur)
        cur.execute("SELECT data, bytes, note, updated FROM crm_db_snapshot WHERE id=1")
        row = cur.fetchone()
        pg.close()
        if not row:
            return False
        blob = bytes(row[0])
        raw = gzip.decompress(blob)
        for suf in ("-wal", "-shm"):
            if os.path.exists(path + suf):
                os.unlink(path + suf)
        with open(path, "wb") as f:
            f.write(raw)
        log.info("db restored from snapshot %s (%s, %.1f MB)", row[3], row[2], len(raw) / 1e6)
        try:
            db.set_meta("db_snapshot_at", row[3].replace(tzinfo=None).isoformat(timespec="seconds"))
            db.set_meta("db_snapshot_bytes", str(row[1]))
            db.set_meta("db_restored_at", datetime.now().isoformat(timespec="seconds"))
        except Exception:
            pass
        return True
    except Exception:
        log.exception("db restore failed")
        return False
