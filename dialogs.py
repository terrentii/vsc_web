"""Настройки комнат, диалоги, аватарки, «печатает / прочитано / в сети».

Спецификация: vsc_mobile/docs/superpowers/specs/2026-10-23-mys-dialogs-design.md.
Правила здесь — общие для Bearer-API приложений (blueprint ниже) и веб-страниц
(rooms.py). Событие уходит мостом: приложениям по /ws, вебу по Socket.IO.
"""
import io
import os
import shutil
import threading
import time
import uuid
from collections import defaultdict, deque
from datetime import datetime, timezone

from flask import Blueprint, g, jsonify, request, send_file
from PIL import Image, ImageOps, UnidentifiedImageError

from bearer import require_bearer
from extensions import db, socketio
from media_cleanup import ROOMS_DIR
from models import Message, Room, RoomMember, User
from ws_centralized import fanout_event, subscribe_user, unsubscribe_user

MAX_TITLE = 64
AVATAR_MAX = 5 * 1024 * 1024
AVATAR_SIZE = 256
AVATAR_FORMATS = {'PNG', 'JPEG', 'WEBP'}
INVITES_PER_DAY = 20
TYPING_MIN_GAP = 2.0
Image.MAX_IMAGE_PIXELS = 40_000_000  # «бомба» распаковки — отказ, а не нехватка памяти

ERROR_TEXT = {
    'forbidden': 'Нет прав на это действие.',
    'not_found': 'Не найдено.',
    'dialog': 'В диалоге состав и открытость менять нельзя.',
    'not_dialog': 'Доступно только в диалоге.',
    'already_dialog': 'Комната уже диалог.',
    'not_eligible': 'Диалог возможен только в комнате ровно на двух зарегистрированных.',
    'no_proposal': 'Нет предложения диалога.',
    'self': 'Нельзя выбрать самого себя.',
    'user_not_found': 'Пользователь не найден.',
    'not_member': 'Такого участника нет.',
    'creator': 'Создатель не может покинуть комнату — только удалить её.',
    'rate_limited': 'Слишком много приглашений за сутки.',
    'bad_image': 'Нужна картинка PNG, JPEG или WebP.',
    'too_large': 'Картинка больше 5 МБ.',
    'bad_request': 'Неверный запрос.',
}

dialogs_bp = Blueprint('dialogs', __name__)


class DialogError(Exception):
    def __init__(self, code: str, status: int = 409):
        super().__init__(code)
        self.code, self.status = code, status


@dialogs_bp.errorhandler(DialogError)
def _on_error(e):
    return jsonify({'error': e.code}), e.status


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ── состав и правила ─────────────────────────────────────────────────────────

def members(room):
    return RoomMember.query.filter_by(room_id=room.room_id).all()


def member(room, login):
    return RoomMember.query.filter_by(room_id=room.room_id, login=login).first() if login else None


def peer_of(room, login):
    """Второй участник комнаты на двоих (диалог, приглашение, пригодная), иначе None."""
    if room.personal_login:
        return None
    ms = members(room)
    if len(ms) != 2:
        return None
    others = [m for m in ms if m.login != login]
    return others[0] if len(others) == 1 else None


def eligible(room) -> bool:
    ms = members(room)
    return not room.personal_login and len(ms) == 2 and all(m.role != 'invited' for m in ms)


def pending_invite(room) -> bool:
    return RoomMember.query.filter_by(room_id=room.room_id, role='invited').first() is not None


def membership_changed(room) -> None:
    """Состав изменился: предложение сгорает, диалог без пары выключается. До commit."""
    db.session.flush()
    room.dialog_proposed_by = None
    if room.is_dialog and len(members(room)) != 2:
        room.is_dialog = False


def _require_member(room, login):
    m = member(room, login)
    if m is None:
        raise DialogError('forbidden', 403)
    return m


def _require_creator(room, login):
    _require_member(room, login)
    if room.creator_login != login:
        raise DialogError('forbidden', 403)


def room_updated_at(room) -> str:
    last = db.session.query(db.func.max(Message.timestamp)).filter(Message.room_id == room.room_id).scalar()
    return (last or room.created_at).isoformat()


def room_view(room, login) -> dict:
    """Комната глазами login: эффективные название/аватар, собеседник, его прочтение/статус."""
    me = member(room, login)
    ms = members(room)
    peer = peer_of(room, login)
    peer_user = User.query.filter_by(login=peer.login).first() if peer else None
    if room.is_dialog:
        title = (me.title if me and me.title else None) or (peer.login if peer else None)
        avatar_rev = me.avatar_rev if me and me.avatar else 0
    else:
        # приглашение без названия — показываем того, кто на другом конце
        invite_peer = peer.login if peer and room.dialog_proposed_by and pending_invite(room) else None
        title = room.name or invite_peer
        avatar_rev = room.avatar_rev if room.avatar else 0
    live = bool(room.is_dialog and peer_user)
    return {
        'id': room.id, 'code': room.room_id, 'name': room.name or None,
        'is_direct': room.personal_login is not None, 'updated_at': room_updated_at(room),
        'is_open': bool(room.is_open), 'is_dialog': bool(room.is_dialog),
        'dialog_proposed_by': room.dialog_proposed_by,
        'pending_invite': pending_invite(room),
        'role': me.role if me else None, 'title': title, 'avatar_rev': avatar_rev,
        'peer': peer.login if peer else None,
        'peer_read': peer.last_read if live and peer_user.share_read else None,
        'peer_online': is_online(peer.login) if live and peer_user.share_online else None,
        'members': len(ms),
    }


def emit(room, kind, **data):
    """Событие участникам комнаты: приложениям по /ws, вебу по Socket.IO."""
    fanout_event(room.id, {'type': kind, **data})
    socketio.emit(kind, {'room_id': room.room_id, **data}, room=room.room_id)


# ── диалог ───────────────────────────────────────────────────────────────────

def _accept(room):
    room.is_dialog = True
    room.is_open = False
    room.tg_visible = False
    room.dialog_proposed_by = None
    for m in members(room):
        if m.role == 'invited':
            m.role = 'member'


def dialog_action(room, login, action) -> bool:
    """propose/accept/decline/disable. False — комната удалена (отклонённое приглашение)."""
    _require_member(room, login)
    if action == 'propose':
        if room.is_dialog:
            raise DialogError('already_dialog')
        if not eligible(room):
            raise DialogError('not_eligible')
        if room.dialog_proposed_by and room.dialog_proposed_by != login:
            _accept(room)  # встречное предложение — согласие
        else:
            room.dialog_proposed_by = login
    elif action == 'accept':
        if not room.dialog_proposed_by or room.dialog_proposed_by == login:
            raise DialogError('no_proposal')
        if room.personal_login or len(members(room)) != 2:
            raise DialogError('not_eligible')
        _accept(room)
    elif action == 'decline':
        if not room.dialog_proposed_by:
            raise DialogError('no_proposal')
        room.dialog_proposed_by = None
        invited = RoomMember.query.filter_by(room_id=room.room_id, role='invited').first()
        if invited is not None:
            if Message.query.filter_by(room_id=room.room_id).count() == 0:
                delete_room(room)
                return False
            db.session.delete(invited)
            unsubscribe_user(invited.login, room.id)
    elif action == 'disable':
        if not room.is_dialog:
            raise DialogError('not_dialog')
        room.is_dialog = False
    else:
        raise DialogError('bad_request', 400)
    db.session.commit()
    emit(room, 'room_updated')
    return True


def delete_room(room) -> None:
    emit(room, 'room_removed')
    rid, code = room.id, room.room_id
    logins = [m.login for m in members(room)]
    Message.query.filter_by(room_id=code).delete()
    RoomMember.query.filter_by(room_id=code).delete()
    db.session.delete(room)
    db.session.commit()
    for login in logins:
        unsubscribe_user(login, rid)
    shutil.rmtree(os.path.join(ROOMS_DIR, code), ignore_errors=True)


_invites: dict[str, deque] = defaultdict(deque)  # логин → время приглашений (сутки)


def invite(caller, login):
    """«Написать пользователю»: (комната, создана_ли). Повтор — существующая комната."""
    login = (login or '').strip()
    if login == caller:
        raise DialogError('self', 400)
    if User.query.filter_by(login=login).first() is None:
        raise DialogError('user_not_found', 404)
    for m in RoomMember.query.filter_by(login=caller).all():
        r = Room.query.filter_by(room_id=m.room_id).first()
        if r is not None and (r.is_dialog or pending_invite(r)):
            p = peer_of(r, caller)
            if p is not None and p.login == login:
                return r, False
    q, now = _invites[caller], time.time()
    while q and now - q[0] > 86400:
        q.popleft()
    if len(q) >= INVITES_PER_DAY:
        raise DialogError('rate_limited', 429)
    q.append(now)
    import rooms  # ленивый импорт: rooms импортирует dialogs
    code, t = rooms._generate_room_id(), _now()
    room = Room(room_id=code, name='', is_open=False, created_at=t, creator_login=caller,
                dialog_proposed_by=caller)
    db.session.add(room)
    db.session.add(RoomMember(room_id=code, login=caller, joined_at=t, role='godfather'))
    db.session.add(RoomMember(room_id=code, login=login, joined_at=t, role='invited'))
    db.session.commit()
    os.makedirs(os.path.join(ROOMS_DIR, code, 'media'), exist_ok=True)
    subscribe_user(caller, room.id)
    subscribe_user(login, room.id)
    emit(room, 'room_updated')
    return room, True


# ── общие настройки и состав ─────────────────────────────────────────────────

def update_settings(room, login, name=None, is_open=None, tg_visible=None):
    _require_creator(room, login)
    if room.is_dialog and (is_open is not None or tg_visible is not None):
        raise DialogError('dialog')
    if name is not None:
        room.name = name.strip()[:MAX_TITLE]
    if is_open is not None:
        room.is_open = bool(is_open)
    if tg_visible is not None:
        room.tg_visible = bool(tg_visible)
    if not room.is_open:
        room.tg_visible = False
    db.session.commit()
    emit(room, 'room_updated')


def add_member(room, login, target):
    _require_creator(room, login)
    if room.is_dialog:
        raise DialogError('dialog')
    if room.personal_login:
        raise DialogError('forbidden', 403)
    target = (target or '').strip()
    if User.query.filter_by(login=target).first() is None:
        raise DialogError('user_not_found', 404)
    if member(room, target) is not None:
        return
    db.session.add(RoomMember(room_id=room.room_id, login=target, joined_at=_now(), role='member'))
    membership_changed(room)
    db.session.commit()
    subscribe_user(target, room.id)
    emit(room, 'room_updated')


def remove_member(room, login, target):
    _require_creator(room, login)
    if room.is_dialog:
        raise DialogError('dialog')
    if target == login:
        raise DialogError('self', 400)
    m = member(room, target)
    if m is None:
        raise DialogError('not_member', 404)
    db.session.delete(m)
    membership_changed(room)
    db.session.commit()
    emit(room, 'room_updated')
    unsubscribe_user(target, room.id)


def leave(room, login):
    m = _require_member(room, login)
    if room.creator_login == login:
        raise DialogError('creator')
    db.session.delete(m)
    membership_changed(room)
    db.session.commit()
    emit(room, 'room_updated')
    unsubscribe_user(login, room.id)


def set_title(room, login, title):
    """Своё название диалога; видит только сам — события собеседнику не шлём."""
    m = _require_member(room, login)
    if not room.is_dialog:
        raise DialogError('not_dialog')
    m.title = (title or '').strip()[:MAX_TITLE] or None
    db.session.commit()


# ── аватарки ─────────────────────────────────────────────────────────────────

def _avatar_owner(room, login, write):
    m = _require_member(room, login)
    if room.is_dialog:
        return m
    if write and room.creator_login != login:
        raise DialogError('forbidden', 403)
    return room


def _normalize(data: bytes) -> bytes:
    """Квадрат 256×256 PNG без метаданных: EXIF (геометка, модель) не переживает пережатие."""
    if len(data) > AVATAR_MAX:
        raise DialogError('too_large', 413)
    try:
        img = Image.open(io.BytesIO(data))
        if img.format not in AVATAR_FORMATS:
            raise DialogError('bad_image', 400)
        img = ImageOps.exif_transpose(img)
        img = ImageOps.fit(img.convert('RGBA'), (AVATAR_SIZE, AVATAR_SIZE), Image.LANCZOS)
    except DialogError:
        raise
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
        raise DialogError('bad_image', 400)
    out = io.BytesIO()
    img.save(out, 'PNG')
    return out.getvalue()


def _avatar_dir(room):
    return os.path.join(ROOMS_DIR, room.room_id, 'avatars')


def _drop_file(room, name):
    if name:
        try:
            os.remove(os.path.join(_avatar_dir(room), os.path.basename(name)))
        except OSError:
            pass


def set_avatar(room, login, data: bytes):
    owner = _avatar_owner(room, login, write=True)
    png = _normalize(data)
    os.makedirs(_avatar_dir(room), exist_ok=True)
    name = uuid.uuid4().hex + '.png'
    with open(os.path.join(_avatar_dir(room), name), 'wb') as f:
        f.write(png)
    _drop_file(room, owner.avatar)
    owner.avatar = name
    owner.avatar_rev = (owner.avatar_rev or 0) + 1
    db.session.commit()
    if owner is room:
        emit(room, 'room_updated')


def delete_avatar(room, login):
    owner = _avatar_owner(room, login, write=True)
    _drop_file(room, owner.avatar)
    owner.avatar = None
    owner.avatar_rev = (owner.avatar_rev or 0) + 1
    db.session.commit()
    if owner is room:
        emit(room, 'room_updated')


def avatar_file(room, login):
    """(путь, rev) эффективной аватарки для login (None — участник открытой комнаты-гость)."""
    owner = _avatar_owner(room, login, write=False) if login and member(room, login) else room
    if room.is_dialog and owner is room:
        return None
    if not owner.avatar:
        return None
    return os.path.join(_avatar_dir(room), owner.avatar), owner.avatar_rev


# ── прочтение, печать, присутствие ───────────────────────────────────────────

def mark_read(room, login, message_id):
    m = _require_member(room, login)
    if not room.is_dialog:
        raise DialogError('not_dialog')
    try:
        msg = db.session.get(Message, int(message_id))
    except (TypeError, ValueError):
        raise DialogError('bad_request', 400)
    if msg is None or msg.room_id != room.room_id:
        raise DialogError('not_found', 404)
    if m.last_read is None or msg.id > m.last_read:
        m.last_read = msg.id
        db.session.commit()
        u = User.query.filter_by(login=login).first()
        if u is not None and u.share_read:
            emit(room, 'read', login=login, message_id=msg.id)


_typing_at: dict = {}


def typing(room, login):
    if room is None or not room.is_dialog or member(room, login) is None:
        return
    key, now = (room.id, login), time.monotonic()
    if now - _typing_at.get(key, 0.0) < TYPING_MIN_GAP:
        return
    _typing_at[key] = now
    emit(room, 'typing', login=login)


_online: dict[str, int] = {}
_plock = threading.Lock()


def is_online(login) -> bool:
    return _online.get(login, 0) > 0


def connected(login):
    with _plock:
        n = _online.get(login, 0) + 1
        _online[login] = n
    if n == 1:
        _presence(login, True)


def disconnected(login):
    with _plock:
        n = _online.get(login, 0) - 1
        if n > 0:
            _online[login] = n
        else:
            _online.pop(login, None)
    if n <= 0:
        _presence(login, False)


def _presence(login, online, force=False):
    u = User.query.filter_by(login=login).first()
    if u is None or (not u.share_online and not force):
        return
    for m in RoomMember.query.filter_by(login=login).all():
        r = Room.query.filter_by(room_id=m.room_id).first()
        if r is not None and r.is_dialog:
            emit(r, 'presence', login=login, online=online)


def set_privacy(login, share_read=None, share_online=None):
    u = User.query.filter_by(login=login).first()
    if share_read is not None:
        u.share_read = bool(share_read)
    if share_online is not None:
        was = u.share_online
        u.share_online = bool(share_online)
        if was and not u.share_online:
            _presence(login, False, force=True)  # перестали показывать — для собеседника «не в сети»
    db.session.commit()
    return u


# ── Bearer-API ───────────────────────────────────────────────────────────────

def _room(room_id):
    room = db.session.get(Room, room_id)
    if room is None:
        raise DialogError('not_found', 404)
    _require_member(room, g.caller_login)
    return room


def _view(room):
    return jsonify(room_view(room, g.caller_login))


@dialogs_bp.get('/rooms/<int:room_id>')
@require_bearer
def api_room(room_id):
    room = _room(room_id)
    v = room_view(room, g.caller_login)
    v['member_list'] = [{'login': m.login, 'role': m.role} for m in members(room)]
    return jsonify(v)


@dialogs_bp.patch('/rooms/<int:room_id>')
@require_bearer
def api_update(room_id):
    room = _room(room_id)
    d = request.get_json(silent=True) or {}
    update_settings(room, g.caller_login, d.get('name'), d.get('is_open'))
    return _view(room)


@dialogs_bp.post('/rooms/<int:room_id>/members')
@require_bearer
def api_add_member(room_id):
    room = _room(room_id)
    add_member(room, g.caller_login, (request.get_json(silent=True) or {}).get('login'))
    return _view(room)


@dialogs_bp.delete('/rooms/<int:room_id>/members/<login>')
@require_bearer
def api_remove_member(room_id, login):
    room = _room(room_id)
    remove_member(room, g.caller_login, login)
    return _view(room)


@dialogs_bp.post('/rooms/<int:room_id>/leave')
@require_bearer
def api_leave(room_id):
    leave(_room(room_id), g.caller_login)
    return '', 204


@dialogs_bp.post('/rooms/<int:room_id>/dialog')
@require_bearer
def api_dialog(room_id):
    room = _room(room_id)
    action = (request.get_json(silent=True) or {}).get('action')
    if not dialog_action(room, g.caller_login, action):
        return jsonify({'deleted': True})
    return _view(room)


@dialogs_bp.post('/dialogs')
@require_bearer
def api_invite():
    room, created = invite(g.caller_login, (request.get_json(silent=True) or {}).get('login'))
    return jsonify(room_view(room, g.caller_login)), 201 if created else 200


@dialogs_bp.patch('/rooms/<int:room_id>/me')
@require_bearer
def api_me_in_room(room_id):
    room = _room(room_id)
    set_title(room, g.caller_login, (request.get_json(silent=True) or {}).get('title'))
    return _view(room)


@dialogs_bp.put('/rooms/<int:room_id>/avatar')
@require_bearer
def api_set_avatar(room_id):
    room = _room(room_id)
    f = request.files.get('file')
    if f is None:
        raise DialogError('bad_request', 400)
    set_avatar(room, g.caller_login, f.read(AVATAR_MAX + 1))
    return _view(room)


@dialogs_bp.delete('/rooms/<int:room_id>/avatar')
@require_bearer
def api_delete_avatar(room_id):
    room = _room(room_id)
    delete_avatar(room, g.caller_login)
    return _view(room)


@dialogs_bp.get('/rooms/<int:room_id>/avatar')
@require_bearer
def api_get_avatar(room_id):
    found = avatar_file(_room(room_id), g.caller_login)
    if found is None or not os.path.isfile(found[0]):
        raise DialogError('not_found', 404)
    resp = send_file(found[0], mimetype='image/png', etag=str(found[1]), max_age=0)
    resp.headers['X-Content-Type-Options'] = 'nosniff'
    return resp


@dialogs_bp.post('/rooms/<int:room_id>/read')
@require_bearer
def api_read(room_id):
    mark_read(_room(room_id), g.caller_login, (request.get_json(silent=True) or {}).get('message_id'))
    return '', 204


def _me(u):
    return jsonify({'login': u.login, 'share_read': bool(u.share_read), 'share_online': bool(u.share_online)})


@dialogs_bp.get('/me')
@require_bearer
def api_me():
    return _me(User.query.filter_by(login=g.caller_login).first())


@dialogs_bp.patch('/me')
@require_bearer
def api_me_update():
    d = request.get_json(silent=True) or {}
    return _me(set_privacy(g.caller_login, d.get('share_read'), d.get('share_online')))
