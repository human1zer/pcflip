"""Test setup: a throwaway data dir + auth config, set up before the app is
imported (config.py and database.py read their paths at import time).
Each test gets a fresh, empty database. Never touches ./data."""
import json
import os
import secrets
import tempfile

import bcrypt
import pytest

_DATA = tempfile.mkdtemp(prefix="pcflip-test-")
os.environ["DB_PATH"] = os.path.join(_DATA, "pcflip.db")
os.environ["AUTH_CONFIG_PATH"] = os.path.join(_DATA, "auth_config.json")
with open(os.environ["AUTH_CONFIG_PATH"], "w") as f:
    json.dump({"username": "test", "secret_key": secrets.token_hex(16),
               "password_hash": bcrypt.hashpw(secrets.token_bytes(16), bcrypt.gensalt(4)).decode()}, f)

from fastapi.testclient import TestClient  # noqa: E402

from app import auth, database, main  # noqa: E402


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", str(tmp_path / "pcflip.db"))
    database.init_db()
    with database.get_conn() as conn:
        yield conn


@pytest.fixture
def client(db):
    """A logged-in client; `client.csrf` is the session's CSRF token."""
    c = TestClient(main.app, follow_redirects=False)
    cookie = auth.create_session_cookie_value("test")
    c.cookies.set(auth.SESSION_COOKIE, cookie)
    c.csrf = auth._serializer.loads(cookie)["csrf"]
    return c


@pytest.fixture
def add_item(db):
    def add(title, category, price=0, status="in_stock"):
        cur = db.execute("INSERT INTO items (title, category, purchase_price, status) VALUES (?, ?, ?, ?)",
                         (title, category, price, status))
        db.commit()
        return cur.lastrowid
    return add
