#!/usr/bin/env bash
set -euo pipefail

ROOT=/opt/fika_xp
PY=python3

echo "[+] Creating layout at $ROOT"
sudo mkdir -p $ROOT/{api,bot/cogs,shared,worker,deploy/systemd}
sudo chown -R "$USER:$USER" "$ROOT"

# --- package markers ---
touch $ROOT/{bot,worker,shared}/__init__.py
touch $ROOT/bot/cogs/__init__.py

# --- requirements ---
cat > $ROOT/requirements.txt <<'EOF'
discord.py>=2.4.0
aiosqlite>=0.20.0
pydantic>=2.8
python-dotenv>=1.0
python-dateutil>=2.9
PyYAML>=6.0.2
fastapi>=0.115
uvicorn[standard]>=0.30
httpx>=0.27
EOF

# --- env example ---
cat > $ROOT/.env.example <<'EOF'
DISCORD_TOKEN=YOUR_DISCORD_BOT_TOKEN
DB_PATH=/opt/fika_xp/fika.db
LOG_FILE=/opt/spt/user/logs/server.log
TIMEZONE=America/New_York
GUILD_ID=000000000000000000
LEADERBOARD_CHANNEL_ID=000000000000000000

# OAuth (website quests tab)
DISCORD_CLIENT_ID=YOUR_CLIENT_ID
DISCORD_CLIENT_SECRET=YOUR_CLIENT_SECRET
DISCORD_REDIRECT_URI=https://your.site/api/callback
SITE_ORIGIN=https://your.site
SESSION_SECRET=change-me
EOF

# --- shared ---
cat > $ROOT/shared/schema.sql <<'EOF'
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS players (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  game_name TEXT UNIQUE,
  discord_id TEXT,
  first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  last_seen  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS stats (
  player_id INTEGER PRIMARY KEY,
  xp INTEGER NOT NULL DEFAULT 0,
  level INTEGER NOT NULL DEFAULT 1,
  kills INTEGER NOT NULL DEFAULT 0,
  deaths INTEGER NOT NULL DEFAULT 0,
  extracts INTEGER NOT NULL DEFAULT 0,
  survivals INTEGER NOT NULL DEFAULT 0,
  dogtags INTEGER NOT NULL DEFAULT 0,
  playtime_seconds INTEGER NOT NULL DEFAULT 0,
  FOREIGN KEY(player_id) REFERENCES players(id)
);

CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TIMESTAMP NOT NULL,
  type TEXT NOT NULL,
  game_name TEXT NOT NULL,
  data TEXT
);

CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS idx_events_type ON events(type);

CREATE TABLE IF NOT EXISTS quests (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  key TEXT UNIQUE NOT NULL,
  title TEXT NOT NULL,
  type TEXT NOT NULL,
  event_type TEXT NOT NULL,
  target INTEGER NOT NULL,
  start_ts TIMESTAMP NOT NULL,
  end_ts   TIMESTAMP NOT NULL,
  active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS quest_progress (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  quest_id INTEGER NOT NULL,
  player_id INTEGER NOT NULL,
  progress INTEGER NOT NULL DEFAULT 0,
  completed_ts TIMESTAMP,
  UNIQUE(quest_id, player_id),
  FOREIGN KEY(quest_id) REFERENCES quests(id),
  FOREIGN KEY(player_id) REFERENCES players(id)
);

CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT
);
EOF

cat > $ROOT/shared/leveling.py <<'EOF'
from __future__ import annotations

def xp_for_level(level: int) -> int:
    if level <= 1:
        return 0
    return 100 * (level - 1) * (level - 1) + 100

def level_from_xp(xp: int) -> int:
    lvl = 1
    while xp >= xp_for_level(lvl + 1):
        lvl += 1
    return lvl

DEFAULT_EVENT_XP = {
    "KILL": 100,
    "HEADSHOT": 25,
    "SURVIVE": 150,
    "EXTRACT": 75,
    "DOGTAG": 30,
    "DEATH": 0,
}

def xp_for_event(event_type: str, data: dict | None = None) -> int:
    return DEFAULT_EVENT_XP.get(event_type.upper(), 0)
EOF

cat > $ROOT/shared/util.py <<'EOF'
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
EOF

# --- worker ---
cat > $ROOT/worker/config.py <<'EOF'
from __future__ import annotations
import os
from dataclasses import dataclass
from dotenv import load_dotenv
load_dotenv()

@dataclass
class Config:
    db_path: str = os.getenv("DB_PATH", "./fika.db")
    log_file: str = os.getenv("LOG_FILE", "./server.log")
    tz: str = os.getenv("TIMEZONE", "America/New_York")

CFG = Config()
EOF

cat > $ROOT/worker/rules.yaml <<'EOF'
xp:
  KILL: 100
  HEADSHOT: 25
  SURVIVE: 150
  EXTRACT: 75
  DOGTAG: 30
  DEATH: 0
patterns:
  KILL: "^(?P<ts>[^ ]+) .* KILL: (?P<killer>[^ ]+) -> (?P<victim>[^ ]+)(?: \\(HEADSHOT\\))?"
  DEATH: "^(?P<ts>[^ ]+) .* DEATH: (?P<victim>[^ ]+) by (?P<killer>[^ ]+)"
  EXTRACT: "^(?P<ts>[^ ]+) .* EXTRACT: (?P<name>[^ ]+)"
  SURVIVE: "^(?P<ts>[^ ]+) .* SURVIVE: (?P<name>[^ ]+)"
  DOGTAG: "^(?P<ts>[^ ]+) .* DOGTAG: (?P<name>[^ ]+) picked up"
EOF

cat > $ROOT/worker/parsers.py <<'EOF'
from __future__ import annotations
import re, yaml
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

@dataclass
class Event:
    ts: datetime
    type: str
    game_name: str
    data: dict

class LineParsers:
    def __init__(self, rules_file: str):
        with open(rules_file, "r", encoding="utf-8") as f:
            self.cfg = yaml.safe_load(f)
        self.patterns = {k.upper(): re.compile(v) for k, v in self.cfg.get("patterns", {}).items()}

    def parse(self, line: str) -> Optional[Event]:
        for etype, rx in self.patterns.items():
            m = rx.search(line)
            if not m:
                continue
            gd = m.groupdict()
            ts_raw = gd.get("ts")
            ts = None
            for fmt in ("%Y-%m-%dT%H:%M:%S", "%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
                try:
                    ts = datetime.strptime(ts_raw, fmt)
                    break
                except Exception:
                    pass
            if ts is None:
                ts = datetime.utcnow()

            if etype == "KILL":
                return Event(ts, "KILL", gd.get("killer") or "", {"victim": gd.get("victim"), "headshot": "HEADSHOT" in line})
            if etype == "DEATH":
                return Event(ts, "DEATH", gd.get("victim") or "", {"killer": gd.get("killer")})
            if etype in ("EXTRACT", "SURVIVE", "DOGTAG"):
                name = gd.get("name") or ""
                return Event(ts, etype, name, {})
        return None
EOF

cat > $ROOT/worker/log_ingestor.py <<'EOF'
from __future__ import annotations
import asyncio, json, sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from worker.config import CFG
from worker.parsers import LineParsers, Event
from shared.leveling import xp_for_event, level_from_xp

SCHEMA = Path(__file__).resolve().parents[1] / "shared" / "schema.sql"
RULES  = Path(__file__).resolve().parent / "rules.yaml"

class Store:
    def __init__(self, db_path: str):
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        with open(SCHEMA, "r", encoding="utf-8") as f:
            self.conn.executescript(f.read())
        self.conn.commit()

    def upsert_player(self, game_name: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO players(game_name) VALUES(?) "
            "ON CONFLICT(game_name) DO UPDATE SET last_seen=CURRENT_TIMESTAMP "
            "RETURNING id",
            (game_name,))
        return int(cur.fetchone()[0])

    def ensure_stats(self, pid: int):
        self.conn.execute("INSERT OR IGNORE INTO stats(player_id) VALUES(?)", (pid,))

    def add_event(self, ev: Event):
        self.conn.execute(
            "INSERT INTO events(ts, type, game_name, data) VALUES(?,?,?,?)",
            (ev.ts.isoformat(), ev.type, ev.game_name, json.dumps(ev.data)))

    def award_xp_and_stats(self, pid: int, ev: Event):
        xp = xp_for_event(ev.type, ev.data)
        field_map = {"KILL":"kills","DEATH":"deaths","EXTRACT":"extracts","SURVIVE":"survivals","DOGTAG":"dogtags"}
        field = field_map.get(ev.type)
        if field:
            self.conn.execute(f"UPDATE stats SET {field} = {field} + 1 WHERE player_id=?", (pid,))
        if xp:
            # gate Discord leveling until player opts in
            cur = self.conn.execute("SELECT eligible FROM players WHERE id=?", (pid,))
            row = cur.fetchone()
            eligible = int(row["eligible"]) if row and "eligible" in row.keys() else 0
            if eligible:
                self.conn.execute("UPDATE stats SET xp = xp + ? WHERE player_id=?", (xp, pid))
                r2 = self.conn.execute("SELECT xp, level FROM stats WHERE player_id=?", (pid,)).fetchone()
                new_level = level_from_xp(int(r2["xp"]))
                if new_level > int(r2["level"]):
                    self.conn.execute("UPDATE stats SET level=? WHERE player_id=?", (new_level, pid))
        # quests: only increment accepted quests (row exists after acceptance)
        self.conn.execute(
            """
            UPDATE quest_progress
               SET progress = progress + 1
             WHERE player_id=?
               AND quest_id IN (SELECT id FROM quests WHERE active=1 AND event_type=?)
            """,
            (pid, ev.type),
        )
        # completion timestamp
        self.conn.execute(
            """
            UPDATE quest_progress
               SET completed_ts=CURRENT_TIMESTAMP
             WHERE player_id=?
               AND quest_id IN (SELECT id FROM quests WHERE active=1)
               AND progress >= (SELECT target FROM quests WHERE quests.id = quest_progress.quest_id)
               AND completed_ts IS NULL
            """,
            (pid,),
        )

    def commit(self):
        self.conn.commit()

async def tail_file(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as f:
        f.seek(0, 2)
        while True:
            line = f.readline()
            if not line:
                await asyncio.sleep(0.25)
                continue
            yield line.rstrip("\n")

async def rotate_weekly_quests(store: Store):
    now = datetime.utcnow()
    store.conn.execute("UPDATE quests SET active=0 WHERE end_ts <= ?", (now.isoformat(),))
    cur = store.conn.execute("SELECT COUNT(*) AS c FROM quests WHERE active=1")
    if cur.fetchone()["c"] == 0:
        start = now; end = now + timedelta(days=7)
        store.conn.execute(
            "INSERT INTO quests(key, title, type, event_type, target, start_ts, end_ts, active) VALUES(?,?,?,?,?,?,?,1)",
            ("dogtags_week", "Collect 5 dog tags", "count_event", "DOGTAG", 5, start.isoformat(), end.isoformat()))
        store.conn.execute(
            "INSERT INTO quests(key, title, type, event_type, target, start_ts, end_ts, active) VALUES(?,?,?,?,?,?,?,1)",
            ("survive_week", "Survive 5 raids", "count_event", "SURVIVE", 5, start.isoformat(), end.isoformat()))
    store.commit()

async def main():
    store = Store(CFG.db_path)
    await rotate_weekly_quests(store)
    parsers = LineParsers(str(RULES))
    log_path = Path(CFG.log_file)

    async for line in tail_file(log_path):
        ev = parsers.parse(line)
        if not ev:
            continue
        pid = store.upsert_player(ev.game_name)
        store.ensure_stats(pid)
        store.add_event(ev)
        store.award_xp_and_stats(pid, ev)
        store.commit()

if __name__ == "__main__":
    asyncio.run(main())
EOF

# --- API ---
cat > $ROOT/api/main.py <<'EOF'
from __future__ import annotations
import os, json, sqlite3, secrets, base64, hmac, hashlib
from urllib.parse import urlencode
import httpx
from fastapi import FastAPI, Request, Response, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse, JSONResponse

DB_PATH = os.getenv("DB_PATH", "./fika.db")
CLIENT_ID = os.getenv("DISCORD_CLIENT_ID")
CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET")
REDIRECT_URI = os.getenv("DISCORD_REDIRECT_URI", "https://your.site/api/callback")
SITE_ORIGIN = os.getenv("SITE_ORIGIN", "https://your.site")
SECRET = os.getenv("SESSION_SECRET", secrets.token_urlsafe(32))

OAUTH_AUTHORIZE = "https://discord.com/api/oauth2/authorize"
OAUTH_TOKEN = "https://discord.com/api/oauth2/token"
ME_ENDPOINT = "https://discord.com/api/users/@me"

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=[SITE_ORIGIN], allow_methods=["*"], allow_headers=["*"], allow_credentials=True)

def sign(data: str) -> str:
    return base64.urlsafe_b64encode(hmac.new(SECRET.encode(), data.encode(), hashlib.sha256).digest()).decode().rstrip("=")

def set_session(resp: Response, payload: dict):
    raw = json.dumps(payload, separators=(",", ":"))
    cookie = base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")
    resp.set_cookie("sess", cookie+"."+sign(cookie), httponly=True, samesite="Lax", secure=True, max_age=60*60*24*30)

def get_session(req: Request) -> dict | None:
    c = req.cookies.get("sess")
    if not c or "." not in c: return None
    cookie,sig = c.rsplit(".",1)
    if sign(cookie) != sig: return None
    raw = base64.urlsafe_b64decode(cookie + "=")
    return json.loads(raw)

conn = sqlite3.connect(DB_PATH, check_same_thread=False)
conn.row_factory = sqlite3.Row

@app.get("/api/login")
async def login():
    q = {
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": "identify",
        "prompt": "consent",
    }
    return RedirectResponse(OAUTH_AUTHORIZE + "?" + urlencode(q))

@app.get("/api/callback")
async def callback(request: Request, code: str):
    async with httpx.AsyncClient() as http:
        data = {
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT_URI,
        }
        tok = (await http.post(OAUTH_TOKEN, data=data, headers={"Content-Type":"application/x-www-form-urlencoded"})).json()
        if "access_token" not in tok:
            raise HTTPException(400, "oauth exchange failed")
        me = (await http.get(ME_ENDPOINT, headers={"Authorization": f"Bearer {tok['access_token']}"})).json()
    resp = RedirectResponse(url=SITE_ORIGIN)
    set_session(resp, {"discord_id": me["id"], "username": me["username"], "avatar": me.get("avatar")})
    return resp

@app.get("/api/logout")
async def logout():
    resp = RedirectResponse(url=SITE_ORIGIN)
    resp.delete_cookie("sess")
    return resp

@app.get("/api/me")
async def me(request: Request):
    sess = get_session(request)
    if not sess:
        return JSONResponse({"auth": False})
    did = sess["discord_id"]
    cur = conn.execute("SELECT p.game_name, s.level, s.xp FROM players p LEFT JOIN stats s ON s.player_id=p.id WHERE p.discord_id=?", (did,))
    r = cur.fetchone()
    return {"auth": True, **sess, "linked": bool(r), "game_name": r["game_name"] if r else None, "level": r["level"] if r else 1, "xp": r["xp"] if r else 0}

@app.post("/api/link_name")
async def link_name(request: Request):
    sess = get_session(request)
    if not sess: raise HTTPException(401, "not logged in")
    body = await request.json()
    name = (body.get("game_name") or "").strip()
    if not name: raise HTTPException(400, "game_name required")
    conn.execute("INSERT OR IGNORE INTO players(game_name) VALUES(?)", (name,))
    conn.execute("UPDATE players SET discord_id=? WHERE game_name=?", (sess["discord_id"], name))
    pid = conn.execute("SELECT id FROM players WHERE game_name=?", (name,)).fetchone()[0]
    conn.execute("INSERT OR IGNORE INTO stats(player_id) VALUES(?)", (pid,))
    conn.commit()
    return {"ok": True}

@app.get("/api/quests")
async def quests(request: Request):
    sess = get_session(request)
    did = sess.get("discord_id") if sess else None
    pid = None
    if did:
        r = conn.execute("SELECT id FROM players WHERE discord_id=?", (did,)).fetchone()
        pid = r[0] if r else None
    q = conn.execute("SELECT id, title, event_type, target, start_ts, end_ts FROM quests WHERE active=1 ORDER BY id").fetchall()
    out = []
    for row in q:
        accepted = False; progress = 0
        if pid:
            pr = conn.execute("SELECT progress FROM quest_progress WHERE quest_id=? AND player_id=?", (row["id"], pid)).fetchone()
            if pr:
                accepted = True; progress = pr[0]
        out.append({"id": row["id"], "title": row["title"], "event_type": row["event_type"], "target": row["target"], "start": row["start_ts"], "end": row["end_ts"], "accepted": accepted, "progress": progress})
    return {"quests": out}

@app.post("/api/quests/accept")
async def accept(request: Request):
    sess = get_session(request)
    if not sess: raise HTTPException(401, "not logged in")
    body = await request.json()
    qid = int(body.get("quest_id"))
    did = sess["discord_id"]
    r = conn.execute("SELECT id FROM players WHERE discord_id=?", (did,)).fetchone()
    if not r: raise HTTPException(400, "link your game name first")
    pid = r[0]
    conn.execute("UPDATE players SET eligible=1 WHERE id=?", (pid,))
    conn.execute("INSERT OR IGNORE INTO quest_progress(quest_id, player_id, progress) VALUES(?,?,0)", (qid, pid))
    conn.commit()
    return {"ok": True}
EOF

# --- bot ---
cat > $ROOT/bot/db.py <<'EOF'
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
EOF

cat > $ROOT/bot/cogs/levels.py <<'EOF'
from __future__ import annotations
import os, discord
from discord import app_commands
from discord.ext import commands, tasks
from shared.util import env_int
from bot.db import top_players, player_card, get_meta, set_meta

DB_PATH = os.getenv("DB_PATH", "./fika.db")
LEADERBOARD_CHANNEL_ID = env_int("LEADERBOARD_CHANNEL_ID")

class Levels(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.update_leaderboard.start()

    def cog_unload(self):
        self.update_leaderboard.cancel()

    @app_commands.command(name="level", description="Show a player's level card")
    @app_commands.describe(game_name="SPT/Fika in-game name")
    async def level(self, interaction: discord.Interaction, game_name: str):
        card = await player_card(DB_PATH, game_name)
        if not card:
            await interaction.response.send_message(f"No stats for **{game_name}** yet.")
            return
        emb = discord.Embed(title=f"{card['game_name']} — Lv {card['level']} ({card['xp']} XP)")
        emb.add_field(name="Kills", value=str(card['kills']))
        emb.add_field(name="Deaths", value=str(card['deaths']))
        emb.add_field(name="Extracts", value=str(card['extracts']))
        emb.add_field(name="Survivals", value=str(card['survivals']))
        emb.add_field(name="Dogtags", value=str(card['dogtags']))
        await interaction.response.send_message(embed=emb)

    @app_commands.command(name="leaderboard", description="Show top XP players")
    async def leaderboard(self, interaction: discord.Interaction):
        items = await top_players(DB_PATH, 10)
        emb = discord.Embed(title="FIKA — XP Leaderboard (Top 10)")
        lines = []
        for i, r in enumerate(items, start=1):
            lines.append(f"**{i}. {r['game_name']}** — Lv {r['level']} • {r['xp']} XP • {r['kills']}K/{r['deaths']}D")
        emb.description = "\n".join(lines) if lines else "(No data yet)"
        await interaction.response.send_message(embed=emb)

    async def get_or_create_message(self, channel: discord.TextChannel) -> discord.Message:
        msg_id = await get_meta(DB_PATH, "leaderboard_msg_id")
        if msg_id:
            try:
                return await channel.fetch_message(int(msg_id))
            except Exception:
                pass
        msg = await channel.send("(initializing leaderboard…)")
        await set_meta(DB_PATH, "leaderboard_msg_id", str(msg.id))
        return msg

    @tasks.loop(minutes=5)
    async def update_leaderboard(self):
        if LEADERBOARD_CHANNEL_ID is None:
            return
        channel = self.bot.get_channel(LEADERBOARD_CHANNEL_ID)
        if channel is None:
            return
        msg = await self.get_or_create_message(channel)
        items = await top_players(DB_PATH, 10)
        emb = discord.Embed(title="FIKA — XP Leaderboard (Top 10)")
        lines = []
        for i, r in enumerate(items, start=1):
            lines.append(f"**{i}. {r['game_name']}** — Lv {r['level']} • {r['xp']} XP • {r['kills']}K/{r['deaths']}D")
        emb.description = "\n".join(lines) if lines else "(No data yet)"
        try:
            await msg.edit(embed=emb)
        except Exception:
            new_msg = await channel.send(embed=emb)
            await set_meta(DB_PATH, "leaderboard_msg_id", str(new_msg.id))

    @update_leaderboard.before_loop
    async def before_loop(self):
        await self.bot.wait_until_ready()

async def setup(bot: commands.Bot):
    await bot.add_cog(Levels(bot))
EOF

cat > $ROOT/bot/cogs/quests.py <<'EOF'
from __future__ import annotations
import os, discord
from discord import app_commands
from discord.ext import commands
from bot.db import active_quests, quest_progress_for_guild

DB_PATH = os.getenv("DB_PATH", "./fika.db")

class Quests(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="quests", description="Show active weekly quests and progress")
    async def quests(self, interaction: discord.Interaction):
        quests = await active_quests(DB_PATH)
        prog = await quest_progress_for_guild(DB_PATH)
        if not quests:
            await interaction.response.send_message("No active quests.")
            return
        emb = discord.Embed(title="Weekly Quests")
        for q in quests:
            lines = [f"**{q['title']}** — target: {q['target']} ({q['start_ts']} → {q['end_ts']})"]
            for row in [r for r in prog if r['title'] == q['title']][:10]:
                lines.append(f"• {row['game_name']}: {row['progress']}/{q['target']}")
            emb.add_field(name=q['title'], value="\n".join(lines), inline=False)
        await interaction.response.send_message(embed=emb)

async def setup(bot: commands.Bot):
    await bot.add_cog(Quests(bot))
EOF

cat > $ROOT/bot/cogs/admin.py <<'EOF'
from __future__ import annotations
import os, discord
from discord import app_commands
from discord.ext import commands
from shared.util import open_db

DB_PATH = os.getenv("DB_PATH", "./fika.db")
ADMIN_ROLE = os.getenv("ADMIN_ROLE", "Admin")

class Admin(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    def _is_admin(self, inter: discord.Interaction) -> bool:
        if inter.user.guild_permissions.administrator:
            return True
        if ADMIN_ROLE and isinstance(inter.user, discord.Member):
            return any(r.name == ADMIN_ROLE for r in inter.user.roles)
        return False

    @app_commands.command(name="quest_rotate_now", description="Force-rotate weekly quests NOW")
    async def quest_rotate_now(self, interaction: discord.Interaction):
        if not self._is_admin(interaction):
            await interaction.response.send_message("Nope.", ephemeral=True)
            return
        async with open_db(DB_PATH) as db:
            await db.execute("UPDATE quests SET active=0 WHERE active=1")
            await db.execute(
                "INSERT INTO quests(key, title, type, event_type, target, start_ts, end_ts, active) VALUES(?,?,?,?,?,datetime('now'),datetime('now','+7 days'),1)",
                ("dogtags_week", "Collect 5 dog tags", "count_event", "DOGTAG", 5),
            )
            await db.execute(
                "INSERT INTO quests(key, title, type, event_type, target, start_ts, end_ts, active) VALUES(?,?,?,?,?,datetime('now'),datetime('now','+7 days'),1)",
                ("survive_week", "Survive 5 raids", "count_event", "SURVIVE", 5),
            )
            await db.commit()
        await interaction.response.send_message("Rotated.")

async def setup(bot: commands.Bot):
    await bot.add_cog(Admin(bot))
EOF

cat > $ROOT/bot/main_example.py <<'EOF'
from __future__ import annotations
import os, asyncio, discord
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

intents = discord.Intents.default()
intents.message_content = False
bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
    gid = os.getenv("GUILD_ID")
    if gid:
        guild_obj = discord.Object(id=int(gid))
        await bot.tree.sync(guild=guild_obj)
    else:
        await bot.tree.sync()

async def main():
    async with bot:
        await bot.load_extension("bot.cogs.levels")
        await bot.load_extension("bot.cogs.quests")
        await bot.load_extension("bot.cogs.admin")
        await bot.start(TOKEN)

if __name__ == "__main__":
    asyncio.run(main())
EOF

# --- systemd (worker, bot, api) ---
cat > $ROOT/deploy/systemd/fika-xp-worker.service <<'EOF'
[Unit]
Description=Fika XP Worker (log ingestor)
After=network.target

[Service]
Type=simple
Environment=DB_PATH=/opt/fika_xp/fika.db
Environment=LOG_FILE=/opt/spt/user/logs/server.log
Environment=TIMEZONE=America/New_York
WorkingDirectory=/opt/fika_xp
ExecStart=/usr/bin/python3 -m worker.log_ingestor
Restart=always
RestartSec=3
User=ubuntu

[Install]
WantedBy=multi-user.target
EOF

cat > $ROOT/deploy/systemd/fika-xp-bot.service <<'EOF'
[Unit]
Description=Fika XP Discord Bot
After=network.target

[Service]
Type=simple
Environment=DISCORD_TOKEN=REDACTED
Environment=DB_PATH=/opt/fika_xp/fika.db
Environment=LEADERBOARD_CHANNEL_ID=000000000000000000
Environment=TIMEZONE=America/New_York
WorkingDirectory=/opt/fika_xp
ExecStart=/usr/bin/python3 -m bot.main_example
Restart=always
RestartSec=3
User=ubuntu

[Install]
WantedBy=multi-user.target
EOF

cat > $ROOT/deploy/systemd/fika-xp-api.service <<'EOF'
[Unit]
Description=Fika XP API (Discord OAuth + Quests)
After=network.target

[Service]
Type=simple
Environment=DB_PATH=/opt/fika_xp/fika.db
Environment=DISCORD_CLIENT_ID=xxxxx
Environment=DISCORD_CLIENT_SECRET=xxxxx
Environment=DISCORD_REDIRECT_URI=https://your.site/api/callback
Environment=SITE_ORIGIN=https://your.site
Environment=SESSION_SECRET=change-me
WorkingDirectory=/opt/fika_xp
ExecStart=/usr/bin/python3 -m uvicorn api.main:app --host 127.0.0.1 --port 8088
Restart=always
User=ubuntu

[Install]
WantedBy=multi-user.target
EOF

# --- README (short) ---
cat > $ROOT/README.md <<'EOF'
1) python3 -m venv venv && source venv/bin/activate && pip install -r requirements.txt
2) sqlite3 /opt/fika_xp/fika.db < shared/schema.sql
3) sqlite3 /opt/fika_xp/fika.db "SELECT 1 FROM pragma_table_info('players') WHERE name='eligible';" | grep -q 1 || sqlite3 /opt/fika_xp/fika.db "ALTER TABLE players ADD COLUMN eligible INTEGER NOT NULL DEFAULT 0;"
4) Adjust worker/rules.yaml to your real log lines.
5) Start worker: python -m worker.log_ingestor
6) Run the bot:  python -m bot.main_example
7) Set up systemd units from deploy/systemd/*.service when ready.
EOF

# --- venv + deps ---
echo "[+] Creating venv and installing deps"
cd $ROOT
$PY -m venv venv
source venv/bin/activate
pip -q install --upgrade pip
pip -q install -r requirements.txt

# --- init DB (with eligible column) ---
echo "[+] Initializing SQLite DB"
sqlite3 "$ROOT/fika.db" < "$ROOT/shared/schema.sql"
if ! sqlite3 "$ROOT/fika.db" "SELECT 1 FROM pragma_table_info('players') WHERE name='eligible';" | grep -q 1; then
  sqlite3 "$ROOT/fika.db" "ALTER TABLE players ADD COLUMN eligible INTEGER NOT NULL DEFAULT 0;"
fi

echo
echo "Done."
echo "Next:"
echo "  1) cp $ROOT/.env.example $ROOT/.env  # fill tokens/IDs/paths"
echo "  2) (optional) systemd:"
echo "     sudo cp $ROOT/deploy/systemd/fika-xp-{worker,bot,api}.service /etc/systemd/system/"
echo "     sudo systemctl daemon-reload"
echo "     sudo systemctl enable --now fika-xp-worker fika-xp-api fika-xp-bot"
echo "  3) NGINX: proxy /api to 127.0.0.1:8088 and serve your index.html"
echo
