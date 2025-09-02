#!/usr/bin/env python3
import os, re, json, time, csv
from datetime import datetime, timezone, timedelta
from collections import defaultdict

CSV_PATH = os.environ.get("CSV_PATH", "/home/ubuntu/profile-stats.csv")  # preferred
LOG_PATH = os.environ.get("PROFILE_LOG", "/home/ubuntu/profile-stats.log")  # fallback
WEB_ROOT = os.environ.get("WEB_ROOT", "/var/www/html")
UPDATE_EVERY_SEC = int(os.environ.get("UPDATE_EVERY_SEC", "60"))

EXCLUDE_NAMES = {n.strip().lower() for n in os.environ.get("EXCLUDE_NAMES", "new,tiny,tytytrakov").split(",") if n.strip()}

STATS_RE = re.compile(
    r"^\[(?P<ts>[\d\-:\s]+)\sUTC\]\s+(?P<name>.+?)\s+lvl\s+(?P<lvl>\d+)\s+\|\s+raids\s+(?P<raids>\d+)\s+\|\s+survived\s+(?P<survived>\d+)\s+\|\s+deaths\s+(?P<deaths>\d+)\s+\|\s+pmc_kills\s+(?P<pmc_kills>\d+)\s+\|\s+kd\s+(?P<kd>[\d\.]+)\s+\|\s+roubles\s+(?P<roubles>\d+)\s+\|\s+achievements\s+(?P<ach>\d+)\s+\|\s+hideout\s+(?P<hideout>\d+)%\s+\|\s+streak\s+(?P<streak>\d+)\s*$"
)

def now_utc():
    return datetime.now(timezone.utc)

def parse_csv(path):
    by = defaultdict(list)
    if not os.path.exists(path):
        return by
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        rdr = csv.DictReader(f)
        for r in rdr:
            name = (r.get("name") or "").strip()
            if not name or name.lower() in EXCLUDE_NAMES or name.lower().startswith("headless"):
                continue
            try:
                ts = datetime.fromisoformat((r.get("ts_utc") or "").replace(" UTC","+00:00"))
            except Exception:
                continue
            def _i(k): 
                v = r.get(k); 
                try: return int(v)
                except: return 0
            def _f(k):
                v = r.get(k); 
                try: return float(v)
                except: return 0.0
            by[name].append({
                "ts": ts,
                "lvl": _i("lvl"),
                "raids": _i("raids"),
                "survived": _i("survived"),
                "deaths": _i("deaths"),
                "kills": _i("pmc_kills"),
                "kd": _f("kd"),
                "roubles": _i("roubles"),
                "achievements": _i("achievements"),
                "hideout": _i("hideout"),
                "streak": _i("streak"),
                "xp": _i("xp"),
            })
    for k in list(by.keys()):
        by[k].sort(key=lambda r: r["ts"])
    return by

def parse_log(path):
    by = defaultdict(list)
    if not os.path.exists(path):
        return by
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            m = STATS_RE.match(line.strip())
            if not m: 
                continue
            name = (m.group("name") or "").strip()
            lname = name.lower()
            if not name or "headless" in lname or lname in EXCLUDE_NAMES:
                continue
            try:
                ts = datetime.strptime(m.group("ts"), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            except Exception:
                ts = now_utc()
            row = {
                "ts": ts,
                "lvl": int(m.group("lvl")),
                "raids": int(m.group("raids")),
                "survived": int(m.group("survived")),
                "deaths": int(m.group("deaths")),
                "kills": int(m.group("pmc_kills")),
                "kd": float(m.group("kd")),
                "roubles": int(m.group("roubles")),
                "achievements": int(m.group("ach")),
                "hideout": int(m.group("hideout")),
                "streak": int(m.group("streak")),
                "xp": None,
            }
            by[name].append(row)
    for k in list(by.keys()):
        by[k].sort(key=lambda r: r["ts"])
    return by

def merge_sources(csv_by, log_by):
    names = set(csv_by.keys()) | set(log_by.keys())
    out = {}
    for n in names:
        if csv_by.get(n):
            out[n] = csv_by[n]
        else:
            out[n] = log_by.get(n, [])
    return out

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
    fields = ("raids","deaths","kills","xp")
    out = {}
    for k in fields:
        a = (base or {}).get(k) or 0
        b = (end  or {}).get(k) or 0
        d = max(0, b - a)
        out[k] = d
    # if xp missing in both rows (log fallback), derive from kills
    if ((base or {}).get("xp") is None and (end or {}).get("xp") is None):
        out["xp"] = out.get("kills",0) * 1000
    return out

def players_for_window(by, start, end, mode):
    players = []
    for name, rows in by.items():
        if not rows: 
            continue
        if mode == "absolute":
            end_row = rows[-1]
            xp = end_row.get("xp")
            if xp is None:
                xp = end_row.get("kills",0) * 1000
            data = {
                "raids": end_row.get("raids",0),
                "kills": end_row.get("kills",0),
                "deaths": end_row.get("deaths",0),
                "xp": xp,
            }
        else:
            end_row = last_in_range(rows, start, end)
            if not end_row:
                data = {"raids":0,"kills":0,"deaths":0,"xp":0}
            else:
                base = latest_before(rows, start) or rows[0]
                data = compute_delta(base, end_row)

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

    now = datetime.now(timezone.utc)
    p24 = players_for_window(merged, now - timedelta(hours=24), now, mode="delta")
    p7  = players_for_window(merged, now - timedelta(days=7),  now, mode="delta")
    pa  = players_for_window(merged, datetime(1970,1,1,tzinfo=timezone.utc), now, mode="absolute")

    ts = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    write_json(os.path.join(WEB_ROOT,"leaderboard-24h.json"), {"updatedAt": ts, "range":"24h", "players": p24})
    write_json(os.path.join(WEB_ROOT,"leaderboard-7d.json"),  {"updatedAt": ts, "range":"7d",  "players": p7})
    write_json(os.path.join(WEB_ROOT,"leaderboard-all.json"), {"updatedAt": ts, "range":"all", "players": pa})

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
            with open(os.path.join(WEB_ROOT,"leaderboard-writer.error.log"),"a",encoding="utf-8") as f:
                f.write(f"[{datetime.now(timezone.utc).isoformat()}] {e}\n")
        time.sleep(int(os.environ.get("UPDATE_EVERY_SEC","60")))

if __name__ == "__main__":
    main()
