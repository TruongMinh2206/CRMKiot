import hashlib
import hmac
import json
import re
import secrets
from fastapi import Request, HTTPException
from itsdangerous import URLSafeSerializer, BadSignature
from . import config

signer = URLSafeSerializer(config.SECRET_KEY, salt="session")
COOKIE = "crm_session"

# ---------------------------------------------------------------------------
# Nhóm quyền: mỗi nhóm có danh sách trang được xem, phạm vi dữ liệu theo sale và quyền thao tác.
# Quản trị chỉnh trong Cài đặt → Phân quyền. Quyền đọc lại từ DB mỗi lần gọi API, nên đổi quyền /
# khoá tài khoản có hiệu lực ngay, không phải chờ người dùng đăng nhập lại.
# ---------------------------------------------------------------------------
PAGES = ["overview", "board", "tasks", "kpi", "campaign", "events", "leads", "sales", "trend", "customers",
         "debts", "products", "journey", "birthdays", "settings"]          # khớp PAGES ở giao diện; "settings" chỉ Quản trị
PERMS = {"manage": "Quản lý dữ liệu: đặt KPI, gán sale, xếp team, khách sếp, sửa thưởng chiến dịch",
         "event_edit": "Điều phối sự kiện: tạo / sửa, chuyển bước, ngân sách, cổng duyệt",
         "export": "Xuất Excel",
         "sync": "Đồng bộ và kiểm tra dữ liệu KiotViet"}
SCOPES = {"all": "Tất cả", "team": "Team của mình", "own": "Chỉ của mình"}
_ALL = [p for p in PAGES if p != "settings"]
DEFAULT_ROLES = [   # (mã, tên, trang, phạm vi dữ liệu, quyền thao tác)
    ("admin", "Quản trị", PAGES, "all", list(PERMS)),
    ("bgd", "Ban giám đốc", _ALL, "all", ["manage", "event_edit", "export"]),
    ("accountant", "Kế toán", _ALL, "all", ["manage", "event_edit", "export", "sync"]),
    ("lead", "Trưởng nhóm sale", _ALL, "team", ["event_edit", "export"]),
    ("sale", "Sale", _ALL, "own", ["export"]),
    ("mkt", "Marketing", ["events", "leads", "campaign", "trend", "products"], "all", ["export"]),
    ("kho", "Kho / Thu mua", ["events", "products", "journey"], "all", ["export"]),
    ("hcns", "Hành chính – Nhân sự", ["events", "kpi", "board"], "all", ["export"]),
]
ROLE_DEPT = {"admin": "BGD", "bgd": "BGD", "accountant": "KT", "lead": "SALE", "sale": "SALE", "mkt": "MKT", "kho": "KHO", "hcns": "HCNS"}
# API → trang dùng nó; người không có trang nào trong danh sách bị chặn (403). API dùng chung (khách, sale, trạng thái) không chặn.
API_PAGES = (("/api/overview", "overview"), ("/api/alerts", "overview"), ("/api/report/sales", "overview sales"),
             ("/api/report/trend", "trend"), ("/api/report/customers", "customers"), ("/api/report/products", "products"),
             ("/api/report/journey", "journey"), ("/api/report/birthdays", "birthdays"), ("/api/board", "board"),
             ("/api/tasks", "tasks debts leads customers"), ("/api/kpi", "kpi"), ("/api/campaign", "campaign"),
             ("/api/events", "events"), ("/api/leads", "leads"), ("/api/debts", "debts"))
EXPORT_PAGES = {"sales": "sales", "trend": "trend", "trend_list": "trend", "customers": "customers", "products": "products",
                "journey": "journey", "campaign": "campaign", "debts": "debts", "board": "board", "birthdays": "birthdays"}
PBKDF2_ROUNDS = 120_000


def _hash(p):
    """Kiểu băm cũ — chỉ còn dùng để nhận mật khẩu tạo trước bản này (tự băm lại khi đăng nhập)."""
    return hashlib.sha256((config.SECRET_KEY + p).encode()).hexdigest()


def hash_pw(p):
    salt = secrets.token_hex(16)
    return f"pbkdf2${PBKDF2_ROUNDS}${salt}${hashlib.pbkdf2_hmac('sha256', p.encode(), salt.encode(), PBKDF2_ROUNDS).hex()}"


def check_pw(p, stored):
    """(đúng mật khẩu?, cần băm lại theo kiểu mới?)"""
    if not stored:
        return False, False
    if stored.startswith("pbkdf2$"):
        _, rounds, salt, h = stored.split("$", 3)
        return hmac.compare_digest(hashlib.pbkdf2_hmac("sha256", p.encode(), salt.encode(), int(rounds)).hex(), h), False
    ok = hmac.compare_digest(_hash(p), stored)
    return ok, ok


def env_ver():
    """Đổi ADMIN_PASSWORD trong .env thì mọi phiên của tài khoản quản trị gốc hết hiệu lực."""
    return hashlib.sha256((config.SECRET_KEY + "|" + config.ADMIN_PASSWORD).encode()).hexdigest()[:16]


def seed_roles():
    """Tạo các nhóm quyền mặc định nếu chưa có (không ghi đè chỉnh sửa của quản trị)."""
    from . import db
    with db.cursor() as con:
        for i, (code, name, pages, scope_, perms) in enumerate(DEFAULT_ROLES):
            con.execute("INSERT OR IGNORE INTO roles(code,name,pages,scope,perms,builtin,position) VALUES(?,?,?,?,?,1,?)",
                        (code, name, json.dumps(pages), scope_, json.dumps(perms), i))
        con.execute("UPDATE roles SET pages=?, scope='all', perms=? WHERE code='admin'", (json.dumps(PAGES), json.dumps(list(PERMS))))


def roles_list():
    from . import db
    out = db.rows("""SELECT r.*, (SELECT COUNT(*) FROM crm_users u WHERE u.role=r.code) users
                     FROM roles r ORDER BY r.position, r.name""")
    for r in out:
        r["pages"], r["perms"] = json.loads(r["pages"] or "[]"), json.loads(r["perms"] or "[]")
    return out


def slug(s):
    import unicodedata
    s = unicodedata.normalize("NFD", s or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "_", s).strip("_")[:30] or "nhom"


def resolve(username, env=False):
    """Người dùng kèm quyền hiện hành, hoặc None nếu tài khoản không còn / đã khoá."""
    from . import db
    if env:
        return {"username": config.ADMIN_USER, "display_name": "Quản trị", "role": "admin", "role_name": "Quản trị",
                "sale_name": None, "dept": "BGD", "pages": PAGES, "scope": "all", "perms": list(PERMS), "env": True, "ver": env_ver()}
    u = db.one("SELECT * FROM crm_users WHERE username=?", (username,))
    if not u or u.get("active") == 0:
        return None
    r = db.one("SELECT * FROM roles WHERE code=?", (u["role"],)) or {}
    admin = u["role"] == "admin"
    return {"username": u["username"], "display_name": u["display_name"] or u["username"], "role": u["role"],
            "role_name": r.get("name") or u["role"], "sale_name": u["sale_name"], "dept": u.get("dept") or "",
            "pages": PAGES if admin else [p for p in json.loads(r.get("pages") or "[]") if p in _ALL],
            "scope": "all" if admin else (r.get("scope") if r.get("scope") in SCOPES else "own"),
            "perms": list(PERMS) if admin else [p for p in json.loads(r.get("perms") or "[]") if p in PERMS],
            "env": False, "ver": u.get("ver") or 0}


def session_cookie(user):
    return signer.dumps({"username": user["username"], "env": 1 if user.get("env") else 0, "ver": user["ver"]})


def current_user(request: Request):
    cached = getattr(request.state, "user", None)
    if cached:
        return cached
    tok = request.cookies.get(COOKIE)
    if not tok:
        raise HTTPException(401, "Chưa đăng nhập")
    try:
        d = signer.loads(tok)
    except BadSignature:
        raise HTTPException(401, "Phiên không hợp lệ")
    name = d.get("username")
    legacy = "ver" not in d                 # cookie của bản trước: chứa sẵn cả quyền, giờ chỉ dùng tên đăng nhập
    env = name == config.ADMIN_USER and (bool(d.get("env")) if not legacy else d.get("role") == "admin")
    user = resolve(name, env)
    if not user or (not legacy and d["ver"] != user["ver"]):
        raise HTTPException(401, "Phiên đăng nhập đã hết hiệu lực, vui lòng đăng nhập lại")
    request.state.user = user
    return user


def pages_for(path):
    """Các trang được phép gọi API này (None = API dùng chung, không chặn theo trang)."""
    if path.startswith("/api/export/"):
        return {EXPORT_PAGES.get(path[len("/api/export/"):].split("/")[0], "settings")}
    for prefix, pages in API_PAGES:
        if path == prefix or path.startswith(prefix + "/"):
            return set(pages.split())
    return None


# ---------------------------------------------------------------------------
# Phạm vi dữ liệu theo sale
#   all  : xem tất cả (có thể lọc)
#   team : xem mọi người cùng team với mình (bảng Xếp team trong Cài đặt)
#   own  : chỉ xem của mình
# ---------------------------------------------------------------------------
def is_admin(user):
    return user.get("role") == "admin"


def can(user, perm):
    return is_admin(user) or perm in (user.get("perms") or [])


def is_mgr(user):
    """Quyền quản lý dữ liệu (trước đây: quản trị + kế toán)."""
    return can(user, "manage")


def team_names(user):
    """Tên các sale mà tài khoản này được xem. None = tất cả."""
    from . import db
    s = user.get("scope")
    if s is None:   # người dùng chưa nạp quyền (không xảy ra qua current_user) — giữ cách tính cũ
        s = "all" if user.get("role") in ("admin", "accountant") else "team" if user.get("role") == "lead" else "own"
    if s == "all":
        return None
    me = user.get("sale_name") or "__none__"
    if s != "team":
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
