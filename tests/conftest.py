"""
Test harness for vsc_web Phase-1 integration tests.

Design choice: function-scoped fixtures with a fresh temporary SQLite DB per
test. This gives full isolation at the cost of a small setup overhead per test.
For the number of tests in Phase 1 the overhead is negligible; shared-session
DBs would require careful teardown to avoid state bleed between tests.

Because app.py reads DATABASE_URL at import time (module-global app build),
we must point the env var at a temp file *before* the first import of `app`.
We do that here at module level so the import happens only once per process;
the function-scoped `app_ctx` fixture creates and drops tables around each test.
"""
import os
import sys
import tempfile
import pytest

# ── point the app at a temp sqlite file before importing app ──────────────────
# We use a named temp file so SQLAlchemy can find it by path.
_db_fd, _db_path = tempfile.mkstemp(suffix='.db', prefix='vsc_test_')
os.close(_db_fd)
os.environ.setdefault('DATABASE_URL', f'sqlite:///{_db_path}')
os.environ.setdefault('SECRET_KEY', 'test-secret-key-not-for-production')
# Disable session filesystem storage to avoid permission issues in CI
os.environ.setdefault('SESSION_TYPE', 'null')

# Ensure vsc_web root is on sys.path for `import app`, `import extensions`, etc.
_vsc_web_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _vsc_web_root not in sys.path:
    sys.path.insert(0, _vsc_web_root)

import app as _app_module          # noqa: E402 — must be after env setup
from extensions import db as _db   # noqa: E402


@pytest.fixture(scope='function')
def app_ctx():
    """
    Push an app context, create all tables (including the new AuthToken model
    and client_msg_id column added by db.create_all), run the same idempotent
    migration SQL so the unique index exists, yield, then tear down.
    """
    from sqlalchemy import text
    with _app_module.app.app_context():
        _db.create_all()
        # Run the same idempotent migration SQL as app.py startup block,
        # so tests see the index even when running against a fresh schema.
        with _db.engine.connect() as conn:
            conn.execute(text(
                'CREATE UNIQUE INDEX IF NOT EXISTS uq_msg_client_id '
                'ON messages(room_id, client_msg_id)'
            ))
            conn.commit()
        yield
        _db.session.remove()
        _db.drop_all()


@pytest.fixture(scope='function')
def client(app_ctx):
    """Flask test client, usable within an active app context."""
    with _app_module.app.test_client() as c:
        yield c
