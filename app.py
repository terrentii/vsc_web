import os
import uuid
from datetime import datetime, timezone, timedelta

from flask import Flask, render_template, session, send_from_directory
from flask_session import Session

from extensions import db, login_manager, csrf

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', uuid.uuid4().hex)

app.config['MAX_CONTENT_LENGTH'] = 5 * 1024 ** 3  # 5 GB
app.config['SESSION_TYPE'] = 'filesystem'
app.config['SESSION_FILE_DIR'] = os.path.join(os.path.dirname(__file__), 'flask_session')
app.config['SESSION_PERMANENT'] = False
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get(
    'DATABASE_URL',
    'sqlite:///' + os.path.join(os.path.dirname(__file__), 'app.db')
)
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['WTF_CSRF_TIME_LIMIT'] = None  # токен не истекает вместе с сессией

Session(app)
db.init_app(app)
login_manager.init_app(app)
csrf.init_app(app)

from auth import auth_bp
from rooms import rooms_bp, get_room_display_name
from api import api_bp

app.register_blueprint(auth_bp)
app.register_blueprint(rooms_bp)
app.register_blueprint(api_bp, url_prefix='/api')

csrf.exempt(api_bp)

MONTHS = ['янв', 'фев', 'мар', 'апр', 'май', 'июн',
          'июл', 'авг', 'сен', 'окт', 'ноя', 'дек']

MSK = timezone(timedelta(hours=3))


@login_manager.user_loader
def load_user(login):
    from models import User
    return User.query.filter_by(login=login).first()


@app.template_filter('ts')
def format_ts(value):
    try:
        if isinstance(value, datetime):
            dt = value.replace(tzinfo=timezone.utc).astimezone(MSK)
        else:
            dt = datetime.fromisoformat(str(value))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc).astimezone(MSK)
    except (ValueError, TypeError):
        return str(value) if value else ''
    today_msk = datetime.now(MSK).date()
    d = dt.date()
    time_str = dt.strftime('%H:%M')
    if d == today_msk:
        return f'сегодня {time_str}'
    if (today_msk - d).days == 1:
        return f'вчера {time_str}'
    return f'{d.day} {MONTHS[d.month - 1]} {time_str}'


def get_room_info(room_id):
    """Return dict with id, name, is_open for sidebar display."""
    from models import Room
    room = Room.query.filter_by(room_id=room_id).first()
    if room:
        return {'id': room_id, 'name': room.name or '', 'is_open': room.is_open}
    return {'id': room_id, 'name': '', 'is_open': True}


@app.context_processor
def inject_room_helpers():
    return {'room_display_name': get_room_display_name, 'get_room_info': get_room_info}


@app.before_request
def assign_anon_id():
    if 'user_type' not in session:
        session['user_type'] = 'anon'
        existing = session.get('_anon_counter', 0) + 1
        session['_anon_counter'] = existing
        session['anon_id'] = f'Anon{existing}'


@app.route('/sw.js')
def service_worker():
    return send_from_directory('static', 'sw.js', mimetype='application/javascript')


@app.route('/')
def index():
    return render_template('index.html')


@app.errorhandler(404)
def not_found(e):
    return render_template('404.html'), 404


@app.errorhandler(500)
def server_error(e):
    return render_template('500.html'), 500


if __name__ == '__main__':
    os.makedirs('rooms', exist_ok=True)
    with app.app_context():
        db.create_all()
    app.run(debug=True)
