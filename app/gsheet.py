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


def export(ym, basis="accounting", title=None, replace=False):
    """Tạo tab mới cho tháng ym từ tab mẫu rồi điền số. Trả về tên tab và link."""
    try:
        return _export(ym, basis, title, replace)
    except ConfigError:
        raise
    except Exception as e:                      # đừng để lỗi lạ thành 500 trắng màn hình
        log.exception("xuất Google Sheet thất bại")
        raise ConfigError(f"{type(e).__name__}: {e}")


def _export(ym, basis, title, replace):
    info, sid, tpl = config()
    tok = _token(info)
    title = title or f"T{int(ym[5:7])}.{ym[:4]} (CRM)"
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

        data = [{"range": f"'{title}'!{rng}", "values": rows}
                for rng, rows in sheet_template.blocks(sheet_export.collect(ym, basis))]
        r = c.post(f"{API}/{sid}/values:batchUpdate",
                   json={"valueInputOption": "USER_ENTERED", "data": data})
        if r.status_code >= 400:
            raise ConfigError(f"Google từ chối khi ghi số liệu: {r.text[:300]}")
        gid = _sheets(c, sid).get(title)
    log.info("đã xuất %s sang Google Sheet", title)
    return {"title": title, "url": f"https://docs.google.com/spreadsheets/d/{sid}/edit#gid={gid}"}
