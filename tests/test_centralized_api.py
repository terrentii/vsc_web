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
