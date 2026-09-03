import sqlite3
import os
from contextlib import contextmanager

DB_PATH = os.environ.get("DB_PATH", "/app/data/pcflip.db")
PHOTOS_DIR = os.environ.get("PHOTOS_DIR", os.path.join(os.path.dirname(DB_PATH), "photos"))
ATTACHMENTS_DIR = os.environ.get("ATTACHMENTS_DIR", os.path.join(os.path.dirname(DB_PATH), "attachments"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category TEXT,
    title TEXT NOT NULL,
    serial_number TEXT,
    condition TEXT,
    status TEXT NOT NULL DEFAULT 'in_stock',
    notes TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),

    seller_name TEXT,
    seller_contact TEXT,
    purchase_price REAL,
    purchase_date TEXT,
    purchase_method TEXT,
    purchase_delivery TEXT,
    purchase_shipping_company TEXT,
    purchase_tracking TEXT,

    buyer_name TEXT,
    buyer_contact TEXT,
    sale_price REAL,
    sale_date TEXT,
    sale_method TEXT,
    sale_delivery TEXT,
    sale_shipping_company TEXT,
    sale_tracking TEXT,

    batch_id TEXT
);

CREATE INDEX IF NOT EXISTS idx_items_status ON items(status);

CREATE TABLE IF NOT EXISTS item_photos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    filename TEXT NOT NULL,
    thumb_filename TEXT NOT NULL,
    sort_order INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_photos_item ON item_photos(item_id);

CREATE TABLE IF NOT EXISTS item_attachments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    filename TEXT NOT NULL,
    original_name TEXT,
    file_type TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_attachments_item ON item_attachments(item_id);
"""


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    os.makedirs(PHOTOS_DIR, exist_ok=True)
    os.makedirs(ATTACHMENTS_DIR, exist_ok=True)
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)


def _migrate(conn):
    """Add columns to a pre-existing DB that predates them. ADD COLUMN is
    metadata-only in SQLite -- it never rewrites existing rows.
    The batch_id index is created here (not in SCHEMA) because on an
    existing DB, CREATE TABLE IF NOT EXISTS is a no-op -- an index on
    batch_id in SCHEMA would run before ALTER TABLE adds the column and
    fail with "no such column". Runs unconditionally, after the column is
    guaranteed to exist either way, so fresh installs get the index too."""
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(items)")}
    if "batch_id" not in cols:
        conn.execute("ALTER TABLE items ADD COLUMN batch_id TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_items_batch ON items(batch_id)")


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()

