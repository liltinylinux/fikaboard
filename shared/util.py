from __future__ import annotations
import os, json
from contextlib import asynccontextmanager
import aiosqlite

@asynccontextmanager
async def open_db(db_path: str):
    db = await aiosqlite.connect(db_path)
    db.row_factory = aiosqlite.Row
    try:
        yield db
    finally:
        await db.close()

def env_int(name: str, default: int | None = None) -> int | None:
    v = os.getenv(name)
    if v is None or v == "":
        return default
    try:
        return int(v)
    except ValueError:
        return default

def jdump(d: dict) -> str:
    return json.dumps(d, separators=(",", ":"))
