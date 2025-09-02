# /opt/fika_xp/api/admin_router.py
from fastapi import APIRouter, Request, HTTPException
from typing import Optional, Dict, Any
import os, sqlite3, time

DB_PATH = os.getenv("DB_PATH", "/opt/fika_xp/fika.db")
ADMIN_IDS = set([x.strip() for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()])

router = APIRouter()

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def require_admin(req: Request) -> str:
    sess = req.state.session  # set by middleware (see main.py patch below)
    if not sess:
        raise HTTPException(status_code=401, detail="Not authenticated")
    did = str(sess.get("discord_id") or "")
    if did not in ADMIN_IDS:
        raise HTTPException(status_code=403, detail="Admin only")
    return did

def normalize_bool(v):
    if isinstance(v, bool): return int(v)
    if v is None: return None
    s = str(v).lower().strip()
    return 1 if s in ("1","true","yes","on") else 0 if s in ("0","false","no","off") else None

# ---- Quests CRUD ----
@router.get("/quests")
def admin_list_quests(request: Request, scope: Optional[str] = None):
    require_admin(request)
    conn = db(); cur = conn.cursor()
    if scope and scope != "all":
        cur.execute("SELECT id, slug, title, descr, scope, goal, xp, metric, active FROM quests WHERE scope=? ORDER BY id", (scope,))
    else:
        cur.execute("SELECT id, slug, title, descr, scope, goal, xp, metric, active FROM quests ORDER BY id")
    out = [dict(r) for r in cur.fetchall()]
    conn.close()
    return {"quests": out}

@router.post("/quests")
async def admin_create_quest(request: Request):
    require_admin(request)
    body = await request.json()
    title  = (body.get("title") or "").strip()
    descr  = (body.get("descr") or "").strip()
    scope  = (body.get("scope") or "daily").strip()
    goal   = int(body.get("goal") or 1)
    xp     = int(body.get("xp") or 100)
    metric = (body.get("metric") or "").strip() or None
    active = normalize_bool(body.get("active"))
    if not title: raise HTTPException(400, "title required")
    conn = db(); cur = conn.cursor()
    cur.execute("""INSERT INTO quests(title,descr,scope,goal,xp,metric,active)
                   VALUES(?,?,?,?,?,?,COALESCE(?,1))""",
                (title,descr,scope,goal,xp,metric,active))
    conn.commit()
    qid = cur.lastrowid
    row = conn.execute("SELECT id, slug, title, descr, scope, goal, xp, metric, active FROM quests WHERE id=?", (qid,)).fetchone()
    conn.close()
    return {"ok": True, "quest": dict(row)}

@router.patch("/quests/{quest_id}")
async def admin_update_quest(request: Request, quest_id: int):
    require_admin(request)
    body: Dict[str, Any] = await request.json()
    fields = []
    vals = []
    for key in ("title","descr","scope","goal","xp","metric","active"):
        if key in body:
            if key in ("goal","xp"):
                fields.append(f"{key}=?"); vals.append(int(body[key]))
            elif key == "active":
                fields.append("active=?"); vals.append(normalize_bool(body[key]))
            else:
                fields.append(f"{key}=?"); vals.append(str(body[key]))
    if not fields:
        raise HTTPException(400, "no fields to update")
    vals.append(quest_id)
    conn = db(); cur = conn.cursor()
    cur.execute(f"UPDATE quests SET {', '.join(fields)} WHERE id=?", vals)
    conn.commit()
    row = conn.execute("SELECT id, slug, title, descr, scope, goal, xp, metric, active FROM quests WHERE id=?", (quest_id,)).fetchone()
    conn.close()
    if not row: raise HTTPException(404, "quest not found")
    return {"ok": True, "quest": dict(row)}

@router.delete("/quests/{quest_id}")
def admin_delete_quest(request: Request, quest_id: int):
    require_admin(request)
    conn = db(); cur = conn.cursor()
    cur.execute("DELETE FROM quests WHERE id=?", (quest_id,))
    conn.commit()
    conn.close()
    return {"ok": True, "deleted": quest_id}

# ---- Player resets ----
@router.post("/reset_user")
async def admin_reset_user(request: Request):
    require_admin(request)
    body = await request.json()
    user_id = str(body.get("user_id") or "").strip()  # discord_id
    if not user_id:
        raise HTTPException(400, "user_id (discord id) required")

    conn = db(); cur = conn.cursor()
    # reset XP (your v1 schema keeps user_xp + user_xp_ledger)
    cur.execute("UPDATE user_xp SET total_xp=0 WHERE user_id=?", (user_id,))
    cur.execute("DELETE FROM user_xp_ledger WHERE user_id=?", (user_id,))

    # clear quest state for user
    cur.execute("DELETE FROM user_quests WHERE user_id=?", (user_id,))

    # also zero stats if present (newer schemas)
    cur.execute("""
      UPDATE stats SET xp=0, level=1, kills=0, deaths=0, extracts=0,
                      survivals=0, dogtags=0, playtime_seconds=0
      WHERE player_id IN (SELECT id FROM players WHERE discord_id=?)
    """, (user_id,))
    conn.commit(); conn.close()
    return {"ok": True, "user_id": user_id}
