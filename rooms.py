import os
import re
import shutil
import random
import threading
import uuid
from datetime import datetime, timezone

from flask import Blueprint, render_template, request, redirect, url_for, session, jsonify, send_from_directory
from flask_socketio import join_room as sio_join_room, emit as sio_emit
from werkzeug.utils import secure_filename

from extensions import db, socketio
from models import Room, Message, RoomMember

rooms_bp = Blueprint('rooms', __name__)

ROOMS_DIR = os.path.join(os.path.dirname(__file__), 'rooms')
MAX_MEDIA_BYTES = 20 * 1024 ** 3  # 20 GB

ALLOWED_MIMES = {
    'image/jpeg', 'image/png', 'image/gif', 'image/webp', 'image/svg+xml',
    'image/bmp', 'image/tiff',
    'video/mp4', 'video/webm', 'video/quicktime', 'video/x-msvideo',
    'audio/mpeg', 'audio/ogg', 'audio/wav', 'audio/webm',
    'application/pdf',
    'text/plain', 'text/csv',
    'application/zip', 'application/x-zip-compressed',
    'application/x-rar-compressed', 'application/vnd.rar',
    'application/msword',
    'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    'application/vnd.ms-excel',
    'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
}


def _cleanup_media(max_bytes=None):
    if max_bytes is None:
        max_bytes = MAX_MEDIA_BYTES
    files = []
    if not os.path.isdir(ROOMS_DIR):
        return
    for room_dir in os.listdir(ROOMS_DIR):
        media_path = os.path.join(ROOMS_DIR, room_dir, 'media')
        if not os.path.isdir(media_path):
            continue
        for fname in os.listdir(media_path):
            fp = os.path.join(media_path, fname)
            try:
                stat = os.stat(fp)
                files.append((fp, stat.st_mtime, stat.st_size))
            except OSError:
                continue
    total = sum(f[2] for f in files)
    if total <= max_bytes:
        return
    files.sort(key=lambda f: f[1])
    for fp, mtime, size in files:
        if total <= max_bytes:
            break
        try:
            os.remove(fp)
        except OSError:
            continue
        total -= size


def _generate_room_id():
    while True:
        room_id = ''.join([str(random.randint(0, 9)) for _ in range(10)])
        if not Room.query.filter_by(room_id=room_id).first():
            return room_id


def get_room_display_name(room_id):
    room = Room.query.filter_by(room_id=room_id).first()
    if room and room.name:
        return room.name
    return room_id


def _can_access_room(room_id):
    room = Room.query.filter_by(room_id=room_id).first()
    if not room:
        return False
    # Личная комната — только её владелец
    if room.personal_login:
        return session.get('login') == room.personal_login
    if room.is_open:
        return True
    if session.get('user_type') != 'registered':
        return False
    return RoomMember.query.filter_by(room_id=room_id, login=session.get('login')).first() is not None


def _track_room(room_id):
    # Личную комнату не трекаем — она всегда сверху отдельно
    if room_id == session.get('personal_room_id'):
        return

    visited = session.get('visited_rooms', [])
    if room_id in visited:
        visited.remove(room_id)
    visited.insert(0, room_id)
    session['visited_rooms'] = visited
    session.modified = True

    # Для зарегистрированных — сохраняем в БД чтобы не терять при сбросе сессии
    if session.get('user_type') == 'registered':
        login = session.get('login')
        if login and not RoomMember.query.filter_by(room_id=room_id, login=login).first():
            member = RoomMember(room_id=room_id, login=login, role='visitor')
            db.session.add(member)
            db.session.commit()


def _untrack_room(room_id):
    visited = session.get('visited_rooms', [])
    if room_id in visited:
        visited.remove(room_id)
    session['visited_rooms'] = visited
    session.modified = True


def _remove_member(room_id, login):
    RoomMember.query.filter_by(room_id=room_id, login=login).delete()
    db.session.commit()


@rooms_bp.route('/room/join', methods=['POST'])
def join_room():
    room_id = request.form.get('room_id', '').strip()
    if not room_id.isdigit() or len(room_id) != 10:
        return render_template('index.html', error='Неверный формат ID: должно быть ровно 10 цифр.')
    if not Room.query.filter_by(room_id=room_id).first():
        return render_template('index.html', error='Комната с таким ID не найдена.')
    return redirect(url_for('rooms.room', room_id=room_id))


@rooms_bp.route('/room/create', methods=['POST'])
def create_room():
    if session.get('user_type') != 'registered':
        return redirect(url_for('index'))

    login = session['login']
    room_id = _generate_room_id()
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    room = Room(room_id=room_id, name='', is_open=True, created_at=now, creator_login=login)
    db.session.add(room)

    member = RoomMember(room_id=room_id, login=login, joined_at=now, role='godfather')
    db.session.add(member)

    db.session.commit()

    media_dir = os.path.join(ROOMS_DIR, room_id, 'media')
    os.makedirs(media_dir, exist_ok=True)

    return redirect(url_for('rooms.room', room_id=room_id))


@rooms_bp.route('/room/<room_id>')
def room(room_id):
    room = Room.query.filter_by(room_id=room_id).first()
    if not room:
        return redirect(url_for('index'))

    if not _can_access_room(room_id):
        return render_template('index.html', error='У вас нет доступа к этой комнате.')

    _track_room(room_id)

    messages = Message.query.filter_by(room_id=room_id).order_by(Message.id).all()
    return render_template('room.html', room_id=room_id, config=room, messages=messages)


@rooms_bp.route('/room/<room_id>/message/<int:msg_index>/edit', methods=['POST'])
def edit_message(room_id, msg_index):
    room = Room.query.filter_by(room_id=room_id).first()
    if not room:
        return jsonify({'ok': False}), 404

    if not _can_access_room(room_id):
        return jsonify({'ok': False}), 403

    messages = Message.query.filter_by(room_id=room_id).order_by(Message.id).all()
    if msg_index < 1 or msg_index > len(messages):
        return jsonify({'ok': False}), 404

    msg = messages[msg_index - 1]

    if session.get('user_type') == 'registered':
        current_user = session.get('login')
    else:
        current_user = session.get('anon_id')

    if msg.author != current_user:
        return jsonify({'ok': False}), 403

    new_text = request.form.get('text', '').strip()
    if not new_text:
        return jsonify({'ok': False}), 400

    msg.text = new_text
    db.session.commit()

    socketio.emit('edit_message', {'index': msg_index, 'text': new_text}, room=room_id)
    return jsonify({'ok': True, 'text': new_text})


@rooms_bp.route('/room/<room_id>/message/<int:msg_index>/delete', methods=['POST'])
def delete_message(room_id, msg_index):
    room = Room.query.filter_by(room_id=room_id).first()
    if not room:
        return jsonify({'ok': False}), 404

    if not _can_access_room(room_id):
        return jsonify({'ok': False}), 403

    messages = Message.query.filter_by(room_id=room_id).order_by(Message.id).all()
    if msg_index < 1 or msg_index > len(messages):
        return jsonify({'ok': False}), 404

    msg = messages[msg_index - 1]

    if session.get('user_type') == 'registered':
        current_user = session.get('login')
    else:
        current_user = session.get('anon_id')

    if msg.author != current_user:
        return jsonify({'ok': False}), 403

    db.session.delete(msg)
    db.session.commit()

    socketio.emit('delete_message', {'index': msg_index}, room=room_id)
    return jsonify({'ok': True})


@rooms_bp.route('/room/<room_id>/messages/poll')
def poll_messages(room_id):
    """Возвращает сообщения начиная с индекса after (1-based) в формате JSON."""
    room = Room.query.filter_by(room_id=room_id).first()
    if not room:
        return jsonify([]), 404

    if not _can_access_room(room_id):
        return jsonify([]), 403

    after = request.args.get('after', '0')
    after = int(after) if after.isdigit() else 0

    base_q = Message.query.filter_by(room_id=room_id).order_by(Message.id)
    total = base_q.count()
    new_messages = base_q.offset(after).all()

    new_msgs = []
    for i, msg in enumerate(new_messages):
        entry = {
            'index': after + i + 1,
            'author': msg.author,
            'timestamp': msg.timestamp.isoformat(),
            'text': msg.text,
            'reply_to': str(msg.reply_to) if msg.reply_to else '',
            'media': msg.media or '',
        }
        rt = entry['reply_to']
        if rt:
            ri = int(rt)
            if 0 < ri <= total:
                reply_msg = base_q.offset(ri - 1).first()
                if reply_msg:
                    entry['reply_author'] = reply_msg.author
                    entry['reply_text'] = reply_msg.text[:60]
        new_msgs.append(entry)

    return jsonify(new_msgs)


@rooms_bp.route('/room/<room_id>/message', methods=['POST'])
def post_message(room_id):
    room = Room.query.filter_by(room_id=room_id).first()
    if not room:
        return redirect(url_for('index'))

    if not _can_access_room(room_id):
        return redirect(url_for('index'))

    text = re.sub(r'\n{6,}', '\n\n\n\n\n', request.form.get('text', '').strip())[:4000]
    media = request.form.get('media', '').strip()
    # Разрешаем только имя файла без пути — только файлы этой комнаты
    if media:
        media = os.path.basename(media)
        media_path = os.path.join(ROOMS_DIR, room_id, 'media', media)
        if not os.path.isfile(media_path):
            media = ''
    if not text and not media:
        return redirect(url_for('rooms.room', room_id=room_id))

    _track_room(room_id)

    reply_to_raw = request.form.get('reply_to', '').strip()
    reply_to = int(reply_to_raw) if reply_to_raw.isdigit() else None

    if session.get('user_type') == 'registered':
        author = session['login']
    else:
        author = session.get('anon_id', 'Anon')

    msg = Message(
        room_id=room_id,
        author=author,
        text=text,
        timestamp=datetime.now(timezone.utc).replace(tzinfo=None),
        reply_to=reply_to,
        media=media or None,
    )
    db.session.add(msg)
    db.session.commit()

    # Determine 1-based index
    msg_index = Message.query.filter_by(room_id=room_id).order_by(Message.id).count()
    entry = {
        'index': msg_index,
        'author': msg.author,
        'timestamp': msg.timestamp.isoformat(),
        'text': msg.text,
        'reply_to': str(msg.reply_to) if msg.reply_to else '',
        'media': msg.media or '',
    }
    if entry['reply_to']:
        ri = int(entry['reply_to'])
        ref = Message.query.filter_by(room_id=room_id).order_by(Message.id).offset(ri - 1).first()
        if ref:
            entry['reply_author'] = ref.author
            entry['reply_text'] = ref.text[:60]
    socketio.emit('new_message', entry, room=room_id)

    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return jsonify({'ok': True})

    return redirect(url_for('rooms.room', room_id=room_id))


@rooms_bp.route('/room/<room_id>/upload', methods=['POST'])
def upload_media(room_id):
    room = Room.query.filter_by(room_id=room_id).first()
    if not room:
        return jsonify({'ok': False, 'error': 'Room not found'}), 404

    if not _can_access_room(room_id):
        return jsonify({'ok': False, 'error': 'Access denied'}), 403

    file = request.files.get('file')
    if not file or not file.filename:
        return jsonify({'ok': False, 'error': 'No file'}), 400

    content_type = (file.content_type or '').split(';')[0].strip()
    if content_type and content_type not in ALLOWED_MIMES:
        return jsonify({'ok': False, 'error': f'Тип файла не разрешён: {content_type}'}), 415

    media_dir = os.path.join(ROOMS_DIR, room_id, 'media')
    os.makedirs(media_dir, exist_ok=True)

    original_name = secure_filename(file.filename) or 'file'
    safe_name = uuid.uuid4().hex + '_' + original_name
    file.save(os.path.join(media_dir, safe_name))

    threading.Thread(target=_cleanup_media, daemon=True).start()

    return jsonify({'ok': True, 'filename': safe_name})


@rooms_bp.route('/room/<room_id>/media/<filename>')
def serve_media(room_id, filename):
    room = Room.query.filter_by(room_id=room_id).first()
    if not room:
        return '', 404

    if not _can_access_room(room_id):
        return '', 403

    media_dir = os.path.join(ROOMS_DIR, room_id, 'media')
    return send_from_directory(media_dir, filename)


@rooms_bp.route('/room/<room_id>/leave', methods=['POST'])
def leave_room(room_id):
    if session.get('user_type') != 'registered':
        return redirect(url_for('index'))

    room = Room.query.filter_by(room_id=room_id).first()
    if not room:
        return redirect(url_for('index'))

    login = session['login']
    if room.creator_login == login:
        return redirect(url_for('rooms.room', room_id=room_id))

    _remove_member(room_id, login)
    _untrack_room(room_id)

    return redirect(url_for('index'))


@rooms_bp.route('/room/<room_id>/delete', methods=['POST'])
def delete_room(room_id):
    if session.get('user_type') != 'registered':
        return redirect(url_for('index'))

    room = Room.query.filter_by(room_id=room_id).first()
    if not room:
        return redirect(url_for('index'))

    if room.creator_login != session.get('login'):
        return redirect(url_for('rooms.room', room_id=room_id))

    room_path = os.path.join(ROOMS_DIR, room_id)
    if os.path.isdir(room_path):
        shutil.rmtree(room_path)

    db.session.delete(room)
    db.session.commit()

    _untrack_room(room_id)
    return redirect(url_for('index'))


@rooms_bp.route('/room/<room_id>/manage/kick', methods=['POST'])
def kick_user(room_id):
    room = Room.query.filter_by(room_id=room_id).first()
    if not room:
        return redirect(url_for('index'))

    if session.get('user_type') != 'registered':
        return redirect(url_for('rooms.room', room_id=room_id))

    if room.creator_login != session.get('login'):
        return redirect(url_for('rooms.room', room_id=room_id))

    target = request.form.get('login', '').strip()
    if target and target != room.creator_login:
        _remove_member(room_id, target)

    return redirect(url_for('rooms.manage_room', room_id=room_id))


@rooms_bp.route('/room/<room_id>/manage', methods=['GET', 'POST'])
def manage_room(room_id):
    room = Room.query.filter_by(room_id=room_id).first()
    if not room:
        return redirect(url_for('index'))

    if session.get('user_type') != 'registered':
        return redirect(url_for('rooms.room', room_id=room_id))

    if room.creator_login != session.get('login'):
        return redirect(url_for('rooms.room', room_id=room_id))

    if request.method == 'GET':
        members = RoomMember.query.filter_by(room_id=room_id).all()
        return render_template('manage.html', room_id=room_id, config=room, users=members)

    room.is_open = request.form.get('is_open', 'true') == 'true'
    room.name = request.form.get('room_name', '').strip()[:64]

    add_user = request.form.get('add_user', '').strip()
    if add_user and not RoomMember.query.filter_by(room_id=room_id, login=add_user).first():
        from models import User as UserModel
        if UserModel.query.filter_by(login=add_user).first():
            member = RoomMember(room_id=room_id, login=add_user, role='member')
            db.session.add(member)

    db.session.commit()
    return redirect(url_for('rooms.manage_room', room_id=room_id))


# ── Socket.IO events ──────────────────────────────────────────────────────────

@socketio.on('join')
def on_join(data):
    room_id = data.get('room_id', '')
    room = Room.query.filter_by(room_id=room_id).first()
    if not room:
        return
    if not _can_access_room(room_id):
        return
    sio_join_room(room_id)
