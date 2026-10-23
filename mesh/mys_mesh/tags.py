"""Метки каналов: ключ из prs, метка меняется каждый час (ретрансляторы не следят за каналом)."""

import hashlib

from mys_crypto.primitives import hkdf


def tag_key(prs: bytes) -> bytes:
    """Ключ меток из prs — не из room_id: room_id видит сервер."""
    return hkdf(prs, 32, salt=b"", info=b"mys-mesh-tag-key")


def tag_for_hour(key: bytes, hour: int) -> bytes:
    return hashlib.blake2b(hour.to_bytes(8, "big"), digest_size=32, key=key,
                           person=b"mys-mesh-tag").digest()


def hour_of(unix_seconds: float) -> int:
    return int(unix_seconds // 3600)
