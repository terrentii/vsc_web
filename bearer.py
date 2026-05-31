import hashlib
import secrets
from functools import wraps
from flask import request, jsonify, g
from extensions import db
from models import AuthToken, User


def _hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def issue_token(login: str) -> str:
    raw = secrets.token_urlsafe(32)
    db.session.add(AuthToken(login=login, token_hash=_hash_token(raw)))
    db.session.commit()
    return raw  # сырой токен возвращаем ОДИН раз


def resolve_bearer() -> str | None:
    """Login владельца Bearer-токена из заголовка Authorization, или None."""
    h = request.headers.get('Authorization', '')
    if not h.startswith('Bearer '):
        return None
    tok = AuthToken.query.filter_by(token_hash=_hash_token(h[7:].strip())).first()
    return tok.login if tok else None


def require_bearer(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        login = resolve_bearer()
        if not login:
            return jsonify({'error': 'unauthorized'}), 401
        g.caller_login = login
        return fn(*a, **kw)
    return wrapper


def resolve_bearer_token(raw: str) -> str | None:
    """Login владельца токена по сырому значению (для ws-кадра auth), или None."""
    if not raw:
        return None
    tok = AuthToken.query.filter_by(token_hash=_hash_token(raw)).first()
    return tok.login if tok else None
