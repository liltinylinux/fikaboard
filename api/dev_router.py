import os, re, sqlite3, json
from fastapi import APIRouter, Request, HTTPException
from pydantic import BaseModel

DB_PATH = os.environ.get("DB_PATH", "/opt/fika_xp/fika.db")
DEV_ID   = os.environ.get("DEV_DISCORD_ID", "")

router = APIRouter(prefix="/api", tags=["dev"])

def _db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def _find_uid_in_any_cookie(request: Request) -> str | None:
    # Try to extract an 17–19 digit Discord ID anywhere in common cookies
    possible = [request.cookies.get(n,"") for n in ("fika_session","fika_sess","sess","session")]
    joined = " ".join([p for p in possible if p])
    # if JSON, try parse
    for raw in possible:
        try:
            obj = json.loads(raw) if raw and raw.strip().startswith("{") else None
            if obj and "user" in obj and "id" in obj["user"]:
                return str(obj["user"]["id"])
        except Exception:
            pass
    m = re.search(r"\b(\d{17,19})\b", joined)
    return m.group(1) if m else None

def _level_from_total(total: int):
    # Level 1 starts at 0 XP. Next level requires 1000 xp, then +500 each level.
    # Return: level, xp_in_level, xp_to_next
    need = 1000
    lvl  = 1
    t    = int(total or 0)
    while t >= need:
        t   -= need
        lvl += 1
        need += 500
    return {"level": lvl, "in_level": t, "to_next": need - t, "next_req": need}

def _sum_total_xp(conn, user_id: str) -> int:
    # Table created by installer: user_xp_ledger(user_id TEXT, delta_xp INT, created_at DATETIME DEFAULT CURRENT_TIMESTAMP)
    cur = conn.execute("SELECT COALESCE(SUM(delta_xp),0) AS s FROM user_xp_ledger WHERE user_id=?", (user_id,))
    return int(cur.fetchone()["s"] or 0)

@router.get("/dev/xp")
def dev_xp(request: Request):
    uid = _find_uid_in_any_cookie(request)
    if not uid:
        return {"authenticated": False}
    dev = (uid == DEV_ID)
    with _db() as conn:
        total = _sum_total_xp(conn, uid)
    curve = _level_from_total(total)
    return {
        "authenticated": True,
        "user_id": uid,
        "developer": dev,
        "xp_total": total,
        **curve
    }

class GrantBody(BaseModel):
    amount: int = 10000
    reason: str | None = "dev:grant"

@router.post("/dev/grant_xp")
def dev_grant_xp(body: GrantBody, request: Request):
    uid = _find_uid_in_any_cookie(request)
    if not uid or uid != DEV_ID:
        raise HTTPException(status_code=403, detail="dev only")
    amt = int(body.amount or 0)
    if amt == 0:
        return {"ok": True, "xp_total": _sum_total_xp(_db(), uid)}
    with _db() as conn:
        conn.execute(
            "INSERT INTO user_xp_ledger(user_id, source, delta_xp) VALUES(?,?,?)",
            (uid, body.reason or "dev:grant", amt),
        )
        conn.commit()
        total = _sum_total_xp(conn, uid)
    return {"ok": True, "granted": amt, "xp_total": total, **_level_from_total(total)}
