"""Phase-1 integration tests for the centralized API (vsc_web)."""


def test_auth_tokens_table_and_client_msg_id_exist(app_ctx):
    from sqlalchemy import inspect as sa_inspect
    from extensions import db
    insp = sa_inspect(db.engine)
    assert 'auth_tokens' in insp.get_table_names()
    cols = {c['name'] for c in insp.get_columns('messages')}
    assert 'client_msg_id' in cols


def test_issue_and_resolve_token(app_ctx):
    from bearer import issue_token, _hash_token, resolve_bearer_token
    from models import AuthToken
    raw = issue_token('alice')
    assert AuthToken.query.filter_by(token_hash=_hash_token(raw)).first().login == 'alice'
    assert resolve_bearer_token(raw) == 'alice'
    assert resolve_bearer_token('garbage') is None


def test_register_returns_token_and_user(client):
    r = client.post('/api/auth/register', json={'username': 'alice', 'password': 'pw12'})
    assert r.status_code == 201
    body = r.get_json()
    assert body['user']['username'] == 'alice' and isinstance(body['user']['id'], int)
    assert isinstance(body['token'], str) and body['token']


def test_register_duplicate_409(client):
    client.post('/api/auth/register', json={'username': 'bob', 'password': 'pw12'})
    r = client.post('/api/auth/register', json={'username': 'bob', 'password': 'pw12'})
    assert r.status_code == 409 and r.get_json() == {'error': 'username_taken'}


def test_login_ok_and_bad_credentials(client):
    client.post('/api/auth/register', json={'username': 'carol', 'password': 'pw12'})
    assert client.post('/api/auth/login', json={'username': 'carol', 'password': 'pw12'}).status_code == 200
    bad = client.post('/api/auth/login', json={'username': 'carol', 'password': 'nope'})
    assert bad.status_code == 401 and bad.get_json() == {'error': 'invalid_credentials'}


def test_logout_invalidates_token(client):
    tok = client.post('/api/auth/register', json={'username': 'dave', 'password': 'pw12'}).get_json()['token']
    h = {'Authorization': f'Bearer {tok}'}
    assert client.post('/api/auth/logout', headers=h).status_code == 204
    # повторный logout тем же токеном — уже невалиден
    assert client.post('/api/auth/logout', headers=h).status_code == 401


# ── Task 1.4: Bearer GET /api/rooms (hook), POST /api/rooms, GET /api/rooms/<id>/messages ─

def _auth(client, username):
    """Регистрирует пользователя и возвращает заголовок Authorization."""
    tok = client.post('/api/auth/register', json={'username': username, 'password': 'pw12'}).get_json()['token']
    return {'Authorization': f'Bearer {tok}'}


def test_create_and_list_rooms(client):
    h = _auth(client, 'rasmus')
    r = client.post('/api/rooms', json={'name': 'проект'}, headers=h)
    assert r.status_code == 201
    created = r.get_json()
    assert set(created) == {'id', 'name', 'is_direct', 'updated_at'}
    assert created['is_direct'] is False
    assert created['name'] == 'проект'
    assert isinstance(created['id'], int)

    rooms = client.get('/api/rooms', headers=h).get_json()['rooms']
    assert created['id'] in [r['id'] for r in rooms]
    # Каждая комната содержит нужные поля
    room_entry = next(r for r in rooms if r['id'] == created['id'])
    assert set(room_entry) >= {'id', 'name', 'is_direct', 'updated_at'}


def test_create_room_no_name(client):
    """POST /api/rooms без поля name создаёт комнату с name=None."""
    h = _auth(client, 'noname_user')
    r = client.post('/api/rooms', json={}, headers=h)
    assert r.status_code == 201
    body = r.get_json()
    assert body['name'] is None
    assert body['is_direct'] is False


def test_list_rooms_no_bearer_unchanged(client):
    """GET /api/rooms без Bearer возвращает прежний формат (список открытых комнат)."""
    # Создаём комнату через Bearer, чтобы было что вернуть
    h = _auth(client, 'webuser')
    client.post('/api/rooms', json={'name': 'webroom'}, headers=h)
    # Запрос без заголовка — старый формат: массив объектов с ключами room_id/name/created_at
    resp = client.get('/api/rooms')
    assert resp.status_code == 200
    data = resp.get_json()
    assert isinstance(data, list)


def test_messages_cursor_pagination(client):
    h = _auth(client, 'pagey')
    rid = client.post('/api/rooms', json={'name': 'r'}, headers=h).get_json()['id']

    # Получаем строковый room_id для прямой вставки сообщений
    from models import Room, Message, db
    from datetime import datetime, timezone
    room_obj = Room.query.get(rid)
    str_room_id = room_obj.room_id

    # Вставляем 3 сообщения напрямую (task 1.5 ещё не реализован)
    for i in range(3):
        msg = Message(
            room_id=str_room_id,
            author='pagey',
            text=f'm{i}',
            timestamp=datetime.now(timezone.utc).replace(tzinfo=None),
            client_msg_id=f'c{i}',
        )
        db.session.add(msg)
    db.session.commit()

    page = client.get(f'/api/rooms/{rid}/messages?limit=2', headers=h).get_json()
    assert len(page['messages']) == 2
    assert page['next_cursor'] == page['messages'][-1]['id']

    m = page['messages'][0]
    assert set(m) >= {'id', 'room_id', 'sender', 'body', 'created_at'}
    assert m['room_id'] == rid
    assert m['sender'] == 'pagey'

    rest = client.get(f"/api/rooms/{rid}/messages?after={page['next_cursor']}", headers=h).get_json()
    assert len(rest['messages']) == 1
    assert rest['next_cursor'] is None


def test_messages_empty_room(client):
    h = _auth(client, 'emptyuser')
    rid = client.post('/api/rooms', json={'name': 'empty'}, headers=h).get_json()['id']
    resp = client.get(f'/api/rooms/{rid}/messages', headers=h)
    assert resp.status_code == 200
    body = resp.get_json()
    assert body['messages'] == []
    assert body['next_cursor'] is None


def test_messages_room_not_found(client):
    h = _auth(client, 'finder')
    assert client.get('/api/rooms/9999/messages', headers=h).status_code == 404


def test_rooms_requires_bearer(client):
    """GET /api/rooms/<id>/messages без Bearer возвращает 401."""
    assert client.get('/api/rooms/9999/messages').status_code == 401


def test_messages_forbidden_for_non_member(client):
    """Пользователь, не состоящий в комнате, получает 403."""
    h_owner = _auth(client, 'owner2')
    rid = client.post('/api/rooms', json={'name': 'private'}, headers=h_owner).get_json()['id']
    h_other = _auth(client, 'stranger2')
    assert client.get(f'/api/rooms/{rid}/messages', headers=h_other).status_code == 403


def test_post_message_idempotent(client):
    h = _auth(client, 'idem')
    rid = client.post('/api/rooms', json={'name': 'r'}, headers=h).get_json()['id']
    body = {'room_id': rid, 'body': 'hi', 'client_msg_id': 'dup-1'}
    first = client.post('/api/messages', json=body, headers=h)
    assert first.status_code == 200
    first_j = first.get_json()
    second_j = client.post('/api/messages', json=body, headers=h).get_json()
    assert first_j['id'] == second_j['id']
    assert set(first_j) >= {'id', 'room_id', 'sender', 'body', 'created_at', 'client_msg_id'}
    assert first_j['room_id'] == rid and first_j['sender'] == 'idem' and first_j['body'] == 'hi'
    page = client.get(f'/api/rooms/{rid}/messages', headers=h).get_json()
    assert len(page['messages']) == 1  # дубля нет

def test_post_message_requires_bearer(client):
    assert client.post('/api/messages', json={'room_id': 1, 'body': 'x', 'client_msg_id': 'c'}).status_code == 401

def test_post_message_unknown_room_404(client):
    h = _auth(client, 'noroom')
    assert client.post('/api/messages', json={'room_id': 999999, 'body': 'x', 'client_msg_id': 'c'}, headers=h).status_code == 404


def test_updated_at_reflects_last_message(client):
    """updated_at комнаты меняется после добавления сообщения."""
    h = _auth(client, 'timey')
    created = client.post('/api/rooms', json={'name': 'timely'}, headers=h).get_json()
    rid = created['id']
    updated_at_before = created['updated_at']

    from models import Room, Message, db
    from datetime import datetime, timezone
    import time
    room_obj = Room.query.get(rid)

    # небольшая задержка чтобы timestamp отличался
    time.sleep(0.01)
    msg = Message(
        room_id=room_obj.room_id,
        author='timey',
        text='hello',
        timestamp=datetime.now(timezone.utc).replace(tzinfo=None),
    )
    db.session.add(msg)
    db.session.commit()

    rooms = client.get('/api/rooms', headers=h).get_json()['rooms']
    room_entry = next(r for r in rooms if r['id'] == rid)
    assert room_entry['updated_at'] > updated_at_before


def test_web_routes_still_work(client):
    """Регрессия: существующие веб-маршруты не сломаны новыми Bearer-эндпоинтами."""
    assert client.get('/').status_code == 200
    # существующий анонимный /api/* отвечает как прежде (не 404/500)
    assert client.get('/api/rooms/tg').status_code in (200, 401, 403)


# ── Вложения (Bearer) ──────────────────────────────────────────────────────────

import io


def _room(client, h, name='media-room'):
    return client.post('/api/rooms', json={'name': name}, headers=h).get_json()['id']


def test_upload_media_requires_bearer(client):
    rid = _room(client, _auth(client, 'up1'))
    r = client.post(f'/api/rooms/{rid}/media',
                     data={'file': (io.BytesIO(b'\x89PNG'), 'x.png')},
                     content_type='multipart/form-data')
    assert r.status_code == 401


def test_upload_media_rejects_bad_extension(client):
    h = _auth(client, 'up2')
    rid = _room(client, h)
    r = client.post(f'/api/rooms/{rid}/media',
                     data={'file': (io.BytesIO(b'<svg/>'), 'x.svg')},
                     content_type='multipart/form-data', headers=h)
    assert r.status_code == 415


def test_upload_media_success_returns_filename_mime_size(client):
    h = _auth(client, 'up3')
    rid = _room(client, h)
    payload = b'\x89PNG\r\n\x1a\n' + b'0' * 100
    r = client.post(f'/api/rooms/{rid}/media',
                     data={'file': (io.BytesIO(payload), 'photo.png')},
                     content_type='multipart/form-data', headers=h)
    assert r.status_code == 201
    body = r.get_json()
    assert body['ok'] is True
    assert body['filename'].endswith('_photo.png')
    assert body['mime_type'] == 'image/png'
    assert body['size'] == len(payload)


def test_upload_media_forbidden_for_non_member(client):
    h_owner = _auth(client, 'up4')
    rid = _room(client, h_owner)
    h_other = _auth(client, 'up4stranger')
    r = client.post(f'/api/rooms/{rid}/media',
                     data={'file': (io.BytesIO(b'x'), 'x.txt')},
                     content_type='multipart/form-data', headers=h_other)
    assert r.status_code == 403


def test_upload_media_room_not_found_404(client):
    h = _auth(client, 'up5')
    r = client.post('/api/rooms/999999/media',
                     data={'file': (io.BytesIO(b'x'), 'x.txt')},
                     content_type='multipart/form-data', headers=h)
    assert r.status_code == 404


def test_download_media_requires_bearer(client):
    h = _auth(client, 'dl1')
    rid = _room(client, h)
    up = client.post(f'/api/rooms/{rid}/media',
                      data={'file': (io.BytesIO(b'hello'), 'a.txt')},
                      content_type='multipart/form-data', headers=h).get_json()
    r = client.get(f'/api/rooms/{rid}/media/{up["filename"]}')
    assert r.status_code == 401


def test_download_media_success_serves_bytes_with_strict_mime(client):
    h = _auth(client, 'dl2')
    rid = _room(client, h)
    payload = b'hello world'
    up = client.post(f'/api/rooms/{rid}/media',
                      data={'file': (io.BytesIO(payload), 'a.txt')},
                      content_type='multipart/form-data', headers=h).get_json()
    r = client.get(f'/api/rooms/{rid}/media/{up["filename"]}', headers=h)
    assert r.status_code == 200
    assert r.data == payload
    assert r.headers['Content-Type'].startswith('text/plain')


def test_download_media_unknown_file_404(client):
    h = _auth(client, 'dl3')
    rid = _room(client, h)
    r = client.get(f'/api/rooms/{rid}/media/does-not-exist.png', headers=h)
    assert r.status_code == 404


def test_download_media_forbidden_for_non_member(client):
    h_owner = _auth(client, 'dl4')
    rid = _room(client, h_owner)
    up = client.post(f'/api/rooms/{rid}/media',
                      data={'file': (io.BytesIO(b'x'), 'x.txt')},
                      content_type='multipart/form-data', headers=h_owner).get_json()
    h_other = _auth(client, 'dl4stranger')
    r = client.get(f'/api/rooms/{rid}/media/{up["filename"]}', headers=h_other)
    assert r.status_code == 403


def test_post_message_with_media_persists_and_included_in_response_and_history(client):
    h = _auth(client, 'msgmedia1')
    rid = _room(client, h)
    up = client.post(f'/api/rooms/{rid}/media',
                      data={'file': (io.BytesIO(b'\x89PNG'), 'p.png')},
                      content_type='multipart/form-data', headers=h).get_json()
    r = client.post('/api/messages',
                     json={'room_id': rid, 'body': '', 'client_msg_id': 'm1', 'media': up['filename']},
                     headers=h)
    assert r.status_code == 200
    assert r.get_json()['media'] == up['filename']

    page = client.get(f'/api/rooms/{rid}/messages', headers=h).get_json()
    assert page['messages'][0]['media'] == up['filename']


def test_post_message_media_must_exist_400(client):
    h = _auth(client, 'msgmedia2')
    rid = _room(client, h)
    r = client.post('/api/messages',
                     json={'room_id': rid, 'body': '', 'client_msg_id': 'm2', 'media': 'nope.png'},
                     headers=h)
    assert r.status_code == 400
    assert r.get_json()['error'] == 'media_not_found'


def test_post_message_empty_body_and_no_media_400(client):
    h = _auth(client, 'msgmedia3')
    rid = _room(client, h)
    r = client.post('/api/messages',
                     json={'room_id': rid, 'body': '', 'client_msg_id': 'm3'},
                     headers=h)
    assert r.status_code == 400
    assert r.get_json()['error'] == 'empty_message'


# ── Bearer: reply_to, edit, delete ────────────────────────────────────────────

def _room_and_msg(client, h, body='первое'):
    room_id = client.post('/api/rooms', json={'name': 'r'}, headers=h).get_json()['id']
    m = client.post('/api/messages', json={
        'room_id': room_id, 'body': body, 'client_msg_id': 'c1',
    }, headers=h).get_json()
    return room_id, m


def test_post_message_with_reply_to(client):
    h = _auth(client, 'replyer')
    room_id, first = _room_and_msg(client, h)
    r = client.post('/api/messages', json={
        'room_id': room_id, 'body': 'ответ', 'client_msg_id': 'c2',
        'reply_to': first['id'],
    }, headers=h)
    assert r.status_code == 200
    reply = r.get_json()['reply_to']
    assert reply == {'id': first['id'], 'sender': 'replyer', 'body': 'первое'}
    # История отдаёт цитату
    msgs = client.get(f'/api/rooms/{room_id}/messages', headers=h).get_json()['messages']
    assert msgs[1]['reply_to']['id'] == first['id']
    assert msgs[0]['reply_to'] is None


def test_post_message_reply_to_foreign_room_400(client):
    h = _auth(client, 'replyer2')
    room_a, first = _room_and_msg(client, h)
    room_b = client.post('/api/rooms', json={'name': 'b'}, headers=h).get_json()['id']
    r = client.post('/api/messages', json={
        'room_id': room_b, 'body': 'x', 'client_msg_id': 'c9', 'reply_to': first['id'],
    }, headers=h)
    assert r.status_code == 400 and r.get_json() == {'error': 'reply_not_found'}


def test_edit_own_message(client):
    h = _auth(client, 'editor')
    room_id, m = _room_and_msg(client, h)
    r = client.post(f"/api/messages/{m['id']}/edit", json={'body': 'новое'}, headers=h)
    assert r.status_code == 200 and r.get_json()['body'] == 'новое'
    msgs = client.get(f'/api/rooms/{room_id}/messages', headers=h).get_json()['messages']
    assert msgs[0]['body'] == 'новое'


def test_edit_foreign_message_403(client):
    h1 = _auth(client, 'author1')
    room_id, m = _room_and_msg(client, h1)
    h2 = _auth(client, 'intruder')
    # intruder даже не участник комнаты
    r = client.post(f"/api/messages/{m['id']}/edit", json={'body': 'x'}, headers=h2)
    assert r.status_code == 403


def test_edit_empty_body_400(client):
    h = _auth(client, 'editor2')
    _room_id, m = _room_and_msg(client, h)
    r = client.post(f"/api/messages/{m['id']}/edit", json={'body': '  '}, headers=h)
    assert r.status_code == 400


def test_delete_own_message(client):
    h = _auth(client, 'deleter')
    room_id, m = _room_and_msg(client, h)
    r = client.post(f"/api/messages/{m['id']}/delete", headers=h)
    assert r.status_code == 200
    msgs = client.get(f'/api/rooms/{room_id}/messages', headers=h).get_json()['messages']
    assert msgs == []
    # повторное удаление — 404
    assert client.post(f"/api/messages/{m['id']}/delete", headers=h).status_code == 404


def test_delete_foreign_message_403(client):
    h1 = _auth(client, 'author2')
    _room_id, m = _room_and_msg(client, h1)
    h2 = _auth(client, 'intruder2')
    assert client.post(f"/api/messages/{m['id']}/delete", headers=h2).status_code == 403
