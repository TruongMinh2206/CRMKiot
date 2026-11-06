"""KiotViet Public API client (https://www.kiotviet.vn/huong-dan-su-dung-public-api/)."""
import time
import logging
import httpx
from . import config

log = logging.getLogger("kiot")
TOKEN_URL = "https://id.kiotviet.vn/connect/token"
BASE_URL = "https://public.kiotapi.com"


class KiotClient:
    def __init__(self, retailer=None, client_id=None, client_secret=None):
        self.retailer = retailer or config.KIOT_RETAILER
        self.client_id = client_id or config.KIOT_CLIENT_ID
        self.client_secret = client_secret or config.KIOT_CLIENT_SECRET
        self._token = None
        self._token_exp = 0
        self.http = httpx.Client(timeout=60)

    # ---- auth -------------------------------------------------------------
    def token(self):
        if self._token and time.time() < self._token_exp - 120:
            return self._token
        r = self.http.post(TOKEN_URL, data={
            "scopes": "PublicApi",
            "grant_type": "client_credentials",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
        })
        r.raise_for_status()
        j = r.json()
        self._token = j["access_token"]
        self._token_exp = time.time() + int(j.get("expires_in", 3600))
        return self._token

    def _headers(self):
        return {"Retailer": self.retailer, "Authorization": f"Bearer {self.token()}"}

    def get(self, path, params=None, retries=6):
        url = f"{BASE_URL}/{path.lstrip('/')}"
        for attempt in range(retries):
            try:
                r = self.http.get(url, params=params, headers=self._headers())
            except (httpx.TransportError, httpx.RemoteProtocolError) as e:
                wait = 3 * (attempt + 1)
                log.warning("KiotViet %s -> %s, retry in %ss", path, e, wait)
                time.sleep(wait)
                continue
            if r.status_code == 429 or r.status_code >= 500:
                wait = 2 ** attempt
                log.warning("KiotViet %s -> %s, retry in %ss", path, r.status_code, wait)
                time.sleep(wait)
                continue
            if r.status_code == 401:
                self._token = None
                continue
            r.raise_for_status()
            return r.json()
        r.raise_for_status()

    # ---- paging -----------------------------------------------------------
    def iter_pages(self, path, params=None, page_size=100, sleep=0.25):
        """Yield items across pages using pageSize/currentItem."""
        params = dict(params or {})
        current = 0
        while True:
            params.update({"pageSize": page_size, "currentItem": current})
            j = self.get(path, params)
            data = j.get("data") or []
            for item in data:
                yield item
            total = j.get("total")
            current += len(data)
            if not data or (total is not None and current >= total) or len(data) < page_size:
                break
            time.sleep(sleep)

    # ---- resources --------------------------------------------------------
    def branches(self):
        return self.get("branches").get("data", [])

    def users(self):
        return self.get("users").get("data", [])

    def customers(self, last_modified_from=None):
        p = {"includeTotal": "true", "includeCustomerGroup": "true", "orderBy": "modifiedDate", "orderDirection": "ASC"}
        if last_modified_from:
            p["lastModifiedFrom"] = last_modified_from
        return self.iter_pages("customers", p)

    def customer(self, cid):
        return self.get(f"customers/{cid}")

    def customer_groups(self):
        return self.get("customers/group").get("data", [])

    def products(self, last_modified_from=None):
        p = {"includeInventory": "true", "includeTotal": "true", "orderBy": "modifiedDate", "orderDirection": "ASC"}
        if last_modified_from:
            p["lastModifiedFrom"] = last_modified_from
        return self.iter_pages("products", p)

    def invoices(self, from_date=None, to_date=None, last_modified_from=None):
        p = {"includeTotal": "true", "includePayment": "true", "includeInvoiceDelivery": "true",
             "orderBy": "modifiedDate", "orderDirection": "ASC"}
        if last_modified_from:
            p["lastModifiedFrom"] = last_modified_from
        if from_date:
            p["fromPurchaseDate"] = from_date
        if to_date:
            p["toPurchaseDate"] = to_date
        return self.iter_pages("invoices", p)

    def returns(self, from_date=None, to_date=None, last_modified_from=None):
        p = {"includeTotal": "true", "orderBy": "modifiedDate", "orderDirection": "ASC"}
        if last_modified_from:
            p["lastModifiedFrom"] = last_modified_from
        if from_date:
            p["fromReturnDate"] = from_date
        if to_date:
            p["toReturnDate"] = to_date
        return self.iter_pages("returns", p)

    def orders(self, from_date=None, to_date=None, last_modified_from=None):
        p = {"includeTotal": "true", "includePayment": "true", "includeOrderDelivery": "true",
             "orderBy": "modifiedDate", "orderDirection": "ASC"}
        if last_modified_from:
            p["lastModifiedFrom"] = last_modified_from
        if from_date:
            p["fromPurchaseDate"] = from_date
        if to_date:
            p["toPurchaseDate"] = to_date
        return self.iter_pages("orders", p)
