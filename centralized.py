"""Blueprint central_bp — Bearer-аутентификация для десктоп-клиента (§6.1).

Маршруты:
  POST /api/auth/register
  POST /api/auth/login
  POST /api/auth/logout
  POST /api/rooms         — создать комнату (Bearer)
  GET  /api/rooms/<int:room_id>/messages — история по курсору (Bearer)

  GET /api/rooms — НЕ здесь; обрабатывается через hook в api.py:list_rooms,
  чтобы избежать коллизии маршрутов.

Никаких сессий/куки не трогаем — только Bearer-токены из AuthToken.
"""
from datetime import datetime, timezone

from flask import Blueprint, request, jsonify, g
from werkzeug.security import generate_password_hash, check_password_hash

from extensions import db
from ws_centralized import fanout_message
from models import User, AuthToken, Room, RoomMember, Message
from bearer import issue_token, _hash_token, require_bearer
from auth import (
    LOGIN_RE, ANON_RE,
    _ensure_personal_room,
    _is_rate_limited, _record_attempt, _clear_attempts,
    _get_ip,
)
from rooms import _generate_room_id

central_bp = Blueprint('centralized', __name__)


# ── Bearer helpers ─────────────────────────────────────────────────────────────

def _room_updated_at(room: Room) -> str:
    """Возвращает isoformat последнего сообщения комнаты или created_at."""
    last_ts = (
        db.session.query(db.func.max(Message.timestamp))
        .filter(Message.room_id == room.room_id)
        .scalar()
    )
    return (last_ts or room.created_at).isoformat()


def _room_dict(room: Room) -> dict:
    """Сериализует комнату в формат Bearer-ответа."""
    return {
        'id': room.id,
        'name': room.name if room.name else None,
        'is_direct': room.personal_login is not None,
        'updated_at': _room_updated_at(room),
    }


def bearer_list_rooms(login: str):
    """Список комнат, в которых состоит пользователь (Bearer-ответ)."""
    member_rows = RoomMember.query.filter_by(login=login).all()
    str_room_ids = [m.room_id for m in member_rows]
    if not str_room_ids:
        return jsonify({'rooms': []})
    rooms = Room.query.filter(Room.room_id.in_(str_room_ids)).all()
    return jsonify({'rooms': [_room_dict(r) for r in rooms]})


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


# ── Rooms (Bearer) ─────────────────────────────────────────────────────────────

@central_bp.post('/rooms')
@require_bearer
def api_create_room():
    """Создать комнату. Тело: {"name": "..."}  (опционально, ≤64 символов)."""
    caller = g.caller_login
    data = request.get_json(silent=True) or {}
    name = (data.get('name') or '').strip()[:64] or ''

    room_id = _generate_room_id()
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    room = Room(
        room_id=room_id,
        name=name,
        is_open=True,
        created_at=now,
        creator_login=caller,
    )
    db.session.add(room)

    member = RoomMember(room_id=room_id, login=caller, joined_at=now, role='godfather')
    db.session.add(member)

    db.session.commit()

    return jsonify(_room_dict(room)), 201


# ── Messages (Bearer) ──────────────────────────────────────────────────────────

@central_bp.post('/messages')
@require_bearer
def api_post_message():
    """Отправить сообщение в комнату. Идемпотентно по client_msg_id.

    Тело: {"room_id": <int>, "body": "...", "client_msg_id": "<uuid hex>"}
    """
    caller = g.caller_login
    data = request.get_json(silent=True) or {}

    room_id = data.get('room_id')
    body = data.get('body', '')
    client_msg_id = data.get('client_msg_id')

    room = db.session.get(Room, room_id)
    if room is None:
        return jsonify({'error': 'not_found'}), 404

    member = RoomMember.query.filter_by(room_id=room.room_id, login=caller).first()
    if not member:
        return jsonify({'error': 'forbidden'}), 403

    # Идемпотентность: вернуть существующее сообщение, если client_msg_id уже есть
    if client_msg_id:
        existing = Message.query.filter_by(
            room_id=room.room_id, client_msg_id=client_msg_id
        ).first()
        if existing:
            m = existing
            return jsonify({
                'id': m.id,
                'room_id': room.id,
                'sender': m.author,
                'body': m.text,
                'created_at': m.timestamp.isoformat(),
                'client_msg_id': m.client_msg_id,
            }), 200

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    m = Message(
        room_id=room.room_id,
        author=caller,
        text=body,
        timestamp=now,
        client_msg_id=client_msg_id,
    )
    db.session.add(m)
    db.session.commit()

    fanout_message(room.id, {
        "id": m.id,
        "sender": m.author,
        "body": m.text,
        "created_at": m.timestamp.isoformat(),
    })

    return jsonify({
        'id': m.id,
        'room_id': room.id,
        'sender': m.author,
        'body': m.text,
        'created_at': m.timestamp.isoformat(),
        'client_msg_id': m.client_msg_id,
    }), 200


@central_bp.get('/rooms/<int:room_id>/messages')
@require_bearer
def api_room_messages(room_id: int):
    """История сообщений комнаты с курсорной пагинацией.

    Query params:
      after=<Message.id>  — вернуть сообщения строго после этого id (курсор)
      limit=<n>           — кол-во сообщений, default 200, max 200
    """
    caller = g.caller_login

    room = db.session.get(Room, room_id)
    if room is None:
        return jsonify({'error': 'not_found'}), 404

    member = RoomMember.query.filter_by(room_id=room.room_id, login=caller).first()
    if not member:
        return jsonify({'error': 'forbidden'}), 403

    # Параметры пагинации
    try:
        limit = min(int(request.args.get('limit', 200)), 200)
        if limit <= 0:
            limit = 200
    except (ValueError, TypeError):
        limit = 200

    after_id = request.args.get('after', type=int)  # None если отсутствует

    # Запрашиваем limit+1 строк чтобы определить наличие следующей страницы
    q = Message.query.filter(Message.room_id == room.room_id)
    if after_id is not None:
        q = q.filter(Message.id > after_id)
    q = q.order_by(Message.id)
    rows = q.limit(limit + 1).all()

    has_more = len(rows) > limit
    page = rows[:limit]

    next_cursor = page[-1].id if has_more and page else None

    messages = [
        {
            'id': m.id,
            'room_id': room.id,          # int — десктоп-клиент знает int id
            'sender': m.author,
            'body': m.text,
            'created_at': m.timestamp.isoformat(),
        }
        for m in page
    ]

    return jsonify({'messages': messages, 'next_cursor': next_cursor})
