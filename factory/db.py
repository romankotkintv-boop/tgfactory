"""SQLite-хранилище: найденные материалы, очередь постов, история цен, журнал публикаций."""
import sqlite3
import statistics
from datetime import datetime, timedelta, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (          -- сырьё: новости, темы, офферы
    id INTEGER PRIMARY KEY,
    channel TEXT NOT NULL,
    kind TEXT NOT NULL,                     -- rss_digest | evergreen | shopee_offers
    uid TEXT NOT NULL,                      -- уникальный ключ источника (url / тема / itemId)
    title TEXT,
    url TEXT,
    payload TEXT,                           -- JSON с данными источника
    created_at TEXT NOT NULL,
    used INTEGER DEFAULT 0,
    UNIQUE(channel, uid)
);
CREATE TABLE IF NOT EXISTS posts (          -- готовые посты
    id INTEGER PRIMARY KEY,
    channel TEXT NOT NULL,
    item_id INTEGER,
    text TEXT NOT NULL,
    image_url TEXT,
    image_path TEXT,
    status TEXT NOT NULL,                   -- queued | pending_approval | approved | rejected | published | failed
    approval_msg_id INTEGER,
    created_at TEXT NOT NULL,
    published_at TEXT,
    tg_message_id INTEGER,
    error TEXT
);
CREATE TABLE IF NOT EXISTS prices (         -- история цен для проверки фейковых скидок
    item_uid TEXT NOT NULL,
    day TEXT NOT NULL,
    price REAL NOT NULL,
    PRIMARY KEY(item_uid, day)
);
CREATE TABLE IF NOT EXISTS slots_used (     -- какие слоты публикаций уже отработали
    channel TEXT NOT NULL,
    day TEXT NOT NULL,
    slot TEXT NOT NULL,
    PRIMARY KEY(channel, day, slot)
);
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class DB:
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # --- items ---
    def add_item(self, channel, kind, uid, title="", url="", payload="{}") -> bool:
        """True, если элемент новый (дедупликация по channel+uid)."""
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO items(channel,kind,uid,title,url,payload,created_at) VALUES (?,?,?,?,?,?,?)",
            (channel, kind, uid, title, url, payload, now_iso()),
        )
        self.conn.commit()
        return cur.rowcount == 1

    def next_unused_item(self, channel, kind):
        return self.conn.execute(
            "SELECT * FROM items WHERE channel=? AND kind=? AND used=0 ORDER BY id DESC LIMIT 1",
            (channel, kind),
        ).fetchone()

    def mark_item_used(self, item_id):
        self.conn.execute("UPDATE items SET used=1 WHERE id=?", (item_id,))
        self.conn.commit()

    def unmark_item(self, item_id):
        self.conn.execute("UPDATE items SET used=0 WHERE id=?", (item_id,))
        self.conn.commit()

    def recover_orphan_items(self, channel=None):
        """Возвращает в очередь сырьё, помеченное использованным, но по которому поста нет."""
        q = ("UPDATE items SET used=0 WHERE used=1 AND id NOT IN "
             "(SELECT item_id FROM posts WHERE item_id IS NOT NULL)")
        args = []
        if channel:
            q += " AND channel=?"
            args.append(channel)
        n = self.conn.execute(q, args).rowcount
        self.conn.commit()
        return n

    def count_items(self, channel, kind, used=None):
        q = "SELECT COUNT(*) FROM items WHERE channel=? AND kind=?"
        args = [channel, kind]
        if used is not None:
            q += " AND used=?"
            args.append(int(used))
        return self.conn.execute(q, args).fetchone()[0]

    # --- posts ---
    def add_post(self, channel, item_id, text, status, image_url=None, image_path=None) -> int:
        cur = self.conn.execute(
            "INSERT INTO posts(channel,item_id,text,image_url,image_path,status,created_at) VALUES (?,?,?,?,?,?,?)",
            (channel, item_id, text, image_url, image_path, status, now_iso()),
        )
        self.conn.commit()
        return cur.lastrowid

    def set_post(self, post_id, **fields):
        cols = ", ".join(f"{k}=?" for k in fields)
        self.conn.execute(f"UPDATE posts SET {cols} WHERE id=?", (*fields.values(), post_id))
        self.conn.commit()

    def get_post(self, post_id):
        return self.conn.execute("SELECT * FROM posts WHERE id=?", (post_id,)).fetchone()

    def posts(self, channel, status):
        return self.conn.execute(
            "SELECT * FROM posts WHERE channel=? AND status=? ORDER BY id", (channel, status)
        ).fetchall()

    def ready_count(self, channel):
        return self.conn.execute(
            "SELECT COUNT(*) FROM posts WHERE channel=? AND status IN ('queued','approved','pending_approval')",
            (channel,),
        ).fetchone()[0]

    # --- prices ---
    def record_price(self, item_uid, price, day=None):
        day = day or datetime.now(timezone.utc).date().isoformat()
        self.conn.execute(
            "INSERT OR REPLACE INTO prices(item_uid,day,price) VALUES (?,?,?)", (item_uid, day, price)
        )
        self.conn.commit()

    def price_median(self, item_uid, days):
        since = (datetime.now(timezone.utc).date() - timedelta(days=days)).isoformat()
        rows = self.conn.execute(
            "SELECT price FROM prices WHERE item_uid=? AND day>=?", (item_uid, since)
        ).fetchall()
        prices = [r[0] for r in rows]
        return (statistics.median(prices), len(prices)) if prices else (None, 0)

    # --- slots ---
    def slot_used(self, channel, day, slot):
        return self.conn.execute(
            "SELECT 1 FROM slots_used WHERE channel=? AND day=? AND slot=?", (channel, day, slot)
        ).fetchone() is not None

    def use_slot(self, channel, day, slot):
        self.conn.execute("INSERT OR IGNORE INTO slots_used VALUES (?,?,?)", (channel, day, slot))
        self.conn.commit()

    # --- kv ---
    def get(self, k, default=None):
        r = self.conn.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        return r[0] if r else default

    def put(self, k, v):
        self.conn.execute("INSERT OR REPLACE INTO kv VALUES (?,?)", (k, str(v)))
        self.conn.commit()
