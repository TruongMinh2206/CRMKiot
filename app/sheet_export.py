"""Sinh dữ liệu theo đúng bố cục sheet "BẢNG TỔNG DOANH SỐ KHÁCH SẾP KHÁCH SALE".

Trả về mảng 2 chiều để ghi thẳng vào Google Sheet (hoặc xuất Excel).
Bố cục: khối tổng (dòng 3-11) · BẢNG 1 · BẢNG 2 · BẢNG 3 (chi tiết theo tuần).
"""
from datetime import date, datetime

from . import db, reports

CATS = [("normal", "HÀNG THÔNG THƯỜNG"), ("excl_new", "HÀNG HÓA ĐỘC QUYỀN MỚI"),
        ("excl_old", "HÀNG HÓA ĐỘC QUYỀN CŨ"), ("slow", "HÀNG HÓA TỒN LÂU - CẬN DATE"),
        ("new_customer", "HÀNG HÓA BÁN CHO KHÁCH MỚI")]
EXCL_TM = ("ĐỘC QUYỀN MỚI", "ĐỘC QUYỀN CŨ", "Độc Quyền", "HÀNG TỒN LÂU", "HÀNG CẬN DATE")


def _month_range(ym):
    f = date.fromisoformat(ym + "-01")
    t = (f.replace(day=28) + __import__("datetime").timedelta(days=4)).replace(day=1) - __import__("datetime").timedelta(days=1)
    return f.isoformat(), t.isoformat()


def _cat_of(tm, cm):
    return cm.get(tm or "", "normal")


def sale_merge():
    """Gộp doanh số của người bán phụ vào một sale chính, ví dụ {"Sales Trang": "Sales Linh Bé"}.

    Kế toán ghi hoá đơn của một số người bán vào dòng của sale phụ trách team,
    nên CRM phải gộp giống vậy thì bảng mới khớp.
    """
    import json
    try:
        return json.loads(db.get_meta("sale_merge") or "{}")
    except ValueError:
        return {}


def _sellers_in_board():
    """Người bán được đưa vào bảng (đã xếp team sale, không phải team Kế toán/Khác)."""
    rows = db.rows("SELECT sale, team FROM teams")
    skip = {"Kế toán", "Khác", ""}
    out = [r["sale"] for r in rows if (r["team"] or "") not in skip]
    # bỏ tên rút gọn chỉ dùng để quy khách sếp về team (không phải người bán trên hoá đơn)
    real = {r["sold_by_name"] for r in db.rows(
        "SELECT DISTINCT sold_by_name FROM invoices WHERE sold_by_name IS NOT NULL")}
    return [s for s in out if s in real]


def collect(from_d, to_d, basis="accounting"):
    """Gom số liệu cho sheet, đúng theo khoảng ngày được chọn."""
    f, t = from_d, to_d
    weeks = reports._weeks(f, t)
    cm = reports.cat_map()
    alloc = reports.kpi_alloc()
    teams = {r["sale"]: r["team"] for r in db.rows("SELECT sale, team FROM teams")}
    targets = {r["sale"]: r for r in db.rows("SELECT * FROM kpi_targets WHERE ym=?", (f[:7],))}
    merge = sale_merge()
    sellers = _sellers_in_board()
    # người bán phụ vẫn phải nằm trong câu truy vấn để lấy được hoá đơn của họ
    src = sorted(set(sellers) | set(merge))
    sellers = sorted({merge.get(s, s) for s in sellers})
    ph = ",".join("?" * len(src)) or "''"

    def mg(name):
        return merge.get(name, name)

    def q(sql, params=()):
        return db.rows(sql, params)

    # ---- khách sale: theo người bán, loại khách sếp (tính gộp, không trừ hàng trả) ----
    sale_week = {}
    for w in weeks:
        a, b = w["from"], w["to"] + " 23:59:59"
        for r in q(f"""SELECT i.sold_by_name s, SUM(COALESCE(i.net_total,i.total)) v FROM invoices i
                JOIN customers c ON c.id=i.customer_id
                WHERE i.status=1 AND COALESCE(c.is_boss,0)=0 AND i.sold_by_name IN ({ph})
                  AND i.purchase_date BETWEEN ? AND ? GROUP BY 1""", tuple(src) + (a, b)):
            d0 = sale_week.setdefault(mg(r["s"]), {})
            d0[w["week"]] = d0.get(w["week"], 0) + (r["v"] or 0)

    # ---- khách sale theo nhóm hàng × tuần (gộp) ----
    cat_week = {}
    for w in weeks:
        a, b = w["from"], w["to"] + " 23:59:59"
        for r in q(f"""SELECT i.sold_by_name s, COALESCE(NULLIF(d.trade_mark,''), p.trade_mark,'') tm,
                SUM(d.sub_total) v FROM invoice_details d JOIN invoices i ON i.id=d.invoice_id
                JOIN customers c ON c.id=i.customer_id LEFT JOIN products p ON p.id=d.product_id
                WHERE i.status=1 AND COALESCE(c.is_boss,0)=0 AND i.sold_by_name IN ({ph})
                  AND i.purchase_date BETWEEN ? AND ? GROUP BY 1,2""", tuple(src) + (a, b)):
            k, sn = _cat_of(r["tm"], cm), mg(r["s"])
            cat_week.setdefault(sn, {}).setdefault(k, {}).setdefault(w["week"], 0)
            cat_week[sn][k][w["week"]] += r["v"] or 0

    # ---- doanh số bán cho khách mới (mua lần đầu trong tháng) × tuần ----
    for w in weeks:
        a, b = w["from"], w["to"] + " 23:59:59"
        for r in q(f"""WITH fb AS (SELECT customer_id, MIN(purchase_date) d FROM invoices WHERE status=1 GROUP BY 1)
                SELECT i.sold_by_name s, SUM(COALESCE(i.net_total,i.total)) v FROM invoices i
                JOIN customers c ON c.id=i.customer_id JOIN fb ON fb.customer_id=i.customer_id
                WHERE i.status=1 AND COALESCE(c.is_boss,0)=0 AND i.sold_by_name IN ({ph})
                  AND i.purchase_date BETWEEN ? AND ? AND fb.d BETWEEN ? AND ? GROUP BY 1""",
                    tuple(src) + (a, b, f, t + " 23:59:59")):
            sn = mg(r["s"])
            cat_week.setdefault(sn, {}).setdefault("new_customer", {}).setdefault(w["week"], 0)
            cat_week[sn]["new_customer"][w["week"]] += r["v"] or 0

    # ---- khách sếp: theo sale phụ trách khách, CÓ trừ hàng trả ----
    boss_rows = q("""SELECT c.id, c.code, c.name,
            COALESCE(NULLIF(c.sale_manual,''), NULLIF(c.sale_name,''),'(Chưa gán)') sale,
            COALESCE(c.boss_no_kpi,0) no_kpi FROM customers c WHERE COALESCE(c.is_boss,0)=1""")
    boss = {}
    for r in boss_rows:
        inv = db.one("""SELECT COALESCE(SUM(COALESCE(net_total,total)),0) v FROM invoices
            WHERE status=1 AND customer_id=? AND purchase_date BETWEEN ? AND ?""", (r["id"], f, t + " 23:59:59"))["v"]
        ret = db.one("""SELECT COALESCE(SUM(total),0) v FROM returns
            WHERE customer_id=? AND return_date BETWEEN ? AND ?""", (r["id"], f, t + " 23:59:59"))["v"]
        wk = {}
        for w in weeks:
            a, b = w["from"], w["to"] + " 23:59:59"
            iv = db.one("""SELECT COALESCE(SUM(COALESCE(net_total,total)),0) v FROM invoices
                WHERE status=1 AND customer_id=? AND purchase_date BETWEEN ? AND ?""", (r["id"], a, b))["v"]
            rv = db.one("""SELECT COALESCE(SUM(total),0) v FROM returns
                WHERE customer_id=? AND return_date BETWEEN ? AND ?""", (r["id"], a, b))["v"]
            wk[w["week"]] = iv - rv
        boss[r["id"]] = {**r, "total": inv - ret, "weeks": wk}

    return {"from": f, "to": t, "weeks": weeks, "sellers": sellers, "teams": teams,
            "targets": targets, "alloc": alloc, "sale_week": sale_week, "cat_week": cat_week,
            "boss": boss}


def _pct(v, tg, text=False):
    """Phần trăm. text=True thêm dấu ' để Google Sheet giữ nguyên chuỗi, không đổi thành số."""
    if not tg:
        return ""
    return f"{'\'' if text else ''}{round(v / tg * 100)}%"


def _st(v, tg):
    return ("ĐẠT" if v >= tg else "K ĐẠT") if tg else ""


def _n(v):
    """Số tiền -> số nguyên (sheet không cần phần thập phân)."""
    return int(round(v)) if isinstance(v, (int, float)) else v


def build_rows(d):
    """Dựng mảng 2 chiều theo đúng bố cục sheet gốc."""
    W = d["weeks"]
    nW = len(W)
    rows = [[""] for _ in range(2)]

    sale_tot = {s: sum(d["sale_week"].get(s, {}).values()) for s in d["sellers"]}
    boss_tot = sum(b["total"] for b in d["boss"].values())
    boss_kpi = sum(b["total"] for b in d["boss"].values() if not b["no_kpi"])
    sale_all = sum(sale_tot.values())
    target_all = sum((t.get("revenue_target") or 0) for t in d["targets"].values())

    def wsum(getter):
        return [sum(getter(s).get(w["week"], 0) for s in d["sellers"]) for w in W]

    tuan_sale = wsum(lambda s: d["sale_week"].get(s, {}))
    tuan_sep = [sum(b["weeks"].get(w["week"], 0) for b in d["boss"].values()) for w in W]
    tuan_tong = [a + b for a, b in zip(tuan_sale, tuan_sep)]

    # ---------------- khối tổng (dòng 3-11) ----------------
    head = [["", "CHỈ TIÊU ", "KẾT QUẢ", "", "TỔNG DOANH SỐ THEO TUẦN", "KẾT QUẢ", "",
             "TỔNG DOANH SỐ SALE THEO TUẦN", "KẾT QUẢ", "", "TỔNG DOANH SỐ SẾP THEO TUẦN", "KẾT QUẢ"]]
    labels = [("DOANH SỐ MỤC TIÊU TỔNG", target_all), ("TỔNG DOANH SỐ THỰC TẾ", sale_all + boss_tot),
              ("DOANH SỐ KHÁCH SẾP", boss_tot), ("DOANH SỐ KHÁCH SALE", sale_all)]
    for i, (lbl, val) in enumerate(labels):
        w = W[i] if i < nW else None
        head.append(["", lbl, val, "",
                     w["label"] if w else "", tuan_tong[i] if i < nW else "", "",
                     f"TUẦN {i+1}" if i < nW else "", tuan_sale[i] if i < nW else "", "",
                     f"TUẦN {i+1}" if i < nW else "", tuan_sep[i] if i < nW else ""])
    head.append(["", "% KPI TỔNG", _pct(sale_all + boss_tot, target_all)])
    head.append(["", "% KPI KHÁCH SẾP", ""])
    head.append(["", "% KPI KHÁCH SALE", _pct(sale_all, target_all)])
    head.append(["", "TỈNH TRẠNG", _st(sale_all + boss_tot, target_all)])
    rows += head
    while len(rows) < 16:
        rows.append([""])

    # ---------------- BẢNG 1 ----------------
    rows.append(["BẢNG 1 - DOANH SỐ TỔNG KHÁCH SẾP + KHÁCH SALE THEO THÁNG"])
    rows.append(["STT", "TÊN SALE", "DANH MỤC", "DOANH SỐ MỤC TIÊU THÁNG", "DOANH SỐ KHÁCH SALE THEO THÁNG",
                 "", "", "", "", "", "2. TỔNG DOANH SỐ KHÁCH SẾP",
                 "3. TỔNG DOANH SỐ BAO GỒM KHÁCH SẾP + KHÁCH SALE"])
    rows.append(["", "", "", "", "1. TỔNG DOANH SỐ KHÁCH SALE"] +
                [f"1.{i+1}. DOANH SỐ {lbl}" for i, (_, lbl) in enumerate(CATS)])

    by_team = {}
    for s in d["sellers"]:
        by_team.setdefault(d["teams"].get(s) or "(Chưa xếp team)", []).append(s)
    # khách sếp quy về team của sale phụ trách; trong team gán cho người doanh số cao nhất (trưởng nhóm)
    lead = {team: max(ms, key=lambda x: sale_tot[x]) for team, ms in by_team.items()}
    boss_by_seller = {}
    for b in d["boss"].values():
        if b["no_kpi"]:
            continue
        team = d["teams"].get(b["sale"]) or ""
        who = lead.get(team)
        if who:
            boss_by_seller[who] = boss_by_seller.get(who, 0) + b["total"]

    def cat_tot(s, k):
        return sum(d["cat_week"].get(s, {}).get(k, {}).values())

    def boss_of_team(team):
        return sum(v for s, v in boss_by_seller.items() if (d["teams"].get(s) or "") == team)

    stt = 0
    for team, members in sorted(by_team.items(), key=lambda x: -sum(sale_tot[s] for s in x[1])):
        stt += 1
        tsale = sum(sale_tot[s] for s in members)
        tsep = boss_of_team(team)
        ttg = sum((d["targets"].get(s, {}).get("revenue_target") or 0) for s in members)
        rows.append([stt, f"TỔNG TEAM {team.upper()}", "", ttg, tsale] +
                    [sum(cat_tot(s, k) for s in members) for k, _ in CATS] +
                    [tsep, tsale + tsep])
        for s in sorted(members, key=lambda x: -sale_tot[x]):
            tg = (d["targets"].get(s, {}) or {}).get("revenue_target") or 0
            mine_sep = boss_by_seller.get(s, 0)
            rows.append(["", s, "MỤC TIÊU", tg, tg] +
                        [round(tg * d["alloc"].get(k, 0) / 100) for k, _ in CATS] + ["", sale_tot[s] + mine_sep])
            rows.append(["", "", "DOANH SỐ THỰC TẾ", "", sale_tot[s]] +
                        [cat_tot(s, k) for k, _ in CATS] + [mine_sep])
            rows.append(["", "", "% KPI THÁNG", "", _pct(sale_tot[s], tg)] +
                        [_pct(cat_tot(s, k), round(tg * d["alloc"].get(k, 0) / 100)) for k, _ in CATS])
            rows.append(["", "", "TÌNH TRẠNG", "", _st(sale_tot[s], tg)] +
                        [_st(cat_tot(s, k), round(tg * d["alloc"].get(k, 0) / 100)) for k, _ in CATS])

    # ---------------- BẢNG 2 ----------------
    rows.append([])
    rows.append(["BẢNG 2 - DOANH SỐ KHÁCH SẾP"])
    rows.append(["STT", "KHÁCH HÀNG", "SALE PHỤ TRÁCH", "DOANH SỐ MỤC TIÊU THÁNG", "DOANH SỐ THỰC TẾ THÁNG",
                 "% KPI THÁNG", "TÌNH TRẠNG", "DOANH SỐ MỤC TIÊU TUẦN"] +
                [x for w in W for x in (f"TUẦN {w['week']}", f"TÌNH TRẠNG TUẦN {w['week']}", f"% KPI TUẦN {w['week']}")])
    for i, b in enumerate(sorted(d["boss"].values(), key=lambda x: -x["total"]), 1):
        wk = []
        for w in W:
            wk += [b["weeks"].get(w["week"], 0), "", ""]
        rows.append([i, b["name"], b["sale"], "K áp KPI" if b["no_kpi"] else "", b["total"], "", "", ""] + wk)

    # ---------------- BẢNG 3 ----------------
    rows.append([])
    rows.append(["BẢNG 3 - DOANH SỐ KHÁCH SALE THEO THÁNG / TUẦN"])
    h1 = ["STT", "TÊN SALE", "DOANH SỐ MỤC TIÊU THÁNG", "DOANH SỐ THỰC TẾ THÁNG", "TÌNH TRẠNG", "% KPI THÁNG"]
    h2 = ["", "", "", "", "", ""]
    for _, lbl in CATS:
        h1 += [f"DOANH SỐ {lbl}"] + [""] * 12
        h2 += ["DOANH SỐ MỤC TIÊU TUẦN"]
        for w in W:
            h2 += [f"TUẦN {w['week']}", f"TÌNH TRẠNG TUẦN {w['week']}", f"% KPI TUẦN {w['week']}"]
    h1 += ["Vấn đề", "Nguyên nhân"]
    rows.append(h1)
    rows.append(h2)
    for i, s in enumerate(sorted(d["sellers"], key=lambda x: -sale_tot[x]), 1):
        tg = (d["targets"].get(s, {}) or {}).get("revenue_target") or 0
        line = [i, s, tg, sale_tot[s], _st(sale_tot[s], tg), _pct(sale_tot[s], tg)]
        for k, _ in CATS:
            ctg = round(tg * d["alloc"].get(k, 0) / 100)
            wtg = round(ctg / max(1, nW))
            line.append(wtg)
            for w in W:
                v = d["cat_week"].get(s, {}).get(k, {}).get(w["week"], 0)
                line += [v, _st(v, wtg), _pct(v, wtg)]
        rows.append(line)
    return [[_n(x) for x in r] for r in rows]
