# CRM bổ trợ KiotViet

Ứng dụng web nhỏ, tự đồng bộ dữ liệu từ KiotViet (Public API) về cơ sở dữ liệu riêng và
tính sẵn các báo cáo mà kế toán đang phải làm tay:

| Trang | Nội dung |
|---|---|
| Tổng quan | Doanh số, số hoá đơn, khách có mua, đơn hàng trong kỳ; so với kỳ trước; sinh nhật 7 ngày tới |
| Khách theo sale | Mỗi sale: tổng khách, khách mới, khách có mua, khách mất, khách ngừng mua, doanh số, so kỳ trước |
| Biến động KH | Theo tháng: khách mới / quay lại / ngừng mua, tăng giảm ròng, doanh số |
| Doanh số KH | Từng khách: doanh số kỳ này vs kỳ trước, phân nhóm (VIP, giảm mua, ngừng mua, mua lần đầu), công nợ |
| Hàng nhanh/chậm | Từng mặt hàng: SL bán, tốc độ bán/ngày, tồn kho, số ngày tồn còn, vốn đọng, phân loại nhanh/chậm/không bán |
| Hành trình đơn | Phễu đơn hàng → hoá đơn → giao hàng, thời gian xử lý, đơn đang kẹt, theo sale, theo kênh |
| Sinh nhật KH | Khách có sinh nhật trong N ngày tới, kèm sale phụ trách, SĐT, mua gần nhất |

Mọi báo cáo đều **xuất Excel** (nút ⬇ Excel), lọc theo khoảng ngày và theo sale.
Bấm vào tên khách để xem hồ sơ: lịch sử hoá đơn, hàng hay mua, doanh số theo tháng, ghi chú CRM, gán lại sale.

## Cài đặt (Windows, không cần Docker)

1. Cài Python 3.11+ từ https://www.python.org/downloads/ (tick "Add Python to PATH").
2. Giải nén thư mục này, mở file `.env` (copy từ `.env.example` nếu chưa có) và điền:
   - `KIOT_RETAILER`, `KIOT_CLIENT_ID`, `KIOT_CLIENT_SECRET` — lấy ở KiotViet: Thiết lập → Kết nối API
   - `ADMIN_USER` / `ADMIN_PASSWORD` — tài khoản quản trị CRM
   - `MONEY_UNIT=1000` nếu cửa hàng nhập giá theo nghìn đồng (như hiện tại)
3. Chạy `run.bat`. Trình duyệt sẽ mở http://localhost:8000 — đăng nhập bằng tài khoản quản trị.
4. Lần đầu CRM tự tải toàn bộ dữ liệu (khoảng 10–15 phút với ~30.000 hoá đơn), sau đó tự đồng bộ
   phần thay đổi mỗi `SYNC_INTERVAL_MIN` phút. Có thể bấm "Đồng bộ ngay" ở trang Cài đặt.

Để nhiều người cùng dùng, chạy trên 1 máy luôn bật (hoặc VPS) rồi mọi người vào `http://<ip-máy-đó>:8000`.

## Cài bằng Docker (máy chủ / VPS)

```bash
cp .env.example .env   # rồi sửa .env
docker compose up -d --build
```

## Phân quyền

- **Quản trị** (`.env`): tất cả + tạo tài khoản, bí danh sale.
- **Kế toán** (tạo ở Cài đặt): xem tất cả báo cáo, xuất Excel, gán sale.
- **Sale** (tạo ở Cài đặt, chọn tên sale tương ứng): chỉ thấy khách của mình.

## Cách CRM xác định "sale phụ trách"

Theo quy ước của cửa hàng, sale được ghi ở **đuôi tên khách** sau dấu `-`
(vd `Vip Chị Xuân - My Lybyn` → sale **My**; `Minh Phúc-TLL LB` → **TLL**).
Các từ `Lybyn`, `LB` được bỏ qua. Khách không có đuôi tên → lấy nhân viên bán nhiều hoá đơn nhất cho khách đó.
Tại Cài đặt → **Bí danh sale**, gộp các cách viết khác nhau (`Linh`, `TLL`, `Linh Lybyn`) về một tên.
Có thể gán tay từng khách trong hồ sơ khách (ưu tiên cao nhất).

Đổi cách nhận diện bằng `SALE_SOURCE` trong `.env`: `auto` (mặc định) | `name` | `group` | `created_by` | `invoice` | `manual`.

## Định nghĩa chỉ số

- **Kỳ trước** = khoảng ngày có cùng độ dài ngay trước kỳ đang chọn.
- **KH có mua** = có ít nhất 1 hoá đơn *Hoàn thành* trong kỳ. **KH mất** = có mua kỳ trước nhưng không mua kỳ này.
- **KH ngừng mua** = lần mua gần nhất cách đầu kỳ quá `CHURN_DAYS` ngày (mặc định 90).
- **Hàng nhanh (sắp hết)**: tồn < 14 ngày bán. **Chậm**: tồn > 90 ngày bán. **Tồn không bán**: còn tồn nhưng không bán trong kỳ.
- **Vốn đọng** = tồn × giá vốn (hoặc giá bán nếu không có giá vốn).
- Hoá đơn *Đã hủy* không tính vào doanh số.

## Cấu trúc

```
app/main.py      API web (FastAPI) + đăng nhập + xuất Excel
app/sync.py      Đồng bộ KiotViet → SQLite (tăng dần, tự chạy theo lịch)
app/kiot.py      Client KiotViet Public API
app/reports.py   Toàn bộ công thức báo cáo (SQL)
app/static/      Giao diện (1 file HTML + Chart.js)
data/crm.db      Cơ sở dữ liệu (SQLite) — sao lưu file này là đủ
```

## Bảo mật

- Không chia sẻ `.env` (chứa Client Secret). Nếu đã lộ, vào KiotViet → Kết nối API → Chỉnh sửa → tạo mã mới.
- Đổi `SECRET_KEY` thành chuỗi ngẫu nhiên trước khi dùng thật.
- Nếu mở ra Internet, đặt sau HTTPS (Caddy/Nginx) hoặc dùng VPN.
