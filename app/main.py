import json
import logging
import threading
from datetime import date, datetime, timedelta
from pathlib import Path

from fastapi import FastAPI, Request, HTTPException, Depends, Query
from fastapi.responses import FileResponse, StreamingResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import config, db, sync, reports, excel, backup, gsheet

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
app = FastAPI(title="KiotViet CRM", docs_url=None, redoc_url=None)
STATIC = Path(__file__).parent / "static"
from .main_auth import signer, COOKIE, _hash, current_user  # noqa
from . import extras
app.include_router(extras.router)


def sale_scope(user, sale):
    """Sale chỉ xem dữ liệu của mình; admin/kế toán xem tất cả (có thể lọc)."""
    if user.get("role") == "sale":
        return user.get("sale_name") or "__none__"
    return sale or None


@app.post("/api/login")
async def login(request: Request):
    body = await request.json()
    u, p = (body.get("username") or "").strip(), body.get("password") or ""
    user = None
    if u == config.ADMIN_USER and p == config.ADMIN_PASSWORD:
        user = {"username": u, "role": "admin", "display_name": "Quản trị", "sale_name": None}
    else:
        r = db.one("SELECT * FROM crm_users WHERE username=?", (u,))
        if r and r["password"] == _hash(p):
            user = {"username": u, "role": r["role"], "display_name": r["display_name"] or u, "sale_name": r["sale_name"]}
    if not user:
        raise HTTPException(401, "Sai tài khoản hoặc mật khẩu")
    resp = JSONResponse(user)
    resp.set_cookie(COOKIE, signer.dumps(user), httponly=True, samesite="lax", max_age=30 * 86400)
    return resp


@app.post("/api/logout")
def logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(COOKIE)
    return resp


@app.get("/api/me")
def me(user=Depends(current_user)):
    return user


# ---------------------------------------------------------------------------
# Sync & meta
# ---------------------------------------------------------------------------
@app.get("/api/status")
def status(user=Depends(current_user)):
    last = db.rows("SELECT * FROM sync_log ORDER BY id DESC LIMIT 5")
    counts = {t: db.one(f"SELECT COUNT(*) n FROM {t}")["n"] for t in ("customers", "products", "invoices", "orders")}
    return {"sync": sync.state, "last_sync": db.get_meta("last_sync"), "log": last, "counts": counts,
            "snapshot": backup.snapshot_info(), "last_verify": db.get_meta("last_verify"),
            "retailer": config.KIOT_RETAILER, "configured": bool(config.KIOT_CLIENT_ID and config.KIOT_CLIENT_SECRET),
            "sale_source": sync.sale_source(), "backup": backup.enabled(), "churn_days": config.CHURN_DAYS, "interval": config.SYNC_INTERVAL_MIN}


@app.post("/api/verify")
def verify_now(user=Depends(current_user)):
    """Đối chiếu số bản ghi với KiotViet và tải lại tháng nào thiếu."""
    if user["role"] not in ("admin", "accountant"):
        raise HTTPException(403)
    if sync.state["running"]:
        return {"ok": False, "message": "Đang đồng bộ"}

    def run():
        sync.state["running"] = True
        try:
            r = sync.verify_and_repair()
            db.set_meta("last_verify_report", json.dumps(r, ensure_ascii=False))
            sync.reapply_sales()
        except Exception:
            logging.exception("verify")
        finally:
            sync.state["running"] = False
            sync.state["progress"] = ""

    threading.Thread(target=run, daemon=True).start()
    return {"ok": True, "message": "Đang kiểm tra & vá dữ liệu"}


@app.post("/api/snapshot")
def snapshot_now(user=Depends(current_user)):
    if user["role"] != "admin":
        raise HTTPException(403)
    return {"ok": True, "result": backup.snapshot_db(force=True)}


@app.post("/api/sync")
def run_sync(full: bool = False, user=Depends(current_user)):
    if user["role"] not in ("admin", "accountant"):
        raise HTTPException(403)
    if sync.state["running"]:
        return {"ok": False, "message": "Đang đồng bộ"}
    threading.Thread(target=sync.sync_all, kwargs={"full": full}, daemon=True).start()
    return {"ok": True, "message": "Đã bắt đầu đồng bộ"}


@app.get("/api/sales")
def sales(user=Depends(current_user)):
    return reports.sales_list()


@app.post("/api/settings")
async def settings_save(request: Request, user=Depends(current_user)):
    """Đổi cách gán sale / số ngày ngừng mua, áp dụng ngay không cần deploy lại."""
    if user["role"] != "admin":
        raise HTTPException(403)
    b = await request.json()
    src = (b.get("sale_source") or "").lower()
    if src in ("auto", "name", "group", "created_by", "manual"):
        db.set_meta("sale_source", src)
    if b.get("churn_days"):
        db.set_meta("churn_days", str(int(b["churn_days"])))
        config.CHURN_DAYS = int(b["churn_days"])
    r = sync.reapply_async()
    return {"ok": True, "sale_source": sync.sale_source(), "pending": r != "started"}


# ---------------------------------------------------------------------------
# Xuất sang Google Sheet
# ---------------------------------------------------------------------------
@app.get("/api/gsheet")
def gsheet_status(user=Depends(current_user)):
    return gsheet.status()


@app.post("/api/gsheet")
async def gsheet_config(request: Request, user=Depends(current_user)):
    if user["role"] != "admin":
        raise HTTPException(403)
    b = await request.json()
    if b.get("sa"):
        try:
            json.loads(b["sa"])
        except ValueError:
            raise HTTPException(400, "Nội dung service account không phải JSON hợp lệ")
        db.set_meta("gsheet_sa", b["sa"])
    for k in ("gsheet_id", "gsheet_tpl"):
        if b.get(k) is not None:
            db.set_meta(k, str(b[k]).strip())
    return gsheet.status()


@app.post("/api/gsheet/export")
async def gsheet_export(request: Request, user=Depends(current_user)):
    if user["role"] not in ("admin", "manager"):
        raise HTTPException(403)
    b = await request.json()
    ym = (b.get("ym") or "").strip()
    if len(ym) != 7:
        raise HTTPException(400, "Thiếu tháng cần xuất (YYYY-MM)")
    try:
        return gsheet.export(ym, db.get_meta("report_basis") or "accounting",
                             b.get("title"), bool(b.get("replace")))
    except gsheet.ConfigError as e:
        raise HTTPException(400, str(e))


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------
def _dates(from_d, to_d):
    today = date.today()
    if not to_d:
        to_d = today.isoformat()
    if not from_d:
        from_d = today.replace(day=1).isoformat()
    return from_d, to_d


@app.get("/api/overview")
def overview(from_d: str = Query(None, alias="from"), to_d: str = Query(None, alias="to"), sale: str = None, user=Depends(current_user)):
    f, t = _dates(from_d, to_d)
    s = sale_scope(user, sale)
    today = date.today().isoformat()
    tcond, tp = ("", ()) if user["role"] != "sale" else (" AND assigned_to=?", (user.get("sale_name") or "",))
    return {"summary": reports.revenue_summary(f, t, s), "birthdays": reports.birthdays(7, s)[:10],
            "journey": reports.order_journey(f, t, s)["funnel"],
            "tasks_today": db.one(f"SELECT COUNT(*) n FROM tasks WHERE status='open' AND due_date=? {tcond}", (today,) + tp)["n"],
            "tasks_overdue": db.one(f"SELECT COUNT(*) n FROM tasks WHERE status='open' AND due_date<? {tcond}", (today,) + tp)["n"]}


@app.get("/api/report/sales")
def r_sales(from_d: str = Query(None, alias="from"), to_d: str = Query(None, alias="to"), user=Depends(current_user)):
    f, t = _dates(from_d, to_d)
    rows = reports.customers_by_sale(f, t)
    if user["role"] == "sale":
        rows = [r for r in rows if r["sale"] == user.get("sale_name")]
    return rows


@app.get("/api/report/trend")
def r_trend(months: int = 12, sale: str = None, user=Depends(current_user)):
    return reports.customer_trend(months, sale_scope(user, sale))


@app.get("/api/report/customers")
def r_customers(from_d: str = Query(None, alias="from"), to_d: str = Query(None, alias="to"), sale: str = None, user=Depends(current_user)):
    f, t = _dates(from_d, to_d)
    return reports.customer_revenue(f, t, sale_scope(user, sale))


@app.get("/api/report/products")
def r_products(from_d: str = Query(None, alias="from"), to_d: str = Query(None, alias="to"), sale: str = None, user=Depends(current_user)):
    f, t = _dates(from_d, to_d)
    return reports.product_velocity(f, t, sale_scope(user, sale))


@app.get("/api/report/journey")
def r_journey(from_d: str = Query(None, alias="from"), to_d: str = Query(None, alias="to"), sale: str = None, user=Depends(current_user)):
    f, t = _dates(from_d, to_d)
    return reports.order_journey(f, t, sale_scope(user, sale))


@app.get("/api/report/birthdays")
def r_birthdays(days: int = 30, sale: str = None, user=Depends(current_user)):
    return reports.birthdays(days, sale_scope(user, sale))


@app.get("/api/customers/search")
def c_search(q: str = "", sale: str = None, user=Depends(current_user)):
    return reports.search_customers(q, sale_scope(user, sale))


@app.get("/api/customers/{cid}")
def c_detail(cid: int, user=Depends(current_user)):
    d = reports.customer_detail(cid)
    if not d:
        raise HTTPException(404)
    if user["role"] == "sale" and d["customer"]["sale"] != user.get("sale_name"):
        raise HTTPException(403)
    return d


@app.post("/api/customers/{cid}/sale")
async def c_assign(cid: int, request: Request, user=Depends(current_user)):
    if user["role"] not in ("admin", "accountant"):
        raise HTTPException(403)
    body = await request.json()
    with db.cursor() as con:
        con.execute("UPDATE customers SET sale_manual=? WHERE id=?", ((body.get("sale") or "").strip(), cid))
    return {"ok": True}


@app.post("/api/customers/{cid}/notes")
async def c_note(cid: int, request: Request, user=Depends(current_user)):
    body = await request.json()
    with db.cursor() as con:
        con.execute("INSERT INTO notes(customer_id,created,author,content) VALUES(?,?,?,?)",
                    (cid, datetime.now().isoformat(timespec="minutes"), user["display_name"], body.get("content", "")))
    return {"ok": True}


# ---------------------------------------------------------------------------
# Users (admin)
# ---------------------------------------------------------------------------
@app.get("/api/users")
def users_list(user=Depends(current_user)):
    if user["role"] != "admin":
        raise HTTPException(403)
    return db.rows("SELECT username, role, sale_name, display_name FROM crm_users ORDER BY username")


@app.post("/api/users")
async def users_save(request: Request, user=Depends(current_user)):
    if user["role"] != "admin":
        raise HTTPException(403)
    b = await request.json()
    if not b.get("username"):
        raise HTTPException(400, "Thiếu tên đăng nhập")
    with db.cursor() as con:
        if b.get("delete"):
            con.execute("DELETE FROM crm_users WHERE username=?", (b["username"],))
            return {"ok": True}
        existing = con.execute("SELECT password FROM crm_users WHERE username=?", (b["username"],)).fetchone()
        pw = _hash(b["password"]) if b.get("password") else (existing["password"] if existing else None)
        if not pw:
            raise HTTPException(400, "Thiếu mật khẩu")
        con.execute("INSERT OR REPLACE INTO crm_users(username,password,role,sale_name,display_name) VALUES(?,?,?,?,?)",
                    (b["username"], pw, b.get("role", "sale"), b.get("sale_name"), b.get("display_name")))
    return {"ok": True}


@app.get("/api/aliases")
def aliases_list(user=Depends(current_user)):
    rows = db.rows("SELECT alias, sale_name FROM sale_aliases ORDER BY alias")
    raw = db.rows("SELECT sale_name AS s, COUNT(*) n FROM customers WHERE sale_name IS NOT NULL AND sale_name<>'' GROUP BY 1 ORDER BY 2 DESC")
    return {"aliases": rows, "detected": raw, "unknown": sync.unknown_suffixes()}


@app.post("/api/aliases")
async def aliases_save(request: Request, user=Depends(current_user)):
    if user["role"] not in ("admin", "accountant"):
        raise HTTPException(403)
    b = await request.json()
    alias = (b.get("alias") or "").strip()
    if not alias:
        raise HTTPException(400, "Thiếu bí danh")
    with db.cursor() as con:
        if b.get("delete"):
            con.execute("DELETE FROM sale_aliases WHERE alias=?", (alias,))
        else:
            con.execute("INSERT OR REPLACE INTO sale_aliases(alias,sale_name) VALUES(?,?)", (alias, (b.get("sale_name") or "").strip()))
    # áp dụng cho dữ liệu hiện có (chạy nền; nếu đang đồng bộ thì hoãn tới cuối kỳ)
    r = sync.reapply_async()
    return {"ok": True, "pending": r != "started"}


# ---------------------------------------------------------------------------
# Excel export
# ---------------------------------------------------------------------------
M, P = excel.MONEY, excel.PCT


@app.get("/api/export/{report}")
def export(report: str, from_d: str = Query(None, alias="from"), to_d: str = Query(None, alias="to"), sale: str = None,
           months: int = 12, days: int = 30, user=Depends(current_user)):
    f, t = _dates(from_d, to_d)
    s = sale_scope(user, sale)
    sheets = []
    if report == "sales":
        rows = reports.customers_by_sale(f, t)
        if user["role"] == "sale":
            rows = [r for r in rows if r["sale"] == user.get("sale_name")]
        sheets.append(("Khach theo sale", [("sale", "Sale"), ("total_customers", "Tổng KH"), ("new_customers", "KH mới"),
                        ("new_customers_prev", "KH mới kỳ trước"), ("buying_customers", "KH có mua"), ("buying_customers_prev", "KH có mua kỳ trước"),
                        ("customer_growth", "Tăng/giảm KH %", P), ("first_time_buyers", "Mua lần đầu"), ("gained_vs_prev", "KH quay lại/mới"),
                        ("lost_vs_prev", "KH mất so kỳ trước"), ("churned", "KH ngừng mua"), ("returned", "Hàng trả", M), ("revenue", "Doanh số", M),
                        ("revenue_prev", "Doanh số kỳ trước", M), ("revenue_growth", "Tăng/giảm DS %", P), ("invoices", "Số HĐ"), ("avg_invoice", "TB/HĐ", M)], rows))
    elif report == "trend":
        sheets.append(("Bien dong KH", [("ym", "Tháng"), ("new_customers", "KH mới"), ("first_buyers", "Mua lần đầu"), ("active_customers", "KH có mua"),
                        ("returning_customers", "KH quay lại"), ("lost_customers", "KH ngừng mua"), ("net_change", "Tăng/giảm ròng"),
                        ("cumulative_customers", "Tổng KH đầu kỳ"), ("returned", "Hàng trả", M), ("revenue", "Doanh số", M)], reports.customer_trend(months, s)))
    elif report == "customers":
        sheets.append(("Doanh so KH", [("code", "Mã KH"), ("name", "Tên KH"), ("contact_number", "SĐT"), ("sale", "Sale"), ("segment", "Phân nhóm"),
                        ("returned", "Hàng trả", M), ("revenue", "Doanh số", M), ("invoices", "Số HĐ"), ("revenue_prev", "DS kỳ trước", M), ("growth", "Tăng/giảm %", P),
                        ("lifetime_revenue", "DS tích luỹ", M), ("lifetime_invoices", "HĐ tích luỹ"), ("last_purchase", "Mua gần nhất"),
                        ("days_since_last", "Số ngày chưa mua"), ("debt", "Công nợ", M), ("groups", "Nhóm KH")], reports.customer_revenue(f, t, s, 5000)))
    elif report == "products":
        sheets.append(("Hang nhanh cham", [("code", "Mã hàng"), ("name", "Tên hàng"), ("category_name", "Nhóm hàng"), ("speed", "Tốc độ"),
                        ("qty", "SL bán"), ("qty_returned", "SL trả"), ("qty_prev", "SL kỳ trước"), ("growth", "Tăng/giảm %", P), ("returned", "Hàng trả", M), ("revenue", "Doanh số", M), ("qty_per_day", "SL/ngày"),
                        ("on_hand", "Tồn kho"), ("days_of_stock", "Ngày tồn còn"), ("stock_value", "Giá trị tồn", M), ("last_sold", "Bán gần nhất"),
                        ("days_since_sold", "Ngày chưa bán")], reports.product_velocity(f, t, s)))
    elif report == "journey":
        j = reports.order_journey(f, t, s)
        sheets.append(("Theo nhan vien ban", [("sale", "Nhân viên"), ("n", "HĐ"), ("done", "Hoàn thành"), ("cancelled", "Hủy"), ("customers", "KH"), ("total", "Doanh số", M), ("unpaid", "Chưa ghi nhận thu", M)], j["inv_by_sale"]))
        sheets.append(("Theo kho", [("branch", "Kho"), ("n", "HĐ"), ("total", "Doanh số", M)], j["by_branch"]))
        sheets.append(("Trang thai don", [("label", "Trạng thái"), ("n", "Số đơn"), ("total", "Giá trị", M)], j["orders"]))
        sheets.append(("Trang thai hoa don", [("label", "Trạng thái"), ("n", "Số HĐ"), ("total", "Giá trị", M)], j["invoices"]))
        sheets.append(("Giao hang", [("label", "Trạng thái giao"), ("n", "Số HĐ"), ("total", "Giá trị", M)], j["delivery"]))
        sheets.append(("Theo sale", [("sale", "Sale"), ("n", "Đơn"), ("done", "Hoàn thành"), ("cancelled", "Hủy"), ("pending", "Đang xử lý"), ("total", "Giá trị HT", M)], j["by_sale"]))
        sheets.append(("Don dang ket", [("code", "Mã đơn"), ("purchase_date", "Ngày"), ("customer_name", "Khách"), ("sold_by_name", "Sale"), ("status", "Trạng thái"),
                        ("delivery_status_value", "Giao hàng"), ("age_days", "Số ngày"), ("total", "Giá trị", M)], j["stuck"]))
    elif report == "board":
        basis = db.get_meta("report_basis") or "customer"
        b = reports.sales_board(f, t, basis)
        cols = ([("team", "Team"), ("sale", "Sale"), ("target", "Mục tiêu", M), ("rev_sale", "DS khách sale", M)]
                + [x for k, lbl in reports.CAT_KEYS for x in ((k, lbl, M), (k + "_pct", "% " + lbl, P))]
                + [("new_customer_rev", "Bán cho KH mới", M), ("new_customer_pct", "% KH mới", P),
                   ("rev_boss", "DS khách sếp", M), ("gross", "Doanh số bán", M), ("returns", "Hàng trả", M),
                   ("total", "TỔNG (đã trừ trả)", M), ("pct", "% KPI", P), ("status", "Tình trạng"),
                   ("prev", "Kỳ trước", M), ("invoices", "Số HĐ"), ("customers", "Số KH")])
        sheets.append(("Bang doanh so", cols, b["rows"]))
        tcols = ([("team", "Team"), ("target", "Mục tiêu", M), ("rev_sale", "DS khách sale", M)]
                 + [(k, lbl, M) for k, lbl in reports.CAT_KEYS]
                 + [("new_customer_rev", "Bán cho KH mới", M), ("rev_boss", "DS khách sếp", M),
                    ("gross", "Doanh số bán", M), ("returns", "Hàng trả", M),
                    ("total", "TỔNG (đã trừ trả)", M), ("pct", "% KPI", P), ("status", "Tình trạng")])
        sheets.append(("Tong team", tcols, b["teams"]))
    elif report == "birthdays":
        sheets.append(("Sinh nhat", [("next_birthday", "Ngày SN"), ("days_until", "Còn (ngày)"), ("code", "Mã KH"), ("name", "Tên"), ("contact_number", "SĐT"),
                        ("age", "Tuổi"), ("sale", "Sale"), ("last_purchase", "Mua gần nhất"), ("lifetime_revenue", "DS tích luỹ", M)], reports.birthdays(days, s)))
    else:
        raise HTTPException(404)
    buf = excel.to_xlsx(sheets)
    name = f"{report}_{f}_{t}.xlsx"
    return StreamingResponse(buf, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                             headers={"Content-Disposition": f"attachment; filename={name}"})


# ---------------------------------------------------------------------------
@app.get("/health")
def health():
    """Điểm ping để giữ web không ngủ (UptimeRobot / cron-job.org gọi mỗi 5 phút)."""
    return {"ok": True, "syncing": sync.state["running"], "last_sync": db.get_meta("last_sync")}


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.on_event("startup")
def startup():
    backup.restore_db()      # lấy lại toàn bộ DB nếu ổ đĩa vừa bị xoá
    db.init()
    backup.restore()
    cd = db.get_meta("churn_days")
    if cd:
        config.CHURN_DAYS = int(cd)
    sync.start_scheduler()


@app.middleware("http")
async def backup_after_write(request: Request, call_next):
    """Mọi thao tác ghi (POST) vào dữ liệu nhập tay -> sao lưu nền sang Postgres."""
    resp = await call_next(request)
    if request.method == "POST" and resp.status_code < 400 and request.url.path.startswith("/api/") \
            and not request.url.path.startswith(("/api/login", "/api/logout", "/api/sync")):
        backup.save()
    return resp
