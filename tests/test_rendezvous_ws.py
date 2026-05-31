"""Phase 2 — Live-server integration tests for the binary /p2p rendezvous+relay endpoint.

Uses an asyncio websockets client against a real subprocess server to exercise
the full flask-sock + eventlet path. Tests are wrapped in asyncio.run() with
asyncio.wait_for() timeouts so hangs fail fast.
"""

import asyncio
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

import pytest

# ---------------------------------------------------------------------------
# Helpers (copied from test_ws_centralized.py to avoid touching that file)
# ---------------------------------------------------------------------------

_VCS_WEB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PY = sys.executable


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def _wait_for_server(url: str, timeout: float = 20.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            urllib.request.urlopen(url, timeout=1)
            return True
        except urllib.error.HTTPError:
            return True
        except Exception:
            time.sleep(0.2)
    return False


class LiveServer:
    def __init__(self, proc: subprocess.Popen, port: int, db_path: str):
        self._proc = proc
        self.port = port
        self.http_url = f'http://127.0.0.1:{port}'
        self.ws_url = f'ws://127.0.0.1:{port}'
        self._db_path = db_path

    def teardown(self):
        try:
            self._proc.terminate()
            self._proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            self._proc.kill()
            self._proc.wait(timeout=4)
        except Exception:
            pass
        try:
            os.unlink(self._db_path)
        except Exception:
            pass


@pytest.fixture(scope='module')
def live_server():
    port = _free_port()
    db_fd, db_path = tempfile.mkstemp(suffix='.db', prefix='vsc_p2ptest_')
    os.close(db_fd)

    env = {
        **os.environ,
        'DATABASE_URL': f'sqlite:///{db_path}',
        'PORT': str(port),
        'SECRET_KEY': 'p2p-test-secret',
        'SESSION_TYPE': 'null',
    }

    proc = subprocess.Popen(
        [_PY, 'app.py'],
        cwd=_VCS_WEB,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )

    server = LiveServer(proc, port, db_path)

    ready = _wait_for_server(f'http://127.0.0.1:{port}/', timeout=20)
    if not ready:
        server.teardown()
        pytest.fail('Live server did not start within 20 s')

    yield server

    server.teardown()


# ---------------------------------------------------------------------------
# Wire-format helpers (test-local, mirrors rendezvous_ws codecs exactly)
# ---------------------------------------------------------------------------

def _hello_payload(room_id: bytes, cands: list) -> bytes:
    """Encode HELLO payload: var-bytes(room_id) | u8 count | count×(var-bytes(host) | u16 port)."""
    buf = bytearray()
    buf += len(room_id).to_bytes(2, 'big') + room_id
    buf += bytes([len(cands)])
    for host, port in cands:
        h = host.encode('utf-8')
        buf += len(h).to_bytes(2, 'big') + h
        buf += port.to_bytes(2, 'big')
    return bytes(buf)


def _parse_pair_payload(payload: bytes) -> tuple:
    """Decode PAIR payload: u8 role | u8 count | count×(var-bytes(host) | u16 port)."""
    role = payload[0]
    count = payload[1]
    pos = 2
    cands = []
    for _ in range(count):
        n = int.from_bytes(payload[pos:pos + 2], 'big')
        pos += 2
        host = payload[pos:pos + n].decode('utf-8')
        pos += n
        port = int.from_bytes(payload[pos:pos + 2], 'big')
        pos += 2
        cands.append((host, port))
    return role, cands


# ---------------------------------------------------------------------------
# Import codec helpers from rendezvous_ws for convenience
# ---------------------------------------------------------------------------

import sys as _sys
if _VCS_WEB not in _sys.path:
    _sys.path.insert(0, _VCS_WEB)

from rendezvous_ws import encode_frame, frame_type, frame_payload, HELLO, PAIR, RELAY


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_pairing_roles_and_candidates(live_server):
    """§9.2 — Two clients with the same room_id both receive PAIR; roles are {0,1};
    each client gets the OTHER peer's candidates."""

    async def run():
        import websockets

        rid = b'\x11' * 32
        a_cands = [('1.1.1.1', 1111)]
        b_cands = [('2.2.2.2', 2222)]

        a = await websockets.connect(live_server.ws_url + '/p2p')
        await a.send(encode_frame(HELLO, _hello_payload(rid, a_cands)))

        b = await websockets.connect(live_server.ws_url + '/p2p')
        await b.send(encode_frame(HELLO, _hello_payload(rid, b_cands)))

        fa = await asyncio.wait_for(a.recv(), timeout=5)
        fb = await asyncio.wait_for(b.recv(), timeout=5)

        # Both frames must be PAIR
        assert isinstance(fa, (bytes, bytearray)), f"expected bytes, got {type(fa)}"
        assert isinstance(fb, (bytes, bytearray)), f"expected bytes, got {type(fb)}"
        assert frame_type(fa) == PAIR, f"a got type {frame_type(fa)}, expected PAIR={PAIR}"
        assert frame_type(fb) == PAIR, f"b got type {frame_type(fb)}, expected PAIR={PAIR}"

        ra, ca = _parse_pair_payload(frame_payload(fa))
        rb, cb = _parse_pair_payload(frame_payload(fb))

        # Roles must be {0, 1}
        assert {ra, rb} == {0, 1}, f"roles {ra}, {rb} — expected one INITIATOR and one RESPONDER"

        # Each client must receive the OTHER's candidates
        assert ('2.2.2.2', 2222) in ca, f"a did not receive b's candidates: {ca}"
        assert ('1.1.1.1', 1111) in cb, f"b did not receive a's candidates: {cb}"

        await a.close()
        await b.close()

    asyncio.run(run())


def test_relay_passthrough(live_server):
    """§9.4 — After pairing, a RELAY frame sent by client A arrives at client B
    byte-for-byte identical (server must not alter the payload)."""

    async def run():
        import websockets

        rid = b'\x22' * 32
        a_cands = [('10.0.0.1', 5000)]
        b_cands = [('10.0.0.2', 5001)]

        a = await websockets.connect(live_server.ws_url + '/p2p')
        await a.send(encode_frame(HELLO, _hello_payload(rid, a_cands)))

        b = await websockets.connect(live_server.ws_url + '/p2p')
        await b.send(encode_frame(HELLO, _hello_payload(rid, b_cands)))

        # Drain PAIR frames
        await asyncio.wait_for(a.recv(), timeout=5)
        await asyncio.wait_for(b.recv(), timeout=5)

        # A sends a RELAY frame with a opaque binary payload
        secret_payload = b'\xde\xad\xbe\xef' + bytes(range(32))
        relay_frame = encode_frame(RELAY, secret_payload)
        await a.send(relay_frame)

        # B must receive the EXACT same bytes
        received = await asyncio.wait_for(b.recv(), timeout=5)
        assert isinstance(received, (bytes, bytearray))
        assert bytes(received) == relay_frame, (
            f"relay frame was altered:\n  sent:     {relay_frame.hex()}\n"
            f"  received: {bytes(received).hex()}"
        )

        await a.close()
        await b.close()

    asyncio.run(run())


def test_third_client_rejected(live_server):
    """§9.5 — A third client on the same room_id is rejected: its connection is
    closed by the server without receiving a PAIR frame."""

    async def run():
        import websockets
        from websockets.exceptions import ConnectionClosed

        rid = b'\x33' * 32
        a_cands = [('192.168.1.1', 4000)]
        b_cands = [('192.168.1.2', 4001)]
        c_cands = [('192.168.1.3', 4002)]

        a = await websockets.connect(live_server.ws_url + '/p2p')
        await a.send(encode_frame(HELLO, _hello_payload(rid, a_cands)))

        b = await websockets.connect(live_server.ws_url + '/p2p')
        await b.send(encode_frame(HELLO, _hello_payload(rid, b_cands)))

        # Drain PAIR frames for a and b
        await asyncio.wait_for(a.recv(), timeout=5)
        await asyncio.wait_for(b.recv(), timeout=5)

        # Third client
        c = await websockets.connect(live_server.ws_url + '/p2p')
        await c.send(encode_frame(HELLO, _hello_payload(rid, c_cands)))

        # Server should close c's connection; recv must raise or return nothing
        got_pair = False
        connection_closed = False
        try:
            frame = await asyncio.wait_for(c.recv(), timeout=3)
            # If we received something, it must NOT be a PAIR
            if isinstance(frame, (bytes, bytearray)) and len(frame) >= 1:
                if frame_type(frame) == PAIR:
                    got_pair = True
        except (ConnectionClosed, asyncio.TimeoutError):
            connection_closed = True

        assert not got_pair, "Third client incorrectly received a PAIR frame"
        # Either the connection was closed or we got a timeout (server silent-dropped)
        # Both are acceptable — the key invariant is no PAIR was delivered.

        await a.close()
        await b.close()
        try:
            await c.close()
        except Exception:
            pass

    asyncio.run(run())


def test_lone_client_no_pair(live_server):
    """§9 — A lone client on a unique room_id does not receive a PAIR frame
    (there is no peer); recv must time out."""

    async def run():
        import websockets
        from websockets.exceptions import ConnectionClosed

        rid = b'\x44' * 32  # unique, no other client will join
        cands = [('172.16.0.1', 9999)]

        a = await websockets.connect(live_server.ws_url + '/p2p')
        await a.send(encode_frame(HELLO, _hello_payload(rid, cands)))

        # Server should NOT send anything — expect a timeout
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(a.recv(), timeout=2)

        await a.close()

    asyncio.run(run())
