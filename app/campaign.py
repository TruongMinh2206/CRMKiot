"""Thưởng chiến dịch: tự tính tiến độ thưởng của từng khách theo hoá đơn KiotViet.

Thay cho việc kế toán xuất báo cáo "Khách theo hàng bán" mỗi tuần rồi gõ vào
sheet "BẢNG TÍNH THƯỞNG CHIẾN DỊCH". Cách tính bám theo sheet đó:

* Mỗi khách (hoặc team nhiều khách chạy chung) tham gia một hay nhiều chương trình.
* Mỗi chương trình gắn với một nhóm hàng, tính theo số lượng hoặc theo doanh số,
  và có bảng quy đổi ra thưởng (vé Hàn Quốc hoặc đô).
* Mỗi dòng có thời điểm bắt đầu riêng (khi khách chạy xong chương trình cũ) và
  phần dư mang sang từ chương trình cũ.
* Hàng tặng (dòng 0 đồng) không tính. Hàng trả bị trừ.
* Hoá đơn sửa (mã có đuôi .01, .02…) chỉ tính một lần — bản gốc đã bị huỷ trên
  KiotViet — và lấy ngày của hoá đơn gốc để xét có nằm trong chương trình không.
"""
import re
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request

from . import db
from .main_auth import current_user, is_mgr, allowed

router = APIRouter(prefix="/api")

TICKET_AMOUNT = 269_000_000
USD_EUNDAN = [(6600, 2000, "IP 18 PRM 512GB"), (2200, 500, ""), (660, 100, "")]

# match: danh sách (các chuỗi phải có trong tên hàng, các chuỗi không được có, hệ số)
PROGRAMS = {
    "NA": {"name": "Nạ Krlab", "unit": "qty", "unit_label": "set", "mode": "ticket", "per": 6600,
           "match": [(["krlab", "mặt nạ"], ["retinal"], 1), (["krlab", "mn "], ["retinal"], 1),
                     (["kr.lab", "mn "], ["retinal"], 1), (["retinal", "krlab"], [], 3)],
           "gifts": [(6600, "1 vé du lịch Hàn Quốc"), (10000, "IP 18 PRM 512GB"), (12000, "IP 18 PRM 1T")],
           "hint": "1 vé Hàn Quốc = 6.600 set · 1 hộp Retinal tính bằng 3 set"},
    "NGHE": {"name": "Nghệ", "unit": "qty", "unit_label": "hộp", "mode": "usd",
             "match": [(["tinh nghệ"], [], 1), (["thạch nghệ"], [], 1)],
             "tiers": [(2200, 2600, "IP 18 PRM 1T"), (1780, 1900, "IP 18 PRM 512GB"), (1100, 1000, "1 vé Hàn Quốc"),
                       (750, 500, ""), (220, 100, "")],
             "hint": "220 hộp = 100$ · 750 = 500$ · 1.100 = 1.000$ · 1.780 = 1.900$ · 2.200 = 2.600$"},
    "EUN": {"name": "Eundan Vitamin C", "unit": "qty", "unit_label": "hộp", "mode": "usd",
            "match": [(["eundan"], [], 1)], "tiers": USD_EUNDAN,
            "hint": "660 hộp = 100$ · 2.200 = 500$ · 6.600 = 2.000$"},
    "ENZ": {"name": "Enzym", "unit": "qty", "unit_label": "hộp", "mode": "usd",
            "match": [(["enzym"], [], 1)], "tiers": USD_EUNDAN,
            "hint": "660 hộp = 100$ · 2.200 = 500$ · 6.600 = 2.000$"},
    "DER": {"name": "Dermaxilic", "unit": "amount", "unit_label": "đ", "mode": "ticket", "per": TICKET_AMOUNT,
            "match": [(["derma"], [], 1)], "hint": "1 vé Hàn Quốc = 269 triệu doanh số"},
    "WB": {"name": "Wonder Bath", "unit": "amount", "unit_label": "đ", "mode": "ticket", "per": TICKET_AMOUNT,
           "match": [(["wonder bath"], [], 1)], "hint": "1 vé Hàn Quốc = 269 triệu doanh số"},
    "LUM": {"name": "Lumos", "unit": "amount", "unit_label": "đ", "mode": "none",
            "match": [(["lumos"], [], 1)], "hint": "Thưởng năm — chưa có bảng quy đổi"},
}
ORDER = ["NA", "NGHE", "EUN", "ENZ", "DER", "WB", "LUM"]


def factor(program, name):
    """Hệ số quy đổi của một mặt hàng trong chương trình; 0 = không thuộc chương trình."""
    n = (name or "").lower()
    for inc, exc, k in PROGRAMS[program]["match"]:
        if all(x in n for x in inc) and not any(x in n for x in exc):
            return k
    return 0


def usd_reward(total, tiers):
    """Quy đổi số lượng ra đô: lấy mốc cao nhất trước, phần còn lại quy tiếp."""
    left, usd, parts = max(0, total), 0, []      # hàng trả nhiều hơn mua thì không có thưởng, không âm
    for need, money, _ in tiers:
        k = int(left // need)
        if k:
            usd += k * money
            left -= k * need
            parts.append(f"{k} × {need:,}".replace(",", "."))
    return usd, parts, left


IMPORT_COLS = ["grp", "stt", "customer_code", "member", "program", "start_at", "end_at", "carry_qty", "carry_amount",
               "carry_note", "registered", "reward", "owner", "note", "paid", "sheet_value", "sheet_last"]


def import_entries(entries):
    """Nạp danh sách khách/chương trình (lấy từ sheet của kế toán). Dòng đã có thì bỏ qua.

    Dữ liệu khách không nằm trong mã nguồn: kho mã là công khai, nên danh sách này
    chỉ được đưa vào qua trang Thưởng chiến dịch của người quản trị.
    """
    added = skipped = 0
    with db.cursor() as con:
        have = {(r["grp"], r["customer_code"], r["program"])
                for r in con.execute("SELECT grp, customer_code, program FROM campaign_entries").fetchall()}
        for e in entries:
            key = (e.get("grp"), e.get("customer_code"), e.get("program"))
            if e.get("program") not in PROGRAMS or not e.get("customer_code") or key in have:
                skipped += 1
                continue
            have.add(key)
            con.execute(f"INSERT INTO campaign_entries({','.join(IMPORT_COLS)}) VALUES({','.join('?' * len(IMPORT_COLS))})",
                        [e.get(k) for k in IMPORT_COLS])
            added += 1
    return {"added": added, "skipped": skipped}


def _lines(codes):
    """Mọi dòng hàng có tiền của các khách tham gia, kèm ngày hiệu lực của hoá đơn."""
    if not codes:
        return {}, {}
    q = ",".join("?" * len(codes))
    cust = {r["code"]: r for r in db.rows(
        f"""SELECT c.id, c.code, c.name, COALESCE(NULLIF(c.sale_manual,''), NULLIF(c.sale_name,''), '(Chưa gán)') sale
            FROM customers c WHERE c.code IN ({q})""", tuple(codes))}
    ids = [c["id"] for c in cust.values()]
    if not ids:
        return cust, {}
    qi = ",".join("?" * len(ids))
    # hoá đơn sửa mang mã gốc + ".01"; ngày của bản gốc mới là ngày bán thật
    base = {r["code"]: r["purchase_date"] for r in db.rows(
        f"SELECT code, purchase_date FROM invoices WHERE customer_id IN ({qi})", tuple(ids))}
    out = {}
    for r in db.rows(f"""SELECT i.customer_id cid, i.code, i.purchase_date d, d.product_code pc, d.product_name pn,
            d.quantity q, d.sub_total amt FROM invoice_details d JOIN invoices i ON i.id=d.invoice_id
            WHERE i.status=1 AND i.customer_id IN ({qi}) AND d.sub_total > 0""", tuple(ids)):
        m = re.match(r"^(.+)\.(\d{2})$", r["code"] or "")
        r["edited"] = bool(m)
        r["eff"] = base.get(m.group(1), r["d"]) if m else r["d"]
        if m and r["eff"] > r["d"]:
            r["eff"] = r["d"]
        r["kind"] = "sale"
        out.setdefault(r["cid"], []).append(r)
    for r in db.rows(f"""SELECT x.customer_id cid, x.code, x.return_date d, rd.product_code pc, rd.product_name pn,
            -rd.quantity q, -rd.sub_total amt FROM return_details rd JOIN returns x ON x.id=rd.return_id
            WHERE x.customer_id IN ({qi}) AND rd.sub_total > 0""", tuple(ids)):
        r.update(edited=False, eff=r["d"], kind="return")
        out.setdefault(r["cid"], []).append(r)
    return cust, out


def _entry_lines(e, cust, lines):
    c = cust.get(e["customer_code"])
    if not c or not e["start_at"]:
        return []
    got = []
    for r in lines.get(c["id"], []):
        if r["eff"] < e["start_at"] or (e["end_at"] and r["eff"][:10] > e["end_at"][:10]):
            continue
        k = factor(e["program"], r["pn"])
        if k:
            got.append({**r, "k": k, "customer": c["name"], "member": e["member"] or ""})
    return got


def compute(user=None):
    entries = db.rows("SELECT * FROM campaign_entries ORDER BY stt, grp, id")
    cust, lines = _lines(sorted({e["customer_code"] for e in entries if e["customer_code"]}))
    groups = {}
    for e in entries:
        if e["program"] not in PROGRAMS:
            continue
        g = groups.setdefault(e["grp"], {"grp": e["grp"], "stt": e["stt"], "owner": e["owner"] or "",
                                         "customers": {}, "rows": {}})
        c = cust.get(e["customer_code"])
        g["customers"][e["customer_code"]] = c["name"] if c else "(không thấy mã này trên KiotViet)"
        if c:
            g.setdefault("sales", set()).add(c["sale"])
        P = PROGRAMS[e["program"]]
        row = g["rows"].setdefault(e["program"], {
            "program": e["program"], "name": P["name"], "unit": P["unit"], "unit_label": P["unit_label"],
            "mode": P["mode"], "hint": P["hint"], "entries": [], "qty": 0, "amount": 0, "carry": 0, "value": 0,
            "started": False, "last": None, "sheet_value": None, "sheet_last": None,
            "reward": "", "registered": "", "note": "", "paid": ""})
        got = _entry_lines(e, cust, lines)
        qty = sum(r["q"] * r["k"] for r in got)
        amount = sum(r["amt"] for r in got)
        carry = (e["carry_amount"] if P["unit"] == "amount" else e["carry_qty"]) or 0
        row["entries"].append({**{k: e[k] for k in ("id", "customer_code", "member", "start_at", "end_at", "carry_qty",
                                                    "carry_amount", "carry_note", "registered", "reward", "note", "paid")},
                               "customer": g["customers"][e["customer_code"]], "qty": qty, "amount": amount,
                               "lines": len(got)})
        row["qty"] += qty
        row["amount"] += amount
        row["carry"] += carry
        row["started"] = row["started"] or bool(e["start_at"])
        for r in got:
            if not row["last"] or r["d"] > row["last"]:
                row["last"] = r["d"]
        if e["sheet_value"] is not None:
            row["sheet_value"] = (row["sheet_value"] or 0) + e["sheet_value"]
            row["sheet_last"] = max(row["sheet_last"] or "", e["sheet_last"] or "")
        for k in ("reward", "registered", "note", "paid"):
            if e[k] and e[k] not in row[k]:
                row[k] = (row[k] + " · " if row[k] else "") + e[k]

    out = []
    for g in groups.values():
        if user is not None and not is_mgr(user) and not any(allowed(user, s) for s in g.get("sales", ())):
            continue
        rows = []
        for key in ORDER:
            row = g["rows"].get(key)
            if not row:
                continue
            P = PROGRAMS[key]
            row["value"] = (row["amount"] if P["unit"] == "amount" else row["qty"]) + row["carry"]
            v = row["value"]
            if P["mode"] == "ticket":
                v = max(0, v)
                row["tickets"] = round(v / P["per"], 3)
                row["text"] = f"{row['tickets']:.2f} vé".replace(".", ",")
                done = int(v // P["per"])
                row["next_gap"] = (done + 1) * P["per"] - v
                row["next_label"] = f"vé thứ {done + 1}"
                row["pct"] = round((v - done * P["per"]) / P["per"] * 100, 1)
                row["gift"] = next((lbl for need, lbl in reversed(P.get("gifts", [])) if v >= need), "")
            elif P["mode"] == "usd":
                usd, parts, left = usd_reward(v, P["tiers"])
                v = max(0, v)
                row["usd"] = usd
                row["text"] = f"{usd:,}$".replace(",", ".")
                row["parts"] = " + ".join(parts)
                low = P["tiers"][-1][0]
                up = next((t for t in reversed(P["tiers"]) if t[0] > v), None)
                row["next_gap"] = (up[0] - v) if up else (low - left)
                row["next_label"] = (f"mốc {up[0]:,} {P['unit_label']}".replace(",", ".") if up
                                     else f"thêm 1 mốc {low}")
                row["pct"] = round(min(100, v / up[0] * 100), 1) if up else round(left / low * 100, 1)
                row["gift"] = next((t[2] for t in P["tiers"] if v >= t[0] and t[2]), "")
            else:
                row["text"], row["next_gap"], row["next_label"], row["pct"], row["gift"] = "", None, "", None, ""
            rows.append(row)
        g["customers"] = [{"code": k, "name": v} for k, v in g["customers"].items()]
        g.pop("sales", None)
        g["rows"] = rows
        out.append(g)
    out.sort(key=lambda g: (g["stt"] or 9999, g["grp"]))

    tickets = sum(int(r["tickets"]) + (1 if r["tickets"] % 1 > 0.5 else 0)       # làm tròn như ô TỔNG SỐ VÉ của sheet
                  for g in out for r in g["rows"] if r["mode"] == "ticket")
    return {"groups": out, "programs": [{"key": k, "name": PROGRAMS[k]["name"], "hint": PROGRAMS[k]["hint"],
                                         "unit": PROGRAMS[k]["unit"]} for k in ORDER],
            "totals": {"tickets": tickets,
                       "tickets_exact": round(sum(r["tickets"] for g in out for r in g["rows"] if r["mode"] == "ticket"), 2),
                       "usd": sum(r.get("usd", 0) for g in out for r in g["rows"]),
                       "groups": len(out),
                       "not_started": sum(1 for g in out for r in g["rows"] if not r["started"])},
            "as_of": db.get_meta("last_sync")}


@router.get("/campaign")
def campaign_get(user=Depends(current_user)):
    return compute(user)


@router.get("/campaign/detail")
def campaign_detail(grp: str, program: str, user=Depends(current_user)):
    """Các dòng hàng được tính cho một khách/team trong một chương trình (như tab riêng của khách trên sheet)."""
    if program not in PROGRAMS:
        raise HTTPException(404)
    entries = db.rows("SELECT * FROM campaign_entries WHERE grp=? AND program=? ORDER BY id", (grp, program))
    cust, lines = _lines(sorted({e["customer_code"] for e in entries if e["customer_code"]}))
    if not is_mgr(user) and not any(allowed(user, c["sale"]) for c in cust.values()):
        raise HTTPException(403)
    rows = []
    for e in entries:
        if e["carry_qty"] or e["carry_amount"]:
            rows.append({"d": e["start_at"] or "", "code": "", "kind": "carry", "member": e["member"] or "",
                         "pn": "Dư từ chương trình trước" + (f" ({e['carry_note']})" if e["carry_note"] else ""),
                         "q": e["carry_qty"] or 0, "k": 1, "amt": e["carry_amount"] or 0, "edited": False})
        for r in _entry_lines(e, cust, lines):
            rows.append({k: r[k] for k in ("d", "code", "kind", "member", "pn", "q", "k", "amt", "edited", "eff")})
    rows.sort(key=lambda r: (r["kind"] != "carry", r["d"]))
    return {"grp": grp, "program": program, "name": PROGRAMS[program]["name"], "unit": PROGRAMS[program]["unit"],
            "rows": rows}


@router.post("/campaign/entry")
async def campaign_entry(request: Request, user=Depends(current_user)):
    if not is_mgr(user):
        raise HTTPException(403)
    b = await request.json()
    with db.cursor() as con:
        if b.get("delete") and b.get("id"):
            con.execute("DELETE FROM campaign_entries WHERE id=?", (b["id"],))
            return {"ok": True}
        if b.get("program") not in PROGRAMS:
            raise HTTPException(400, "Chưa chọn chương trình")
        code = (b.get("customer_code") or "").strip()
        if not con.execute("SELECT 1 FROM customers WHERE code=?", (code,)).fetchone():
            raise HTTPException(400, f"Không thấy mã khách {code} trên KiotViet")
        start = (b.get("start_at") or "").strip().replace("T", " ") or None
        if start:
            try:
                start = datetime.fromisoformat(start).strftime("%Y-%m-%d %H:%M:%S")
            except ValueError:
                raise HTTPException(400, "Ngày bắt đầu không hợp lệ")
        end = (b.get("end_at") or "").strip()[:10] or None
        vals = [(b.get("grp") or "").strip() or code, int(b.get("stt") or 0) or None, code, (b.get("member") or "").strip(),
                b["program"], start, end, float(b.get("carry_qty") or 0), float(b.get("carry_amount") or 0),
                b.get("carry_note") or "", b.get("registered") or "", b.get("reward") or "", b.get("owner") or "",
                b.get("note") or "", b.get("paid") or ""]
        cols = ["grp", "stt", "customer_code", "member", "program", "start_at", "end_at", "carry_qty", "carry_amount",
                "carry_note", "registered", "reward", "owner", "note", "paid"]
        if b.get("id"):
            con.execute(f"UPDATE campaign_entries SET {','.join(c + '=?' for c in cols)} WHERE id=?", vals + [b["id"]])
        else:
            con.execute(f"INSERT INTO campaign_entries({','.join(cols)}) VALUES({','.join('?' * len(cols))})", vals)
    return {"ok": True}


@router.post("/campaign/import")
async def campaign_import(request: Request, user=Depends(current_user)):
    if user.get("role") != "admin":
        raise HTTPException(403)
    b = await request.json()
    entries = b.get("entries") if isinstance(b, dict) else b
    if not isinstance(entries, list) or not all(isinstance(e, dict) for e in entries):
        raise HTTPException(400, "File không đúng định dạng")
    return import_entries(entries)


def export_sheets(user):
    d = compute(user)
    rows, det = [], []
    for g in d["groups"]:
        for r in g["rows"]:
            rows.append({"stt": g["stt"], "grp": g["grp"], "codes": ", ".join(c["code"] for c in g["customers"]),
                         "owner": g["owner"], "program": r["name"], "reward": r["reward"], "registered": r["registered"],
                         "start": min((e["start_at"] for e in r["entries"] if e["start_at"]), default=""),
                         "carry": r["carry"], "qty": r["qty"], "amount": r["amount"], "value": r["value"],
                         "result": r["text"] if r["started"] else "Chưa bắt đầu", "gift": r.get("gift", ""),
                         "gap": r["next_gap"], "next": r["next_label"], "last": (r["last"] or "")[:10],
                         "paid": r["paid"], "note": r["note"]})
    return rows
