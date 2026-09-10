"""Сигналинг голосового канала: кто кому шлёт offer и что релеится.

Проверяется ровно то, что легко сломать правкой voice.py:
  1. новичок узнаёт про уже сидящих, а они получают voice_peer_joined;
  2. релей доставляет payload адресату внутри одной комнаты;
  3. релей в чужую комнату не проходит (иначе — утечка сигналинга).
"""
import pytest

from extensions import db, socketio
from models import Room
import app as app_module


def _make_room(room_id):
    db.session.add(Room(room_id=room_id, name='', is_open=True, creator_login='tester'))
    db.session.commit()


def _events(client, name):
    return [e for e in client.get_received() if e['name'] == name]


@pytest.fixture
def voice_clients(app_ctx):
    _make_room('r1')
    _make_room('r2')
    made = []

    def connect():
        c = socketio.test_client(app_module.app, flask_test_client=app_module.app.test_client())
        made.append(c)
        return c

    yield connect
    for c in made:
        if c.is_connected():
            c.disconnect()


def test_joiner_is_announced_to_existing_peers(voice_clients):
    first = voice_clients()
    first.emit('voice_join', {'room_id': 'r1'})
    first.get_received()  # своё voice_peers не мешает следующему assert

    second = voice_clients()
    second.emit('voice_join', {'room_id': 'r1'})

    # Новичок видит уже сидящего и получает ICE-конфиг.
    peers = _events(second, 'voice_peers')
    assert len(peers) == 1
    payload = peers[0]['args'][0]
    assert len(payload['peers']) == 1
    assert payload['ice']

    # Старожил получает уведомление и должен будет прислать offer.
    joined = _events(first, 'voice_peer_joined')
    assert len(joined) == 1
    assert joined[0]['args'][0]['sid'] == payload['self']


def test_signal_is_relayed_inside_room(voice_clients):
    a, b = voice_clients(), voice_clients()
    a.emit('voice_join', {'room_id': 'r1'})
    b.emit('voice_join', {'room_id': 'r1'})
    a_sid = _events(b, 'voice_peers')[0]['args'][0]['peers'][0]['sid']
    b_sid = _events(a, 'voice_peer_joined')[0]['args'][0]['sid']

    a.emit('voice_signal', {'to': b_sid, 'data': {'sdp': 'FAKE_OFFER'}})

    relayed = _events(b, 'voice_signal')
    assert len(relayed) == 1
    assert relayed[0]['args'][0] == {'from': a_sid, 'data': {'sdp': 'FAKE_OFFER'}}


def test_signal_across_rooms_is_dropped(voice_clients):
    a, b = voice_clients(), voice_clients()
    a.emit('voice_join', {'room_id': 'r1'})
    b.emit('voice_join', {'room_id': 'r2'})
    b_sid = _events(b, 'voice_peers')[0]['args'][0]['self']

    a.emit('voice_signal', {'to': b_sid, 'data': {'sdp': 'FAKE_OFFER'}})

    assert _events(b, 'voice_signal') == []


def test_leave_notifies_remaining_peers(voice_clients):
    a, b = voice_clients(), voice_clients()
    a.emit('voice_join', {'room_id': 'r1'})
    b.emit('voice_join', {'room_id': 'r1'})
    b_sid = _events(a, 'voice_peer_joined')[0]['args'][0]['sid']

    b.emit('voice_leave')

    left = _events(a, 'voice_peer_left')
    assert len(left) == 1
    assert left[0]['args'][0]['sid'] == b_sid


# ── Разбор TURN-конфигурации из окружения ─────────────────────────────────
# deploy.sh выставляет именно TURN_URL/TURN_USER/TURN_PASSWORD: JSON в
# systemd Environment= доезжает покалеченным, поэтому путь простых
# переменных обязан работать.

@pytest.fixture
def clean_env(monkeypatch):
    for k in ('ICE_SERVERS', 'TURN_URL', 'TURN_USER', 'TURN_PASSWORD'):
        monkeypatch.delenv(k, raising=False)
    return monkeypatch


def test_ice_defaults_to_public_stun(clean_env):
    import voice
    assert voice._ice_from_env() == [{"urls": "stun:stun.l.google.com:19302"}]


def test_ice_from_turn_vars(clean_env):
    import voice
    clean_env.setenv('TURN_URL', 'turn:soufos.ru:3478')
    clean_env.setenv('TURN_USER', 'vsc')
    clean_env.setenv('TURN_PASSWORD', 'secret')
    assert voice._ice_from_env() == [
        {"urls": "stun:soufos.ru:3478"},
        {"urls": "turn:soufos.ru:3478", "username": "vsc", "credential": "secret"},
    ]


def test_broken_ice_json_falls_back_instead_of_crashing(clean_env):
    import voice
    clean_env.setenv('ICE_SERVERS', '[{"urls": не json}]')
    clean_env.setenv('TURN_URL', 'turn:soufos.ru:3478')
    # Битый JSON не должен ронять импорт и не должен глушить TURN_URL.
    assert voice._ice_from_env()[-1]["urls"] == 'turn:soufos.ru:3478'
