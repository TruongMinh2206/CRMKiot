"""Quy trình sự kiện liên phòng ban (vd RCD): 6 bước từ lên kế hoạch tới tổng kết, mỗi bước có việc
của từng phòng ban; sau sự kiện mỗi khách là một thẻ đi qua các bước chăm sóc (làm quen, mời đăng bài
kèm hashtag, chốt đơn). Khách được nối với khách KiotViet theo số điện thoại để biết khách cũ/mới và
doanh số mua sau sự kiện.

Các phòng làm việc nối với nhau qua:
  - mục tiêu chung của sự kiện (data mới, khách mua, doanh số, bài hashtag, trần chi phí) — số thực tế tự tính;
  - mỗi việc ghi rõ phục vụ chỉ tiêu nào và phải chờ việc nào (thường của phòng khác) xong trước;
  - việc xong thì ghi "bàn giao" cho các việc vừa hết phải chờ, phòng nhận thấy ngay trên trang;
  - các việc "cổng duyệt" của BGĐ chặn chuyển sang bước sau khi chưa duyệt;
  - ngân sách theo phòng, trao đổi ngay trong từng việc, bài học để nhân bản cho sự kiện sau.
"""
import json
import re
from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request

from . import db
from .main_auth import current_user, is_mgr, team_names

router = APIRouter(prefix="/api")

DEPTS = {"BGD": "Sếp / BGĐ", "SALE": "Sale", "MKT": "Marketing", "KT": "Kế toán", "KHO": "Kho / Thu mua", "HCNS": "HCNS"}
STEPS = ["s0", "s1", "s2", "s3", "s4", "s5", "sx"]
STATUS = ["todo", "doing", "done"]
STATUS_VN = {"todo": "Cần làm", "doing": "Đang làm", "done": "Xong"}
PHASES = ["Lên kế hoạch", "Chuẩn bị", "Tại sự kiện", "Sau sự kiện 72h", "Chăm sóc & lan toả", "Tổng kết"]
# Chỉ tiêu chung: (mã, tên, đơn vị). "budget" là trần chi phí — thấp hơn là tốt.
KPIS = [("data_new", "Data khách mới", "người"), ("buyers", "Khách mua sau sự kiện", "người"),
        ("revenue", "Doanh số từ khách sự kiện", "đ"), ("posts", "Bài đăng hashtag", "bài"),
        ("budget", "Chi phí (trần ngân sách)", "đ")]
KPI_KEYS = {k for k, _, _ in KPIS}
TEMPLATE = [
    ("t01", 0, "BGD", "Chốt mục tiêu: số data khách mới, doanh số, số bài đăng hashtag, ngân sách", -30),
    ("t02", 0, "MKT", "Kế hoạch truyền thông, concept booth, chốt hashtag chính thức", -25),
    ("t03", 0, "SALE", "Lập danh sách khách cũ sẽ mời tới booth; chia chỉ tiêu data cho từng team", -25),
    ("t04", 0, "KT", "Dự toán chi phí; chính sách thưởng team (data, vé, doanh số)", -25),
    ("t05", 0, "KHO", "Danh sách hàng bán, quà tặng, mẫu thử kèm số lượng", -25),
    ("t06", 0, "HCNS", "Phân công nhân sự, lịch trực booth, đồng phục", -21),
    ("t07", 0, "BGD", "Duyệt kế hoạch tổng", -21),
    ("t08", 1, "MKT", "Thiết kế backdrop / standee, mã QR đăng ký khách, form ghi data", -10),
    ("t09", 1, "MKT", "Bài teaser trên fanpage; gửi khách cũ thông tin sự kiện + hashtag", -7),
    ("t10", 1, "SALE", "Nhắn mời từng khách cũ, xác nhận ngày giờ ghé booth", -5),
    ("t11", 1, "KT", "Bảng giá sự kiện, mã khuyến mãi trên KiotViet", -3),
    ("t12", 1, "KHO", "Xuất hàng / quà, đóng thùng, checklist vận chuyển", -3),
    ("t13", 1, "HCNS", "Đào tạo kịch bản tư vấn và cách ghi data cho người trực", -2),
    ("t14", 1, "BGD", "Duyệt lần cuối trước sự kiện", -1),
    ("t15", 2, "SALE", "Ghi data 100% khách ghé booth qua form / QR: tên, SĐT, nhu cầu, khách mới hay cũ", 0),
    ("t16", 2, "MKT", "Chụp ảnh khách cùng sản phẩm (xin phép), đặt tên ảnh theo tên + SĐT khách", 0),
    ("t17", 2, "SALE", "Mời khách chụp check-in và đăng bài kèm hashtag ngay tại booth", 0),
    ("t18", 2, "KHO", "Kiểm hàng / quà còn lại cuối mỗi ngày", 0),
    ("t19", 2, "KT", "Ghi nhận hoá đơn bán tại booth lên KiotViet trong ngày", 0),
    ("t20", 3, "SALE", "Làm sạch data: bỏ trùng SĐT, tách khách mới / khách cũ, gán sale phụ trách, nhập vào tab Khách sau sự kiện", 1),
    ("t21", 3, "MKT", "Gom ảnh lên Drive theo từng khách, gửi link ảnh cho sale phụ trách", 2),
    ("t22", 3, "SALE", "Kết bạn Zalo và nhắn làm quen 100% khách mới trong 48 giờ", 2),
    ("t23", 3, "SALE", "Gửi ảnh cho từng khách + mời đăng bài kèm hashtag", 3),
    ("t24", 3, "MKT", "Đăng album sự kiện trên fanpage, gắn hashtag", 3),
    ("t25", 3, "KT", "Đối soát doanh thu, hàng xuất – hàng về sau sự kiện", 3),
    ("t26", 4, "SALE", "Chăm khách mới theo bảng: gửi ưu đãi, chốt đơn đầu tiên", 14),
    ("t27", 4, "MKT", "Theo dõi bài đăng hashtag, gom link, chọn bài hay để repost", 14),
    ("t28", 4, "SALE", "Nhắc lại khách chưa đăng bài (sau 3 ngày), cập nhật link bài vào thẻ khách", 10),
    ("t29", 4, "KT", "Gửi quà cảm ơn khách đã đăng bài; chốt thưởng team", 20),
    ("t30", 5, "BGD", "Họp tổng kết: data mới, doanh số, số bài hashtag, chi phí trên mỗi khách", 30),
    ("t31", 5, "HCNS", "Ghi nhận khen thưởng, lưu bài học cho sự kiện sau", 30),
]
# Việc mẫu phải chờ việc nào xong trước (bàn giao giữa các phòng) và phục vụ chỉ tiêu nào
FLOW = {
    "t02": ("t01", "posts"), "t03": ("t01", "data_new"), "t04": ("t01", "budget"), "t05": ("t01", "revenue"),
    "t06": ("t01", ""), "t07": ("t02 t03 t04 t05 t06", ""),
    "t08": ("t07", "data_new"), "t09": ("t07", "posts"), "t10": ("t03 t09", "revenue"), "t11": ("t04 t07", "revenue"),
    "t12": ("t05 t07", "revenue"), "t13": ("t06 t08", "data_new"), "t14": ("t08 t09 t10 t11 t12 t13", ""),
    "t15": ("t14", "data_new"), "t16": ("t14", "posts"), "t17": ("t14", "posts"), "t18": ("t14", "budget"),
    "t19": ("t14", "revenue"),
    "t20": ("t15", "data_new"), "t21": ("t16 t20", "posts"), "t22": ("t20", "buyers"), "t23": ("t21 t22", "posts"),
    "t24": ("t21", "posts"), "t25": ("t18 t19", "revenue"),
    "t26": ("t22", "buyers"), "t27": ("t23", "posts"), "t28": ("t23", "posts"), "t29": ("t27 t28", "posts"),
    "t30": ("t25 t26 t29", ""), "t31": ("t30", ""),
}
GATES = {"t07", "t14", "t30"}   # BGĐ duyệt: chưa xong thì không chuyển sang bước sau


def _now():
    return datetime.now().isoformat(timespec="seconds")


def _who(user):
    return user.get("display_name") or user.get("username") or ""


def _can_edit(user):
    return is_mgr(user) or user.get("role") == "lead"


def _json(v, default):
    try:
        return json.loads(v) if v else default
    except (TypeError, ValueError):
        return default


def _num(v):
    try:
        return max(0.0, float(v or 0))
    except (TypeError, ValueError):
        return 0.0


def _short(s, n=80):
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _due(d, off):
    try:
        return (date.fromisoformat(d) + timedelta(days=int(off or 0))).isoformat()
    except (TypeError, ValueError):
        return None


def norm_phone(p):
    p = re.sub(r"\D", "", str(p or ""))
    if p.startswith("84") and len(p) >= 11:
        p = "0" + p[2:]
    return p


def _phone_index():
    """SĐT (chuẩn hoá) -> khách KiotViet. Một khách có thể ghi nhiều số, cách nhau bởi dấu phẩy / gạch."""
    idx = {}
    for c in db.rows("SELECT id, code, name, contact_number FROM customers WHERE contact_number IS NOT NULL AND contact_number<>''"):
        for part in re.split(r"[,;/\s-]+(?=\+?\d{8,})", c["contact_number"]):
            n = norm_phone(part)
            if len(n) >= 9:
                idx.setdefault(n, c)
    return idx


def _visible(user, sale):
    names = team_names(user)
    return names is None or not sale or sale in names


def _event(eid):
    e = db.one("SELECT * FROM events WHERE id=?", (eid,))
    if not e:
        raise HTTPException(404, "Không tìm thấy sự kiện")
    return e


def _log(con, eid, kind, content, user, task_id=None):
    """Nhật ký sự kiện: bàn giao, đổi trạng thái, chuyển bước, trao đổi."""
    con.execute("INSERT INTO event_activity(event_id,task_id,kind,author,username,content,created) VALUES(?,?,?,?,?,?,?)",
                (eid, task_id, kind, _who(user), user.get("username") or "", content, _now()))


def _link(con, eid=None):
    """Gắn việc phải chờ / chỉ tiêu / cổng duyệt cho việc mẫu chưa có (sự kiện mới, hoặc tạo trước bản này)."""
    w, p = (" AND event_id=?", (eid,)) if eid else ("", ())
    rows = con.execute("SELECT id, event_id, tid FROM event_tasks WHERE deps IS NULL" + w, p).fetchall()
    if not rows:
        return
    ids = {(r["event_id"], r["tid"]): r["id"] for r in con.execute("SELECT id, event_id, tid FROM event_tasks WHERE tid<>'c'" + w, p)}
    for r in rows:
        deps, kpi = FLOW.get(r["tid"], ("", ""))
        dl = [ids[(r["event_id"], d)] for d in deps.split() if (r["event_id"], d) in ids]
        con.execute("UPDATE event_tasks SET deps=?, kpi=COALESCE(kpi, ?), gate=? WHERE id=?",
                    (json.dumps(dl), kpi, int(r["tid"] in GATES), r["id"]))


def upgrade():
    """Chạy lúc khởi động: nối chuỗi bàn giao cho các sự kiện tạo từ bản cũ."""
    with db.cursor() as con:
        _link(con)


def _deps(con, eid, tid, v):
    """Danh sách việc phải chờ: chỉ nhận việc cùng sự kiện, không tự chờ mình, không tạo vòng lặp."""
    live = {r["id"]: _json(r["deps"], []) for r in con.execute("SELECT id, deps FROM event_tasks WHERE event_id=? AND custom>=0", (eid,))}
    out = []
    for d in v if isinstance(v, list) else []:
        try:
            d = int(d)
        except (TypeError, ValueError):
            continue
        if d in live and d != tid and d not in out:
            out.append(d)
    stack, seen = list(out), set()
    while tid and stack:
        x = stack.pop()
        if x == tid:
            raise HTTPException(400, "Không chọn được: việc kia đang (gián tiếp) chờ chính việc này, sẽ thành vòng lặp")
        if x not in seen:
            seen.add(x)
            stack.extend(live.get(x, []))
    return out


def _handoff(con, eid, tid, user):
    """Việc vừa xong: ghi bàn giao cho các việc đã hết phải chờ (thường là việc của phòng khác)."""
    rows = con.execute("SELECT id, dept, text, deps, status FROM event_tasks WHERE event_id=? AND custom>=0", (eid,)).fetchall()
    st = {r["id"]: r["status"] for r in rows}
    me = next((r for r in rows if r["id"] == tid), None)
    for r in rows if me else ():
        deps = [d for d in _json(r["deps"], []) if d in st]
        if tid in deps and r["status"] != "done" and all(st[d] == "done" for d in deps):
            _log(con, eid, "handoff", f"{DEPTS.get(me['dept'], me['dept'])} xong “{_short(me['text'], 60)}” → "
                                      f"{DEPTS.get(r['dept'], r['dept'])} bắt đầu được “{_short(r['text'], 60)}”", user, r["id"])


def _set_phase(con, eid, b, user):
    old = con.execute("SELECT phase FROM events WHERE id=?", (eid,)).fetchone()
    if not old:
        raise HTTPException(404, "Không tìm thấy sự kiện")
    old, new = old["phase"] or 0, max(0, min(5, int(b["phase"])))
    if new > old:
        gates = con.execute("""SELECT text FROM event_tasks WHERE event_id=? AND gate=1 AND custom>=0 AND status<>'done'
                               AND phase>=? AND phase<? ORDER BY phase, off""", (eid, old, new)).fetchall()
        if gates and not (b.get("force") and user.get("role") == "admin"):
            raise HTTPException(400, "Chưa qua cổng duyệt: " + "; ".join(g["text"] for g in gates))
    con.execute("UPDATE events SET phase=? WHERE id=?", (new, eid))
    if new != old:
        _log(con, eid, "phase", f"Chuyển sang bước {new + 1}: {PHASES[new]}", user)
    return {"ok": True}


def _clone(con, src, eid):
    """Nhân bản quy trình từ sự kiện cũ: giữ việc (cả việc tự thêm), người phụ trách, chuỗi chờ, ngân sách dự kiến."""
    _link(con, src)
    old = con.execute("SELECT * FROM event_tasks WHERE event_id=? AND custom>=0 ORDER BY id", (src,)).fetchall()
    ids = {}
    for t in old:
        cur = con.execute("""INSERT INTO event_tasks(event_id,tid,phase,dept,text,off,custom,status,owner,note,deps,kpi,gate)
                             VALUES(?,?,?,?,?,?,?,'todo',?,'','[]',?,?)""",
                          (eid, t["tid"], t["phase"], t["dept"], t["text"], t["off"], t["custom"], t["owner"] or "", t["kpi"] or "", t["gate"] or 0))
        ids[t["id"]] = cur.lastrowid
    for t in old:
        con.execute("UPDATE event_tasks SET deps=? WHERE id=?", (json.dumps([ids[d] for d in _json(t["deps"], []) if d in ids]), ids[t["id"]]))
    for r in con.execute("SELECT dept, item, planned, note FROM event_budget WHERE event_id=? ORDER BY id", (src,)).fetchall():
        con.execute("INSERT INTO event_budget(event_id,dept,item,planned,actual,note,updated) VALUES(?,?,?,?,0,?,?)",
                    (eid, r["dept"], r["item"], r["planned"], r["note"], _now()))


def _targets(v):
    return {k: _num(v[k]) for k in KPI_KEYS if _num(v.get(k))} if isinstance(v, dict) else {}


@router.get("/events")
def events_list(user=Depends(current_user)):
    rows = db.rows("SELECT * FROM events ORDER BY COALESCE(NULLIF(date,''),'9999') DESC, id DESC")
    for e in rows:
        t = db.one("SELECT COUNT(*) n, SUM(status='done') d FROM event_tasks WHERE event_id=? AND custom>=0", (e["id"],))
        e["tasks"], e["done"] = t["n"], t["d"] or 0
        e["customers"] = db.one("SELECT COUNT(*) n FROM event_customers WHERE event_id=?", (e["id"],))["n"]
        e["targets"] = _json(e["targets"], {})
    return rows


@router.post("/events")
async def events_save(request: Request, user=Depends(current_user)):
    b = await request.json()
    eid = b.get("id")
    with db.cursor() as con:
        if eid and b.get("delete"):
            if user.get("role") != "admin":
                raise HTTPException(403, "Chỉ quản trị mới xoá được sự kiện")
            for t in ("event_tasks", "event_customers", "event_budget", "event_activity"):
                con.execute(f"DELETE FROM {t} WHERE event_id=?", (eid,))
            con.execute("DELETE FROM events WHERE id=?", (eid,))
            return {"ok": True}
        if not _can_edit(user):
            raise HTTPException(403, "Chỉ quản lý / trưởng nhóm được tạo, sửa hoặc chuyển bước sự kiện")
        if eid and "phase" in b and set(b) <= {"id", "phase", "force"}:
            return _set_phase(con, eid, b, user)
        if eid and "lessons" in b and set(b) <= {"id", "lessons"}:
            con.execute("UPDATE events SET lessons=? WHERE id=?", ((b.get("lessons") or "").strip(), eid))
            return {"ok": True}
        name = (b.get("name") or "").strip()
        if not name:
            raise HTTPException(400, "Nhập tên sự kiện")
        vals = (name, (b.get("date") or "")[:10], (b.get("place") or "").strip(), (b.get("hashtag") or "").strip(),
                (b.get("goal") or "").strip(), json.dumps(_targets(b.get("targets"))))
        if eid:
            con.execute("UPDATE events SET name=?, date=?, place=?, hashtag=?, goal=?, targets=? WHERE id=?", vals + (eid,))
        else:
            src = con.execute("SELECT id, name FROM events WHERE id=?", (b["clone_from"],)).fetchone() if b.get("clone_from") else None
            cur = con.execute("""INSERT INTO events(name,date,place,hashtag,goal,targets,phase,created,created_by,source_id)
                                 VALUES(?,?,?,?,?,?,0,?,?,?)""", vals + (_now(), _who(user), src["id"] if src else None))
            eid = cur.lastrowid
            if src:
                _clone(con, src["id"], eid)
            else:
                con.executemany("INSERT INTO event_tasks(event_id,tid,phase,dept,text,off,custom,status) VALUES(?,?,?,?,?,?,0,'todo')",
                                [(eid, t, p, d, x, o) for t, p, d, x, o in TEMPLATE])
                _link(con, eid)
            _log(con, eid, "phase", "Tạo sự kiện" + (f" theo quy trình của “{src['name']}”" if src else ""), user)
    return {"ok": True, "id": eid}


@router.get("/events/{eid}")
def event_detail(eid: int, user=Depends(current_user)):
    e = _event(eid)
    e["targets"] = _json(e["targets"], {})
    tasks = db.rows("SELECT * FROM event_tasks WHERE event_id=? AND custom>=0 ORDER BY phase, off, id", (eid,))
    live = {t["id"]: t for t in tasks}
    for t in tasks:
        t["deps"] = [d for d in _json(t["deps"], []) if d in live and d != t["id"]]
        t["waiting"] = [d for d in t["deps"] if live[d]["status"] != "done"]
        t["blocks"] = []
    for t in tasks:
        if t["status"] != "done":
            for d in t["deps"]:
                live[d]["blocks"].append(t["id"])
    allc = db.rows("SELECT * FROM event_customers WHERE event_id=? ORDER BY updated DESC, id DESC", (eid,))
    # doanh số mua trên KiotViet từ ngày sự kiện (khách đã nối được theo SĐT)
    ids = sorted({c["customer_id"] for c in allc if c["customer_id"]})
    after, kv = {}, {}
    if ids:
        q = ",".join("?" * len(ids))
        if e["date"]:
            for r in db.rows(f"""SELECT customer_id cid, SUM(COALESCE(net_total,total)) rev, COUNT(*) n, MAX(purchase_date) last
                                 FROM invoices WHERE status=1 AND customer_id IN ({q}) AND purchase_date>=? GROUP BY 1""", (*ids, e["date"])):
                after[r["cid"]] = r
        kv = {r["id"]: r for r in db.rows(f"SELECT id, code, name FROM customers WHERE id IN ({q})", tuple(ids))}
    for c in allc:
        c["history"] = json.loads(c["history"] or "[]")
        a = after.get(c["customer_id"]) or {}
        c["rev_after"], c["inv_after"], c["last_buy"] = a.get("rev") or 0, a.get("n") or 0, a.get("last")
        k = kv.get(c["customer_id"])
        c["kv_code"], c["kv_name"] = (k["code"], k["name"]) if k else (None, None)
    budget = db.rows("SELECT * FROM event_budget WHERE event_id=? ORDER BY dept, id", (eid,))
    # số thực tế của chỉ tiêu chung tính trên toàn bộ khách sự kiện (không lọc theo sale đang xem)
    goals = {"data_new": sum(c["kind"] == "moi" for c in allc), "buyers": sum(c["rev_after"] > 0 for c in allc),
             "revenue": sum(c["rev_after"] for c in allc), "posts": sum(bool((c["post"] or "").strip()) for c in allc),
             "budget": sum(r["actual"] or 0 for r in budget)}
    src = db.one("SELECT id, name, date, lessons FROM events WHERE id=?", (e["source_id"],)) if e.get("source_id") else None
    return {"event": e, "tasks": tasks, "customers": [c for c in allc if _visible(user, c["sale"])], "depts": DEPTS,
            "goals": goals, "budget": budget, "source": src, "mgr": is_mgr(user), "edit": _can_edit(user),
            "activity": db.rows("SELECT * FROM event_activity WHERE event_id=? ORDER BY id DESC LIMIT 300", (eid,))}


@router.post("/events/{eid}/task")
async def event_task(eid: int, request: Request, user=Depends(current_user)):
    _event(eid)
    b = await request.json()
    tid = b.get("id")
    with db.cursor() as con:
        old = con.execute("SELECT * FROM event_tasks WHERE id=? AND event_id=?", (tid, eid)).fetchone() if tid else None
        if tid and not old:
            raise HTTPException(404, "Không tìm thấy việc")
        if old and b.get("delete"):
            con.execute("UPDATE event_tasks SET custom=-1 WHERE id=?", (tid,))
            _log(con, eid, "task", f"Xoá việc “{_short(old['text'])}” của {DEPTS.get(old['dept'], old['dept'])}", user, tid)
            return {"ok": True}
        kpi = b.get("kpi") if b.get("kpi") in KPI_KEYS else ""
        if not old:
            text = (b.get("text") or "").strip()
            if not text or b.get("dept") not in DEPTS:
                raise HTTPException(400, "Nhập nội dung việc và chọn phòng ban")
            status = b.get("status") if b.get("status") in STATUS else "todo"
            cur = con.execute("""INSERT INTO event_tasks(event_id,tid,phase,dept,text,off,custom,status,owner,note,deps,kpi,gate,done_at,updated,updated_by)
                                 VALUES(?,?,?,?,?,?,1,?,?,?,?,?,?,?,?,?)""",
                              (eid, "c", int(b.get("phase") or 0), b["dept"], text, int(b.get("off") or 0), status,
                               (b.get("owner") or "").strip(), (b.get("note") or "").strip(), json.dumps(_deps(con, eid, None, b.get("deps"))),
                               kpi, int(bool(b.get("gate")) and _can_edit(user)), _now() if status == "done" else None, _now(), _who(user)))
            _log(con, eid, "task", f"Thêm việc “{_short(text)}” cho {DEPTS[b['dept']]}", user, cur.lastrowid)
            return {"ok": True, "id": cur.lastrowid}
        sets, vals = [], []
        for k in ("status", "owner", "note", "text", "dept", "phase", "off"):
            if k in b:
                v = b[k]
                if k == "status" and v not in STATUS:
                    continue
                if k == "dept" and v not in DEPTS:
                    continue
                sets.append(f"{k}=?")
                vals.append(int(v or 0) if k in ("phase", "off") else (v or "").strip() if isinstance(v, str) else v)
        if "deps" in b:
            sets.append("deps=?")
            vals.append(json.dumps(_deps(con, eid, tid, b["deps"])))
        if "kpi" in b:
            sets.append("kpi=?")
            vals.append(kpi)
        if "gate" in b and _can_edit(user):
            sets.append("gate=?")
            vals.append(int(bool(b["gate"])))
        moved = b.get("status") in STATUS and b["status"] != old["status"]
        if moved:
            sets.append("done_at=?")
            vals.append(_now() if b["status"] == "done" else None)
        if sets:
            con.execute(f"UPDATE event_tasks SET {', '.join(sets)}, updated=?, updated_by=? WHERE id=? AND event_id=?",
                        (*vals, _now(), _who(user), tid, eid))
        if moved:
            _log(con, eid, "status", f"{DEPTS.get(old['dept'], old['dept'])}: “{_short(old['text'])}” → {STATUS_VN[b['status']]}", user, tid)
            if b["status"] == "done":
                _handoff(con, eid, tid, user)
    return {"ok": True}


@router.post("/events/{eid}/comment")
async def event_comment(eid: int, request: Request, user=Depends(current_user)):
    """Trao đổi giữa các phòng ngay trong từng việc (hoặc trao đổi chung của sự kiện)."""
    _event(eid)
    b = await request.json()
    with db.cursor() as con:
        if b.get("delete"):
            r = con.execute("SELECT username FROM event_activity WHERE id=? AND event_id=? AND kind='comment'", (b["delete"], eid)).fetchone()
            if not r:
                raise HTTPException(404, "Không tìm thấy trao đổi")
            if r["username"] != user.get("username") and user.get("role") != "admin":
                raise HTTPException(403, "Chỉ xoá được trao đổi của mình")
            con.execute("DELETE FROM event_activity WHERE id=?", (b["delete"],))
            return {"ok": True}
        text = (b.get("content") or "").strip()
        if not text:
            raise HTTPException(400, "Nhập nội dung trao đổi")
        tid = b.get("task_id") or None
        if tid and not con.execute("SELECT 1 FROM event_tasks WHERE id=? AND event_id=?", (tid, eid)).fetchone():
            raise HTTPException(404, "Không tìm thấy việc")
        _log(con, eid, "comment", text[:2000], user, tid)
    return {"ok": True}


@router.post("/events/{eid}/budget")
async def event_budget(eid: int, request: Request, user=Depends(current_user)):
    """Ngân sách theo phòng ban: dự kiến và thực chi từng hạng mục."""
    _event(eid)
    if not _can_edit(user):
        raise HTTPException(403, "Chỉ quản lý / kế toán / trưởng nhóm được sửa ngân sách")
    b = await request.json()
    bid = b.get("id")
    with db.cursor() as con:
        if bid and b.get("delete"):
            con.execute("DELETE FROM event_budget WHERE id=? AND event_id=?", (bid, eid))
            return {"ok": True}
        item = (b.get("item") or "").strip()
        if not item or b.get("dept") not in DEPTS:
            raise HTTPException(400, "Nhập hạng mục và chọn phòng ban")
        vals = (b["dept"], item, _num(b.get("planned")), _num(b.get("actual")), (b.get("note") or "").strip(), _now(), _who(user))
        if bid:
            con.execute("UPDATE event_budget SET dept=?, item=?, planned=?, actual=?, note=?, updated=?, updated_by=? WHERE id=? AND event_id=?",
                        vals + (bid, eid))
        else:
            bid = con.execute("INSERT INTO event_budget(dept,item,planned,actual,note,updated,updated_by,event_id) VALUES(?,?,?,?,?,?,?,?)",
                              vals + (eid,)).lastrowid
    return {"ok": True, "id": bid}


def alerts():
    """Cảnh báo ở trang Tổng quan: việc sự kiện trễ hạn (sự kiện gần đây, chưa sang bước Tổng kết)."""
    today = date.today()
    out = []
    for e in db.rows("SELECT id, name, date FROM events WHERE COALESCE(phase,0)<5 AND date>=?", ((today - timedelta(days=60)).isoformat(),)):
        late = [t for t in db.rows("SELECT dept, off FROM event_tasks WHERE event_id=? AND custom>=0 AND status<>'done'", (e["id"],))
                if (_due(e["date"], t["off"]) or "9999") < today.isoformat()]
        if late:
            depts = sorted({DEPTS.get(t["dept"], t["dept"]) for t in late})
            out.append({"key": f"event{e['id']}", "level": "bad", "n": len(late), "href": "#events",
                        "title": f"Việc sự kiện trễ hạn · {e['name']}", "note": "phòng đang chậm: " + ", ".join(depts)})
    return out


def _kind_auto(con, cid, date):
    if not cid:
        return "moi"
    if not date:
        return "cu"
    r = con.execute("SELECT 1 FROM invoices WHERE customer_id=? AND status=1 AND purchase_date<? LIMIT 1", (cid, date)).fetchone()
    return "cu" if r else "moi"


@router.post("/events/{eid}/customer")
async def event_customer(eid: int, request: Request, user=Depends(current_user)):
    e = _event(eid)
    b = await request.json()
    cid = b.get("id")
    with db.cursor() as con:
        old = con.execute("SELECT * FROM event_customers WHERE id=? AND event_id=?", (cid, eid)).fetchone() if cid else None
        if cid and not old:
            raise HTTPException(404, "Không tìm thấy khách")
        if old and not _visible(user, old["sale"]):
            raise HTTPException(403, "Khách này do sale khác phụ trách")
        if old and b.get("delete"):
            con.execute("DELETE FROM event_customers WHERE id=?", (cid,))
            return {"ok": True}
        merged = {**(dict(old) if old else {"kind": "", "step": "s0", "sale": "", "history": "[]"}), **b}
        step = merged.get("step") if merged.get("step") in STEPS else "s0"
        if step == "s3" and not (merged.get("post") or "").strip():
            raise HTTPException(400, "Bước “Đã đăng bài” cần link bài đăng của khách")
        phone = (merged.get("phone") or "").strip()
        np_ = norm_phone(phone)
        if np_:
            for r in con.execute("SELECT id, phone FROM event_customers WHERE event_id=?", (eid,)).fetchall():
                if r["id"] != cid and norm_phone(r["phone"]) == np_:
                    raise HTTPException(400, "SĐT này đã có trong danh sách sự kiện")
        kv = _phone_index().get(np_) if np_ else None
        kvid = kv["id"] if kv else None
        kind = merged.get("kind") if merged.get("kind") in ("moi", "cu") else _kind_auto(con, kvid, e["date"])
        hist = json.loads(old["history"] or "[]") if old else []
        if not old or old["step"] != step:
            hist = (hist + [{"step": step, "at": _now(), "by": _who(user)}])[-30:]
        sale = (merged.get("sale") or "").strip()
        if not is_mgr(user) and not _visible(user, sale):
            raise HTTPException(403, "Chỉ giao được cho sale trong nhóm của bạn")
        vals = ((merged.get("name") or "").strip() or (kv["name"] if kv else ""), phone, kind, sale, step,
                (merged.get("photo") or "").strip(), (merged.get("post") or "").strip(), (merged.get("note") or "").strip(),
                kvid, json.dumps(hist, ensure_ascii=False), _now(), _who(user))
        if not vals[0] and not phone:
            raise HTTPException(400, "Nhập tên hoặc SĐT khách")
        if old:
            con.execute("""UPDATE event_customers SET name=?, phone=?, kind=?, sale=?, step=?, photo=?, post=?, note=?,
                           customer_id=?, history=?, updated=?, updated_by=? WHERE id=?""", vals + (cid,))
        else:
            cur = con.execute("""INSERT INTO event_customers(name,phone,kind,sale,step,photo,post,note,customer_id,history,updated,updated_by,
                                 event_id,created) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", vals + (eid, _now()))
            cid = cur.lastrowid
    return {"ok": True, "id": cid}


@router.post("/events/{eid}/import")
async def event_import(eid: int, request: Request, user=Depends(current_user)):
    """Nhập nhiều khách một lúc (dán từ Google Sheet / Excel). SĐT trùng trong sự kiện thì bỏ qua."""
    e = _event(eid)
    b = await request.json()
    rows = b.get("rows") or []
    if not isinstance(rows, list) or len(rows) > 3000:
        raise HTTPException(400, "Danh sách không hợp lệ (tối đa 3.000 dòng)")
    idx = _phone_index()
    mine = None if is_mgr(user) else user.get("sale_name")
    added = dup = 0
    with db.cursor() as con:
        have = {norm_phone(r["phone"]) for r in con.execute("SELECT phone FROM event_customers WHERE event_id=?", (eid,)).fetchall()}
        have.discard("")
        for r in rows:
            name, phone = (r.get("name") or "").strip(), (r.get("phone") or "").strip()
            n = norm_phone(phone)
            if not name and not n:
                continue
            if n and n in have:
                dup += 1
                continue
            if n:
                have.add(n)
            kv = idx.get(n) if n else None
            kvid = kv["id"] if kv else None
            kind = r.get("kind") if r.get("kind") in ("moi", "cu") else _kind_auto(con, kvid, e["date"])
            sale = (r.get("sale") or "").strip() or (mine or "")
            now = _now()
            con.execute("""INSERT INTO event_customers(event_id,name,phone,kind,sale,step,photo,post,note,customer_id,history,created,updated,updated_by)
                           VALUES(?,?,?,?,?,'s0','','',?,?,?,?,?,?)""",
                        (eid, name or (kv["name"] if kv else ""), phone, kind, sale, (r.get("note") or "").strip(), kvid,
                         json.dumps([{"step": "s0", "at": now, "by": _who(user)}], ensure_ascii=False), now, now, _who(user)))
            added += 1
    return {"added": added, "dup": dup}
