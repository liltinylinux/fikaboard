from __future__ import annotations
import os, json, sqlite3, secrets, base64, hmac, hashlib, time
from urllib.parse import urlencode, quote
import httpx
from fastapi import FastAPI, Request, Response, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse, JSONResponse

# ---------------------------
# Config
# ---------------------------
DB_PATH        = os.getenv("DB_PATH", "/opt/fika_xp/fika.db")
CLIENT_ID      = os.getenv("DISCORD_CLIENT_ID", "")
CLIENT_SECRET  = os.getenv("DISCORD_CLIENT_SECRET", "")
REDIRECT_URI   = os.getenv("DISCORD_REDIRECT_URI", "http://51.222.136.98/xp/api/callback")
SITE_ORIGIN    = os.getenv("SITE_ORIGIN", "http://51.222.136.98")
SESSION_SECRET = os.getenv("SESSION_SECRET", secrets.token_urlsafe(32))

OAUTH_AUTHORIZE = "https://discord.com/api/oauth2/authorize"
OAUTH_TOKEN     = "https://discord.com/api/oauth2/token"
ME_ENDPOINT     = "https://discord.com/api/users/@me"

# cookie flags
IS_HTTPS = SITE_ORIGIN.startswith("https://")

# ---------------------------
# App + CORS
# ---------------------------
app = FastAPI(title="Fika XP API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[SITE_ORIGIN],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------
# Session helpers (signed cookie)
# ---------------------------
def _b64(s: bytes) -> str:
    return base64.urlsafe_b64encode(s).decode().rstrip("=")

def _ub64(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode((s + pad).encode())

def sign(payload: str) -> str:
    mac = hmac.new(SESSION_SECRET.encode(), payload.encode(), hashlib.sha256).digest()
    return _b64(mac)

def set_session(resp: Response, payload: dict):
    raw = json.dumps(payload, separators=(",", ":"))
    blob = _b64(raw.encode())
    cookie = f"{blob}.{sign(blob)}"
    resp.set_cookie(
        "sess",
        cookie,
        httponly=True,
        samesite="Lax",
        secure=IS_HTTPS,       # only secure if you’re on https
        max_age=60*60*24*30,
        path="/",
    )

def clear_session(resp: Response):
    resp.delete_cookie("sess", path="/")

def get_session(req: Request) -> dict | None:
    c = req.cookies.get("sess")
    if not c or "." not in c:
        return None
    blob, sig = c.rsplit(".", 1)
    if sign(blob) != sig:
        return None
    try:
        raw = _ub64(blob)
        return json.loads(raw)
    except Exception:
        return None

# ---------------------------
# DB helpers
# ---------------------------
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def table_has_column(conn: sqlite3.Connection, table: str, col: str) -> bool:
    cur = conn.execute(f"PRAGMA table_info({table})")
    return any(r["name"] == col for r in cur.fetchall())

def ensure_schema():
    conn = db(); cur = conn.cursor()

    # users
    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
      discord_id TEXT PRIMARY KEY,
      username   TEXT,
      avatar     TEXT,
      created_at INTEGER,
      updated_at INTEGER
    )""")

    # quests
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
    )""")

    # user_quests – modern schema
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
    )""")

    # XP (ledger-based; also tolerate older tables)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS user_xp_ledger (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      user_id TEXT NOT NULL,
      amount  INTEGER NOT NULL,
      reason  TEXT,
      ts      INTEGER DEFAULT (strftime('%s','now'))
    )""")

    # seed a few quests if none
    qcount = cur.execute("SELECT COUNT(*) FROM quests").fetchone()[0]
    if qcount == 0:
        cur.executemany(
            "INSERT INTO quests(slug,title,descr,scope,goal,xp,metric,active) VALUES (?,?,?,?,?,?,?,1)",
            [
                ("dev-cheat", "Dev Cheat Quest", "Dev-only quest to verify accept/claim flow", "daily", 1, 500, "manual"),
                ("survive-3", "Survive 3 Raids", "Extract with your loot three times today.", "daily", 3, 250, "extracts"),
                ("kills-10", "Eliminate 10 PMCs", "Eliminate 10 PMCs this week.", "weekly", 10, 400, "pmc_kills"),
            ],
        )

    conn.commit()
    conn.close()

ensure_schema()

# ---------------------------
# OAuth login
# ---------------------------
@app.get("/api/login", summary="Oauth Login")
async def oauth_login(redirect: str = "/quests.html"):
    # store desired redirect path into 'state'
    state = quote(redirect, safe="/?=&:#-")
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
async def oauth_callback(request: Request, code: str, state: str = "/quests.html"):
    async with httpx.AsyncClient(timeout=15) as http:
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
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )).json()

        if "access_token" not in tok:
            raise HTTPException(400, "OAuth exchange failed")

        me = (await http.get(
            ME_ENDPOINT,
            headers={"Authorization": f"Bearer {tok['access_token']}"},
        )).json()

    # upsert user
    conn = db(); cur = conn.cursor()
    now = int(time.time())
    cur.execute("""
      INSERT INTO users(discord_id,username,avatar,created_at,updated_at)
      VALUES(?,?,?,?,?)
      ON CONFLICT(discord_id) DO UPDATE SET
        username=excluded.username, avatar=excluded.avatar, updated_at=excluded.updated_at
    """, (me["id"], me["username"], me.get("avatar"), now, now))
    conn.commit(); conn.close()

    # set session + bounce to requested state path (only within our site)
    redir_path = state if state.startswith("/") else "/quests.html"
    resp = RedirectResponse(url=SITE_ORIGIN + redir_path)
    set_session(resp, {"discord_id": me["id"], "username": me["username"], "avatar": me.get("avatar")})
    return resp

@app.get("/api/logout")
async def logout(redirect: str = "/quests.html"):
    resp = RedirectResponse(url=SITE_ORIGIN + redirect)
    clear_session(resp)
    return resp

# ---------------------------
# API
# ---------------------------
@app.get("/api/me")
async def get_me(request: Request):
    sess = get_session(request)
    if not sess:
        return {"auth": False}
    return {
        "auth": True,
        "discord_id": sess["discord_id"],
        "username"  : sess.get("username"),
        "avatar"    : sess.get("avatar"),
    }

def compute_summary(conn: sqlite3.Connection, user_id: str) -> dict:
    # total XP from ledger (fallbacks if empty)
    cur = conn.execute("SELECT COALESCE(SUM(amount),0) FROM user_xp_ledger WHERE user_id=?", (user_id,))
    total = cur.fetchone()[0] or 0

    # simple leveling: 1000 xp / level
    cap = 1000
    level = total // cap + 1
    xp_in_level = total % cap
    return {
        "level": int(level),
        "total_xp": int(total),
        "xp_in_level": int(xp_in_level),
        "level_cap": cap,
        "progress_pct": round(100 * xp_in_level / cap) if cap else 0,
    }

@app.get("/api/summary")
async def summary(user_id: str):
    conn = db()
    out = compute_summary(conn, user_id)
    conn.close()
    return out

def status_to_flags(status: str) -> tuple[bool,bool,bool]:
    s = (status or "not_accepted").lower()
    accepted = s in ("accepted","in_progress","completed","claimed")
    completed = s in ("completed","claimed")
    claimed   = s == "claimed"
    return accepted, completed, claimed

@app.get("/api/quests")
async def list_quests(request: Request, scope: str = "daily"):
    sess = get_session(request)
    user_id = sess["discord_id"] if sess else None

    conn = db(); cur = conn.cursor()

    if scope == "all":
        qrows = cur.execute("SELECT id, slug, title, descr, scope, goal, xp FROM quests WHERE active=1 ORDER BY id").fetchall()
    else:
        qrows = cur.execute("SELECT id, slug, title, descr, scope, goal, xp FROM quests WHERE active=1 AND scope=? ORDER BY id", (scope,)).fetchall()

    # fetch user progress (if logged in)
    progress_map: dict[int, dict] = {}
    if user_id:
        # support either modern 'status' or legacy accepted/completed/claimed
        has_acc = table_has_column(conn, "user_quests", "accepted")
        if has_acc:
            upr = cur.execute("""
                SELECT quest_id, accepted, completed, claimed, progress
                FROM user_quests WHERE user_id=?
            """, (user_id,)).fetchall()
            for r in upr:
                progress_map[r["quest_id"]] = {
                    "accepted": bool(r["accepted"]),
                    "completed": bool(r["completed"]),
                    "claimed": bool(r["claimed"]),
                    "progress": int(r["progress"] or 0),
                }
        else:
            upr = cur.execute("""
                SELECT quest_id, status, progress
                FROM user_quests WHERE user_id=?
            """, (user_id,)).fetchall()
            for r in upr:
                a,c,l = status_to_flags(r["status"])
                progress_map[r["quest_id"]] = {
                    "accepted": a, "completed": c, "claimed": l,
                    "progress": int(r["progress"] or 0),
                }

    out = []
    for r in qrows:
        p = progress_map.get(r["id"], {"accepted": False, "completed": False, "claimed": False, "progress": 0})
        out.append({
            "id": r["id"], "slug": r["slug"], "title": r["title"], "descr": r["descr"],
            "scope": r["scope"], "goal": r["goal"], "xp": r["xp"],
            "accepted": p["accepted"], "completed": p["completed"], "claimed": p["claimed"],
            "progress": p["progress"],
        })

    conn.close()
    return out

# --------- quest mutations ---------
def upsert_progress(conn: sqlite3.Connection, user_id: str, quest_id: int):
    # ensure row exists
    conn.execute("""
        INSERT INTO user_quests(user_id, quest_id, status, accepted_at, progress)
        VALUES(?, ?, 'accepted', ?, 0)
        ON CONFLICT(user_id, quest_id) DO NOTHING
    """, (user_id, quest_id, int(time.time())))

@app.post("/api/user/quests/accept")
async def accept(request: Request, user_id: str, quest_id: int):
    conn = db(); cur = conn.cursor()
    upsert_progress(conn, user_id, quest_id)

    if table_has_column(conn, "user_quests", "accepted"):  # legacy
        cur.execute("""
          UPDATE user_quests
          SET accepted=1
          WHERE user_id=? AND quest_id=?
        """, (user_id, quest_id))
    else:
        cur.execute("""
          UPDATE user_quests
          SET status=CASE
              WHEN status IN ('completed','claimed') THEN status
              ELSE 'accepted'
          END,
              accepted_at=COALESCE(accepted_at, ?)
          WHERE user_id=? AND quest_id=?
        """, (int(time.time()), user_id, quest_id))

    conn.commit(); conn.close()
    return {"ok": True}

@app.post("/api/user/quests/claim")
async def claim(request: Request, user_id: str, quest_id: int):
    conn = db(); cur = conn.cursor()
    upsert_progress(conn, user_id, quest_id)

    # grant XP once
    already = False
    if table_has_column(conn, "user_quests", "claimed"):  # legacy
        row = cur.execute("SELECT claimed FROM user_quests WHERE user_id=? AND quest_id=?", (user_id, quest_id)).fetchone()
        already = bool(row and row[0])
    else:
        row = cur.execute("SELECT status FROM user_quests WHERE user_id=? AND quest_id=?", (user_id, quest_id)).fetchone()
        already = (row and str(row[0]).lower() == "claimed")

    if already:
        conn.close()
        return {"ok": True, "message": "Already claimed"}

    # mark claimed + write ledger
    if table_has_column(conn, "user_quests", "claimed"):  # legacy
        cur.execute("UPDATE user_quests SET completed=1, claimed=1 WHERE user_id=? AND quest_id=?", (user_id, quest_id))
    else:
        cur.execute("""
          UPDATE user_quests SET
            status='claimed',
            completed_at = COALESCE(completed_at, ?),
            claimed_at   = ?
          WHERE user_id=? AND quest_id=?
        """, (int(time.time()), int(time.time()), user_id, quest_id))

    # xp
    xp = cur.execute("SELECT xp FROM quests WHERE id=?", (quest_id,)).fetchone()
    grant = int(xp[0] if xp else 0)
    if grant:
        cur.execute("INSERT INTO user_xp_ledger(user_id, amount, reason) VALUES (?,?,?)",
                    (user_id, grant, f"claim:quest:{quest_id}"))

    conn.commit(); conn.close()
    return {"ok": True, "granted": grant}

@app.post("/api/user/quests/discard")
async def discard(request: Request, user_id: str, quest_id: int):
    conn = db(); cur = conn.cursor()
    if table_has_column(conn, "user_quests", "accepted"):  # legacy
        cur.execute("DELETE FROM user_quests WHERE user_id=? AND quest_id=?", (user_id, quest_id))
    else:
        cur.execute("""
          UPDATE user_quests SET
            status='not_accepted', progress=0, accepted_at=NULL, completed_at=NULL, claimed_at=NULL
          WHERE user_id=? AND quest_id=?
        """, (user_id, quest_id))
    conn.commit(); conn.close()
    return {"ok": True}

# ---------------------------
# Debug endpoint
# ---------------------------
@app.get("/api/debug/dbcheck")
def dbcheck():
    conn = db()
    rows = conn.execute("SELECT name, sql FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
    qc = conn.execute("SELECT COUNT(*) FROM quests").fetchone()[0]
    conn.close()
    return {
        "db_path": DB_PATH,
        "tables": [{"name": r["name"], "sql": r["sql"]} for r in rows],
        "quest_count": qc
    }
