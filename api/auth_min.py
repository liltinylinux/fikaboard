import os, time, json, base64, secrets, sqlite3, urllib.parse
import httpx
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import RedirectResponse

DB_PATH = os.getenv("DB_PATH", "/opt/fika_xp/fika.db")
SITE_ORIGIN = os.getenv("SITE_ORIGIN", "http://127.0.0.1")
CLIENT_ID = os.getenv("DISCORD_CLIENT_ID")
CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET")
REDIRECT_URI = os.getenv("DISCORD_REDIRECT_URI", f"{SITE_ORIGIN}/api/callback")
SESSION_COOKIE = "fika_session"

router = APIRouter()

def _db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def _cookie_flags():
    secure = SITE_ORIGIN.startswith("https://")
    # HttpOnly + SameSite=Lax lets cookies work over http for dev, and safe-ish in prod
    return dict(httponly=True, samesite="lax", secure=secure, path="/", max_age=60*60*24*30)

def _now(): return int(time.time())

def _get_session(req: Request):
    sid = req.cookies.get(SESSION_COOKIE)
    if not sid: return None
    with _db() as c:
        row = c.execute(
            "SELECT s.session_id, s.discord_id, u.username, u.avatar "
            "FROM sessions s JOIN users u ON u.discord_id=s.discord_id "
            "WHERE s.session_id=? AND s.expires_at>? ",
            (sid, _now())
        ).fetchone()
        return dict(row) if row else None

def _create_session(discord_id: str):
    sid = secrets.token_urlsafe(32)
    now = _now()
    exp = now + 60*60*24*30
    with _db() as c:
        c.execute(
            "INSERT OR REPLACE INTO sessions(session_id, discord_id, created_at, expires_at) VALUES(?,?,?,?)",
            (sid, discord_id, now, exp)
        )
    return sid

async def _exchange_code_for_user(code: str):
    async with httpx.AsyncClient(timeout=10) as cl:
        tok = await cl.post(
            "https://discord.com/api/oauth2/token",
            data={
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": REDIRECT_URI,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        token = tok.json()
        if "access_token" not in token:
            raise HTTPException(400, "oauth exchange failed")
        user = (await cl.get(
            "https://discord.com/api/users/@me",
            headers={"Authorization": f"Bearer {token['access_token']}"}
        )).json()
    return user

def _upsert_user(user):
    did = user["id"]
    username = f"{user.get('global_name') or user.get('username')}"
    avatar = user.get("avatar") or ""
    now = _now()
    with _db() as c:
        c.execute(
            "INSERT INTO users(discord_id, username, avatar, created_at, updated_at) "
            "VALUES(?,?,?,?,?) "
            "ON CONFLICT(discord_id) DO UPDATE SET username=excluded.username, avatar=excluded.avatar, updated_at=excluded.updated_at",
            (did, username, avatar, now, now)
        )
    return did

def _auth_url(state: str):
    params = {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "scope": "identify",
        "redirect_uri": REDIRECT_URI,
        "state": state,
        "prompt": "consent",
    }
    return "https://discord.com/oauth2/authorize?" + urllib.parse.urlencode(params)

@router.get("/api/login")
async def login(redirect: str = "/quests.html"):
    state = base64.urlsafe_b64encode(json.dumps({"redirect": redirect}).encode()).decode()
    return RedirectResponse(_auth_url(state), status_code=307)

@router.get("/api/callback")
async def callback(code: str, state: str | None = None):
    target = "/quests.html"
    if state:
        try:
            data = json.loads(base64.urlsafe_b64decode(state + "==").decode())
            if isinstance(data, dict) and isinstance(data.get("redirect"), str):
                tgt = data["redirect"]
                if tgt.startswith("/"):
                    target = tgt
        except Exception:
            pass

    user = await _exchange_code_for_user(code)
    did = _upsert_user(user)
    sid = _create_session(did)

    resp = RedirectResponse(target, status_code=303)
    resp.set_cookie(SESSION_COOKIE, sid, **_cookie_flags())
    return resp

@router.get("/api/logout")
async def logout():
    resp = RedirectResponse("/quests.html", status_code=303)
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp

@router.get("/api/me")
async def me(req: Request):
    s = _get_session(req)
    if not s:
        return {"authenticated": False}
    with _db() as c:
        link = c.execute(
            "SELECT profile_name FROM profile_links WHERE discord_id=?",
            (s["discord_id"],)
        ).fetchone()
    return {
        "authenticated": True,
        "user": {"id": s["discord_id"], "name": s["username"], "avatar": s["avatar"]},
        "link": {"profile_name": (link["profile_name"] if link else None)},
    }

@router.post("/api/link_name")
async def link_name(req: Request):
    s = _get_session(req)
    if not s:
        raise HTTPException(401, "not logged in")
    body = await req.json()
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "name required")
    now = _now()
    with _db() as c:
        taken = c.execute(
            "SELECT discord_id FROM profile_links WHERE lower(profile_name)=lower(?) AND discord_id<>?",
            (name, s["discord_id"])
        ).fetchone()
        if taken:
            raise HTTPException(409, "that profile name is linked to another account")
        c.execute(
            "INSERT INTO profile_links(discord_id, profile_name, created_at) "
            "VALUES(?,?,?) "
            "ON CONFLICT(discord_id) DO UPDATE SET profile_name=excluded.profile_name",
            (s["discord_id"], name, now)
        )
    return {"ok": True, "profile_name": name}
