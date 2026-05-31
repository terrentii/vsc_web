"""Токен + REST API для десктоп-клиента mys_centralized (под-проект №6).

Bearer-авторизация (см. bearer.py), без cookie/CSRF. Контракт зафиксирован в
docs/superpowers/specs/2026-05-30-centralized.md §5–§6; клиентские кодеки —
src/mys_centralized/api_client.py. Имена полей контракта (username/sender/body/
created_at, целочисленный room_id=Room.id) маппятся на модели сервера (login/
author/text/timestamp, строковый Message.room_id) — см. §4 инструкции.

Существующие маршруты /api/* и веб-версия не трогаются: новые маршруты —
дополнительно, blueprint освобождён от CSRF в app.py.
"""
from datetime import datetime, timezone

from flask import Blueprint, request, jsonify, g
from werkzeug.security import generate_password_hash, check_password_hash

from extensions import db
from models import User, Room, Message, RoomMember
from bearer import issue_token, require_bearer, revoke_bearer
from auth import (
    LOGIN_RE, ANON_RE, _ensure_personal_room,
    _get_ip, _is_rate_limited, _record_attempt, _clear_attempts,
)

central_bp = Blueprint('centralized', __name__)


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ── Маппинг моделей в контракт (§4) ─────────────────────────────────────────────

def _bearer_can_access(room: Room, login: str) -> bool:
    """Доступ Bearer-пользователя к комнате."""
    if room.personal_login is not None:
        return room.personal_login == login   # своя «Избранное»
    if RoomMember.query.filter_by(room_id=room.room_id, login=login).first():
        return True
    return bool(room.is_open)


def _room_dict(room: Room) -> dict:
    last = (
        db.session.query(db.func.max(Message.timestamp))
        .filter(Message.room_id == room.room_id)
        .scalar()
    )
    updated = last or room.created_at
    return {
        'id': room.id,                                  # клиент маршрутизирует по Room.id
        'name': room.name or None,
        'is_direct': room.personal_login is not None,
        'updated_at': updated.isoformat() if updated else None,
    }


def _msg_dict(m: Message, room_db_id: int, *, with_client: bool = False) -> dict:
    d = {
        'id': m.id,
        'room_id': room_db_id,                          # Room.id (целое), НЕ строковый Message.room_id
        'sender': m.author,
        'body': m.text,
        'created_at': m.timestamp.isoformat(),
    }
    if with_client:
        d['client_msg_id'] = m.client_msg_id
    return d


def member_rooms_payload(login: str) -> dict:
    """{"rooms":[...]} — комнаты, где login участник (включая личную). Используется
    и здесь, и из ветки Bearer в api.list_rooms (один URL /api/rooms)."""
    rids = [m.room_id for m in RoomMember.query.filter_by(login=login).all()]
    rooms = Room.query.filter(Room.room_id.in_(rids)).all() if rids else []
    rooms.sort(key=lambda r: r.created_at or datetime.min, reverse=True)
    return {'rooms': [_room_dict(r) for r in rooms]}


# ── Аутентификация (§6.1) ───────────────────────────────────────────────────────

@central_bp.route('/auth/register', methods=['POST'])
def auth_register():
    data = request.get_json(silent=True) or {}
    login = (data.get('username') or '').strip()
    password = (data.get('password') or '').strip()

    if not login or not password:
        return jsonify({'error': 'invalid_request'}), 400
    if not LOGIN_RE.match(login) or ANON_RE.match(login):
        return jsonify({'error': 'invalid_username'}), 400
    if len(password) < 4:
        return jsonify({'error': 'weak_password'}), 400
    if User.query.filter_by(login=login).first():
        return jsonify({'error': 'username_taken'}), 409

    user = User(login=login, password_hash=generate_password_hash(password))
    db.session.add(user)
    db.session.commit()
    _ensure_personal_room(login)
    token = issue_token(login)
    return jsonify({'token': token, 'user': {'id': user.id, 'username': user.login}}), 201


@central_bp.route('/auth/login', methods=['POST'])
def auth_login():
    ip = _get_ip()
    if _is_rate_limited(ip):
        return jsonify({'error': 'rate_limited'}), 429

    data = request.get_json(silent=True) or {}
    login = (data.get('username') or '').strip()
    password = (data.get('password') or '').strip()

    user = User.query.filter_by(login=login).first()
    if not user or not check_password_hash(user.password_hash, password):
        _record_attempt(ip)
        return jsonify({'error': 'invalid_credentials'}), 401

    _clear_attempts(ip)
    _ensure_personal_room(login)
    token = issue_token(login)
    return jsonify({'token': token, 'user': {'id': user.id, 'username': user.login}}), 200


@central_bp.route('/auth/logout', methods=['POST'])
@require_bearer
def auth_logout():
    revoke_bearer()
    return '', 204


# ── Комнаты (§6.2) ──────────────────────────────────────────────────────────────
# GET /api/rooms обслуживается в api.list_rooms (один URL): при наличии валидного
# Bearer-токена там вызывается member_rooms_payload() и возвращается {"rooms":[...]}.
# Дублировать правило здесь нельзя — api_bp регистрируется первым и перехватил бы.


# ── История (§6.3) ──────────────────────────────────────────────────────────────

@central_bp.route('/rooms/<int:room_id>/messages', methods=['GET'])
@require_bearer
def get_messages(room_id):
    room = db.session.get(Room, room_id)
    if not room:
        return jsonify({'error': 'not_found'}), 404
    if not _bearer_can_access(room, g.caller_login):
        return jsonify({'error': 'forbidden'}), 403

    after = request.args.get('after', type=int)
    limit = request.args.get('limit', default=200, type=int)
    limit = max(1, min(limit, 200))

    q = Message.query.filter(Message.room_id == room.room_id)
    if after is not None:
        q = q.filter(Message.id > after)
    rows = q.order_by(Message.id).limit(limit + 1).all()
    has_more = len(rows) > limit
    rows = rows[:limit]
    next_cursor = rows[-1].id if (has_more and rows) else None

    return jsonify({
        'messages': [_msg_dict(m, room.id) for m in rows],
        'next_cursor': next_cursor,
    })


# ── Отправка, идемпотентная (§6.4) ──────────────────────────────────────────────

@central_bp.route('/messages', methods=['POST'])
@require_bearer
def post_message():
    data = request.get_json(silent=True) or {}
    room_id = data.get('room_id')
    body = (data.get('body') or '').strip()[:4000]
    client_msg_id = (data.get('client_msg_id') or None)

    if not isinstance(room_id, int):
        return jsonify({'error': 'invalid_request'}), 400
    room = db.session.get(Room, room_id)
    if not room:
        return jsonify({'error': 'not_found'}), 404
    if not _bearer_can_access(room, g.caller_login):
        return jsonify({'error': 'forbidden'}), 403
    if not body:
        return jsonify({'error': 'empty_body'}), 400

    caller = g.caller_login

    # Идемпотентность: тот же (room.room_id, client_msg_id) → вернуть ту же запись.
    if client_msg_id:
        existing = Message.query.filter_by(
            room_id=room.room_id, client_msg_id=client_msg_id
        ).first()
        if existing:
            return jsonify(_msg_dict(existing, room.id, with_client=True)), 200

    msg = Message(
        room_id=room.room_id,
        author=caller,
        text=body,
        timestamp=_utcnow(),
        client_msg_id=client_msg_id,
    )
    db.session.add(msg)
    db.session.commit()

    payload = _msg_dict(msg, room.id)
    # Фан-аут в сырой /ws (десктоп) и Socket.IO (веб-UI).
    from ws_centralized import fanout_message
    fanout_message(room.id, payload)

    from extensions import socketio
    msg_index = Message.query.filter_by(room_id=room.room_id).order_by(Message.id).count()
    socketio.emit('new_message', {
        'index': msg_index,
        'author': caller,
        'timestamp': msg.timestamp.isoformat(),
        'text': body,
        'reply_to': '',
        'media': '',
        'room_id': room.room_id,
    }, room=room.room_id)

    return jsonify(_msg_dict(msg, room.id, with_client=True)), 200
