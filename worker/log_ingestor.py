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
