from __future__ import annotations

def xp_for_level(level: int) -> int:
    if level <= 1:
        return 0
    return 100 * (level - 1) * (level - 1) + 100

def level_from_xp(xp: int) -> int:
    lvl = 1
    while xp >= xp_for_level(lvl + 1):
        lvl += 1
    return lvl

DEFAULT_EVENT_XP = {
    "KILL": 100,
    "HEADSHOT": 25,
    "SURVIVE": 150,
    "EXTRACT": 75,
    "DOGTAG": 30,
    "DEATH": 0,
}

def xp_for_event(event_type: str, data: dict | None = None) -> int:
    return DEFAULT_EVENT_XP.get(event_type.upper(), 0)
