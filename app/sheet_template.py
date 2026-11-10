"""Nạp số liệu CRM vào đúng template sheet của kế toán.

Template "T10.2026" gần như toàn công thức; chỉ 3 vùng là ô nhập tay:

* BẢNG 2 (dòng 59-73): D mục tiêu tháng · I/L/O/R doanh số 4 tuần · G/J/M/P/S tình trạng
* BẢNG 3 (dòng 81-88): C mục tiêu tháng · 4 tuần × 5 nhóm hàng · các ô tình trạng
* BẢNG 1 (dòng 20-54): D mục tiêu tháng · ô tình trạng (phần còn lại tự tính)

Các ô khác trả về None để update_values bỏ qua, giữ nguyên công thức và định dạng gốc.
"""
from . import reports

# ---- bố cục template -----------------------------------------------------
# Không gán cứng số dòng: người dùng có thể chèn/bớt dòng trong tab mẫu, nên
# bố cục được đọc thẳng từ mẫu mỗi lần xuất (xem detect_layout).

# BẢNG 2: thứ tự mã khách đúng theo thứ tự dòng trong mẫu
B2_CODES = ["KH002227", "KH002165", "KH005613", "KH001680", "KH003915",
            "KH000716", "KH005797", "KH003858", "KH003164", "KH003160",
            "KH001744", "KH005939", "KH003548", "KH003886", "KH004609"]


def detect_layout(rows):
    """Đọc bố cục từ 3 cột đầu của tab mẫu (rows[i] ứng với dòng i+1).

    Dòng team là dòng có cột B bắt đầu bằng "TỔNG TEAM"; dòng của mỗi sale là
    dòng có cột C là "MỤC TIÊU". BẢNG 2 và BẢNG 3 nhận ra qua tiêu đề ở cột A,
    dữ liệu là các dòng có số thứ tự.
    """
    def cell(i, j):
        r = rows[i] if i < len(rows) else []
        return (r[j] if j < len(r) else "") or ""

    def banner(prefix):
        for i in range(len(rows)):
            if cell(i, 0).strip().upper().startswith(prefix):
                return i
        return None

    def numbered(start):
        out = []
        for i in range(start, len(rows)):
            if cell(i, 0).strip().isdigit():
                out.append(i + 1)
            elif out:
                break
        return out

    b1, b2, b3 = banner("BẢNG 1"), banner("BẢNG 2"), banner("BẢNG 3")
    if b1 is None or b2 is None or b3 is None:
        raise LayoutError("Tab mẫu thiếu một trong các bảng: BẢNG 1, BẢNG 2, BẢNG 3.")

    team_slots, cur = [], None
    for i in range(b1, b2):
        if cell(i, 1).strip().upper().startswith("TỔNG TEAM"):
            cur = (i + 1, [])
            team_slots.append(cur)
        elif cell(i, 2).strip().upper() == "MỤC TIÊU" and cur:
            cur[1].append(i + 1)
    if not team_slots:
        raise LayoutError("Không tìm thấy dòng \"TỔNG TEAM\" nào trong tab mẫu.")

    return {"b1_row1": team_slots[0][0], "b1_last": b2,
            "team_slots": team_slots,
            "b2_rows": numbered(b2), "b3_rows": numbered(b3)}

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


class LayoutError(Exception):
    """Mẫu không đủ chỗ cho danh sách team/sale hiện tại."""


def _layout(d, L):
    """Xếp team và sale vào các ô có sẵn của mẫu, theo bảng phân team trong CRM.

    Trước đây danh sách này gán cứng nên đổi team trong CRM mà sheet không đổi.
    Giờ team nào, ai trong team đều lấy từ bảng phân team; xếp theo doanh số
    giảm dần để người bán nhiều nhất lên trên.
    """
    tot = {s: sum(sum(w.values()) for k, w in d["cat_week"].get(s, {}).items()
                  if k != "new_customer") for s in d["sellers"]}
    teams = {}
    for s in d["sellers"]:
        teams.setdefault(d["teams"].get(s) or "(Chưa xếp team)", []).append(s)
    order = sorted(teams, key=lambda t: -sum(tot.get(x, 0) for x in teams[t]))

    # các khối của mẫu rộng hẹp khác nhau (3 - 2 - 3 dòng), nên xếp team to vào
    # khối vừa nhất còn trống thay vì xếp lần lượt
    free = list(L["team_slots"])
    placed, thieu = {}, []
    for team in order:
        members = sorted(teams[team], key=lambda x: -tot.get(x, 0))
        fit = [b for b in free if len(b[1]) >= len(members)]
        if not fit:
            thieu += members
            continue
        b = min(fit, key=lambda x: len(x[1]))
        free.remove(b)
        placed[b[0]] = (team, list(zip(b[1], members)))
    assign = [(trow, *placed[trow]) for trow, _ in L["team_slots"] if trow in placed]

    seq = [s for _, _, ms in assign for _, s in ms]
    b3rows = L["b3_rows"]
    thieu += seq[len(b3rows):]
    if thieu:
        raise LayoutError(
            "Tab mẫu chỉ đủ chỗ cho %d team và %d sale, thiếu chỗ cho: %s. "
            "Thêm dòng vào tab mẫu, hoặc gộp bớt người bán trong Cài đặt."
            % (len(L["team_slots"]), len(b3rows), ", ".join(sorted(set(thieu)))))
    return assign, {s: b3rows[i] for i, s in enumerate(seq)}


def blocks(d, layout):
    """Trả về [(vùng A1, mảng 2 chiều)] để ghi đè lên bản sao template."""
    L = layout
    B1_ROW1, B1_LAST = L["b1_row1"], L["b1_last"]
    B2_ROWS, B3_ROWS = L["b2_rows"], L["b3_rows"]
    TEAM_SLOTS = L["team_slots"]
    weeks = [w["week"] for w in d["weeks"]]
    # luôn lấp đủ 4 cột tuần của mẫu; khoảng ngày ngắn thì cột thừa bị xoá trắng
    slots = (weeks + [None] * 4)[:4]
    tg = {s: (d["targets"].get(s, {}) or {}).get("revenue_target") or 0 for s in d["sellers"]}
    assign, b3 = _layout(d, L)
    b3_of = {r: s for s, r in b3.items()}
    # khách sếp của từng sale: cột K ở BẢNG 1 phải trỏ đúng dòng BẢNG 2 của
    # những khách do sale đó phụ trách, nếu không khối team đổi chỗ là lệch hết
    lead = {trow: (ms[0][0] if ms else None) for trow, _, ms in assign}
    team_of_row = {trow: team for trow, team, _ in assign}
    boss_rows = {}
    code_row = dict(zip(B2_CODES, B2_ROWS))
    for b in d["boss"].values():
        if b["no_kpi"] or b["code"] not in code_row:
            continue
        team = d["teams"].get(b["sale"]) or ""
        for trow, t in team_of_row.items():
            if t == team and lead[trow]:
                boss_rows.setdefault(lead[trow], []).append(code_row[b["code"]])
    out = []

    # ---------------- nhãn tuần ở khối tổng ----------------
    lbl = [w["label"] for w in d["weeks"]]
    out.append(("E4:E7", [[lbl[i]] if i < len(lbl) else [""] for i in range(4)]))

    # ---------------- BẢNG 2 ----------------
    by_code = {b["code"]: b for b in d["boss"].values()}
    rows = []
    for r, code in zip(B2_ROWS, B2_CODES):
        b = by_code.get(code)
        wk = [((b["weeks"].get(w, 0) if b else 0) if w else "") for w in slots]
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
    for r in B3_ROWS:
        s = b3_of.get(r)
        if not s:                           # dòng thừa của mẫu -> xoá trắng
            rows.append([""] * (width - 2))
            continue
        line = [None] * (width - 2)        # bắt đầu từ cột C
        line[0] = tg.get(s, 0) or None     # C: mục tiêu tháng
        line[2] = _st(f"C{r}", f"D{r}")    # E: tình trạng tháng
        for k in CAT_ORDER:
            c0 = CAT_COL0[k]               # cột mục tiêu tuần của nhóm (0-based)
            for i, w in enumerate(slots):
                v = d["cat_week"].get(s, {}).get(k, {}).get(w, 0) if w else ""
                line[c0 - 2 + 1 + i * 3] = v                                   # doanh số tuần
                line[c0 - 2 + 2 + i * 3] = (_st(f"${_col(c0)}{r}",
                                                f"{_col(c0 + 1 + i * 3)}{r}") if w else "")
        rows.append(line)
    out.append((f"C{B3_ROWS[0]}:{_col(width - 1)}{B3_ROWS[-1]}", rows))

    # ---------------- BẢNG 1 ----------------
    # D: mục tiêu tháng; dòng THỰC TẾ: trỏ lại đúng dòng của người đó ở BẢNG 3
    n1 = B1_LAST - B1_ROW1 + 1                             # số dòng của BẢNG 1
    rows = [[None] * 9 for _ in range(n1)]                 # D..L
    names = [[None] for _ in range(n1)]                    # cột B: tên team / tên sale
    for trow, team, members in assign:
        names[trow - B1_ROW1] = [f"TỔNG TEAM {team.upper()}"]
        used = {r for r, _ in members}
        for r0 in dict(TEAM_SLOTS)[trow]:
            if r0 not in used:             # ô trống của mẫu -> xoá sạch cả khối 4 dòng
                i = r0 - B1_ROW1
                names[i] = [""]
                for k in range(4):
                    rows[i + k] = [""] * 9
        for r0, s in members:
            i = r0 - B1_ROW1
            names[i] = [s]
            rows[i][0] = tg.get(s, 0) or None
            r3 = b3[s]
            f = rows[i + 1]
            f[2] = f"=H{r3}+K{r3}+N{r3}+Q{r3}"
            f[3] = f"=U{r3}+X{r3}+AA{r3}+AD{r3}"
            f[4] = f"=AH{r3}+AK{r3}+AN{r3}+AQ{r3}"
            f[5] = f"=AU{r3}+AX{r3}+BA{r3}+BD{r3}"
            f[6] = f"=BH{r3}+BK{r3}+BN{r3}+BQ{r3}"
            br = sorted(boss_rows.get(r0, []))
            rows[i][7] = "=" + "+".join(f"D{x}" for x in br) if br else ""      # K: mục tiêu khách sếp
            f[7] = "=" + "+".join(f"E{x}" for x in br) if br else ""            # K: thực tế khách sếp
            for c in range(1, 8):          # E..K: tình trạng so mục tiêu với thực tế
                col = _col(3 + c)
                rows[i + 3][c] = _st(f"{col}{r0}", f"{col}{r0 + 1}")
    for trow, mrows in TEAM_SLOTS:        # khối mẫu thừa -> xoá trắng
        if trow not in {t for t, _, _ in assign}:
            names[trow - B1_ROW1] = [""]
            rows[trow - B1_ROW1] = [""] * 9
            for r0 in mrows:
                i = r0 - B1_ROW1
                names[i] = [""]
                for k in range(4):
                    rows[i + k] = [""] * 9
    out.append((f"B{B1_ROW1}:B{B1_LAST}", names))
    out.append((f"D{B1_ROW1}:L{B1_LAST}", rows))

    # ---------------- tên sale ở BẢNG 3 ----------------
    out.append((f"B{B3_ROWS[0]}:B{B3_ROWS[-1]}", [[b3_of.get(r, "")] for r in B3_ROWS]))
    return out
