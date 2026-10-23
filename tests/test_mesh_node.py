"""Публичный mesh-узел: демон + веб-прокси /mesh; два клиента находят друг друга.

Нужен libsodium (ctypes, как у десктопа): MYS_LIBSODIUM или системный — иначе пропуск.
"""
import asyncio
import filecmp
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "mesh"))
try:
    from mys_mesh.node import MeshConfig, MeshNode  # noqa: E402
    from mys_mesh.tags import tag_key  # noqa: E402
    from mys_mesh.wire import WsWire  # noqa: E402
except Exception as exc:  # pragma: no cover — нет libsodium
    pytest.skip(f"mesh недоступен: {exc}", allow_module_level=True)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def mesh_server():
    web_port, mesh_port = _free_port(), _free_port()
    tmp = tempfile.mkdtemp(prefix="vsc_mesh_")
    env = {**os.environ, "PORT": str(web_port), "MESH_PORT": str(mesh_port),
           "MESH_PUBLIC_URL": f"ws://127.0.0.1:{web_port}/mesh",
           "DATABASE_URL": f"sqlite:///{tmp}/web.db", "SECRET_KEY": "mesh", "SESSION_TYPE": "null",
           "ROOMS_DIR": f"{tmp}/rooms", "MEDIA_CLEANUP_THRESHOLD": "1.0"}
    daemon = subprocess.Popen([sys.executable, "mesh_daemon.py"], cwd=_ROOT, env=env,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    web = subprocess.Popen([sys.executable, "app.py"], cwd=_ROOT, env=env,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(100):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{web_port}/", timeout=1)
            break
        except Exception:
            time.sleep(0.2)
    yield f"ws://127.0.0.1:{web_port}/mesh"
    web.terminate()
    daemon.terminate()


async def _client(url):
    node = MeshNode(MeshConfig(rreq_retry=0.3, sweep=0.3))
    learned = []
    node.on_peers = learned.extend
    await node.attach(await WsWire.connect(url))
    return node, learned


def test_two_clients_meet_through_public_node(mesh_server):
    async def run():
        (a, learned), (b, _) = await asyncio.gather(_client(mesh_server), _client(mesh_server))
        try:
            key = tag_key(b"\x05" * 32)
            (_, ta), (_, tb) = await asyncio.gather(a.find(key, 15), b.find(key, 15))
            await ta.send(b"via soufos mesh")
            assert await asyncio.wait_for(tb.recv(), 10) == b"via soufos mesh"
            assert learned == [mesh_server]  # узел сообщил свой публичный адрес
        finally:
            await a.close()
            await b.close()
    asyncio.run(run())


def test_vendored_core_matches_desktop():
    desktop = os.path.join(_ROOT, "..", "vsc_desktop", "src")
    if not os.path.isdir(desktop):
        pytest.skip("vsc_desktop рядом нет")
    pairs = [(f"mys_mesh/{f}.py") for f in ("__init__", "packet", "tags", "wire", "link", "node")]
    pairs += ["mys_crypto/_ristretto.py", "mys_crypto/linkcrypto.py"]
    stale = [p for p in pairs if not filecmp.cmp(os.path.join(_ROOT, "mesh", p), os.path.join(desktop, p), shallow=False)]
    assert not stale, f"обновить tools/vendor_mesh.sh: {stale}"
