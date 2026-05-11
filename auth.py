import re
import time
import threading

from flask import Blueprint, render_template, request, redirect, url_for, session
from flask_login import login_user, logout_user
from werkzeug.security import generate_password_hash, check_password_hash

from extensions import db
from models import User, RoomMember

LOGIN_RE = re.compile(r'^[a-zA-Zа-яА-ЯёЁ0-9_]{3,32}$')
ANON_RE  = re.compile(r'^[Aa]non\d+$')

auth_bp = Blueprint('auth', __name__)

# Брутфорс-защита: max 5 попыток за 10 минут с одного IP
_LIMIT_ATTEMPTS = 5
_LIMIT_WINDOW   = 600  # секунд
_login_attempts: dict[str, list[float]] = {}
_attempts_lock  = threading.Lock()


def _get_ip() -> str:
    return request.remote_addr or 'unknown'


def _is_rate_limited(ip: str) -> bool:
    now = time.time()
    with _attempts_lock:
        timestamps = _login_attempts.get(ip, [])
        # Оставляем только попытки в пределах окна
        timestamps = [t for t in timestamps if now - t < _LIMIT_WINDOW]
        _login_attempts[ip] = timestamps
        return len(timestamps) >= _LIMIT_ATTEMPTS


def _record_attempt(ip: str) -> int:
    """Записывает неудачную попытку, возвращает сколько попыток осталось."""
    now = time.time()
    with _attempts_lock:
        timestamps = _login_attempts.get(ip, [])
        timestamps = [t for t in timestamps if now - t < _LIMIT_WINDOW]
        timestamps.append(now)
        _login_attempts[ip] = timestamps
        return max(0, _LIMIT_ATTEMPTS - len(timestamps))


def _clear_attempts(ip: str) -> None:
    with _attempts_lock:
        _login_attempts.pop(ip, None)


@auth_bp.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'GET':
        return render_template('register.html')

    login = request.form.get('login', '').strip()
    password = request.form.get('password', '').strip()
    password2 = request.form.get('password2', '').strip()

    if not login or not password:
        return render_template('register.html', error='Логин и пароль обязательны.')

    if not LOGIN_RE.match(login):
        return render_template('register.html', error='Логин 3–32 символа: буквы (в т.ч. кириллица), цифры и _.')

    if ANON_RE.match(login):
        return render_template('register.html', error='Этот логин зарезервирован для анонимных пользователей.', conflict=login)

    if len(password) < 4:
        return render_template('register.html', error='Пароль должен быть не менее 4 символов.')

    if password != password2:
        return render_template('register.html', error='Пароли не совпадают.')

    if User.query.filter_by(login=login).first():
        return render_template('register.html', error='Этот логин уже занят.', conflict=login)

    user = User(login=login, password_hash=generate_password_hash(password))
    db.session.add(user)
    db.session.commit()

    login_user(user)
    session['user_type'] = 'registered'
    session['login'] = login
    session['visited_rooms'] = []
    return redirect(url_for('index'))


@auth_bp.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'GET':
        return render_template('login.html')

    ip = _get_ip()

    if _is_rate_limited(ip):
        return render_template('login.html',
                               error=f'Слишком много попыток. Подождите {_LIMIT_WINDOW // 60} минут.')

    login_val = request.form.get('login', '').strip()
    password = request.form.get('password', '').strip()

    user = User.query.filter_by(login=login_val).first()
    if not user or not check_password_hash(user.password_hash, password):
        remaining = _record_attempt(ip)
        if remaining == 0:
            return render_template('login.html',
                                   error=f'Неверный логин или пароль. Аккаунт заблокирован на {_LIMIT_WINDOW // 60} мин.')
        return render_template('login.html',
                               error=f'Неверный логин или пароль. Осталось попыток: {remaining}.')

    _clear_attempts(ip)
    login_user(user)
    session['user_type'] = 'registered'
    session['login'] = login_val
    members = RoomMember.query.filter_by(login=login_val).order_by(RoomMember.joined_at.desc()).all()
    session['visited_rooms'] = [m.room_id for m in members]
    return redirect(url_for('index'))


@auth_bp.route('/logout', methods=['POST'])
def logout():
    logout_user()
    session.clear()
    return redirect(url_for('index'))
