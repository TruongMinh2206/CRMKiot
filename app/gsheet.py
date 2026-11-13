"""Ghi bảng doanh số sang Google Sheet bằng service account.

Cách hoạt động: nhân bản tab mẫu (đã có sẵn bố cục, màu, công thức của kế toán)
rồi điền số của tháng vào 3 vùng nhập liệu. Không đụng tới tab mẫu.

Thiết lập trong Cài đặt:
* gsheet_sa  — nội dung file JSON của service account
* gsheet_id  — ID file Google Sheet (phần giữa /d/ và /edit trên URL)
* gsheet_tpl — tên tab mẫu để nhân bản
"""
import json
import logging
import time

import httpx
from urllib.parse import quote

from . import db, sheet_export, sheet_template

log = logging.getLogger("gsheet")
API = "https://sheets.googleapis.com/v4/spreadsheets"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPE = ["https://www.googleapis.com/auth/spreadsheets"]


class ConfigError(Exception):
    """Thiếu hoặc sai thiết lập — thông báo thẳng cho người dùng."""


def config():
    sa, sid = db.get_meta("gsheet_sa"), db.get_meta("gsheet_id")
    tpl = db.get_meta("gsheet_tpl") or ""
    if not sa or not sid:
        raise ConfigError("Chưa khai báo service account hoặc ID file Google Sheet trong Cài đặt.")
    try:
        info = json.loads(sa)
    except ValueError:
        raise ConfigError("Nội dung service account không phải JSON hợp lệ.")
    if not info.get("client_email") or not info.get("private_key"):
        raise ConfigError("File service account thiếu client_email hoặc private_key.")
    return info, sid.strip(), tpl.strip()


def status():
    """Tóm tắt thiết lập để hiển thị ở Cài đặt (không trả về khoá bí mật)."""
    sa = db.get_meta("gsheet_sa")
    email = ""
    if sa:
        try:
            email = json.loads(sa).get("client_email", "")
        except ValueError:
            email = "(JSON lỗi)"
    return {"configured": bool(sa and db.get_meta("gsheet_id")), "client_email": email,
            "sheet_id": db.get_meta("gsheet_id") or "", "template": db.get_meta("gsheet_tpl") or "",
            "sale_merge": db.get_meta("sale_merge") or ""}


def _token(info):
    """Đổi khoá service account lấy access token.

    Tự ký JWT rồi gọi oauth2 bằng httpx; dùng transport sẵn có của google-auth
    sẽ kéo theo thư viện requests mà dự án không cài.
    """
    try:
        from google.auth import crypt, jwt
    except ImportError as e:
        raise ConfigError(f"Thiếu thư viện google-auth trên máy chủ ({e}).")
    now = int(time.time())
    try:
        signer = crypt.RSASigner.from_service_account_info(info)
        assertion = jwt.encode(signer, {
            "iss": info["client_email"], "scope": " ".join(SCOPE),
            "aud": TOKEN_URL, "iat": now, "exp": now + 3600,
        })
    except Exception as e:
        raise ConfigError(f"Khoá service account không hợp lệ: {e}")
    if isinstance(assertion, bytes):
        assertion = assertion.decode()
    r = httpx.post(TOKEN_URL, timeout=60, data={
        "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": assertion})
    if r.status_code >= 400:
        raise ConfigError(f"Google từ chối cấp token: {r.text[:200]}")
    return r.json()["access_token"]


def _client(tok):
    return httpx.Client(timeout=90, headers={"Authorization": f"Bearer {tok}"})


def _sheets(c, sid):
    r = c.get(f"{API}/{sid}", params={"fields": "sheets.properties.sheetId,sheets.properties.title"})
    if r.status_code == 403:
        raise ConfigError("Service account chưa được chia sẻ quyền Editor trên file Google Sheet này.")
    if r.status_code == 404:
        raise ConfigError("Không tìm thấy file Google Sheet — kiểm tra lại ID.")
    r.raise_for_status()
    return {s["properties"]["title"]: s["properties"]["sheetId"] for s in r.json().get("sheets", [])}


def _batch(c, sid, requests):
    r = c.post(f"{API}/{sid}:batchUpdate", json={"requests": requests})
    if r.status_code >= 400:
        raise ConfigError(f"Google từ chối yêu cầu: {r.text[:300]}")
    return r.json()


def _default_title(from_d, to_d):
    """Trọn tháng thì đặt theo tháng; khoảng lẻ thì ghi rõ ngày."""
    import calendar
    y, m = int(from_d[:4]), int(from_d[5:7])
    last = calendar.monthrange(y, m)[1]
    if from_d[8:] == "01" and to_d[:7] == from_d[:7] and int(to_d[8:]) == last:
        return f"T{m}.{y} (CRM)"
    return f"{from_d[8:]}-{from_d[5:7]} đến {to_d[8:]}-{to_d[5:7]}.{to_d[:4]} (CRM)"


def export(from_d, to_d, basis="accounting", title=None, replace=False):
    """Tạo tab mới cho khoảng ngày từ tab mẫu rồi điền số. Trả về tên tab và link."""
    try:
        return _export(from_d, to_d, basis, title, replace)
    except ConfigError:
        raise
    except sheet_template.LayoutError as e:
        raise ConfigError(str(e))
    except Exception as e:                      # đừng để lỗi lạ thành 500 trắng màn hình
        log.exception("xuất Google Sheet thất bại")
        raise ConfigError(f"{type(e).__name__}: {e}")


def _export(from_d, to_d, basis, title, replace):
    info, sid, tpl = config()
    tok = _token(info)
    title = title or _default_title(from_d, to_d)
    with _client(tok) as c:
        tabs = _sheets(c, sid)
        if not tpl:
            raise ConfigError("Chưa chọn tab mẫu trong Cài đặt.")
        if tpl not in tabs:
            raise ConfigError(f"Không thấy tab mẫu \"{tpl}\" trong file. "
                              f"Các tab đang có: {', '.join(sorted(tabs)) or '(không có)'}")
        if title == tpl:
            raise ConfigError(f"Tab đích trùng tên tab mẫu (\"{tpl}\"). Đổi tên tab mẫu "
                              "thành tên riêng, ví dụ MẪU, rồi cập nhật lại ô Tab mẫu.")
        if title in tabs:
            if not replace:
                raise ConfigError(f"Tab \"{title}\" đã tồn tại. Chọn ghi đè hoặc đổi tên.")
            _batch(c, sid, [{"deleteSheet": {"sheetId": tabs[title]}}])
        _batch(c, sid, [{"duplicateSheet": {"sourceSheetId": tabs[tpl], "newSheetName": title,
                                            "insertSheetIndex": 0}}])

        # bố cục đọc thẳng từ tab vừa nhân bản, để người dùng chèn/bớt dòng
        # trong tab mẫu mà không phải sửa code
        g = c.get(f"{API}/{sid}/values/{quote(title)}!A1:C200",
                  params={"majorDimension": "ROWS"})
        if g.status_code >= 400:
            raise ConfigError(f"Không đọc được bố cục tab mẫu: {g.text[:200]}")
        layout = sheet_template.detect_layout(g.json().get("values") or [])
        data = [{"range": f"'{title}'!{rng}", "values": rows}
                for rng, rows in sheet_template.blocks(
                    sheet_export.collect(from_d, to_d, basis), layout)]
        r = c.post(f"{API}/{sid}/values:batchUpdate",
                   json={"valueInputOption": "USER_ENTERED", "data": data})
        if r.status_code >= 400:
            raise ConfigError(f"Google từ chối khi ghi số liệu: {r.text[:300]}")
        gid = _sheets(c, sid).get(title)
    log.info("đã xuất %s sang Google Sheet", title)
    return {"title": title, "url": f"https://docs.google.com/spreadsheets/d/{sid}/edit#gid={gid}"}


# ---------------------------------------------------------------------------
# Bảng thưởng chiến dịch
# ---------------------------------------------------------------------------
CAMPAIGN_TABS = ("THƯỞNG CHIẾN DỊCH (CRM)", "THƯỞNG CHIẾN DỊCH - CHI TIẾT (CRM)")


def export_campaign(tables, replace=False):
    """Ghi bảng tổng và bảng chi tiết vào hai tab cố định. Tab đã có thì ghi đè nội dung
    (giữ nguyên tab nên đường link cũ vẫn dùng được) khi replace=True."""
    try:
        return _export_campaign(tables, replace)
    except ConfigError:
        raise
    except Exception as e:
        log.exception("xuất thưởng chiến dịch sang Google Sheet thất bại")
        raise ConfigError(f"{type(e).__name__}: {e}")


def _safe(v):
    """Ghi theo kiểu RAW nên chuỗi không bao giờ bị hiểu thành công thức; chỉ cần bỏ None."""
    return "" if v is None else v


def _export_campaign(tables, replace):
    from datetime import datetime, timedelta, timezone
    info, sid, _ = config()
    tok = _token(info)
    t_sum, t_det = CAMPAIGN_TABS
    with _client(tok) as c:
        tabs = _sheets(c, sid)
        have = [t for t in CAMPAIGN_TABS if t in tabs]
        if have and not replace:
            raise ConfigError(f"Tab \"{have[0]}\" đã có. Tick \"ghi đè\" để thay nội dung cũ.")
        new = [t for t in CAMPAIGN_TABS if t not in tabs]
        if new:
            _batch(c, sid, [{"addSheet": {"properties": {"title": t, "index": i}}} for i, t in enumerate(new)])
            tabs = _sheets(c, sid)
        for t in have:                              # xoá nội dung cũ, giữ tab
            r = c.post(f"{API}/{sid}/values/{quote(t)}:clear", json={})
            if r.status_code >= 400:
                raise ConfigError(f"Không xoá được nội dung tab cũ: {r.text[:200]}")

        stamp = datetime.now(timezone(timedelta(hours=7))).strftime("%d/%m/%Y %H:%M")   # giờ Việt Nam
        scope = (f"chỉ tính hoá đơn đến {tables['until'][8:10]}/{tables['until'][5:7]}/{tables['until'][:4]} "
                 f"{tables['until'][11:16]}" if tables.get("until") else "tính đến lần đồng bộ KiotViet gần nhất")
        tt = tables["totals"]
        title = [[f"BẢNG THƯỞNG CHIẾN DỊCH — xuất từ CRM lúc {stamp} · {scope}"],
                 [f"Tổng vé Hàn Quốc: {tt['tickets']} (cộng lẻ {tt['tickets_exact']}) · Tổng thưởng đô: {tt['usd']}$ · "
                  f"{tt['groups']} khách/team · {tt['not_started']} dòng chưa bắt đầu"], []]
        data = [{"range": f"'{t_sum}'!A1", "values": title + [[_safe(v) for v in row] for row in tables["summary"]]},
                {"range": f"'{t_det}'!A1", "values": [[f"CHI TIẾT CÁC DÒNG HÀNG ĐƯỢC TÍNH — xuất từ CRM lúc {stamp} · {scope}"], []]
                 + [[_safe(v) for v in row] for row in tables["detail"]]}]
        r = c.post(f"{API}/{sid}/values:batchUpdate", json={"valueInputOption": "RAW", "data": data})
        if r.status_code >= 400:
            raise ConfigError(f"Google từ chối khi ghi số liệu: {r.text[:300]}")

        def fmt(tab, head_row, ncols, money_cols):
            gid = tabs[tab]
            bold = {"userEnteredFormat": {"textFormat": {"bold": True}}}
            reqs = [
                {"updateSheetProperties": {"properties": {"sheetId": gid, "gridProperties": {"frozenRowCount": head_row}},
                                           "fields": "gridProperties.frozenRowCount"}},
                {"repeatCell": {"range": {"sheetId": gid, "startRowIndex": 0, "endRowIndex": 1},
                                "cell": {"userEnteredFormat": {"textFormat": {"bold": True, "fontSize": 12}}},
                                "fields": "userEnteredFormat.textFormat"}},
                {"repeatCell": {"range": {"sheetId": gid, "startRowIndex": head_row - 1, "endRowIndex": head_row,
                                          "startColumnIndex": 0, "endColumnIndex": ncols},
                                "cell": {"userEnteredFormat": {"textFormat": {"bold": True, "foregroundColor": {"red": 1, "green": 1, "blue": 1}},
                                                               "backgroundColor": {"red": 0.12, "green": 0.44, "blue": 0.26},
                                                               "horizontalAlignment": "CENTER", "wrapStrategy": "WRAP"}},
                                "fields": "userEnteredFormat(textFormat,backgroundColor,horizontalAlignment,wrapStrategy)"}},
                {"autoResizeDimensions": {"dimensions": {"sheetId": gid, "dimension": "COLUMNS",
                                                         "startIndex": 0, "endIndex": ncols}}},
                # cột A chứa dòng tiêu đề dài nên tự giãn rất rộng; đặt lại cho vừa cột STT
                {"updateDimensionProperties": {"range": {"sheetId": gid, "dimension": "COLUMNS",
                                                         "startIndex": 0, "endIndex": 1},
                                               "properties": {"pixelSize": 55}, "fields": "pixelSize"}}]
            for col in money_cols:
                reqs.append({"repeatCell": {"range": {"sheetId": gid, "startRowIndex": head_row,
                                                      "startColumnIndex": col, "endColumnIndex": col + 1},
                                            "cell": {"userEnteredFormat": {"numberFormat": {"type": "NUMBER", "pattern": "#,##0.##"}}},
                                            "fields": "userEnteredFormat.numberFormat"}})
            return reqs

        _batch(c, sid, fmt(t_sum, 4, len(tables["summary"][0]), [8, 9, 13])
               + fmt(t_det, 3, len(tables["detail"][0]), [9, 11, 12]))
        gid = tabs[t_sum]
    log.info("đã xuất thưởng chiến dịch sang Google Sheet")
    return {"title": t_sum, "detail": t_det, "rows": len(tables["summary"]) - 1, "lines": len(tables["detail"]) - 1,
            "url": f"https://docs.google.com/spreadsheets/d/{sid}/edit#gid={gid}"}
