#!/usr/bin/env python3
import csv, json, os, re, time, glob
from datetime import datetime, timezone, timedelta
from collections import defaultdict

CSV_PATH = "/home/ubuntu/profile-stats.csv"
LOG_PATH = "/home/ubuntu/profile-stats.log"
WEB_ROOT = "/var/www/html"
UPDATE_EVERY_SEC = 60

# Your bot’s regex (verbatim)
STATS_RE = re.compile(
    r"^\[(?P<ts>[\d\-:\s]+) UTC\]\s+(?P<name>.+?)\s+lvl\s+(?P<lvl>\d+)\s+\|\s+raids\s+(?P<raids>\d+)\s+\|\s+survived\s+(?P<survived>\d+)\s+\|\s+deaths\s+(?P<deaths>\d+)\s+\|\s+pmc_kills\s+(?P<pmc_kills>\d+)\s+\|\s+kd\s+(?P<kd>[\d\.]+)\s+\|\s+roubles\s+(?P<roubles>\d+)\s+\|\s+achievements\s+(?P<ach>\d+)\s+\|\s+hideout\s+(?P<hideout>\d+)%\s+\|\s+streak\s+(?P<streak>\d+)\s*$"
)

EXCLUDE_NAMES = {"new", "tiny", "tytytrakov"}  # same idea as your bot; add/remove as needed

def now_utc():
    return datetime.now(timezone.utc)

def parse_csv(path):
    rows_by = defaultdict(list)
    if not os.path.exists(path):
        return rows_by
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        rdr = csv.DictReader(f)
        for r in rdr:
            try:
                ts = datetime.fromisoformat(r["ts_utc"].replace(" UTC", "+00:00"))
                name = (r["name"] or "").strip()
                if not name or name.lower().startswith("headless") or name.lower() in EXCLUDE_NAMES:
                    continue
                rows_by[name].append({
                    "ts": ts,
                    "xp": int(r.get("xp", 0) or 0),
                    "raids": int(r.get("raids", 0) or 0),
                    "deaths": int(r.get("deaths", 0) or 0),
                    "kills": int(r.get("pmc_kills", 0) or 0),
                })
            except Exception:
                pass
    for k in rows_by:
        rows_by[k].sort(key=lambda x: x["ts"])
    return rows_by

def parse_log(path):
    rows_by = defaultdict(list)
    if not os.path.exists(path):
        return rows_by
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            m = STATS_RE.match(line.strip())
            if not m: 
                continue
            name = m.group("name").strip()
            if "headless" in name.lower() or name.lower() in EXCLUDE_NAMES:
                continue
            try:
                ts = datetime.strptime(m.group("ts"), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            except Exception:
                ts = now_utc()
            rows_by[name].append({
                "ts": ts,
                "xp": None,  # log doesn’t have XP
                "raids": int(m.group("raids")),
                "deaths": int(m.group("deaths")),
                "kills": int(m.group("pmc_kills")),
            })
    for k in rows_by:
        rows_by[k].sort(key=lambda x: x["ts"])
    return rows_by

def merge_sources(csv_by, log_by):
    # Prefer CSV when present (has XP), else use log
    names = set(csv_by.keys()) | set(log_by.keys())
    merged = {}
    for n in names:
        if n in csv_by and csv_by[n]:
            merged[n] = csv_by[n]
        else:
            merged[n] = log_by.get(n, [])
    return merged

def latest_before(rows, cutoff):
    best = None
    for r in rows:
        if r["ts"] < cutoff:
            best = r
        else:
            break
    return best

def last_in_range(rows, start, end):
    last = None
    for r in rows:
        if start <= r["ts"] <= end:
            last = r
    return last

def compute_delta(base, end):
    fields = ("xp","raids","deaths","kills")
    out = {}
    for k in fields:
        a = (base or {}).get(k) or 0
        b = (end or  {}).get(k) or 0
        d = max(0, b - a)
        out[k] = d
    return out

def players_for_window(merged_by, start, end, mode):
    players = []
    for name, rows in merged_by.items():
        if not rows:
            continue
        if mode == "absolute":
            end_row = rows[-1]
            xp = end_row["xp"]
            if xp is None:  # no XP values? make a harmless heuristic
                xp = end_row["kills"] * 1000
            data = {
                "xp": xp,
                "raids": end_row["raids"],
                "deaths": end_row["deaths"],
                "kills": end_row["kills"],
            }
        else:
            end_row = last_in_range(rows, start, end)
            if not end_row:
                data = {"xp":0,"raids":0,"deaths":0,"kills":0}
            else:
                base = latest_before(rows, start) or rows[0]
                delta = compute_delta(base, end_row)
                if delta["xp"] == 0 and end_row["xp"] is None:
                    # no XP field => simple heuristic
                    delta["xp"] = delta["kills"] * 1000
                data = delta

        players.append({
            "name": name,
            "raids": data["raids"],
            "kills": data["kills"],
            "deaths": data["deaths"],
            "xp": data["xp"],
            "playtime": "—",
        })
    players.sort(key=lambda p: (p["xp"], p["kills"], -p["deaths"]), reverse=True)
    return players

def write_json(path, payload):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    os.replace(tmp, path)

def cycle_once():
    csv_by = parse_csv(CSV_PATH)
    log_by = parse_log(LOG_PATH)
    merged = merge_sources(csv_by, log_by)

    now = now_utc()
    start_24h = now - timedelta(hours=24)
    start_7d  = now - timedelta(days=7)

    p24 = players_for_window(merged, start_24h, now, mode="delta")
    p7  = players_for_window(merged, start_7d,  now, mode="delta")
    pa  = players_for_window(merged, datetime(1970,1,1,tzinfo=timezone.utc), now, mode="absolute")

    ts = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    write_json(os.path.join(WEB_ROOT,"leaderboard-24h.json"), {"updatedAt": ts, "range":"24h", "players": p24})
    write_json(os.path.join(WEB_ROOT,"leaderboard-7d.json"),  {"updatedAt": ts, "range":"7d",  "players": p7})
    write_json(os.path.join(WEB_ROOT,"leaderboard-all.json"), {"updatedAt": ts, "range":"all", "players": pa})

    # Debug so you can see what it used
    debug = {
        "csv_players": len(csv_by),
        "log_players": len(log_by),
        "merged_players": len(merged),
        "last_csv_ts": max((rows[-1]["ts"].isoformat() for rows in csv_by.values()), default=None),
        "last_log_ts": max((rows[-1]["ts"].isoformat() for rows in log_by.values()), default=None),
        "wrote": ts
    }
    write_json(os.path.join(WEB_ROOT,"leaderboard-debug.json"), debug)

def main():
    while True:
        try:
            cycle_once()
        except Exception as e:
            with open(os.path.join(WEB_ROOT,"profile-writer.error.log"),"a",encoding="utf-8") as f:
                f.write(f"[{now_utc().isoformat()}] {e}\n")
        time.sleep(UPDATE_EVERY_SEC)

if __name__ == "__main__":
    main()
