#!/usr/bin/env python3
import os, re, csv, time
from datetime import datetime, timezone

LOG_PATH = os.environ.get("PROFILE_LOG", "/home/ubuntu/profile-stats.log")
CSV_PATH = os.environ.get("CSV_PATH", "/home/ubuntu/profile-stats.csv")
INTERVAL = int(os.environ.get("CSV_UPDATE_SEC", "30"))

STATS_RE = re.compile(
    r"^\[(?P<ts>[\d\-:\s]+)\sUTC\]\s+(?P<name>.+?)\s+lvl\s+(?P<lvl>\d+)\s+\|\s+raids\s+(?P<raids>\d+)\s+\|\s+survived\s+(?P<survived>\d+)\s+\|\s+deaths\s+(?P<deaths>\d+)\s+\|\s+pmc_kills\s+(?P<pmc_kills>\d+)\s+\|\s+kd\s+(?P<kd>[\d\.]+)\s+\|\s+roubles\s+(?P<roubles>\d+)\s+\|\s+achievements\s+(?P<ach>\d+)\s+\|\s+hideout\s+(?P<hideout>\d+)%\s+\|\s+streak\s+(?P<streak>\d+)\s*$"
)

HEADERS = ["ts_utc","name","lvl","raids","survived","deaths","pmc_kills","kd","roubles","achievements","hideout","streak","xp"]

def parse_all():
    out = []
    try:
        with open(LOG_PATH, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                m = STATS_RE.match(line.strip())
                if not m: 
                    continue
                ts = m.group("ts").strip() + " UTC"  # ISO-ish with UTC suffix
                name = (m.group("name") or "").strip()
                lvl = int(m.group("lvl"))
                raids = int(m.group("raids"))
                survived = int(m.group("survived"))
                deaths = int(m.group("deaths"))
                pmc_kills = int(m.group("pmc_kills"))
                kd = float(m.group("kd"))
                roubles = int(m.group("roubles"))
                ach = int(m.group("ach"))
                hideout = int(m.group("hideout"))
                streak = int(m.group("streak"))
                # If you later have a real XP value, compute it there; for now, heuristic:
                xp = pmc_kills * 1000
                out.append([ts,name,lvl,raids,survived,deaths,pmc_kills,kd,roubles,ach,hideout,streak,xp])
    except FileNotFoundError:
        pass
    return out

def write_csv(rows):
    tmp = CSV_PATH + ".tmp"
    os.makedirs(os.path.dirname(CSV_PATH), exist_ok=True)
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(HEADERS)
        w.writerows(rows)
    os.replace(tmp, CSV_PATH)

def main():
    while True:
        try:
            rows = parse_all()
            write_csv(rows)
        except Exception as e:
            try:
                with open(os.path.join("/var/www/html","profile-stats-csv.error.log"),"a",encoding="utf-8") as ef:
                    ef.write(f"[{datetime.now(timezone.utc).isoformat()}] {e}\n")
            except Exception:
                pass
        time.sleep(INTERVAL)

if __name__ == "__main__":
    main()
