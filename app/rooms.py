"""Đặt lịch phòng họp — công cụ nội bộ trong tab Công cụ ngoại vi.

Mọi tài khoản xem lịch và đặt phòng; trùng giờ cùng phòng thì chặn và báo ai đã đặt.
Người đặt (hoặc Quản trị) sửa / huỷ; Quản trị thêm / sửa danh sách phòng.
"""
import uuid
from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request

from . import db
from .main_auth import current_user, is_admin
from .work import now

router = APIRouter(prefix="/api/rooms")
COLORS = ["#1f63e0", "#0f9d58", "#f76b15", "#8e4ec6", "#d6409f", "#12a594"]


def seed():
    with db.cursor() as con:
        if not con.execute("SELECT COUNT(*) FROM rooms").fetchone()[0]:
            con.execute("INSERT INTO rooms(name,capacity,note,color,active,position) VALUES('Phòng họp',NULL,'',?,1,0)", (COLORS[0],))


def _dm(s):
    return f"{s[8:10]}/{s[5:7]}"


def _hm(s, label):
    try:
        return datetime.strptime((s or "").strip(), "%H:%M").strftime("%H:%M")
    except ValueError:
        raise HTTPException(400, f"Giờ {label} chưa đúng (dạng 09:30)")


@router.get("")
def rooms_view(from_d: str = None, to_d: str = None, user=Depends(current_user)):
    t = now()
    d1 = date.fromisoformat(from_d) if from_d else t.date()
    d2 = date.fromisoformat(to_d) if to_d else d1 + timedelta(days=6)
    if (d2 - d1).days > 62:
        raise HTTPException(400, "Xem tối đa 2 tháng một lần")
    rooms = db.rows("SELECT * FROM rooms ORDER BY active DESC, position, id")
    books = db.rows("""SELECT b.*, r.name room_name FROM room_bookings b JOIN rooms r ON r.id=b.room_id
                       WHERE b.status='ok' AND b.start_at < ? AND b.end_at > ? ORDER BY b.start_at, r.position, r.id""",
                    ((d2 + timedelta(days=1)).isoformat(), d1.isoformat()))
    for b in books:
        b["mine"] = b["booked_by"] == user["username"]
        b["can_edit"] = b["mine"] or is_admin(user)
    # Tình trạng hiện tại của từng phòng (luôn tính theo hôm nay, không phụ thuộc tuần đang xem)
    today = db.rows("SELECT room_id, title, booked_name, start_at, end_at FROM room_bookings WHERE status='ok' AND start_at < ? AND end_at > ? ORDER BY start_at",
                    ((t.date() + timedelta(days=1)).isoformat(), t.date().isoformat()))
    nowiso = t.strftime("%Y-%m-%dT%H:%M")
    for r in rooms:
        mine = [b for b in today if b["room_id"] == r["id"]]
        r["busy"] = next((b for b in mine if b["start_at"] <= nowiso < b["end_at"]), None)
        r["next"] = next((b for b in mine if b["start_at"] > nowiso), None)
    return {"rooms": rooms, "bookings": books, "now": nowiso, "admin": is_admin(user), "colors": COLORS}


@router.post("/book")
async def book(request: Request, user=Depends(current_user)):
    """Đặt / sửa / huỷ. repeat = số tuần lặp (1–12). Huỷ có scope='series' để huỷ cả các buổi sau của chuỗi lặp."""
    b = await request.json()
    t = now()
    with db.cursor() as con:
        old = None
        if b.get("id"):
            old = con.execute("SELECT * FROM room_bookings WHERE id=? AND status='ok'", (b["id"],)).fetchone()
            if not old:
                raise HTTPException(404, "Lịch này không còn (có thể đã bị huỷ)")
            old = dict(old)
            if old["booked_by"] != user["username"] and not is_admin(user):
                raise HTTPException(403, f"Lịch do {old['booked_name']} đặt — chỉ người đặt hoặc quản trị sửa / huỷ được")
            if b.get("cancel"):
                if b.get("scope") == "series" and old["series"]:
                    n = con.execute("UPDATE room_bookings SET status='cancelled', updated=? WHERE series=? AND status='ok' AND start_at>=?",
                                    (t.isoformat(timespec="minutes"), old["series"], old["start_at"])).rowcount
                else:
                    n = con.execute("UPDATE room_bookings SET status='cancelled', updated=? WHERE id=?", (t.isoformat(timespec="minutes"), old["id"])).rowcount
                return {"ok": True, "cancelled": n}
        title = (b.get("title") or "").strip()
        if not title:
            raise HTTPException(400, "Nhập nội dung cuộc họp")
        room = con.execute("SELECT * FROM rooms WHERE id=? AND active=1", (b.get("room_id"),)).fetchone()
        if not room:
            raise HTTPException(400, "Chọn phòng")
        try:
            day = date.fromisoformat(b.get("date") or "")
        except ValueError:
            raise HTTPException(400, "Chọn ngày họp")
        s, e = _hm(b.get("start"), "bắt đầu"), _hm(b.get("end"), "kết thúc")
        if e <= s:
            raise HTTPException(400, "Giờ kết thúc phải sau giờ bắt đầu")
        if not old and f"{day.isoformat()}T{e}" <= t.strftime("%Y-%m-%dT%H:%M"):
            raise HTTPException(400, "Không đặt được lịch đã qua")
        weeks = 1 if old else max(1, min(12, int(b.get("repeat") or 1)))
        slots = [(day + timedelta(weeks=i)).isoformat() for i in range(weeks)]
        clash = []
        for d in slots:
            st, en = f"{d}T{s}", f"{d}T{e}"
            for c in con.execute("SELECT * FROM room_bookings WHERE room_id=? AND status='ok' AND id<>? AND start_at<? AND end_at>?",
                                 (room["id"], old["id"] if old else 0, en, st)):
                clash.append(f"{_dm(d)}: “{c['title']}” {c['start_at'][11:]}–{c['end_at'][11:]} ({c['booked_name']})")
        if clash:
            raise HTTPException(409, f"{room['name']} đã có lịch trùng giờ — " + "; ".join(clash[:4]) + (f" và {len(clash) - 4} buổi khác" if len(clash) > 4 else ""))
        vals = dict(room_id=room["id"], title=title, attendees=(b.get("attendees") or "").strip(), note=(b.get("note") or "").strip(),
                    updated=t.isoformat(timespec="minutes"))
        if old:
            con.execute("UPDATE room_bookings SET room_id=?, title=?, attendees=?, note=?, updated=?, start_at=?, end_at=? WHERE id=?",
                        tuple(vals.values()) + (f"{slots[0]}T{s}", f"{slots[0]}T{e}", old["id"]))
            return {"ok": True, "id": old["id"]}
        series = uuid.uuid4().hex[:12] if weeks > 1 else None
        ids = []
        for d in slots:
            ids.append(con.execute("""INSERT INTO room_bookings(room_id,title,start_at,end_at,booked_by,booked_name,dept,attendees,note,series,status,created,updated)
                                      VALUES(?,?,?,?,?,?,?,?,?,?, 'ok', ?, ?)""",
                                   (room["id"], title, f"{d}T{s}", f"{d}T{e}", user["username"], user["display_name"], user.get("dept") or "",
                                    vals["attendees"], vals["note"], series, vals["updated"], vals["updated"])).lastrowid)
        return {"ok": True, "ids": ids}


@router.post("")
async def rooms_save(request: Request, user=Depends(current_user)):
    if not is_admin(user):
        raise HTTPException(403, "Chỉ quản trị thêm / sửa phòng")
    b = await request.json()
    with db.cursor() as con:
        if b.get("id") and b.get("delete"):
            n = con.execute("SELECT COUNT(*) FROM room_bookings WHERE room_id=? AND status='ok' AND end_at>?",
                            (b["id"], now().strftime("%Y-%m-%dT%H:%M"))).fetchone()[0]
            if n:
                raise HTTPException(409, f"Phòng còn {n} lịch sắp tới — huỷ các lịch đó hoặc tắt phòng thay vì xoá")
            con.execute("DELETE FROM rooms WHERE id=?", (b["id"],))
            return {"ok": True}
        name = (b.get("name") or "").strip()
        if not name:
            raise HTTPException(400, "Nhập tên phòng")
        try:
            cap = int(b["capacity"]) if str(b.get("capacity") or "").strip() else None
        except ValueError:
            raise HTTPException(400, "Sức chứa là số người")
        color = b.get("color") if b.get("color") in COLORS else COLORS[0]
        vals = (name, cap, (b.get("note") or "").strip(), color, 1 if b.get("active", True) else 0, int(b.get("position") or 0))
        if b.get("id"):
            con.execute("UPDATE rooms SET name=?, capacity=?, note=?, color=?, active=?, position=? WHERE id=?", vals + (b["id"],))
            return {"ok": True, "id": b["id"]}
        return {"ok": True, "id": con.execute("INSERT INTO rooms(name,capacity,note,color,active,position) VALUES(?,?,?,?,?,?)", vals).lastrowid}
