from __future__ import annotations
from shared.util import open_db

async def top_players(db_path: str, limit: int = 10):
    async with open_db(db_path) as db:
        cur = await db.execute("""
            SELECT p.game_name, s.level, s.xp, s.kills, s.deaths, s.extracts, s.survivals, s.dogtags
            FROM stats s JOIN players p ON s.player_id = p.id
            ORDER BY s.xp DESC LIMIT ?""", (limit,))
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

async def player_card(db_path: str, name: str):
    async with open_db(db_path) as db:
        cur = await db.execute("""
            SELECT p.game_name, s.* FROM stats s
            JOIN players p ON p.id = s.player_id
            WHERE p.game_name = ?""", (name,))
        r = await cur.fetchone()
        return dict(r) if r else None

async def active_quests(db_path: str):
    async with open_db(db_path) as db:
        cur = await db.execute("SELECT * FROM quests WHERE active=1 ORDER BY id")
        return [dict(r) for r in await cur.fetchall()]

async def quest_progress_for_guild(db_path: str):
    async with open_db(db_path) as db:
        cur = await db.execute("""
            SELECT q.title, p.game_name, qp.progress, q.target
            FROM quest_progress qp
            JOIN quests q ON q.id = qp.quest_id
            JOIN players p ON p.id = qp.player_id
            WHERE q.active=1
            ORDER BY q.id, qp.progress DESC""")
        return [dict(r) for r in await cur.fetchall()]

async def get_meta(db_path: str, key: str):
    async with open_db(db_path) as db:
        cur = await db.execute("SELECT value FROM meta WHERE key=?", (key,))
        r = await cur.fetchone()
        return r[0] if r else None

async def set_meta(db_path: str, key: str, value: str):
    async with open_db(db_path) as db:
        await db.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
        await db.commit()
