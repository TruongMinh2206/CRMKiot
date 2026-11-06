import os
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def env(key, default=None, cast=str):
    v = os.getenv(key)
    if v is None or v == "":
        return default
    try:
        return cast(v)
    except Exception:
        return default


KIOT_RETAILER = env("KIOT_RETAILER", "")
KIOT_CLIENT_ID = env("KIOT_CLIENT_ID", "")
KIOT_CLIENT_SECRET = env("KIOT_CLIENT_SECRET", "")
ADMIN_USER = env("ADMIN_USER", "admin")
ADMIN_PASSWORD = env("ADMIN_PASSWORD", "admin")
SECRET_KEY = env("SECRET_KEY", "change-me")
SYNC_INTERVAL_MIN = env("SYNC_INTERVAL_MIN", 30, int)
SYNC_HISTORY_DAYS = env("SYNC_HISTORY_DAYS", 730, int)
CHURN_DAYS = env("CHURN_DAYS", 90, int)
SALE_SOURCE = env("SALE_SOURCE", "auto")
DB_PATH = str(BASE_DIR / env("DB_PATH", "data/crm.db"))
Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
MONEY_UNIT = env("MONEY_UNIT", 1, float)  # 1000 nếu KiotViet nhập giá theo đơn vị nghìn đồng
