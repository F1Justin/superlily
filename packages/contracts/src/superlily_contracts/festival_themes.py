"""Fixed, reviewed festival windows through Lantern Festival 2027 (UTC+08:00)."""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json

CST = timezone(timedelta(hours=8), name="Asia/Shanghai")
# Lunar dates and winter solstice: Hong Kong Observatory 2026/2027 calendars.
SCHEDULE = (
    ("2026-09-25", "mid_autumn"),
    ("2026-10-01", "national_day"),
    ("2026-10-18", "double_ninth"),
    ("2026-12-22", "winter_solstice"),
    ("2027-01-01", "new_year"),
    ("2027-02-05", "new_years_eve"),
    ("2027-02-06", "new_years_day"),
    ("2027-02-20", "lantern_festival"),
)
# Background, body, headings. Decorations contain no text.
PALETTES = {
    "default": ("FFFFFF", "000000", "000000"),
    "mid_autumn": ("E9E0EF", "39313F", "735A79"),
    "national_day": ("FFFAF3", "292322", "9E302B"),
    "double_ninth": ("FBF3E7", "3A3026", "84502B"),
    "winter_solstice": ("FFFFFF", "222222", "222222"),
    "new_year": ("182D48", "EDF2F5", "F1D69D"),
    "new_years_eve": ("702923", "FFF0D8", "F1D095"),
    "new_years_day": ("B33328", "FFF5E4", "FFE4A3"),
    "lantern_festival": ("842B3F", "FFF0DC", "F5D393"),
}
THEME_VERSION = sha256(json.dumps(["festival-v1", "63c4a04e186caf3e0c9301321d0afd6fc6c987417128051a74107846e894ecf0", SCHEDULE, PALETTES], sort_keys=True).encode()).hexdigest()

@dataclass(frozen=True)
class ThemeWindow:
    theme_id: str
    valid_until: datetime | None

    @property
    def key(self) -> str:
        return self.theme_id + ":" + (self.valid_until.isoformat() if self.valid_until else "")


def theme_window(now: datetime, *, enabled: bool = True) -> ThemeWindow:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("theme clock must be timezone-aware")
    if not enabled:
        return ThemeWindow("default", None)
    for date, theme_id in SCHEDULE:
        start = datetime.fromisoformat(date).replace(tzinfo=CST)
        end = start + timedelta(days=1)
        if now < start:
            return ThemeWindow("default", start)
        if now < end:
            return ThemeWindow(theme_id, end)
    return ThemeWindow("default", None)
