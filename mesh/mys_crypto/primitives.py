"""HKDF-SHA256 для меток mesh — та же функция, что в vsc_desktop mys_crypto.primitives.

Полный primitives тянет argon2 и пр., демону не нужные."""

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF


def hkdf(ikm: bytes, length: int, salt: bytes, info: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=length, salt=salt, info=info).derive(ikm)
