from __future__ import annotations
import os, json, sqlite3, secrets, base64, hmac, hashlib
from urllib.parse import urlencode
import httpx
from fastapi import FastAPI, Request, Response, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse, JSONResponse

# === Config ===
DB_PATH = os.getenv("DB_PATH", "/opt/fika_xp/fika.db")
CLIENT_ID = os.getenv("DISCORD_CLIENT_ID")
CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET")
REDIRECT_URI = os.getenv("DISCORD_REDIRECT_URI", "http://51.222.136.98/xp/callback")
SITE_ORIGIN = os.getenv("SITE_ORIGIN", "http://51.222.136.98")
SECRET = os.getenv("SESSION_SECRET", secrets.token_urlsafe(32))

OAUTH_AUTHORIZE = "https://discord.com/api/oauth2/authorize"
OAUTH_TOKEN = "https://discord.com/api/oauth2/token"
ME_ENDPOINT = "https://discord.com/api/users/@me"

# === App ===
app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=[SITE_ORIGIN],
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=True
)

# === Session Helpers ===
def sign(data: str) -> str:
    return base64.urlsafe_b64encode(
        hmac.new(SECRET.encode(), data.encode(), hashlib.sha256).digest()
    ).decode().rstrip("=")

def set_session(resp: Response, payload: dict):
    raw = json.dumps(payload, separators=(",", ":"))
    cookie = base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")
    resp.set_cookie(
        "sess", cookie+"."+sign(cookie),
        httponly=True, samesite="Lax", secure=False, max_age=60*60*24*30
    )

def get_session(req: Request) -> dict | None:
    c = req.cookies.get("sess")
    if not c or "." not in c: return None
    cookie,sig = c.rsplit(".",1)
    if sign(cookie) != sig: return None
    raw = base64.urlsafe_b64decode(cookie + "=")
    return json.loads(raw)

# === Database ===
conn = sqlite3.connect(DB_PATH, check_same_thread=False)
conn.row_factory = sqlite3.Row

# === OAuth Routes ===
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
        tok = (await http.post(
            OAUTH_TOKEN, data=data,
            headers={"Content-Type":"application/x-www-form-urlencoded"}
        )).json()
        if "access_token" not in tok:
            raise HTTPException(400, "oauth exchange failed")
        me = (await http.get(ME_ENDPOINT, headers={"Authorization": f"Bearer {tok['access_token']}"})).json()
    resp = RedirectResponse(url=SITE_ORIGIN + "/quests.html")
    set_session(resp, {
        "discord_id": me["id"],
        "username": me["username"],
        "avatar": me.get("avatar")
    })
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
        return JSONResponse({"authenticated": False})
    return {"authenticated": True, "user": {
        "id": sess["discord_id"],
        "name": sess["username"],
        "avatar": f"https://cdn.discordapp.com/avatars/{sess['discord_id']}/{sess['avatar']}.png"
                  if sess.get("avatar") else None
    }}

# === Mount quests router ===
from quests_ext import router as quests_router
app.include_router(quests_router, prefix="/xp")
