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
import os
import threading
import uuid
from datetime import datetime, timezone

from flask import Blueprint, request, jsonify, g, send_from_directory
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

from extensions import db, socketio
from ws_centralized import fanout_message, fanout_event
from models import User, AuthToken, Room, RoomMember, Message
from bearer import issue_token, _hash_token, require_bearer
from auth import (
    LOGIN_RE, ANON_RE,
    _ensure_personal_room,
    _is_rate_limited, _record_attempt, _clear_attempts,
    _get_ip,
)
from rooms import (
    _generate_room_id, EXT_TO_MIME, INLINE_MIMES, _ext_of, ROOMS_DIR, _cleanup_media,
)

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


def _msg_index(m: Message) -> int:
    """1-based позиция сообщения в комнате.

    Веб-клиент (rooms.py/socketio) адресует сообщения индексом, а Message.reply_to
    хранит именно индекс — Bearer-API конвертирует id⇄индекс на границе."""
    return Message.query.filter(
        Message.room_id == m.room_id, Message.id <= m.id
    ).count()


def _ref_by_index(room_str_id: str, index) -> Message | None:
    """Сообщение комнаты по 1-based индексу (семантика веб-`reply_to`)."""
    if not index or index < 1:
        return None
    return (
        Message.query.filter_by(room_id=room_str_id)
        .order_by(Message.id).offset(index - 1).first()
    )


def _reply_payload(m: Message) -> dict | None:
    """Готовая цитата для Bearer-клиента: {id, sender, body(≤60)} либо None.

    У вложений без текста цитата — «Изображение» либо имя файла (байты
    вложения в цитату не «расшифровываем»)."""
    ref = _ref_by_index(m.room_id, m.reply_to)
    if ref is None:
        return None
    body = (ref.text or '')[:60]
    if not body and ref.media:
        mime = EXT_TO_MIME.get(_ext_of(ref.media), '')
        if mime.startswith('image/'):
            body = 'Изображение'
        else:
            _, _, original = ref.media.partition('_')
            body = original or 'Файл'
    return {'id': ref.id, 'sender': ref.author, 'body': body}


def _message_dict(m: Message, room_int_id: int) -> dict:
    return {
        'id': m.id,
        'room_id': room_int_id,
        'sender': m.author,
        'body': m.text,
        'created_at': m.timestamp.isoformat(),
        'client_msg_id': m.client_msg_id,
        'media': m.media,
        'reply_to': _reply_payload(m),
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


# ── Вложения (Bearer) ──────────────────────────────────────────────────────────

@central_bp.post('/rooms/<int:room_id>/media')
@require_bearer
def api_upload_media(room_id: int):
    """Загрузить файл в комнату (multipart, поле file). Не создаёт сообщение —
    только сохраняет файл; ссылка на него передаётся в POST /api/messages."""
    caller = g.caller_login
    room = db.session.get(Room, room_id)
    if room is None:
        return jsonify({'error': 'not_found'}), 404
    if not RoomMember.query.filter_by(room_id=room.room_id, login=caller).first():
        return jsonify({'error': 'forbidden'}), 403

    file = request.files.get('file')
    if not file or not file.filename:
        return jsonify({'error': 'no_file'}), 400

    ext = _ext_of(file.filename)
    expected_mime = EXT_TO_MIME.get(ext)
    if not expected_mime:
        return jsonify({'error': 'extension_not_allowed', 'ext': ext}), 415

    media_dir = os.path.join(ROOMS_DIR, room.room_id, 'media')
    os.makedirs(media_dir, exist_ok=True)

    original_name = secure_filename(file.filename) or 'file'
    safe_name = uuid.uuid4().hex + '_' + original_name
    if not safe_name.lower().endswith('.' + ext):
        safe_name += '.' + ext

    dest = os.path.join(media_dir, safe_name)
    file.save(dest)
    size = os.path.getsize(dest)

    threading.Thread(target=_cleanup_media, daemon=True).start()

    return jsonify({'ok': True, 'filename': safe_name, 'mime_type': expected_mime, 'size': size}), 201


@central_bp.get('/rooms/<int:room_id>/media/<path:filename>')
@require_bearer
def api_download_media(room_id: int, filename: str):
    """Отдать байты вложения (Bearer). Десктоп-клиент не имеет сессионной куки,
    поэтому существующий /room/<id>/media/<file> (сессия) ему не подходит."""
    caller = g.caller_login
    room = db.session.get(Room, room_id)
    if room is None:
        return '', 404
    if not RoomMember.query.filter_by(room_id=room.room_id, login=caller).first():
        return '', 403

    # Basename-only: защита от path traversal через URL.
    safe_filename = os.path.basename(filename)
    if not safe_filename or safe_filename != filename:
        return '', 404

    ext = _ext_of(safe_filename)
    mime = EXT_TO_MIME.get(ext) or 'application/octet-stream'
    media_dir = os.path.join(ROOMS_DIR, room.room_id, 'media')
    as_attachment = mime not in INLINE_MIMES
    resp = send_from_directory(media_dir, safe_filename, mimetype=mime, as_attachment=as_attachment)
    resp.headers['X-Content-Type-Options'] = 'nosniff'
    return resp


# ── Messages (Bearer) ──────────────────────────────────────────────────────────

@central_bp.post('/messages')
@require_bearer
def api_post_message():
    """Отправить сообщение в комнату. Идемпотентно по client_msg_id.

    Тело: {"room_id": <int>, "body": "...", "client_msg_id": "<uuid hex>",
           "media": "<серверное имя файла>" (опционально),
           "reply_to": <server Message.id> (опционально)}
    """
    caller = g.caller_login
    data = request.get_json(silent=True) or {}

    room_id = data.get('room_id')
    body = data.get('body', '')
    client_msg_id = data.get('client_msg_id')
    media = data.get('media')
    reply_to_id = data.get('reply_to')

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
            return jsonify(_message_dict(existing, room.id)), 200

    if media:
        media = os.path.basename(media)
        media_path = os.path.join(ROOMS_DIR, room.room_id, 'media', media)
        if not os.path.isfile(media_path):
            return jsonify({'error': 'media_not_found'}), 400
    else:
        media = None

    if not body and not media:
        return jsonify({'error': 'empty_message'}), 400

    # Bearer-клиент присылает reply_to серверным id — храним индексом (веб-семантика)
    reply_index = None
    if reply_to_id:
        ref = db.session.get(Message, reply_to_id)
        if ref is None or ref.room_id != room.room_id:
            return jsonify({'error': 'reply_not_found'}), 400
        reply_index = _msg_index(ref)

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    m = Message(
        room_id=room.room_id,
        author=caller,
        text=body,
        timestamp=now,
        client_msg_id=client_msg_id,
        media=media,
        reply_to=reply_index,
    )
    db.session.add(m)
    db.session.commit()

    payload = _message_dict(m, room.id)
    fanout_message(room.id, payload)
    # Веб-клиенты слушают socketio 'new_message' (формат rooms.py, index-based)
    entry = {
        'index': _msg_index(m),
        'author': m.author,
        'timestamp': m.timestamp.isoformat(),
        'text': m.text,
        'reply_to': str(m.reply_to) if m.reply_to else '',
        'media': m.media or '',
        'room_id': room.room_id,
    }
    if payload['reply_to']:
        entry['reply_author'] = payload['reply_to']['sender']
        entry['reply_text'] = payload['reply_to']['body']
    socketio.emit('new_message', entry, room=room.room_id)

    return jsonify(payload), 200


@central_bp.post('/messages/<int:message_id>/edit')
@require_bearer
def api_edit_message(message_id: int):
    """Изменить своё сообщение. Тело: {"body": "..."} (непустое)."""
    caller = g.caller_login
    m = db.session.get(Message, message_id)
    if m is None:
        return jsonify({'error': 'not_found'}), 404
    room = Room.query.filter_by(room_id=m.room_id).first()
    if room is None:
        return jsonify({'error': 'not_found'}), 404
    if not RoomMember.query.filter_by(room_id=m.room_id, login=caller).first():
        return jsonify({'error': 'forbidden'}), 403
    if m.author != caller:
        return jsonify({'error': 'forbidden'}), 403

    data = request.get_json(silent=True) or {}
    body = (data.get('body') or '').strip()
    if not body:
        return jsonify({'error': 'empty_message'}), 400

    idx = _msg_index(m)
    m.text = body
    db.session.commit()

    socketio.emit('edit_message', {'index': idx, 'text': body}, room=m.room_id)
    fanout_event(room.id, {'type': 'message_edited', 'id': m.id, 'body': body})
    return jsonify({'ok': True, 'id': m.id, 'body': body}), 200


@central_bp.post('/messages/<int:message_id>/delete')
@require_bearer
def api_delete_message(message_id: int):
    """Удалить своё сообщение (без тела запроса)."""
    caller = g.caller_login
    m = db.session.get(Message, message_id)
    if m is None:
        return jsonify({'error': 'not_found'}), 404
    room = Room.query.filter_by(room_id=m.room_id).first()
    if room is None:
        return jsonify({'error': 'not_found'}), 404
    if not RoomMember.query.filter_by(room_id=m.room_id, login=caller).first():
        return jsonify({'error': 'forbidden'}), 403
    if m.author != caller:
        return jsonify({'error': 'forbidden'}), 403

    idx = _msg_index(m)  # до удаления — потом позиция не восстановима
    room_str_id = m.room_id
    msg_id = m.id
    db.session.delete(m)
    db.session.commit()

    socketio.emit('delete_message', {'index': idx}, room=room_str_id)
    fanout_event(room.id, {'type': 'message_deleted', 'id': msg_id})
    return jsonify({'ok': True}), 200


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

    messages = [_message_dict(m, room.id) for m in page]

    return jsonify({'messages': messages, 'next_cursor': next_cursor})
