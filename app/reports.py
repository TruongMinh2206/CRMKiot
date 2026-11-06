"""Các báo cáo CRM tính từ SQLite. Tất cả hàm nhận (from_date, to_date, sale=None) dạng 'YYYY-MM-DD'."""
from datetime import date, datetime, timedelta
from . import db, config

INV_OK = "i.status = 1"  # hoá đơn hoàn thành


def _period(from_d, to_d):
    f = datetime.fromisoformat(from_d)
    t = datetime.fromisoformat(to_d)
    days = (t - f).days + 1
    pf = f - timedelta(days=days)
    pt = f - timedelta(days=1)
    return f.strftime("%Y-%m-%d"), t.strftime("%Y-%m-%d") + " 23:59:59", pf.strftime("%Y-%m-%d"), pt.strftime("%Y-%m-%d") + " 23:59:59"


def sale_expr():
    return "COALESCE(NULLIF(c.sale_manual,''), NULLIF(c.sale_name,''), '(Chưa gán)')"


def _sale_filter(sale, alias="c"):
    if not sale:
        return "", ()
    return f" AND COALESCE(NULLIF({alias}.sale_manual,''), NULLIF({alias}.sale_name,''), '(Chưa gán)') = ?", (sale,)


def sales_list():
    r = db.rows(f"SELECT DISTINCT {sale_expr()} AS s FROM customers c ORDER BY 1")
    return [x["s"] for x in r]


# ---------------------------------------------------------------------------
# 1. Khách hàng theo sale
# ---------------------------------------------------------------------------
def customers_by_sale(from_d, to_d):
    f, t, pf, pt = _period(from_d, to_d)
    churn = (datetime.fromisoformat(from_d) - timedelta(days=config.CHURN_DAYS)).strftime("%Y-%m-%d")
    sql = f"""
    WITH c2 AS (SELECT c.id, {sale_expr()} AS sale, c.created_date FROM customers c WHERE c.is_active=1),
    cur AS (SELECT i.customer_id, SUM(i.total) rev, COUNT(*) n FROM invoices i WHERE {INV_OK} AND i.purchase_date BETWEEN ? AND ? GROUP BY 1),
    prev AS (SELECT i.customer_id, SUM(i.total) rev, COUNT(*) n FROM invoices i WHERE {INV_OK} AND i.purchase_date BETWEEN ? AND ? GROUP BY 1),
    lastbuy AS (SELECT i.customer_id, MAX(i.purchase_date) d FROM invoices i WHERE {INV_OK} GROUP BY 1),
    firstbuy AS (SELECT i.customer_id, MIN(i.purchase_date) d FROM invoices i WHERE {INV_OK} GROUP BY 1)
    SELECT c2.sale,
      COUNT(*) AS total_customers,
      SUM(CASE WHEN c2.created_date BETWEEN ? AND ? THEN 1 ELSE 0 END) AS new_customers,
      SUM(CASE WHEN c2.created_date BETWEEN ? AND ? THEN 1 ELSE 0 END) AS new_customers_prev,
      SUM(CASE WHEN cur.customer_id IS NOT NULL THEN 1 ELSE 0 END) AS buying_customers,
      SUM(CASE WHEN prev.customer_id IS NOT NULL THEN 1 ELSE 0 END) AS buying_customers_prev,
      SUM(CASE WHEN firstbuy.d BETWEEN ? AND ? THEN 1 ELSE 0 END) AS first_time_buyers,
      SUM(CASE WHEN lastbuy.d IS NOT NULL AND lastbuy.d < ? THEN 1 ELSE 0 END) AS churned,
      SUM(CASE WHEN prev.customer_id IS NOT NULL AND cur.customer_id IS NULL THEN 1 ELSE 0 END) AS lost_vs_prev,
      SUM(CASE WHEN prev.customer_id IS NULL AND cur.customer_id IS NOT NULL THEN 1 ELSE 0 END) AS gained_vs_prev,
      COALESCE(SUM(cur.rev),0) AS revenue, COALESCE(SUM(prev.rev),0) AS revenue_prev,
      COALESCE(SUM(cur.n),0) AS invoices, COALESCE(SUM(prev.n),0) AS invoices_prev
    FROM c2 LEFT JOIN cur ON cur.customer_id=c2.id LEFT JOIN prev ON prev.customer_id=c2.id
      LEFT JOIN lastbuy ON lastbuy.customer_id=c2.id LEFT JOIN firstbuy ON firstbuy.customer_id=c2.id
    GROUP BY c2.sale ORDER BY revenue DESC"""
    rows = db.rows(sql, (f, t, pf, pt, f, t, pf, pt, f, t, churn))
    for r in rows:
        r["revenue_growth"] = _growth(r["revenue"], r["revenue_prev"])
        r["customer_growth"] = _growth(r["buying_customers"], r["buying_customers_prev"])
        r["avg_invoice"] = r["revenue"] / r["invoices"] if r["invoices"] else 0
    return rows


def _growth(a, b):
    if not b:
        return None
    return round((a - b) / b * 100, 1)


# ---------------------------------------------------------------------------
# 2. Biến động khách hàng theo tháng
# ---------------------------------------------------------------------------
def customer_trend(months=12, sale=None):
    sf, sp = _sale_filter(sale)
    start = (date.today().replace(day=1) - timedelta(days=31 * (months - 1))).replace(day=1).isoformat()
    sql = f"""
    WITH cust AS (SELECT c.id, c.created_date FROM customers c WHERE 1=1 {sf}),
    buys AS (SELECT i.customer_id, substr(i.purchase_date,1,7) ym, SUM(i.total) rev FROM invoices i
             JOIN cust ON cust.id=i.customer_id WHERE {INV_OK} GROUP BY 1,2),
    firstbuy AS (SELECT customer_id, MIN(ym) ym FROM buys GROUP BY 1),
    m AS (SELECT DISTINCT ym FROM buys WHERE ym >= substr(?,1,7) UNION SELECT DISTINCT substr(created_date,1,7) FROM cust WHERE created_date >= ?)
    SELECT m.ym,
      (SELECT COUNT(*) FROM cust WHERE substr(created_date,1,7)=m.ym) AS new_customers,
      (SELECT COUNT(*) FROM buys WHERE buys.ym=m.ym) AS active_customers,
      (SELECT COUNT(*) FROM firstbuy WHERE firstbuy.ym=m.ym) AS first_buyers,
      (SELECT COALESCE(SUM(rev),0) FROM buys WHERE buys.ym=m.ym) AS revenue,
      (SELECT COUNT(*) FROM cust WHERE created_date < m.ym||'-01') AS cumulative_customers,
      (SELECT COUNT(*) FROM buys b1 WHERE b1.ym = m.ym AND EXISTS (SELECT 1 FROM buys b2 WHERE b2.customer_id=b1.customer_id AND b2.ym < b1.ym)) AS returning_customers
    FROM m WHERE m.ym <= strftime('%Y-%m','now') ORDER BY m.ym"""
    rows = db.rows(sql, sp + (start, start))
    # KH ngừng mua trong tháng m = khách có lần mua cuối + CHURN_DAYS rơi vào tháng m (và đã thực sự quá hạn tính đến hôm nay)
    cutoff = (date.today() - timedelta(days=config.CHURN_DAYS)).isoformat()
    lost = {}
    for r in db.rows(f"""SELECT substr(date(MAX(i.purchase_date), '+{config.CHURN_DAYS} days'),1,7) ym, COUNT(*) n
                         FROM invoices i JOIN customers c ON c.id=i.customer_id WHERE {INV_OK} {sf}
                         GROUP BY i.customer_id HAVING MAX(i.purchase_date) < ?""", sp + (cutoff,)):
        lost[r["ym"]] = lost.get(r["ym"], 0) + 1
    # cách trên trả về 1 dòng / khách; gom lại theo tháng
    for r in rows:
        r["lost_customers"] = lost.get(r["ym"], 0)
        r["net_change"] = r["new_customers"] - r["lost_customers"]
        r["churn_pending"] = r["ym"] == date.today().strftime("%Y-%m")
    return rows


# ---------------------------------------------------------------------------
# 3. Doanh số khách hàng (top, giảm, RFM)
# ---------------------------------------------------------------------------
def customer_revenue(from_d, to_d, sale=None, limit=4000):
    f, t, pf, pt = _period(from_d, to_d)
    sf, sp = _sale_filter(sale)
    today = date.today().isoformat()
    sql = f"""
    WITH cur AS (SELECT customer_id, SUM(total) rev, COUNT(*) n FROM invoices i WHERE {INV_OK} AND purchase_date BETWEEN ? AND ? GROUP BY 1),
    prev AS (SELECT customer_id, SUM(total) rev, COUNT(*) n FROM invoices i WHERE {INV_OK} AND purchase_date BETWEEN ? AND ? GROUP BY 1),
    allb AS (SELECT customer_id, SUM(total) rev, COUNT(*) n, MAX(purchase_date) last_d, MIN(purchase_date) first_d FROM invoices i WHERE {INV_OK} GROUP BY 1)
    SELECT c.id, c.code, c.name, c.contact_number, c.groups, {sale_expr()} AS sale, c.debt,
      COALESCE(cur.rev,0) revenue, COALESCE(cur.n,0) invoices, COALESCE(prev.rev,0) revenue_prev, COALESCE(prev.n,0) invoices_prev,
      COALESCE(allb.rev,0) lifetime_revenue, COALESCE(allb.n,0) lifetime_invoices, allb.last_d last_purchase, allb.first_d first_purchase,
      CAST(julianday(?) - julianday(allb.last_d) AS INTEGER) AS days_since_last
    FROM customers c LEFT JOIN cur ON cur.customer_id=c.id LEFT JOIN prev ON prev.customer_id=c.id LEFT JOIN allb ON allb.customer_id=c.id
    WHERE c.is_active=1 AND (cur.customer_id IS NOT NULL OR prev.customer_id IS NOT NULL OR allb.last_d >= ?) {sf}
    ORDER BY revenue DESC, lifetime_revenue DESC LIMIT ?"""
    year_ago = (date.today() - timedelta(days=365)).isoformat()
    rows = db.rows(sql, (f, t, pf, pt, today, year_ago) + sp + (limit,))
    for r in rows:
        r["growth"] = _growth(r["revenue"], r["revenue_prev"])
        r["segment"] = _segment(r)
    return rows


def _segment(r):
    d = r.get("days_since_last")
    if r["lifetime_invoices"] == 0:
        return "Chưa mua"
    if d is not None and d > config.CHURN_DAYS:
        return "Ngừng mua"
    if r["revenue_prev"] and r["revenue"] < r["revenue_prev"] * 0.7:
        return "Giảm mua"
    if r["lifetime_invoices"] == 1:
        return "Mua lần đầu"
    if r["lifetime_invoices"] >= 5 and (d or 0) <= 30:
        return "VIP/Trung thành"
    return "Bình thường"


def revenue_summary(from_d, to_d, sale=None):
    f, t, pf, pt = _period(from_d, to_d)
    sf, sp = _sale_filter(sale)
    join = "JOIN customers c ON c.id=i.customer_id" if sale else "LEFT JOIN customers c ON c.id=i.customer_id"
    cur = db.one(f"SELECT COALESCE(SUM(i.total),0) rev, COUNT(*) n, COUNT(DISTINCT i.customer_id) cust FROM invoices i {join} WHERE {INV_OK} AND i.purchase_date BETWEEN ? AND ? {sf}", (f, t) + sp)
    prev = db.one(f"SELECT COALESCE(SUM(i.total),0) rev, COUNT(*) n, COUNT(DISTINCT i.customer_id) cust FROM invoices i {join} WHERE {INV_OK} AND i.purchase_date BETWEEN ? AND ? {sf}", (pf, pt) + sp)
    daily = db.rows(f"SELECT substr(i.purchase_date,1,10) d, SUM(i.total) rev, COUNT(*) n FROM invoices i {join} WHERE {INV_OK} AND i.purchase_date BETWEEN ? AND ? {sf} GROUP BY 1 ORDER BY 1", (f, t) + sp)
    return {"revenue": cur["rev"], "invoices": cur["n"], "customers": cur["cust"],
            "revenue_prev": prev["rev"], "invoices_prev": prev["n"], "customers_prev": prev["cust"],
            "revenue_growth": _growth(cur["rev"], prev["rev"]), "daily": daily,
            "total_customers": db.one(f"SELECT COUNT(*) n FROM customers c WHERE c.is_active=1 {sf}", sp)["n"],
            "new_customers": db.one(f"SELECT COUNT(*) n FROM customers c WHERE c.created_date BETWEEN ? AND ? {sf}", (f, t) + sp)["n"],
            "new_customers_prev": db.one(f"SELECT COUNT(*) n FROM customers c WHERE c.created_date BETWEEN ? AND ? {sf}", (pf, pt) + sp)["n"]}


# ---------------------------------------------------------------------------
# 4. Hàng nhanh / chậm
# ---------------------------------------------------------------------------
def product_velocity(from_d, to_d, sale=None):
    f, t, pf, pt = _period(from_d, to_d)
    days = (datetime.fromisoformat(to_d) - datetime.fromisoformat(from_d)).days + 1
    sf, sp = _sale_filter(sale)
    join = "JOIN customers c ON c.id=i.customer_id" if sale else ""
    sql = f"""
    WITH cur AS (SELECT d.product_id, SUM(d.quantity) qty, SUM(d.sub_total) rev, COUNT(DISTINCT d.invoice_id) n
                 FROM invoice_details d JOIN invoices i ON i.id=d.invoice_id {join} WHERE {INV_OK} AND i.purchase_date BETWEEN ? AND ? {sf} GROUP BY 1),
    prev AS (SELECT d.product_id, SUM(d.quantity) qty, SUM(d.sub_total) rev
             FROM invoice_details d JOIN invoices i ON i.id=d.invoice_id {join} WHERE {INV_OK} AND i.purchase_date BETWEEN ? AND ? {sf} GROUP BY 1),
    lastsold AS (SELECT d.product_id, MAX(i.purchase_date) d FROM invoice_details d JOIN invoices i ON i.id=d.invoice_id WHERE {INV_OK} GROUP BY 1)
    SELECT p.id, p.code, p.name, p.category_name, p.base_price, p.cost, p.on_hand,
      COALESCE(cur.qty,0) qty, COALESCE(cur.rev,0) revenue, COALESCE(cur.n,0) invoices,
      COALESCE(prev.qty,0) qty_prev, COALESCE(prev.rev,0) revenue_prev, lastsold.d last_sold
    FROM products p LEFT JOIN cur ON cur.product_id=p.id LEFT JOIN prev ON prev.product_id=p.id LEFT JOIN lastsold ON lastsold.product_id=p.id
    WHERE p.is_active=1 AND (cur.product_id IS NOT NULL OR prev.product_id IS NOT NULL OR COALESCE(p.on_hand,0) > 0)"""
    rows = db.rows(sql, (f, t) + sp + (pf, pt) + sp)
    today = date.today()
    for r in rows:
        r["qty_per_day"] = round(r["qty"] / days, 2) if days else 0
        r["days_of_stock"] = round(r["on_hand"] / r["qty_per_day"]) if r["qty_per_day"] and r["on_hand"] else None
        r["growth"] = _growth(r["qty"], r["qty_prev"])
        r["days_since_sold"] = (today - datetime.fromisoformat(r["last_sold"]).date()).days if r["last_sold"] else None
        r["stock_value"] = (r["on_hand"] or 0) * (r["cost"] or r["base_price"] or 0)
        # Phân loại
        if r["qty"] == 0 and (r["on_hand"] or 0) > 0:
            r["speed"] = "Tồn không bán"
        elif r["days_of_stock"] is not None and r["days_of_stock"] > 90:
            r["speed"] = "Chậm"
        elif r["days_of_stock"] is not None and r["days_of_stock"] < 14:
            r["speed"] = "Nhanh (sắp hết)"
        elif r["qty"] > 0 and r["qty_prev"] == 0:
            r["speed"] = "Mới bán"
        elif r["growth"] is not None and r["growth"] >= 30:
            r["speed"] = "Tăng tốc"
        elif r["growth"] is not None and r["growth"] <= -30:
            r["speed"] = "Giảm tốc"
        else:
            r["speed"] = "Ổn định"
    rows.sort(key=lambda r: -r["revenue"])
    return rows


# ---------------------------------------------------------------------------
# 5. Hành trình đơn hàng
# ---------------------------------------------------------------------------
ORDER_STATUS = {1: "Phiếu tạm", 2: "Đang giao hàng", 3: "Hoàn thành", 4: "Đã hủy", 5: "Đã xác nhận"}
INVOICE_STATUS = {1: "Hoàn thành", 2: "Đã hủy", 3: "Đang xử lý", 5: "Không giao được"}


def order_journey(from_d, to_d, sale=None):
    f, t, pf, pt = _period(from_d, to_d)
    sf, sp = _sale_filter(sale)
    join = "JOIN customers c ON c.id=o.customer_id" if sale else ""
    joini = "JOIN customers c ON c.id=i.customer_id" if sale else ""
    orders = db.rows(f"SELECT o.status, COALESCE(o.status_value, '') sv, COUNT(*) n, COALESCE(SUM(o.total),0) total FROM orders o {join} WHERE o.purchase_date BETWEEN ? AND ? {sf} GROUP BY 1,2 ORDER BY 1", (f, t) + sp)
    for r in orders:
        r["label"] = r["sv"] or ORDER_STATUS.get(r["status"], str(r["status"]))
    invs = db.rows(f"SELECT i.status, COALESCE(i.status_value,'') sv, COUNT(*) n, COALESCE(SUM(i.total),0) total FROM invoices i {joini} WHERE i.purchase_date BETWEEN ? AND ? {sf} GROUP BY 1,2 ORDER BY 1", (f, t) + sp)
    for r in invs:
        r["label"] = r["sv"] or INVOICE_STATUS.get(r["status"], str(r["status"]))
    delivery = db.rows(f"SELECT COALESCE(i.delivery_status_value, 'Không giao hàng') label, COUNT(*) n, COALESCE(SUM(i.total),0) total FROM invoices i {joini} WHERE i.purchase_date BETWEEN ? AND ? {sf} GROUP BY 1 ORDER BY 2 DESC", (f, t) + sp)
    # Đơn đang kẹt: phiếu tạm / đã xác nhận / đang giao quá 3 ngày
    stuck = db.rows(f"""SELECT o.code, o.purchase_date, o.customer_name, o.sold_by_name, o.total, COALESCE(o.status_value,'') status,
        CAST(julianday('now','localtime') - julianday(o.purchase_date) AS INTEGER) age_days, o.delivery_status_value
        FROM orders o {join} WHERE o.status IN (1,2,5) {sf} ORDER BY o.purchase_date LIMIT 300""", sp)
    # Thời gian từ đơn -> hoá đơn
    lead = db.one(f"""SELECT AVG(julianday(i.purchase_date) - julianday(o.purchase_date)) * 24 AS hours, COUNT(*) n
        FROM invoices i JOIN orders o ON o.code = i.order_code {joini} WHERE i.purchase_date BETWEEN ? AND ? {sf}""", (f, t) + sp)
    funnel_total = sum(r["n"] for r in orders)
    done = sum(r["n"] for r in orders if r["status"] == 3)
    cancel = sum(r["n"] for r in orders if r["status"] == 4)
    by_sale = db.rows(f"""SELECT COALESCE(o.sold_by_name,'(Không rõ)') sale, COUNT(*) n,
        SUM(CASE WHEN o.status=3 THEN 1 ELSE 0 END) done, SUM(CASE WHEN o.status=4 THEN 1 ELSE 0 END) cancelled,
        SUM(CASE WHEN o.status IN (1,2,5) THEN 1 ELSE 0 END) pending, COALESCE(SUM(CASE WHEN o.status=3 THEN o.total END),0) total
        FROM orders o {join} WHERE o.purchase_date BETWEEN ? AND ? {sf} GROUP BY 1 ORDER BY n DESC""", (f, t) + sp)
    channels = db.rows(f"SELECT COALESCE(o.sale_channel,'Trực tiếp') channel, COUNT(*) n, COALESCE(SUM(o.total),0) total FROM orders o {join} WHERE o.purchase_date BETWEEN ? AND ? {sf} GROUP BY 1 ORDER BY 2 DESC", (f, t) + sp)
    # Hành trình theo hoá đơn (cửa hàng xuất hoá đơn trực tiếp, ít dùng đơn đặt)
    inv_funnel = db.one(f"""SELECT COUNT(*) total, SUM(CASE WHEN i.status=1 THEN 1 ELSE 0 END) done,
        SUM(CASE WHEN i.status=2 THEN 1 ELSE 0 END) cancelled, SUM(CASE WHEN i.status NOT IN (1,2) THEN 1 ELSE 0 END) pending,
        SUM(CASE WHEN i.status=1 AND i.total_payment >= i.total - 1 THEN 1 ELSE 0 END) paid_full,
        SUM(CASE WHEN i.status=1 AND i.total_payment < i.total - 1 THEN 1 ELSE 0 END) unpaid,
        COALESCE(SUM(CASE WHEN i.status=1 THEN i.total - i.total_payment END),0) unpaid_amount,
        SUM(CASE WHEN i.using_cod=1 THEN 1 ELSE 0 END) cod,
        SUM(CASE WHEN i.delivery_status IS NOT NULL THEN 1 ELSE 0 END) with_delivery
        FROM invoices i {joini} WHERE i.purchase_date BETWEEN ? AND ? {sf}""", (f, t) + sp)
    inv_by_sale = db.rows(f"""SELECT COALESCE(i.sold_by_name,'(Không rõ)') sale, COUNT(*) n,
        SUM(CASE WHEN i.status=1 THEN 1 ELSE 0 END) done, SUM(CASE WHEN i.status=2 THEN 1 ELSE 0 END) cancelled,
        COALESCE(SUM(CASE WHEN i.status=1 THEN i.total END),0) total,
        COALESCE(SUM(CASE WHEN i.status=1 THEN i.total - i.total_payment END),0) unpaid,
        COUNT(DISTINCT i.customer_id) customers
        FROM invoices i {joini} WHERE i.purchase_date BETWEEN ? AND ? {sf} GROUP BY 1 ORDER BY total DESC""", (f, t) + sp)
    by_branch = db.rows(f"""SELECT COALESCE(i.branch_name,'?') branch, COUNT(*) n, COALESCE(SUM(CASE WHEN i.status=1 THEN i.total END),0) total
        FROM invoices i {joini} WHERE i.purchase_date BETWEEN ? AND ? {sf} GROUP BY 1 ORDER BY 2 DESC""", (f, t) + sp)
    by_hour = db.rows(f"""SELECT CAST(substr(i.purchase_date,12,2) AS INTEGER) h, COUNT(*) n FROM invoices i {joini}
        WHERE {INV_OK} AND i.purchase_date BETWEEN ? AND ? {sf} GROUP BY 1 ORDER BY 1""", (f, t) + sp)
    by_dow = db.rows(f"""SELECT CAST(strftime('%w', i.purchase_date) AS INTEGER) d, COUNT(*) n, COALESCE(SUM(i.total),0) total FROM invoices i {joini}
        WHERE {INV_OK} AND i.purchase_date BETWEEN ? AND ? {sf} GROUP BY 1 ORDER BY 1""", (f, t) + sp)
    unpaid_list = db.rows(f"""SELECT i.code, i.purchase_date, i.customer_name, i.sold_by_name, i.total, i.total_payment, i.total - i.total_payment remain,
        CAST(julianday('now','localtime') - julianday(i.purchase_date) AS INTEGER) age_days
        FROM invoices i {joini} WHERE i.status=1 AND i.total_payment < i.total - 1 {sf} ORDER BY remain DESC LIMIT 200""", sp)
    return {"orders": orders, "invoices": invs, "delivery": delivery, "stuck": stuck,
            "inv_funnel": inv_funnel, "inv_by_sale": inv_by_sale, "by_branch": by_branch, "by_hour": by_hour, "by_dow": by_dow, "unpaid_list": unpaid_list,
            "lead_hours": round(lead["hours"], 1) if lead and lead["hours"] is not None else None, "lead_n": lead["n"] if lead else 0,
            "funnel": {"total": funnel_total, "done": done, "cancelled": cancel, "pending": funnel_total - done - cancel,
                       "completion_rate": round(done / funnel_total * 100, 1) if funnel_total else None},
            "by_sale": by_sale, "channels": channels}


# ---------------------------------------------------------------------------
# 6. Sinh nhật
# ---------------------------------------------------------------------------
def birthdays(days_ahead=30, sale=None):
    sf, sp = _sale_filter(sale)
    today = date.today()
    sql = f"""SELECT c.id, c.code, c.name, c.contact_number, c.birth_date, c.gender, {sale_expr()} AS sale,
        (SELECT MAX(purchase_date) FROM invoices i WHERE i.customer_id=c.id AND {INV_OK}) last_purchase,
        (SELECT COALESCE(SUM(total),0) FROM invoices i WHERE i.customer_id=c.id AND {INV_OK}) lifetime_revenue
        FROM customers c WHERE c.is_active=1 AND c.birth_date IS NOT NULL AND c.birth_date <> '' AND substr(c.birth_date,1,4) > '1900' {sf}"""
    out = []
    for r in db.rows(sql, sp):
        try:
            bd = datetime.fromisoformat(r["birth_date"][:10]).date()
        except Exception:
            continue
        nxt = bd.replace(year=today.year) if not (bd.month == 2 and bd.day == 29) else date(today.year, 2, 28)
        if nxt < today:
            nxt = nxt.replace(year=today.year + 1)
        delta = (nxt - today).days
        if delta <= days_ahead:
            r["days_until"] = delta
            r["next_birthday"] = nxt.isoformat()
            r["age"] = nxt.year - bd.year
            r["birthday_ddmm"] = bd.strftime("%d/%m")
            out.append(r)
    out.sort(key=lambda r: (r["days_until"], r["name"] or ""))
    return out


# ---------------------------------------------------------------------------
def customer_detail(cid):
    c = db.one(f"SELECT c.*, {sale_expr()} AS sale FROM customers c WHERE id=?", (cid,))
    if not c:
        return None
    c.pop("raw", None)
    invs = db.rows("SELECT id, code, purchase_date, sold_by_name, total, status_value, delivery_status_value, branch_name FROM invoices WHERE customer_id=? ORDER BY purchase_date DESC LIMIT 100", (cid,))
    items = db.rows(f"""SELECT d.product_name, SUM(d.quantity) qty, SUM(d.sub_total) rev, MAX(i.purchase_date) last_d FROM invoice_details d
        JOIN invoices i ON i.id=d.invoice_id WHERE i.customer_id=? AND {INV_OK} GROUP BY d.product_id ORDER BY rev DESC LIMIT 50""", (cid,))
    monthly = db.rows(f"SELECT substr(purchase_date,1,7) ym, SUM(total) rev, COUNT(*) n FROM invoices i WHERE customer_id=? AND {INV_OK} GROUP BY 1 ORDER BY 1 DESC LIMIT 24", (cid,))
    notes = db.rows("SELECT * FROM notes WHERE customer_id=? ORDER BY created DESC", (cid,))
    return {"customer": c, "invoices": invs, "items": items, "monthly": monthly[::-1], "notes": notes}


def search_customers(q, sale=None, limit=50):
    sf, sp = _sale_filter(sale)
    like = f"%{q}%"
    return db.rows(f"""SELECT c.id, c.code, c.name, c.contact_number, {sale_expr()} sale, c.total_revenue, c.debt, c.groups
        FROM customers c WHERE c.is_active=1 AND (c.name LIKE ? OR c.code LIKE ? OR c.contact_number LIKE ?) {sf} ORDER BY c.total_revenue DESC LIMIT ?""",
                   (like, like, like) + sp + (limit,))


# ---------------------------------------------------------------------------
# BẢNG DOANH SỐ (mô phỏng bảng Excel của cửa hàng)
# ---------------------------------------------------------------------------
CAT_KEYS = [("normal", "Hàng thông thường"), ("excl_new", "Hàng hoá độc quyền mới"),
            ("excl_old", "Hàng hoá độc quyền cũ"), ("slow", "Hàng tồn lâu - cận date")]
DEFAULT_CAT_MAP = {
    "ĐỘC QUYỀN MỚI": "excl_new",
    "ĐỘC QUYỀN CŨ": "excl_old", "Độc Quyền": "excl_old",
    "HÀNG TỒN LÂU": "slow", "HÀNG CẬN DATE": "slow",
}


def cat_map():
    """Ánh xạ nhãn hiệu KiotViet -> cột nhóm hàng trong báo cáo (sửa được trong Cài đặt)."""
    import json
    raw = db.get_meta("cat_map")
    if raw:
        try:
            return json.loads(raw)
        except Exception:
            pass
    return dict(DEFAULT_CAT_MAP)


def trade_marks():
    """Danh sách nhãn hiệu đang có kèm doanh số 12 tháng, để cấu hình ánh xạ."""
    rows = db.rows(f"""SELECT COALESCE(p.trade_mark,'') tm, SUM(d.sub_total) rev, COUNT(DISTINCT p.id) n
        FROM invoice_details d JOIN invoices i ON i.id=d.invoice_id LEFT JOIN products p ON p.id=d.product_id
        WHERE {INV_OK} AND i.purchase_date >= date('now','-365 days') GROUP BY 1 ORDER BY 2 DESC""")
    m = cat_map()
    for r in rows:
        r["cat"] = m.get(r["tm"], "normal")
    return rows


def _seller_expr(basis):
    if basis == "invoice":
        return "COALESCE(NULLIF(i.sold_by_name,''),'(Không rõ)')"
    return "COALESCE(NULLIF(c.sale_manual,''), NULLIF(c.sale_name,''), '(Chưa gán)')"


def sales_board(from_d, to_d, basis="customer", ym=None):
    """Bảng doanh số: theo sale/team, tách khách sếp, tách nhóm hàng, so chỉ tiêu."""
    import json
    f, t, pf, pt = _period(from_d, to_d)
    ym = ym or from_d[:7]
    sx = _seller_expr(basis)
    cm = cat_map()

    # 1) Tổng theo hoá đơn: tách khách sale / khách sếp
    totals = {}
    for r in db.rows(f"""SELECT {sx} s,
            SUM(CASE WHEN COALESCE(c.is_boss,0)=1 THEN i.total ELSE 0 END) rev_boss,
            SUM(CASE WHEN COALESCE(c.is_boss,0)=0 THEN i.total ELSE 0 END) rev_sale,
            SUM(i.total) total, COUNT(*) invoices, COUNT(DISTINCT i.customer_id) customers
        FROM invoices i LEFT JOIN customers c ON c.id=i.customer_id
        WHERE {INV_OK} AND i.purchase_date BETWEEN ? AND ? GROUP BY 1""", (f, t)):
        totals[r["s"]] = r

    # 2) Kỳ trước (để so sánh)
    prev = {r["s"]: r["total"] for r in db.rows(f"""SELECT {sx} s, SUM(i.total) total
        FROM invoices i LEFT JOIN customers c ON c.id=i.customer_id
        WHERE {INV_OK} AND i.purchase_date BETWEEN ? AND ? GROUP BY 1""", (pf, pt))}

    # 3) Nhóm hàng (chỉ tính trên khách sale, giống bảng Excel)
    cats = {}
    for r in db.rows(f"""SELECT {sx} s, COALESCE(NULLIF(d.trade_mark,''), p.trade_mark, '') tm, SUM(d.sub_total) rev
        FROM invoice_details d JOIN invoices i ON i.id=d.invoice_id
        LEFT JOIN customers c ON c.id=i.customer_id LEFT JOIN products p ON p.id=d.product_id
        WHERE {INV_OK} AND COALESCE(c.is_boss,0)=0 AND i.purchase_date BETWEEN ? AND ? GROUP BY 1,2""", (f, t)):
        key = cm.get(r["tm"] or "", "normal")
        cats.setdefault(r["s"], {}).setdefault(key, 0)
        cats[r["s"]][key] += r["rev"] or 0

    # 4) Doanh số từ khách mua lần đầu trong kỳ
    newc = {r["s"]: r["rev"] for r in db.rows(f"""
        WITH firstbuy AS (SELECT customer_id, MIN(purchase_date) d FROM invoices i WHERE {INV_OK} GROUP BY 1)
        SELECT {sx} s, SUM(i.total) rev FROM invoices i
        LEFT JOIN customers c ON c.id=i.customer_id JOIN firstbuy fb ON fb.customer_id=i.customer_id
        WHERE {INV_OK} AND i.purchase_date BETWEEN ? AND ? AND fb.d BETWEEN ? AND ? GROUP BY 1""", (f, t, f, t))}

    targets = {r["sale"]: r for r in db.rows("SELECT * FROM kpi_targets WHERE ym=?", (ym,))}
    team_of = {r["sale"]: r["team"] for r in db.rows("SELECT sale, team FROM teams")}

    out = []
    for s in set(totals) | set(targets):
        tt = totals.get(s) or {}
        c = cats.get(s, {})
        tg = (targets.get(s) or {}).get("revenue_target") or 0
        row = {"sale": s, "team": team_of.get(s) or "(Chưa xếp team)",
               "rev_sale": tt.get("rev_sale", 0) or 0, "rev_boss": tt.get("rev_boss", 0) or 0,
               "total": tt.get("total", 0) or 0, "invoices": tt.get("invoices", 0) or 0,
               "customers": tt.get("customers", 0) or 0, "prev": prev.get(s, 0) or 0,
               "new_customer_rev": newc.get(s, 0) or 0, "target": tg}
        for k, _ in CAT_KEYS:
            row[k] = c.get(k, 0)
        row["pct"] = round(row["total"] / tg * 100, 1) if tg else None
        row["status"] = None if tg == 0 else ("ĐẠT" if row["total"] >= tg else "K ĐẠT")
        row["growth"] = _growth(row["total"], row["prev"])
        out.append(row)
    out.sort(key=lambda r: (r["team"], -r["total"]))

    teams = {}
    for r in out:
        g = teams.setdefault(r["team"], {"team": r["team"], "target": 0, "rev_sale": 0, "rev_boss": 0,
                                         "total": 0, "prev": 0, "new_customer_rev": 0,
                                         **{k: 0 for k, _ in CAT_KEYS}})
        for k in ("target", "rev_sale", "rev_boss", "total", "prev", "new_customer_rev") + tuple(k for k, _ in CAT_KEYS):
            g[k] += r[k]
    for g in teams.values():
        g["pct"] = round(g["total"] / g["target"] * 100, 1) if g["target"] else None
        g["status"] = None if not g["target"] else ("ĐẠT" if g["total"] >= g["target"] else "K ĐẠT")
    return {"basis": basis, "ym": ym, "from": from_d, "to": to_d, "rows": out,
            "teams": sorted(teams.values(), key=lambda g: -g["total"]), "cats": CAT_KEYS}
