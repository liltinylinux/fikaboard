# worker/parsers.py
from __future__ import annotations

import re
import yaml
from dataclasses import dataclass
from datetime import datetime, timezone, date
from typing import Any, Dict, List, Optional

# ==========================
# Event model
# ==========================
@dataclass
class Event:
    ts: datetime            # timezone-aware UTC timestamp
    type: str               # e.g. "KILL", "HEADSHOT", "DEATH", "SURVIVE", "EXTRACT", "DOGTAG", ...
    game_name: str          # actor name associated with the event (killer, survivor, etc.)
    data: Dict[str, Any]    # extra fields (victim, map, headshot=True, etc.)


# ==========================
# Parser
# ==========================
class LineParsers:
    """
    Reads regex patterns from a YAML rules file and emits one or more Events per log line.

    YAML schema (example):
      patterns:
        KILL:    '(?P<ts>\\d{4}-\\d{2}-\\d{2}T\\d{2}:\\d{2}:\\d{2}).*?KILL.*?killer=(?P<killer>\\S+).*?victim=(?P<victim>\\S+).*?(?P<headshot>HEADSHOT)?'
        DEATH:   '(?P<ts>...) ... (?P<killer>...) (?P<victim>...)'
        EXTRACT: '(?P<ts>...) ... (?P<name>...)'
        SURVIVE: '(?P<ts>...) ... (?P<name>...)'
        DOGTAG:  '(?P<ts>...) ... (?P<name>...) (?:victim=(?P<victim>\\S+))? (?:level=(?P<level>\\d+))?'

      headshot_keywords: ['HEADSHOT', 'HS']  # optional, default provided below
    """

    DEFAULT_HEADSHOT_KEYWORDS = ["HEADSHOT", "HS"]

    def __init__(self, rules_file: str):
        with open(rules_file, "r", encoding="utf-8") as f:
            self.cfg = yaml.safe_load(f) or {}

        # Compile all patterns (case-sensitive by default to avoid false-positives; change to IGNORECASE if you like)
        patterns_cfg = (self.cfg.get("patterns") or {})
        self.patterns: Dict[str, re.Pattern] = {
            k.upper(): re.compile(v) for k, v in patterns_cfg.items() if isinstance(v, str)
        }

        # Optional headshot keywords list
        self.headshot_keywords: List[str] = list(
            self.cfg.get("headshot_keywords") or self.DEFAULT_HEADSHOT_KEYWORDS
        )

    # ------------- helpers -------------
    @staticmethod
    def _to_utc(dt: datetime) -> datetime:
        """Ensure the datetime is timezone-aware in UTC."""
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)

    def _parse_ts(self, ts_raw: Optional[str]) -> datetime:
        """
        Parse a timestamp string from the log into a tz-aware UTC datetime.
        Supports:
          - 'YYYY-MM-DDTHH:MM:SS'
          - 'YYYY-MM-DD HH:MM:SS'
          - 'HH:MM:SS'  (assumes today's UTC date)
        Falls back to now() UTC.
        """
        if not ts_raw:
            return datetime.now(timezone.utc)

        # Try full datetime first
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
            try:
                return self._to_utc(datetime.strptime(ts_raw, fmt))
            except Exception:
                pass

        # Try time-only and merge with today's date (UTC)
        for fmt in ("%H:%M:%S",):
            try:
                t = datetime.strptime(ts_raw, fmt).time()
                today = date.today()
                return datetime(
                    today.year, today.month, today.day, t.hour, t.minute, t.second, tzinfo=timezone.utc
                )
            except Exception:
                pass

        # Fallback
        return datetime.now(timezone.utc)

    def _contains_headshot(self, line: str, groups: Dict[str, Any]) -> bool:
        """
        Determine if the line indicates a headshot.
        Checks:
          - explicit named group like (?P<headshot>...) capturing something truthy
          - presence of configured keywords in the raw line
        """
        # Named group signal
        hs = groups.get("headshot") or groups.get("hs")
        if isinstance(hs, str) and hs.strip():
            return True
        # Raw line keywords
        upper_line = line.upper()
        return any(kw.upper() in upper_line for kw in self.headshot_keywords)

    # ------------- public API -------------
    def parse(self, line: str) -> List[Event]:
        """
        NEW behavior: returns a list of Events (may be empty).
        This enables multi-award per line (e.g. KILL + HEADSHOT).
        """
        events: List[Event] = []
        # Track duplicates within the same line
        seen: set[tuple] = set()

        for etype, rx in self.patterns.items():
            m = rx.search(line)
            if not m:
                continue

            gd = m.groupdict()
            ts = self._parse_ts(gd.get("ts"))

            # Normalize common fields
            killer = (gd.get("killer") or "").strip()
            victim = (gd.get("victim") or "").strip()
            name   = (gd.get("name")   or "").strip()

            # Build events by type
            if etype == "KILL":
                # Base kill event
                ev = Event(ts, "KILL", killer, {"victim": victim})
                key = ("KILL", ts, killer, victim)
                if key not in seen:
                    events.append(ev)
                    seen.add(key)

                # Bonus HEADSHOT if indicated
                if self._contains_headshot(line, gd):
                    hs_ev = Event(ts, "HEADSHOT", killer, {"victim": victim})
                    hs_key = ("HEADSHOT", ts, killer, victim)
                    if hs_key not in seen:
                        events.append(hs_ev)
                        seen.add(hs_key)

            elif etype == "DEATH":
                # Death for the victim; killer included in data for context
                ev = Event(ts, "DEATH", victim, {"killer": killer})
                key = ("DEATH", ts, victim, killer)
                if key not in seen:
                    events.append(ev)
                    seen.add(key)

            elif etype in ("SURVIVE", "EXTRACT"):
                # Treat both as survival-related; if you want both awards, emit both
                base = name
                # Primary event using the matched type
                ev = Event(ts, etype, base, {})
                key = (etype, ts, base, "")
                if key not in seen:
                    events.append(ev)
                    seen.add(key)

                # Optional: also emit a canonical SURVIVE when we match EXTRACT
                if etype == "EXTRACT":
                    also = Event(ts, "SURVIVE", base, {"from": "EXTRACT"})
                    also_key = ("SURVIVE", ts, base, "EXTRACT")
                    if also_key not in seen:
                        events.append(also)
                        seen.add(also_key)

            elif etype == "DOGTAG":
                # Dogtag pickup; include any fields the pattern provided
                payload: Dict[str, Any] = {}
                for k in ("victim", "level", "side", "weapon", "status"):
                    v = gd.get(k)
                    if v is not None and f"{v}".strip() != "":
                        payload[k] = v
                ev = Event(ts, "DOGTAG", name or killer or victim, payload)
                key = ("DOGTAG", ts, ev.game_name, payload.get("victim", ""))
                if key not in seen:
                    events.append(ev)
                    seen.add(key)

            else:
                # Generic passthrough for any custom types in rules.yaml
                ev = Event(ts, etype, name or killer or victim, {k: v for k, v in gd.items() if v is not None})
                key = (etype, ts, ev.game_name, tuple(sorted(ev.data.items())))
                if key not in seen:
                    events.append(ev)
                    seen.add(key)

        return events

    # Back-compat: old code expects Optional[Event]
    def parse_one(self, line: str) -> Optional[Event]:
        """
        Legacy wrapper: return only the first Event (or None).
        Prefer using parse() which returns a list of Events.
        """
        evs = self.parse(line)
        return evs[0] if evs else None
