import os
import csv
import shutil
import random
from datetime import datetime
from flask import Blueprint, render_template, request, redirect, url_for, session, jsonify

rooms_bp = Blueprint('rooms', __name__)

ROOMS_DIR = os.path.join(os.path.dirname(__file__), 'rooms')


def _generate_room_id():
    while True:
        room_id = ''.join([str(random.randint(0, 9)) for _ in range(10)])
        if not os.path.exists(os.path.join(ROOMS_DIR, room_id)):
            return room_id


def _read_config(room_id):
    path = os.path.join(ROOMS_DIR, room_id, 'config.csv')
    with open(path, 'r', newline='', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        return next(reader)


def _read_messages(room_id):
    path = os.path.join(ROOMS_DIR, room_id, 'messages.csv')
    with open(path, 'r', newline='', encoding='utf-8') as f:
        return list(csv.DictReader(f))


def _read_users(room_id):
    path = os.path.join(ROOMS_DIR, room_id, 'users.csv')
    with open(path, 'r', newline='', encoding='utf-8') as f:
        return list(csv.DictReader(f))


def _is_user_in_room(room_id, login):
    users = _read_users(room_id)
    return any(u['login'] == login for u in users)


def get_room_display_name(room_id):
    """Возвращает имя комнаты если задано, иначе её ID."""
    try:
        config = _read_config(room_id)
        name = config.get('room_name', '').strip()
        return name if name else room_id
    except Exception:
        return room_id


def _can_access_room(room_id):
    config = _read_config(room_id)
    if config['is_open'] == 'true':
        return True
    if session.get('user_type') != 'registered':
        return False
    return _is_user_in_room(room_id, session.get('login'))


@rooms_bp.route('/room/join', methods=['POST'])
def join_room():
    room_id = request.form.get('room_id', '').strip()
    if not room_id.isdigit() or len(room_id) != 10:
        return render_template('index.html', error='Неверный формат ID: должно быть ровно 10 цифр.')
    room_path = os.path.join(ROOMS_DIR, room_id)
    if not os.path.isdir(room_path):
        return render_template('index.html', error='Комната с таким ID не найдена.')
    return redirect(url_for('rooms.room', room_id=room_id))


@rooms_bp.route('/room/create', methods=['POST'])
def create_room():
    if session.get('user_type') != 'registered':
        return redirect(url_for('index'))

    login = session['login']
    room_id = _generate_room_id()
    room_path = os.path.join(ROOMS_DIR, room_id)
    os.makedirs(room_path)

    now = datetime.utcnow().isoformat()

    with open(os.path.join(room_path, 'config.csv'), 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow(['room_id', 'is_open', 'created_at', 'creator_login', 'room_name'])
        writer.writerow([room_id, 'true', now, login, ''])

    with open(os.path.join(room_path, 'messages.csv'), 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow(['author', 'timestamp', 'text', 'reply_to'])

    with open(os.path.join(room_path, 'users.csv'), 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow(['login', 'joined_at', 'role'])
        writer.writerow([login, now, 'godfather'])

    return redirect(url_for('rooms.room', room_id=room_id))


def _track_room(room_id):
    visited = session.get('visited_rooms', [])
    if room_id in visited:
        visited.remove(room_id)
    visited.insert(0, room_id)
    session['visited_rooms'] = visited

    if session.get('user_type') == 'registered':
        from auth import save_user_rooms
        save_user_rooms(session['login'], visited)


def _untrack_room(room_id):
    visited = session.get('visited_rooms', [])
    if room_id in visited:
        visited.remove(room_id)
    session['visited_rooms'] = visited

    if session.get('user_type') == 'registered':
        from auth import save_user_rooms
        save_user_rooms(session['login'], visited)


def _remove_user_from_room(room_id, login):
    users = _read_users(room_id)
    users = [u for u in users if u['login'] != login]
    users_path = os.path.join(ROOMS_DIR, room_id, 'users.csv')
    with open(users_path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow(['login', 'joined_at', 'role'])
        for u in users:
            writer.writerow([u['login'], u['joined_at'], u['role']])


@rooms_bp.route('/room/<room_id>')
def room(room_id):
    room_path = os.path.join(ROOMS_DIR, room_id)
    if not os.path.isdir(room_path):
        return redirect(url_for('index'))

    if not _can_access_room(room_id):
        return render_template('index.html', error='У вас нет доступа к этой комнате.')

    _track_room(room_id)

    config = _read_config(room_id)
    messages = _read_messages(room_id)
    return render_template('room.html', room_id=room_id, config=config, messages=messages)


@rooms_bp.route('/room/<room_id>/message/<int:msg_index>/edit', methods=['POST'])
def edit_message(room_id, msg_index):
    room_path = os.path.join(ROOMS_DIR, room_id)
    if not os.path.isdir(room_path):
        return jsonify({'ok': False}), 404

    if not _can_access_room(room_id):
        return jsonify({'ok': False}), 403

    messages = _read_messages(room_id)
    if msg_index < 1 or msg_index > len(messages):
        return jsonify({'ok': False}), 404

    msg = messages[msg_index - 1]

    # проверяем авторство
    if session.get('user_type') == 'registered':
        current_user = session.get('login')
    else:
        current_user = session.get('anon_id')

    if msg['author'] != current_user:
        return jsonify({'ok': False}), 403

    new_text = request.form.get('text', '').strip()
    if not new_text:
        return jsonify({'ok': False}), 400

    messages[msg_index - 1]['text'] = new_text

    msg_path = os.path.join(room_path, 'messages.csv')
    with open(msg_path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=['author', 'timestamp', 'text', 'reply_to'])
        writer.writeheader()
        writer.writerows(messages)

    return jsonify({'ok': True, 'text': new_text})


@rooms_bp.route('/room/<room_id>/messages/poll')
def poll_messages(room_id):
    """Возвращает сообщения начиная с индекса after (1-based) в формате JSON."""
    room_path = os.path.join(ROOMS_DIR, room_id)
    if not os.path.isdir(room_path):
        return jsonify([]), 404

    if not _can_access_room(room_id):
        return jsonify([]), 403

    after = request.args.get('after', '0')
    after = int(after) if after.isdigit() else 0

    messages = _read_messages(room_id)
    total = len(messages)

    new_msgs = []
    for i in range(after, total):
        msg = messages[i]
        entry = {
            'index': i + 1,
            'author': msg['author'],
            'timestamp': msg['timestamp'],
            'text': msg['text'],
            'reply_to': msg.get('reply_to', '').strip(),
        }
        # добавляем данные об оригинальном сообщении для ответов
        rt = entry['reply_to']
        if rt and rt.isdigit():
            ri = int(rt)
            if 0 < ri <= total:
                entry['reply_author'] = messages[ri - 1]['author']
                entry['reply_text'] = messages[ri - 1]['text'][:60]
        new_msgs.append(entry)

    return jsonify(new_msgs)


@rooms_bp.route('/room/<room_id>/message', methods=['POST'])
def post_message(room_id):
    room_path = os.path.join(ROOMS_DIR, room_id)
    if not os.path.isdir(room_path):
        return redirect(url_for('index'))

    if not _can_access_room(room_id):
        return redirect(url_for('index'))

    text = request.form.get('text', '').strip()
    if not text:
        return redirect(url_for('rooms.room', room_id=room_id))

    reply_to = request.form.get('reply_to', '').strip()

    if session.get('user_type') == 'registered':
        author = session['login']
    else:
        author = session.get('anon_id', 'Anon')

    now = datetime.utcnow().isoformat()
    msg_path = os.path.join(room_path, 'messages.csv')
    with open(msg_path, 'a', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow([author, now, text, reply_to])

    # AJAX-запрос — возвращаем JSON
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return jsonify({'ok': True})

    return redirect(url_for('rooms.room', room_id=room_id))


@rooms_bp.route('/room/<room_id>/leave', methods=['POST'])
def leave_room(room_id):
    if session.get('user_type') != 'registered':
        return redirect(url_for('index'))

    room_path = os.path.join(ROOMS_DIR, room_id)
    if not os.path.isdir(room_path):
        return redirect(url_for('index'))

    login = session['login']
    config = _read_config(room_id)

    # godfather не может покинуть свою комнату
    if config['creator_login'] == login:
        return redirect(url_for('rooms.room', room_id=room_id))

    _remove_user_from_room(room_id, login)
    _untrack_room(room_id)

    return redirect(url_for('index'))


@rooms_bp.route('/room/<room_id>/delete', methods=['POST'])
def delete_room(room_id):
    if session.get('user_type') != 'registered':
        return redirect(url_for('index'))

    room_path = os.path.join(ROOMS_DIR, room_id)
    if not os.path.isdir(room_path):
        return redirect(url_for('index'))

    config = _read_config(room_id)
    if config['creator_login'] != session.get('login'):
        return redirect(url_for('rooms.room', room_id=room_id))

    # убираем комнату из списков всех её участников
    from auth import load_user_rooms, save_user_rooms
    users = _read_users(room_id)
    for u in users:
        user_rooms = load_user_rooms(u['login'])
        if room_id in user_rooms:
            user_rooms.remove(room_id)
            save_user_rooms(u['login'], user_rooms)

    # удаляем папку комнаты целиком
    shutil.rmtree(room_path)

    _untrack_room(room_id)

    return redirect(url_for('index'))


@rooms_bp.route('/room/<room_id>/manage/kick', methods=['POST'])
def kick_user(room_id):
    room_path = os.path.join(ROOMS_DIR, room_id)
    if not os.path.isdir(room_path):
        return redirect(url_for('index'))

    if session.get('user_type') != 'registered':
        return redirect(url_for('rooms.room', room_id=room_id))

    config = _read_config(room_id)
    if config['creator_login'] != session.get('login'):
        return redirect(url_for('rooms.room', room_id=room_id))

    target = request.form.get('login', '').strip()
    # нельзя удалить самого себя (прародителя)
    if target and target != config['creator_login']:
        _remove_user_from_room(room_id, target)
        # убираем комнату из списка посещённых у удалённого пользователя
        from auth import load_user_rooms, save_user_rooms
        user_rooms = load_user_rooms(target)
        if room_id in user_rooms:
            user_rooms.remove(room_id)
            save_user_rooms(target, user_rooms)

    return redirect(url_for('rooms.manage_room', room_id=room_id))


@rooms_bp.route('/room/<room_id>/manage', methods=['GET', 'POST'])
def manage_room(room_id):
    room_path = os.path.join(ROOMS_DIR, room_id)
    if not os.path.isdir(room_path):
        return redirect(url_for('index'))

    if session.get('user_type') != 'registered':
        return redirect(url_for('rooms.room', room_id=room_id))

    config = _read_config(room_id)
    if config['creator_login'] != session.get('login'):
        return redirect(url_for('rooms.room', room_id=room_id))

    if request.method == 'GET':
        users = _read_users(room_id)
        return render_template('manage.html', room_id=room_id, config=config, users=users)

    new_is_open = request.form.get('is_open', 'true')
    new_name = request.form.get('room_name', '').strip()
    config_path = os.path.join(room_path, 'config.csv')
    with open(config_path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow(['room_id', 'is_open', 'created_at', 'creator_login', 'room_name'])
        writer.writerow([room_id, new_is_open, config['created_at'], config['creator_login'], new_name])

    add_user = request.form.get('add_user', '').strip()
    if add_user and not _is_user_in_room(room_id, add_user):
        users_path = os.path.join(room_path, 'users.csv')
        with open(users_path, 'a', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow([add_user, datetime.utcnow().isoformat(), 'member'])

    return redirect(url_for('rooms.manage_room', room_id=room_id))
