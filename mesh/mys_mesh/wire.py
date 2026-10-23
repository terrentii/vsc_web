"""Провода: упорядоченный надёжный поток кадров к соседу (память, TCP).

Шифрования здесь нет — его делает :mod:`.link`. BLE/Wi-Fi Direct — этап 3.
"""

import asyncio
from abc import ABC, abstractmethod

MAX_FRAME = 72 * 1024


class WireClosed(Exception):
    """Провод закрыт или оборвался."""


class Wire(ABC):
    @abstractmethod
    async def send(self, frame: bytes) -> None: ...

    @abstractmethod
    async def recv(self) -> bytes: ...

    @abstractmethod
    async def close(self) -> None: ...


_CLOSED = object()


class MemoryWire(Wire):
    """Пара очередей; ``close`` любой стороны обрывает ``recv`` обеих."""

    def __init__(self, inbox: asyncio.Queue, outbox: asyncio.Queue):
        self._inbox, self._outbox, self._closed = inbox, outbox, False

    @classmethod
    def pair(cls) -> tuple["MemoryWire", "MemoryWire"]:
        ab, ba = asyncio.Queue(), asyncio.Queue()
        return cls(ba, ab), cls(ab, ba)

    async def send(self, frame: bytes) -> None:
        if self._closed:
            raise WireClosed("провод закрыт")
        self._outbox.put_nowait(bytes(frame))

    async def recv(self) -> bytes:
        item = await self._inbox.get()
        if item is _CLOSED:
            self._inbox.put_nowait(_CLOSED)
            raise WireClosed("провод закрыт")
        if len(item) > MAX_FRAME:
            raise WireClosed("кадр больше MAX_FRAME")
        return item

    async def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._outbox.put_nowait(_CLOSED)
            self._inbox.put_nowait(_CLOSED)


class WsWire(Wire):
    """Провод через WebSocket (wss://…/mesh): одно бинарное сообщение = один кадр.

    Интернет-линк к публичному узлу: проходит прокси и NAT как обычный HTTPS."""

    def __init__(self, ws):
        self._ws = ws

    @classmethod
    async def connect(cls, url: str, *, open_timeout: float = 60.0) -> "WsWire":
        from websockets.asyncio.client import connect
        try:
            ws = await connect(url, max_size=MAX_FRAME, open_timeout=open_timeout, ping_interval=20)
        except Exception as e:  # DNS, отказ, TLS, таймаут — для менеджера связи одно и то же
            raise WireClosed(f"{url}: {e}") from e
        return cls(ws)

    async def send(self, frame: bytes) -> None:
        try:
            await self._ws.send(bytes(frame))
        except Exception as e:
            raise WireClosed(str(e)) from e

    async def recv(self) -> bytes:
        try:
            msg = await self._ws.recv()
        except Exception as e:
            raise WireClosed(str(e)) from e
        if isinstance(msg, str) or len(msg) > MAX_FRAME:
            raise WireClosed("не бинарный кадр или больше MAX_FRAME")
        return msg

    async def close(self) -> None:
        try:
            await self._ws.close()
        except Exception:
            pass


class TcpWire(Wire):
    """Кадры ``u32be длина ‖ байты`` поверх TCP."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self._r, self._w = reader, writer

    @classmethod
    async def connect(cls, host: str, port: int) -> "TcpWire":
        r, w = await asyncio.open_connection(host, port)
        return cls(r, w)

    async def send(self, frame: bytes) -> None:
        try:
            self._w.write(len(frame).to_bytes(4, "big") + frame)
            await self._w.drain()
        except (ConnectionError, OSError) as e:
            raise WireClosed(str(e)) from e

    async def recv(self) -> bytes:
        try:
            n = int.from_bytes(await self._r.readexactly(4), "big")
            if n > MAX_FRAME:
                raise WireClosed("кадр больше MAX_FRAME")
            return await self._r.readexactly(n)
        except (asyncio.IncompleteReadError, ConnectionError, OSError) as e:
            raise WireClosed(str(e)) from e

    async def close(self) -> None:
        self._w.close()
        try:  # мёртвый сосед не должен подвешивать остановку
            await asyncio.wait_for(self._w.wait_closed(), 2.0)
        except (ConnectionError, OSError, asyncio.TimeoutError):
            pass
