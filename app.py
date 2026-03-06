import os
import uuid
from datetime import datetime, timezone, date
from flask import Flask, render_template, request, redirect, url_for, session
from flask_session import Session
from auth import auth_bp
from rooms import rooms_bp, get_room_display_name

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', uuid.uuid4().hex)

app.config['SESSION_TYPE'] = 'filesystem'
app.config['SESSION_FILE_DIR'] = os.path.join(os.path.dirname(__file__), 'flask_session')
app.config['SESSION_PERMANENT'] = False
Session(app)

app.register_blueprint(auth_bp)
app.register_blueprint(rooms_bp)


MONTHS = ['янв', 'фев', 'мар', 'апр', 'май', 'июн',
          'июл', 'авг', 'сен', 'окт', 'ноя', 'дек']


@app.template_filter('ts')
def format_ts(value):
    """Форматирует ISO-timestamp в читабельный вид: сегодня/вчера/дата + время."""
    try:
        dt = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return value
    today = date.today()
    d = dt.date()
    time_str = dt.strftime('%H:%M')
    if d == today:
        return f'сегодня {time_str}'
    if (today - d).days == 1:
        return f'вчера {time_str}'
    return f'{d.day} {MONTHS[d.month - 1]} {time_str}'


@app.context_processor
def inject_room_helpers():
    # делаем get_room_display_name доступной во всех шаблонах
    return {'room_display_name': get_room_display_name}


@app.before_request
def assign_anon_id():
    if 'user_type' not in session:
        session['user_type'] = 'anon'
        existing = session.get('_anon_counter', 0) + 1
        session['_anon_counter'] = existing
        session['anon_id'] = f'Anon{existing}'


@app.route('/')
def index():
    return render_template('index.html')


if __name__ == '__main__':
    os.makedirs('rooms', exist_ok=True)
    os.makedirs('users', exist_ok=True)
    app.run(debug=True)
