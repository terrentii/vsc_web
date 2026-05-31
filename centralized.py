"""Blueprint central_bp — Bearer-аутентификация для десктоп-клиента (§6.1).

Маршруты:
  POST /api/auth/register
  POST /api/auth/login
  POST /api/auth/logout

Никаких сессий/куки не трогаем — только Bearer-токены из AuthToken.
"""
from flask import Blueprint, request, jsonify
from werkzeug.security import generate_password_hash, check_password_hash

from extensions import db
from models import User, AuthToken
from bearer import issue_token, _hash_token
from auth import (
    LOGIN_RE, ANON_RE,
    _ensure_personal_room,
    _is_rate_limited, _record_attempt, _clear_attempts,
    _get_ip,
)

central_bp = Blueprint('centralized', __name__)


@central_bp.post('/auth/register')
def api_register():
    data = request.get_json(silent=True) or {}
    username = data.get('username', '')
    password = data.get('password', '')

    # Валидация имени пользователя
    if not LOGIN_RE.match(username):
        return jsonify({'error': 'invalid_username'}), 400
    if ANON_RE.match(username):
        return jsonify({'error': 'invalid_username'}), 400

    # Валидация пароля
    if len(password) < 4:
        return jsonify({'error': 'weak_password'}), 400

    # Проверка дубликата
    if User.query.filter_by(login=username).first():
        return jsonify({'error': 'username_taken'}), 409

    # Создаём пользователя
    user = User(login=username, password_hash=generate_password_hash(password))
    db.session.add(user)
    db.session.commit()

    _ensure_personal_room(username)
    raw = issue_token(username)

    return jsonify({
        'token': raw,
        'user': {'id': user.id, 'username': user.login},
    }), 201


@central_bp.post('/auth/login')
def api_login():
    ip = _get_ip()

    if _is_rate_limited(ip):
        return jsonify({'error': 'rate_limited'}), 429

    data = request.get_json(silent=True) or {}
    username = data.get('username', '')
    password = data.get('password', '')

    user = User.query.filter_by(login=username).first()
    if not user or not check_password_hash(user.password_hash, password):
        _record_attempt(ip)
        return jsonify({'error': 'invalid_credentials'}), 401

    _clear_attempts(ip)
    _ensure_personal_room(username)
    raw = issue_token(username)

    return jsonify({
        'token': raw,
        'user': {'id': user.id, 'username': user.login},
    }), 200


@central_bp.post('/auth/logout')
def api_logout():
    auth_header = request.headers.get('Authorization', '')
    if not auth_header.startswith('Bearer '):
        return jsonify({'error': 'unauthorized'}), 401

    raw = auth_header[7:].strip()
    tok = AuthToken.query.filter_by(token_hash=_hash_token(raw)).first()
    if not tok:
        return jsonify({'error': 'unauthorized'}), 401

    db.session.delete(tok)
    db.session.commit()
    return '', 204
