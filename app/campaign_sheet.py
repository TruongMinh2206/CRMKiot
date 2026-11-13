"""Đổ hoá đơn mới từ CRM vào đúng file "BẢNG TÍNH THƯỞNG CHIẾN DỊCH" theo bố cục của kế toán.

File đó có một tab cho mỗi khách; trong tab, mỗi chương trình là một khối 5 cột
(ngày, sản phẩm, số lượng, đơn giá, tổng doanh số) kết thúc bằng dòng "Tổng", bên dưới
là bảng quy đổi thưởng. Ở đây chỉ làm đúng việc kế toán làm mỗi tuần:

* tìm khối của từng khách/chương trình,
* nối các dòng hàng CRM có sau dòng cuối cùng đang có trong khối (dòng đã đổ thì không đổ lại),
* hết chỗ thì chèn thêm dòng ngay trong khối đó, giữ nguyên các khối bên cạnh,
* cập nhật ô thưởng đô gõ tay mà BẢNG TỔNG đang trỏ tới.

Không sửa, không xoá dòng nào kế toán đã nhập.
"""
import logging
import re
from datetime import datetime, timedelta

from . import campaign, db, gsheet
from .gsheet import API, ConfigError

log = logging.getLogger("campaign_sheet")
EPOCH = datetime(1899, 12, 30)
TONG = "BẢNG TỔNG"
LABELS = {"NẠ KRLAB": "NA", "NGHỆ": "NGHE", "EUNDAN VITAMIN C": "EUN", "DEMAXILIC": "DER", "DERMAXILIC": "DER",
          "WONDER BATH": "WB", "ENZYM": "EUN", "LUMOS": "LUM", "EUNDAN VITAMIN C, ENZYM": "EUN"}
CLASSIFY = ["RET", "NA", "NGHE", "ENZ", "EUN", "DER", "WB", "LUM"]


def sheet_id():
    v = (db.get_meta("campaign_gsheet_id") or "").strip()
    m = re.search(r"/d/([A-Za-z0-9_-]{20,})", v)
    return m.group(1) if m else v


def col(n):
    s = ""
    while n > 0:
        n, m = divmod(n - 1, 26)
        s = chr(65 + m) + s
    return s


def a1(tab):
    return "'" + tab.replace("'", "''") + "'"


def isnum(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def serial(d):
    """'2026-11-12 09:40:15' -> số serial của Google Sheet."""
    dt = datetime.fromisoformat(d)
    return (dt - EPOCH).total_seconds() / 86400


def from_serial(v):
    return EPOCH + timedelta(days=float(v))


# ---------------------------------------------------------------------------
# Đọc file
# ---------------------------------------------------------------------------
def _get(c, url, params):
    r = c.get(url, params=params)
    if r.status_code == 403:
        raise ConfigError("CRM chưa có quyền sửa file này. Chia sẻ file cho tài khoản dịch vụ của CRM với quyền Người chỉnh sửa.")
    if r.status_code == 404:
        raise ConfigError("Không tìm thấy file Google Sheet thưởng chiến dịch — kiểm tra lại đường link.")
    if r.status_code >= 400:
        raise ConfigError(f"Google từ chối khi đọc file: {r.text[:200]}")
    return r.json()


def read_tabs(c, sid, titles):
    """Giá trị thô của các tab -> {title: {(cột, dòng): giá trị}}."""
    out = {}
    for i in range(0, len(titles), 20):
        part = titles[i:i + 20]
        params = [("ranges", f"{a1(t)}!A1:AN500") for t in part]
        params += [("valueRenderOption", "UNFORMATTED_VALUE"), ("dateTimeRenderOption", "SERIAL_NUMBER")]
        data = _get(c, f"{API}/{sid}/values:batchGet", params)
        for t, vr in zip(part, data.get("valueRanges", [])):
            g = {}
            for y, row in enumerate(vr.get("values", []), 1):
                for x, v in enumerate(row, 1):
                    if v != "" and v is not None:
                        g[(x, y)] = v
            out[t] = g
    return out


def read_tong(c, sid):
    """BẢNG TỔNG: mỗi dòng chương trình của mỗi khách, kèm ô mà cột TIẾN ĐỘ đang trỏ tới."""
    data = _get(c, f"{API}/{sid}/values/{a1(TONG)}!A1:N400", [("valueRenderOption", "FORMULA")])
    rows, stt = [], None
    for y, r in enumerate(data.get("values", []), 1):
        r = list(r) + [""] * (14 - len(r))
        if y == 1:
            continue
        if isnum(r[0]):
            stt = int(r[0])
        elif isinstance(r[0], str) and r[0].startswith("="):
            stt = (stt or 0) + 1            # công thức đếm STT: =IF(C2<>"",COUNTA(...))
        label = re.sub(r"\s+", " ", str(r[4] or "")).strip().upper()
        if not label or stt is None:
            continue
        ref = re.search(r"'((\d+)\.\s[^']*)'!\$?([A-Z]+)\$?(\d+)", str(r[9] or ""))
        rows.append({"stt": stt, "program": LABELS.get(label), "label": label, "row": y,
                     "tab": ref.group(1) if ref else None, "tab_no": int(ref.group(2)) if ref else None,
                     "cell": (ref.group(3), int(ref.group(4))) if ref else None})
    return rows


# ---------------------------------------------------------------------------
# Tìm các khối trong một tab
# ---------------------------------------------------------------------------
def parse_blocks(g):
    heads = sorted((k for k, v in g.items() if isinstance(v, str) and v.strip().upper().startswith("SẢN PHẨM")),
                   key=lambda k: (k[0], k[1]))
    blocks = []
    for c, r in heads:
        rows, carry, end, member, empty, last_used = [], None, None, "", 0, r
        for y in range(r + 1, r + 480):
            nm, dt, q, p = g.get((c, y)), g.get((c - 1, y)), g.get((c + 1, y)), g.get((c + 2, y))
            nms = nm.strip() if isinstance(nm, str) else ""
            dts = dt.strip() if isinstance(dt, str) else ""
            if nms.lower() == "tổng" or (dts.upper() == "TỔNG" and not nms):
                end = y
                member = dts if nms else ""
                break
            if nms.upper().startswith("SẢN PHẨM"):
                break
            # tab của team: dòng tổng không ghi "Tổng" mà ghi tên thành viên ở cột ngày
            if dts and not nms and isnum(q) and rows:
                end, member = y, dts
                break
            if nm is None and dt is None and not isnum(q):
                empty += 1
                if empty > 60:
                    break
                continue
            empty = 0
            last_used = y
            if nms.startswith("Khách dư"):
                carry = {"y": y, "q": q if isnum(q) else 0}
                continue
            if not isnum(q):
                continue
            rows.append({"y": y, "dt": dt if isnum(dt) else None, "nm": nms, "q": q, "p": p if isnum(p) else None})
        qty = {}
        for x in rows:
            for k in CLASSIFY:
                if campaign.factor(k, x["nm"]):
                    qty[k] = qty.get(k, 0) + abs(x["q"])
                    break
        prog = max(qty, key=qty.get) if qty else None
        blocks.append({"c": c, "r": r, "end": end, "member": member, "rows": rows, "carry": carry,
                       "last_used": last_used, "prog": prog, "has_enz": bool(qty.get("ENZ")),
                       "sum_q": sum(x["q"] for x in rows) + (carry["q"] if carry else 0),
                       "total_q": g.get((c + 1, end)) if end else None, "total_a": g.get((c + 3, end)) if end else None})
    return blocks


def _norm(s):
    return re.sub(r"\s+", " ", (s or "")).strip().upper()


def pick_block(blocks, program, member, grp):
    """Khối ứng với một dòng CRM (chương trình + thành viên)."""
    want = [program] + (["EUN"] if program == "ENZ" else [])
    for p in want:
        cand = [b for b in blocks if b["prog"] == p and b["end"]]
        if p == "EUN" and program == "ENZ":
            cand = [b for b in cand if b["has_enz"]] or cand
        if not cand:
            continue
        if len(cand) == 1:
            return cand[0], None
        m, gname = _norm(member), _norm(grp).replace("TEAM ", "")
        for b in cand:
            bm = _norm(b["member"])
            if bm and (bm == m or (not m and bm in _norm(grp)) or (m and (bm in m or m in bm))):
                return b, None
        for b in cand:
            bm = _norm(b["member"])
            if bm and (bm in gname or gname in bm) and (not m or m in gname or gname in m):
                return b, None
        return None, "có nhiều khối cùng chương trình, không xác định được khối của thành viên này"
    # chưa có dòng nào của chương trình này: khối trống mang đúng tên thành viên (tab của team)
    m = _norm(member)
    empty = [b for b in blocks if b["prog"] is None and b["end"] and not b["rows"]
             and m and _norm(b["member"]) and (_norm(b["member"]) == m or _norm(b["member"]) in m or m in _norm(b["member"]))]
    if len(empty) == 1:
        return empty[0], None
    return None, "tab chưa có khối cho chương trình này"


# ---------------------------------------------------------------------------
# Lập kế hoạch và ghi
# ---------------------------------------------------------------------------
def build_plan(c, sid, until=None):
    meta = _get(c, f"{API}/{sid}", [("fields", "sheets.properties(sheetId,title)")])
    tabs = {s["properties"]["title"]: s["properties"]["sheetId"] for s in meta.get("sheets", [])}
    if TONG not in tabs:
        raise ConfigError(f"File không có tab \"{TONG}\" — có đúng là file bảng tính thưởng chiến dịch không?")
    by_no = {}
    for t in tabs:
        m = re.match(r"^(\d+)\.\s", t)
        if m:
            by_no[int(m.group(1))] = t
    tong = read_tong(c, sid)
    tab_of = {}
    for r in tong:                                    # STT trên BẢNG TỔNG không phải lúc nào cũng trùng số tab
        if r["tab"] and r["stt"] not in tab_of:
            tab_of[r["stt"]] = r["tab"]

    entries = [e for e in db.rows("SELECT * FROM campaign_entries ORDER BY stt, grp, id") if e["program"] in campaign.PROGRAMS]
    cust, lines = campaign._lines(sorted({e["customer_code"] for e in entries if e["customer_code"]}))
    need_tabs = sorted({tab_of.get(e["stt"]) or by_no.get(e["stt"]) for e in entries} - {None})
    grids = read_tabs(c, sid, [t for t in need_tabs if t in tabs])
    blocks_of = {t: parse_blocks(g) for t, g in grids.items()}

    # các bản trước của hoá đơn sửa (mã gốc, .01, .02…): nếu bản trước đã nằm trong khối thì không đổ bản sửa nữa
    versions = {}
    for r in db.rows("SELECT code, purchase_date FROM invoices WHERE code IS NOT NULL"):
        versions.setdefault(r["code"].split(".")[0], []).append((r["code"], r["purchase_date"]))

    def already_poured(x, b):
        if not x.get("edited"):
            return None
        for code, d in versions.get(x["code"].split(".")[0], []):
            if code == x["code"] or not d or d >= x["d"]:
                continue
            t = serial(d)
            if any(row["dt"] is not None and abs(row["dt"] - t) < 90 / 86400 for row in b["rows"]):
                return code
        return None

    todo, skipped, edits = {}, [], []
    for e in entries:
        tab = tab_of.get(e["stt"]) or by_no.get(e["stt"])
        who = f"{e['grp']}{' / ' + e['member'] if e['member'] else ''} · {campaign.PROGRAMS[e['program']]['name']}"
        if not tab or tab not in blocks_of:
            skipped.append({"who": who, "why": "không tìm thấy tab của khách này trên file"})
            continue
        got = campaign._entry_lines(e, cust, lines, until)
        if not got:
            continue
        b, why = pick_block(blocks_of[tab], e["program"], e["member"], e["grp"])
        if not b:
            skipped.append({"who": who, "why": why, "lines": len(got)})
            continue
        dated = [x["dt"] for x in b["rows"] if x["dt"] is not None]
        if not b["rows"] and all(x["kind"] == "return" for x in got):
            continue                                   # khối chưa có đơn nào thì không đổ riêng dòng trả hàng của đợt trước
        if b["rows"] and not dated:
            skipped.append({"who": who, "why": "khối có dòng nhưng không có ngày, không biết đã đổ đến đâu", "lines": len(got)})
            continue
        if dated:                                      # chỉ lấy dòng sau phút của dòng cuối cùng đang có
            last = from_serial(max(dated)) + timedelta(seconds=0.5)
            cut = last.replace(second=59, microsecond=0).strftime("%Y-%m-%d %H:%M:%S")
            got = [x for x in got if x["d"] > cut]
        keep = []
        for x in got:
            prev = already_poured(x, b)
            if prev:
                edits.append({"who": who, "tab": tab, "code": x["code"], "prev": prev, "d": x["d"], "pn": x["pn"], "q": x["q"]})
            else:
                keep.append(x)
        got = keep
        if not got:
            continue
        key = (tab, b["c"], b["r"])
        slot = todo.setdefault(key, {"tab": tab, "block": b, "who": who, "program": e["program"], "lines": {}})
        for x in got:
            slot["lines"][(x["code"], x["pn"], x["q"], x["d"], x["kind"])] = x

    plan = []
    for (tab, _, _), slot in todo.items():
        b = slot["block"]
        new = sorted(slot["lines"].values(), key=lambda x: (x["d"], x["code"]))
        g = grids[tab]
        free = [y for y in range(b["last_used"] + 1, b["end"])
                if g.get((b["c"] - 1, y)) is None and g.get((b["c"], y)) is None and g.get((b["c"] + 1, y)) is None]
        insert = max(0, len(new) - len(free))
        # không để vùng chèn lấn sang khối bên phải
        right = min((o["c"] for o in blocks_of[tab] if o["c"] > b["c"]), default=10 ** 6)
        plan.append({"tab": tab, "gid": tabs[tab], "c": b["c"], "r": b["r"], "end": b["end"], "who": slot["who"],
                     "program": slot["program"], "member": b["member"], "free": free, "insert": insert,
                     "last_used": b["last_used"], "width_end": min(b["c"] + 5, right - 2),
                     "fix_sum": insert > 0 and isnum(b["total_q"]), "fix_sum_a": insert > 0 and isnum(b["total_a"]),
                     "old_sum": b["sum_q"], "carry": b["carry"]["q"] if b["carry"] else 0,
                     "lines": [{"d": x["d"], "code": x["code"], "pn": x["pn"], "q": x["q"],
                                "price": round(x["amt"] / x["q"]) if x["q"] else 0, "kind": x["kind"]} for x in new]})
    plan.sort(key=lambda p: (p["tab"], p["c"], -p["r"]))
    return {"tabs": tabs, "tong": tong, "plan": plan, "skipped": skipped, "edits": edits, "by_no": by_no, "tab_of": tab_of}


def _cell(v):
    if isinstance(v, str) and v.startswith("="):
        return {"userEnteredValue": {"formulaValue": v}}
    if isnum(v):
        return {"userEnteredValue": {"numberValue": v}}
    return {"userEnteredValue": {"stringValue": str(v)}}


def requests_for(p):
    """Các lệnh ghi cho một khối: chèn chỗ (nếu thiếu), chép định dạng dòng trên, điền dòng mới, sửa công thức Tổng."""
    gid, c = p["gid"], p["c"]
    reqs, n = [], len(p["lines"])
    targets = list(p["free"][:n])
    if p["insert"]:
        reqs.append({"insertRange": {"range": {"sheetId": gid, "startRowIndex": p["end"] - 1,
                                               "endRowIndex": p["end"] - 1 + p["insert"],
                                               "startColumnIndex": c - 2, "endColumnIndex": p["width_end"]},
                                     "shiftDimension": "ROWS"}})
        targets += list(range(p["end"], p["end"] + p["insert"]))
    src = p["last_used"] if p["last_used"] > p["r"] else p["r"] + 1
    runs, start = [], None
    for i, y in enumerate(targets):                   # gom các dòng liền nhau
        if start is None:
            start = y
        if i == len(targets) - 1 or targets[i + 1] != y + 1:
            runs.append((start, y))
            start = None
    for a, b in runs:
        if not (a <= src <= b):
            reqs.append({"copyPaste": {"source": {"sheetId": gid, "startRowIndex": src - 1, "endRowIndex": src,
                                                  "startColumnIndex": c - 2, "endColumnIndex": c + 3},
                                       "destination": {"sheetId": gid, "startRowIndex": a - 1, "endRowIndex": b,
                                                       "startColumnIndex": c - 2, "endColumnIndex": c + 3},
                                       "pasteType": "PASTE_FORMAT"}})
    it = iter(p["lines"])
    for a, b in runs:
        rows = []
        for y in range(a, b + 1):
            x = next(it)
            rows.append({"values": [_cell(serial(x["d"])), _cell(x["pn"]), _cell(x["q"]), _cell(x["price"]),
                                    _cell(f"={col(c + 1)}{y}*{col(c + 2)}{y}")]})
        reqs.append({"updateCells": {"rows": rows, "fields": "userEnteredValue",
                                     "start": {"sheetId": gid, "rowIndex": a - 1, "columnIndex": c - 2}}})
    if p["insert"]:
        end2, first = p["end"] + p["insert"], p["r"] + 1
        for on, cc in ((p["fix_sum"], c + 1), (p["fix_sum_a"], c + 3)):
            if on:
                reqs.append({"updateCells": {
                    "rows": [{"values": [_cell(f"=SUM({col(cc)}{first}:{col(cc)}{end2 - 1})")]}],
                    "fields": "userEnteredValue", "start": {"sheetId": gid, "rowIndex": end2 - 1, "columnIndex": cc - 1}}})
    return reqs


def usd_updates(c, sid, tong, until=None):
    """Ô thưởng đô gõ tay mà BẢNG TỔNG đang trỏ tới: đề xuất giá trị CRM tính được."""
    d = campaign.compute(None, until)
    val = {}
    for g in d["groups"]:
        for r in g["rows"]:
            if r["mode"] == "usd" and r["started"]:
                val[(g["stt"], r["program"])] = f"{r['usd']}$"
    want = [(r, val[(r["stt"], r["program"])]) for r in tong if r["cell"] and (r["stt"], r["program"]) in val]
    out = []
    for i in range(0, len(want), 40):
        part = want[i:i + 40]
        params = [("ranges", f"{a1(r['tab'])}!{r['cell'][0]}{r['cell'][1]}") for r, _ in part]
        params.append(("valueRenderOption", "FORMULA"))
        data = _get(c, f"{API}/{sid}/values:batchGet", params)
        for (r, new), vr in zip(part, data.get("valueRanges", [])):
            cur = (vr.get("values") or [[""]])[0][0] if vr.get("values") else ""
            cur_s = str(cur).strip()
            # chỉ đụng tới ô trống hoặc ô đang là số đô gõ tay; công thức và ghi chú khác thì để nguyên
            if (cur_s == "" or re.fullmatch(r"\d+\s*\$", cur_s)) and cur_s.replace(" ", "") != new and not (cur_s == "" and new == "0$"):
                out.append({"tab": r["tab"], "cell": f"{r['cell'][0]}{r['cell'][1]}", "old": cur_s, "new": new,
                            "label": r["label"]})
    return out


def run(until=None, write=False):
    sid = sheet_id()
    if not sid:
        raise ConfigError("Chưa khai báo file Google Sheet thưởng chiến dịch.")
    until = campaign._until(until)
    try:
        info = gsheet.config_account()
        tok = gsheet._token(info)
        with gsheet._client(tok) as c:
            st = build_plan(c, sid, until)
            plan = st["plan"]
            usd = usd_updates(c, sid, st["tong"], until)
            result = {"sheet_id": sid, "url": f"https://docs.google.com/spreadsheets/d/{sid}/edit", "written": False,
                      "until": until, "blocks": [{"tab": p["tab"], "who": p["who"], "add": len(p["lines"]),
                                                  "insert": p["insert"], "lines": p["lines"]} for p in plan],
                      "total_lines": sum(len(p["lines"]) for p in plan), "usd": usd, "skipped": st["skipped"],
                      "edits": st["edits"]}
            if not write or (not plan and not usd):
                return result
            reqs = [q for p in plan for q in requests_for(p)]
            if reqs:
                r = c.post(f"{API}/{sid}:batchUpdate", json={"requests": reqs})
                if r.status_code >= 400:
                    raise ConfigError(f"Google từ chối khi ghi vào file (chưa ghi gì): {r.text[:300]}")
            if usd:
                # đọc lại vị trí ô sau khi chèn dòng — công thức ở BẢNG TỔNG tự dịch theo
                usd = usd_updates(c, sid, read_tong(c, sid), until)
                if usd:
                    r = c.post(f"{API}/{sid}/values:batchUpdate", json={"valueInputOption": "RAW", "data": [
                        {"range": f"{a1(u['tab'])}!{u['cell']}", "values": [[u["new"]]]} for u in usd]})
                    if r.status_code >= 400:
                        raise ConfigError(f"Đã đổ dòng hàng nhưng chưa cập nhật được ô thưởng đô: {r.text[:200]}")
                result["usd"] = usd
            # kiểm tra lại: tổng số lượng trong khối sau khi ghi phải bằng tổng cũ + phần vừa thêm
            touched = sorted({p["tab"] for p in plan})
            grids = read_tabs(c, sid, touched)
            checks = []
            for p in plan:
                bl = [b for b in parse_blocks(grids[p["tab"]]) if b["c"] == p["c"] and b["r"] == p["r"]]
                exp = p["old_sum"] + sum(x["q"] for x in p["lines"])
                b = bl[0] if bl else None
                ok = bool(b) and abs(b["sum_q"] - exp) < 0.01 and (not isnum(b["total_q"]) or abs(b["total_q"] - exp) < 0.01)
                if not ok:
                    checks.append({"tab": p["tab"], "who": p["who"], "expected": exp,
                                   "rows_sum": b["sum_q"] if b else None, "total_cell": b["total_q"] if b else None})
            result.update(written=True, problems=checks)
            return result
    except ConfigError:
        raise
    except Exception as e:
        log.exception("đổ thưởng chiến dịch vào sheet thất bại")
        raise ConfigError(f"{type(e).__name__}: {e}")
