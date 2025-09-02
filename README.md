1) python3 -m venv venv && source venv/bin/activate && pip install -r requirements.txt
2) sqlite3 /opt/fika_xp/fika.db < shared/schema.sql
3) sqlite3 /opt/fika_xp/fika.db "SELECT 1 FROM pragma_table_info('players') WHERE name='eligible';" | grep -q 1 || sqlite3 /opt/fika_xp/fika.db "ALTER TABLE players ADD COLUMN eligible INTEGER NOT NULL DEFAULT 0;"
4) Adjust worker/rules.yaml to your real log lines.
5) Start worker: python -m worker.log_ingestor
6) Run the bot:  python -m bot.main_example
7) Set up systemd units from deploy/systemd/*.service when ready.
