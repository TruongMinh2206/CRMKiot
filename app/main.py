import hmac
import json
import logging
import re
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
from .main_auth import (COOKIE, current_user, scope, allowed, is_mgr, is_admin, can, team_names, col_in, task_cond,  # noqa
                        PAGES, PERMS, SCOPES, ROLE_DEPT, hash_pw, check_pw, resolve, session_cookie, seed_roles,
                        roles_list, slug, pages_for)
from . import extras, campaign, events, tools, work, rooms
app.include_router(extras.router)
app.include_router(campaign.router)
app.include_router(events.router)
app.include_router(tools.router)
app.include_router(work.router)
app.include_router(rooms.router)


def sale_scope(user, sale):
    """Sale xem của mình, trưởng nhóm xem cả team, admin/kế toán xem tất cả (có thể lọc)."""
    return scope(user, sale)


_fails = {}   # tên đăng nhập -> [số lần sai, thời điểm mở khoá]


def _login_cookie(resp, user):
    resp.set_cookie(COOKIE, session_cookie(user), httponly=True, samesite="lax", max_age=30 * 86400)
    return resp


@app.post("/api/login")
async def login(request: Request):
    body = await request.json()
    u, p = (body.get("username") or "").strip(), body.get("password") or ""
    key = u.lower()
    n, until = _fails.get(key, (0, None))
    if until and datetime.now() < until:
        raise HTTPException(429, f"Sai mật khẩu nhiều lần. Thử lại sau {max(1, int((until - datetime.now()).total_seconds() // 60) + 1)} phút")
    user = None
    if u == config.ADMIN_USER and hmac.compare_digest(p.encode(), config.ADMIN_PASSWORD.encode()):
        user = resolve(u, env=True)
    else:
        r = db.one("SELECT * FROM crm_users WHERE username=?", (u,))
        ok, rehash = check_pw(p, r["password"]) if r else (False, False)
        if ok and r.get("active") == 0:
            raise HTTPException(403, "Tài khoản đã bị khoá. Liên hệ quản trị")
        if ok:
            with db.cursor() as con:
                con.execute("UPDATE crm_users SET last_login=?" + (", password=?" if rehash else "") + " WHERE username=?",
                            (datetime.now().isoformat(timespec="seconds"),) + ((hash_pw(p),) if rehash else ()) + (u,))
            user = resolve(u)
    if not user:
        n += 1
        _fails[key] = (n, datetime.now() + timedelta(minutes=5) if n >= 5 else None)
        raise HTTPException(401, "Sai tài khoản hoặc mật khẩu")
    _fails.pop(key, None)
    return _login_cookie(JSONResponse(user), user)


@app.post("/api/logout")
def logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(COOKIE)
    return resp


@app.get("/api/me")
def me(user=Depends(current_user)):
    return user


@app.post("/api/me/password")
async def me_password(request: Request, user=Depends(current_user)):
    """Tự đổi mật khẩu. Các phiên đăng nhập khác của tài khoản này bị đăng xuất."""
    if user.get("env"):
        raise HTTPException(400, "Tài khoản quản trị gốc đổi mật khẩu ở biến ADMIN_PASSWORD trên Render / file .env")
    b = await request.json()
    old, new = b.get("old") or "", b.get("new") or ""
    r = db.one("SELECT password, ver FROM crm_users WHERE username=?", (user["username"],))
    if not r or not check_pw(old, r["password"])[0]:
        raise HTTPException(400, "Mật khẩu hiện tại không đúng")
    if len(new) < 6:
        raise HTTPException(400, "Mật khẩu mới tối thiểu 6 ký tự")
    with db.cursor() as con:
        con.execute("UPDATE crm_users SET password=?, ver=COALESCE(ver,0)+1 WHERE username=?", (hash_pw(new), user["username"]))
    return _login_cookie(JSONResponse({"ok": True}), resolve(user["username"]))


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
    if not can(user, "sync"):
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
    if not is_admin(user):
        raise HTTPException(403)
    return {"ok": True, "result": backup.snapshot_db(force=True)}


@app.post("/api/sync")
def run_sync(full: bool = False, user=Depends(current_user)):
    if not can(user, "sync"):
        raise HTTPException(403)
    if sync.state["running"]:
        return {"ok": False, "message": "Đang đồng bộ"}
    threading.Thread(target=sync.sync_all, kwargs={"full": full}, daemon=True).start()
    return {"ok": True, "message": "Đã bắt đầu đồng bộ"}


@app.get("/api/sales")
def sales(user=Depends(current_user)):
    allnames = reports.sales_list()
    mine = team_names(user)                   # trưởng nhóm / sale chỉ lọc trong phạm vi của mình
    if mine is not None:
        return [x for x in allnames if x in mine] or mine
    return allnames


@app.post("/api/settings")
async def settings_save(request: Request, user=Depends(current_user)):
    """Đổi cách gán sale / số ngày ngừng mua, áp dụng ngay không cần deploy lại."""
    if not is_admin(user):
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
    """Tài khoản Google dùng chung + danh sách các file Google Sheet mà từng chức năng đang kết nối."""
    s = gsheet.status()
    s["sa_ok"] = bool(db.get_meta("gsheet_sa"))
    sid = work._sheet_id
    s["sheets"] = [{"name": "Xuất bảng doanh số", "id": sid(db.get_meta("gsheet_id") or ""), "access": "Người chỉnh sửa", "where": "Khung “Xuất bảng doanh số” ngay dưới"},
                   {"name": "Thưởng chiến dịch – đọc dữ liệu", "id": sid(db.get_meta("campaign_gsheet_id") or ""), "access": "Người xem", "where": "Trang Thưởng chiến dịch"},
                   {"name": "Thưởng chiến dịch – xuất kết quả", "id": sid(db.get_meta("campaign_export_id") or ""), "access": "Người chỉnh sửa", "where": "Trang Thưởng chiến dịch"}]
    for d in work.depts(False):
        ls = work.last_sync(d["code"]) or {}
        s["sheets"].append({"name": f"Giao việc – {d['name']}", "id": d["cfg"].get("sheet_id") or "", "access": "Người xem",
                            "where": "Giao việc & KPI → KPI → Cài đặt phòng", "error": ls.get("error"), "at": ls.get("at")})
    return s


@app.post("/api/gsheet")
async def gsheet_config(request: Request, user=Depends(current_user)):
    if not is_admin(user):
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
    if b.get("sale_merge") is not None:
        # {"Sales Trang": "Sales Linh Bé"} — gộp doanh số người bán phụ vào sale chính
        try:
            m = json.loads(b["sale_merge"]) if isinstance(b["sale_merge"], str) else b["sale_merge"]
            assert isinstance(m, dict) and all(isinstance(v, str) for v in m.values())
        except Exception:
            raise HTTPException(400, "Thiết lập gộp người bán không hợp lệ")
        db.set_meta("sale_merge", json.dumps(m, ensure_ascii=False))
    return gsheet.status()


@app.post("/api/gsheet/export")
async def gsheet_export(request: Request, user=Depends(current_user)):
    if not is_mgr(user):
        raise HTTPException(403)
    b = await request.json()
    f, t = (b.get("from") or "").strip(), (b.get("to") or "").strip()
    if not (f and t):
        ym = (b.get("ym") or "").strip()          # tương thích cách gọi cũ
        if len(ym) != 7:
            raise HTTPException(400, "Thiếu khoảng ngày cần xuất")
        import calendar
        y, mth = int(ym[:4]), int(ym[5:7])
        f, t = f"{ym}-01", f"{ym}-{calendar.monthrange(y, mth)[1]:02d}"
    if t < f:
        raise HTTPException(400, "Ngày kết thúc nhỏ hơn ngày bắt đầu")
    try:
        return gsheet.export(f, t, db.get_meta("report_basis") or "accounting",
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
    tcond, tp = task_cond(user, "assigned_to")
    return {"summary": reports.revenue_summary(f, t, s), "birthdays": reports.birthdays(7, s)[:10],
            "journey": reports.order_journey(f, t, s)["funnel"],
            "tasks_today": db.one(f"SELECT COUNT(*) n FROM tasks WHERE status='open' AND due_date=? {tcond}", (today,) + tp)["n"],
            "tasks_overdue": db.one(f"SELECT COUNT(*) n FROM tasks WHERE status='open' AND due_date<? {tcond}", (today,) + tp)["n"]}


@app.get("/api/report/sales")
def r_sales(from_d: str = Query(None, alias="from"), to_d: str = Query(None, alias="to"), user=Depends(current_user)):
    f, t = _dates(from_d, to_d)
    return [r for r in reports.customers_by_sale(f, t) if allowed(user, r["sale"])]


@app.get("/api/report/trend")
def r_trend(months: int = 12, sale: str = None, user=Depends(current_user)):
    return reports.customer_trend(months, sale_scope(user, sale))


@app.get("/api/report/trend/list")
def r_trend_list(ym: str, kind: str, sale: str = None, user=Depends(current_user)):
    if kind not in reports.TREND_KINDS or not re.fullmatch(r"\d{4}-\d{2}", ym or ""):
        raise HTTPException(400, "Tham số không hợp lệ")
    return reports.trend_list(ym, kind, sale_scope(user, sale))


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
    if not allowed(user, d["customer"]["sale"]):
        raise HTTPException(403)
    return d


@app.post("/api/customers/{cid}/sale")
async def c_assign(cid: int, request: Request, user=Depends(current_user)):
    if not is_mgr(user):
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
# Tài khoản & nhóm quyền (chỉ Quản trị)
# ---------------------------------------------------------------------------
def _admin_only(user):
    if not is_admin(user):
        raise HTTPException(403, "Chỉ quản trị được quản lý tài khoản và phân quyền")


@app.get("/api/users")
def users_list(user=Depends(current_user)):
    _admin_only(user)
    return {"users": db.rows("""SELECT username, display_name, role, dept, sale_name, COALESCE(active,1) active, created, last_login
                                FROM crm_users ORDER BY role, username"""),
            "roles": roles_list(), "pages": [p for p in PAGES if p != "settings"], "perms": PERMS, "scopes": SCOPES,
            "depts": events.DEPTS, "role_dept": ROLE_DEPT, "admin_user": config.ADMIN_USER, "me": user["username"]}


@app.post("/api/users")
async def users_save(request: Request, user=Depends(current_user)):
    _admin_only(user)
    b = await request.json()
    name = (b.get("username") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9._-]{2,40}", name):
        raise HTTPException(400, "Tên đăng nhập 2–40 ký tự: chữ không dấu, số, dấu chấm, gạch dưới hoặc gạch ngang")
    if name.lower() == config.ADMIN_USER.lower():
        raise HTTPException(400, "Trùng tên tài khoản quản trị gốc")
    with db.cursor() as con:
        old = con.execute("SELECT * FROM crm_users WHERE username=?", (name,)).fetchone()
        if b.get("delete"):
            if name == user["username"]:
                raise HTTPException(400, "Không tự xoá tài khoản đang đăng nhập")
            con.execute("DELETE FROM crm_users WHERE username=?", (name,))
            return {"ok": True}
        if b.get("create") and old:
            raise HTTPException(400, "Tên đăng nhập đã có, chọn tên khác")
        role = b.get("role") or (old["role"] if old else "sale")
        r = con.execute("SELECT * FROM roles WHERE code=?", (role,)).fetchone()
        if not r:
            raise HTTPException(400, "Nhóm quyền không tồn tại")
        keep = lambda k, default=None: b[k] if k in b else (old[k] if old else default)   # sửa một phần: ô không gửi thì giữ nguyên
        sale = (keep("sale_name") or "").strip() or None
        if role != "admin" and r["scope"] in ("own", "team") and not sale:
            raise HTTPException(400, f"Nhóm “{r['name']}” xem dữ liệu theo sale — chọn tên sale cho tài khoản")
        active = 0 if keep("active", 1) in (False, 0, "0") else 1
        if not active and name == user["username"]:
            raise HTTPException(400, "Không tự khoá tài khoản đang đăng nhập")
        pw = b.get("password") or ""
        if pw and len(pw) < 6:
            raise HTTPException(400, "Mật khẩu tối thiểu 6 ký tự")
        if not old and not pw:
            raise HTTPException(400, "Nhập mật khẩu cho tài khoản mới")
        dept = keep("dept") if keep("dept") in events.DEPTS else ROLE_DEPT.get(role, "")
        vals = ((keep("display_name") or "").strip() or name, role, dept, sale, active)
        if old:
            con.execute("UPDATE crm_users SET display_name=?, role=?, dept=?, sale_name=?, active=?"
                        + (", password=?, ver=COALESCE(ver,0)+1" if pw else "") + " WHERE username=?",
                        vals + ((hash_pw(pw),) if pw else ()) + (name,))
        else:
            con.execute("INSERT INTO crm_users(display_name,role,dept,sale_name,active,username,password,ver,created) VALUES(?,?,?,?,?,?,?,0,?)",
                        vals + (name, hash_pw(pw), datetime.now().isoformat(timespec="seconds")))
    return {"ok": True}


@app.get("/api/roles")
def roles_get(user=Depends(current_user)):
    _admin_only(user)
    return roles_list()


@app.post("/api/roles")
async def roles_save(request: Request, user=Depends(current_user)):
    """Lưu một hoặc nhiều nhóm quyền ({roles: [...]}), tạo nhóm mới, hoặc xoá nhóm tự tạo ({delete: mã})."""
    _admin_only(user)
    b = await request.json()
    with db.cursor() as con:
        if b.get("delete"):
            r = con.execute("SELECT builtin FROM roles WHERE code=?", (b["delete"],)).fetchone()
            if not r:
                raise HTTPException(404, "Không tìm thấy nhóm quyền")
            if r["builtin"]:
                raise HTTPException(400, "Nhóm có sẵn không xoá được, chỉ chỉnh quyền")
            n = con.execute("SELECT COUNT(*) FROM crm_users WHERE role=?", (b["delete"],)).fetchone()[0]
            if n:
                raise HTTPException(400, f"Còn {n} tài khoản thuộc nhóm này — chuyển họ sang nhóm khác trước")
            con.execute("DELETE FROM roles WHERE code=?", (b["delete"],))
            return {"ok": True}
        for x in b.get("roles") or [b]:
            name = (x.get("name") or "").strip()
            if not name:
                raise HTTPException(400, "Nhập tên nhóm quyền")
            code = x.get("code")
            if code == "admin":
                continue                       # Quản trị luôn có toàn quyền
            vals = (name, json.dumps([p for p in x.get("pages") or [] if p in PAGES and p != "settings"]),
                    x.get("scope") if x.get("scope") in SCOPES else "own", json.dumps([p for p in x.get("perms") or [] if p in PERMS]))
            if code and con.execute("SELECT 1 FROM roles WHERE code=?", (code,)).fetchone():
                con.execute("UPDATE roles SET name=?, pages=?, scope=?, perms=? WHERE code=?", vals + (code,))
            else:
                base = code = slug(name)
                i = 2
                while con.execute("SELECT 1 FROM roles WHERE code=?", (code,)).fetchone():
                    code, i = f"{base}_{i}", i + 1
                con.execute("INSERT INTO roles(name,pages,scope,perms,code,builtin,position) VALUES(?,?,?,?,?,0,99)", vals + (code,))
    return {"ok": True, "roles": roles_list()}


@app.get("/api/aliases")
def aliases_list(user=Depends(current_user)):
    rows = db.rows("SELECT alias, sale_name FROM sale_aliases ORDER BY alias")
    raw = db.rows("SELECT sale_name AS s, COUNT(*) n FROM customers WHERE sale_name IS NOT NULL AND sale_name<>'' GROUP BY 1 ORDER BY 2 DESC")
    return {"aliases": rows, "detected": raw, "unknown": sync.unknown_suffixes()}


@app.post("/api/aliases")
async def aliases_save(request: Request, user=Depends(current_user)):
    if not is_mgr(user):
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
           months: int = 12, days: int = 30, ym: str = None, kind: str = None, user=Depends(current_user)):
    if not can(user, "export"):
        raise HTTPException(403, "Tài khoản chưa được cấp quyền xuất Excel")
    f, t = _dates(from_d, to_d)
    s = sale_scope(user, sale)
    sheets = []
    if report == "sales":
        rows = [r for r in reports.customers_by_sale(f, t) if allowed(user, r["sale"])]
        sheets.append(("Khach theo sale", [("sale", "Sale"), ("total_customers", "Tổng KH"), ("new_customers", "KH mới"),
                        ("new_customers_prev", "KH mới kỳ trước"), ("buying_customers", "KH có mua"), ("buying_customers_prev", "KH có mua kỳ trước"),
                        ("customer_growth", "Tăng/giảm KH %", P), ("first_time_buyers", "Mua lần đầu"), ("gained_vs_prev", "KH quay lại/mới"),
                        ("lost_vs_prev", "KH mất so kỳ trước"), ("churned", "KH ngừng mua"), ("returned", "Hàng trả", M), ("revenue", "Doanh số", M),
                        ("revenue_prev", "Doanh số kỳ trước", M), ("revenue_growth", "Tăng/giảm DS %", P), ("invoices", "Số HĐ"), ("avg_invoice", "TB/HĐ", M)], rows))
    elif report == "trend":
        sheets.append(("Bien dong KH", [("ym", "Tháng"), ("new_customers", "KH mới"), ("first_buyers", "Mua lần đầu"), ("active_customers", "KH có mua"),
                        ("returning_customers", "KH quay lại"), ("lost_customers", "KH ngừng mua"), ("net_change", "Tăng/giảm ròng"),
                        ("cumulative_customers", "Tổng KH đầu kỳ"), ("returned", "Hàng trả", M), ("revenue", "Doanh số", M)], reports.customer_trend(months, s)))
    elif report == "trend_list":
        if kind not in reports.TREND_KINDS or not re.fullmatch(r"\d{4}-\d{2}", ym or ""):
            raise HTTPException(400, "Tham số không hợp lệ")
        sheets.append((f"{kind} {ym}", [("code", "Mã KH"), ("name", "Tên KH"), ("contact_number", "SĐT"), ("sale", "Sale"),
                        ("created_date", "Ngày tạo"), ("revenue", f"Doanh số tháng {ym[5:]}/{ym[:4]}", M), ("invoices", "Số HĐ trong tháng"),
                        ("first_purchase", "Mua lần đầu"), ("last_purchase", "Mua gần nhất"), ("days_since_last", "Số ngày chưa mua"),
                        ("lifetime_revenue", "DS tích luỹ", M), ("lifetime_invoices", "HĐ tích luỹ"), ("debt", "Công nợ", M)],
                       reports.trend_list(ym, kind, s)))
    elif report == "customers":
        sheets.append(("Doanh so KH", [("code", "Mã KH"), ("name", "Tên KH"), ("contact_number", "SĐT"), ("sale", "Sale"), ("segment", "Phân nhóm"),
                        ("returned", "Hàng trả", M), ("revenue", "Doanh số", M), ("invoices", "Số HĐ"), ("revenue_prev", "DS kỳ trước", M), ("growth", "Tăng/giảm %", P),
                        ("lifetime_revenue", "DS tích luỹ", M), ("lifetime_invoices", "HĐ tích luỹ"), ("last_purchase", "Mua gần nhất"),
                        ("days_since_last", "Số ngày chưa mua"), ("debt", "Công nợ", M), ("groups", "Nhóm KH")], reports.customer_revenue(f, t, s, 5000)))
    elif report == "products":
        sheets.append(("Hang nhanh cham", [("code", "Mã hàng"), ("name", "Tên hàng"), ("category_name", "Nhóm hàng"), ("speed", "Tốc độ"),
                        ("qty", "SL bán"), ("qty_returned", "SL trả"), ("qty_prev", "SL kỳ trước"), ("growth", "Tăng/giảm %", P), ("returned", "Hàng trả", M), ("revenue", "Doanh số", M), ("qty_per_day", "SL/ngày"),
                        ("on_hand", "Tồn kho"), ("days_of_stock", "Ngày tồn còn"), ("reorder_qty", "Đề xuất nhập (đủ 30 ngày)"), ("stock_value", "Giá trị tồn", M), ("last_sold", "Bán gần nhất"),
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
    elif report == "campaign":
        sheets.append(("Thuong chien dich", [("stt", "STT"), ("grp", "Khách / Team"), ("codes", "Mã KH"), ("owner", "Phụ trách"),
                        ("program", "Chương trình"), ("reward", "Thưởng"), ("registered", "Số khách ĐK"), ("start", "Bắt đầu"),
                        ("carry", "Dư mang sang", M), ("qty", "Số lượng tính", M), ("amount", "Doanh số", M),
                        ("value", "Số thực đạt", M), ("result", "Kết quả"), ("gift", "Quy đổi quà"),
                        ("gap", "Còn thiếu", M), ("next", "Tới mốc"), ("last", "Đơn gần nhất"),
                        ("paid", "Đã trả thưởng"), ("note", "Ghi chú")], campaign.export_sheets(user)))
    elif report == "debts":
        d = reports.debt_aging(s)
        sheets.append(("Cong no", [("code", "Mã KH"), ("name", "Tên KH"), ("contact_number", "SĐT"), ("sale", "Sale"),
                        ("debt", "Tổng nợ", M), ("b0", "0-30 ngày", M), ("b1", "31-60 ngày", M), ("b2", "61-90 ngày", M),
                        ("b3", "Trên 90 ngày", M), ("overdue", f"Quá hạn (>{d['term']} ngày)", M),
                        ("oldest_days", "Khoản cũ nhất (ngày)"), ("last_purchase", "Mua gần nhất"),
                        ("days_since_last", "Số ngày chưa mua")], d["rows"]))
        sheets.append(("Theo sale", [("sale", "Sale"), ("customers", "Số KH nợ"), ("debt", "Tổng nợ", M), ("b0", "0-30 ngày", M),
                        ("b1", "31-60 ngày", M), ("b2", "61-90 ngày", M), ("b3", "Trên 90 ngày", M),
                        ("overdue", "Quá hạn", M)], d["by_sale"]))
    elif report == "board":
        if not is_mgr(user):
            raise HTTPException(403)
        basis = db.get_meta("report_basis") or "customer"
        b = reports.sales_board(f, t, basis)
        cols = ([("team", "Team"), ("sale", "Sale"), ("target", "Mục tiêu", M), ("rev_sale", "DS khách sale", M)]
                + [x for k, lbl in reports.CAT_KEYS for x in ((k, lbl, M), (k + "_pct", "% " + lbl, P))]
                + [("new_customer_rev", "Bán cho KH mới", M), ("new_customer_pct", "% KH mới", P),
                   ("rev_boss", "DS khách sếp", M), ("gross", "Doanh số bán", M), ("returns", "Hàng trả", M),
                   ("cancelled", "HĐ huỷ", M), ("cancelled_n", "Số HĐ huỷ"),
                   ("total", "TỔNG", M), ("pct", "% KPI", P), ("status", "Tình trạng"),
                   ("prev", "Kỳ trước", M), ("invoices", "Số HĐ"), ("customers", "Số KH")])
        sheets.append(("Bang doanh so", cols, b["rows"]))
        tcols = ([("team", "Team"), ("target", "Mục tiêu", M), ("rev_sale", "DS khách sale", M)]
                 + [(k, lbl, M) for k, lbl in reports.CAT_KEYS]
                 + [("new_customer_rev", "Bán cho KH mới", M), ("rev_boss", "DS khách sếp", M),
                    ("gross", "Doanh số bán", M), ("returns", "Hàng trả", M),
                    ("cancelled", "HĐ huỷ", M), ("cancelled_n", "Số HĐ huỷ"),
                    ("total", "TỔNG", M), ("pct", "% KPI", P), ("status", "Tình trạng")])
        sheets.append(("Tong team", tcols, b["teams"]))
        sheets.append(("HD da huy", [("code", "Mã HĐ"), ("d", "Ngày"), ("customer", "Khách hàng"),
                                     ("sale", "Người bán"), ("total", "Giá trị", M)],
                       reports.cancelled_invoices(f, t)))
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
    # luôn tải trang mới nhất sau mỗi lần cập nhật CRM, tránh trình duyệt giữ bản cũ
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache, must-revalidate"})


app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.on_event("startup")
def startup():
    backup.restore_db()      # lấy lại toàn bộ DB nếu ổ đĩa vừa bị xoá
    db.init()
    backup.restore()
    seed_roles()
    tools.seed()
    work.seed()
    rooms.seed()
    events.upgrade()
    cd = db.get_meta("churn_days")
    if cd:
        config.CHURN_DAYS = int(cd)
    sync.start_scheduler()


@app.middleware("http")
async def page_guard(request: Request, call_next):
    """Chặn API của những trang mà nhóm quyền của người dùng không được xem."""
    need = pages_for(request.url.path)
    if need:
        try:
            user = current_user(request)
        except HTTPException:
            user = None                       # để chính API trả lời 401
        if user and not need & set(user["pages"]):
            return JSONResponse({"detail": "Tài khoản chưa được cấp quyền xem mục này"}, status_code=403)
    return await call_next(request)


@app.middleware("http")
async def backup_after_write(request: Request, call_next):
    """Mọi thao tác ghi (POST) vào dữ liệu nhập tay -> sao lưu nền sang Postgres."""
    resp = await call_next(request)
    if request.method == "POST" and resp.status_code < 400 and request.url.path.startswith("/api/") \
            and not request.url.path.startswith(("/api/login", "/api/logout", "/api/sync")):
        backup.save()
    return resp
