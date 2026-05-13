try:
    import eventlet
    eventlet.monkey_patch()
    _async_mode = 'eventlet'
except ImportError:
    _async_mode = 'threading'

import hashlib
import hmac
import os
import re
import secrets
import uuid
from datetime import datetime, timezone, timedelta

from flask import Flask, render_template, request, session, send_from_directory
from flask_session import Session
from markupsafe import Markup, escape as html_escape

from extensions import db, login_manager, csrf, socketio

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

# ProxyFix должен работать ТОЛЬКО когда приложение реально за реверс-прокси,
# иначе атакующий подделает X-Forwarded-For и обойдёт rate limit по IP.
# Включаем через env BEHIND_PROXY=1.
if os.environ.get('BEHIND_PROXY', '').strip() in ('1', 'true', 'yes', 'on'):
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
# Включаем строгую проверку Referer на HTTPS — закрывает class CSRF-атак
# через mixed-content и подделанные origin'ы. Отключить только если фронт
# и бэк на разных origins без CORS-кооперации.
app.config['WTF_CSRF_SSL_STRICT'] = True
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
# CORS для Socket.IO: по умолчанию same-origin only (cors_allowed_origins=None).
# Можно явно расширить через env ALLOWED_ORIGINS="https://example.com,https://x.com".
_socket_origins_env = os.environ.get('ALLOWED_ORIGINS', '').strip()
if _socket_origins_env:
    _socket_origins = [o.strip() for o in _socket_origins_env.split(',') if o.strip()]
else:
    _socket_origins = None  # same-origin only (защита от cross-site WebSocket hijacking)
socketio.init_app(app, cors_allowed_origins=_socket_origins, async_mode=_async_mode, manage_session=False)

from auth import auth_bp
from rooms import rooms_bp, get_room_display_name
from api import api_bp

app.register_blueprint(auth_bp)
app.register_blueprint(rooms_bp)
app.register_blueprint(api_bp, url_prefix='/api')

# Автоматическая миграция при старте (idempotent, работает и под gunicorn).
with app.app_context():
    try:
        from sqlalchemy import text, inspect as sa_inspect
        insp = sa_inspect(db.engine)
        existing_cols = {c['name'] for c in insp.get_columns('rooms')}
        if 'tg_visible' not in existing_cols:
            with db.engine.connect() as _conn:
                _conn.execute(text('ALTER TABLE rooms ADD COLUMN tg_visible BOOLEAN NOT NULL DEFAULT 0'))
                _conn.commit()
    except Exception:
        pass  # таблица ещё не создана — create_all создаст с нужной колонкой

# Освобождаем blueprint от автоматической CSRF-проверки.
# Внутри api.py сами вызываем csrf.protect() для эндпоинтов, использующих
# session-cookie, и пропускаем проверку только когда есть валидный X-Api-Key.
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


@app.template_filter('render_text')
def render_text_filter(text):
    """Рендерит текст: ```код``` → блок кода, \n → <br>."""
    parts = re.split(r'```([\s\S]*?)```', text or '')
    result = []
    for i, part in enumerate(parts):
        if i % 2 == 0:
            result.append(str(html_escape(part)).replace('\n', '<br>'))
        else:
            code = re.sub(r'^\r?\n', '', re.sub(r'\r?\n$', '', part))
            escaped = str(html_escape(code))
            result.append(
                '<div class="code-block">'
                '<div class="code-block-bar">'
                '<span class="code-block-label-wrap">'
                '<span class="code-block-stripes">////</span>'
                '<span class="code-block-label">code</span>'
                '</span>'
                '<button class="code-copy-btn" type="button">Копировать</button>'
                '</div>'
                f'<pre class="code-pre"><code>{escaped}</code></pre>'
                '</div>'
            )
    return Markup(''.join(result))


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


def _sign_anon(fp: str) -> str:
    """HMAC-подпись fingerprint от server secret. Cookie = fp.sig."""
    sig = hmac.new(app.secret_key.encode(), fp.encode(), hashlib.sha256).hexdigest()[:32]
    return f'{fp}.{sig}'


def _verify_anon_cookie(value: str) -> str | None:
    """Возвращает fingerprint только если подпись валидна."""
    if not value or '.' not in value:
        return None
    fp, sig = value.rsplit('.', 1)
    expected = hmac.new(app.secret_key.encode(), fp.encode(), hashlib.sha256).hexdigest()[:32]
    if not hmac.compare_digest(sig, expected):
        return None
    # fingerprint должен быть нашего формата (hex 32 символа), иначе отбрасываем.
    if len(fp) != 32 or not all(c in '0123456789abcdef' for c in fp):
        return None
    return fp


def _get_anon_token():
    return request.cookies.get('vsc_anon')


@app.before_request
def assign_anon_id():
    if 'user_type' not in session:
        from models import AnonIdentity
        raw = _get_anon_token()
        fp = _verify_anon_cookie(raw) if raw else None
        identity = None
        if fp:
            # Лукап только по проверенному fingerprint — никаких чужих захватов.
            identity = AnonIdentity.query.filter_by(fingerprint=fp).first()
        if not identity:
            # Генерируем НОВЫЙ fingerprint ТОЛЬКО серверной стороной.
            new_fp = secrets.token_hex(16)
            identity = AnonIdentity(fingerprint=new_fp)
            db.session.add(identity)
            db.session.commit()
        session['user_type'] = 'anon'
        session['anon_id'] = f'Anon{identity.id}'
        session['_anon_fp'] = identity.fingerprint
    elif session.get('user_type') == 'registered' and 'personal_room_id' not in session:
        from auth import _ensure_personal_room
        login = session.get('login')
        if login:
            personal_id = _ensure_personal_room(login)
            session['personal_room_id'] = personal_id
            session.modified = True


@app.after_request
def set_anon_cookie(response):
    if session.get('user_type') == 'anon':
        fp = session.get('_anon_fp')
        existing = _verify_anon_cookie(request.cookies.get('vsc_anon', ''))
        # Ставим/перевыпускаем cookie, если её ещё нет ИЛИ она с невалидной подписью.
        if fp and existing != fp:
            response.set_cookie(
                'vsc_anon', _sign_anon(fp),
                max_age=365 * 24 * 3600,
                httponly=True,
                samesite='Lax',
                secure=app.config.get('SESSION_COOKIE_SECURE', False),
            )
    return response


@app.after_request
def set_security_headers(response):
    """Минимальные defense-in-depth заголовки на все ответы."""
    # Контент рендерится через {{ ... }} (Jinja autoescape), пользовательский
    # HTML не вставляется — но inline-скрипты в шаблонах используются,
    # поэтому 'unsafe-inline' для script-src оставляем. style — тоже inline.
    # Закрываем frame-embedding и MIME-sniffing, ужесточаем Referrer-Policy.
    response.headers.setdefault(
        'Content-Security-Policy',
        "default-src 'self'; "
        "img-src 'self' data: blob:; "
        "media-src 'self' blob:; "
        "font-src 'self' https://fonts.gstatic.com data:; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "script-src 'self' 'unsafe-inline'; "
        "connect-src 'self' ws: wss:; "
        "frame-ancestors 'none'; "
        "base-uri 'self'; "
        "form-action 'self'; "
        "object-src 'none'"
    )
    response.headers.setdefault('X-Content-Type-Options', 'nosniff')
    response.headers.setdefault('X-Frame-Options', 'DENY')
    response.headers.setdefault('Referrer-Policy', 'same-origin')
    response.headers.setdefault('Permissions-Policy', 'geolocation=(), microphone=(), camera=(), payment=()')
    # HSTS — только если действительно на HTTPS.
    if request.is_secure:
        response.headers.setdefault('Strict-Transport-Security', 'max-age=31536000; includeSubDomains')
    return response


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
    socketio.run(app, debug=False)
