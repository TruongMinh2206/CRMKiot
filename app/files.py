"""Ảnh / hoá đơn người dùng tải lên (quà sinh nhật…). Lưu trong Postgres (cùng nơi sao lưu) để không mất khi
máy chủ khởi động lại; chạy thử trên máy không có Postgres thì lưu vào thư mục data/files. Chỉ người đã đăng nhập
mới tải lên và xem được; tên file là chuỗi ngẫu nhiên không đoán được."""
import base64
import logging
import re
import secrets
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response

from . import backup, config
from .main_auth import current_user

log = logging.getLogger("files")
router = APIRouter(prefix="/api")
TYPES = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "application/pdf": "pdf"}
MAGIC = {"image/jpeg": (b"\xff\xd8\xff",), "image/png": (b"\x89PNG",), "image/webp": (b"RIFF",), "application/pdf": (b"%PDF",)}
MAX_BYTES = 4 * 1024 * 1024
DIR = Path(config.DB_PATH).parent / "files"          # bản lưu tạm trên máy chủ (đọc nhanh, mất thì lấy lại từ Postgres)
_ready = False


def _pg():
    global _ready
    con = backup._pg()
    if not _ready:
        cur = con.cursor()
        cur.execute("""CREATE TABLE IF NOT EXISTS crm_files (
            id TEXT PRIMARY KEY, mime TEXT, bytes INT, name TEXT, owner TEXT, data BYTEA NOT NULL,
            created TIMESTAMPTZ DEFAULT now())""")
        con.commit()
        _ready = True
    return con


def _path(fid):
    return DIR / fid


@router.post("/files")
async def upload(request: Request, user=Depends(current_user)):
    b = await request.json()
    mime = b.get("mime")
    if mime not in TYPES:
        raise HTTPException(400, "Chỉ nhận ảnh JPG, PNG, WEBP hoặc file PDF")
    try:
        data = base64.b64decode(b.get("data") or "", validate=True)
    except Exception:
        raise HTTPException(400, "File tải lên không hợp lệ")
    if not data or len(data) > MAX_BYTES:
        raise HTTPException(400, "File trống hoặc lớn hơn 4 MB")
    if not data.startswith(MAGIC[mime]):
        raise HTTPException(400, "Nội dung file không đúng với loại file")
    fid = secrets.token_urlsafe(18) + "." + TYPES[mime]
    name = re.sub(r"[^\w.\- ]", "_", str(b.get("name") or ""))[:120]
    if backup.enabled():
        import psycopg2
        con = _pg()
        try:
            con.cursor().execute("INSERT INTO crm_files(id,mime,bytes,name,owner,data) VALUES(%s,%s,%s,%s,%s,%s)",
                                 (fid, mime, len(data), name, user["username"], psycopg2.Binary(data)))
            con.commit()
        finally:
            con.close()
    DIR.mkdir(parents=True, exist_ok=True)
    _path(fid).write_bytes(data)
    return {"id": fid, "url": f"/api/files/{fid}", "bytes": len(data)}


@router.get("/files/{fid}")
def download(fid: str, user=Depends(current_user)):
    m = re.fullmatch(r"[A-Za-z0-9_-]{20,40}\.(jpg|png|webp|pdf)", fid)
    if not m:
        raise HTTPException(404)
    mime = next(k for k, v in TYPES.items() if v == m.group(1))
    p = _path(fid)
    if p.exists():
        data = p.read_bytes()
    elif backup.enabled():
        con = _pg()
        try:
            cur = con.cursor()
            cur.execute("SELECT data FROM crm_files WHERE id=%s", (fid,))
            row = cur.fetchone()
        finally:
            con.close()
        if not row:
            raise HTTPException(404)
        data = bytes(row[0])
        try:
            DIR.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
        except OSError:
            pass
    else:
        raise HTTPException(404)
    return Response(data, media_type=mime, headers={"Cache-Control": "private, max-age=86400", "X-Content-Type-Options": "nosniff",
                                                     "Content-Disposition": "inline"})
