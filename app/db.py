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
  id INTEGER PRIMARY KEY AUTOINCREMENT, customer_id INTEGER, created TEXT, author TEXT, content TEXT
);
"""


def connect():
    con = sqlite3.connect(config.DB_PATH, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    return con


def init():
    with connect() as con:
        con.executescript(SCHEMA)


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
