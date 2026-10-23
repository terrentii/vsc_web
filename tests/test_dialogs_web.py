"""Веб-страницы диалогов: настройки, приглашение, пузыри, прочтение, аватарка."""
import io

import pytest
from PIL import Image

import app as app_module
from extensions import db
from models import Room


@pytest.fixture
def web(client):
    app_module.app.config['WTF_CSRF_ENABLED'] = False
    yield client
    app_module.app.config['WTF_CSRF_ENABLED'] = True


def _register(client, user):
    client.post('/api/auth/register', json={'username': user, 'password': 'pw12'})


def _login_as(client, user):
    with client.session_transaction() as s:
        s.clear()
        s['user_type'] = 'registered'
        s['login'] = user


def _invite(web):
    _register(web, 'alice')
    _register(web, 'bobby')
    _login_as(web, 'alice')
    web.post('/dialogs/new', data={'login': 'bobby'})
    return Room.query.filter_by(creator_login='alice', dialog_proposed_by='alice').one().room_id


def test_invite_page_locks_input_and_banner(web):
    code = _invite(web)
    page = web.get(f'/room/{code}').get_data(as_text=True)
    assert 'Ждём, пока bobby примет приглашение' in page and 'disabled' in page
    _login_as(web, 'bobby')
    page = web.get(f'/room/{code}').get_data(as_text=True)
    assert 'приглашает вас в диалог' in page
    side = web.get('/').get_data(as_text=True)
    assert 'Приглашения' in side and 'зовёт в диалог' in side


def test_accept_then_dialog_page_and_settings(web):
    code = _invite(web)
    _login_as(web, 'bobby')
    web.post(f'/room/{code}/settings', data={'action': 'accept', 'back': 'room'})
    room = Room.query.filter_by(room_id=code).one()
    assert room.is_dialog
    web.post(f'/room/{code}/message', data={'text': 'привет'})
    _login_as(web, 'alice')
    page = web.get(f'/room/{code}').get_data(as_text=True)
    assert 'is-dialog' in page and 'class="msg theirs"' in page and 'id="blueprint"' in page
    assert 'Диалог' in page and '>bobby<' in page
    settings = web.get(f'/room/{code}/settings').get_data(as_text=True)
    assert 'Моё название' in settings and 'Выключить диалог' in settings and 'Добавить по логину' not in settings
    web.post(f'/room/{code}/settings', data={'action': 'title', 'title': 'Бобби'})
    assert '>Бобби<' in web.get(f'/room/{code}').get_data(as_text=True)


def test_read_and_avatar_routes(web):
    code = _invite(web)
    _login_as(web, 'bobby')
    web.post(f'/room/{code}/settings', data={'action': 'accept'})
    web.post(f'/room/{code}/message', data={'text': 'раз'})
    room = Room.query.filter_by(room_id=code).one()
    from models import Message
    mid = Message.query.filter_by(room_id=code).one().id
    _login_as(web, 'alice')
    assert web.post(f'/room/{code}/read', json={'message_id': mid}).get_json() == {'ok': True}
    png = io.BytesIO()
    Image.new('RGB', (40, 20), (1, 2, 3)).save(png, 'PNG')
    web.post(f'/room/{code}/settings', data={'action': 'avatar', 'avatar': (io.BytesIO(png.getvalue()), 'a.png')},
             content_type='multipart/form-data')
    db.session.refresh(room)
    got = web.get(f'/room/{code}/avatar')
    assert got.status_code == 200 and Image.open(io.BytesIO(got.data)).size == (256, 256)
    _login_as(web, 'bobby')
    assert web.get(f'/room/{code}/avatar').status_code == 404  # аватарка alice — только её


def test_creator_settings_and_kick(web):
    _register(web, 'alice')
    _register(web, 'carol')
    _login_as(web, 'alice')
    web.post('/room/create')
    code = Room.query.filter_by(creator_login='alice', is_dialog=False).filter(Room.personal_login.is_(None)).one().room_id
    web.post(f'/room/{code}/settings', data={'action': 'add', 'login': 'carol'})
    page = web.get(f'/room/{code}/settings').get_data(as_text=True)
    assert 'carol' in page and 'Сделать диалогом' in page
    web.post(f'/room/{code}/settings', data={'action': 'general', 'room_name': 'Штаб', 'is_open': 'false'})
    assert Room.query.filter_by(room_id=code).one().name == 'Штаб'
    web.post(f'/room/{code}/manage/kick', data={'login': 'carol'})
    assert 'carol' not in web.get(f'/room/{code}/settings').get_data(as_text=True)
