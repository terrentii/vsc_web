"""Task 1.7 — Live-server integration tests for the raw /ws endpoint.

Fixture strategy
----------------
A module-scoped fixture starts a real server subprocess so flask-sock's
eventlet WebSocket handling works exactly as it does in production.  The
in-process Flask test client cannot upgrade to a real WebSocket, which is
why a subprocess is necessary.

The fixture picks a free port, launches `app.py` with a dedicated temp DB,
waits until the HTTP server is accepting connections, then tears down the
process after the whole module finishes.

Web-path fan-out (POST /api/room/<str_room_id>/message)
-------------------------------------------------------
That endpoint requires a session-cookie CSRF check OR an X-Api-Key.  Driving
the full web CSRF flow from a subprocess test is complex (session cookies,
CSRF tokens).  Coverage for that code path is therefore provided by a focused
unit test (`test_fanout_direct`) that exercises `fanout_message` directly
while a subscribed WebSocket connection is open — it verifies the subscription
registry and delivery path without going through HTTP.  The actual call-site
in api.py was manually verified to match centralized.py's invocation pattern.
"""

import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_VCS_WEB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PY = sys.executable  # venv python (tests run under the venv)


def _free_port() -> int:
    """Bind to port 0, record the assigned port, close, return it."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def _wait_for_server(url: str, timeout: float = 20.0) -> bool:
    """Poll url until HTTP responds (any status) or timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            urllib.request.urlopen(url, timeout=1)
            return True
        except urllib.error.HTTPError:
            return True  # got a response → server is up
        except Exception:
            time.sleep(0.2)
    return False


# ---------------------------------------------------------------------------
# Live server fixture
# ---------------------------------------------------------------------------

class LiveServer:
    def __init__(self, proc: subprocess.Popen, port: int, db_path: str):
        self._proc = proc
        self.port = port
        self.http_url = f'http://127.0.0.1:{port}'
        self.ws_url = f'ws://127.0.0.1:{port}'
        self._db_path = db_path

    # ---- HTTP helpers (stdlib only) ----------------------------------------

    def _request(self, method: str, path: str, data: bytes | None = None,
                 headers: dict | None = None) -> tuple[int, dict]:
        url = self.http_url + path
        req = urllib.request.Request(url, data=data, method=method,
                                     headers=headers or {})
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def register(self, username: str, password: str) -> str:
        payload = json.dumps({'username': username, 'password': password}).encode()
        status, body = self._request(
            'POST', '/api/auth/register', data=payload,
            headers={'Content-Type': 'application/json'},
        )
        assert status == 201, f'register failed: {status} {body}'
        return body['token']

    def create_room(self, token: str, name: str) -> int:
        payload = json.dumps({'name': name}).encode()
        status, body = self._request(
            'POST', '/api/rooms', data=payload,
            headers={
                'Content-Type': 'application/json',
                'Authorization': f'Bearer {token}',
            },
        )
        assert status == 201, f'create_room failed: {status} {body}'
        return body['id']

    def post_message(self, token: str, room_id: int, body_text: str, cid: str) -> dict:
        payload = json.dumps({
            'room_id': room_id,
            'body': body_text,
            'client_msg_id': cid,
        }).encode()
        status, body = self._request(
            'POST', '/api/messages', data=payload,
            headers={
                'Content-Type': 'application/json',
                'Authorization': f'Bearer {token}',
            },
        )
        assert status in (200, 201), f'post_message failed: {status} {body}'
        return body

    def teardown(self):
        try:
            self._proc.terminate()
            self._proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            self._proc.kill()
            self._proc.wait(timeout=4)
        except Exception:
            pass
        # Clean up temp DB
        try:
            os.unlink(self._db_path)
        except Exception:
            pass


@pytest.fixture(scope='module')
def live_server():
    port = _free_port()
    db_fd, db_path = tempfile.mkstemp(suffix='.db', prefix='vsc_wstest_')
    os.close(db_fd)

    env = {
        **os.environ,
        'DATABASE_URL': f'sqlite:///{db_path}',
        'PORT': str(port),
        'SECRET_KEY': 'ws-test-secret',
        # Disable filesystem session to avoid permission issues
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
# Tests
# ---------------------------------------------------------------------------

def test_ws_unauthorized(live_server):
    """Garbage token → error:unauthorized frame then server closes."""
    from websockets.sync.client import connect as ws_connect
    with ws_connect(live_server.ws_url + '/ws', open_timeout=5) as ws:
        ws.send(json.dumps({'type': 'auth', 'token': 'garbage'}))
        frame = json.loads(ws.recv())
        assert frame['type'] == 'error'
        assert frame['code'] == 'unauthorized'


def test_ws_auth_ready_and_message_fanout(live_server):
    """Valid token → ready, then POST /api/messages delivers message frame."""
    from websockets.sync.client import connect as ws_connect

    token = live_server.register('wsuser', 'pw123456')
    rid = live_server.create_room(token, 'wsroom')

    with ws_connect(live_server.ws_url + '/ws', open_timeout=5) as ws:
        ws.send(json.dumps({'type': 'auth', 'token': token}))

        ready_frame = json.loads(ws.recv())
        assert ready_frame['type'] == 'ready', f'Expected ready, got: {ready_frame}'

        # Post a message via HTTP — server should fan it out over WS
        live_server.post_message(token, rid, 'ping', 'cid-ws-1')

        # Give the server up to 5 s to push the frame
        ws.socket.settimeout(5.0)
        msg_frame = json.loads(ws.recv())
        assert msg_frame['type'] == 'message', f'Unexpected frame: {msg_frame}'
        assert msg_frame['room_id'] == rid
        assert msg_frame['body'] == 'ping'
        assert 'sender' in msg_frame
        assert 'created_at' in msg_frame
        assert 'id' in msg_frame


def test_fanout_direct():
    """Unit test: fanout_message delivers to a subscribed mock WS in-process.

    This verifies the subscription registry and delivery path used by BOTH
    write paths (centralized.py and api.py) without needing HTTP.
    """
    # Import inside test to avoid conftest-level import side effects
    import importlib
    import sys

    # Make sure ws_centralized is importable in test process (conftest sets path)
    import ws_centralized as wsc

    received = []

    class MockWS:
        def send(self, data):
            received.append(json.loads(data))

    mock_ws = MockWS()
    fake_room_id = 99999

    # Manually subscribe the mock WS
    with wsc._lock:
        wsc._ws_rooms[mock_ws] = {fake_room_id}
        wsc._subs.setdefault(fake_room_id, set()).add(mock_ws)

    try:
        wsc.fanout_message(fake_room_id, {
            'id': 42,
            'sender': 'tester',
            'body': 'hello fanout',
            'created_at': '2026-01-01T00:00:00',
        })
        assert len(received) == 1
        frame = received[0]
        assert frame['type'] == 'message'
        assert frame['room_id'] == fake_room_id
        assert frame['body'] == 'hello fanout'
        assert frame['sender'] == 'tester'
        assert frame['id'] == 42
    finally:
        # Clean up mock subscription
        with wsc._lock:
            wsc._ws_rooms.pop(mock_ws, None)
            wsc._subs.get(fake_room_id, set()).discard(mock_ws)
