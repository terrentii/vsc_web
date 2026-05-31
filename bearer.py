"""Bearer-токен авторизация для десктоп-клиента (под-проект №6).

Opaque-токены: на руки клиенту отдаётся сырой токен один раз, в БД хранится
только sha256-хэш (как ApiKey.key_hash). Логаут удаляет запись из auth_tokens.
Эти эндпоинты НЕ используют cookie-сессию и CSRF — только заголовок
`Authorization: Bearer <token>`.
"""
import hashlib
import secrets
from functools import wraps

from flask import request, jsonify, g

from extensions import db
from models import AuthToken


def _hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def issue_token(login: str) -> str:
    """Выпускает новый токен для login. Сырой токен возвращается ОДИН раз."""
    raw = secrets.token_urlsafe(32)
    db.session.add(AuthToken(login=login, token_hash=_hash_token(raw)))
    db.session.commit()
    return raw


def resolve_bearer_token(raw: str) -> str | None:
    """Login владельца сырого токена или None (для WS-кадра auth)."""
    if not raw:
        return None
    tok = AuthToken.query.filter_by(token_hash=_hash_token(raw.strip())).first()
    return tok.login if tok else None


def resolve_bearer() -> str | None:
    """Login владельца Bearer-токена из заголовка Authorization или None."""
    h = request.headers.get('Authorization', '')
    if not h.startswith('Bearer '):
        return None
    return resolve_bearer_token(h[7:])


def revoke_bearer() -> bool:
    """Удаляет текущий Bearer-токен (логаут). True если что-то удалили."""
    h = request.headers.get('Authorization', '')
    if not h.startswith('Bearer '):
        return False
    tok = AuthToken.query.filter_by(token_hash=_hash_token(h[7:].strip())).first()
    if not tok:
        return False
    db.session.delete(tok)
    db.session.commit()
    return True


def require_bearer(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        login = resolve_bearer()
        if not login:
            return jsonify({'error': 'unauthorized'}), 401
        g.caller_login = login
        return fn(*a, **kw)
    return wrapper
