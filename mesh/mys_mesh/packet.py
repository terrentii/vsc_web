"""Mesh-пакет: ``version | type | ttl | hops | id(16) | body`` (big-endian)."""

from dataclasses import dataclass

VERSION = 1
RREQ, RREP, DATA, RERR, PEERS = 1, 2, 3, 4, 5
ID_LEN, TAG_LEN, NONCE_LEN = 16, 32, 16
HEADER_LEN = 4 + ID_LEN
MAX_DATA = 65_600
MAX_PACKET = HEADER_LEN + MAX_DATA
TTL_MAX = 8
PEERS_MAX = 16      # адресов в одном PEERS
PEER_URL_MAX = 200  # байт на адрес


class MeshProtocolError(Exception):
    """Неверный пакет/линк — соединение с этим соседом рвётся."""


@dataclass(frozen=True)
class Packet:
    type: int
    id: bytes
    ttl: int = 0
    hops: int = 0
    tag: bytes = b""
    nonce: bytes = b""
    route_id: bytes = b""
    payload: bytes = b""


def encode(p: Packet) -> bytes:
    if p.type == RREQ:
        body = p.tag + p.nonce
    elif p.type == RREP:
        body = p.route_id + p.nonce
    elif p.type in (DATA, PEERS):
        body = p.payload
    else:
        body = b""
    return bytes([VERSION, p.type, p.ttl, p.hops]) + p.id + body


def decode(raw: bytes, ttl_max: int = TTL_MAX) -> Packet:
    if not HEADER_LEN <= len(raw) <= MAX_PACKET:
        raise MeshProtocolError("неверная длина пакета")
    version, ptype, ttl, hops = raw[0], raw[1], raw[2], raw[3]
    pid, body = bytes(raw[4:HEADER_LEN]), bytes(raw[HEADER_LEN:])
    if version != VERSION:
        raise MeshProtocolError("неизвестная версия")
    if ptype == RREQ:
        if len(body) != TAG_LEN + NONCE_LEN or not 1 <= ttl <= ttl_max:
            raise MeshProtocolError("неверный RREQ")
        return Packet(RREQ, pid, ttl, hops, tag=body[:TAG_LEN], nonce=body[TAG_LEN:])
    if ttl != 0:
        raise MeshProtocolError("ttl только у RREQ")
    if ptype == RREP:
        if len(body) != ID_LEN + NONCE_LEN:
            raise MeshProtocolError("неверный RREP")
        return Packet(RREP, pid, 0, hops, route_id=body[:ID_LEN], nonce=body[ID_LEN:])
    if ptype == DATA:
        return Packet(DATA, pid, 0, hops, payload=body)
    if ptype == RERR:
        if body:
            raise MeshProtocolError("RERR с телом")
        return Packet(RERR, pid, 0, hops)
    if ptype == PEERS:
        decode_peers(body)  # проверка формата
        return Packet(PEERS, pid, 0, hops, payload=body)
    raise MeshProtocolError("неизвестный тип")


def encode_peers(urls: list[str]) -> bytes:
    """Адреса публичных узлов (wss://…) для пакета PEERS: строки через \\n."""
    keep = [u for u in urls if _valid_url(u.encode("utf-8"))][:PEERS_MAX]
    return "\n".join(keep).encode("utf-8")


def decode_peers(body: bytes) -> list[str]:
    if not body:
        return []
    parts = body.split(b"\n")
    if len(parts) > PEERS_MAX or not all(_valid_url(p) for p in parts):
        raise MeshProtocolError("неверный PEERS")
    return [p.decode("utf-8") for p in parts]


def _valid_url(raw: bytes) -> bool:
    if not 0 < len(raw) <= PEER_URL_MAX or not raw.startswith((b"wss://", b"ws://")):
        return False
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return text.isprintable() and " " not in text
