# rendezvous_ws.py — WebSocket rendezvous + relay для децентрализованного режима.
# Никакой персистентности: всё in-memory и эфемерно. Сервер НЕ парсит payload RELAY.
import threading
from dataclasses import dataclass, field
from flask import Blueprint
from flask_sock import Sock

HELLO, PAIR, RELAY = 1, 2, 5
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
                if room is not None and member in room:
                    room.remove(member)
                    if not room:
                        _rooms.pop(room_id, None)
