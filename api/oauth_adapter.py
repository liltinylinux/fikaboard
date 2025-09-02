from __future__ import annotations
import os, re, json, time, base64
from urllib.parse import urlencode
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import RedirectResponse

SITE_ORIGIN = os.getenv("SITE_ORIGIN", "http://localhost")
CLIENT_ID   = os.getenv("DISCORD_CLIENT_ID", "")
REDIRECT_URI= os.getenv("DISCORD_REDIRECT_URI", f"{SITE_ORIGIN.rstrip('/')}/api/callback")

def _b64url_pack(obj: dict) -> str:
    raw = json.dumps(obj, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

def _b64url_unpack(s: str, default_next: str = "/") -> str:
    try:
        pad = "=" * (-len(s) % 4)
        data = base64.urlsafe_b64decode(s + pad)
        payload = json.loads(data.decode("utf-8"))
        if abs(time.time() - int(payload.get("ts", 0))) > 3600:
            return default_next
        return payload.get("next") or default_next
    except Exception:
        return default_next

class OAuthCompatMiddleware(BaseHTTPMiddleware):
    """
    - GET /api/login -> 307 to Discord with state carrying ?redirect=...
    - On /api/callback:
        * keep the original response object (so Set-Cookie survives)
        * on HTTP, remove 'Secure'
        * ensure 'Path=/' and 'SameSite=Lax'
        * if state has next=..., set status_code=307 and Location to it
    """
    async def dispatch(self, request, call_next):
        path   = request.url.path
        method = request.method.upper()

        if method == "GET" and path == "/api/login":
            redirect_to = request.query_params.get("redirect", "/")
            state = _b64url_pack({"next": redirect_to, "ts": int(time.time())})
            params = {
                "client_id": CLIENT_ID,
                "redirect_uri": REDIRECT_URI,
                "response_type": "code",
                "scope": "identify",
                "state": state,
                "prompt": "consent",
            }
            url = "https://discord.com/api/oauth2/authorize?" + urlencode(params)
            return RedirectResponse(url, status_code=307)

        # let the app handle it
        response = await call_next(request)

        if path == "/api/callback":
            is_https = SITE_ORIGIN.lower().startswith("https://")
            # rewrite every Set-Cookie in-place on the SAME response
            new_raw = []
            for k, v in response.raw_headers:
                if k.lower() == b"set-cookie":
                    s = v.decode("latin1")
                    if not is_https:
                        s = re.sub(r";\s*Secure\b", "", s, flags=re.I)
                    if "Path=" not in s:
                        s += "; Path=/"
                    # Lax works for top-level GET redirects (our case)
                    if "SameSite=" not in s:
                        s += "; SameSite=Lax"
                    new_raw.append((k, s.encode("latin1")))
                else:
                    new_raw.append((k, v))
            response.raw_headers = new_raw

            # if state present, force client redirect but KEEP cookies
            state_q = request.query_params.get("state")
            if state_q:
                next_path = _b64url_unpack(state_q, "/")
                response.status_code = 307
                # headers is a CIMultiDict; set Location on the same response
                response.headers["Location"] = next_path

        return response
