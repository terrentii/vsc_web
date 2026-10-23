"""Линк: временные X25519-ключи → crypto_kx → secretstream в обе стороны.

Постоянной личности у узла нет; содержимое защищено сквозным шифрованием
канала, линк скрывает метки и маршруты от наблюдателя эфира/сети.
"""

import asyncio

from mys_crypto.linkcrypto import LinkCryptoError, Puller, Pusher, kx_keypair, kx_session_keys

from .packet import MeshProtocolError
from .wire import Wire, WireClosed

MAGIC = b"MYSM"
VERSION = 1
HANDSHAKE_TIMEOUT = 10.0


class Link:
    def __init__(self, wire: Wire, push: Pusher, pull: Puller):
        self._wire, self._push, self._pull = wire, push, pull

    @classmethod
    async def open(cls, wire: Wire, timeout: float = HANDSHAKE_TIMEOUT) -> "Link":
        async def run() -> "Link":
            pk, sk = kx_keypair()
            await wire.send(MAGIC + bytes([VERSION]) + pk)
            hello = await wire.recv()
            if len(hello) != 37 or hello[:4] != MAGIC or hello[4] != VERSION:
                raise MeshProtocolError("неверное приветствие линка")
            rx, tx = kx_session_keys(pk, sk, hello[5:])
            push = Pusher(tx)
            await wire.send(push.header)
            return cls(wire, push, Puller(rx, await wire.recv()))

        try:
            return await asyncio.wait_for(run(), timeout)
        except LinkCryptoError as e:
            await wire.close()
            raise MeshProtocolError(str(e)) from e
        except BaseException:
            await wire.close()
            raise

    async def send(self, packet: bytes) -> None:
        await self._wire.send(self._push.push(packet))

    async def recv(self) -> bytes:
        c = await self._wire.recv()
        try:
            return self._pull.pull(c)
        except LinkCryptoError as e:
            raise MeshProtocolError(f"линк: {e}") from e

    async def close(self) -> None:
        await self._wire.close()


__all__ = ["Link", "WireClosed"]
