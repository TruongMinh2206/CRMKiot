"""Xuất thưởng chiến dịch ra một file Google Sheet riêng, theo đúng bố cục file của kế toán.

File đích chỉ dành cho bản xuất này (khác file bảng doanh số và khác file "Đổ vào file kế toán").
Mỗi lần xuất dựng lại toàn bộ file:

* tab BẢNG TỔNG: mỗi khách một nhóm dòng, mỗi dòng một chương trình, cột TIẾN ĐỘ là công thức
  trỏ sang ô kết quả trong tab của khách, cột LINK bấm sang tab đó;
* mỗi khách một tab "STT. TÊN": mỗi chương trình một khối 5 cột (ngày tháng, sản phẩm, số lượng,
  đơn giá bán, tổng doanh số) có dòng "Khách dư…" ở đầu và dòng Tổng ở cuối, bên dưới là bảng quy đổi
  thưởng giống bản của kế toán. Team / Retinal / Enzym có thêm bảng cộng chung.
"""
import logging
import re
from datetime import datetime, timedelta, timezone

import json

from . import campaign, campaign_sheet, db, gsheet
from .campaign_sheet import col, serial
from .gsheet import API, ConfigError

log = logging.getLogger("campaign_export")

TONG = "BẢNG TỔNG"
TONG_ID = 1
FONT = "Times New Roman"
YELLOW = "#FBBC04"
PINK = "#EAD1DC"
ORANGE = "#F8BB33"
ORANGE2 = "#F6BA47"
LABEL = {"NA": "NẠ KRLAB", "NGHE": "NGHỆ", "EUN": "EUNDAN VITAMIN C", "DER": "DEMAXILIC",
         "WB": "WONDER BATH", "LUM": "LUMOS"}
HEAD_FILL = {"NA": YELLOW, "NGHE": YELLOW, "EUN": YELLOW, "DER": PINK, "WB": PINK, "LUM": PINK}
SUB = {"RET": "RETINAL KRLAB (×3)", "ENZ": "ENZYM"}
COMMA_DECIMAL = ("vi", "de", "fr", "es", "it", "pt", "ru", "id", "nl", "pl", "tr", "cs", "da", "sv", "nb",
                 "fi", "ro", "hu", "uk", "el", "bg", "hr", "sk", "sl", "lt", "lv", "et", "sr", "ca")


def sheet_id():
    v = (db.get_meta("campaign_export_id") or "").strip()
    m = re.search(r"/d/([A-Za-z0-9_-]{20,})", v)
    return m.group(1) if m else v


def _rgb(h):
    h = h.lstrip("#")
    return {"red": int(h[0:2], 16) / 255, "green": int(h[2:4], 16) / 255, "blue": int(h[4:6], 16) / 255}


SOLID = {"style": "SOLID"}
BORDERS = {"top": SOLID, "bottom": SOLID, "left": SOLID, "right": SOLID}


def C(v=None, bold=False, fill=None, num=None, h=None, wrap=False, size=11, border=True, link=None,
      color=None, underline=False):
    """Một ô: giá trị (chuỗi, số hoặc công thức bắt đầu bằng '=') kèm định dạng."""
    fmt = {"textFormat": {"fontFamily": FONT, "fontSize": size, "bold": bold}, "verticalAlignment": "MIDDLE"}
    if underline:
        fmt["textFormat"]["underline"] = True
    if color:
        fmt["textFormat"]["foregroundColor"] = _rgb(color)
    if link:
        fmt["textFormat"]["link"] = {"uri": link}
    if fill:
        fmt["backgroundColor"] = _rgb(fill)
    if num == "date":
        fmt["numberFormat"] = {"type": "DATE_TIME", "pattern": "dd/mm/yyyy hh:mm"}
    elif num == "day":
        fmt["numberFormat"] = {"type": "DATE", "pattern": "dd/mm/yyyy"}
    elif num == "int":
        fmt["numberFormat"] = {"type": "NUMBER", "pattern": "#,##0"}
    elif num == "dec":
        fmt["numberFormat"] = {"type": "NUMBER", "pattern": "#,##0.00"}
    if h:
        fmt["horizontalAlignment"] = h
    if wrap:
        fmt["wrapStrategy"] = "WRAP"
    if border:
        fmt["borders"] = BORDERS
    cell = {"userEnteredFormat": fmt}
    if isinstance(v, str) and v.startswith("="):
        cell["userEnteredValue"] = {"formulaValue": v}
    elif isinstance(v, (int, float)) and not isinstance(v, bool):
        cell["userEnteredValue"] = {"numberValue": v}
    elif v not in (None, ""):
        cell["userEnteredValue"] = {"stringValue": str(v)}
    return cell


class Grid:
    """Lưới ô của một tab (đánh số dòng, cột từ 1) + các vùng gộp ô + độ rộng cột."""

    def __init__(self):
        self.cells, self.merges, self.widths, self.maxr, self.maxc = {}, [], {}, 1, 1

    def put(self, r, c, cell):
        self.cells[(r, c)] = cell
        self.maxr, self.maxc = max(self.maxr, r), max(self.maxc, c)

    def merge(self, r1, c1, r2, c2):
        if r2 > r1 or c2 > c1:
            self.merges.append((r1, c1, r2, c2))

    def requests(self, gid):
        rows = []
        for r in range(1, self.maxr + 1):
            rows.append({"values": [self.cells.get((r, c), {}) for c in range(1, self.maxc + 1)]})
        reqs = []
        for i in range(0, len(rows), 400):                         # chia nhỏ để mỗi lệnh không quá nặng
            reqs.append({"updateCells": {"rows": rows[i:i + 400], "fields": "userEnteredValue,userEnteredFormat",
                                         "start": {"sheetId": gid, "rowIndex": i, "columnIndex": 0}}})
        for r1, c1, r2, c2 in self.merges:
            reqs.append({"mergeCells": {"mergeType": "MERGE_ALL", "range": {
                "sheetId": gid, "startRowIndex": r1 - 1, "endRowIndex": r2, "startColumnIndex": c1 - 1, "endColumnIndex": c2}}})
        for c, px in self.widths.items():
            reqs.append({"updateDimensionProperties": {"range": {"sheetId": gid, "dimension": "COLUMNS",
                                                                 "startIndex": c - 1, "endIndex": c},
                                                       "properties": {"pixelSize": px}, "fields": "pixelSize"}})
        return reqs


def _tab_title(stt, grp, used):
    t = re.sub(r"[\[\]\*\?:/\\']", " ", f"{stt}. {(grp or '').upper()}" if stt else (grp or "").upper())
    t = re.sub(r"\s+", " ", t).strip()[:90] or "KHÁCH"
    base, i = t, 2
    while t in used:
        t, i = f"{base} ({i})", i + 1
    used.add(t)
    return t


def _ref(title, r, c):
    return "'" + title.replace("'", "''") + f"'!{col(c)}{r}"


def build_customer(title, g, entries, cust, lines, until, sep):
    """Dựng tab của một khách. Trả về lưới và ô kết quả (dòng, cột) của từng chương trình."""
    G = Grid()
    result = {}
    by_prog = {}
    for e in entries:
        by_prog.setdefault(campaign.row_of(e["program"]), []).append(e)
    blk = 0                                                    # thứ tự khối trên tab, mỗi khối cách nhau 7 cột
    for prog in campaign.ORDER:
        es = by_prog.get(prog)
        if not es:
            continue
        es.sort(key=lambda e: (e["program"] != prog, e["id"]))
        P = campaign.PROGRAMS[prog]
        amount = P["unit"] == "amount"
        members = {(e["member"] or e["customer_code"]) for e in es if e["program"] == prog}
        blocks = []
        for e in es:
            c0 = 1 + blk * 7
            blk += 1
            fill = HEAD_FILL.get(prog, YELLOW)
            head = ["NGÀY THÁNG", "SẢN PHẨM DERMAXILIC" if prog == "DER" else "SẢN PHẨM", "SỐ LƯỢNG",
                    "ĐƠN GIÁ BÁN", "TỔNG DOANH SỐ"]
            for i, h in enumerate(head):
                G.put(1, c0 + i, C(h, bold=True, fill=fill, h="CENTER", wrap=True))
            G.widths.update({c0: 125, c0 + 1: 330, c0 + 2: 105, c0 + 3: 100, c0 + 4: 145, c0 + 5: 30, c0 + 6: 30})
            r = 2
            got = sorted(campaign._entry_lines(e, cust, lines, until), key=lambda x: (x["d"], x["code"] or ""))
            cq, ca = e["carry_qty"] or 0, e["carry_amount"] or 0
            if cq and not ca and not amount and got:          # phần dư tính theo đơn giá hay gặp nhất của khối, như kế toán
                prices = {}
                for x in got:
                    if x["q"] > 0:
                        pr = round(x["amt"] / x["q"])
                        prices[pr] = prices.get(pr, 0) + 1
                if prices:
                    ca = max(prices, key=prices.get) * cq
            if cq or ca or e["carry_note"]:
                note = e["carry_note"] or ""
                txt = "Khách dư từ chương trình trước" + (f" ({note})" if note else "")
                G.put(r, c0, C(None))
                G.put(r, c0 + 1, C(txt, bold=True, h="CENTER", wrap=True))
                if amount:
                    G.put(r, c0 + 2, C(None, num="int"))
                    G.put(r, c0 + 3, C(None, num="int"))
                    G.put(r, c0 + 4, C(ca or 0, bold=True, num="int", h="RIGHT"))
                else:
                    price = round(ca / cq) if cq and ca else None
                    G.put(r, c0 + 2, C(cq, bold=True, num="int", h="RIGHT"))
                    G.put(r, c0 + 3, C(price, bold=True, num="int", h="RIGHT"))
                    G.put(r, c0 + 4, C(f"={col(c0 + 2)}{r}*{col(c0 + 3)}{r}" if price else (ca or 0),
                                       bold=True, num="int", h="RIGHT"))
                r += 1
            for x in got:
                price = x["amt"] / x["q"] if x["q"] else 0
                G.put(r, c0, C(serial(x["d"]), num="date", h="RIGHT"))
                name = x["pn"] + (" (trả hàng)" if x["kind"] == "return" else "")
                G.put(r, c0 + 1, C(name))
                G.put(r, c0 + 2, C(x["q"], num="int", h="RIGHT", color="#CC0000" if x["q"] < 0 else None))
                G.put(r, c0 + 3, C(round(price, 2), num="int", h="RIGHT"))
                G.put(r, c0 + 4, C(f"={col(c0 + 2)}{r}*{col(c0 + 3)}{r}", num="int", h="CENTER"))
                r += 1
            for _ in range(3):                                    # vài dòng trống để kế toán ghi tay nếu cần
                for i in range(5):
                    G.put(r, c0 + i, C(None, num="date" if i == 0 else ("int" if i >= 2 else None)))
                r += 1
            tot = r
            label = ""
            if e["program"] != prog:
                label = SUB.get(e["program"], e["program"])
            elif len(members) > 1:
                label = (e["member"] or e["customer_code"]).upper()
            G.put(tot, c0, C(label, bold=True, h="CENTER", wrap=True))
            G.put(tot, c0 + 1, C("Tổng", bold=True, h="CENTER"))
            G.put(tot, c0 + 2, C(f"=SUM({col(c0 + 2)}2:{col(c0 + 2)}{tot - 1})", bold=True, num="int", h="CENTER"))
            G.put(tot, c0 + 3, C(None))
            G.put(tot, c0 + 4, C(f"=SUM({col(c0 + 4)}2:{col(c0 + 4)}{tot - 1})", bold=True, num="int", h="CENTER"))
            k = 3 if e["program"] == "RET" else 1
            ref = f"{col(c0 + 4)}{tot}" if amount else f"{col(c0 + 2)}{tot}"
            blocks.append({"c0": c0, "tot": tot, "label": label or LABEL.get(prog, prog), "val": ref if k == 1 else f"{ref}*{k}"})

        # bảng cộng chung (team / Retinal / Enzym) + bảng quy đổi, đặt dưới khối đầu tiên của chương trình
        b0 = blocks[0]["c0"] + 1
        r = max(b["tot"] for b in blocks) + 4
        if len(blocks) > 1:
            G.put(r, b0, C("CHẠY CHUNG THƯỞNG", bold=True, fill=ORANGE, h="CENTER"))
            G.put(r, b0 + 1, C("DOANH SỐ" if amount else "SỐ LƯỢNG", bold=True, fill=ORANGE, h="CENTER"))
            first = r + 1
            for b in blocks:
                r += 1
                G.put(r, b0, C(b["label"], bold=True, h="CENTER"))
                G.put(r, b0 + 1, C("=" + b["val"], num="int", h="CENTER"))
            r += 1
            G.put(r, b0, C("TỔNG CỘNG", bold=True, h="CENTER"))
            G.put(r, b0 + 1, C(f"=SUM({col(b0 + 1)}{first}:{col(b0 + 1)}{r - 1})", bold=True, num="int", h="CENTER"))
            val = f"{col(b0 + 1)}{r}"
            r += 3
        else:
            val = blocks[0]["val"]
        row = next((x for x in g["rows"] if x["program"] == prog), {})
        reg = row.get("registered", "")
        if P["mode"] == "ticket" and not amount:                      # Nạ Krlab: vé theo số set
            G.put(r, b0, C("1 VÉ HÀN QUỐC", bold=True, fill=ORANGE, h="CENTER"))
            G.put(r, b0 + 1, C("SỐ THỰC ĐẠT", bold=True, fill=ORANGE, h="CENTER"))
            G.put(r, b0 + 2, C("BẢNG QUY ĐỔI", bold=True, fill=ORANGE, h="CENTER"))
            G.put(r, b0 + 3, C(None, fill=ORANGE))
            G.merge(r, b0 + 2, r, b0 + 3)
            G.put(r + 1, b0, C(P["per"], bold=True, fill=ORANGE, num="int", h="CENTER"))
            G.put(r + 1, b0 + 1, C("=" + val, bold=True, fill=ORANGE, num="int", h="CENTER"))
            G.put(r + 1, b0 + 2, C("SỐ LƯỢNG", bold=True, fill=ORANGE, h="CENTER"))
            G.put(r + 1, b0 + 3, C("THƯỞNG CHIẾN DỊCH", bold=True, fill=ORANGE, h="CENTER"))
            gifts = P.get("gifts", [])
            labels = [("SỐ VÉ KHÁCH ĐK", None), ("SỐ VÉ CHẠY ĐƯỢC", ORANGE2), ("SỐ CÒN LẠI", None)]
            for i, (lb, fl) in enumerate(labels):
                rr = r + 2 + i
                G.put(rr, b0, C(lb, bold=True, fill=fl, h="CENTER"))
                if i == 0:
                    G.put(rr, b0 + 1, C(reg, h="CENTER"))
                elif i == 1:
                    G.put(rr, b0 + 1, C(f"={col(b0 + 1)}{r + 1}/{col(b0)}{r + 1}", bold=True, fill=fl, num="dec", h="CENTER"))
                else:
                    G.put(rr, b0 + 1, C(f"={col(b0 + 1)}{r + 1}-INT({col(b0 + 1)}{r + 3})*{col(b0)}{r + 1}", num="int", h="CENTER"))
                need, gift = gifts[i] if i < len(gifts) else ("", "")
                G.put(rr, b0 + 2, C(f"{need} set" if need else "", h="CENTER"))
                G.put(rr, b0 + 3, C(gift, h="CENTER"))
            result[prog] = (r + 3, b0 + 1)
        elif P["mode"] == "ticket":                                   # Dermaxilic / Wonder Bath: vé theo doanh số
            G.put(r, b0, C("VÉ HÀN QUỐC", bold=True, fill=PINK, h="CENTER"))
            G.put(r, b0 + 1, C(None, fill=PINK))
            G.merge(r, b0, r, b0 + 1)
            rows = [("1 vé du lịch Hàn Quốc", "Doanh số", True), ("", P["per"], False),
                    ("Số khách đăng kí chạy", reg, False), ("Số chạy được", "=" + val, False),
                    ("Số vé đạt được", None, True)]
            for i, (a, b, bold) in enumerate(rows):
                rr = r + 1 + i
                G.put(rr, b0, C(a, bold=bold, fill=PINK if i in (0, 4) else None, h="RIGHT"))
                if i == 4:
                    b = f"={col(b0 + 1)}{r + 4}/{col(b0 + 1)}{r + 2}"
                G.put(rr, b0 + 1, C(b, bold=bold, fill=PINK if i in (0, 4) else None,
                                    num="dec" if i == 4 else ("int" if i in (1, 3) else None), h="CENTER"))
            G.merge(r + 1, b0, r + 2, b0)
            result[prog] = (r + 5, b0 + 1)
        elif P["mode"] == "usd":                                      # Nghệ / Eundan: thưởng đô theo mốc
            G.put(r, b0, C("THƯỞNG ĐÔ", bold=True, fill=ORANGE, h="CENTER"))
            G.put(r, b0 + 1, C("SỐ THỰC ĐẠT", bold=True, fill=ORANGE, h="CENTER"))
            G.put(r, b0 + 2, C("THƯỞNG CHIẾN DỊCH", bold=True, h="CENTER"))
            G.put(r, b0 + 3, C(None))
            G.put(r, b0 + 4, C(None))
            G.merge(r, b0 + 2, r, b0 + 4)
            G.put(r + 1, b0, C(None, fill=ORANGE))
            G.put(r + 1, b0 + 1, C("=" + val, bold=True, fill=ORANGE, num="int", h="CENTER"))
            for i, h in enumerate(("SỐ LƯỢNG", "THƯỞNG ĐÔ", "QUY ĐỔI TỪ ĐÔ")):
                G.put(r + 1, b0 + 2 + i, C(h, bold=True, h="CENTER"))
            tiers = sorted(P["tiers"])
            G.put(r + 2, b0, C("SỐ ĐÔ KHÁCH ĐK", bold=True, h="CENTER"))
            G.put(r + 2, b0 + 1, C(reg, h="CENTER"))
            G.put(r + 3, b0, C("SỐ ĐÔ KHÁCH CHẠY ĐƯỢC", bold=True, fill=ORANGE, h="CENTER"))
            G.put(r + 3, b0 + 1, C(f"{row.get('usd', 0)}$" if row.get("started") else "", bold=True, fill=ORANGE, h="CENTER"))
            for i, (need, money, gift) in enumerate(tiers):
                rr = r + 2 + i
                if rr > r + 3:
                    G.put(rr, b0, C(None))
                    G.put(rr, b0 + 1, C(None))
                G.put(rr, b0 + 2, C(f"{need} Hộp", h="CENTER"))
                G.put(rr, b0 + 3, C(f"{money}$", h="CENTER"))
                G.put(rr, b0 + 4, C(gift, h="CENTER"))
            result[prog] = (r + 3, b0 + 1)
        else:
            result[prog] = None
    return G, result


def build_tong(groups, titles, gids, results, stamp, scope, sep):
    G = Grid()
    head = ["STT", "MÃ KH", "KHÁCH HÀNG", "NGƯỜI PHỤ TRÁCH", "CHƯƠNG TRÌNH CHẠY", "THƯỞNG", "SỐ KHÁCH ĐK",
            "TG BẮT ĐẦU", "TG KẾT THÚC", "TIẾN ĐỘ", "ĐÃ TRẢ THƯỞNG", "TRẢ THƯỞNG CHIẾN DỊCH - CUỐI NĂM", "LINK", "NOTE"]
    for i, h in enumerate(head, 1):
        G.put(1, i, C(h, bold=True, size=10, h="CENTER", wrap=True, fill="#FFFF00" if i == 11 else None))
    G.widths.update({1: 35, 2: 120, 3: 160, 4: 140, 5: 185, 6: 160, 7: 100, 8: 95, 9: 95, 10: 80, 11: 120,
                     12: 160, 13: 160, 14: 200, 15: 130, 16: 110})
    G.groups = []                                   # (dòng đầu, dòng cuối) của từng khách để kẻ viền đậm
    r = 2
    for g in groups:
        title = titles[g["grp"]]
        rows = g["rows"] or [{}]
        r0 = r
        for row in rows:
            prog = row.get("program")
            label = LABEL.get(prog, prog or "")
            if prog == "EUN" and any(e.get("program") == "ENZ" for e in row.get("entries", [])):
                label += ", ENZYM"
            start = min((e["start_at"] for e in row.get("entries", []) if e.get("start_at")), default="")
            cell = results.get(g["grp"], {}).get(prog)
            prog_v = f"={_ref(title, *cell)}" if cell and row.get("started") else ""
            G.put(r, 5, C(label, size=10))
            G.put(r, 6, C((row.get("reward") or "").upper(), size=10))
            G.put(r, 7, C(row.get("registered") or "", size=10, h="RIGHT"))
            G.put(r, 8, C(serial(start[:10] + " 00:00:00") if start else "", size=10, num="day", h="RIGHT"))
            G.put(r, 9, C(None, size=10))
            G.put(r, 10, C(prog_v, size=10, h="RIGHT",
                           num="dec" if row.get("mode") == "ticket" else None))
            G.put(r, 11, C(row.get("paid") or "", size=10, bold=True, fill="#FFFF00"))
            G.put(r, 12, C(None, size=10))
            G.put(r, 14, C(row.get("note") or "", size=10, wrap=True))
            r += 1
        r1 = r - 1
        name = g["grp"]
        codes = ", ".join(c["code"] for c in g["customers"])
        for c, v in ((1, g["stt"] or ""), (2, codes), (3, name), (4, g["owner"] or "")):
            for rr in range(r0, r1 + 1):
                G.put(rr, c, C(v if rr == r0 else None, size=10, h="RIGHT" if c == 1 else None, wrap=c in (2, 3)))
            G.merge(r0, c, r1, c)
        for rr in range(r0, r1 + 1):
            G.put(rr, 13, C(title.split(". ", 1)[-1] if rr == r0 else None, size=10, color="#0000FF", underline=True,
                            link=f"#gid={gids[title]}"))
        G.merge(r0, 13, r1, 13)
        G.groups.append((r0, r1))
    # tổng số vé / số đô như ô bên phải bảng của kế toán
    last = max(r - 1, 2)
    G.put(2, 15, C("TỔNG SỐ VÉ", bold=True, size=10, fill=ORANGE))
    G.put(2, 16, C(f"=SUMPRODUCT(IFERROR(INT(J2:J{last})+(MOD(J2:J{last}{sep}1)*2>1){sep}0))",
                   bold=True, size=10, fill=ORANGE, num="int", h="RIGHT"))
    G.put(3, 15, C("TỔNG SỐ ĐÔ", bold=True, size=10, fill=ORANGE))
    G.put(3, 16, C(f'=SUMPRODUCT(IFERROR(VALUE(SUBSTITUTE(J2:J{last}{sep}"$"{sep}""))*REGEXMATCH(TO_TEXT(J2:J{last}){sep}"\\$"){sep}0))',
                   bold=True, size=10, fill=ORANGE, num="int", h="RIGHT"))
    G.put(5, 15, C(f"Xuất từ CRM lúc {stamp}", size=10, border=False))
    G.put(6, 15, C(scope, size=10, border=False))
    return G


THICK = {"style": "SOLID_THICK", "color": {"red": 0, "green": 0, "blue": 0}}
THIN = {"style": "SOLID", "color": {"red": 0, "green": 0, "blue": 0}}


def style_tong(c, sid, G):
    """Cho BẢNG TỔNG giống bản của kế toán: viền đậm giữa các khách, nút lọc ở dòng tiêu đề, và các ô
    chọn (người phụ trách, chương trình, thưởng) dạng nhãn màu.

    Nhãn màu của ô chọn không đặt được bằng API, nên chép tab BẢNG TỔNG từ file mẫu của kế toán
    (file "Đổ vào file kế toán" — bản sao, không phải file gốc) sang làm tab tạm, dán quy tắc chọn
    của cột D:F xuống toàn bộ bảng rồi xoá tab tạm."""
    last = max((r1 for _, r1 in G.groups), default=1)
    rng = lambda r1, r2, c1, c2: {"sheetId": TONG_ID, "startRowIndex": r1 - 1, "endRowIndex": r2,
                                  "startColumnIndex": c1 - 1, "endColumnIndex": c2}
    reqs = [{"updateBorders": {"range": rng(1, 1, 1, 14), "top": THIN, "bottom": THICK, "left": THIN, "right": THIN,
                               "innerVertical": THIN}}]
    for r0, r1 in G.groups:
        reqs.append({"updateBorders": {"range": rng(r0, r1, 1, 14), "top": THICK, "bottom": THICK, "left": THICK,
                                       "right": THICK, "innerHorizontal": THIN, "innerVertical": THIN}})
    reqs.append({"setBasicFilter": {"filter": {"range": rng(1, last, 1, 14)}}})
    _batch(c, sid, reqs, "kẻ viền bảng tổng")
    tpl = campaign_sheet.sheet_id()
    if not tpl or tpl == sid or last < 2:
        return
    try:
        m = _get(c, f"{API}/{tpl}", [("fields", "sheets.properties(sheetId,title)")])
        src = next((s["properties"]["sheetId"] for s in m.get("sheets", [])
                    if s["properties"]["title"].strip().upper() == TONG), None)
        if src is None:
            return
        r = c.post(f"{API}/{tpl}/sheets/{src}:copyTo", json={"destinationSpreadsheetId": sid})
        if r.status_code >= 400:
            log.warning("không chép được bảng tổng mẫu: %s", r.text[:200])
            return
        tmp = r.json()["sheetId"]
        src_rng = {"sheetId": tmp, "startRowIndex": 1, "endRowIndex": 2, "startColumnIndex": 3, "endColumnIndex": 6}
        _batch(c, sid, [{"copyPaste": {"source": src_rng, "destination": rng(2, last, 4, 6),
                                       "pasteType": "PASTE_DATA_VALIDATION"}},
                        {"deleteSheet": {"sheetId": tmp}}], "tô màu ô chọn")
    except ConfigError as e:
        log.warning("bỏ qua nhãn màu bảng tổng: %s", e)


def _get(c, url, params=None):
    r = c.get(url, params=params or [])
    if r.status_code == 403:
        raise ConfigError("CRM chưa có quyền sửa file xuất. Chia sẻ file cho tài khoản dịch vụ của CRM với quyền Người chỉnh sửa.")
    if r.status_code == 404:
        raise ConfigError("Không tìm thấy file Google Sheet xuất thưởng chiến dịch — kiểm tra lại đường link.")
    if r.status_code >= 400:
        raise ConfigError(f"Google từ chối khi đọc file: {r.text[:200]}")
    return r.json()


def _batch(c, sid, reqs, what, limit=3_000_000):
    """Gửi theo từng gói khoảng 3 MB để không vượt giới hạn kích thước của Google."""
    pack, size = [], 0
    for q in reqs + [None]:
        n = len(json.dumps(q, ensure_ascii=False)) if q is not None else 0
        if pack and (q is None or size + n > limit):
            r = c.post(f"{API}/{sid}:batchUpdate", json={"requests": pack})
            if r.status_code >= 400:
                raise ConfigError(f"Google từ chối khi {what}: {r.text[:300]}")
            pack, size = [], 0
        if q is not None:
            pack.append(q)
            size += n


def _vn(ts):
    """Giờ đồng bộ lưu theo giờ máy chủ (UTC) -> giờ Việt Nam."""
    try:
        return (datetime.fromisoformat(ts) + timedelta(hours=7)).strftime("%d/%m/%Y %H:%M")
    except (TypeError, ValueError):
        return ts or ""


def run(until=None, replace=False):
    sid = sheet_id()
    if not sid:
        raise ConfigError("Chưa khai báo file Google Sheet để xuất thưởng chiến dịch.")
    if sid in {(db.get_meta("gsheet_id") or "").strip(), campaign_sheet.sheet_id()}:
        raise ConfigError("File xuất phải là một file riêng, không trùng file bảng doanh số hay file đổ dữ liệu của kế toán.")
    until = campaign._until(until)
    try:
        info = gsheet.config_account()
        with gsheet._client(gsheet._token(info)) as c:
            meta = _get(c, f"{API}/{sid}", [("fields", "properties(locale,title),sheets.properties(sheetId,title,gridProperties)")])
            locale = (meta.get("properties", {}).get("locale") or "en_US").split("_")[0].lower()
            sep = ";" if locale in COMMA_DECIMAL else ","
            old = [s["properties"] for s in meta.get("sheets", [])]
            has_data = any(p["title"] != "Trang tính1" and p["title"] != "Sheet1" for p in old)
            if has_data and not replace:
                raise ConfigError("File xuất đã có dữ liệu lần trước. Tick \"ghi đè\" để dựng lại toàn bộ file.")

            d = campaign.compute(None, until)
            groups = d["groups"]
            entries = [e for e in db.rows("SELECT * FROM campaign_entries ORDER BY stt, grp, id") if e["program"] in campaign.PROGRAMS]
            cust, lines = campaign._lines(sorted({e["customer_code"] for e in entries if e["customer_code"]}))
            by_grp = {}
            for e in entries:
                by_grp.setdefault(e["grp"], []).append(e)

            used, titles, gids = {TONG}, {}, {TONG: TONG_ID}
            for i, g in enumerate(groups):
                t = _tab_title(g["stt"], g["grp"], used)
                titles[g["grp"]] = t
                gids[t] = 1000 + i
            grids, results = {}, {}
            for g in groups:
                G, res = build_customer(titles[g["grp"]], g, by_grp.get(g["grp"], []), cust, lines, until, sep)
                grids[titles[g["grp"]]] = G
                results[g["grp"]] = res
            stamp = datetime.now(timezone(timedelta(hours=7))).strftime("%d/%m/%Y %H:%M")
            scope = (f"Chỉ tính hoá đơn đến {until[8:10]}/{until[5:7]}/{until[:4]} {until[11:16]}" if until
                     else "Tính đến lần đồng bộ KiotViet " + _vn(d.get("as_of")))
            grids[TONG] = build_tong(groups, titles, gids, results, stamp, scope, sep)

            # dựng lại file: thêm tab tạm, xoá tab cũ, tạo tab mới với mã tab cố định, xoá tab tạm
            tmp = 999999
            _batch(c, sid, [{"addSheet": {"properties": {"sheetId": tmp, "title": "_dang_xuat_"}}}]
                   + [{"deleteSheet": {"sheetId": p["sheetId"]}} for p in old if p["sheetId"] != tmp], "xoá dữ liệu cũ")
            order = [TONG] + [titles[g["grp"]] for g in groups]
            adds = []
            for i, t in enumerate(order):
                G = grids[t]
                adds.append({"addSheet": {"properties": {
                    "sheetId": gids[t], "title": t, "index": i,
                    "gridProperties": {"rowCount": max(200, G.maxr + 30), "columnCount": max(26, G.maxc + 3),
                                       "frozenRowCount": 1, "frozenColumnCount": 6 if t == TONG else 0}}}})
            _batch(c, sid, adds + [{"deleteSheet": {"sheetId": tmp}}], "tạo tab")
            _batch(c, sid, [q for t in order for q in grids[t].requests(gids[t])], "ghi dữ liệu")
            style_tong(c, sid, grids[TONG])
            if meta.get("properties", {}).get("title", "").startswith(("Bảng tính không có tiêu đề", "Untitled")):
                _batch(c, sid, [{"updateSpreadsheetProperties": {"properties": {"title": "THƯỞNG CHIẾN DỊCH - XUẤT TỪ CRM"},
                                                                  "fields": "title"}}], "đặt tên file")
        log.info("đã xuất thưởng chiến dịch: %d tab", len(order))
        return {"url": f"https://docs.google.com/spreadsheets/d/{sid}/edit#gid={TONG_ID}", "tabs": len(order),
                "groups": len(groups), "until": until, "totals": d["totals"]}
    except ConfigError:
        raise
    except Exception as e:
        log.exception("xuất thưởng chiến dịch thất bại")
        raise ConfigError(f"{type(e).__name__}: {e}")
