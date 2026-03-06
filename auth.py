import os
import csv
from flask import Blueprint, render_template, request, redirect, url_for, session
from werkzeug.security import generate_password_hash, check_password_hash

auth_bp = Blueprint('auth', __name__)

USERS_DIR = os.path.join(os.path.dirname(__file__), 'users')
ACCOUNTS_FILE = os.path.join(USERS_DIR, 'accounts.csv')


def _rooms_file(login):
    return os.path.join(USERS_DIR, f'{login}_rooms.csv')


def load_user_rooms(login):
    path = _rooms_file(login)
    if not os.path.exists(path):
        return []
    with open(path, 'r', encoding='utf-8') as f:
        reader = csv.reader(f)
        next(reader, None)  # skip header
        return [row[0] for row in reader if row]


def save_user_rooms(login, room_list):
    path = _rooms_file(login)
    with open(path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow(['room_id'])
        for rid in room_list:
            writer.writerow([rid])


def _ensure_accounts_file():
    os.makedirs(USERS_DIR, exist_ok=True)
    if not os.path.exists(ACCOUNTS_FILE):
        with open(ACCOUNTS_FILE, 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow(['login', 'password_hash'])


def _find_user(login):
    _ensure_accounts_file()
    with open(ACCOUNTS_FILE, 'r', newline='', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row['login'] == login:
                return row
    return None


@auth_bp.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'GET':
        return render_template('register.html')

    login = request.form.get('login', '').strip()
    password = request.form.get('password', '').strip()

    if not login or not password:
        return render_template('register.html', error='Login and password are required.')

    if _find_user(login):
        return render_template('register.html', error='This login is already taken.')

    _ensure_accounts_file()
    with open(ACCOUNTS_FILE, 'a', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow([login, generate_password_hash(password)])

    session['user_type'] = 'registered'
    session['login'] = login
    session['visited_rooms'] = load_user_rooms(login)
    return redirect(url_for('index'))


@auth_bp.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'GET':
        return render_template('login.html')

    login_val = request.form.get('login', '').strip()
    password = request.form.get('password', '').strip()

    user = _find_user(login_val)
    if not user or not check_password_hash(user['password_hash'], password):
        return render_template('login.html', error='Invalid login or password.')

    session['user_type'] = 'registered'
    session['login'] = login_val
    session['visited_rooms'] = load_user_rooms(login_val)
    return redirect(url_for('index'))


@auth_bp.route('/logout', methods=['POST'])
def logout():
    session.clear()
    return redirect(url_for('index'))
