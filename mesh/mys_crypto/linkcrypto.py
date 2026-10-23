"""Шифрование линка mesh: crypto_kx (X25519) + secretstream XChaCha20-Poly1305.

Только вызовы libsodium (тот же экземпляр, что у ristretto255): своей
криптографии и I/O здесь нет. Используется :mod:`mys_mesh.link`.
"""

import ctypes

from ._ristretto import _lib

KX_PUBLICKEYBYTES = 32
KX_SESSIONKEYBYTES = 32
HEADERBYTES = 24
ABYTES = 17
TAG_MESSAGE = 0


class LinkCryptoError(Exception):
    """Сбой обмена ключами или расшифровки кадра линка."""


_P = ctypes.c_char_p
_ULL = ctypes.c_ulonglong


def _fn(name, argtypes, restype=ctypes.c_int):
    f = getattr(_lib, name)
    f.argtypes = argtypes
    f.restype = restype
    return f


_kx_keypair = _fn("crypto_kx_keypair", [_P, _P])
_kx_client = _fn("crypto_kx_client_session_keys", [_P] * 5)
_kx_server = _fn("crypto_kx_server_session_keys", [_P] * 5)
_statebytes = _fn("crypto_secretstream_xchacha20poly1305_statebytes", [], ctypes.c_size_t)
_init_push = _fn("crypto_secretstream_xchacha20poly1305_init_push", [_P, _P, _P])
_push = _fn("crypto_secretstream_xchacha20poly1305_push",
            [_P, _P, ctypes.c_void_p, _P, _ULL, _P, _ULL, ctypes.c_ubyte])
_init_pull = _fn("crypto_secretstream_xchacha20poly1305_init_pull", [_P, _P, _P])
_pull = _fn("crypto_secretstream_xchacha20poly1305_pull",
            [_P, _P, ctypes.c_void_p, _P, _P, _ULL, _P, _ULL])
_STATEBYTES = _statebytes()


def _buf(n: int):
    return ctypes.create_string_buffer(max(n, 1))


def kx_keypair() -> tuple[bytes, bytes]:
    """Временная пара X25519 для одного линка: (pk, sk)."""
    pk, sk = _buf(KX_PUBLICKEYBYTES), _buf(KX_PUBLICKEYBYTES)
    if _kx_keypair(pk, sk) != 0:  # pragma: no cover
        raise LinkCryptoError("crypto_kx_keypair")
    return pk.raw, sk.raw


def kx_session_keys(pk: bytes, sk: bytes, peer_pk: bytes) -> tuple[bytes, bytes]:
    """(rx, tx). «Клиент» crypto_kx — сторона с меньшим pk (побайтово)."""
    if len(peer_pk) != KX_PUBLICKEYBYTES or peer_pk == pk:
        raise LinkCryptoError("неверный ключ соседа")
    rx, tx = _buf(KX_SESSIONKEYBYTES), _buf(KX_SESSIONKEYBYTES)
    fn = _kx_client if pk < peer_pk else _kx_server
    if fn(rx, tx, pk, sk, peer_pk) != 0:
        raise LinkCryptoError("crypto_kx отверг ключ соседа")
    return rx.raw, tx.raw


class Pusher:
    """Шифрующая сторона secretstream; ``header`` отправляется соседу первым."""

    def __init__(self, key: bytes):
        self._state = _buf(_STATEBYTES)
        header = _buf(HEADERBYTES)
        if _init_push(self._state, header, key) != 0:  # pragma: no cover
            raise LinkCryptoError("init_push")
        self.header = header.raw

    def push(self, message: bytes) -> bytes:
        c = _buf(len(message) + ABYTES)
        if _push(self._state, c, None, message, len(message), None, 0, TAG_MESSAGE) != 0:
            raise LinkCryptoError("push")  # pragma: no cover
        return c.raw[: len(message) + ABYTES]


class Puller:
    """Расшифровывающая сторона: подмена, повтор, перестановка — ``LinkCryptoError``."""

    def __init__(self, key: bytes, header: bytes):
        self._state = _buf(_STATEBYTES)
        if len(header) != HEADERBYTES or _init_pull(self._state, header, key) != 0:
            raise LinkCryptoError("неверный заголовок потока")

    def pull(self, ciphertext: bytes) -> bytes:
        n = len(ciphertext) - ABYTES
        if n < 0:
            raise LinkCryptoError("кадр короче ABYTES")
        m, tag = _buf(n), _buf(1)
        if _pull(self._state, m, None, tag, ciphertext, len(ciphertext), None, 0) != 0:
            raise LinkCryptoError("кадр не расшифрован")
        if tag.raw[0] != TAG_MESSAGE:
            raise LinkCryptoError("неожиданный тег потока")
        return m.raw[:n]
