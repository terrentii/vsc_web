"""RAW WebSocket endpoint /ws для десктоп-клиента (§7).

Протокол:
  Клиент → {"type":"auth","token":"<raw>"}
  Сервер → {"type":"ready"}  или  {"type":"error","code":"unauthorized"}

После ready сервер пушит новые сообщения в комнатах пользователя:
  {"type":"message","room_id":<int>,"id":<int>,"sender":"...","body":"...","created_at":"..."}

fanout_message(room_db_id, payload) вызывается из ОБОИХ путей записи после commit.
payload: {"id":..., "sender":..., "body":..., "created_at":...}
"""
import json
import threading

from flask import Blueprint
from flask_sock import Sock

from bearer import resolve_bearer_token
from models import Room, RoomMember

sock = Sock()                          # init_app вызывается в app.py
ws_bp = Blueprint('ws_centralized', __name__)

_subs: dict[int, set] = {}            # Room.id (int PK) -> set(ws)
_ws_rooms: dict[object, set] = {}     # ws -> set(Room.id)
_lock = threading.Lock()


def _rooms_for(login: str) -> list[int]:
    """Целочисленные PK комнат, в которых состоит пользователь."""
    member_rows = RoomMember.query.filter_by(login=login).all()
    str_room_ids = [m.room_id for m in member_rows]
    if not str_room_ids:
        return []
    rooms = Room.query.filter(Room.room_id.in_(str_room_ids)).all()
    return [r.id for r in rooms]


def fanout_event(room_db_id: int, payload: dict) -> None:
    """Отправить произвольный кадр (с ключом type) всем WS-подписчикам комнаты.

    Вызывать ПОСЛЕ commit. Типы кадров: message / message_edited / message_deleted.
    """
    frame = json.dumps({"room_id": room_db_id, **payload})
    with _lock:
        targets = list(_subs.get(room_db_id, ()))
    for ws in targets:
        try:
            ws.send(frame)
        except Exception:
            pass


def fanout_message(room_db_id: int, payload: dict) -> None:
    """Отправить payload всем подписанным WebSocket-соединениям комнаты.

    Вызывать ПОСЛЕ commit из обоих путей записи.
    payload должен содержать ключи: id, sender, body, created_at.
    """
    fanout_event(room_db_id, {"type": "message", **payload})


@sock.route('/ws', bp=ws_bp)
def ws_centralized(ws):
    # Шаг 1: авторизация по первому кадру
    raw = ws.receive()
    if raw is None:
        return
    try:
        msg = json.loads(raw)
    except Exception:
        ws.send(json.dumps({"type": "error", "code": "bad_request"}))
        return

    if msg.get("type") != "auth":
        ws.send(json.dumps({"type": "error", "code": "unauthorized"}))
        return

    login = resolve_bearer_token(msg.get("token", ""))
    if not login:
        ws.send(json.dumps({"type": "error", "code": "unauthorized"}))
        return

    ws.send(json.dumps({"type": "ready"}))

    # Шаг 2: подписка на комнаты пользователя (DB-запрос здесь — в request-контексте)
    room_ids = _rooms_for(login)
    with _lock:
        _ws_rooms[ws] = set(room_ids)
        for rid in room_ids:
            _subs.setdefault(rid, set()).add(ws)

    # Шаг 3: держим соединение открытым, пока клиент не закроет его
    try:
        while True:
            data = ws.receive()
            if data is None:
                break
            # Входящие кадры после авторизации игнорируем (протокол push-only)
    finally:
        with _lock:
            for rid in _ws_rooms.pop(ws, ()):
                _subs.get(rid, set()).discard(ws)
