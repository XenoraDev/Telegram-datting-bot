import asyncio

import aiosqlite

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT,
    name TEXT,
    age INTEGER,
    gender TEXT,            -- 'm' | 'f' | 'o'
    looking_for TEXT,       -- 'm' | 'f' | 'any'
    city TEXT,
    bio TEXT,
    photo TEXT,
    partner_id INTEGER,     -- current anonymous chat partner
    banned INTEGER DEFAULT 0,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS queue (
    user_id INTEGER PRIMARY KEY,
    joined_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS swipes (
    from_id INTEGER,
    to_id INTEGER,
    liked INTEGER,
    PRIMARY KEY (from_id, to_id)
);
CREATE TABLE IF NOT EXISTS reports (
    from_id INTEGER,
    to_id INTEGER,
    PRIMARY KEY (from_id, to_id)
);
CREATE TABLE IF NOT EXISTS blocks (
    from_id INTEGER,
    to_id INTEGER,
    PRIMARY KEY (from_id, to_id)
);
"""

# Serialises matchmaking so two users can never be paired with the same partner.
match_lock = asyncio.Lock()
_db: aiosqlite.Connection | None = None


async def init() -> None:
    global _db
    _db = await aiosqlite.connect(config.DB_PATH)
    _db.row_factory = aiosqlite.Row
    await _db.executescript(SCHEMA)
    await _db.commit()


async def close() -> None:
    if _db:
        await _db.close()


async def fetchone(sql: str, *args):
    async with _db.execute(sql, args) as cur:
        return await cur.fetchone()


async def fetchall(sql: str, *args):
    async with _db.execute(sql, args) as cur:
        return await cur.fetchall()


async def execute(sql: str, *args) -> None:
    await _db.execute(sql, args)
    await _db.commit()


# ---------- users ----------
async def get_user(uid: int):
    return await fetchone("SELECT * FROM users WHERE id=?", uid)


async def has_profile(uid: int) -> bool:
    u = await get_user(uid)
    return bool(u and u["name"] and u["photo"] and not u["banned"])


async def save_profile(uid, username, name, age, gender, looking_for, city, bio, photo):
    await execute(
        """INSERT INTO users (id, username, name, age, gender, looking_for, city, bio, photo)
           VALUES (?,?,?,?,?,?,?,?,?)
           ON CONFLICT(id) DO UPDATE SET username=excluded.username, name=excluded.name,
             age=excluded.age, gender=excluded.gender, looking_for=excluded.looking_for,
             city=excluded.city, bio=excluded.bio, photo=excluded.photo""",
        uid, username, name, age, gender, looking_for, city, bio, photo,
    )


async def set_username(uid: int, username: str | None) -> None:
    await execute("UPDATE users SET username=? WHERE id=?", username, uid)


async def set_banned(uid: int, banned: bool) -> None:
    await execute("UPDATE users SET banned=? WHERE id=?", int(banned), uid)
    if banned:
        await leave_queue(uid)


# ---------- compatibility ----------
def _compatible(a, b) -> bool:
    """a wants b's gender and b wants a's gender."""
    return (a["looking_for"] in ("any", b["gender"])) and (b["looking_for"] in ("any", a["gender"]))


async def _blocked(a: int, b: int) -> bool:
    row = await fetchone(
        "SELECT 1 FROM blocks WHERE (from_id=? AND to_id=?) OR (from_id=? AND to_id=?)", a, b, b, a
    )
    return row is not None


# ---------- anonymous random chat ----------
async def join_or_match(uid: int):
    """Return partner row if matched instantly, else put user in queue and return None."""
    async with match_lock:
        me = await get_user(uid)
        rows = await fetchall(
            """SELECT u.* FROM queue q JOIN users u ON u.id=q.user_id
               WHERE q.user_id != ? AND u.banned=0 AND u.partner_id IS NULL
               ORDER BY q.joined_at""",
            uid,
        )
        for other in rows:
            if _compatible(me, other) and not await _blocked(uid, other["id"]):
                await _db.execute("DELETE FROM queue WHERE user_id IN (?,?)", (uid, other["id"]))
                await _db.execute("UPDATE users SET partner_id=? WHERE id=?", (other["id"], uid))
                await _db.execute("UPDATE users SET partner_id=? WHERE id=?", (uid, other["id"]))
                await _db.commit()
                return other
        await _db.execute("INSERT OR IGNORE INTO queue (user_id) VALUES (?)", (uid,))
        await _db.commit()
        return None


async def leave_queue(uid: int) -> bool:
    async with _db.execute("DELETE FROM queue WHERE user_id=?", (uid,)) as cur:
        n = cur.rowcount
    await _db.commit()
    return n > 0


async def end_chat(uid: int):
    """Disconnect uid from partner. Returns partner id or None."""
    async with match_lock:
        me = await get_user(uid)
        if not me or me["partner_id"] is None:
            return None
        pid = me["partner_id"]
        await _db.execute("UPDATE users SET partner_id=NULL WHERE id IN (?,?)", (uid, pid))
        await _db.commit()
        return pid


# ---------- profile browsing / matches ----------
async def next_profile(uid: int):
    me = await get_user(uid)
    rows = await fetchall(
        """SELECT * FROM users u
           WHERE u.id != ? AND u.banned=0 AND u.photo IS NOT NULL
             AND u.id NOT IN (SELECT to_id FROM swipes WHERE from_id=?)
             AND u.id NOT IN (SELECT to_id FROM blocks WHERE from_id=?)
             AND u.id NOT IN (SELECT from_id FROM blocks WHERE to_id=?)
           ORDER BY RANDOM() LIMIT 50""",
        uid, uid, uid, uid,
    )
    for r in rows:
        if _compatible(me, r):
            return r
    return None


async def swipe(from_id: int, to_id: int, liked: bool) -> bool:
    """Record swipe. Returns True if it created a mutual match."""
    await execute("INSERT OR REPLACE INTO swipes VALUES (?,?,?)", from_id, to_id, int(liked))
    if not liked:
        return False
    return await fetchone("SELECT 1 FROM swipes WHERE from_id=? AND to_id=? AND liked=1", to_id, from_id) is not None


async def my_matches(uid: int):
    return await fetchall(
        """SELECT u.* FROM swipes a JOIN swipes b ON a.to_id=b.from_id AND a.from_id=b.to_id
           JOIN users u ON u.id=a.to_id
           WHERE a.from_id=? AND a.liked=1 AND b.liked=1 AND u.banned=0""",
        uid,
    )


# ---------- safety ----------
async def block(from_id: int, to_id: int) -> None:
    await execute("INSERT OR IGNORE INTO blocks VALUES (?,?)", from_id, to_id)


async def report(from_id: int, to_id: int) -> int:
    """Returns total distinct reports against to_id."""
    await execute("INSERT OR IGNORE INTO reports VALUES (?,?)", from_id, to_id)
    row = await fetchone("SELECT COUNT(*) c FROM reports WHERE to_id=?", to_id)
    return row["c"]


async def stats():
    r = await fetchone(
        "SELECT COUNT(*) users, (SELECT COUNT(*) FROM queue) waiting,"
        " (SELECT COUNT(*) FROM users WHERE partner_id IS NOT NULL) chatting FROM users"
    )
    return dict(r)
