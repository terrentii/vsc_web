import hashlib
import os
import uuid
from datetime import datetime, timezone, timedelta

from flask import Flask, render_template, request, session, send_from_directory
from flask_session import Session

from extensions import db, login_manager, csrf

app = Flask(__name__)

# Постоянный секретный ключ — читается из env или из файла, генерируется один раз
def _get_secret_key():
    if os.environ.get('SECRET_KEY'):
        return os.environ['SECRET_KEY']
    key_file = os.path.join(os.path.dirname(__file__), '.secret_key')
    if os.path.exists(key_file):
        with open(key_file) as f:
            key = f.read().strip()
        if key:
            return key
    key = uuid.uuid4().hex + uuid.uuid4().hex
    with open(key_file, 'w') as f:
        f.write(key)
    os.chmod(key_file, 0o600)
    return key

app.secret_key = _get_secret_key()

from werkzeug.middleware.proxy_fix import ProxyFix
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=0)

app.config['MAX_CONTENT_LENGTH'] = 5 * 1024 ** 3  # 5 GB

# Настройки сессии
app.config['SESSION_TYPE'] = 'filesystem'
app.config['SESSION_FILE_DIR'] = os.path.join(os.path.dirname(__file__), 'flask_session')
app.config['SESSION_PERMANENT'] = True
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=7)
app.config['SESSION_COOKIE_NAME'] = 'vsc_sid'
app.config['SESSION_COOKIE_PATH'] = '/'
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_SECURE'] = True
app.config['SESSION_COOKIE_DOMAIN'] = None

# CSRF настройки
app.config['WTF_CSRF_ENABLED'] = True
app.config['WTF_CSRF_TIME_LIMIT'] = None
app.config['WTF_CSRF_SSL_STRICT'] = False
app.config['WTF_CSRF_CHECK_DEFAULT'] = True
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get(
    'DATABASE_URL',
    'sqlite:///' + os.path.join(os.path.dirname(__file__), 'app.db')
)
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
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
csrf.exempt(rooms_bp)  

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


def _anon_fingerprint():
    ip = request.remote_addr or ''
    ua = request.headers.get('User-Agent', '')
    return hashlib.sha256(f'{ip}|{ua}'.encode()).hexdigest()


@app.before_request
def assign_anon_id():
    if 'user_type' not in session:
        from models import AnonIdentity
        fp = _anon_fingerprint()
        identity = AnonIdentity.query.filter_by(fingerprint=fp).first()
        if not identity:
            identity = AnonIdentity(fingerprint=fp)
            db.session.add(identity)
            db.session.commit()
        session['user_type'] = 'anon'
        session['anon_id'] = f'Anon{identity.id}'
    elif session.get('user_type') == 'registered' and 'personal_room_id' not in session:
        from auth import _ensure_personal_room
        login = session.get('login')
        if login:
            personal_id = _ensure_personal_room(login)
            session['personal_room_id'] = personal_id
            session.modified = True


@app.route('/sw.js')
def service_worker():
    return send_from_directory('static', 'sw.js', mimetype='application/javascript')


@app.route('/')
def index():
    return render_template('index.html')


@app.errorhandler(400)
def bad_request(e):
    return render_template('400.html'), 400


@app.errorhandler(403)
def forbidden(e):
    return render_template('403.html'), 403


@app.errorhandler(404)
def not_found(e):
    return render_template('404.html'), 404


@app.errorhandler(405)
def method_not_allowed(e):
    return render_template('405.html'), 405


@app.errorhandler(413)
def too_large(e):
    return render_template('413.html'), 413


@app.errorhandler(500)
def server_error(e):
    return render_template('500.html'), 500


@app.errorhandler(503)
def service_unavailable(e):
    return render_template('503.html'), 503


if __name__ == '__main__':
    os.makedirs('rooms', exist_ok=True)
    with app.app_context():
        db.create_all()
    app.run(debug=True)
