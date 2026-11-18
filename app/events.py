"""Quy trình sự kiện liên phòng ban (vd RCD): 6 bước từ lên kế hoạch tới tổng kết, mỗi bước có việc
của từng phòng ban; sau sự kiện mỗi khách là một thẻ đi qua các bước chăm sóc (làm quen, mời đăng bài
kèm hashtag, chốt đơn). Khách được nối với khách KiotViet theo số điện thoại để biết khách cũ/mới và
doanh số mua sau sự kiện.
"""
import json
import re
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request

from . import db
from .main_auth import current_user, is_mgr, team_names

router = APIRouter(prefix="/api")

DEPTS = {"BGD": "Sếp / BGĐ", "SALE": "Sale", "MKT": "Marketing", "KT": "Kế toán", "KHO": "Kho / Thu mua", "HCNS": "HCNS"}
STEPS = ["s0", "s1", "s2", "s3", "s4", "s5", "sx"]
STATUS = ["todo", "doing", "done"]
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


def _now():
    return datetime.now().isoformat(timespec="seconds")


def _who(user):
    return user.get("display_name") or user.get("username") or ""


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


@router.get("/events")
def events_list(user=Depends(current_user)):
    rows = db.rows("SELECT * FROM events ORDER BY COALESCE(NULLIF(date,''),'9999') DESC, id DESC")
    for e in rows:
        t = db.one("SELECT COUNT(*) n, SUM(status='done') d FROM event_tasks WHERE event_id=? AND custom>=0", (e["id"],))
        e["tasks"], e["done"] = t["n"], t["d"] or 0
        e["customers"] = db.one("SELECT COUNT(*) n FROM event_customers WHERE event_id=?", (e["id"],))["n"]
    return rows


@router.post("/events")
async def events_save(request: Request, user=Depends(current_user)):
    b = await request.json()
    eid = b.get("id")
    with db.cursor() as con:
        if eid and b.get("delete"):
            if user.get("role") != "admin":
                raise HTTPException(403, "Chỉ quản trị mới xoá được sự kiện")
            for t in ("event_tasks", "event_customers"):
                con.execute(f"DELETE FROM {t} WHERE event_id=?", (eid,))
            con.execute("DELETE FROM events WHERE id=?", (eid,))
            return {"ok": True}
        if eid and "phase" in b and len(b) <= 2:
            con.execute("UPDATE events SET phase=? WHERE id=?", (max(0, min(5, int(b["phase"]))), eid))
            return {"ok": True}
        if not is_mgr(user) and user.get("role") != "lead":
            raise HTTPException(403, "Chỉ quản lý / trưởng nhóm được tạo hoặc sửa sự kiện")
        name = (b.get("name") or "").strip()
        if not name:
            raise HTTPException(400, "Nhập tên sự kiện")
        vals = (name, (b.get("date") or "")[:10], (b.get("place") or "").strip(), (b.get("hashtag") or "").strip())
        if eid:
            con.execute("UPDATE events SET name=?, date=?, place=?, hashtag=? WHERE id=?", vals + (eid,))
        else:
            cur = con.execute("INSERT INTO events(name,date,place,hashtag,phase,created,created_by) VALUES(?,?,?,?,?,?,?)",
                              vals + (int(b.get("phase") or 0), _now(), _who(user)))
            eid = cur.lastrowid
            con.executemany("INSERT INTO event_tasks(event_id,tid,phase,dept,text,off,custom,status) VALUES(?,?,?,?,?,?,0,'todo')",
                            [(eid, t, p, d, x, o) for t, p, d, x, o in TEMPLATE])
    return {"ok": True, "id": eid}


@router.get("/events/{eid}")
def event_detail(eid: int, user=Depends(current_user)):
    e = _event(eid)
    tasks = db.rows("SELECT * FROM event_tasks WHERE event_id=? AND custom>=0 ORDER BY phase, off, id", (eid,))
    cust = [c for c in db.rows("SELECT * FROM event_customers WHERE event_id=? ORDER BY updated DESC, id DESC", (eid,))
            if _visible(user, c["sale"])]
    # doanh số mua trên KiotViet từ ngày sự kiện (khách đã nối được theo SĐT)
    ids = sorted({c["customer_id"] for c in cust if c["customer_id"]})
    after = {}
    if ids and e["date"]:
        q = ",".join("?" * len(ids))
        for r in db.rows(f"""SELECT customer_id cid, SUM(COALESCE(net_total,total)) rev, COUNT(*) n, MAX(purchase_date) last
                             FROM invoices WHERE status=1 AND customer_id IN ({q}) AND purchase_date>=? GROUP BY 1""", (*ids, e["date"])):
            after[r["cid"]] = r
    kv = {}
    if ids:
        q = ",".join("?" * len(ids))
        kv = {r["id"]: r for r in db.rows(f"SELECT id, code, name FROM customers WHERE id IN ({q})", tuple(ids))}
    for c in cust:
        c["history"] = json.loads(c["history"] or "[]")
        a = after.get(c["customer_id"]) or {}
        c["rev_after"], c["inv_after"], c["last_buy"] = a.get("rev") or 0, a.get("n") or 0, a.get("last")
        k = kv.get(c["customer_id"])
        c["kv_code"], c["kv_name"] = (k["code"], k["name"]) if k else (None, None)
    return {"event": e, "tasks": tasks, "customers": cust, "depts": DEPTS, "mgr": is_mgr(user)}


@router.post("/events/{eid}/task")
async def event_task(eid: int, request: Request, user=Depends(current_user)):
    _event(eid)
    b = await request.json()
    tid = b.get("id")
    with db.cursor() as con:
        if tid and b.get("delete"):
            con.execute("UPDATE event_tasks SET custom=-1 WHERE id=? AND event_id=?", (tid, eid))
            return {"ok": True}
        if not tid:
            text = (b.get("text") or "").strip()
            if not text or b.get("dept") not in DEPTS:
                raise HTTPException(400, "Nhập nội dung việc và chọn phòng ban")
            cur = con.execute("""INSERT INTO event_tasks(event_id,tid,phase,dept,text,off,custom,status,owner,note,updated,updated_by)
                                 VALUES(?,?,?,?,?,?,1,?,?,?,?,?)""",
                              (eid, "c", int(b.get("phase") or 0), b["dept"], text, int(b.get("off") or 0),
                               b.get("status") if b.get("status") in STATUS else "todo", (b.get("owner") or "").strip(),
                               (b.get("note") or "").strip(), _now(), _who(user)))
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
        if sets:
            con.execute(f"UPDATE event_tasks SET {', '.join(sets)}, updated=?, updated_by=? WHERE id=? AND event_id=?",
                        (*vals, _now(), _who(user), tid, eid))
    return {"ok": True}


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
