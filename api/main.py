from __future__ import annotations

import os
import json
import time
import hmac
import base64
import hashlib
import sqlite3
from urllib.parse import urlencode, quote

import httpx
from fastapi import FastAPI, Request, Response, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse, JSONResponse

# =============================================================================
# Config
# =============================================================================

DB_PATH = os.getenv("DB_PATH", "/opt/fika_xp/fika.db")

SITE_ORIGIN = os.getenv("SITE_ORIGIN", "http://51.222.136.98").rstrip("/")
CLIENT_ID = os.getenv("DISCORD_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET", "")
REDIRECT_URI = os.getenv("DISCORD_REDIRECT_URI", f"{SITE_ORIGIN}/xp/api/callback").rstrip("/")

SESSION_SECRET = os.getenv("SESSION_SECRET", "dev-session-secret-change-me")

OAUTH_AUTHORIZE = "https://discord.com/api/oauth2/authorize"
OAUTH_TOKEN = "https://discord.com/api/oauth2/token"
ME_ENDPOINT = "https://discord.com/api/users/@me"

# =============================================================================
# App
# =============================================================================

app = FastAPI(title="Fika XP API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[SITE_ORIGIN],  # front-end origin
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# =============================================================================
# DB
# =============================================================================

_conn = sqlite3.connect(DB_PATH, check_same_thread=False)
_conn.row_factory = sqlite3.Row


def db() -> sqlite3.Connection:
    # simple shared connection
    return _conn


def ensure_schema() -> None:
    c = db().cursor()

    # Users (Discord)
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
          discord_id TEXT PRIMARY KEY,
          username   TEXT,
          avatar     TEXT,
          created_at INTEGER,
          updated_at INTEGER
        )
        """
    )

    # Cookie sessions (optional; we still sign the cookie, this table is for debug/ops)
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS sessions (
          session_id TEXT PRIMARY KEY,
          discord_id TEXT NOT NULL,
          created_at INTEGER,
          expires_at INTEGER
        )
        """
    )

    # Quests catalog
    c.execute(
        """
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
        """
    )

    # Per-user quest state (status-based)
    c.execute(
        """
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
        """
    )

    # XP accounting (summary + ledger)
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS users_levels (
          user_id  TEXT PRIMARY KEY,
          total_xp INTEGER DEFAULT 0
        )
        """
    )
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS user_xp_ledger (
          id      INTEGER PRIMARY KEY AUTOINCREMENT,
          user_id TEXT NOT NULL,
          amount  INTEGER NOT NULL,
          reason  TEXT,
          ts      INTEGER DEFAULT (strftime('%s','now'))
        )
        """
    )

    # Seed quests if empty
    count = c.execute("SELECT COUNT(*) FROM quests").fetchone()[0]
    if count == 0:
        seeds = [
            ("dev-cheat", "Dev Cheat Quest", "Dev-only quest to verify accept/claim flow", "daily", 1, 500, "manual"),
            ("survive-3", "Survive 3 Raids", "Extract with your loot three times today.", "daily", 3, 250, "extracts"),
            ("kill-10", "Eliminate 10 PMCs", "Rack up 10 PMC kills this week.", "weekly", 10, 600, "pmc_kills"),
        ]
        for slug, title, descr, scope, goal, xp, metric in seeds:
            c.execute(
                """
                INSERT OR IGNORE INTO quests(slug,title,descr,scope,goal,xp,metric,active)
                VALUES (?,?,?,?,?,?,?,1)
                """,
                (slug, title, descr, scope, goal, xp, metric),
            )
        db().commit()


ensure_schema()

# =============================================================================
# Session helpers
# =============================================================================


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _b64url_decode(s: str) -> bytes:
    # add padding back
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def sign(value: str) -> str:
    return _b64url(hmac.new(SESSION_SECRET.encode(), value.encode(), hashlib.sha256).digest())


def set_session(resp: Response, payload: dict, max_age: int = 60 * 60 * 24 * 30) -> None:
    raw = json.dumps(payload, separators=(",", ":"))
    enc = _b64url(raw.encode())
    cookie = f"{enc}.{sign(enc)}"
    # secure=False since you’re on http (not https). Change to True if you add TLS.
    resp.set_cookie(
        "sess",
        cookie,
        httponly=True,
        samesite="Lax",
        secure=False,
        max_age=max_age,
        path="/",
    )


def get_session(req: Request) -> dict | None:
    c = req.cookies.get("sess")
    if not c or "." not in c:
        return None
    enc, sig = c.rsplit(".", 1)
    if sign(enc) != sig:
        return None
    try:
        raw = _b64url_decode(enc)
        return json.loads(raw)
    except Exception:
        return None


# =============================================================================
# Small helpers
# =============================================================================


def upsert_user(discord_id: str, username: str | None, avatar: str | None) -> None:
    now = int(time.time())
    c = db().cursor()
    c.execute(
        """
        INSERT INTO users(discord_id, username, avatar, created_at, updated_at)
        VALUES(?,?,?,?,?)
        ON CONFLICT(discord_id) DO UPDATE SET
          username=excluded.username,
          avatar=excluded.avatar,
          updated_at=excluded.updated_at
        """,
        (discord_id, username, avatar, now, now),
    )
    db().commit()


def get_total_xp(user_id: str) -> int:
    c = db().cursor()
    # Prefer summary table if present
    row = c.execute("SELECT total_xp FROM users_levels WHERE user_id=?", (user_id,)).fetchone()
    if row and row["total_xp"] is not None:
        return int(row["total_xp"])

    # Fallback to ledger sum
    row = c.execute("SELECT COALESCE(SUM(amount),0) AS total FROM user_xp_ledger WHERE user_id=?", (user_id,)).fetchone()
    return int(row["total"] or 0)


def grant_xp(user_id: str, amount: int, reason: str) -> None:
    c = db().cursor()
    c.execute(
        "INSERT INTO user_xp_ledger(user_id, amount, reason) VALUES (?,?,?)",
        (user_id, amount, reason),
    )
    # maintain summary
    c.execute(
        """
        INSERT INTO users_levels(user_id, total_xp)
        VALUES (?, ?)
        ON CONFLICT(user_id) DO UPDATE SET total_xp = total_xp + excluded.total_xp
        """,
        (user_id, amount),
    )
    db().commit()


def level_breakdown(total_xp: int) -> dict:
    # simple: 1000 XP per level
    level_cap = 1000
    if total_xp < 0:
        total_xp = 0
    level = total_xp // level_cap + 1
    xp_in_level = total_xp % level_cap
    pct = int(round(100 * xp_in_level / level_cap)) if level_cap else 0
    return {
        "level": level,
        "total_xp": total_xp,
        "xp_in_level": xp_in_level,
        "level_cap": level_cap,
        "progress_pct": pct,
    }


def normalize_redirect_param(request: Request) -> str:
    redir = request.query_params.get("redirect", "/quests.html")
    if not redir.startswith("/"):
        redir = "/" + redir
    return redir


# =============================================================================
# Health / debug
# =============================================================================


@app.get("/api/health")
async def health():
    return {"ok": True}


@app.get("/api/debug/dbcheck")
async def dbcheck():
    c = db().cursor()
    tables = []
    for row in c.execute("SELECT name, sql FROM sqlite_master WHERE type='table' ORDER BY name"):
        tables.append({"name": row["name"], "sql": row["sql"]})
    qcount = c.execute("SELECT COUNT(*) AS n FROM quests").fetchone()["n"]
    return {"db_path": DB_PATH, "tables": tables, "quest_count": qcount}


# =============================================================================
# OAuth (Discord)
# =============================================================================


@app.get("/api/login")
async def oauth_login(request: Request):
    # Optional landing redirect after login (frontend passes ?redirect=/quests.html)
    redir = normalize_redirect_param(request)
    state = quote(redir, safe="")
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
async def oauth_callback(request: Request, code: str, state: str | None = None):
    if not CLIENT_ID or not CLIENT_SECRET:
        raise HTTPException(500, "discord oauth not configured")

    async with httpx.AsyncClient() as http:
        data = {
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT_URI,
        }
        tok = (
            await http.post(
                OAUTH_TOKEN,
                data=data,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=20,
            )
        ).json()

        if "access_token" not in tok:
            raise HTTPException(400, "oauth exchange failed")

        me = (await http.get(ME_ENDPOINT, headers={"Authorization": f"Bearer {tok['access_token']}"}, timeout=20)).json()

    discord_id = str(me.get("id") or "")
    username = me.get("username") or ""
    avatar = me.get("avatar")

    if not discord_id:
        raise HTTPException(400, "discord profile missing id")

    # Upsert user
    upsert_user(discord_id, username, avatar)

    # Set session cookie
    resp = RedirectResponse(url=SITE_ORIGIN + (state or "/quests.html"))
    set_session(resp, {"discord_id": discord_id, "username": username, "avatar": avatar})
    return resp


@app.get("/api/logout")
async def logout(request: Request):
    redir = normalize_redirect_param(request)
    resp = RedirectResponse(url=SITE_ORIGIN + redir)
    resp.delete_cookie("sess", path="/")
    return resp


# =============================================================================
# Session / identity
# =============================================================================


@app.get("/api/me")
async def me(request: Request):
    sess = get_session(request)
    if not sess:
        return {"auth": False}
    return {
        "auth": True,
        "discord_id": sess.get("discord_id"),
        "username": sess.get("username"),
        "avatar": sess.get("avatar"),
    }


# =============================================================================
# Summary / XP
# =============================================================================


@app.get("/api/summary")
async def summary(user_id: str):
    total = get_total_xp(user_id)
    return level_breakdown(total)


# =============================================================================
# Quests listing + user actions
# =============================================================================


@app.get("/api/quests")
async def list_quests(request: Request, scope: str = "all"):
    """
    Returns an array of quests. If logged in, includes user's accepted/completed/claimed flags and progress.
    """
    sess = get_session(request)
    user_id = sess.get("discord_id") if sess else None

    c = db().cursor()
    if scope == "all":
        rows = c.execute("SELECT id, title, descr, scope, goal, xp FROM quests WHERE active=1 ORDER BY id").fetchall()
    else:
        rows = c.execute(
            "SELECT id, title, descr, scope, goal, xp FROM quests WHERE active=1 AND scope=? ORDER BY id",
            (scope,),
        ).fetchall()

    quests = []
    # If user logged in, prefetch their quest states
    uq_map = {}
    if user_id:
        for r in c.execute("SELECT quest_id, status, progress, claimed_at, completed_at, accepted_at FROM user_quests WHERE user_id=?", (user_id,)):
            uq_map[r["quest_id"]] = {
                "status": r["status"],
                "progress": int(r["progress"] or 0),
                "accepted_at": r["accepted_at"],
                "completed_at": r["completed_at"],
                "claimed_at": r["claimed_at"],
            }

    for r in rows:
        qid = int(r["id"])
        st = uq_map.get(qid, None)
        accepted = bool(st and st["status"] in ("accepted", "completed", "claimed"))
        completed = bool(st and st["status"] in ("completed", "claimed"))
        claimed = bool(st and st["status"] == "claimed")
        progress = int(st["progress"]) if st else 0

        quests.append(
            {
                "id": qid,
                "title": r["title"],
                "descr": r["descr"],
                "scope": r["scope"],
                "goal": int(r["goal"] or 0),
                "xp": int(r["xp"] or 0),
                "accepted": accepted,
                "completed": completed,
                "claimed": claimed,
                "progress": progress,
            }
        )

    return quests


@app.post("/api/user/quests/accept")
async def accept_quest(request: Request):
    sess = get_session(request)
    if not sess:
        raise HTTPException(401, "not logged in")
    user_id = str(sess["discord_id"])

    body = await request.json()
    qid = int(body.get("quest_id", 0))
    if not qid:
        raise HTTPException(400, "quest_id required")

    ts = int(time.time())
    c = db().cursor()
    c.execute(
        """
        INSERT INTO user_quests(user_id, quest_id, status, accepted_at, progress)
        VALUES (?,?,?,?,0)
        ON CONFLICT(user_id, quest_id) DO UPDATE SET
          status='accepted',
          accepted_at=excluded.accepted_at
        """,
        (user_id, qid, "accepted", ts),
    )
    db().commit()
    return {"ok": True}


@app.post("/api/user/quests/claim")
async def claim_quest(request: Request):
    sess = get_session(request)
    if not sess:
        raise HTTPException(401, "not logged in")
    user_id = str(sess["discord_id"])

    body = await request.json()
    qid = int(body.get("quest_id", 0))
    if not qid:
        raise HTTPException(400, "quest_id required")

    c = db().cursor()
    q = c.execute("SELECT goal, xp FROM quests WHERE id=?", (qid,)).fetchone()
    if not q:
        raise HTTPException(404, "quest not found")

    uq = c.execute("SELECT status, progress, claimed_at FROM user_quests WHERE user_id=? AND quest_id=?", (user_id, qid)).fetchone()
    if not uq or uq["status"] == "not_accepted":
        raise HTTPException(400, "quest not accepted")
    if uq["status"] == "claimed":
        raise HTTPException(400, "already claimed")
    # Consider completed if progress >= goal
    goal = int(q["goal"] or 0)
    progress = int(uq["progress"] or 0)
    if goal > 0 and progress < goal and uq["status"] != "completed":
        # allow manual claim only if already explicitly marked 'completed'
        raise HTTPException(400, "quest not completed")

    ts = int(time.time())
    c.execute(
        "UPDATE user_quests SET status='claimed', claimed_at=? WHERE user_id=? AND quest_id=?",
        (ts, user_id, qid),
    )
    db().commit()

    grant_xp(user_id, int(q["xp"] or 0), f"claim:quest:{qid}")

    return {"ok": True, "granted": int(q["xp"] or 0), "summary": level_breakdown(get_total_xp(user_id))}


@app.post("/api/user/quests/discard")
async def discard_quest(request: Request):
    sess = get_session(request)
    if not sess:
        raise HTTPException(401, "not logged in")
    user_id = str(sess["discord_id"])

    body = await request.json()
    qid = int(body.get("quest_id", 0))
    if not qid:
        raise HTTPException(400, "quest_id required")

    c = db().cursor()
    c.execute("DELETE FROM user_quests WHERE user_id=? AND quest_id=?", (user_id, qid))
    db().commit()
    return {"ok": True}
