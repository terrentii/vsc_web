"""Сырой WebSocket /ws для десктоп-клиента (под-проект №6).

Голый WS на flask-sock (НЕ Socket.IO). Клиент шлёт первым кадром
{"type":"auth","token":"<raw>"}; сервер отвечает {"type":"ready"} либо
{"type":"error","code":"unauthorized"} и закрывает соединение. После ready
сервер пушит {"type":"message",...} по всем комнатам пользователя при появлении
нового сообщения — из обоих путей записи (REST §6.4 и веб post_message).

In-process реестр подписчиков: под eventlet это greenlet'ы одного процесса,
threading.Lock достаточно. Несколько воркеров потребуют общего брокера (follow-up).
"""
import json
import threading

from flask import Blueprint
from flask_sock import Sock

from bearer import resolve_bearer_token
from models import Room, RoomMember

sock = Sock()  # init_app в app.py
ws_bp = Blueprint('ws_centralized', __name__)

_subs: dict[int, set] = {}         # Room.id -> set(ws)
_ws_rooms: dict[object, set] = {}  # ws -> set(Room.id)
_lock = threading.Lock()


def _rooms_for(login: str) -> list[int]:
    rids = [m.room_id for m in RoomMember.query.filter_by(login=login).all()]
    if not rids:
        return []
    return [r.id for r in Room.query.filter(Room.room_id.in_(rids)).all()]


def fanout_message(room_db_id: int, payload: dict) -> None:
    """Пуш нового сообщения подписчикам комнаты.

    Вызывать ПОСЛЕ commit из обоих путей записи. payload — поля сообщения
    (id, sender, body, created_at, room_id) уже в форме контракта (§4).
    """
    frame = json.dumps({"type": "message", **payload})
    with _lock:
        targets = list(_subs.get(room_db_id, ()))
    for ws in targets:
        try:
            ws.send(frame)
        except Exception:
            pass


@sock.route('/ws', bp=ws_bp)
def ws_centralized(ws):
    raw = ws.receive()  # первый кадр — auth
    try:
        msg = json.loads(raw)
    except Exception:
        ws.send(json.dumps({"type": "error", "code": "bad_request"}))
        return
    login = resolve_bearer_token(msg.get("token", "")) if msg.get("type") == "auth" else None
    if not login:
        ws.send(json.dumps({"type": "error", "code": "unauthorized"}))
        return
    ws.send(json.dumps({"type": "ready"}))
    room_ids = _rooms_for(login)
    with _lock:
        _ws_rooms[ws] = set(room_ids)
        for rid in room_ids:
            _subs.setdefault(rid, set()).add(ws)
    try:
        while True:
            if ws.receive() is None:  # клиент закрыл соединение
                break
    finally:
        with _lock:
            for rid in _ws_rooms.pop(ws, ()):
                subs = _subs.get(rid)
                if subs:
                    subs.discard(ws)
                    if not subs:
                        _subs.pop(rid, None)
