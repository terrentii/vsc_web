"""Узел mesh: соседи, маршрутизация по запросу (RREQ/RREP/DATA/RERR), точка встречи.

Всё состояние — в памяти и живёт минуты: чужие узлы ничего не хранят.
Отвечает на поиск только сам собеседник; ретрансляторы не кэшируют метки.
"""

import asyncio
import os
import time
from collections import OrderedDict
from dataclasses import dataclass, field, replace
from enum import IntEnum
from typing import Callable

from . import packet as P
from .link import Link
from .tags import hour_of, tag_for_hour
from .wire import Wire, WireClosed


class MeshRole(IntEnum):
    INITIATOR = 0  # чей запрос получил ответ: шлёт «прайм», Alice в ratchet
    RESPONDER = 1


class MeshClosed(Exception):
    """Маршрут закрыт или оборвался."""


@dataclass
class MeshConfig:
    relay: bool = True
    ttl_max: int = P.TTL_MAX
    dedup_ttl: float = 120.0
    dedup_max: int = 4096
    route_idle: float = 120.0
    route_max: int = 1024
    keepalive: float = 30.0
    rreq_retry: float = 5.0
    rreq_rate: float = 10.0
    relay_rate: float = 256 * 1024
    sweep: float = 5.0
    clock: Callable[[], float] = time.time
    # Публичный узел (сервер): свой адрес wss://…/mesh — сообщается соседям в PEERS
    public_url: str | None = None


class _Bucket:
    """Токен-бакет: ``rate`` в секунду, запас — на одну секунду."""

    def __init__(self, rate: float):
        self._rate, self._tokens, self._t = rate, rate, time.monotonic()

    def take(self, n: float = 1.0) -> bool:
        now = time.monotonic()
        self._tokens = min(self._rate, self._tokens + (now - self._t) * self._rate)
        self._t = now
        if self._tokens >= n:
            self._tokens -= n
            return True
        return False


@dataclass(eq=False)
class _Search:
    tags: set
    nonce: bytes
    future: asyncio.Future


@dataclass(eq=False)
class _Route:
    a: int | None                      # сосед; None — этот узел конечная точка
    b: int
    endpoint: "MeshTransport | None" = None
    last: float = field(default_factory=time.monotonic)


class MeshNode:
    def __init__(self, config: MeshConfig | None = None):
        self.config = config or MeshConfig()
        self._links: dict[int, Link] = {}
        self._rreq_buckets: dict[int, _Bucket] = {}
        self._next = 1
        self._seen: OrderedDict[bytes, tuple[int | None, float]] = OrderedDict()
        self._routes: dict[bytes, _Route] = {}
        self._by_tag: dict[bytes, _Search] = {}
        self._by_req: dict[bytes, _Search] = {}
        self._relay_bucket = _Bucket(self.config.relay_rate)
        self._relayed = 0
        self._rreq_forwarded = 0
        self._tasks: set[asyncio.Task] = set()
        self._sweeper: asyncio.Task | None = None
        self._closed: dict[int, asyncio.Future] = {}
        # адреса публичных узлов: свой (если есть) + выученные из PEERS
        self.known_urls: list[str] = [self.config.public_url] if self.config.public_url else []
        self.on_peers: Callable[[list[str]], None] | None = None  # выучили новые адреса

    # -- соседи ------------------------------------------------------------

    async def attach(self, wire: Wire) -> int:
        link = await Link.open(wire)
        nid = self._next
        self._next += 1
        self._links[nid] = link
        self._rreq_buckets[nid] = _Bucket(self.config.rreq_rate)
        self._closed[nid] = asyncio.get_running_loop().create_future()
        self._spawn(self._read_loop(nid, link))
        self._ensure_sweeper()
        if self.known_urls:  # поделиться адресами публичных узлов
            await self._send(nid, P.encode(P.Packet(P.PEERS, os.urandom(P.ID_LEN),
                                                    payload=P.encode_peers(self.known_urls))))
        return nid

    async def serve(self, wire: Wire) -> None:
        """Подключить провод и держать, пока линк жив (для серверов и менеджеров связи)."""
        nid = await self.attach(wire)
        await asyncio.shield(self._closed[nid])

    def learn_urls(self, urls: list[str]) -> list[str]:
        """Добавить адреса публичных узлов; → новые (не больше 32 всего)."""
        fresh = [u for u in urls if u not in self.known_urls]
        self.known_urls = (self.known_urls + fresh)[:32]
        return [u for u in fresh if u in self.known_urls]

    async def detach(self, nid: int) -> None:
        link = self._links.get(nid)
        if link is not None:
            await link.close()  # цикл чтения увидит обрыв и уберёт соседа

    def stats(self) -> dict:
        return {"neighbors": len(self._links), "routes": len(self._routes),
                "relayed_bytes": self._relayed, "rreq_forwarded": self._rreq_forwarded}

    async def close(self) -> None:
        for link in list(self._links.values()):
            await link.close()
        for t in list(self._tasks):
            t.cancel()

    # -- поиск собеседника ---------------------------------------------------

    async def find(self, key: bytes, timeout: float) -> tuple[MeshRole, "MeshTransport"]:
        hour = hour_of(self.config.clock())
        cur = tag_for_hour(key, hour)
        s = _Search({cur, tag_for_hour(key, hour - 1)}, os.urandom(P.NONCE_LEN),
                    asyncio.get_running_loop().create_future())
        for t in s.tags:
            self._by_tag[t] = s
        self._ensure_sweeper()
        asker = asyncio.create_task(self._ask(s, cur))
        try:
            return await asyncio.wait_for(asyncio.shield(s.future), timeout)
        finally:
            asker.cancel()
            if not s.future.done():
                s.future.cancel()  # поздний RREP получит RERR
            for t in s.tags:
                if self._by_tag.get(t) is s:
                    del self._by_tag[t]

    async def _ask(self, s: _Search, tag: bytes) -> None:
        while not s.future.done():
            rid = os.urandom(P.ID_LEN)
            self._remember(rid, None)
            self._by_req[rid] = s
            await self._broadcast(P.encode(P.Packet(P.RREQ, rid, ttl=self.config.ttl_max,
                                                    tag=tag, nonce=s.nonce)), exclude=None)
            await asyncio.sleep(self.config.rreq_retry)

    # -- приём -----------------------------------------------------------------

    async def _read_loop(self, nid: int, link: Link) -> None:
        try:
            while True:
                raw = await link.recv()
                await self._handle(nid, P.decode(raw, self.config.ttl_max), raw)
        except (WireClosed, P.MeshProtocolError):
            pass
        finally:
            await self._drop_neighbor(nid)

    async def _handle(self, nid: int, p: P.Packet, raw: bytes) -> None:
        if p.type == P.RREQ:
            await self._on_rreq(nid, p)
        elif p.type == P.RREP:
            await self._on_rrep(nid, p)
        elif p.type == P.DATA:
            await self._on_data(nid, p, raw)
        elif p.type == P.PEERS:  # только между соседями, дальше не идёт
            fresh = self.learn_urls(P.decode_peers(p.payload))
            if fresh and self.on_peers is not None:
                self.on_peers(fresh)
        else:
            await self._on_rerr(nid, p, raw)

    async def _on_rreq(self, nid: int, p: P.Packet) -> None:
        if p.id in self._seen or not self._rreq_buckets[nid].take():
            return
        self._remember(p.id, nid)
        s = self._by_tag.get(p.tag)
        if s is not None:  # ищут нас: отвечает тот, у кого nonce меньше
            if not s.future.done() and s.nonce < p.nonce and len(self._routes) < self.config.route_max:
                rid = os.urandom(P.ID_LEN)
                t = MeshTransport(self, rid, nid)
                self._routes[rid] = _Route(None, nid, t)
                await self._send(nid, P.encode(P.Packet(P.RREP, p.id, route_id=rid, nonce=s.nonce)))
                s.future.set_result((MeshRole.RESPONDER, t))
                t._start()
            return
        if self.config.relay and p.ttl > 1:
            self._rreq_forwarded += 1
            await self._broadcast(P.encode(replace(p, ttl=p.ttl - 1, hops=min(p.hops + 1, 255))),
                                  exclude=nid)

    async def _on_rrep(self, mid: int, p: P.Packet) -> None:
        s = self._by_req.get(p.id)
        if s is not None:
            if p.route_id in self._routes:
                return  # повтор уже принятого ответа
            if s.future.done():
                await self._send(mid, P.encode(P.Packet(P.RERR, p.route_id)))
                return
            t = MeshTransport(self, p.route_id, mid)
            self._routes[p.route_id] = _Route(None, mid, t)
            s.future.set_result((MeshRole.INITIATOR, t))
            t._start()
            return
        entry = self._seen.get(p.id)
        if not self.config.relay or entry is None:
            return
        nid = entry[0]
        if (nid is None or nid == mid or nid not in self._links or p.route_id in self._routes
                or len(self._routes) >= self.config.route_max):
            return
        self._routes[p.route_id] = _Route(nid, mid)
        await self._send(nid, P.encode(replace(p, hops=min(p.hops + 1, 255))))

    async def _on_data(self, nid: int, p: P.Packet, raw: bytes) -> None:
        r = self._routes.get(p.id)
        if r is None:
            return
        if r.endpoint is not None:
            if nid == r.b:
                r.last = time.monotonic()
                if p.payload:
                    r.endpoint._deliver(p.payload)
            return
        other = r.b if nid == r.a else r.a if nid == r.b else None
        if other is None or not self._relay_bucket.take(len(p.payload)):
            return
        r.last = time.monotonic()
        self._relayed += len(p.payload)
        await self._send(other, raw)

    async def _on_rerr(self, nid: int, p: P.Packet, raw: bytes) -> None:
        r = self._routes.get(p.id)
        if r is None or nid not in (r.a, r.b):
            return
        del self._routes[p.id]
        if r.endpoint is not None:
            r.endpoint._fail()
        else:
            await self._send(r.b if nid == r.a else r.a, raw)

    # -- служебное ---------------------------------------------------------------

    async def _drop_neighbor(self, nid: int) -> None:
        link = self._links.pop(nid, None)
        self._rreq_buckets.pop(nid, None)
        closed = self._closed.pop(nid, None)
        if closed is not None and not closed.done():
            closed.set_result(None)
        if link is not None:
            await link.close()
        for rid, r in list(self._routes.items()):
            if nid not in (r.a, r.b):
                continue
            del self._routes[rid]
            if r.endpoint is not None:
                r.endpoint._fail()
            else:
                await self._send(r.b if nid == r.a else r.a, P.encode(P.Packet(P.RERR, rid)))

    async def _send(self, nid: int, raw: bytes) -> bool:
        link = self._links.get(nid)
        if link is None:
            return False
        try:
            await link.send(raw)
            return True
        except WireClosed:
            await link.close()
            return False

    async def _broadcast(self, raw: bytes, exclude: int | None) -> None:
        for nid in list(self._links):
            if nid != exclude:
                await self._send(nid, raw)

    def _remember(self, rid: bytes, nid: int | None) -> None:
        self._seen[rid] = (nid, time.monotonic())
        while len(self._seen) > self.config.dedup_max:
            self._seen.popitem(last=False)

    def _touch(self, rid: bytes) -> None:
        r = self._routes.get(rid)
        if r is not None:
            r.last = time.monotonic()

    def _spawn(self, coro) -> None:
        t = asyncio.create_task(coro)
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    def _ensure_sweeper(self) -> None:
        if self._sweeper is None or self._sweeper.done():
            self._sweeper = asyncio.create_task(self._sweep_loop())
            self._tasks.add(self._sweeper)

    async def _sweep_loop(self) -> None:
        while True:
            await asyncio.sleep(self.config.sweep)
            now = time.monotonic()
            while self._seen:
                _, (_, t) = next(iter(self._seen.items()))
                if now - t < self.config.dedup_ttl:
                    break
                self._seen.popitem(last=False)
            for rid in [r for r in self._by_req if r not in self._seen]:
                del self._by_req[rid]
            for rid, r in list(self._routes.items()):
                if now - r.last > self.config.route_idle:
                    del self._routes[rid]
                    if r.endpoint is not None:
                        r.endpoint._fail()


class MeshTransport:
    """Конечная точка маршрута: кадры сессии в DATA, keepalive, RERR при закрытии."""

    def __init__(self, node: MeshNode, route_id: bytes, neighbor: int):
        self._node, self.route_id, self._nb = node, route_id, neighbor
        self._q: asyncio.Queue = asyncio.Queue()
        self._closed = False
        self._ka: asyncio.Task | None = None

    def _start(self) -> None:
        self._ka = asyncio.create_task(self._keepalive())

    async def send(self, frame: bytes) -> None:
        if self._closed:
            raise MeshClosed("маршрут закрыт")
        if len(frame) > P.MAX_DATA:
            raise ValueError("кадр больше MAX_DATA")
        self._node._touch(self.route_id)
        if not await self._node._send(self._nb, P.encode(P.Packet(P.DATA, self.route_id, payload=frame))):
            raise MeshClosed("сосед отключился")

    async def recv(self) -> bytes:
        item = await self._q.get()
        if item is None:
            self._q.put_nowait(None)
            raise MeshClosed("маршрут оборвался")
        return item

    async def close(self) -> None:
        if self._closed:
            return
        self._fail()
        if self._node._routes.pop(self.route_id, None) is not None:
            await self._node._send(self._nb, P.encode(P.Packet(P.RERR, self.route_id)))

    def _deliver(self, payload: bytes) -> None:
        if not self._closed:
            self._q.put_nowait(payload)

    def _fail(self) -> None:
        if self._ka is not None and self._ka is not asyncio.current_task():
            self._ka.cancel()
        if not self._closed:
            self._closed = True
            self._q.put_nowait(None)

    async def _keepalive(self) -> None:
        try:
            while True:
                await asyncio.sleep(self._node.config.keepalive)
                await self.send(b"")
        except MeshClosed:
            pass
