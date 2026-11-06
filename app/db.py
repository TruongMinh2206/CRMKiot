import sqlite3
import json
from contextlib import contextmanager
from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS branches (id INTEGER PRIMARY KEY, name TEXT);

CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY, user_name TEXT, given_name TEXT, mobile TEXT, email TEXT, is_active INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS customers (
  id INTEGER PRIMARY KEY, code TEXT, name TEXT, gender INTEGER, birth_date TEXT,
  contact_number TEXT, email TEXT, address TEXT, location_name TEXT, ward_name TEXT,
  organization TEXT, comments TEXT, groups TEXT, branch_id INTEGER, created_by TEXT,
  created_date TEXT, modified_date TEXT, debt REAL, total_invoiced REAL, total_revenue REAL,
  total_point REAL, reward_point REAL, is_active INTEGER DEFAULT 1,
  sale_name TEXT,          -- sale phụ trách lấy từ KiotViet (auto)
  sale_manual TEXT,        -- sale do CRM gán tay (ưu tiên)
  raw TEXT
);
CREATE INDEX IF NOT EXISTS ix_customers_sale ON customers(sale_name);

CREATE TABLE IF NOT EXISTS products (
  id INTEGER PRIMARY KEY, code TEXT, name TEXT, full_name TEXT, category_id INTEGER, category_name TEXT,
  base_price REAL, cost REAL, on_hand REAL, is_active INTEGER DEFAULT 1, unit TEXT,
  created_date TEXT, modified_date TEXT, raw TEXT
);

CREATE TABLE IF NOT EXISTS invoices (
  id INTEGER PRIMARY KEY, code TEXT, purchase_date TEXT, branch_id INTEGER, branch_name TEXT,
  sold_by_id INTEGER, sold_by_name TEXT, customer_id INTEGER, customer_code TEXT, customer_name TEXT,
  order_code TEXT, total REAL, total_payment REAL, discount REAL, status INTEGER, status_value TEXT,
  using_cod INTEGER, created_date TEXT, modified_date TEXT,
  delivery_status INTEGER, delivery_status_value TEXT, delivery_code TEXT, raw TEXT
);
CREATE INDEX IF NOT EXISTS ix_inv_date ON invoices(purchase_date);
CREATE INDEX IF NOT EXISTS ix_inv_cust ON invoices(customer_id);

CREATE TABLE IF NOT EXISTS invoice_details (
  invoice_id INTEGER, line INTEGER, product_id INTEGER, product_code TEXT, product_name TEXT,
  category_name TEXT, quantity REAL, price REAL, discount REAL, sub_total REAL,
  PRIMARY KEY (invoice_id, line)
);
CREATE INDEX IF NOT EXISTS ix_invd_prod ON invoice_details(product_id);

CREATE TABLE IF NOT EXISTS returns (
  id INTEGER PRIMARY KEY, code TEXT, return_date TEXT, invoice_id INTEGER, branch_id INTEGER,
  customer_id INTEGER, customer_code TEXT, customer_name TEXT, sold_by_name TEXT,
  total REAL, return_fee REAL, status INTEGER, status_value TEXT, description TEXT, created_date TEXT
);
CREATE INDEX IF NOT EXISTS ix_ret_date ON returns(return_date);
CREATE INDEX IF NOT EXISTS ix_ret_cust ON returns(customer_id);

CREATE TABLE IF NOT EXISTS return_details (
  return_id INTEGER, line INTEGER, product_id INTEGER, product_code TEXT, product_name TEXT,
  quantity REAL, price REAL, sub_total REAL, trade_mark TEXT,
  PRIMARY KEY (return_id, line)
);

CREATE TABLE IF NOT EXISTS orders (
  id INTEGER PRIMARY KEY, code TEXT, purchase_date TEXT, branch_id INTEGER, branch_name TEXT,
  sold_by_id INTEGER, sold_by_name TEXT, customer_id INTEGER, customer_code TEXT, customer_name TEXT,
  total REAL, total_payment REAL, status INTEGER, status_value TEXT, sale_channel TEXT,
  created_date TEXT, modified_date TEXT,
  delivery_status INTEGER, delivery_status_value TEXT, raw TEXT
);

CREATE TABLE IF NOT EXISTS sync_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT, started TEXT, finished TEXT, ok INTEGER, message TEXT,
  customers INTEGER, products INTEGER, invoices INTEGER, orders INTEGER
);

CREATE TABLE IF NOT EXISTS crm_users (
  username TEXT PRIMARY KEY, password TEXT, role TEXT, sale_name TEXT, display_name TEXT
);

CREATE TABLE IF NOT EXISTS sale_aliases (alias TEXT PRIMARY KEY, sale_name TEXT);

CREATE TABLE IF NOT EXISTS notes (
  id INTEGER PRIMARY KEY AUTOINCREMENT, customer_id INTEGER, lead_id INTEGER, created TEXT, author TEXT, content TEXT
);

CREATE TABLE IF NOT EXISTS teams (
  sale TEXT PRIMARY KEY, team TEXT, position INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS kpi_targets (
  sale TEXT, ym TEXT, revenue_target REAL DEFAULT 0, new_customers_target INTEGER DEFAULT 0,
  buying_customers_target INTEGER DEFAULT 0, PRIMARY KEY (sale, ym)
);

CREATE TABLE IF NOT EXISTS tasks (
  id INTEGER PRIMARY KEY AUTOINCREMENT, customer_id INTEGER, lead_id INTEGER, title TEXT, type TEXT DEFAULT 'call',
  due_date TEXT, assigned_to TEXT, status TEXT DEFAULT 'open', result TEXT, created TEXT, created_by TEXT,
  done_at TEXT, auto_key TEXT UNIQUE, priority INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_tasks_due ON tasks(status, due_date);

CREATE TABLE IF NOT EXISTS leads (
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, phone TEXT, source TEXT, sale TEXT, status TEXT DEFAULT 'new',
  value REAL DEFAULT 0, note TEXT, lost_reason TEXT, address TEXT, product_interest TEXT,
  created TEXT, updated TEXT, created_by TEXT, converted_customer_id INTEGER, next_action TEXT, next_date TEXT
);
"""


def connect():
    con = sqlite3.connect(config.DB_PATH, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=20000")   # chờ tối đa 20s khi DB đang bận (lúc đồng bộ)
    con.execute("PRAGMA synchronous=NORMAL")
    return con


def init():
    with connect() as con:
        con.executescript(SCHEMA)
        # nâng cấp nhẹ cho DB cũ
        cols = [r[1] for r in con.execute("PRAGMA table_info(notes)")]
        if "lead_id" not in cols:
            con.execute("ALTER TABLE notes ADD COLUMN lead_id INTEGER")
        # Bỏ cột raw không dùng ở invoices/products/orders -> DB nhẹ đi ~4 lần
        pcols = [r[1] for r in con.execute("PRAGMA table_info(products)")]
        if "trade_mark" not in pcols:
            con.execute("ALTER TABLE products ADD COLUMN trade_mark TEXT")
            con.execute("DELETE FROM meta WHERE key='trade_mark_filled'")
        ccols = [r[1] for r in con.execute("PRAGMA table_info(customers)")]
        if "is_boss" not in ccols:
            con.execute("ALTER TABLE customers ADD COLUMN is_boss INTEGER DEFAULT 0")
        if "boss_no_kpi" not in ccols:
            con.execute("ALTER TABLE customers ADD COLUMN boss_no_kpi INTEGER DEFAULT 0")
        dcols = [r[1] for r in con.execute("PRAGMA table_info(invoice_details)")]
        if "trade_mark" not in dcols:
            con.execute("ALTER TABLE invoice_details ADD COLUMN trade_mark TEXT")
        done = con.execute("SELECT value FROM meta WHERE key='slim_v1'").fetchone()
        if not done:
            for t in ("invoices", "products", "orders"):
                try:
                    con.execute(f"UPDATE {t} SET raw=NULL WHERE raw IS NOT NULL")
                except Exception:
                    pass
            con.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('slim_v1','1')")
            con.commit()
            con.execute("VACUUM")


@contextmanager
def cursor():
    con = connect()
    try:
        yield con
        con.commit()
    finally:
        con.close()


def get_meta(key, default=None):
    with cursor() as con:
        r = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return r["value"] if r else default


def set_meta(key, value):
    with cursor() as con:
        con.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, value))


def rows(sql, params=()):
    with cursor() as con:
        return [dict(r) for r in con.execute(sql, params).fetchall()]


def one(sql, params=()):
    r = rows(sql, params)
    return r[0] if r else None
