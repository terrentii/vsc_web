# rendezvous_ws.py — WebSocket rendezvous + relay для децентрализованного режима.
# Никакой персистентности: всё in-memory и эфемерно. Сервер НЕ парсит payload RELAY.
import threading
from dataclasses import dataclass, field
from flask import Blueprint
from flask_sock import Sock

HELLO, PAIR, RELAY, PEER_LEFT = 1, 2, 5, 9
ROLE_INITIATOR, ROLE_RESPONDER = 0, 1
_HEADER = 6


def encode_frame(mtype: int, payload: bytes, flags: int = 0) -> bytes:
    return bytes([mtype, flags & 0xFF]) + len(payload).to_bytes(4, "big") + payload


def frame_type(buf: bytes) -> int:
    if len(buf) < _HEADER:
        raise ValueError("усечённый заголовок кадра")
    length = int.from_bytes(buf[2:6], "big")
    if len(buf) < _HEADER + length:
        raise ValueError("неполный payload кадра")
    return buf[0]


def frame_payload(buf: bytes) -> bytes:
    length = int.from_bytes(buf[2:6], "big")
    return buf[_HEADER:_HEADER + length]


def _get_var(mv: memoryview, pos: int):
    n = int.from_bytes(mv[pos:pos + 2], "big"); pos += 2
    return bytes(mv[pos:pos + n]), pos + n


def _put_var(buf: bytearray, chunk: bytes):
    buf += len(chunk).to_bytes(2, "big") + chunk


def parse_hello(payload: bytes):
    mv = memoryview(payload)
    room_id, pos = _get_var(mv, 0)
    count = mv[pos]; pos += 1
    candidates = []
    for _ in range(count):
        host, pos = _get_var(mv, pos)
        port = int.from_bytes(mv[pos:pos + 2], "big"); pos += 2
        candidates.append((host.decode("utf-8"), port))
    return room_id, candidates


def encode_pair(role: int, peer_candidates) -> bytes:
    buf = bytearray([role, len(peer_candidates)])
    for host, port in peer_candidates:
        _put_var(buf, host.encode("utf-8"))
        buf += port.to_bytes(2, "big")
    return encode_frame(PAIR, bytes(buf))


@dataclass
class _Member:
    ws: object
    role: int
    candidates: list
    send_lock: threading.Lock = field(default_factory=threading.Lock)

    def send(self, data: bytes):
        with self.send_lock:
            self.ws.send(data)


_rooms: dict[bytes, list] = {}
_rooms_lock = threading.Lock()


def _peer_of(room_id: bytes, member: _Member):
    for other in _rooms.get(room_id, ()):
        if other is not member:
            return other
    return None


p2p_bp = Blueprint("p2p", __name__)
p2p_sock = Sock()   # инициализировать на app: p2p_sock.init_app(app)


@p2p_sock.route("/p2p", bp=p2p_bp)
def p2p(ws):
    room_id = None
    member = None
    try:
        hello = ws.receive()
        if not isinstance(hello, (bytes, bytearray)) or frame_type(hello) != HELLO:
            return
        room_id, candidates = parse_hello(frame_payload(hello))
        with _rooms_lock:
            room = _rooms.setdefault(room_id, [])
            if len(room) >= 2:
                return
            role = ROLE_INITIATOR if not room else ROLE_RESPONDER
            member = _Member(ws, role, candidates)
            room.append(member)
            pair_now = len(room) == 2
            first, second = (room[0], room[1]) if pair_now else (None, None)
        if pair_now:
            first.send(encode_pair(first.role, second.candidates))
            second.send(encode_pair(second.role, first.candidates))
        while True:
            msg = ws.receive()
            if msg is None:
                break
            if not isinstance(msg, (bytes, bytearray)):
                continue
            if frame_type(msg) == RELAY:
                with _rooms_lock:
                    peer = _peer_of(room_id, member)
                if peer is not None:
                    try:
                        peer.send(bytes(msg))
                    except Exception:
                        pass
    except Exception:
        pass
    finally:
        if room_id is not None and member is not None:
            with _rooms_lock:
                room = _rooms.get(room_id)
                remaining = []
                if room is not None and member in room:
                    room.remove(member)
                    remaining = list(room)
                    if not room:
                        _rooms.pop(room_id, None)
            # Оставшийся участник узнаёт об уходе пира явно — его собственное
            # WS-соединение с сервером живо и без этого молчало бы вечно (relay
            # просто перестаёт получать кадры), а «онлайн»-статус на клиенте
            # навсегда завис бы в True.
            for other in remaining:
                try:
                    other.send(encode_frame(PEER_LEFT, b""))
                except Exception:
                    pass


# ── /mesh: прокси к публичному mesh-узлу (mesh_daemon.py) ─────────────────────
# Одно бинарное WS-сообщение = один кадр линка; к демону — тот же кадр с префиксом
# u32be длины (TcpWire). Содержимое не разбираем: шифрование линка — от клиента
# до демона.
import os as _os
import socket as _socket

MESH_PORT = int(_os.environ.get("MESH_PORT", "7811"))
MESH_MAX_FRAME = 72 * 1024


def _recv_exact(sock, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("mesh-узел закрыл соединение")
        buf += chunk
    return bytes(buf)


@p2p_sock.route("/mesh", bp=p2p_bp)
def mesh(ws):
    try:
        sock = _socket.create_connection(("127.0.0.1", MESH_PORT), timeout=5)
    except OSError:
        return  # демон не запущен — клиент повторит позже
    sock.settimeout(None)

    def down():  # демон → клиент
        try:
            while True:
                n = int.from_bytes(_recv_exact(sock, 4), "big")
                if n > MESH_MAX_FRAME:
                    break
                ws.send(_recv_exact(sock, n))
        except Exception:
            pass
        finally:
            try:
                ws.close()
            except Exception:
                pass

    threading.Thread(target=down, daemon=True).start()
    try:
        while True:  # клиент → демон
            data = ws.receive()
            if not isinstance(data, (bytes, bytearray)) or len(data) > MESH_MAX_FRAME:
                break
            sock.sendall(len(data).to_bytes(4, "big") + bytes(data))
    except Exception:
        pass
    finally:
        try:
            sock.close()
        except OSError:
            pass
