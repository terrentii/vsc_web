"""Вход в комнату по 10-значному коду через Bearer-API (паритет с вебом).

Открытая — любой зарегистрированный становится участником (как visitor на
вебе); закрытая — только уже участник; личная — только владелец.
"""
from extensions import db
from models import Room


def _auth(client, username):
    tok = client.post('/api/auth/register', json={'username': username, 'password': 'pw12'}).get_json()['token']
    return {'Authorization': f'Bearer {tok}'}


def _room(client, h, name='комната'):
    return client.post('/api/rooms', json={'name': name}, headers=h).get_json()


def _join(client, h, code):
    return client.post('/api/rooms/join', json={'code': code}, headers=h)


def test_room_dict_has_code(client):
    h = _auth(client, 'coder')
    room = _room(client, h)
    assert room['code'].isdigit() and len(room['code']) == 10
    listed = client.get('/api/rooms', headers=h).get_json()['rooms']
    assert next(r for r in listed if r['id'] == room['id'])['code'] == room['code']


def test_join_open_room(client):
    room = _room(client, _auth(client, 'owner_o'))
    guest = _auth(client, 'guest_o')
    r = _join(client, guest, room['code'])
    assert r.status_code == 200
    assert r.get_json()['id'] == room['id'] and r.get_json()['code'] == room['code']
    assert room['id'] in [x['id'] for x in client.get('/api/rooms', headers=guest).get_json()['rooms']]
    sent = client.post('/api/messages', json={'room_id': room['id'], 'body': 'привет', 'client_msg_id': 'j1'}, headers=guest)
    assert sent.status_code == 200
    assert _join(client, guest, room['code']).status_code == 200  # повторный вход — без ошибки


def test_join_closed_room_only_for_members(client):
    owner = _auth(client, 'owner_c')
    room = _room(client, owner)
    Room.query.filter_by(room_id=room['code']).update({'is_open': False})
    db.session.commit()
    r = _join(client, _auth(client, 'guest_c'), room['code'])
    assert r.status_code == 403 and r.get_json()['error'] == 'forbidden'
    assert _join(client, owner, room['code']).status_code == 200


def test_join_personal_room_forbidden(client):
    _auth(client, 'owner_p')
    code = Room.query.filter_by(personal_login='owner_p').first().room_id
    assert _join(client, _auth(client, 'guest_p'), code).status_code == 403


def test_join_bad_or_unknown_code(client):
    h = _auth(client, 'guest_x')
    assert _join(client, h, '12345').status_code == 400
    assert _join(client, h, 'abcdefghij').status_code == 400
    r = _join(client, h, '0000000000')
    assert r.status_code == 404 and r.get_json()['error'] == 'not_found'


def test_join_requires_bearer(client):
    assert client.post('/api/rooms/join', json={'code': '0000000000'}).status_code == 401
