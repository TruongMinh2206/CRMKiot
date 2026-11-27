"""Xuất công nợ ra một file Google Sheet riêng: mỗi tháng một tab khách (nợ đầu tháng, mua, trả hàng,
đã thanh toán, còn nợ, ngày nợ, hạn nợ, quá hạn bao nhiêu tiền / bao nhiêu ngày) và một tab theo sale.
Xuất lại cùng tháng thì ghi đè tab của tháng đó; tab của các tháng khác giữ nguyên."""
import logging
import re
from datetime import datetime, timedelta, timezone

from . import db, gsheet, reports
from .campaign_export import C, Grid, _batch, COMMA_DECIMAL
from .campaign_sheet import col, serial
from .gsheet import API, ConfigError

log = logging.getLogger("debt_sheet")
HEAD = "#FFF2CC"
RED = "#CC0000"


def sheet_id():
    v = (db.get_meta("debt_sheet_id") or "").strip()
    m = re.search(r"/d/([A-Za-z0-9_-]{20,})", v)
    return m.group(1) if m else v


def status():
    sid = sheet_id()
    return {"sheet_id": sid, "url": f"https://docs.google.com/spreadsheets/d/{sid}/edit" if sid else "",
            "client_email": gsheet.status().get("client_email", "")}


def _day(iso):
    return serial(iso[:10] + " 00:00:00") if iso else ""


def _customers(d, sep):
    G = Grid()
    head = ["STT", "MÃ KH", "KHÁCH HÀNG", "SĐT", "SALE", "NỢ ĐẦU THÁNG", "MUA TRONG THÁNG", "TRẢ HÀNG", "ĐÃ THANH TOÁN",
            "CÒN NỢ CUỐI THÁNG" if not d["is_current"] else "CÒN NỢ HIỆN TẠI", "NGÀY NỢ (HĐ CŨ NHẤT CÒN NỢ)", "HẠN NỢ (NGÀY)",
            "NGÀY ĐẾN HẠN", "QUÁ HẠN (TIỀN)", "QUÁ HẠN (NGÀY)", "0–30 NGÀY", "31–60 NGÀY", "61–90 NGÀY", "TRÊN 90 NGÀY",
            "THANH TOÁN GẦN NHẤT", "MUA GẦN NHẤT", "GHI CHÚ"]
    for i, h in enumerate(head, 1):
        G.put(1, i, C(h, bold=True, size=10, h="CENTER", wrap=True, fill=HEAD))
    G.widths.update({1: 40, 2: 85, 3: 240, 4: 100, 5: 150, 6: 115, 7: 115, 8: 95, 9: 115, 10: 120, 11: 100, 12: 65, 13: 95,
                     14: 115, 15: 70, 16: 105, 17: 105, 18: 105, 19: 105, 20: 100, 21: 100, 22: 220})
    rows = sorted(d["rows"], key=lambda r: (-(r["overdue"] or 0), -(r["debt"] or 0)))
    r = 2
    for i, c in enumerate(rows, 1):
        late = c["overdue"] > 0
        note = " · ".join(x for x in (("Khách sếp" if c["is_boss"] else ""), ("đã ngừng mua trên 60 ngày" if c["risk"] else ""),
                                      ("có nợ từ trước dữ liệu CRM" if c["older"] else ""), c.get("term_note") or "") if x)
        vals = [(i, "int"), (c["code"], None), (c["name"], None), (c["contact_number"] or "", None), (c["sale"], None),
                (c["opening"], "int"), (c["bought"], "int"), (c["returned"], "int"), (c["paid"], "int"),
                (f"=F{r}+G{r}-H{r}-I{r}", "int"), (_day(c["oldest_date"]), "day"), (c["term"], "int"), (_day(c["due_date"]), "day"),
                (c["overdue"] or "", "int"), (c["overdue_days"] or "", "int"), (c["b0"] or "", "int"), (c["b1"] or "", "int"),
                (c["b2"] or "", "int"), (c["b3"] or "", "int"), (_day(c["last_pay"]), "day"), (_day(c["last_purchase"]), "day"), (note, None)]
        for j, (v, num) in enumerate(vals, 1):
            G.put(r, j, C(v, size=10, num=num, h="RIGHT" if num else None, bold=j in (10, 14),
                          color=RED if late and j in (13, 14, 15) else None, wrap=j in (3, 22)))
        r += 1
    last = max(r - 1, 2)
    G.put(r, 3, C("TỔNG", bold=True, size=10, fill=HEAD))
    for j in (6, 7, 8, 9, 10, 14, 16, 17, 18, 19):
        G.put(r, j, C(f"=SUM({col(j)}2:{col(j)}{last})", bold=True, size=10, num="int", h="RIGHT", fill=HEAD))
    return G


def _by_sale(d):
    G = Grid()
    head = ["SALE", "SỐ KH CÒN NỢ", "NỢ ĐẦU THÁNG", "MUA TRONG THÁNG", "TRẢ HÀNG", "ĐÃ THANH TOÁN", "CÒN NỢ", "QUÁ HẠN", "% QUÁ HẠN",
            "0–30 NGÀY", "31–60 NGÀY", "61–90 NGÀY", "TRÊN 90 NGÀY"]
    for i, h in enumerate(head, 1):
        G.put(1, i, C(h, bold=True, size=10, h="CENTER", wrap=True, fill=HEAD))
        G.widths[i] = 180 if i == 1 else 115
    r = 2
    for g in d["by_sale"]:
        vals = [g["sale"], g["customers"], g["opening"], g["bought"], g["returned"], g["paid"], f"=C{r}+D{r}-E{r}-F{r}",
                g["overdue"] or "", f"=IFERROR(H{r}/G{r})", g["b0"] or "", g["b1"] or "", g["b2"] or "", g["b3"] or ""]
        for j, v in enumerate(vals, 1):
            cell = C(v, size=10, num=None if j == 1 else "int", h=None if j == 1 else "RIGHT", color=RED if j == 8 and g["overdue"] else None)
            if j == 9:
                cell["userEnteredFormat"]["numberFormat"] = {"type": "PERCENT", "pattern": "0%"}
            G.put(r, j, cell)
        r += 1
    last = max(r - 1, 2)
    G.put(r, 1, C("TỔNG", bold=True, size=10, fill=HEAD))
    for j in (2, 3, 4, 5, 6, 7, 8, 10, 11, 12, 13):
        G.put(r, j, C(f"=SUM({col(j)}2:{col(j)}{last})", bold=True, size=10, num="int", h="RIGHT", fill=HEAD))
    return G


def run(ym=None, sale=None):
    sid = sheet_id()
    if not sid:
        raise ConfigError("Chưa khai báo file Google Sheet để xuất công nợ (Công nợ → ⬇ Google Sheet → dán link file).")
    if sid in {(db.get_meta("gsheet_id") or "").strip(), (db.get_meta("campaign_export_id") or "").strip(),
               (db.get_meta("campaign_gsheet_id") or "").strip()}:
        raise ConfigError("File công nợ phải là một file riêng, không trùng file bảng doanh số hay file thưởng chiến dịch.")
    d = reports.debt_aging(sale, ym)
    mm = f"T{d['ym'][5:]}-{d['ym'][:4]}"
    tabs = [(f"CÔNG NỢ {mm}", None), (f"THEO SALE {mm}", None)]
    try:
        info = gsheet.config_account()
        with gsheet._client(gsheet._token(info)) as c:
            r = c.get(f"{API}/{sid}", params=[("fields", "properties(locale,title),sheets.properties(sheetId,title)")])
            if r.status_code == 403:
                raise ConfigError("CRM chưa có quyền sửa file công nợ. Chia sẻ file cho tài khoản dịch vụ của CRM với quyền Người chỉnh sửa.")
            if r.status_code == 404:
                raise ConfigError("Không tìm thấy file Google Sheet công nợ — kiểm tra lại đường link.")
            if r.status_code >= 400:
                raise ConfigError(f"Google từ chối khi đọc file: {r.text[:200]}")
            meta = r.json()
            locale = (meta.get("properties", {}).get("locale") or "en_US").split("_")[0].lower()
            sep = ";" if locale in COMMA_DECIMAL else ","
            old = {s["properties"]["title"]: s["properties"]["sheetId"] for s in meta.get("sheets", [])}
            grids = [_customers(d, sep), _by_sale(d)]
            stamp = datetime.now(timezone(timedelta(hours=7))).strftime("%d/%m/%Y %H:%M")
            grids[0].put(1, 24, C(f"Xuất từ CRM lúc {stamp}" + (" · chỉ một phần sale" if sale else ""), size=10, border=False))
            used = set(old.values())
            reqs, gids = [], []
            for i, ((title, _), G) in enumerate(zip(tabs, grids)):
                gid = next(x for x in range(2000 + i * 1000, 9_000_000) if x not in used)
                used.add(gid)
                gids.append(gid)
                reqs.append({"addSheet": {"properties": {"sheetId": gid, "title": f"_dang_xuat_{i}", "index": i, "gridProperties": {
                    "rowCount": max(100, G.maxr + 20), "columnCount": max(26, G.maxc + 2), "frozenRowCount": 1,
                    "frozenColumnCount": 3 if i == 0 else 1}}}})
            # xoá tab cũ cùng tháng và tab trống mặc định, rồi đặt tên tab mới
            drop = [old[t] for t, _ in tabs if t in old] + [g for t, g in old.items() if t in ("Trang tính1", "Sheet1", "Trang tính 1")]
            drop += [g for t, g in old.items() if t.startswith("_dang_xuat_")]
            reqs += [{"deleteSheet": {"sheetId": g}} for g in dict.fromkeys(drop)]
            reqs += [{"updateSheetProperties": {"properties": {"sheetId": g, "title": t}, "fields": "title"}}
                     for g, (t, _) in zip(gids, tabs)]
            _batch(c, sid, reqs, "tạo tab công nợ")
            _batch(c, sid, [q for g, G in zip(gids, grids) for q in G.requests(g)]
                   + [{"setBasicFilter": {"filter": {"range": {"sheetId": gids[0], "startRowIndex": 0, "endRowIndex": grids[0].maxr - 1,
                                                                 "startColumnIndex": 0, "endColumnIndex": 22}}}}], "ghi công nợ")
        return {"url": f"https://docs.google.com/spreadsheets/d/{sid}/edit#gid={gids[0]}", "rows": len(d["rows"]), "ym": d["ym"], "tab": tabs[0][0]}
    except ConfigError:
        raise
    except Exception as e:
        log.exception("xuất công nợ thất bại")
        raise ConfigError(f"{type(e).__name__}: {e}")
