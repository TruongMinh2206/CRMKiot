"""KPI theo sale · Công việc & nhắc việc · Khách tiềm năng."""
from datetime import date, datetime, timedelta
from fastapi import APIRouter, Depends, HTTPException, Request, Query
from . import db, reports, config
from .main_auth import current_user, scope, allowed, col_in, is_mgr, can, team_names, task_names, task_cond, task_key

router = APIRouter(prefix="/api")

LEAD_STATUS = ["new", "contacted", "consulting", "won", "lost"]
LEAD_LABEL = {"new": "Mới", "contacted": "Đã liên hệ", "consulting": "Đang tư vấn", "won": "Đã chốt", "lost": "Thất bại"}
TASK_TYPES = {"call": "Gọi điện", "visit": "Ghé thăm", "message": "Nhắn tin", "birthday": "Chúc sinh nhật",
              "reorder": "Nhắc mua lại", "winback": "Kéo lại KH", "debt": "Nhắc công nợ", "other": "Khác"}


def _now():
    return datetime.now().isoformat(timespec="minutes")


def _is_mgr(user):
    return is_mgr(user)


def _my_sale(user):
    """Tên sale của chính người đăng nhập — dùng làm mặc định khi giao việc, tạo lead."""
    return user.get("sale_name") if team_names(user) is not None else None


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
    r["rows"] = [x for x in r["rows"] if allowed(user, x["sale"])]
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
    cond, p = task_cond(user, "t.assigned_to", sale)
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
    names = task_names(user)
    with db.cursor() as con:
        if b.get("id"):
            old = con.execute("SELECT assigned_to FROM tasks WHERE id=?", (b["id"],)).fetchone()
            if not old:
                raise HTTPException(404, "Không có việc này")
            if names is not None and (old["assigned_to"] or "") not in names:
                raise HTTPException(403, "Đây không phải việc của bạn")
            if b.get("status") in ("done", "cancelled"):
                con.execute("UPDATE tasks SET status=?, result=?, done_at=? WHERE id=?",
                            (b["status"], b.get("result"), _now(), b["id"]))
            elif b.get("status") == "open":
                con.execute("UPDATE tasks SET status='open', done_at=NULL WHERE id=?", (b["id"],))
            else:
                if names is not None and (b.get("assigned_to") or "") not in names:
                    raise HTTPException(403, "Không giao việc cho người ngoài phạm vi của bạn")
                con.execute("UPDATE tasks SET title=?, type=?, due_date=?, assigned_to=?, priority=? WHERE id=?",
                            (b.get("title"), b.get("type", "call"), b.get("due_date"), b.get("assigned_to"), int(b.get("priority") or 0), b["id"]))
            return {"ok": True, "id": b["id"]}
        assigned = b.get("assigned_to") or task_key(user)
        if names is not None and assigned not in names:
            raise HTTPException(403, "Không giao việc cho người ngoài phạm vi của bạn")
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
    names = task_names(user)
    with db.cursor() as con:
        for t in b.get("items", []):
            who = t.get("assigned_to") or task_key(user)
            if names is not None and who not in names:
                who = task_key(user)
            cur = con.execute("""INSERT OR IGNORE INTO tasks(customer_id,title,type,due_date,assigned_to,status,created,created_by,auto_key,priority)
                VALUES(?,?,?,?,?,'open',?,?,?,?)""",
                              (t.get("customer_id"), t.get("title"), t.get("type", "call"), t.get("due_date"), who,
                               _now(), user["display_name"], t.get("auto_key"), int(t.get("priority") or 0)))
            n += cur.rowcount
    return {"ok": True, "created": n}


@router.get("/tasks/people")
def tasks_people(user=Depends(current_user)):
    """Những người có thể xem / giao việc: {me, people:[{value,label}]}. Giá trị là tên ghi ở cột assigned_to."""
    names = task_names(user)
    label = {}
    for u in db.rows("SELECT display_name, username, sale_name FROM crm_users WHERE active IS NOT 0"):
        label[u["sale_name"] or u["display_name"] or u["username"]] = u["display_name"] or u["username"]
    if names is None:
        for r in db.rows("SELECT DISTINCT assigned_to a FROM tasks WHERE assigned_to<>''"):
            label.setdefault(r["a"], r["a"])
        for s in reports.sales_list():
            label.setdefault(s, s)
        label.pop("(Chưa gán)", None)
        vals = sorted(label, key=lambda v: label[v].lower())
    else:
        vals = [v for v in names if v == task_key(user) or v != user["display_name"]]
    me = task_key(user)
    vals = [me] + [v for v in vals if v != me]
    return {"me": me, "all": names is None, "default": "" if names is None or user.get("scope") == "team" else me,
            "people": [{"value": v, "label": label.get(v) or v} for v in vals]}


@router.get("/tasks/suggest")
def tasks_suggest(sale: str = None, user=Depends(current_user)):
    """Gợi ý việc tự động: sinh nhật, ngừng mua, đến kỳ mua lại — chỉ cho những người mà tài khoản này được xem việc."""
    names = task_names(user)
    s = (sale or None) if names is None else (names[0] if len(names) == 1 else names)
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

    # 4. Công nợ quá hạn: mỗi khách nhắc một lần mỗi tháng
    debts = [c for c in reports.debt_aging(s)["rows"] if c["overdue"] > 0]
    debts.sort(key=lambda c: -c["overdue"])
    for c in debts[:60]:
        key = f"debt:{c['id']}:{today.strftime('%Y-%m')}"
        out.append({"kind": "debt", "customer_id": c["id"], "customer_name": c["name"], "phone": c["contact_number"], "sale": c["sale"],
                    "reason": f"Nợ quá hạn {round(c['overdue']/1e6, 1)} tr / tổng nợ {round(c['debt']/1e6, 1)} tr"
                              + (f", khoản cũ nhất {c['oldest_days']} ngày" if c["oldest_days"] is not None else ""),
                    "due_date": today.isoformat(), "type": "debt", "title": f"Nhắc công nợ: {c['name']}", "auto_key": key,
                    "exists": key in existing, "priority": 2 if c["risk"] else 1, "revenue": c["overdue"]})
    return out


# ===========================================================================
# Khách tiềm năng
# ===========================================================================
@router.get("/leads")
def leads_list(status: str = None, sale: str = None, q: str = "", user=Depends(current_user)):
    cond, p = [], []
    c0, p0 = col_in("sale", scope(user, sale))
    if c0:
        cond.append(c0[5:]); p += list(p0)
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
    f = from_d or (date.today() - timedelta(days=90)).isoformat()
    t = (to_d or date.today().isoformat()) + "T23:59"
    cond, p0 = col_in("sale", scope(user, sale))
    p = (f, t) + p0
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
        val = float(b["value"]) if b.get("value") else None      # giá trị thực khi chốt
        con.execute("UPDATE leads SET status=?, lost_reason=COALESCE(?,lost_reason), value=COALESCE(?,value), updated=? WHERE id=?",
                    (b["status"], b.get("lost_reason"), val, _now(), b["id"]))
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
    b["cancelled_list"] = reports.cancelled_invoices(f, t)
    b["boss_daily"] = reports.boss_daily(f, t)
    if team_names(user) is not None:        # phạm vi team / của mình: chỉ giữ dòng được xem
        b["rows"] = [r for r in b["rows"] if allowed(user, r["sale"])]
        b["teams"] = [g for g in b["teams"] if any(r["team"] == g["team"] for r in b["rows"])]
        b["cancelled_list"] = [r for r in b["cancelled_list"] if allowed(user, r["sale"])]
        bd = b["boss_daily"]
        bd["rows"] = [r for r in bd["rows"] if allowed(user, r["sale"])]
        cnt = [r for r in bd["rows"] if not r["no_kpi"]]
        bd["day_total"] = [sum(r["daily"][i] for r in cnt) for i in range(len(bd["days"]))]
        bd["total"] = sum(r["total"] for r in cnt)
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


# ===========================================================================
# Công nợ & tuổi nợ
# ===========================================================================
@router.get("/debts")
def debts_get(sale: str = None, ym: str = None, user=Depends(current_user)):
    d = reports.debt_aging(scope(user, sale), ym)
    for r in d["rows"]:
        r.pop("open_invoices", None)             # chi tiết từng hoá đơn chỉ tải khi mở một khách
    return d


@router.get("/debts/customer/{cid}")
def debts_customer(cid: int, ym: str = None, user=Depends(current_user)):
    d = reports.debt_detail(cid, ym)
    if not d or not allowed(user, d["customer"]["sale"]):
        raise HTTPException(404, "Không tìm thấy khách")
    return d


@router.post("/debts/term")
async def debts_term(request: Request, user=Depends(current_user)):
    """Hạn nợ chung (30/60/90 ngày) hoặc hạn riêng của một khách (customer_id + days; days trống = dùng lại hạn chung)."""
    if not is_mgr(user):
        raise HTTPException(403)
    b = await request.json()
    try:
        v = int(b.get("days"))
    except (TypeError, ValueError):
        v = None
    cid = b.get("customer_id")
    if cid:
        with db.cursor() as con:
            if v is None:
                con.execute("DELETE FROM debt_terms WHERE customer_id=?", (cid,))
            elif not 0 <= v <= 365:
                raise HTTPException(400, "Hạn nợ riêng từ 0 đến 365 ngày")
            else:
                con.execute("""INSERT INTO debt_terms(customer_id,days,note,updated,updated_by) VALUES(?,?,?,?,?)
                    ON CONFLICT(customer_id) DO UPDATE SET days=excluded.days, note=excluded.note, updated=excluded.updated,
                    updated_by=excluded.updated_by""", (cid, v, (b.get("note") or "").strip(), _now(), user["display_name"]))
        return {"ok": True}
    if v not in (30, 60, 90):
        raise HTTPException(400, "Hạn nợ chung chỉ nhận 30, 60 hoặc 90 ngày")
    db.set_meta("debt_term_days", str(v))
    return {"ok": True, "term": v}


@router.get("/debts/sheet")
def debts_sheet_status(user=Depends(current_user)):
    from . import debt_sheet
    return debt_sheet.status()


@router.post("/debts/sheet")
async def debts_sheet(request: Request, user=Depends(current_user)):
    """Xuất công nợ của tháng đang xem ra file Google Sheet riêng (ghi đè tab của tháng đó)."""
    from . import debt_sheet, gsheet
    if not can(user, "export"):
        raise HTTPException(403, "Tài khoản chưa được cấp quyền xuất dữ liệu")
    b = await request.json()
    if b.get("action") == "config":
        if not is_mgr(user):
            raise HTTPException(403)
        db.set_meta("debt_sheet_id", (b.get("sheet") or "").strip())
        return debt_sheet.status()
    try:
        return debt_sheet.run(b.get("ym"), scope(user, b.get("sale")))
    except gsheet.ConfigError as e:
        raise HTTPException(400, str(e))


# ===========================================================================
# Cảnh báo đầu ngày ở trang Tổng quan
# ===========================================================================
@router.get("/alerts")
def alerts(user=Depends(current_user)):
    s = scope(user)
    today = date.today()
    out = []

    if today.day >= reports.BIRTHDAY_PLAN_DAY and "birthdays" in (user.get("pages") or []):
        b = reports.birthday_month(None, s)
        if b["planning"] and b["totals"]["customers"] and not b["totals"]["gifts"]:
            out.append({"key": "bdgift", "level": "warn", "n": b["totals"]["customers"], "href": "#birthdays",
                        "title": f"Chọn khách tặng quà sinh nhật tháng {b['ym'][5:]}/{b['ym'][:4]}",
                        "note": "khách có sinh nhật tháng sau · chưa tick khách nào được tặng quà"})

    d = reports.debt_aging(s)
    if d["overdue_customers"]:
        out.append({"key": "debt", "level": "bad", "n": d["overdue_customers"], "amount": d["overdue"], "href": "#debts",
                    "title": "Khách nợ quá hạn", "note": f"quá {d['term']} ngày"
                    + (f" · {d['risk_customers']} khách đã ngừng mua trên 60 ngày" if d["risk_customers"] else "")})

    if "products" in (user.get("pages") or []):
        w = reports.stock_watch()
        near = [b for b in w["batches"] if b["band"] in ("expired", "m3", "m6")]
        if near:
            exp = sum(1 for b in near if b["band"] == "expired")
            out.append({"key": "expiry", "level": "bad" if exp else "warn", "n": len(near), "amount": sum(b["value"] for b in near),
                        "href": "#products", "title": "Lô hàng cận date",
                        "note": "còn hạn dưới 6 tháng" + (f" · {exp} lô đã hết hạn" if exp else "") + " · số tiền là giá trị tồn"})

    c, p = task_cond(user, "assigned_to")
    n = db.one(f"SELECT COUNT(*) n FROM tasks WHERE status='open' AND due_date<? {c}", (today.isoformat(),) + p)["n"]
    if n:
        out.append({"key": "tasks", "level": "bad", "n": n, "href": "#tasks", "title": "Việc quá hạn", "note": "chưa đánh dấu xong"})

    sug = [x for x in tasks_suggest(sale=None, user=user) if not x["exists"]]
    wb = [x for x in sug if x["kind"] == "winback"]
    if wb:
        out.append({"key": "winback", "level": "warn", "n": len(wb), "amount": sum(x["revenue"] or 0 for x in wb), "href": "#tasks",
                    "title": "Khách giá trị đang ngừng mua", "note": "chưa có việc chăm sóc · số tiền là doanh số tích luỹ"})
    ro = [x for x in sug if x["kind"] == "reorder"]
    if ro:
        out.append({"key": "reorder", "level": "info", "n": len(ro), "href": "#tasks",
                    "title": "Khách đến kỳ mua lại", "note": "quá chu kỳ mua trung bình, chưa có việc nhắc"})

    c, p = col_in("sale", s)
    stale = db.one(f"""SELECT COUNT(*) n, COALESCE(SUM(value),0) v FROM leads
        WHERE status NOT IN ('won','lost') AND updated < ? {c}""", ((today - timedelta(days=7)).isoformat(),) + p)
    if stale["n"]:
        out.append({"key": "leads", "level": "warn", "n": stale["n"], "amount": stale["v"] or None, "href": "#leads",
                    "title": "Khách tiềm năng bị bỏ quên", "note": "hơn 7 ngày chưa cập nhật"})

    f30 = (today - timedelta(days=29)).isoformat()
    pv = reports.product_velocity(f30, today.isoformat())
    oos = [r for r in pv if r["speed"] == "Hết hàng"]
    low = [r for r in pv if r["speed"] == "Nhanh (sắp hết)"]
    if oos:
        out.append({"key": "oos", "level": "bad", "n": len(oos), "amount": sum(r["revenue"] for r in oos), "href": "#products",
                    "title": "Mặt hàng đang bán đã hết tồn", "note": "số tiền là doanh số 30 ngày qua của các mã này"})
    if low:
        out.append({"key": "low", "level": "warn", "n": len(low), "href": "#products",
                    "title": "Mặt hàng sắp hết", "note": "tồn còn dưới 14 ngày bán"})
    from . import events
    out.extend(events.alerts())
    return out
