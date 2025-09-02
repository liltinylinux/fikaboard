#!/usr/bin/env bash
set -euo pipefail
DB=${1:-/opt/fika_xp/fika.db}
STAMP=$(date -u +%Y%m%dT%H%M%SZ)

echo "== Migrating quests table in $DB ($STAMP) =="
cp -a "$DB" "${DB}.bak.$STAMP"
echo "[OK] Backup: ${DB}.bak.$STAMP"

# helpers
have_tbl(){ sqlite3 "$DB" "SELECT 1 FROM sqlite_master WHERE type='table' AND name='$1';" | grep -q 1; }
have_col(){ sqlite3 "$DB" "PRAGMA table_info($1);" | awk -F'|' '{print $2}' | grep -qx "$2"; }

if ! have_tbl quests; then
  echo "[!!] No quests table found in $DB"; exit 1
fi

# Ensure 'descr' column exists; copy from "desc" if it exists.
if ! have_col quests descr; then
  echo "[..] Adding column quests.descr"
  sqlite3 "$DB" "ALTER TABLE quests ADD COLUMN descr TEXT;"
  if have_col quests desc; then
    echo "[..] Copying data from quests.\"desc\" -> quests.descr"
    sqlite3 "$DB" "UPDATE quests SET descr = COALESCE(descr, \"desc\");"
  fi
fi

# Ensure 'metric' exists.
if ! have_col quests metric; then
  echo "[..] Adding column quests.metric"
  sqlite3 "$DB" "ALTER TABLE quests ADD COLUMN metric TEXT;"
  echo "[..] Filling metric heuristically"
  sqlite3 "$DB" "UPDATE quests SET metric = CASE
      WHEN lower(title) LIKE '%dogtag%' THEN 'dogtags'
      WHEN lower(title) LIKE '%survive%' OR lower(title) LIKE '%extract%' THEN 'extracts'
      WHEN lower(title) LIKE '%pmc%' OR lower(title) LIKE '%eliminate%' THEN 'pmc_kills'
      ELSE 'pmc_kills' END
    WHERE metric IS NULL OR metric='';"
fi

# Seed defaults (idempotent)
echo "[..] Seeding default quests (INSERT OR IGNORE)"
sqlite3 "$DB" "
INSERT OR IGNORE INTO quests(id,title,descr,scope,goal,xp,metric) VALUES
('q_dogtags_5','Collect 5 PMC Dogtags','Find and secure five PMC dogtags in any raids this week.','weekly',5,500,'dogtags'),
('q_extract_3','Survive 3 Raids','Extract with your loot three times today.','daily',3,250,'extracts'),
('q_pmc_10','Eliminate 10 PMCs','Any map. Any weapon. No mercy.','weekly',10,900,'pmc_kills');
"

echo "[OK] Migration complete. Current schema:"
sqlite3 "$DB" ".schema quests"
