"""Công cụ ngoại vi: các công cụ nhỏ chạy ở nơi khác (GitHub Pages, Apps Script, PythonAnywhere…)
gom về một tab trong CRM. Mọi tài khoản đều xem và dùng được; chỉ Quản trị thêm / sửa danh sách.
Công cụ cho phép nhúng thì mở ngay trong CRM, công cụ chặn nhúng thì mở ở tab mới.
"""
import re
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request

from . import db
from .main_auth import current_user, is_admin

router = APIRouter(prefix="/api")

DEFAULT_TOOLS = [   # (tên, mô tả, link, mở trong CRM, nhóm)
    ("Sáng kiến & Cảm ơn", "Gửi sáng kiến, lời cảm ơn và thư góp ý trong công ty",
     "https://lbglobal.pythonanywhere.com/", 0, "Nội bộ"),
    ("Ghép thư mời", "Tạo thư mời tự động: ghép tên, ảnh khách vào mẫu thư mời LB Global",
     "https://truongminh2206.github.io/thumoi-lbglobal/", 1, "Sự kiện"),
    ("Form khách đăng ký RCD 2026", "Khách đăng ký nhận tư vấn tại booth; dữ liệu đổ về Google Sheet thống kê sự kiện",
     "https://truongminh2206.github.io/RCD2026/", 1, "Sự kiện"),
    ("Tìm ảnh bằng AI", "Tìm ảnh của một khách trong kho ảnh sự kiện bằng AI",
     "https://findimageai.lbglobal-ba.workers.dev/", 1, "Sự kiện"),
    ("Ảnh sang link", "Tải ảnh lên, lấy link và công thức chèn ảnh cho Excel / Google Sheet",
     "https://truongminh2206.github.io/anhsanglink/", 1, "Tiện ích"),
    ("Báo cáo kênh TikTok", "Tự tổng hợp kết quả các kênh TikTok của công ty",
     "https://script.google.com/macros/s/AKfycbxrHtzfIGo3YJaQoVsNKURwRvpIv6T4Qaa1oz1_DyXL64Ggsxol4Wfj3YNKqPJdkkzO/exec", 0, "Marketing"),
]


def seed():
    """Lần đầu (bảng trống) nạp sẵn các công cụ đang dùng."""
    with db.cursor() as con:
        if con.execute("SELECT COUNT(*) FROM tools").fetchone()[0]:
            return
        now = datetime.now().isoformat(timespec="seconds")
        con.executemany("INSERT INTO tools(name,description,url,embed,category,position,created,updated) VALUES(?,?,?,?,?,?,?,?)",
                        [(n, d, u, e, c, i, now, now) for i, (n, d, u, e, c) in enumerate(DEFAULT_TOOLS)])


@router.get("/tools")
def tools_list(user=Depends(current_user)):
    return db.rows("SELECT * FROM tools ORDER BY position, id")


@router.post("/tools")
async def tools_save(request: Request, user=Depends(current_user)):
    if not is_admin(user):
        raise HTTPException(403, "Chỉ quản trị được thêm / sửa công cụ")
    b = await request.json()
    tid = b.get("id")
    with db.cursor() as con:
        if tid and b.get("delete"):
            con.execute("DELETE FROM tools WHERE id=?", (tid,))
            return {"ok": True}
        name, url = (b.get("name") or "").strip(), (b.get("url") or "").strip()
        if not name:
            raise HTTPException(400, "Nhập tên công cụ")
        if not re.fullmatch(r"https?://[^\s<>\"']+", url):
            raise HTTPException(400, "Link phải bắt đầu bằng https:// (hoặc http://)")
        try:
            pos = int(b.get("position") or 0)
        except (TypeError, ValueError):
            pos = 0
        vals = (name, (b.get("description") or "").strip(), url, 1 if b.get("embed") else 0,
                (b.get("category") or "").strip() or "Khác", pos, datetime.now().isoformat(timespec="seconds"), user.get("display_name") or "")
        if tid:
            con.execute("UPDATE tools SET name=?, description=?, url=?, embed=?, category=?, position=?, updated=?, updated_by=? WHERE id=?", vals + (tid,))
        else:
            tid = con.execute("INSERT INTO tools(name,description,url,embed,category,position,updated,updated_by,created) VALUES(?,?,?,?,?,?,?,?,?)",
                              vals + (vals[6],)).lastrowid
    return {"ok": True, "id": tid}
