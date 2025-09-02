from __future__ import annotations
import os, json, sqlite3, secrets, base64, hmac, hashlib, time
from urllib.parse import urlencode, quote, unquote
import httpx
from fastapi import FastAPI, Request, Response, HTTPException, Body, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse, JSONResponse
from pydantic import BaseModel

import os

ADMIN_IDS = set([x.strip() for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()])

# ... your existing app = FastAPI() & CORS ...

@app.middleware("http")
async def attach_session(request: Request, call_next):
    # make cookie session accessible to routers via request.state.session
    try:
        sess = get_session(request)
    except Exception:
        sess = None
    request.state.session = sess
    return await call_next(request)

# --- API ---
@app.get("/api/me")
async def me(request: Request):
    sess = get_session(request)
    if not sess:
        return {"auth": False}
    did = str(sess["discord_id"])
    # ... your existing DB lookups ...
    return {
        "auth": True,
        "discord_id": did,
        "username": sess.get("username"),
        "avatar": sess.get("avatar"),
        "is_admin": did in ADMIN_IDS,
        # ... keep the rest of your fields ...
    }

# Mount admin router
from .admin_router import router as admin_router
app.include_router(admin_router, prefix="/api/admin")

# =========================
# Config
# =========================
DB_PATH        = os.getenv("DB_PATH", "/opt/fika_xp/fika.db")
CLIENT_ID      = os.getenv("DISCORD_CLIENT_ID")
CLIENT_SECRET  = os.getenv("DISCORD_CLIENT_SECRET")
SITE_ORIGIN    = os.getenv("SITE_ORIGIN", "http://51.222.136.98")
REDIRECT_URI   = os.getenv("DISCORD_REDIRECT_URI", f"{SITE_ORIGIN}/xp/api/callback")
SESSION_SECRET = os.getenv("SESSION_SECRET", secrets.token_urlsafe(32))

OAUTH_AUTHORIZE = "https://discord.com/api/oauth2/authorize"
OAUTH_TOKEN     = "https://discord.com/api/oauth2/token"
ME_ENDPOINT     = "https://discord.com/api/users/@me"

# =========================
# App & CORS
# =========================
app = FastAPI(title="Fika XP API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[SITE_ORIGIN],
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=True,
)

# =========================
# DB helpers
# =========================
def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def seed_quests_if_missing() -> None:
    """
    Safe seeding using slug (UNIQUE) so we don't collide with existing IDs.
    Matches your current quests schema:
        quests(id INTEGER PK AUTOINCREMENT, slug TEXT UNIQUE, title, descr, scope, goal, xp, metric, active INT)
    """
    conn = connect()
    cur  = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS quests (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            slug    TEXT UNIQUE,
            title   TEXT,
            descr   TEXT,
            scope   TEXT,
            goal    INTEGER,
            xp      INTEGER,
            metric  TEXT,
            active  INTEGER DEFAULT 1
        )
    """)
    # Dev cheat
    cur.execute("""
        INSERT OR IGNORE INTO quests(slug, title, descr, scope, goal, xp, metric, active)
        VALUES (?, ?, ?, ?, ?, ?, ?, 1)
    """, ("dev-cheat", "Dev Cheat Quest", "Dev-only quest to verify accept/claim flow", "daily", 1, 500, "manual"))
    # Example daily
    cur.execute("""
        INSERT OR IGNORE INTO quests(slug, title, descr, scope, goal, xp, metric, active)
        VALUES (?, ?, ?, ?, ?, ?, ?, 1)
    """, ("survive-3", "Survive 3 Raids", "Extract with your loot three times today.", "daily", 3, 250, "extracts"))
    conn.commit()
    conn.close()

# =========================
# Session cookie helpers
# =========================
def _sign(data: str) -> str:
    return base64.urlsafe_b64encode(
        hmac.new(SESSION_SECRET.encode(), data.encode(), hashlib.sha256).digest()
    ).decode().rstrip("=")

def set_session(resp: Response, payload: dict):
    raw = json.dumps(payload, separators=(",", ":"))
    cookie = base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")
    resp.set_cookie(
        "sess", cookie + "." + _sign(cookie),
        httponly=True, samesite="Lax", secure=False, max_age=60*60*24*30
    )

def get_session(req: Request) -> dict | None:
    c = req.cookies.get("sess")
    if not c or "." not in c:
        return None
    cookie, sig = c.rsplit(".", 1)
    if _sign(cookie) != sig:
        return None
    try:
        raw = base64.urlsafe_b64decode(cookie + "=")
        return json.loads(raw)
    except Exception:
        return None

# =========================
# Models
# =========================
class QuestAction(BaseModel):
    user_id: str
    quest_id: int

# =========================
# OAuth
# =========================
@app.get("/api/login", summary="Oauth Login")
async def oauth_login(redirect: str = Query("/quests.html")):
    # carry the desired redirect in 'state'
    state = quote(redirect, safe="")
    q = {
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": "identify",
        "prompt": "consent",
        "state": state,
    }
    return RedirectResponse(OAUTH_AUTHORIZE + "?" + urlencode(q))

@app.get("/api/callback", summary="Oauth Callback")
async def oauth_callback(request: Request, code: str, state: str | None = None):
    async with httpx.AsyncClient() as http:
        data = {
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT_URI,
        }
        tok = (await http.post(
            OAUTH_TOKEN, data=data,
            headers={"Content-Type":"application/x-www-form-urlencoded"}
        )).json()
        if "access_token" not in tok:
            raise HTTPException(400, "oauth exchange failed")
        me = (await http.get(ME_ENDPOINT, headers={"Authorization": f"Bearer {tok['access_token']}"})).json()

    # ensure user record exists (users table seen in your DB)
    conn = connect()
    cur  = conn.cursor()
    now  = int(time.time())
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
          discord_id TEXT PRIMARY KEY,
          username   TEXT,
          avatar     TEXT,
          created_at INTEGER,
          updated_at INTEGER
        )
    """)
    cur.execute("""
        INSERT INTO users(discord_id, username, avatar, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(discord_id) DO UPDATE SET
          username=excluded.username,
          avatar=excluded.avatar,
          updated_at=excluded.updated_at
    """, (me["id"], me.get("username"), me.get("avatar"), now, now))
    conn.commit()
    conn.close()

    dest = unquote(state) if state else "/quests.html"
    resp = RedirectResponse(url=f"{SITE_ORIGIN}{dest}")
    set_session(resp, {"discord_id": me["id"], "username": me.get("username"), "avatar": me.get("avatar")})
    return resp

@app.get("/api/logout")
async def logout():
    resp = RedirectResponse(url=SITE_ORIGIN)
    resp.delete_cookie("sess")
    return resp

# =========================
# Me / Summary
# =========================
@app.get("/api/me")
async def me(request: Request):
    sess = get_session(request)
    if not sess:
        return {"authenticated": False}
    # Build avatar URL if present
    avatar_url = None
    if sess.get("avatar"):
        avatar_url = f"https://cdn.discordapp.com/avatars/{sess['discord_id']}/{sess['avatar']}.png"
    return {
        "authenticated": True,
        "user": {
            "id": sess["discord_id"],
            "name": sess.get("username"),
            "avatar": avatar_url
        }
    }

def level_summary(user_id: str) -> dict:
    """
    Level math derived from user_xp_ledger (present in your DB).
    1000 XP per level.
    """
    conn = connect()
    cur  = conn.cursor()
    cur.execute("CREATE TABLE IF NOT EXISTS user_xp_ledger (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL, amount INTEGER NOT NULL, reason TEXT, ts INTEGER DEFAULT (strftime('%s','now')))")  # safety
    cur.execute("SELECT COALESCE(SUM(amount),0) as total FROM user_xp_ledger WHERE user_id=?", (user_id,))
    total = int((cur.fetchone() or {"total": 0})["total"] or 0)
    conn.close()

    level_cap = 1000
    level = (total // level_cap) + 1
    xp_in_level = total % level_cap
    return {
        "level": level,
        "total_xp": total,
        "xp_in_level": xp_in_level,
        "level_cap": level_cap,
        "progress_pct": round(100 * xp_in_level / level_cap) if level_cap else 0
    }

@app.get("/api/summary")
async def summary(user_id: str = Query(...)):
    return level_summary(user_id)

# =========================
# Quests: list + actions (schema-compatible with user_quests)
# =========================
@app.get("/api/quests")
async def list_quests(request: Request, scope: str = "all"):
    """
    Returns an ARRAY of quests with booleans derived from user_quests'
    accepted_at/completed_at/claimed_at/status/progress fields.
    """
    seed_quests_if_missing()

    sess = get_session(request)
    user_id = sess["discord_id"] if sess else None

    conn = connect()
    cur  = conn.cursor()

    # Ensure user_quests exists the way your DB has it:
    cur.execute("""
        CREATE TABLE IF NOT EXISTS user_quests (
            user_id      TEXT NOT NULL,
            quest_id     INTEGER NOT NULL,
            accepted_at  INTEGER,
            progress     INTEGER DEFAULT 0,
            completed_at INTEGER,
            claimed_at   INTEGER,
            status       TEXT DEFAULT 'not_accepted',
            PRIMARY KEY (user_id, quest_id),
            FOREIGN KEY (quest_id) REFERENCES quests(id)
        )
    """)

    # Get quests
    if scope == "all":
        cur.execute("SELECT id,title,descr,scope,goal,xp FROM quests WHERE active=1 ORDER BY id")
    else:
        cur.execute("SELECT id,title,descr,scope,goal,xp FROM quests WHERE active=1 AND scope=? ORDER BY id", (scope,))
    qrows = cur.fetchall()

    # Map user progress if authed
    uq_map = {}
    if user_id:
        cur.execute(
            "SELECT quest_id, accepted_at, completed_at, claimed_at, status, progress "
            "FROM user_quests WHERE user_id=?",
            (user_id,),
        )
        for r in cur.fetchall():
            qid = r["quest_id"]
            accepted  = bool(r["accepted_at"]) or (r["status"] in ("accepted", "complete", "claimed"))
            completed = bool(r["completed_at"]) or (r["status"] in ("complete", "claimed"))
            claimed   = bool(r["claimed_at"]) or (r["status"] == "claimed")
            uq_map[qid] = {
                "accepted": accepted,
                "completed": completed,
                "claimed": claimed,
                "progress": int(r["progress"] or 0),
            }

    out = []
    for r in qrows:
        qid  = r["id"]
        goal = int(r["goal"] or 0)
        u    = uq_map.get(qid, {"accepted": False, "completed": False, "claimed": False, "progress": 0})

        # For single-step quests, "accepted" can imply completion if goal==1 and progress logic says so.
        completed = u["completed"] or (goal == 1 and u["accepted"])

        out.append({
            "id": qid,
            "title": r["title"],
            "descr": r["descr"],
            "scope": r["scope"],
            "goal": goal,
            "xp": int(r["xp"] or 0),
            "accepted": u["accepted"],
            "completed": completed,
            "claimed": u["claimed"],
            "progress": u["progress"] if goal > 1 else (1 if completed else 0),
        })

    conn.close()
    return out

@app.post("/api/user/quests/accept")
async def accept(body: QuestAction = Body(...)):
    """
    Upsert into user_quests using accepted_at + status (no accepted/completed/claimed boolean columns).
    """
    now = int(time.time())
    conn = connect()
    cur  = conn.cursor()

    # Ensure quest exists
    cur.execute("SELECT 1 FROM quests WHERE id=?", (body.quest_id,))
    if not cur.fetchone():
        conn.close()
        raise HTTPException(400, "Unknown quest")

    # Ensure table exists (safety)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS user_quests (
            user_id      TEXT NOT NULL,
            quest_id     INTEGER NOT NULL,
            accepted_at  INTEGER,
            progress     INTEGER DEFAULT 0,
            completed_at INTEGER,
            claimed_at   INTEGER,
            status       TEXT DEFAULT 'not_accepted',
            PRIMARY KEY (user_id, quest_id),
            FOREIGN KEY (quest_id) REFERENCES quests(id)
        )
    """)

    # Upsert acceptance
    cur.execute(
        "INSERT INTO user_quests (user_id, quest_id, accepted_at, status, progress) "
        "VALUES (?, ?, ?, 'accepted', 0) "
        "ON CONFLICT(user_id, quest_id) DO UPDATE SET "
        "  accepted_at = COALESCE(user_quests.accepted_at, excluded.accepted_at), "
        "  status = CASE WHEN user_quests.status IN ('claimed','complete') "
        "                THEN user_quests.status ELSE 'accepted' END",
        (body.user_id, body.quest_id, now),
    )
    conn.commit()
    conn.close()
    return {"ok": True}

@app.post("/api/user/quests/claim")
async def claim(body: QuestAction = Body(...)):
    """
    Validate accepted, ensure not already claimed, write ledger, and mark claimed/completed/status.
    """
    now = int(time.time())
    conn = connect()
    cur  = conn.cursor()

    # Check acceptance
    cur.execute(
        "SELECT accepted_at, completed_at, claimed_at, status, progress "
        "FROM user_quests WHERE user_id=? AND quest_id=?",
        (body.user_id, body.quest_id),
    )
    row = cur.fetchone()
    if not row or not row["accepted_at"]:
        conn.close()
        raise HTTPException(400, "Quest not accepted")
    if row["claimed_at"]:
        conn.close()
        raise HTTPException(400, "Already claimed")

    # XP from quest
    cur.execute("SELECT xp FROM quests WHERE id=?", (body.quest_id,))
    r = cur.fetchone()
    if not r:
        conn.close()
        raise HTTPException(400, "Unknown quest")
    xp = int(r["xp"] or 0)

    # Mark claimed/completed
    cur.execute(
        "UPDATE user_quests SET claimed_at=?, completed_at=COALESCE(completed_at, ?), status='claimed' "
        "WHERE user_id=? AND quest_id=?",
        (now, now, body.user_id, body.quest_id),
    )

    # Ledger entry
    cur.execute("""
        CREATE TABLE IF NOT EXISTS user_xp_ledger (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            amount INTEGER NOT NULL,
            reason TEXT,
            ts INTEGER DEFAULT (strftime('%s','now'))
        )
    """)
    cur.execute(
        "INSERT INTO user_xp_ledger (user_id, amount, reason, ts) VALUES (?, ?, ?, ?)",
        (body.user_id, xp, f"claim:{body.quest_id}", now),
    )

    conn.commit()
    conn.close()
    return {"ok": True, "granted": xp, "summary": level_summary(body.user_id)}

@app.post("/api/user/quests/discard")
async def discard(body: QuestAction = Body(...)):
    conn = connect()
    cur  = conn.cursor()
    cur.execute("DELETE FROM user_quests WHERE user_id=? AND quest_id=?", (body.user_id, body.quest_id))
    conn.commit()
    conn.close()
    return {"ok": True}

# =========================
# Debug helpers
# =========================
@app.get("/api/debug/dbcheck")
def dbcheck():
    conn = connect()
    cur  = conn.cursor()
    cur.execute("SELECT name, sql FROM sqlite_master WHERE type='table' ORDER BY name")
    tables = [{"name": r["name"], "sql": r["sql"]} for r in cur.fetchall()]
    # Count quests
    try:
        cur.execute("SELECT COUNT(*) AS c FROM quests")
        quest_count = int(cur.fetchone()["c"])
    except Exception:
        quest_count = 0
    conn.close()
    return {"db_path": DB_PATH, "tables": tables, "quest_count": quest_count}
