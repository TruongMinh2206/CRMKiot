"""Giao việc nội bộ & KPI theo giờ — làm theo bảng "HCNS - TỔNG HỢP LINK + BÁO CÁO" (tab GIAO VIỆC, TỰ TẠO VIỆC,
VIỆC ĐỊNH KỲ, DASHBOARD, CÁCH TÍNH KPI, CÀI ĐẶT). Chạy song song với bảng, không ghi gì sang Google Sheet.

Mỗi việc: độ khó 1–5 × thời lượng chuẩn (giờ) = điểm tiêu chuẩn.
  MỚI GIAO → (nhận) ĐANG LÀM → (hoàn thành) CHỜ DUYỆT → ĐÃ DUYỆT / CẦN SỬA / KHÔNG DUYỆT; TẠM DỪNG, ĐÃ DỪNG.
KPI năng suất = giờ (thời lượng chuẩn) của việc đã báo hoàn thành trong kỳ ÷ giờ khoán (8 giờ T2–T6, 4 giờ T7).
"""
import json
from datetime import date, datetime, timedelta, timezone
from fastapi import APIRouter, Depends, HTTPException, Request
from . import db, config
from .main_auth import current_user, can, resolve

router = APIRouter(prefix="/api/work")

VN = timezone(timedelta(hours=7))
STATUS = {"new": "Mới giao", "doing": "Đang làm", "revise": "Cần sửa", "review": "Chờ duyệt", "approved": "Đã duyệt",
          "rejected": "Không duyệt", "paused": "Tạm dừng", "stopped": "Đã dừng"}
OPEN = ("new", "doing", "revise", "paused")
DONE = ("review", "approved")                 # đã báo hoàn thành → tính giờ KPI năng suất
DEFAULT_CFG = {"day_hours": 8, "sat_hours": 4, "manager_pct": 20, "bonus_pct": 10, "revise_pct": 10,
               "late": [1, 0.8, 0.6], "due_time": "23:59", "keep_days": 30}
DEFAULT_DEPTS = [("HCNS", "Hành chính – Nhân sự", "HCNS"), ("MKT", "Marketing", "MKT")]
CYCLES = {"daily": "Hằng ngày", "weekly": "Hằng tuần", "monthly": "Hằng tháng"}


def now():
    """Giờ Việt Nam (máy chủ Render chạy giờ UTC)."""
    return datetime.now(VN).replace(tzinfo=None, second=0, microsecond=0)


def _iso(d):
    return d.strftime("%Y-%m-%dT%H:%M") if d else None


def _dt(s):
    if not s:
        return None
    s = str(s).replace(" ", "T")[:16]
    return datetime.fromisoformat(s if "T" in s else s + "T00:00")


# ---------------------------------------------------------------------------
# Phòng ban & người
# ---------------------------------------------------------------------------
def seed():
    with db.cursor() as con:
        for code, name, prefix in DEFAULT_DEPTS:
            con.execute("INSERT OR IGNORE INTO work_depts(code,name,prefix,active,cfg) VALUES(?,?,?,1,?)",
                        (code, name, prefix, json.dumps(DEFAULT_CFG)))


def depts(active_only=True):
    out = db.rows("SELECT * FROM work_depts" + (" WHERE active=1" if active_only else "") + " ORDER BY code")
    for d in out:
        cfg = dict(DEFAULT_CFG)
        cfg.update(json.loads(d["cfg"] or "{}"))
        d["cfg"] = cfg
    return out


def _cfg(dept):
    return next((d["cfg"] for d in depts(False) if d["code"] == dept), dict(DEFAULT_CFG))


def people():
    """Tài khoản đang mở kèm quyền giao việc / quản lý (đọc lại quyền mỗi lần)."""
    out = []
    for u in db.rows("SELECT username FROM crm_users WHERE COALESCE(active,1)=1 ORDER BY display_name"):
        r = resolve(u["username"])
        if r:
            out.append({"username": r["username"], "name": r["display_name"], "dept": r.get("dept") or "",
                        "manager": can(r, "work_manage"), "assign": can(r, "work_assign"), "role": r["role_name"]})
    return out


def _names():
    m = {p["username"]: p["name"] for p in people()}
    m.setdefault(config.ADMIN_USER, "Quản trị")
    return m


def _see_all(user):
    return can(user, "task_all")          # Quản trị / BGĐ: xem việc mọi phòng


def _mgr_of(user, dept):
    return _see_all(user) or (can(user, "work_manage") and dept and user.get("dept") == dept)


def _vis(user, alias="w"):
    if _see_all(user):
        return "", ()
    me = user["username"]
    if can(user, "work_manage") and user.get("dept"):
        return f" AND ({alias}.dept=? OR {alias}.assignee=? OR {alias}.assigner=?)", (user["dept"], me, me)
    return f" AND ({alias}.assignee=? OR {alias}.assigner=?)", (me, me)


def _rights(user, it):
    me = user["username"]
    mgr = _mgr_of(user, it["dept"])
    approver = mgr or (it["assigner"] == me and it["assignee"] != me and can(user, "work_assign"))
    return {"work": it["assignee"] == me or mgr,                       # nhận / báo hoàn thành / tạm dừng
            "approve": approver and (it["assignee"] != me or _see_all(user)),
            "edit": mgr or (it["assigner"] == me and it["source"] != "self") or (it["source"] == "self" and it["created_by"] == me and it["status"] in OPEN)}


# ---------------------------------------------------------------------------
# Tính toán
# ---------------------------------------------------------------------------
def workdays(d1, d2):
    """Số ngày làm việc trong [d1, d2]: T2–T6 = 1, T7 = 0,5, CN = 0."""
    n, d = 0.0, d1
    while d <= d2:
        n += 1 if d.weekday() < 5 else 0.5 if d.weekday() == 5 else 0
        d += timedelta(days=1)
    return n


def _span_days(d1, d2):
    """Số ngày để chia đều workload (T2–T7 mỗi ngày 1, CN không tính), tối thiểu 1."""
    n, d = 0, d1
    while d <= d2:
        n += 1 if d.weekday() < 6 else 0
        d += timedelta(days=1)
    return max(n, 1)


def late_factor(it, cfg):
    due, done = _dt(it["due_at"]), _dt(it["done_at"])
    if not due or not done or done <= due:
        return cfg["late"][0]
    return cfg["late"][1] if (done - due).total_seconds() <= 24 * 3600 else cfg["late"][2]


def enrich(it, user=None, names=None, cfg=None, t=None):
    cfg = cfg or _cfg(it["dept"])
    t = t or now()
    due, done = _dt(it["due_at"]), _dt(it["done_at"])
    std = round((it["difficulty"] or 0) * (it["hours"] or 0), 2)
    it["points_std"] = std
    it["status_vn"] = STATUS.get(it["status"], it["status"])
    if it["status"] in DONE or (it["status"] in ("rejected", "stopped") and done):
        it["due_state"] = "Đúng hạn" if not due or (done and done <= due) else "Trễ hạn"
    elif it["status"] == "paused":
        it["due_state"] = "Tạm dừng"
    elif it["status"] in OPEN and due:
        it["due_state"] = ("Quá hạn" if t > due else "Đến hạn hôm nay" if due.date() == t.date()
                           else "Sắp đến hạn" if due.date() == t.date() + timedelta(days=1) else "Chưa đến hạn")
    else:
        it["due_state"] = ""
    it["late_hours"] = round(((done or t) - due).total_seconds() / 3600, 1) if due and (done or t) > due and it["status"] not in ("stopped",) else 0
    it["points_real"] = None
    if it["status"] == "approved":
        f = late_factor(it, cfg) * max(0, 1 - cfg["revise_pct"] / 100 * (it["revisions"] or 0)) * (1 + cfg["bonus_pct"] / 100 if it["good"] else 1)
        it["points_real"] = round(std * f, 2)
    a = _dt(it["assigned_at"])
    it["workload_day"] = round((it["hours"] or 0) / _span_days(a.date(), due.date()), 2) if a and due and due.date() >= a.date() else it["hours"]
    if names is not None:
        it["assignee_name"] = names.get(it["assignee"], it["assignee"])
        it["assigner_name"] = names.get(it["assigner"], it["assigner"] or "")
    if user is not None:
        it["can"] = _rights(user, it)
    return it


def _log(con, item_id, who, action, old=None, new=None, note=None):
    con.execute("INSERT INTO work_log(item_id,at,who,action,old,new,note) VALUES(?,?,?,?,?,?,?)",
                (item_id, _iso(now()), who, action, old, new, note))


def _code(con, dept, t):
    prefix = (next((d["prefix"] for d in depts(False) if d["code"] == dept), dept) or dept)
    head = f"{prefix}-{t.strftime('%y%m')}-"
    r = con.execute("SELECT code FROM work_items WHERE code LIKE ? ORDER BY id DESC LIMIT 1", (head + "%",)).fetchone()
    n = 1
    if r:
        try:
            n = int(r["code"].rsplit("-", 1)[1]) + 1
        except ValueError:
            n = con.execute("SELECT COUNT(*) FROM work_items WHERE code LIKE ?", (head + "%",)).fetchone()[0] + 1
    return f"{head}{n:03d}"


def _due(s, cfg):
    s = (s or "").strip()
    if not s:
        raise HTTPException(400, "Chọn hạn hoàn thành")
    try:
        return _iso(_dt(s if "T" in s or " " in s else f"{s}T{cfg['due_time']}"))
    except ValueError:
        raise HTTPException(400, "Hạn hoàn thành chưa đúng (ngày/tháng/năm giờ:phút)")


def _num(v, lo, hi, msg):
    try:
        x = float(str(v).replace(",", "."))
    except (TypeError, ValueError):
        raise HTTPException(400, msg)
    if not lo <= x <= hi:
        raise HTTPException(400, msg)
    return x


# ---------------------------------------------------------------------------
# Việc định kỳ: tạo việc của hôm nay (và bù tối đa 7 ngày nếu chưa ai mở CRM)
# ---------------------------------------------------------------------------
def gen_recurring(t=None):
    t = t or now()
    today = t.date()
    active = {d["code"] for d in depts()}
    made = 0
    with db.cursor() as con:
        for r in con.execute("SELECT * FROM work_recurring WHERE active=1").fetchall():
            r = dict(r)
            if r["dept"] not in active:
                continue
            start = today - timedelta(days=6)
            if r["last_on"]:
                start = max(start, date.fromisoformat(r["last_on"]) + timedelta(days=1))
            d = start
            while d <= today:
                hit = ((r["cycle"] == "daily" and d.weekday() < 6) or (r["cycle"] == "weekly" and d.weekday() == r["weekday"])
                       or (r["cycle"] == "monthly" and d.day == r["monthday"]))
                if hit:
                    key = f"dk:{r['id']}:{d.isoformat()}"
                    if not con.execute("SELECT 1 FROM work_items WHERE auto_key=?", (key,)).fetchone():
                        at = t if d == today else datetime(d.year, d.month, d.day, 7, 0)
                        due = f"{d.isoformat()}T{r['due_time'] or '23:59'}"
                        cur = con.execute("""INSERT INTO work_items(code,dept,title,note,assigner,assignee,difficulty,hours,assigned_at,due_at,status,
                                             source,recur_id,auto_key,created_by,updated) VALUES(?,?,?,?,?,?,?,?,?,?,'new','recurring',?,?,?,?)""",
                                          (_code(con, r["dept"], t), r["dept"], r["title"], f"Việc định kỳ DK-{r['id']:02d}", r["assigner"], r["assignee"],
                                           r["difficulty"], r["hours"], _iso(at), due, r["id"], key, "hệ thống", _iso(t)))
                        _log(con, cur.lastrowid, "hệ thống", "Giao việc định kỳ", None, "new")
                        made += 1
                d += timedelta(days=1)
            con.execute("UPDATE work_recurring SET last_on=? WHERE id=?", (today.isoformat(), r["id"]))
    return made


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------
@router.get("/meta")
def meta(user=Depends(current_user)):
    P = people()
    return {"me": {"username": user["username"], "name": user["display_name"], "dept": user.get("dept") or "",
                   "assign": can(user, "work_assign"), "manage": can(user, "work_manage"), "all": _see_all(user),
                   "admin": user.get("role") == "admin"},
            "depts": depts(False), "people": P, "status": STATUS, "cycles": CYCLES}


@router.get("")
def items(view: str = "mine", who: str = None, dept: str = None, state: str = "active", user=Depends(current_user)):
    """view=mine: việc giao cho tôi · view=team: mọi việc tôi được xem (tôi giao, phòng tôi quản lý).
    state=active: đang mở + chờ duyệt + đã kết thúc trong N ngày gần đây · review: chỉ việc chờ tôi duyệt · all."""
    gen_recurring()
    me, t = user["username"], now()
    if view == "mine":
        cond, p = " AND w.assignee=?", (me,)
    else:
        cond, p = _vis(user)
        if who:
            cond, p = cond + " AND w.assignee=?", p + (who,)
        if dept:
            cond, p = cond + " AND w.dept=?", p + (dept,)
    if state == "active":
        keep = (t - timedelta(days=30)).strftime("%Y-%m-%d")
        cond += f" AND (w.status IN ({','.join('?' * len(OPEN + DONE[:1]))}) OR w.updated>=?)"
        p += OPEN + DONE[:1] + (keep,)
    elif state == "review":
        cond += " AND w.status='review'"
    rows = db.rows(f"SELECT * FROM work_items w WHERE 1=1 {cond} ORDER BY CASE w.status WHEN 'review' THEN 0 WHEN 'revise' THEN 1 WHEN 'new' THEN 2 WHEN 'doing' THEN 3 WHEN 'paused' THEN 4 ELSE 5 END, w.due_at", p)
    names, cfgs = _names(), {d["code"]: d["cfg"] for d in depts(False)}
    out = [enrich(r, user, names, cfgs.get(r["dept"]), t) for r in rows]
    if state == "review":
        out = [r for r in out if r["can"]["approve"]]
    res = {"items": out}
    if view == "mine":
        res["summary"] = my_summary(user, t)
    return res


def my_summary(user, t=None):
    """Các ô đếm đầu sheet cá nhân."""
    t = t or now()
    me, today = user["username"], t.date()
    rows = db.rows("SELECT * FROM work_items WHERE assignee=?", (me,))
    cfg = _cfg(user.get("dept"))
    for r in rows:
        enrich(r, None, None, cfg, t)
    open_ = [r for r in rows if r["status"] in OPEN]
    doing = [r for r in open_ if r["status"] in ("doing", "revise")]
    done_today = [r for r in rows if r["status"] in DONE and _dt(r["done_at"]).date() == today]
    wl_today = 0.0
    for r in doing:
        a, d = _dt(r["assigned_at"]), _dt(r["due_at"])
        if a and d and a.date() <= today <= d.date() and today.weekday() < 6:
            wl_today += r["workload_day"]
        elif d and d.date() < today:
            wl_today += r["workload_day"]          # quá hạn: vẫn còn phải làm hôm nay
    khoan = cfg["day_hours"] if today.weekday() < 5 else cfg["sat_hours"] if today.weekday() == 5 else 0
    h_today = sum(r["hours"] for r in done_today)
    return {"due_today": sum(1 for r in open_ if r["due_state"] in ("Đến hạn hôm nay", "Sắp đến hạn")),
            "new": sum(1 for r in open_ if r["status"] == "new"), "doing": len(doing),
            "review": sum(1 for r in rows if r["status"] == "review"),
            "overdue": sum(1 for r in open_ if r["due_state"] == "Quá hạn"),
            "workload_today": round(wl_today, 2), "workload_new": round(sum(r["hours"] for r in open_ if r["status"] == "new"), 2),
            "hours_today": round(h_today, 2), "khoan_today": khoan,
            "level_today": round(sum(r["hours"] * r["difficulty"] for r in done_today) / h_today, 2) if h_today else None}


@router.get("/{item_id}/log")
def item_log(item_id: int, user=Depends(current_user)):
    it = db.one("SELECT * FROM work_items WHERE id=?", (item_id,))
    if not it:
        raise HTTPException(404, "Không có việc này")
    cond, p = _vis(user)
    if not db.one(f"SELECT 1 FROM work_items w WHERE w.id=? {cond}", (item_id,) + p):
        raise HTTPException(403, "Bạn không xem được việc này")
    names = _names()
    return [dict(r, who_name=names.get(r["who"], r["who"])) for r in db.rows("SELECT * FROM work_log WHERE item_id=? ORDER BY id", (item_id,))]


@router.post("")
async def save(request: Request, user=Depends(current_user)):
    """Giao việc / tự tạo việc / sửa / xoá."""
    b = await request.json()
    me, t = user["username"], now()
    P = {p["username"]: p for p in people()}
    active = {d["code"] for d in depts()}
    with db.cursor() as con:
        if b.get("id"):
            it = con.execute("SELECT * FROM work_items WHERE id=?", (b["id"],)).fetchone()
            if not it:
                raise HTTPException(404, "Không có việc này")
            it = dict(it)
            if not _rights(user, it)["edit"]:
                raise HTTPException(403, "Bạn không sửa được việc này")
            if b.get("delete"):
                con.execute("DELETE FROM work_items WHERE id=?", (it["id"],))
                _log(con, it["id"], me, "Xoá việc", it["status"], None, it["title"])
                return {"ok": True}
            cfg = _cfg(it["dept"])
            to = b.get("assignee") or it["assignee"]
            if to != it["assignee"]:
                _check_assign(user, to, P, active)
            new = {"title": (b.get("title") or it["title"]).strip(), "note": b.get("note", it["note"]),
                   "difficulty": int(_num(b.get("difficulty", it["difficulty"]), 1, 5, "Độ khó từ 1 đến 5")),
                   "hours": _num(b.get("hours", it["hours"]), 0.05, 500, "Thời lượng phải lớn hơn 0"),
                   "due_at": _due(b["due_at"], cfg) if b.get("due_at") else it["due_at"], "assignee": to,
                   "dept": P[to]["dept"] if to in P else it["dept"]}
            changes = [f"{k}: {it[k]} → {v}" for k, v in new.items() if str(it[k]) != str(v)]
            con.execute("UPDATE work_items SET title=?,note=?,difficulty=?,hours=?,due_at=?,assignee=?,dept=?,updated=? WHERE id=?",
                        (new["title"], new["note"], new["difficulty"], new["hours"], new["due_at"], new["assignee"], new["dept"], _iso(t), it["id"]))
            if changes:
                _log(con, it["id"], me, "Sửa việc", None, None, "; ".join(changes))
            return {"ok": True, "id": it["id"]}
        # tạo mới
        title = (b.get("title") or "").strip()
        if not title:
            raise HTTPException(400, "Nhập tên công việc")
        to = b.get("assignee") or me
        self_made = to == me
        if self_made:
            if me not in P:
                raise HTTPException(400, "Tài khoản quản trị gốc không nhận việc — dùng tài khoản cá nhân")
            dept = P[me]["dept"]
            if dept not in active:
                raise HTTPException(400, "Phòng của bạn chưa bật giao việc trên CRM")
            giver = b.get("assigner") or me
            if giver != me and giver not in P:
                raise HTTPException(400, "Người giao không có trong danh sách tài khoản")
        else:
            _check_assign(user, to, P, active)
            dept, giver = P[to]["dept"], me
        cfg = _cfg(dept)
        diff = int(_num(b.get("difficulty", 1), 1, 5, "Độ khó từ 1 đến 5"))
        hours = _num(b.get("hours", 1), 0.05, 500, "Thời lượng phải lớn hơn 0")
        due = _due(b.get("due_at"), cfg)
        status = "doing" if self_made else "new"
        cur = con.execute("""INSERT INTO work_items(code,dept,title,note,assigner,assignee,difficulty,hours,assigned_at,due_at,status,accepted_at,
                             source,created_by,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                          (_code(con, dept, t), dept, title, b.get("note") or "", giver, to, diff, hours, _iso(t), due, status,
                           _iso(t) if self_made else None, "self" if self_made else "assign", me, _iso(t)))
        _log(con, cur.lastrowid, me, "Tự tạo việc" if self_made else "Giao việc", None, status, f"Độ khó {diff}, {hours:g} giờ, hạn {due.replace('T', ' ')}")
        return {"ok": True, "id": cur.lastrowid}


def _check_assign(user, to, P, active):
    if to not in P:
        raise HTTPException(400, "Người nhận không có trong danh sách tài khoản")
    if P[to]["dept"] not in active:
        raise HTTPException(400, "Phòng của người nhận chưa bật giao việc trên CRM")
    if to == user["username"]:
        return
    if not can(user, "work_assign"):
        raise HTTPException(403, "Bạn chỉ tự tạo việc cho mình — nhờ quản lý giao việc cho người khác")
    if not _see_all(user) and P[to]["dept"] != user.get("dept"):
        raise HTTPException(403, "Chỉ giao việc cho người trong phòng của bạn")


ACTIONS = {"accept": ("work", ("new",), "doing", "Nhận việc"),
           "done": ("work", ("doing", "revise", "new"), "review", "Hoàn thành"),
           "pause": ("work", ("new", "doing", "revise"), "paused", "Tạm dừng"),
           "resume": ("work", ("paused",), "doing", "Tiếp tục"),
           "approve": ("approve", ("review",), "approved", "Duyệt"),
           "revise": ("approve", ("review", "approved"), "revise", "Cần sửa"),
           "reject": ("approve", ("review",), "rejected", "Không duyệt"),
           "stop": ("edit", OPEN + ("review",), "stopped", "Dừng việc")}


@router.post("/{item_id}/act")
async def act(item_id: int, request: Request, user=Depends(current_user)):
    b = await request.json()
    a = b.get("action")
    if a not in ACTIONS:
        raise HTTPException(400, "Thao tác không hợp lệ")
    need, frm, to, label = ACTIONS[a]
    me, t = user["username"], now()
    with db.cursor() as con:
        it = con.execute("SELECT * FROM work_items WHERE id=?", (item_id,)).fetchone()
        if not it:
            raise HTTPException(404, "Không có việc này")
        it = dict(it)
        if not _rights(user, it)[need]:
            raise HTTPException(403, {"work": "Đây không phải việc của bạn", "approve": "Bạn không duyệt được việc này",
                                      "edit": "Bạn không dừng được việc này"}[need])
        if it["status"] not in frm:
            raise HTTPException(409, f"Việc đang ở trạng thái {STATUS.get(it['status'])}, không {label.lower()} được")
        sets, vals = ["status=?", "updated=?"], [to, _iso(t)]
        note = (b.get("note") or "").strip() or None
        if a == "accept" or (a == "done" and not it["accepted_at"]):
            sets.append("accepted_at=?"); vals.append(_iso(t))
        if a == "done":
            sets.append("done_at=?"); vals.append(_iso(t))
            if b.get("link") is not None:
                sets.append("link=?"); vals.append((b.get("link") or "").strip())
        if a == "approve":
            sets += ["approved_at=?", "good=?"]; vals += [_iso(t), 1 if b.get("good") else 0]
            label = "Duyệt – hoàn thành tốt" if b.get("good") else label
        if a in ("revise", "reject"):
            reason = (b.get("reason") or "").strip()
            if not reason:
                raise HTTPException(400, "Ghi lý do để người làm biết cần sửa gì")
            sets.append("reason=?"); vals.append(reason)
            note = reason
            if a == "revise":
                sets += ["revisions=COALESCE(revisions,0)+1", "done_at=NULL", "approved_at=NULL", "good=0"]
        con.execute(f"UPDATE work_items SET {', '.join(sets)} WHERE id=?", vals + [item_id])
        _log(con, item_id, me, label, it["status"], to, note or (b.get("link") if a == "done" else None))
    return {"ok": True, "status": to}


# ---------------------------------------------------------------------------
# KPI / Dashboard
# ---------------------------------------------------------------------------
def kpi_rows(dept, d1, d2, t=None, only=None):
    t = t or now()
    cfg = _cfg(dept)
    members = [p for p in people() if p["dept"] == dept and (only is None or p["username"] == only)]
    if only is not None and not members:
        return []
    allm = [p for p in people() if p["dept"] == dept]
    days = workdays(d1, d2)
    khoan = round(cfg["day_hours"] * days, 2)
    s1, s2 = d1.isoformat(), (d2 + timedelta(days=1)).isoformat()
    rows = db.rows("SELECT * FROM work_items WHERE dept=? AND status<>'stopped'", (dept,))
    for r in rows:
        enrich(r, None, None, cfg, t)

    def calc(u):
        mine = [r for r in rows if r["assignee"] == u]
        done = [r for r in mine if r["status"] in DONE and r["done_at"] and s1 <= r["done_at"] < s2]
        appr = [r for r in done if r["status"] == "approved"]
        h = sum(r["hours"] for r in done)
        ha = sum(r["hours"] for r in appr)
        ontime = sum(1 for r in appr if r["due_state"] == "Đúng hạn")
        over = [r for r in mine if r["status"] in ("new", "doing", "revise") and r["due_at"] and s1 <= r["due_at"] < s2 and _dt(r["due_at"]) < t]
        unacc = [r for r in mine if r["status"] == "new" and r["assigned_at"] and s1 <= r["assigned_at"] < s2]
        return {"tasks": len(appr), "ontime": ontime, "late": len(appr) - ontime,
                "ontime_pct": round(ontime / len(appr) * 100, 1) if appr else None,
                "overdue_open": len(over), "not_accepted": len(unacc), "unaccepted_hours": round(sum(r["hours"] for r in unacc), 2),
                "hours_done": round(h, 2), "hours_review": round(h - ha, 2),
                "level": round(sum(r["hours"] * r["difficulty"] for r in appr) / ha, 2) if ha else None,
                "points_std": round(sum(r["points_std"] for r in appr), 2), "points_real": round(sum(r["points_real"] or 0 for r in appr), 2)}

    staff = [p for p in allm if not p["manager"]]
    staff_avg = sum(calc(p["username"])["hours_done"] for p in staff) / len(staff) if staff else 0
    out = []
    for p in members:
        c = calc(p["username"])
        c["mgr_hours"] = round(staff_avg * cfg["manager_pct"] / 100, 2) if p["manager"] else 0
        total = c["hours_done"] + c["mgr_hours"]
        c["kpi_pct"] = round(total / khoan * 100, 1) if khoan else None
        c["rank"] = None if c["kpi_pct"] is None else "A" if c["kpi_pct"] >= 85 else "B" if c["kpi_pct"] >= 65 else "C" if c["kpi_pct"] >= 50 else "D"
        c["level_rank"] = None if c["level"] is None else f"L{min(5, max(1, int(c['level'])))}"
        out.append(dict(p, **c, khoan=khoan, days=days))
    return out


@router.get("/kpi")
def kpi(dept: str = None, from_d: str = None, to_d: str = None, user=Depends(current_user)):
    t = now()
    d1 = date.fromisoformat(from_d) if from_d else t.date().replace(day=1)
    d2 = date.fromisoformat(to_d) if to_d else t.date()
    if d2 < d1:
        raise HTTPException(400, "Từ ngày phải trước đến ngày")
    dept = dept or user.get("dept") or (depts()[0]["code"] if depts() else "")
    if _mgr_of(user, dept):
        rows = kpi_rows(dept, d1, d2, t)
    else:
        rows = kpi_rows(dept, d1, d2, t, only=user["username"]) if user.get("dept") == dept else []
    return {"dept": dept, "from": d1.isoformat(), "to": d2.isoformat(), "days": workdays(d1, d2), "rows": rows,
            "manager_view": _mgr_of(user, dept), "cfg": _cfg(dept)}


# ---------------------------------------------------------------------------
# Việc định kỳ & cài đặt phòng
# ---------------------------------------------------------------------------
@router.get("/recurring")
def recurring_list(user=Depends(current_user)):
    names = _names()
    rows = db.rows("SELECT * FROM work_recurring ORDER BY active DESC, dept, title")
    rows = [r for r in rows if _mgr_of(user, r["dept"]) or r["assignee"] == user["username"]]
    for r in rows:
        r["assignee_name"], r["assigner_name"] = names.get(r["assignee"], r["assignee"]), names.get(r["assigner"], r["assigner"])
        r["can_edit"] = _mgr_of(user, r["dept"])
    return rows


@router.post("/recurring")
async def recurring_save(request: Request, user=Depends(current_user)):
    b = await request.json()
    P = {p["username"]: p for p in people()}
    with db.cursor() as con:
        if b.get("id"):
            old = con.execute("SELECT * FROM work_recurring WHERE id=?", (b["id"],)).fetchone()
            if not old or not _mgr_of(user, old["dept"]):
                raise HTTPException(403, "Chỉ quản lý phòng sửa được việc định kỳ")
            if b.get("delete"):
                con.execute("DELETE FROM work_recurring WHERE id=?", (b["id"],))
                return {"ok": True}
        to = b.get("assignee")
        if to not in P or P[to]["dept"] not in {d["code"] for d in depts()}:
            raise HTTPException(400, "Chọn người nhận thuộc phòng đã bật giao việc")
        dept = P[to]["dept"]
        if not _mgr_of(user, dept):
            raise HTTPException(403, "Chỉ quản lý phòng tạo được việc định kỳ")
        cyc = b.get("cycle")
        if cyc not in CYCLES:
            raise HTTPException(400, "Chọn chu kỳ")
        wd = int(b.get("weekday") or 0) if cyc == "weekly" else None
        md = int(_num(b.get("monthday") or 0, 1, 31, "Ngày trong tháng từ 1 đến 31")) if cyc == "monthly" else None
        due_time = (b.get("due_time") or "23:59").strip()
        try:
            datetime.strptime(due_time, "%H:%M")
        except ValueError:
            raise HTTPException(400, "Giờ hạn dạng 17:00")
        title = (b.get("title") or "").strip()
        if not title:
            raise HTTPException(400, "Nhập tên công việc")
        vals = (dept, title, b.get("assigner") or user["username"], to, int(_num(b.get("difficulty", 1), 1, 5, "Độ khó từ 1 đến 5")),
                _num(b.get("hours", 1), 0.05, 100, "Thời lượng phải lớn hơn 0"), cyc, wd, md, due_time, 1 if b.get("active", True) else 0)
        if b.get("id"):
            con.execute("""UPDATE work_recurring SET dept=?,title=?,assigner=?,assignee=?,difficulty=?,hours=?,cycle=?,weekday=?,monthday=?,
                           due_time=?,active=? WHERE id=?""", vals + (b["id"],))
            return {"ok": True, "id": b["id"]}
        cur = con.execute("""INSERT INTO work_recurring(dept,title,assigner,assignee,difficulty,hours,cycle,weekday,monthday,due_time,active,last_on,created)
                             VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""", vals + ((now().date() - timedelta(days=1)).isoformat(), _iso(now())))
        return {"ok": True, "id": cur.lastrowid}


@router.post("/depts")
async def depts_save(request: Request, user=Depends(current_user)):
    if user.get("role") != "admin":
        raise HTTPException(403, "Chỉ quản trị")
    b = await request.json()
    code = (b.get("code") or "").strip().upper()
    if not code:
        raise HTTPException(400, "Thiếu mã phòng")
    cfg = dict(DEFAULT_CFG)
    cfg.update({k: v for k, v in (b.get("cfg") or {}).items() if k in DEFAULT_CFG})
    for k in ("day_hours", "sat_hours", "manager_pct", "bonus_pct", "revise_pct", "keep_days"):
        cfg[k] = _num(cfg[k], 0, 1000, f"Giá trị {k} chưa đúng")
    late = cfg["late"] if isinstance(cfg["late"], list) else [x for x in str(cfg["late"]).replace(",", ".").split(";")]
    cfg["late"] = [_num(x, 0, 2, "Hệ số hạn dạng 1; 0,8; 0,6") for x in late][:3]
    if len(cfg["late"]) != 3:
        raise HTTPException(400, "Hệ số hạn cần 3 số: đúng hạn; trễ ≤ 1 ngày; trễ hơn")
    with db.cursor() as con:
        con.execute("""INSERT INTO work_depts(code,name,prefix,active,cfg) VALUES(?,?,?,?,?)
                       ON CONFLICT(code) DO UPDATE SET name=excluded.name, prefix=excluded.prefix, active=excluded.active, cfg=excluded.cfg""",
                    (code, b.get("name") or code, (b.get("prefix") or code).strip().upper(), 1 if b.get("active", True) else 0, json.dumps(cfg)))
    return {"ok": True}
