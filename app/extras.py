"""KPI theo sale · Công việc & nhắc việc · Khách tiềm năng."""
from datetime import date, datetime, timedelta
from fastapi import APIRouter, Depends, HTTPException, Request, Query
from . import db, reports, config
from .main_auth import current_user

router = APIRouter(prefix="/api")

LEAD_STATUS = ["new", "contacted", "consulting", "won", "lost"]
LEAD_LABEL = {"new": "Mới", "contacted": "Đã liên hệ", "consulting": "Đang tư vấn", "won": "Đã chốt", "lost": "Thất bại"}
TASK_TYPES = {"call": "Gọi điện", "visit": "Ghé thăm", "message": "Nhắn tin", "birthday": "Chúc sinh nhật",
              "reorder": "Nhắc mua lại", "winback": "Kéo lại KH", "other": "Khác"}


def _now():
    return datetime.now().isoformat(timespec="minutes")


def _is_mgr(user):
    return user["role"] in ("admin", "accountant")


def _my_sale(user):
    return user.get("sale_name") if user["role"] == "sale" else None


# ===========================================================================
# KPI
# ===========================================================================
def kpi_report(ym):
    first = date.fromisoformat(ym + "-01")
    last = (first.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    to_d = min(last, date.today()) if first <= date.today() else first
    actual = {r["sale"]: r for r in reports.customers_by_sale(first.isoformat(), to_d.isoformat())}
    targets = {r["sale"]: r for r in db.rows("SELECT * FROM kpi_targets WHERE ym=?", (ym,))}
    sales = set(actual) | set(targets)
    days_total = (last - first).days + 1
    days_passed = max(1, min(days_total, (date.today() - first).days + 1)) if first <= date.today() else 0
    out = []
    for s in sales:
        a = actual.get(s, {})
        t = targets.get(s, {})
        row = {"sale": s, "revenue": a.get("revenue", 0), "new_customers": a.get("new_customers", 0),
               "buying_customers": a.get("buying_customers", 0), "invoices": a.get("invoices", 0),
               "revenue_target": t.get("revenue_target") or 0, "new_customers_target": t.get("new_customers_target") or 0,
               "buying_customers_target": t.get("buying_customers_target") or 0}
        for k in ("revenue", "new_customers", "buying_customers"):
            tg = row[k + "_target"]
            row[k + "_pct"] = round(row[k] / tg * 100, 1) if tg else None
        # dự báo cuối tháng theo tốc độ hiện tại
        row["revenue_forecast"] = round(row["revenue"] / days_passed * days_total) if days_passed else 0
        row["forecast_pct"] = round(row["revenue_forecast"] / row["revenue_target"] * 100, 1) if row["revenue_target"] else None
        out.append(row)
    out.sort(key=lambda r: (-(r["revenue_pct"] or -1), -r["revenue"]))
    for i, r in enumerate(out, 1):
        r["rank"] = i
    return {"ym": ym, "days_total": days_total, "days_passed": days_passed, "rows": out,
            "time_pct": round(days_passed / days_total * 100) if days_total else 0}


@router.get("/kpi")
def kpi_get(ym: str = None, user=Depends(current_user)):
    ym = ym or date.today().strftime("%Y-%m")
    r = kpi_report(ym)
    if user["role"] == "sale":
        r["rows"] = [x for x in r["rows"] if x["sale"] == user.get("sale_name")]
    return r


@router.post("/kpi")
async def kpi_set(request: Request, user=Depends(current_user)):
    if not _is_mgr(user):
        raise HTTPException(403)
    b = await request.json()
    rows = b.get("rows") or [b]
    with db.cursor() as con:
        for r in rows:
            if not r.get("sale") or not r.get("ym"):
                continue
            con.execute("""INSERT INTO kpi_targets(sale,ym,revenue_target,new_customers_target,buying_customers_target) VALUES(?,?,?,?,?)
                ON CONFLICT(sale,ym) DO UPDATE SET revenue_target=excluded.revenue_target,
                new_customers_target=excluded.new_customers_target,buying_customers_target=excluded.buying_customers_target""",
                        (r["sale"], r["ym"], float(r.get("revenue_target") or 0), int(r.get("new_customers_target") or 0),
                         int(r.get("buying_customers_target") or 0)))
    return {"ok": True}


@router.post("/kpi/copy")
async def kpi_copy(request: Request, user=Depends(current_user)):
    """Sao chép chỉ tiêu tháng trước sang tháng này."""
    if not _is_mgr(user):
        raise HTTPException(403)
    b = await request.json()
    src, dst = b["from"], b["to"]
    with db.cursor() as con:
        con.execute("""INSERT OR IGNORE INTO kpi_targets(sale,ym,revenue_target,new_customers_target,buying_customers_target)
                       SELECT sale,?,revenue_target,new_customers_target,buying_customers_target FROM kpi_targets WHERE ym=?""", (dst, src))
    return {"ok": True}


# ===========================================================================
# Công việc & nhắc việc
# ===========================================================================
@router.get("/tasks")
def tasks_list(status: str = "open", sale: str = None, days: int = 7, user=Depends(current_user)):
    s = _my_sale(user) or sale
    cond, p = "", ()
    if s:
        cond, p = " AND t.assigned_to=?", (s,)
    if status == "open":
        rows = db.rows(f"""SELECT t.*, c.name customer_name, c.contact_number, c.code customer_code, l.name lead_name, l.phone lead_phone
            FROM tasks t LEFT JOIN customers c ON c.id=t.customer_id LEFT JOIN leads l ON l.id=t.lead_id
            WHERE t.status='open' {cond} ORDER BY t.due_date, t.priority DESC, t.id""", p)
    else:
        rows = db.rows(f"""SELECT t.*, c.name customer_name, c.contact_number, c.code customer_code, l.name lead_name, l.phone lead_phone
            FROM tasks t LEFT JOIN customers c ON c.id=t.customer_id LEFT JOIN leads l ON l.id=t.lead_id
            WHERE t.status<>'open' {cond} ORDER BY t.done_at DESC LIMIT 300""", p)
    today = date.today().isoformat()
    for r in rows:
        r["overdue"] = r["status"] == "open" and r["due_date"] and r["due_date"] < today
        r["today"] = r["due_date"] == today
        r["type_label"] = TASK_TYPES.get(r["type"], r["type"])
    return rows


@router.post("/tasks")
async def tasks_save(request: Request, user=Depends(current_user)):
    b = await request.json()
    with db.cursor() as con:
        if b.get("id"):
            if b.get("status") in ("done", "cancelled"):
                con.execute("UPDATE tasks SET status=?, result=?, done_at=? WHERE id=?",
                            (b["status"], b.get("result"), _now(), b["id"]))
            elif b.get("status") == "open":
                con.execute("UPDATE tasks SET status='open', done_at=NULL WHERE id=?", (b["id"],))
            else:
                con.execute("UPDATE tasks SET title=?, type=?, due_date=?, assigned_to=?, priority=? WHERE id=?",
                            (b.get("title"), b.get("type", "call"), b.get("due_date"), b.get("assigned_to"), int(b.get("priority") or 0), b["id"]))
            return {"ok": True, "id": b["id"]}
        assigned = b.get("assigned_to") or _my_sale(user) or ""
        cur = con.execute("""INSERT OR IGNORE INTO tasks(customer_id,lead_id,title,type,due_date,assigned_to,status,created,created_by,auto_key,priority)
            VALUES(?,?,?,?,?,?,'open',?,?,?,?)""",
                          (b.get("customer_id"), b.get("lead_id"), b.get("title") or TASK_TYPES.get(b.get("type"), "Việc"), b.get("type", "call"),
                           b.get("due_date") or date.today().isoformat(), assigned, _now(), user["display_name"], b.get("auto_key"), int(b.get("priority") or 0)))
        return {"ok": True, "id": cur.lastrowid}


@router.post("/tasks/bulk")
async def tasks_bulk(request: Request, user=Depends(current_user)):
    """Tạo nhiều việc từ gợi ý (bỏ qua việc đã tồn tại nhờ auto_key)."""
    b = await request.json()
    n = 0
    with db.cursor() as con:
        for t in b.get("items", []):
            cur = con.execute("""INSERT OR IGNORE INTO tasks(customer_id,title,type,due_date,assigned_to,status,created,created_by,auto_key,priority)
                VALUES(?,?,?,?,?,'open',?,?,?,?)""",
                              (t.get("customer_id"), t.get("title"), t.get("type", "call"), t.get("due_date"), t.get("assigned_to") or "",
                               _now(), user["display_name"], t.get("auto_key"), int(t.get("priority") or 0)))
            n += cur.rowcount
    return {"ok": True, "created": n}


@router.get("/tasks/suggest")
def tasks_suggest(sale: str = None, user=Depends(current_user)):
    """Gợi ý việc tự động: sinh nhật, ngừng mua, đến kỳ mua lại."""
    s = _my_sale(user) or sale
    sf, sp = reports._sale_filter(s)
    today = date.today()
    out = []
    existing = {r["auto_key"] for r in db.rows("SELECT auto_key FROM tasks WHERE auto_key IS NOT NULL AND status='open'")}

    # 1. Sinh nhật 7 ngày tới
    for b in reports.birthdays(7, s):
        key = f"bd:{b['id']}:{today.year}"
        out.append({"kind": "birthday", "customer_id": b["id"], "customer_name": b["name"], "phone": b["contact_number"], "sale": b["sale"],
                    "reason": f"Sinh nhật {b['birthday_ddmm']} ({'hôm nay' if b['days_until']==0 else str(b['days_until'])+' ngày nữa'})",
                    "due_date": b["next_birthday"], "type": "birthday", "title": f"Chúc sinh nhật {b['name']}", "auto_key": key,
                    "exists": key in existing, "priority": 1, "revenue": b["lifetime_revenue"]})

    # 2. Chu kỳ mua: khách có >=3 HĐ, quá hạn mua lại (so với chu kỳ TB) từ 0-30 ngày
    rows = db.rows(f"""SELECT c.id, c.name, c.contact_number, {reports.sale_expr()} sale, COUNT(*) n, MIN(i.purchase_date) f, MAX(i.purchase_date) l,
            SUM(i.total) rev FROM invoices i JOIN customers c ON c.id=i.customer_id WHERE i.status=1 {sf}
            GROUP BY c.id HAVING n>=3""", sp)
    for r in rows:
        f, l = datetime.fromisoformat(r["f"]), datetime.fromisoformat(r["l"])
        cycle = (l - f).days / (r["n"] - 1)
        if cycle < 3:
            continue
        expected = l + timedelta(days=cycle)
        overdue = (datetime.now() - expected).days
        if 0 <= overdue <= 30:
            key = f"re:{r['id']}:{l.date().isoformat()}"
            out.append({"kind": "reorder", "customer_id": r["id"], "customer_name": r["name"], "phone": r["contact_number"], "sale": r["sale"],
                        "reason": f"Chu kỳ mua TB {round(cycle)} ngày, lần cuối {l.strftime('%d/%m')}, quá kỳ {overdue} ngày",
                        "due_date": today.isoformat(), "type": "reorder", "title": f"Nhắc mua lại: {r['name']}", "auto_key": key,
                        "exists": key in existing, "priority": 2 if r["rev"] > 50_000_000 else 1, "revenue": r["rev"]})

    # 3. Khách giá trị đang ngừng mua (90-180 ngày) – ưu tiên doanh số tích luỹ cao
    churn_from = (today - timedelta(days=180)).isoformat()
    churn_to = (today - timedelta(days=config.CHURN_DAYS)).isoformat()
    rows = db.rows(f"""SELECT c.id, c.name, c.contact_number, {reports.sale_expr()} sale, MAX(i.purchase_date) l, SUM(i.total) rev, COUNT(*) n
            FROM invoices i JOIN customers c ON c.id=i.customer_id WHERE i.status=1 {sf} GROUP BY c.id
            HAVING l BETWEEN ? AND ? AND n>=2 ORDER BY rev DESC LIMIT 60""", sp + (churn_from, churn_to))
    for r in rows:
        key = f"wb:{r['id']}:{r['l'][:10]}"
        out.append({"kind": "winback", "customer_id": r["id"], "customer_name": r["name"], "phone": r["contact_number"], "sale": r["sale"],
                    "reason": f"Không mua từ {r['l'][8:10]}/{r['l'][5:7]}, DS tích luỹ {round(r['rev']/1e6)} tr, {r['n']} HĐ",
                    "due_date": today.isoformat(), "type": "winback", "title": f"Kéo lại KH: {r['name']}", "auto_key": key,
                    "exists": key in existing, "priority": 1, "revenue": r["rev"]})
    return out


# ===========================================================================
# Khách tiềm năng
# ===========================================================================
@router.get("/leads")
def leads_list(status: str = None, sale: str = None, q: str = "", user=Depends(current_user)):
    s = _my_sale(user) or sale
    cond, p = [], []
    if s:
        cond.append("sale=?"); p.append(s)
    if status:
        cond.append("status=?"); p.append(status)
    if q:
        cond.append("(name LIKE ? OR phone LIKE ?)"); p += [f"%{q}%", f"%{q}%"]
    where = (" WHERE " + " AND ".join(cond)) if cond else ""
    rows = db.rows(f"SELECT * FROM leads{where} ORDER BY updated DESC LIMIT 1000", tuple(p))
    for r in rows:
        r["status_label"] = LEAD_LABEL.get(r["status"], r["status"])
    return rows


@router.get("/leads/stats")
def leads_stats(from_d: str = None, to_d: str = None, sale: str = None, user=Depends(current_user)):
    s = _my_sale(user) or sale
    f = from_d or (date.today() - timedelta(days=90)).isoformat()
    t = (to_d or date.today().isoformat()) + "T23:59"
    cond, p = "", (f, t)
    if s:
        cond, p = " AND sale=?", (f, t, s)
    by = lambda col: db.rows(f"""SELECT COALESCE({col},'(trống)') k, COUNT(*) n, SUM(status='won') won, SUM(status='lost') lost,
        COALESCE(SUM(CASE WHEN status='won' THEN value END),0) won_value FROM leads WHERE created BETWEEN ? AND ? {cond} GROUP BY 1 ORDER BY n DESC""", p)
    funnel = db.one(f"""SELECT COUNT(*) total, SUM(status='new') new, SUM(status='contacted') contacted, SUM(status='consulting') consulting,
        SUM(status='won') won, SUM(status='lost') lost FROM leads WHERE created BETWEEN ? AND ? {cond}""", p)
    reasons = db.rows(f"SELECT COALESCE(lost_reason,'(không ghi)') k, COUNT(*) n FROM leads WHERE status='lost' AND created BETWEEN ? AND ? {cond} GROUP BY 1 ORDER BY n DESC", p)
    return {"funnel": funnel, "by_sale": by("sale"), "by_source": by("source"), "lost_reasons": reasons}


@router.post("/leads")
async def leads_save(request: Request, user=Depends(current_user)):
    b = await request.json()
    sale = b.get("sale") or _my_sale(user) or ""
    with db.cursor() as con:
        if b.get("delete") and b.get("id"):
            if not _is_mgr(user):
                raise HTTPException(403)
            con.execute("DELETE FROM leads WHERE id=?", (b["id"],))
            return {"ok": True}
        if b.get("id"):
            con.execute("""UPDATE leads SET name=?,phone=?,source=?,sale=?,status=?,value=?,note=?,lost_reason=?,address=?,product_interest=?,
                next_action=?,next_date=?,updated=? WHERE id=?""",
                        (b.get("name"), b.get("phone"), b.get("source"), sale, b.get("status", "new"), float(b.get("value") or 0), b.get("note"),
                         b.get("lost_reason"), b.get("address"), b.get("product_interest"), b.get("next_action"), b.get("next_date"), _now(), b["id"]))
            return {"ok": True, "id": b["id"]}
        if not b.get("name"):
            raise HTTPException(400, "Thiếu tên")
        cur = con.execute("""INSERT INTO leads(name,phone,source,sale,status,value,note,address,product_interest,next_action,next_date,created,updated,created_by)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                          (b.get("name"), b.get("phone"), b.get("source"), sale, b.get("status", "new"), float(b.get("value") or 0), b.get("note"),
                           b.get("address"), b.get("product_interest"), b.get("next_action"), b.get("next_date"), _now(), _now(), user["display_name"]))
        return {"ok": True, "id": cur.lastrowid}


@router.post("/leads/status")
async def leads_status(request: Request, user=Depends(current_user)):
    b = await request.json()
    with db.cursor() as con:
        con.execute("UPDATE leads SET status=?, lost_reason=COALESCE(?,lost_reason), updated=? WHERE id=?",
                    (b["status"], b.get("lost_reason"), _now(), b["id"]))
    return {"ok": True}


@router.post("/leads/import")
async def leads_import(request: Request, user=Depends(current_user)):
    """Nhập từ bảng dán (tab/csv): tên, sđt, nguồn, sale, ghi chú."""
    b = await request.json()
    n = 0
    with db.cursor() as con:
        for line in (b.get("text") or "").splitlines():
            parts = [x.strip() for x in (line.split("\t") if "\t" in line else line.split(","))]
            if not parts or not parts[0]:
                continue
            parts += [""] * (5 - len(parts))
            con.execute("INSERT INTO leads(name,phone,source,sale,note,status,created,updated,created_by) VALUES(?,?,?,?,?,'new',?,?,?)",
                        (parts[0], parts[1], parts[2], parts[3] or _my_sale(user) or "", parts[4], _now(), _now(), user["display_name"]))
            n += 1
    return {"ok": True, "created": n}


@router.get("/leads/{lid}")
def lead_detail(lid: int, user=Depends(current_user)):
    l = db.one("SELECT * FROM leads WHERE id=?", (lid,))
    if not l:
        raise HTTPException(404)
    match = None
    if l.get("phone"):
        digits = "".join(ch for ch in l["phone"] if ch.isdigit())[-9:]
        if len(digits) >= 8:
            match = db.one("SELECT id, code, name FROM customers WHERE replace(replace(contact_number,' ',''),'.','') LIKE ? LIMIT 1", (f"%{digits}%",))
    return {"lead": l, "notes": db.rows("SELECT * FROM notes WHERE lead_id=? ORDER BY created DESC", (lid,)),
            "tasks": db.rows("SELECT * FROM tasks WHERE lead_id=? ORDER BY due_date", (lid,)), "kiot_match": match}


@router.post("/leads/{lid}/notes")
async def lead_note(lid: int, request: Request, user=Depends(current_user)):
    b = await request.json()
    with db.cursor() as con:
        con.execute("INSERT INTO notes(lead_id,created,author,content) VALUES(?,?,?,?)", (lid, _now(), user["display_name"], b.get("content", "")))
    return {"ok": True}


@router.post("/leads/{lid}/convert")
async def lead_convert(lid: int, request: Request, user=Depends(current_user)):
    b = await request.json()
    with db.cursor() as con:
        con.execute("UPDATE leads SET status='won', converted_customer_id=?, updated=? WHERE id=?", (b.get("customer_id"), _now(), lid))
    return {"ok": True}


# ===========================================================================
# Bảng doanh số theo mẫu Excel: team · khách sếp · nhóm hàng
# ===========================================================================
@router.get("/board")
def board(from_d: str = Query(None, alias="from"), to_d: str = Query(None, alias="to"), basis: str = None, weekly: int = 0, user=Depends(current_user)):
    from datetime import date as _d
    today = _d.today()
    f = from_d or today.replace(day=1).isoformat()
    t = to_d or today.isoformat()
    basis = basis or db.get_meta("report_basis") or "customer"
    b = reports.sales_board(f, t, basis, weekly=bool(weekly))
    if user["role"] == "sale":
        me = user.get("sale_name")
        b["rows"] = [r for r in b["rows"] if r["sale"] == me]
        b["teams"] = [g for g in b["teams"] if any(r["team"] == g["team"] for r in b["rows"])]
    return b


@router.get("/teams")
def teams_get(user=Depends(current_user)):
    """Danh sách người để xếp team, sắp theo doanh số 12 tháng (người bán nhiều lên đầu)."""
    rev = {}
    for r in db.rows("""SELECT sold_by_name s, SUM(total) v FROM invoices
                        WHERE status=1 AND purchase_date >= date('now','-365 days')
                          AND sold_by_name IS NOT NULL AND sold_by_name<>'' GROUP BY 1"""):
        rev[r["s"]] = r["v"] or 0
    for r in db.rows(f"""SELECT {reports.sale_expr()} s, SUM(i.total) v FROM invoices i
                         JOIN customers c ON c.id=i.customer_id
                         WHERE i.status=1 AND i.purchase_date >= date('now','-365 days') GROUP BY 1"""):
        rev[r["s"]] = max(rev.get(r["s"], 0), r["v"] or 0)
    rev.pop("(Chưa gán)", None)
    people = [{"name": n, "revenue": v} for n, v in rev.items()]
    people.sort(key=lambda x: -x["revenue"])
    return {"teams": db.rows("SELECT sale, team FROM teams ORDER BY team, sale"),
            "people": people, "basis": db.get_meta("report_basis") or "customer"}


@router.post("/teams")
async def teams_save(request: Request, user=Depends(current_user)):
    if not _is_mgr(user):
        raise HTTPException(403)
    b = await request.json()
    with db.cursor() as con:
        if b.get("replace") is not None:
            con.execute("DELETE FROM teams")
            for it in b["replace"]:
                if it.get("sale"):
                    con.execute("INSERT OR REPLACE INTO teams(sale,team) VALUES(?,?)", (it["sale"], (it.get("team") or "").strip()))
        elif b.get("sale"):
            if (b.get("team") or "").strip():
                con.execute("INSERT OR REPLACE INTO teams(sale,team) VALUES(?,?)", (b["sale"], b["team"].strip()))
            else:
                con.execute("DELETE FROM teams WHERE sale=?", (b["sale"],))
    if b.get("basis") in ("customer", "invoice", "accounting"):
        db.set_meta("report_basis", b["basis"])
    return {"ok": True}


@router.get("/catmap")
def catmap_get(user=Depends(current_user)):
    return {"marks": reports.trade_marks(), "cats": reports.CAT_KEYS, "alloc": reports.kpi_alloc()}


@router.post("/catmap")
async def catmap_save(request: Request, user=Depends(current_user)):
    if not _is_mgr(user):
        raise HTTPException(403)
    import json as _j
    b = await request.json()
    if b.get("map") is not None:
        db.set_meta("cat_map", _j.dumps(b["map"], ensure_ascii=False))
    if b.get("alloc") is not None:
        db.set_meta("kpi_alloc", _j.dumps(b["alloc"], ensure_ascii=False))
    return {"ok": True}


@router.get("/boss")
def boss_list(user=Depends(current_user)):
    rows = db.rows(f"""SELECT c.id, c.code, c.name, c.contact_number, COALESCE(c.boss_no_kpi,0) boss_no_kpi,
        COALESCE((SELECT SUM(i.total) FROM invoices i WHERE i.customer_id=c.id AND i.status=1
                  AND i.purchase_date >= date('now','-365 days')),0) rev
        FROM customers c WHERE COALESCE(c.is_boss,0)=1 ORDER BY rev DESC""")
    return rows


@router.post("/boss")
async def boss_save(request: Request, user=Depends(current_user)):
    """Đánh dấu / bỏ đánh dấu khách sếp. Nhận id lẻ hoặc danh sách mã KH dán từ Excel."""
    if not _is_mgr(user):
        raise HTTPException(403)
    b = await request.json()
    n = 0
    with db.cursor() as con:
        if b.get("id") is not None:
            if "no_kpi" in b:
                con.execute("UPDATE customers SET boss_no_kpi=? WHERE id=?", (1 if b.get("no_kpi") else 0, b["id"]))
            else:
                con.execute("UPDATE customers SET is_boss=? WHERE id=?", (1 if b.get("on") else 0, b["id"]))
            n = 1
        elif b.get("codes"):
            if b.get("reset"):
                con.execute("UPDATE customers SET is_boss=0, boss_no_kpi=0")
            for line in str(b["codes"]).replace(",", "\n").replace("\t", "\n").splitlines():
                v = line.strip()
                if not v:
                    continue
                cur = con.execute("UPDATE customers SET is_boss=1 WHERE code=? OR name=?", (v, v))
                n += cur.rowcount
    return {"ok": True, "updated": n}
