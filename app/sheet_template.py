"""Nạp số liệu CRM vào đúng template sheet của kế toán.

Template "T10.2026" gần như toàn công thức; chỉ 3 vùng là ô nhập tay:

* BẢNG 2 (dòng 59-73): D mục tiêu tháng · I/L/O/R doanh số 4 tuần · G/J/M/P/S tình trạng
* BẢNG 3 (dòng 81-88): C mục tiêu tháng · 4 tuần × 5 nhóm hàng · các ô tình trạng
* BẢNG 1 (dòng 20-54): D mục tiêu tháng · ô tình trạng (phần còn lại tự tính)

Các ô khác trả về None để update_values bỏ qua, giữ nguyên công thức và định dạng gốc.
"""
from . import reports

# ---- bố cục template -----------------------------------------------------
B1_ROW1 = 20           # dòng "TỔNG TEAM ..." đầu tiên
B2_ROWS = range(59, 74)
B3_ROWS = range(81, 89)

# (dòng MỤC TIÊU của khối, dòng tương ứng ở BẢNG 3, tên người bán trong CRM)
B1_BLOCKS = [
    (21, 81, "Sales Vân Anh"),
    (25, 85, "Sales My"),
    (29, 88, "NV Thử Việc"),
    (34, 82, "Sales Trần Phương Linh"),
    (38, 84, "Sales Huyền"),
    (43, 83, "Sales Linh Bé"),
    (47, 87, "Sales Ngát"),
    (51, 86, "Sales Thu Hằng"),
]

# BẢNG 3: dòng -> người bán trong CRM
B3_SALES = [
    (81, "Sales Vân Anh"),
    (82, "Sales Trần Phương Linh"),
    (83, "Sales Linh Bé"),
    (84, "Sales Huyền"),
    (85, "Sales My"),
    (86, "Sales Thu Hằng"),
    (87, "Sales Ngát"),
    (88, "NV Thử Việc"),
]

# BẢNG 2: dòng -> mã khách trên KiotViet
B2_CUSTOMERS = [
    (59, "KH002227"),   # TOÁN AUTH
    (60, "KH002165"),   # TRÀ GIANG
    (61, "KH005613"),   # CHU THỊ LỆ
    (62, "KH001680"),   # LINH NẤM
    (63, "KH003915"),   # LAN DƯỢC
    (64, "KH000716"),   # HINHY
    (65, "KH005797"),   # TRÚC SÀI GÒN
    (66, "KH003858"),   # PHẠM HÙNG
    (67, "KH003164"),   # NAM PHONG
    (68, "KH003160"),   # AHIHI
    (69, "KH001744"),   # HUẾ FLORA
    (70, "KH005939"),   # VÂN ANH CÂY RƠM
    (71, "KH003548"),   # HÒA AN
    (72, "KH003886"),   # HÀ LINH LONG BIÊN (K áp KPI)
    (73, "KH004609"),   # NGỌC COCOLUX (K áp KPI)
]

# BẢNG 3: cột đầu của 5 khối nhóm hàng (0-based, so với cột A)
CAT_COL0 = {"normal": 6, "excl_new": 19, "excl_old": 32, "slow": 45, "new_customer": 58}
CAT_ORDER = ["normal", "excl_new", "excl_old", "slow", "new_customer"]


def _col(n):
    """0-based -> tên cột A1 (0 -> A, 26 -> AA)."""
    s = ""
    n += 1
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def _st(target, actual):
    """Công thức TÌNH TRẠNG. N() = 0 khi ô mục tiêu là chữ ("K áp KPI") hoặc trống.

    Template dùng dấu ; ngăn tham số (locale VN), nên giữ đúng dấu đó.
    """
    return f'=IF(N({target})=0;"";IF({actual}>={target};"ĐẠT";"K ĐẠT"))'


def blocks(d):
    """Trả về [(vùng A1, mảng 2 chiều)] để ghi đè lên bản sao template."""
    weeks = [w["week"] for w in d["weeks"]]
    tg = {s: (d["targets"].get(s, {}) or {}).get("revenue_target") or 0 for s in d["sellers"]}
    out = []

    # ---------------- BẢNG 2 ----------------
    by_code = {b["code"]: b for b in d["boss"].values()}
    rows = []
    for r, code in B2_CUSTOMERS:
        b = by_code.get(code)
        wk = [(b["weeks"].get(w, 0) if b else 0) for w in weeks]
        rows.append([
            None, None, None, _st(f"D{r}", f"E{r}"),                 # D E F G
            None, wk[0], _st(f"$H{r}", f"I{r}"), None,               # H I J K
            wk[1], _st(f"$H{r}", f"L{r}"), None,                     # L M N
            wk[2], _st(f"$H{r}", f"O{r}"), None,                     # O P Q
            wk[3], _st(f"$H{r}", f"R{r}"), None,                     # R S T
        ])
    out.append((f"D{B2_ROWS[0]}:T{B2_ROWS[-1]}", rows))

    # ---------------- BẢNG 3 ----------------
    width = CAT_COL0["new_customer"] + 13  # tới hết khối cuối
    rows = []
    for r, s in B3_SALES:
        line = [None] * (width - 2)        # bắt đầu từ cột C
        line[0] = tg.get(s, 0) or None     # C: mục tiêu tháng
        line[2] = _st(f"C{r}", f"D{r}")    # E: tình trạng tháng
        for k in CAT_ORDER:
            c0 = CAT_COL0[k]               # cột mục tiêu tuần của nhóm (0-based)
            for i, w in enumerate(weeks):
                v = d["cat_week"].get(s, {}).get(k, {}).get(w, 0)
                line[c0 - 2 + 1 + i * 3] = v                                   # doanh số tuần
                line[c0 - 2 + 2 + i * 3] = _st(f"${_col(c0)}{r}",
                                               f"{_col(c0 + 1 + i * 3)}{r}")   # tình trạng tuần
        rows.append(line)
    out.append((f"C{B3_ROWS[0]}:{_col(width - 1)}{B3_ROWS[-1]}", rows))

    # ---------------- BẢNG 1 ----------------
    # D: mục tiêu tháng; dòng THỰC TẾ: trỏ lại đúng dòng của người đó ở BẢNG 3
    rows = [[None] * 9 for _ in range(54 - B1_ROW1 + 1)]   # D..L, dòng 20..54
    for r0, b3, s in B1_BLOCKS:
        i = r0 - B1_ROW1
        rows[i][0] = tg.get(s, 0) or None
        f = rows[i + 1]
        f[2] = f"=H{b3}+K{b3}+N{b3}+Q{b3}"
        f[3] = f"=U{b3}+X{b3}+AA{b3}+AD{b3}"
        f[4] = f"=AH{b3}+AK{b3}+AN{b3}+AQ{b3}"
        f[5] = f"=AU{b3}+AX{b3}+BA{b3}+BD{b3}"
        f[6] = f"=BH{b3}+BK{b3}+BN{b3}+BQ{b3}"
        for c in range(1, 8):              # E..K: tình trạng so mục tiêu với thực tế
            col = _col(3 + c)
            rows[i + 3][c] = _st(f"{col}{r0}", f"{col}{r0 + 1}")
    out.append((f"D{B1_ROW1}:L54", rows))
    return out
