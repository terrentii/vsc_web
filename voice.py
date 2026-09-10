"""Голосовые каналы и демонстрация экрана — сигналинг для WebRTC-mesh.

Сервер медиа НЕ проксирует: браузеры соединяются напрямую (или через TURN),
здесь только обмен SDP/ICE между участниками одной комнаты.

ponytail: mesh, каждый шлёт каждому. До ~6 человек нормально; выше —
нужен SFU (mediasoup/janus), это отдельный сервис, а не правка этого файла.
"""
import json
import os

from flask import request, session
from flask_socketio import join_room, leave_room, emit

from extensions import socketio
from rooms import _can_access_room

# STUN хватает для звонков внутри одной сети и при «дружелюбном» NAT.
# Через интернет за symmetric NAT нужен TURN. Обычный путь — три простые
# переменные (их выставляет deploy.sh):
#   TURN_URL=turn:soufos.ru:3478  TURN_USER=vsc  TURN_PASSWORD=...
# Сложные конфигурации (несколько серверов, TURNS) — через ICE_SERVERS с JSON.
_DEFAULT_ICE = [{"urls": "stun:stun.l.google.com:19302"}]


def _ice_from_env():
    raw = os.environ.get('ICE_SERVERS', '').strip()
    if raw:
        try:
            return json.loads(raw) or _DEFAULT_ICE
        except ValueError:
            pass  # битый JSON — не роняем приложение, откатываемся ниже

    url = os.environ.get('TURN_URL', '').strip()
    if not url:
        return _DEFAULT_ICE

    entry = {"urls": url}
    user = os.environ.get('TURN_USER', '').strip()
    if user:
        entry["username"] = user
        entry["credential"] = os.environ.get('TURN_PASSWORD', '')
    # coturn на том же порту отвечает и как STUN — srflx-кандидат дешевле relay.
    if url.startswith('turn:'):
        return [{"urls": 'stun:' + url[len('turn:'):]}, entry]
    return [entry]


ICE_SERVERS = _ice_from_env()

# sid -> (room_id, display_name). ponytail: обычный dict без блокировки —
# eventlet-гринлеты в одном воркере, операции над dict атомарны.
# При переходе на несколько воркеров нужен message_queue у SocketIO.
_peers: dict[str, tuple[str, str]] = {}


def _voice_channel(room_id: str) -> str:
    return 'voice:' + room_id


def _drop(sid: str) -> None:
    entry = _peers.pop(sid, None)
    if not entry:
        return
    room_id, _name = entry
    channel = _voice_channel(room_id)
    leave_room(channel, sid=sid)
    emit('voice_peer_left', {'sid': sid}, to=channel)


@socketio.on('voice_join')
def on_voice_join(data):
    room_id = str((data or {}).get('room_id', ''))
    if not _can_access_room(room_id):
        return
    name = session.get('login') or session.get('anon_id') or 'Аноним'
    channel = _voice_channel(room_id)

    # Список тех, кто уже в канале. Новичок только ОТВЕЧАЕТ на их offer'ы —
    # односторонняя инициация убирает glare, perfect negotiation не нужен.
    others = [{'sid': s, 'name': n} for s, (r, n) in _peers.items() if r == room_id]

    _peers[request.sid] = (room_id, name)
    join_room(channel)

    emit('voice_peers', {'peers': others, 'ice': ICE_SERVERS, 'self': request.sid})
    emit('voice_peer_joined', {'sid': request.sid, 'name': name},
         to=channel, include_self=False)


@socketio.on('voice_signal')
def on_voice_signal(data):
    """Прозрачный релей SDP/ICE. Содержимое не парсим — только проверяем,
    что отправитель и получатель находятся в одном голосовом канале."""
    data = data or {}
    target = data.get('to')
    me = _peers.get(request.sid)
    peer = _peers.get(target)
    if not me or not peer or me[0] != peer[0]:
        return
    emit('voice_signal', {'from': request.sid, 'data': data.get('data')}, to=target)


@socketio.on('voice_leave')
def on_voice_leave(_data=None):
    _drop(request.sid)


@socketio.on('disconnect')
def on_voice_disconnect(*_args):
    _drop(request.sid)
