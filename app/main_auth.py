import hashlib
from fastapi import Request, HTTPException
from itsdangerous import URLSafeSerializer, BadSignature
from . import config

signer = URLSafeSerializer(config.SECRET_KEY, salt="session")
COOKIE = "crm_session"


def _hash(p):
    return hashlib.sha256((config.SECRET_KEY + p).encode()).hexdigest()


def current_user(request: Request):
    tok = request.cookies.get(COOKIE)
    if not tok:
        raise HTTPException(401, "Chưa đăng nhập")
    try:
        return signer.loads(tok)
    except BadSignature:
        raise HTTPException(401, "Phiên không hợp lệ")


# ---------------------------------------------------------------------------
# Phân quyền xem dữ liệu
#   admin, accountant : xem tất cả
#   lead (trưởng nhóm): xem mọi người cùng team với mình (bảng Xếp team trong Cài đặt)
#   sale              : chỉ xem của mình
# ---------------------------------------------------------------------------
def is_mgr(user):
    return user.get("role") in ("admin", "accountant")


def team_names(user):
    """Tên các sale mà tài khoản này được xem. None = tất cả."""
    from . import db
    role = user.get("role")
    if role in ("admin", "accountant"):
        return None
    me = user.get("sale_name") or "__none__"
    if role != "lead":
        return [me]
    t = db.one("SELECT team FROM teams WHERE sale=?", (me,))
    if not t or not (t["team"] or "").strip():
        return [me]
    names = [r["sale"] for r in db.rows("SELECT sale FROM teams WHERE team=? ORDER BY sale", (t["team"],))]
    return names if me in names else names + [me]


def scope(user, sale=None):
    """Bộ lọc sale áp cho báo cáo: None (tất cả) | 'tên' | [danh sách tên]."""
    names = team_names(user)
    if names is None:
        return sale or None
    if len(names) == 1:
        return names[0]
    return sale if sale and sale in names else names


def allowed(user, sale_value):
    names = team_names(user)
    return names is None or sale_value in names


def col_in(col, s):
    """Điều kiện SQL cho một cột theo kết quả của scope()."""
    if not s:
        return "", ()
    if isinstance(s, (list, tuple)):
        return f" AND {col} IN ({','.join('?' * len(s))})", tuple(s)
    return f" AND {col}=?", (s,)
