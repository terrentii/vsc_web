"""Настройки комнат, диалоги, приглашения, аватарки, прочтение/присутствие/печать."""
import io

import pytest
from PIL import Image

import dialogs


def _auth(client, user):
    tok = client.post('/api/auth/register', json={'username': user, 'password': 'pw12'}).get_json()['token']
    return {'Authorization': f'Bearer {tok}'}


def _room(client, h, name='комната'):
    return client.post('/api/rooms', json={'name': name}, headers=h).get_json()


def _view(client, h, rid):
    return client.get(f'/api/rooms/{rid}', headers=h)


def _act(client, h, rid, action):
    return client.post(f'/api/rooms/{rid}/dialog', json={'action': action}, headers=h)


def _post(client, h, rid, body='привет'):
    return client.post('/api/messages', json={'room_id': rid, 'body': body}, headers=h)


@pytest.fixture
def events(monkeypatch):
    got = []
    monkeypatch.setattr(dialogs, 'emit', lambda room, kind, **d: got.append((kind, d)))
    return got


@pytest.fixture
def pair(client):
    a, b = _auth(client, 'alice'), _auth(client, 'bobby')
    r = _room(client, a)
    client.post('/api/rooms/join', json={'code': r['code']}, headers=b)
    return a, b, r['id']


@pytest.fixture
def dialog(client, pair):
    a, b, rid = pair
    _act(client, a, rid, 'propose')
    assert _act(client, b, rid, 'accept').status_code == 200
    return pair


def _jpeg_with_exif() -> bytes:
    img = Image.new('RGB', (640, 320), (77, 123, 255))
    exif = Image.Exif()
    exif[0x010F] = 'SecretPhoneMaker'
    out = io.BytesIO()
    img.save(out, 'JPEG', exif=exif)
    return out.getvalue()


def test_propose_accept_makes_closed_dialog(client, pair):
    a, b, rid = pair
    r = _act(client, a, rid, 'propose').get_json()
    assert r['dialog_proposed_by'] == 'alice' and not r['is_dialog']
    v = _act(client, b, rid, 'accept').get_json()
    assert v['is_dialog'] and not v['is_open'] and v['dialog_proposed_by'] is None
    va = _view(client, a, rid).get_json()
    assert va['peer'] == 'bobby' and va['title'] == 'bobby' and len(va['member_list']) == 2


def test_mutual_propose_is_accept(client, pair):
    a, b, rid = pair
    _act(client, a, rid, 'propose')
    assert _act(client, b, rid, 'propose').get_json()['is_dialog']


def test_cannot_accept_own_or_missing_proposal(client, pair):
    a, b, rid = pair
    assert _act(client, a, rid, 'accept').get_json()['error'] == 'no_proposal'
    _act(client, a, rid, 'propose')
    assert _act(client, a, rid, 'accept').status_code == 409


def test_three_members_not_eligible_and_proposal_burns(client, pair):
    a, b, rid = pair
    _act(client, a, rid, 'propose')
    c = _auth(client, 'carol')
    code = _view(client, a, rid).get_json()['code']
    client.post('/api/rooms/join', json={'code': code}, headers=c)
    assert _view(client, a, rid).get_json()['dialog_proposed_by'] is None
    assert _act(client, a, rid, 'propose').get_json()['error'] == 'not_eligible'


def test_dialog_locks_membership_and_openness(client, dialog):
    a, b, rid = dialog
    c = _auth(client, 'carol')
    code = _view(client, a, rid).get_json()['code']
    assert client.post('/api/rooms/join', json={'code': code}, headers=c).status_code == 403
    assert client.post(f'/api/rooms/{rid}/members', json={'login': 'carol'}, headers=a).get_json()['error'] == 'dialog'
    assert client.delete(f'/api/rooms/{rid}/members/bobby', headers=a).status_code == 409
    assert client.patch(f'/api/rooms/{rid}', json={'is_open': True}, headers=a).status_code == 409


def test_disable_keeps_closed_room(client, dialog):
    a, b, rid = dialog
    v = _act(client, b, rid, 'disable').get_json()
    assert not v['is_dialog'] and not v['is_open']


def test_leave_turns_dialog_off_and_creator_cannot_leave(client, dialog):
    a, b, rid = dialog
    assert client.post(f'/api/rooms/{rid}/leave', headers=a).get_json()['error'] == 'creator'
    assert client.post(f'/api/rooms/{rid}/leave', headers=b).status_code == 204
    v = _view(client, a, rid).get_json()
    assert not v['is_dialog'] and v['members'] == 1
    assert _view(client, b, rid).status_code == 403


def test_invite_flow_blocks_posting_until_accept(client):
    a, b = _auth(client, 'alice'), _auth(client, 'bobby')
    r = client.post('/api/dialogs', json={'login': 'bobby'}, headers=a)
    assert r.status_code == 201
    rid = r.get_json()['id']
    assert r.get_json()['role'] == 'godfather' and r.get_json()['dialog_proposed_by'] == 'alice'
    mine = client.get('/api/rooms', headers=b).get_json()['rooms']
    assert [x['role'] for x in mine if x['id'] == rid] == ['invited']
    assert _post(client, a, rid).get_json()['error'] == 'pending_invite'
    assert _act(client, b, rid, 'accept').get_json()['is_dialog']
    assert _post(client, b, rid).status_code == 200


def test_invite_decline_deletes_empty_room(client):
    a, b = _auth(client, 'alice'), _auth(client, 'bobby')
    rid = client.post('/api/dialogs', json={'login': 'bobby'}, headers=a).get_json()['id']
    assert _act(client, b, rid, 'decline').get_json() == {'deleted': True}
    assert _view(client, a, rid).status_code == 404


def test_invite_errors_and_repeat(client):
    a = _auth(client, 'alice')
    _auth(client, 'bobby')
    assert client.post('/api/dialogs', json={'login': 'alice'}, headers=a).status_code == 400
    assert client.post('/api/dialogs', json={'login': 'nobody'}, headers=a).status_code == 404
    first = client.post('/api/dialogs', json={'login': 'bobby'}, headers=a).get_json()['id']
    again = client.post('/api/dialogs', json={'login': 'bobby'}, headers=a)
    assert again.status_code == 200 and again.get_json()['id'] == first


def test_invite_rate_limit(client, monkeypatch):
    monkeypatch.setattr(dialogs, 'INVITES_PER_DAY', 1)
    a = _auth(client, 'zed')
    _auth(client, 'bobby')
    _auth(client, 'carol')
    assert client.post('/api/dialogs', json={'login': 'bobby'}, headers=a).status_code == 201
    assert client.post('/api/dialogs', json={'login': 'carol'}, headers=a).status_code == 429


def test_personal_title_is_private(client, dialog):
    a, b, rid = dialog
    assert client.patch(f'/api/rooms/{rid}/me', json={'title': 'Друг'}, headers=a).get_json()['title'] == 'Друг'
    assert _view(client, b, rid).get_json()['title'] == 'alice'
    client.patch(f'/api/rooms/{rid}/me', json={'title': ''}, headers=a)
    assert _view(client, a, rid).get_json()['title'] == 'bobby'


def test_title_only_in_dialog(client, pair):
    a, b, rid = pair
    assert client.patch(f'/api/rooms/{rid}/me', json={'title': 'x'}, headers=a).get_json()['error'] == 'not_dialog'


def test_dialog_avatar_is_personal_and_stripped(client, dialog):
    a, b, rid = dialog
    up = client.put(f'/api/rooms/{rid}/avatar', data={'file': (io.BytesIO(_jpeg_with_exif()), 'p.jpg')}, headers=a)
    assert up.status_code == 200 and up.get_json()['avatar_rev'] == 1
    assert _view(client, b, rid).get_json()['avatar_rev'] == 0
    got = client.get(f'/api/rooms/{rid}/avatar', headers=a)
    img = Image.open(io.BytesIO(got.data))
    assert got.mimetype == 'image/png' and img.size == (256, 256)
    assert not img.getexif() and b'SecretPhoneMaker' not in got.data
    assert client.get(f'/api/rooms/{rid}/avatar', headers=b).status_code == 404
    assert client.delete(f'/api/rooms/{rid}/avatar', headers=a).get_json()['avatar_rev'] == 0


def test_room_avatar_creator_only_and_bad_image(client, pair):
    a, b, rid = pair
    png = io.BytesIO()
    Image.new('RGB', (10, 10)).save(png, 'PNG')
    put = lambda h, data: client.put(f'/api/rooms/{rid}/avatar', data={'file': (io.BytesIO(data), 'a.png')}, headers=h)
    assert put(b, png.getvalue()).status_code == 403
    assert put(a, png.getvalue()).status_code == 200
    assert _view(client, b, rid).get_json()['avatar_rev'] == 1
    assert put(a, b'not an image').get_json()['error'] == 'bad_image'


def test_settings_creator_only(client, pair):
    a, b, rid = pair
    assert client.patch(f'/api/rooms/{rid}', json={'name': 'x'}, headers=b).status_code == 403
    v = client.patch(f'/api/rooms/{rid}', json={'name': 'Новое', 'is_open': False}, headers=a).get_json()
    assert v['name'] == 'Новое' and not v['is_open']


def test_members_add_and_kick(client, pair):
    a, b, rid = pair
    _auth(client, 'carol')
    assert client.post(f'/api/rooms/{rid}/members', json={'login': 'nobody'}, headers=a).status_code == 404
    assert client.post(f'/api/rooms/{rid}/members', json={'login': 'carol'}, headers=a).get_json()['members'] == 3
    assert client.delete(f'/api/rooms/{rid}/members/carol', headers=a).get_json()['members'] == 2
    assert client.delete(f'/api/rooms/{rid}/members/alice', headers=a).status_code == 400


def test_read_receipts_respect_privacy(client, dialog, events):
    a, b, rid = dialog
    mid = _post(client, b, rid).get_json()['id']
    assert client.post(f'/api/rooms/{rid}/read', json={'message_id': mid}, headers=a).status_code == 204
    assert _view(client, b, rid).get_json()['peer_read'] == mid
    assert ('read', {'login': 'alice', 'message_id': mid}) in events
    client.patch('/api/me', json={'share_read': False}, headers=a)
    assert _view(client, b, rid).get_json()['peer_read'] is None


def test_read_only_in_dialog(client, pair):
    a, b, rid = pair
    mid = _post(client, b, rid).get_json()['id']
    assert client.post(f'/api/rooms/{rid}/read', json={'message_id': mid}, headers=a).status_code == 409


def test_presence(client, dialog, events):
    a, b, rid = dialog
    dialogs.connected('bobby')
    assert _view(client, a, rid).get_json()['peer_online'] is True
    assert ('presence', {'login': 'bobby', 'online': True}) in events
    dialogs.disconnected('bobby')
    assert _view(client, a, rid).get_json()['peer_online'] is False
    client.patch('/api/me', json={'share_online': False}, headers=b)
    assert _view(client, a, rid).get_json()['peer_online'] is None


def test_typing_is_throttled_and_dialog_only(client, dialog, pair, events):
    from extensions import db
    from models import Room
    a, b, rid = dialog
    room = db.session.get(Room, rid)
    dialogs.typing(room, 'alice')
    dialogs.typing(room, 'alice')
    assert [e for e in events if e[0] == 'typing'] == [('typing', {'login': 'alice'})]


def test_me_settings(client):
    a = _auth(client, 'alice')
    assert client.get('/api/me', headers=a).get_json() == {'login': 'alice', 'share_read': True, 'share_online': True}
    assert client.patch('/api/me', json={'share_online': False}, headers=a).get_json()['share_online'] is False
