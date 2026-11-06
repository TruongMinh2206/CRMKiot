"""Đồng bộ dữ liệu KiotViet -> SQLite (tăng dần theo lastModifiedFrom)."""
import json
import logging
import re
import threading
from datetime import datetime, timedelta
from . import config, db
from .kiot import KiotClient

log = logging.getLogger("sync")
_lock = threading.Lock()
state = {"running": False, "progress": "", "last_error": None}


U = config.MONEY_UNIT


def _m(v):
    return (v or 0) * U if v is not None else None


def _d(s):
    """Normalize KiotViet datetime string -> 'YYYY-MM-DD HH:MM:SS' (None-safe)."""
    if not s:
        return None
    s = str(s).replace("T", " ")
    return s[:19]


def _fmt_kiot(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


# ---------------------------------------------------------------------------
# Resolve "sale phụ trách" của khách hàng
# ---------------------------------------------------------------------------
SALE_TOKENS = {"lybyn", "ly byn", "lb", "lybin", "byn"}
ONLINE_PREFIX = ("onl ", "online ", "ol ")
DEFAULT_ALIASES = {"nb": "Nhàn Béo", "nhàn béo": "Nhàn Béo", "ttl": "TLL", "vanh": "Vân Anh"}


def _norm(s):
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def _clean_seg(seg):
    """'My Lybyn' -> ('my', True) ; 'TLL Linh (Signal)' -> ('tll linh', False)"""
    seg = re.split(r"[(\[:]", seg)[0].strip()
    if not seg or len(seg) > 30:
        return None, False
    words = seg.split()
    has = any(_norm(w) in SALE_TOKENS for w in words)
    alias = _norm(" ".join(w for w in words if _norm(w) not in SALE_TOKENS)).strip(" .")
    if not alias or len(alias) < 2 or re.search(r"\d{3,}", alias):
        return None, has
    return alias, has


def split_name(name):
    """-> (alias, has_token). Lấy đoạn sau dấu '-' cuối cùng có ý nghĩa."""
    if not name or not re.search(r"[-–]", name):
        return None, False
    segs = [x for x in re.split(r"[-–]", name)[1:] if x.strip()]
    for seg in reversed(segs):
        alias, has = _clean_seg(seg)
        if alias:
            return alias, has
    return None, False


def sale_from_name(name, known=None):
    """Tên sale từ đuôi tên khách. Quét các đoạn sau dấu '-' từ phải sang trái."""
    if not name or not re.search(r"[-–]", name):
        return None
    known = known or set()
    segs = [x for x in re.split(r"[-–]", name)[1:] if x.strip()]
    for seg in reversed(segs):
        alias, has = _clean_seg(seg)
        if not alias:
            continue
        if has or alias in known:
            return alias
        for pfx in ONLINE_PREFIX:
            if alias.startswith(pfx):
                return alias
        # 'TLL Linh' -> nhận ra 'tll' nếu đó là bí danh đã biết
        for w in alias.split():
            if w in known:
                return w
    return None


def load_aliases():
    try:
        rows = db.rows("SELECT alias, sale_name FROM sale_aliases")
    except Exception:
        rows = []
    out = dict(DEFAULT_ALIASES)
    out.update({_norm(r["alias"]): r["sale_name"] for r in rows})
    return out


def build_context():
    """Tập alias 'quen': xuất hiện kèm token Lybyn >= 3 lần, tên nhân viên, bí danh khai báo."""
    users = db.rows("SELECT given_name FROM users")
    user_names = {u["given_name"] for u in users if u["given_name"]}
    aliases = load_aliases()
    cnt = {}
    for r in db.rows("SELECT name FROM customers"):
        alias, has = split_name(r["name"])
        if alias and has:
            cnt[alias] = cnt.get(alias, 0) + 1
    known = {a for a, n in cnt.items() if n >= 3} | set(aliases.keys())
    for u in user_names:
        known.add(_norm(u))
        known.add(_norm(u).split(" ")[-1])
    return {"user_names": user_names, "aliases": aliases, "known": known}


def canon_alias(alias, ctx):
    if not alias:
        return None
    key = _norm(alias)
    if key in ctx["aliases"]:
        return ctx["aliases"][key]
    for pfx in ONLINE_PREFIX:
        if key.startswith(pfx):
            return key[len(pfx):].title() + " (Online)"
    for u in ctx["user_names"]:
        if _norm(u) == key:
            return u
    if len(key) <= 3 and key.isascii() and not re.search(r"[aeiouy]", key):
        return key.upper()
    t = key.title()
    return re.sub(r"^(Nvkd|Nv|Kt|Gd|Ql|Tll|Nb|Ttl)(?=\s|$)", lambda m: m.group(1).upper(), t)


def sale_source():
    """Nguồn gán sale: đổi được ngay trong Cài đặt (lưu trong DB), mặc định lấy từ .env."""
    return (db.get_meta("sale_source") or config.SALE_SOURCE or "auto").lower()


def resolve_sale(c, ctx):
    """Trả về tên sale phụ trách từ dữ liệu khách hàng KiotViet."""
    src = sale_source()

    def by_name():
        return canon_alias(sale_from_name(c.get("name"), ctx["known"]), ctx)

    def by_group():
        g = c.get("groups")
        if not g:
            return None
        for p in [p.strip() for p in re.split(r"[|,]", str(g)) if p.strip()]:
            if p in ctx["user_names"]:
                return p
            if _norm(p) in ctx["aliases"]:
                return ctx["aliases"][_norm(p)]
        return None

    def by_created():
        v = c.get("createdBy")
        return str(v).strip() if v else None

    if src == "name":
        return by_name()
    if src == "group":
        return by_group()
    if src == "created_by":
        return by_created()
    if src in ("invoice", "manual"):
        return None
    return by_name() or by_group()


# ---------------------------------------------------------------------------
def sync_all(full=False):
    if not _lock.acquire(blocking=False):
        return {"ok": False, "message": "Đang đồng bộ, vui lòng chờ"}
    state.update(running=True, progress="Bắt đầu", last_error=None)
    started = datetime.now()
    counts = {"customers": 0, "products": 0, "invoices": 0, "orders": 0}
    try:
        db.init()
        k = KiotClient()
        last = None if full else db.get_meta("last_sync")
        if full:
            with db.cursor() as con:
                con.execute("DELETE FROM meta WHERE key LIKE 'win_done:%'")
        # lùi 10 phút để tránh sót dữ liệu do lệch giờ
        last_from = None
        if last:
            last_from = _fmt_kiot(datetime.fromisoformat(last) - timedelta(minutes=10))
        history_from = _fmt_kiot(datetime.now() - timedelta(days=config.SYNC_HISTORY_DAYS))

        state["progress"] = "Chi nhánh & nhân viên"
        with db.cursor() as con:
            for b in k.branches():
                con.execute("INSERT OR REPLACE INTO branches(id,name) VALUES(?,?)", (b["id"], b.get("branchName")))
            users = k.users()
            for u in users:
                con.execute("INSERT OR REPLACE INTO users(id,user_name,given_name,mobile,email,is_active) VALUES(?,?,?,?,?,?)",
                            (u["id"], u.get("userName"), u.get("givenName"), u.get("mobilePhone"), u.get("email"),
                             0 if u.get("isActive") is False else 1))

        state["progress"] = "Khách hàng"
        with db.cursor() as con:
            for c in k.customers(last_from):
                counts["customers"] += 1
                con.execute("""INSERT INTO customers(id,code,name,gender,birth_date,contact_number,email,address,location_name,
                    ward_name,organization,comments,groups,branch_id,created_by,created_date,modified_date,debt,total_invoiced,
                    total_revenue,total_point,reward_point,is_active,sale_name,raw)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(id) DO UPDATE SET code=excluded.code,name=excluded.name,gender=excluded.gender,
                    birth_date=excluded.birth_date,contact_number=excluded.contact_number,email=excluded.email,
                    address=excluded.address,location_name=excluded.location_name,ward_name=excluded.ward_name,
                    organization=excluded.organization,comments=excluded.comments,groups=excluded.groups,
                    branch_id=excluded.branch_id,created_by=excluded.created_by,created_date=excluded.created_date,
                    modified_date=excluded.modified_date,debt=excluded.debt,total_invoiced=excluded.total_invoiced,
                    total_revenue=excluded.total_revenue,total_point=excluded.total_point,reward_point=excluded.reward_point,
                    is_active=excluded.is_active,sale_name=excluded.sale_name,raw=excluded.raw""",
                            (c["id"], c.get("code"), c.get("name"), 1 if c.get("gender") else 0, _d(c.get("birthDate")),
                             c.get("contactNumber"), c.get("email"), c.get("address"), c.get("locationName"), c.get("wardName"),
                             c.get("organization"), c.get("comments"), c.get("groups"), c.get("branchId"), c.get("createdBy"),
                             _d(c.get("createdDate")), _d(c.get("modifiedDate")), _m(c.get("debt")), _m(c.get("totalInvoiced")),
                             _m(c.get("totalRevenue")), c.get("totalPoint"), c.get("rewardPoint"),
                             0 if c.get("isActive") is False else 1, None, json.dumps(c, ensure_ascii=False)))
                if counts["customers"] % 500 == 0:
                    state["progress"] = f"Khách hàng: {counts['customers']}"

        state["progress"] = "Hàng hoá"
        with db.cursor() as con:
            for p in k.products(last_from):
                counts["products"] += 1
                inv = p.get("inventories") or []
                on_hand = sum((i.get("onHand") or 0) for i in inv)
                cost = _m(next((i.get("cost") for i in inv if i.get("cost")), None))
                con.execute("""INSERT OR REPLACE INTO products(id,code,name,full_name,category_id,category_name,base_price,cost,
                    on_hand,is_active,unit,created_date,modified_date,raw) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (p["id"], p.get("code"), p.get("name"), p.get("fullName"), p.get("categoryId"), p.get("categoryName"),
                             _m(p.get("basePrice")), cost, on_hand, 0 if p.get("isActive") is False else 1, p.get("unit"),
                             _d(p.get("createdDate")), _d(p.get("modifiedDate")), json.dumps(p, ensure_ascii=False)))

        state["progress"] = "Hoá đơn"
        if last_from:
            counts["invoices"] += _sync_invoices(k, last_modified_from=last_from)
            state["progress"] = "Đơn hàng"
            counts["orders"] += _sync_orders(k, last_modified_from=last_from)
        else:
            # Lần đầu: tải theo từng tháng, lưu sau mỗi tháng để có thể chạy tiếp nếu đứt kết nối
            for wf, wt in _month_windows(config.SYNC_HISTORY_DAYS):
                key = f"win_done:{wf[:7]}"
                if db.get_meta(key) == "1":
                    continue
                state["progress"] = f"Hoá đơn tháng {wf[:7]}"
                counts["invoices"] += _sync_invoices(k, from_date=wf, to_date=wt)
                state["progress"] = f"Đơn hàng tháng {wf[:7]}"
                counts["orders"] += _sync_orders(k, from_date=wf, to_date=wt)
                if wt < _fmt_kiot(datetime.now() - timedelta(days=1)):
                    db.set_meta(key, "1")

        state["progress"] = "Gán sale cho khách"
        state["_in_sync"] = True
        try:
            reapply_sales()
        finally:
            state["_in_sync"] = False
            state["reapply_pending"] = False
        from . import backup
        backup.restore_customer_manual()

        db.set_meta("last_sync", started.isoformat(timespec="seconds"))
        msg = "OK"
        ok = 1
    except Exception as e:  # noqa
        log.exception("sync failed")
        msg = f"{type(e).__name__}: {e}"
        ok = 0
        state["last_error"] = msg
    finally:
        with db.cursor() as con:
            con.execute("INSERT INTO sync_log(started,finished,ok,message,customers,products,invoices,orders) VALUES(?,?,?,?,?,?,?,?)",
                        (started.isoformat(timespec="seconds"), datetime.now().isoformat(timespec="seconds"), ok, msg,
                         counts["customers"], counts["products"], counts["invoices"], counts["orders"]))
        state.update(running=False, progress="")
        _lock.release()
    return {"ok": bool(ok), "message": msg, **counts}


def _month_windows(days):
    start = (datetime.now() - timedelta(days=days)).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    out = []
    cur = start
    while cur <= datetime.now():
        nxt = (cur.replace(day=28) + timedelta(days=4)).replace(day=1)
        out.append((_fmt_kiot(cur), _fmt_kiot(nxt - timedelta(seconds=1))))
        cur = nxt
    return out


def _sync_invoices(k, **kw):
    n = 0
    con = db.connect()
    try:
        for v in k.invoices(**kw):
            n += 1
            dl = v.get("invoiceDelivery") or {}
            con.execute("""INSERT OR REPLACE INTO invoices(id,code,purchase_date,branch_id,branch_name,sold_by_id,sold_by_name,
                customer_id,customer_code,customer_name,order_code,total,total_payment,discount,status,status_value,using_cod,
                created_date,modified_date,delivery_status,delivery_status_value,delivery_code,raw)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (v["id"], v.get("code"), _d(v.get("purchaseDate")), v.get("branchId"), v.get("branchName"),
                         v.get("soldById"), v.get("soldByName"), v.get("customerId"), v.get("customerCode"), v.get("customerName"),
                         v.get("orderCode"), _m(v.get("total")), _m(v.get("totalPayment")), _m(v.get("discount")), v.get("status"),
                         v.get("statusValue"), 1 if v.get("usingCod") else 0, _d(v.get("createdDate")), _d(v.get("modifiedDate")),
                         dl.get("status"), dl.get("statusValue"), dl.get("deliveryCode"), json.dumps(v, ensure_ascii=False)))
            con.execute("DELETE FROM invoice_details WHERE invoice_id=?", (v["id"],))
            for i, d in enumerate(v.get("invoiceDetails") or []):
                con.execute("""INSERT INTO invoice_details(invoice_id,line,product_id,product_code,product_name,category_name,
                    quantity,price,discount,sub_total) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                            (v["id"], i, d.get("productId"), d.get("productCode"), d.get("productName"), d.get("categoryName"),
                             d.get("quantity"), _m(d.get("price")), _m(d.get("discount")), _m(d.get("subTotal"))))
            if n % 200 == 0:
                con.commit()
                state["progress"] = state["progress"].split(" (")[0] + f" ({n})"
        con.commit()
    finally:
        con.close()
    return n


def _sync_orders(k, **kw):
    n = 0
    con = db.connect()
    try:
        for o in k.orders(**kw):
            n += 1
            dl = o.get("orderDelivery") or {}
            sc = o.get("saleChannel")
            con.execute("""INSERT OR REPLACE INTO orders(id,code,purchase_date,branch_id,branch_name,sold_by_id,sold_by_name,
                customer_id,customer_code,customer_name,total,total_payment,status,status_value,sale_channel,created_date,
                modified_date,delivery_status,delivery_status_value,raw) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (o["id"], o.get("code"), _d(o.get("purchaseDate")), o.get("branchId"), o.get("branchName"),
                         o.get("soldById"), o.get("soldByName"), o.get("customerId"), o.get("customerCode"), o.get("customerName"),
                         _m(o.get("total")), _m(o.get("totalPayment")), o.get("status"), o.get("statusValue"),
                         sc.get("name") if isinstance(sc, dict) else o.get("saleChannelName"),
                         _d(o.get("createdDate")), _d(o.get("modifiedDate")), dl.get("status"), dl.get("statusValue"),
                         json.dumps(o, ensure_ascii=False)))
            if n % 200 == 0:
                con.commit()
        con.commit()
    finally:
        con.close()
    return n


def fill_sale_from_invoices():
    """Khách chưa có sale (từ KiotViet) -> lấy nhân viên bán nhiều hoá đơn nhất cho khách đó."""
    with db.cursor() as con:
        con.execute("""
            UPDATE customers SET sale_name = (
                SELECT sold_by_name FROM invoices i WHERE i.customer_id = customers.id AND sold_by_name IS NOT NULL
                  AND i.status = 1
                GROUP BY sold_by_name ORDER BY COUNT(*) DESC, MAX(purchase_date) DESC LIMIT 1)
            WHERE (sale_name IS NULL OR sale_name = '')""")


def unknown_suffixes(limit=40):
    """Đuôi tên xuất hiện trên tên khách nhưng CRM chưa nhận ra là sale nào."""
    ctx = build_context()
    agg = {}
    for r in db.rows("""SELECT c.id, c.name, COALESCE((SELECT SUM(i.total) FROM invoices i
                        WHERE i.customer_id=c.id AND i.status=1 AND i.purchase_date >= date('now','-365 days')),0) rev
                        FROM customers c WHERE c.is_active=1 AND (c.sale_name IS NULL OR c.sale_name='')
                          AND (c.sale_manual IS NULL OR c.sale_manual='')"""):
        alias, _ = split_name(r["name"])
        if not alias:
            continue
        a = agg.setdefault(alias, {"alias": alias, "customers": 0, "revenue": 0.0, "sample": r["name"]})
        a["customers"] += 1
        a["revenue"] += r["rev"] or 0
    out = sorted(agg.values(), key=lambda x: (-x["revenue"], -x["customers"]))
    return out[:limit]


def reapply_sales():
    """Tính lại sale_name cho toàn bộ khách từ dữ liệu đã lưu (sau khi đồng bộ / đổi bí danh)."""
    if state.get("running") and not state.get("_in_sync"):
        state["reapply_pending"] = True   # đang đồng bộ -> để cuối kỳ đồng bộ tự làm
        return
    ctx = build_context()
    rows = db.rows("SELECT id, raw FROM customers")
    with db.cursor() as con:
        for r in rows:
            try:
                c = json.loads(r["raw"] or "{}")
            except Exception:
                c = {}
            con.execute("UPDATE customers SET sale_name=? WHERE id=?", (resolve_sale(c, ctx), r["id"]))
    if sale_source() not in ("name", "group", "manual"):
        fill_sale_from_invoices()


# ---------------------------------------------------------------------------
def start_scheduler():
    import time

    def loop():
        time.sleep(5)
        while True:
            try:
                if config.KIOT_CLIENT_ID and config.KIOT_CLIENT_SECRET:
                    sync_all()
            except Exception:
                log.exception("scheduled sync failed")
            time.sleep(max(5, config.SYNC_INTERVAL_MIN) * 60)

    t = threading.Thread(target=loop, daemon=True, name="kiot-sync")
    t.start()
    return t
