from __future__ import annotations

import os, json, time, base64, hmac, hashlib, secrets, sqlite3
from urllib.parse import urlencode, quote_plus
from typing import Optional, Dict, Any

import httpx
from fastapi import FastAPI, Request, Response, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse, JSONResponse, PlainTextResponse

# ========= Config =========
DB_PATH       = os.getenv("DB_PATH", "/opt/fika_xp/fika.db")
SITE_ORIGIN   = os.getenv("SITE_ORIGIN", "http://51.222.136.98")
CLIENT_ID     = os.getenv("DISCORD_CLIENT_ID")
CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET")
REDIRECT_URI  = os.getenv("DISCORD_REDIRECT_URI", "http://51.222.136.98/xp/api/callback")
SESSION_SECRET = os.getenv("SESSION_SECRET", secrets.token_urlsafe(32))

OAUTH_AUTHORIZE = "https://discord.com/api/oauth2/authorize"
OAUTH_TOKEN     = "https://discord.com/api/oauth2/token"
ME_ENDPOINT     = "https://discord.com/api/users/@me"

# ========= App =========
app = FastAPI(title="Fika XP API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[SITE_ORIGIN],
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=True,
)

# ========= DB helpers =========
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def table_exists(conn: sqlite3.Connection, name: str) -> bool:
    r = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?;", (name,)).fetchone()
    return bool(r)

def column_exists(conn: sqlite3.Connection, table: str, col: str) -> bool:
    cols = [c["name"] for c in conn.execute(f"PRAGMA table_info({table});").fetchall()]
    return col in cols

def ensure_min_schema() -> None:
    """
    Create minimally-required tables *if they don't exist*.
    Do NOT ALTER existing columns to avoid 'datatype mismatch' crashes.
    """
    conn = db()
    cur = conn.cursor()

    # users: holds Discord identity (safe to create if missing)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
          discord_id TEXT PRIMARY KEY,
          username   TEXT,
          avatar     TEXT,
          created_at INTEGER,
          updated_at INTEGER
        )
    """)

    # quests: generic definition (keep flexible)
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

    # user_quests: use flexible single 'status' if that’s what exists
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

    # optional ledgers (used for summary)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS user_xp_ledger (
          id      INTEGER PRIMARY KEY AUTOINCREMENT,
          user_id TEXT NOT NULL,
          amount  INTEGER NOT NULL,
          reason  TEXT,
          ts      INTEGER DEFAULT (strftime('%s','now'))
        )
    """)

    # Try to seed a couple of dev quests (idempotent)
    try:
        cur.execute("""
            INSERT OR IGNORE INTO quests(slug,title,descr,scope,goal,xp,metric,active)
            VALUES (?,?,?,?,?,?,?,1)
        """, ("dev-cheat", "Dev Cheat Quest", "Dev-only quest to verify accept/claim flow", "daily", 1, 500, "manual"))
        cur.execute("""
            INSERT OR IGNORE INTO quests(slug,title,descr,scope,goal,xp,metric,active)
            VALUES (?,?,?,?,?,?,?,1)
        """, ("daily-survive-3", "Survive 3 Raids", "Extract with your loot three times today.", "daily", 3, 250, "extracts"))
    except Exception:
        # If there's any constraint mismatch, ignore seeding (schema is still usable)
        pass

    conn.commit()
    conn.close()

# ========= Session (HMAC cookie) =========
def _sign(b64: str) -> str:
    return base64.urlsafe_b64encode(
        hmac.new(SESSION_SECRET.encode(), b64.encode(), hashlib.sha256).digest()
    ).decode().rstrip("=")

def set_session(resp: Response, payload: Dict[str, Any]) -> None:
    raw = json.dumps(payload, separators=(",", ":"))
    b64 = base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")
    sig = _sign(b64)
    resp.set_cookie(
        "sess", f"{b64}.{sig}",
        httponly=True, samesite="Lax", secure=False, max_age=60*60*24*30
    )

def get_session(req: Request) -> Optional[Dict[str, Any]]:
    c = req.cookies.get("sess")
    if not c or "." not in c:
        return None
    b64, sig = c.rsplit(".", 1)
    if _sign(b64) != sig:
        return None
    try:
        raw = base64.urlsafe_b64decode(b64 + "=")
        return json.loads(raw)
    except Exception:
        return None

# ========= Utilities =========
def now_s() -> int:
    return int(time.time())

def to_bool_status(status: str) -> Dict[str, bool]:
    s = (status or "not_accepted").lower()
    return {
        "accepted":  s in ("accepted", "in_progress", "completed", "claimed"),
        "completed": s in ("completed", "claimed"),
        "claimed":   s == "claimed",
    }

def user_total_xp(conn: sqlite3.Connection, user_id: str) -> int:
    total = 0
    # prefer user_xp_ledger if present
    if table_exists(conn, "user_xp_ledger"):
        total += conn.execute(
            "SELECT COALESCE(SUM(amount),0) AS t FROM user_xp_ledger WHERE user_id=?",
            (user_id,)
        ).fetchone()[0] or 0
    # fallback or additional legacy ledger
    if table_exists(conn, "xp_ledger") and column_exists(conn, "xp_ledger", "discord_id"):
        total += conn.execute(
            "SELECT COALESCE(SUM(xp),0) AS t FROM xp_ledger WHERE discord_id=?",
            (user_id,)
        ).fetchone()[0] or 0
    return int(total)

def xp_to_level(total_xp: int) -> Dict[str, int]:
    LEVEL_CAP = 1000
    level = total_xp // LEVEL_CAP + 1
    xp_in_level = total_xp % LEVEL_CAP
    return dict(level=level, xp_in_level=xp_in_level, level_cap=LEVEL_CAP)

# ========= Startup =========
@app.on_event("startup")
def _startup() -> None:
    ensure_min_schema()

# ========= Debug / Health =========
@app.get("/api/debug/ping")
def ping() -> Dict[str, Any]:
    return {"ok": True, "t": now_s()}

@app.get("/api/debug/dbcheck")
def dbcheck() -> Dict[str, Any]:
    conn = db()
    try:
        tables = []
        for r in conn.execute("SELECT name, sql FROM sqlite_master WHERE type='table' ORDER BY name;"):
            tables.append({"name": r["name"], "sql": r["sql"]})
        qcount = conn.execute("SELECT COUNT(*) FROM quests;").fetchone()[0]
        return {"db_path": DB_PATH, "tables": tables, "quest_count": qcount}
    finally:
        conn.close()

# ========= OAuth =========
@app.get("/api/login")
def oauth_login(redirect: str = "/quests.html"):
    state = redirect  # you can HMAC this if you want, but keeping it simple
    q = {
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": "identify",
        "prompt": "consent",
        "state": state,
    }
    return RedirectResponse(OAUTH_AUTHORIZE + "?" + urlencode(q))

@app.get("/api/callback")
async def oauth_callback(request: Request, code: str, state: str = "/quests.html"):
    if not CLIENT_ID or not CLIENT_SECRET:
        raise HTTPException(500, "OAuth not configured")

    async with httpx.AsyncClient() as http:
        data = {
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT_URI,
        }
        tok = (await http.post(
            OAUTH_TOKEN,
            data=data,
            headers={"Content-Type":"application/x-www-form-urlencoded"}
        )).json()
        if "access_token" not in tok:
            raise HTTPException(400, "oauth exchange failed")

        me = (await http.get(
            ME_ENDPOINT,
            headers={"Authorization": f"Bearer {tok['access_token']}"}
        )).json()

    # upsert users
    conn = db()
    try:
        conn.execute("""
            INSERT INTO users(discord_id, username, avatar, created_at, updated_at)
            VALUES(?,?,?,?,?)
            ON CONFLICT(discord_id) DO UPDATE SET
              username=excluded.username,
              avatar=excluded.avatar,
              updated_at=excluded.updated_at
        """, (me["id"], me.get("username"), me.get("avatar"), now_s(), now_s()))
        conn.commit()
    finally:
        conn.close()

    # set session cookie
    resp = RedirectResponse(url=f"{SITE_ORIGIN}{state}")
    set_session(resp, {
        "discord_id": me["id"],
        "username": me.get("username"),
        "avatar": me.get("avatar"),
    })
    return resp

@app.get("/api/logout")
def logout(redirect: str = "/login.html"):
    resp = RedirectResponse(url=f"{SITE_ORIGIN}{redirect}")
    resp.delete_cookie("sess")
    return resp

# ========= Auth / Session =========
@app.get("/api/me")
def me(request: Request):
    sess = get_session(request)
    if not sess:
        return {"auth": False}
    return {
        "auth": True,
        "discord_id": sess["discord_id"],
        "username": sess.get("username"),
        "avatar": sess.get("avatar"),
    }

# ========= Summary / Quests =========
@app.get("/api/summary")
def summary(user_id: str):
    """
    user_id is the Discord ID (string). Frontend already sends that.
    """
    conn = db()
    try:
        total = user_total_xp(conn, user_id)
        lvl = xp_to_level(total)
        return {
            "level": lvl["level"],
            "total_xp": total,
            "xp_in_level": lvl["xp_in_level"],
            "level_cap": lvl["level_cap"],
            "progress_pct": round(100 * lvl["xp_in_level"] / max(1, lvl["level_cap"]))
        }
    finally:
        conn.close()

@app.get("/api/quests")
def list_quests(request: Request, scope: str = "all"):
    """
    Returns quests. If user is logged in, merges their status from user_quests.status.
    """
    sess = get_session(request)
    user_id = sess["discord_id"] if sess else None

    conn = db()
    try:
        if scope == "all":
            rows = conn.execute("SELECT id, slug, title, descr, scope, goal, xp FROM quests WHERE active=1 ORDER BY id;").fetchall()
        else:
            rows = conn.execute("SELECT id, slug, title, descr, scope, goal, xp FROM quests WHERE active=1 AND scope=? ORDER BY id;", (scope,)).fetchall()

        status_map: Dict[int, Dict[str, Any]] = {}
        if user_id and table_exists(conn, "user_quests"):
            uq_rows = conn.execute("SELECT quest_id, status, progress FROM user_quests WHERE user_id=?;", (user_id,)).fetchall()
            for r in uq_rows:
                status_map[int(r["quest_id"])] = {
                    "status": r["status"],
                    "progress": r["progress"] or 0
                }

        out = []
        for r in rows:
            qid = int(r["id"])
            st_row = status_map.get(qid, {"status": "not_accepted", "progress": 0})
            flags = to_bool_status(st_row["status"])
            out.append({
                "id": qid,
                "title": r["title"],
                "descr": r["descr"],
                "scope": r["scope"],
                "goal": r["goal"],
                "xp": r["xp"],
                "accepted": flags["accepted"],
                "completed": flags["completed"],
                "claimed": flags["claimed"],
                "progress": st_row["progress"],
            })
        return out
    finally:
        conn.close()

@app.post("/api/user/quests/accept")
async def accept_quest(request: Request):
    body = await request.json()
    user_id = str(body.get("user_id") or "").strip()
    quest_id = int(body.get("quest_id"))

    if not user_id:
        raise HTTPException(401, "not logged in")

    conn = db()
    try:
        # Insert or bump to accepted if not already
        conn.execute("""
            INSERT INTO user_quests(user_id, quest_id, accepted_at, status, progress)
            VALUES(?,?,?,?,?)
            ON CONFLICT(user_id, quest_id) DO UPDATE SET
              status='accepted',
              accepted_at=COALESCE(user_quests.accepted_at, excluded.accepted_at)
        """, (user_id, quest_id, now_s(), "accepted", 0))
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()

@app.post("/api/user/quests/claim")
async def claim_quest(request: Request):
    body = await request.json()
    user_id = str(body.get("user_id") or "").strip()
    quest_id = int(body.get("quest_id"))

    if not user_id:
        raise HTTPException(401, "not logged in")

    conn = db()
    try:
        # Mark as claimed if currently completed or accepted
        conn.execute("""
            INSERT INTO user_quests(user_id, quest_id, accepted_at, status, progress, completed_at, claimed_at)
            VALUES(?,?,?,?,?,?,?)
            ON CONFLICT(user_id, quest_id) DO UPDATE SET
              status='claimed',
              completed_at=COALESCE(user_quests.completed_at, excluded.completed_at),
              claimed_at=excluded.claimed_at
        """, (user_id, quest_id, now_s(), "claimed", 0, now_s(), now_s()))

        # Credit XP
        xp_row = conn.execute("SELECT xp FROM quests WHERE id=?;", (quest_id,)).fetchone()
        xp = int(xp_row["xp"]) if xp_row and xp_row["xp"] is not None else 0
        if xp:
            conn.execute("INSERT INTO user_xp_ledger(user_id, amount, reason, ts) VALUES (?,?,?,?)",
                         (user_id, xp, f"claim:{quest_id}", now_s()))
        conn.commit()
        return {"ok": True, "granted": xp}
    finally:
        conn.close()

@app.post("/api/user/quests/discard")
async def discard_quest(request: Request):
    body = await request.json()
    user_id = str(body.get("user_id") or "").strip()
    quest_id = int(body.get("quest_id"))

    if not user_id:
        raise HTTPException(401, "not logged in")

    conn = db()
    try:
        conn.execute("DELETE FROM user_quests WHERE user_id=? AND quest_id=?", (user_id, quest_id))
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()
