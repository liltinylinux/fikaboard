PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS users(
  discord_id TEXT PRIMARY KEY,
  display_name TEXT,
  avatar_url TEXT,
  created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS profile_links(
  discord_id TEXT PRIMARY KEY,
  nickname   TEXT UNIQUE,
  linked_at  TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS quests(
  id     TEXT PRIMARY KEY,
  title  TEXT NOT NULL,
  descr  TEXT NOT NULL,
  scope  TEXT NOT NULL CHECK(scope IN ('daily','weekly')),
  goal   INTEGER NOT NULL,
  xp     INTEGER NOT NULL,
  metric TEXT NOT NULL CHECK(metric IN ('pmc_kills','dogtags','extracts'))
);

CREATE TABLE IF NOT EXISTS user_quests(
  discord_id TEXT NOT NULL,
  quest_id   TEXT NOT NULL,
  status     TEXT NOT NULL CHECK(status IN ('accepted','claimed')),
  accepted_at TEXT DEFAULT (datetime('now')),
  claimed_at  TEXT,
  baseline_json TEXT NOT NULL,
  PRIMARY KEY(discord_id, quest_id),
  FOREIGN KEY(quest_id) REFERENCES quests(id)
);

CREATE TABLE IF NOT EXISTS xp_ledger(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  discord_id TEXT NOT NULL,
  xp INTEGER NOT NULL,
  reason TEXT,
  ts TEXT DEFAULT (datetime('now'))
);

-- live counters keyed by in-game nickname
CREATE TABLE IF NOT EXISTS player_counters(
  nickname TEXT PRIMARY KEY,
  pmc_kills INTEGER NOT NULL DEFAULT 0,
  dogtags INTEGER NOT NULL DEFAULT 0,
  extracts INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT DEFAULT (datetime('now'))
);

-- seed default quests if missing
INSERT OR IGNORE INTO quests(id,title,descr,scope,goal,xp,metric) VALUES
('q_dogtags_5', 'Collect 5 PMC Dogtags', 'Find and secure five PMC dogtags in any raids this week.', 'weekly', 5, 500, 'dogtags'),
('q_extract_3', 'Survive 3 Raids', 'Extract with your loot three times today.', 'daily', 3, 250, 'extracts'),
('q_pmc_10', 'Eliminate 10 PMCs', 'Any map. Any weapon. No mercy.', 'weekly', 10, 900, 'pmc_kills');
