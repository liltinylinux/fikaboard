from __future__ import annotations
import os
from dataclasses import dataclass
from dotenv import load_dotenv
load_dotenv()

@dataclass
class Config:
    db_path: str = os.getenv("DB_PATH", "./fika.db")
    log_file: str = os.getenv("LOG_FILE", "./server.log")
    tz: str = os.getenv("TIMEZONE", "America/New_York")

CFG = Config()
