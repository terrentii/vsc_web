"""
REST API Blueprint
-----------------
GET  /api/rooms                   — список открытых комнат
GET  /api/room/<room_id>/messages — сообщения комнаты (?after=N)
POST /api/room/<room_id>/message  — отправить сообщение (JSON: {"text": "..."})

POST /api/keys                    — создать API-ключ (требует сессии зарег. пользователя)
GET  /api/keys                    — список своих ключей
DELETE /api/keys/<int:key_id>     — удалить ключ

Аутентификация: заголовок X-Api-Key: vsc_<token>
"""
import hashlib
import os
import secrets
import threading
import uuid
from datetime import datetime, timezone

from flask import Blueprint, jsonify, request, session
from werkzeug.utils import secure_filename
from flask_wtf.csrf import validate_csrf, CSRFError
from wtforms.validators import ValidationError

from extensions import db
from models import ApiKey, Room, Message, RoomMember

api_bp = Blueprint('api', __name__)

# ── Helpers ───────────────────────────────────────────────────────────────────

def _hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def _resolve_api_key() -> str | None:
    """Возвращает login владельца ключа или None."""
    raw = request.headers.get('X-Api-Key', '').strip()
    if not raw.startswith('vsc_'):
        return None
    h = _hash_key(raw)
    key = ApiKey.query.filter_by(key_hash=h).first()
    return key.login if key else None


def _require_csrf_unless_apikey() -> bool:
    """CSRF-проверка для запросов с session-cookie. Возвращает True если ок."""
    # Machine-to-machine — API-ключ. Браузер с cookie — нужен CSRF-токен.
    if _resolve_api_key():
        return True
    token = (
        request.headers.get('X-CSRFToken')
        or request.headers.get('X-CSRF-Token')
        or (request.get_json(silent=True) or {}).get('csrf_token')
        or request.form.get('csrf_token')
    )
    try:
        validate_csrf(token)
        return True
    except (CSRFError, ValidationError):
        return False


def _get_caller() -> str | None:
    """Login из API-ключа или из сессии (браузер)."""
    via_key = _resolve_api_key()
    if via_key:
        return via_key
    if session.get('user_type') == 'registered':
        return session.get('login')
    if session.get('user_type') == 'anon':
        return session.get('anon_id')
    return None


def _can_access(room: Room, login: str | None) -> bool:
    if room.personal_login:
        return False  # личные комнаты недоступны через API
    if room.is_open:
        return True
    if not login:
        return False
    return RoomMember.query.filter_by(
        room_id=room.room_id, login=login
    ).first() is not None


# ── Rooms ─────────────────────────────────────────────────────────────────────

@api_bp.route('/rooms/tg')
def list_rooms_tg():
    """Список комнат, разрешённых к показу в Telegram-боте.

    Требует X-Api-Key. Возвращает только открытые комнаты с tg_visible=True.
    Поле last_message_at отсутствует в модели — отдаём created_at для сортировки.
    """
    if not _resolve_api_key():
        return jsonify({'error': 'API key required'}), 401

    rooms = (
        Room.query
        .filter_by(is_open=True, tg_visible=True)
        .filter(Room.personal_login.is_(None))
        .order_by(Room.created_at.desc())
        .all()
    )
    return jsonify([
        {
            'room_id': r.room_id,
            'name': r.name or r.room_id,
            'created_at': r.created_at.isoformat(),
        }
        for r in rooms
    ])


@api_bp.route('/rooms')
def list_rooms():
    rooms = (
        Room.query
        .filter_by(is_open=True)
        .filter(Room.personal_login.is_(None))
        .order_by(Room.created_at.desc())
        .limit(50)
        .all()
    )
    return jsonify([
        {
            'room_id': r.room_id,
            'name': r.name or r.room_id,
            'created_at': r.created_at.isoformat(),
        }
        for r in rooms
    ])


@api_bp.route('/room/<room_id>/messages')
def get_messages(room_id):
    room = Room.query.filter_by(room_id=room_id).first()
    if not room:
        return jsonify({'error': 'Room not found'}), 404

    caller = _get_caller()
    if not _can_access(room, caller):
        return jsonify({'error': 'Access denied'}), 403

    after = max(0, request.args.get('after', 0, type=int))
    messages = (
        Message.query
        .filter_by(room_id=room_id)
        .order_by(Message.id)
        .offset(after)
        .limit(200)
        .all()
    )
    return jsonify([
        {
            'id': m.id,
            'author': m.author,
            'text': m.text,
            'timestamp': m.timestamp.isoformat(),
            'reply_to': m.reply_to,
            'media': m.media,
        }
        for m in messages
    ])


@api_bp.route('/room/<room_id>/message', methods=['POST'])
def post_message(room_id):
    if not _require_csrf_unless_apikey():
        return jsonify({'error': 'CSRF token missing or invalid'}), 400

    room = Room.query.filter_by(room_id=room_id).first()
    if not room:
        return jsonify({'error': 'Room not found'}), 404

    caller = _get_caller()
    if not caller or not _can_access(room, caller):
        return jsonify({'error': 'Access denied'}), 403

    data = request.get_json(silent=True) or {}
    text = (data.get('text') or '').strip()[:4000]
    media = (data.get('media') or '').strip()[:256]

    if not text and not media:
        return jsonify({'error': 'text or media is required'}), 400

    # X-Bot-Author: бот указывает alias-имя отправителя.
    # Запрещаем выдавать себя за существующий логин или за Anon<N> —
    # это закрыло бы impersonation любого пользователя через API-ключ.
    bot_author = request.headers.get('X-Bot-Author', '').strip()[:64]
    if bot_author and _resolve_api_key():
        from models import User as UserModel
        looks_like_anon = bot_author.lower().startswith('anon') and bot_author[4:].isdigit()
        is_real_user = UserModel.query.filter_by(login=bot_author).first() is not None
        if looks_like_anon or is_real_user:
            return jsonify({'error': 'X-Bot-Author не должен совпадать с существующим логином или Anon<N>'}), 400
        author = bot_author
    else:
        author = caller

    msg = Message(
        room_id=room_id,
        author=author,
        text=text,
        timestamp=datetime.now(timezone.utc).replace(tzinfo=None),
        media=media if media else None,
    )
    db.session.add(msg)
    db.session.commit()

    from extensions import socketio
    msg_index = Message.query.filter_by(room_id=room_id).order_by(Message.id).count()
    socketio.emit('new_message', {
        'index': msg_index,
        'author': author,
        'timestamp': msg.timestamp.isoformat(),
        'text': text,
        'reply_to': '',
        'media': media,
        'room_id': room_id,
    }, room=room_id)

    return jsonify({'ok': True, 'id': msg.id, 'author': author, 'text': text, 'media': media}), 201


# ── Media upload ──────────────────────────────────────────────────────────────

@api_bp.route('/room/<room_id>/upload', methods=['POST'])
def api_upload_media(room_id):
    """Загрузка файла в комнату через API-ключ (без CSRF)."""
    if not _resolve_api_key():
        return jsonify({'error': 'API key required'}), 401

    room = Room.query.filter_by(room_id=room_id).first()
    if not room:
        return jsonify({'error': 'Room not found'}), 404

    caller = _get_caller()
    if not _can_access(room, caller):
        return jsonify({'error': 'Access denied'}), 403

    from rooms import EXT_TO_MIME, ROOMS_DIR, _cleanup_media

    file = request.files.get('file')
    if not file or not file.filename:
        return jsonify({'error': 'No file'}), 400

    ext = file.filename.rsplit('.', 1)[-1].lower() if '.' in file.filename else ''
    if not EXT_TO_MIME.get(ext):
        return jsonify({'error': f'Extension not allowed: .{ext}'}), 415

    media_dir = os.path.join(ROOMS_DIR, room_id, 'media')
    os.makedirs(media_dir, exist_ok=True)

    original_name = secure_filename(file.filename) or 'file'
    safe_name = uuid.uuid4().hex + '_' + original_name
    if not safe_name.lower().endswith('.' + ext):
        safe_name += '.' + ext

    file.save(os.path.join(media_dir, safe_name))
    threading.Thread(target=_cleanup_media, daemon=True).start()

    return jsonify({'ok': True, 'filename': safe_name})


# ── API Keys ──────────────────────────────────────────────────────────────────

@api_bp.route('/keys', methods=['GET'])
def list_keys():
    """Список ключей текущего пользователя (только через сессию браузера)."""
    if session.get('user_type') != 'registered':
        return jsonify({'error': 'Login required'}), 401
    login = session['login']
    keys = ApiKey.query.filter_by(login=login).order_by(ApiKey.created_at.desc()).all()
    return jsonify([
        {
            'id': k.id,
            'label': k.label,
            'created_at': k.created_at.isoformat(),
        }
        for k in keys
    ])


@api_bp.route('/keys', methods=['POST'])
def create_key():
    """Создать новый API-ключ. Возвращает ключ ОДИН РАЗ — сохрани его."""
    if not _require_csrf_unless_apikey():
        return jsonify({'error': 'CSRF token missing or invalid'}), 400
    if session.get('user_type') != 'registered':
        return jsonify({'error': 'Login required'}), 401
    login = session['login']

    if ApiKey.query.filter_by(login=login).count() >= 10:
        return jsonify({'error': 'Максимум 10 ключей на аккаунт'}), 429

    data = request.get_json(silent=True) or {}
    label = (data.get('label') or '').strip()[:64]

    raw = 'vsc_' + secrets.token_urlsafe(32)
    key = ApiKey(login=login, key_hash=_hash_key(raw), label=label)
    db.session.add(key)
    db.session.commit()

    return jsonify({
        'ok': True,
        'id': key.id,
        'key': raw,   # показывается только здесь и только один раз
        'label': label,
    }), 201


@api_bp.route('/keys/<int:key_id>', methods=['DELETE'])
def delete_key(key_id):
    """Удалить ключ по ID."""
    if not _require_csrf_unless_apikey():
        return jsonify({'error': 'CSRF token missing or invalid'}), 400
    if session.get('user_type') != 'registered':
        return jsonify({'error': 'Login required'}), 401
    login = session['login']
    key = ApiKey.query.filter_by(id=key_id, login=login).first()
    if not key:
        return jsonify({'error': 'Not found'}), 404
    db.session.delete(key)
    db.session.commit()
    return jsonify({'ok': True})
