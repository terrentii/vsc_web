"""Phase-1 integration tests for the centralized API (vsc_web)."""


def test_auth_tokens_table_and_client_msg_id_exist(app_ctx):
    from sqlalchemy import inspect as sa_inspect
    from extensions import db
    insp = sa_inspect(db.engine)
    assert 'auth_tokens' in insp.get_table_names()
    cols = {c['name'] for c in insp.get_columns('messages')}
    assert 'client_msg_id' in cols
