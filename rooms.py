import os
import csv
import random
from datetime import datetime
from flask import Blueprint, render_template, request, redirect, url_for, session

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
    if len(room_id) != 10 or not room_id.isdigit():
        return redirect(url_for('index'))
    room_path = os.path.join(ROOMS_DIR, room_id)
    if not os.path.isdir(room_path):
        return redirect(url_for('index'))
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
        writer.writerow(['author', 'timestamp', 'text'])

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

    if session.get('user_type') == 'registered':
        author = session['login']
    else:
        author = session.get('anon_id', 'Anon')

    now = datetime.utcnow().isoformat()
    msg_path = os.path.join(room_path, 'messages.csv')
    with open(msg_path, 'a', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow([author, now, text])

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
