import hashlib
from fastapi import Request, HTTPException
from itsdangerous import URLSafeSerializer, BadSignature
from . import config

signer = URLSafeSerializer(config.SECRET_KEY, salt="session")
COOKIE = "crm_session"


def _hash(p):
    return hashlib.sha256((config.SECRET_KEY + p).encode()).hexdigest()


def current_user(request: Request):
    tok = request.cookies.get(COOKIE)
    if not tok:
        raise HTTPException(401, "Chưa đăng nhập")
    try:
        return signer.loads(tok)
    except BadSignature:
        raise HTTPException(401, "Phiên không hợp lệ")
