"""
schema.py
=========
Single source of truth for the target spreadsheet ("ריצות" sheet) and for
turning the raw JSON that the vision model returns into a spreadsheet-ready row.

The column order here MUST match the header row of the master workbook exactly:

    A Date              -> "D.M.YY"          e.g. 4.8.26
    B Time              -> "HH:MM-HH:MM"      e.g. 05:17-06:17
    C Duration          -> "H:MM:SS"          e.g. 1:00:04
    D Distance[Km]      -> float              e.g. 8.65
    E Avg. Heart Rate   -> int                e.g. 160
    F Avg. Pace         -> "mm:ss" (min/km)   e.g. 6:56
    G Elevation Gain[m] -> int, or "X"        e.g. 52 / X
    H Avg. Power[W]     -> int                e.g. 151
    I Avg. Cadence[SPM] -> int                e.g. 161
    J Temperature[C]    -> int                e.g. 25

The vision model is asked to return null for anything it cannot see. The
normaliser below decides what to do with a missing value per column
(elevation becomes "X", everything else raises so nothing silently lands wrong).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date


# The exact header text of the target sheet, in column order (A..J).
HEADERS = [
    "Date ",
    "Time",
    "Duration[HH:MM:SS]",
    "Distance[Km]",
    "Avg. Heart Rate",
    "Avg. Pace[Km/h]",
    "Elevation Gain[m]",
    "Avg. Power[W]",
    "Avg. Cadence[SPM]",
    "Temeture[C]",
]

TARGET_SHEET_NAME = "ריצות"

# Canonical field order used by the JSON extraction, the CSV log, and the CLI.
# Matches HEADERS (A..J) one-to-one.
CSV_FIELDS = [
    "date",
    "time_range",
    "duration",
    "distance_km",
    "avg_heart_rate",
    "avg_pace",
    "elevation_gain",
    "avg_power",
    "avg_cadence",
    "temperature",
]

# Value used when a numeric field cannot be found on the screenshots. The user
# already uses this convention in the master file (e.g. Elevation Gain).
MISSING = "X"


class ExtractionError(ValueError):
    """Raised when the model output cannot be turned into a valid row."""


@dataclass
class RunRecord:
    """Normalised, spreadsheet-ready representation of one workout."""

    date: str            # A  "D.M.YY"
    time_range: str      # B  "HH:MM-HH:MM"
    duration: str        # C  "H:MM:SS"
    distance_km: float   # D
    avg_heart_rate: int  # E
    avg_pace: str        # F  "mm:ss"
    elevation_gain: object  # G  int or "X"
    avg_power: int       # H
    avg_cadence: int     # I
    temperature: int     # J

    def as_row(self) -> list:
        """Return the values in column order, ready to write to A..J."""
        return [
            self.date,
            self.time_range,
            self.duration,
            self.distance_km,
            self.avg_heart_rate,
            self.avg_pace,
            self.elevation_gain,
            self.avg_power,
            self.avg_cadence,
            self.temperature,
        ]


# ---------------------------------------------------------------------------
# Helpers for parsing the loose strings that come off an Apple Fitness screen
# ---------------------------------------------------------------------------

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def parse_date(raw: str, default_year: int) -> str:
    """
    "Tue, 4 Aug"            -> "4.8.26"   (year taken from default_year)
    "4 Aug 2026"           -> "4.8.26"
    "04/08/2026"           -> "4.8.26"
    Apple's summary header usually shows no year, so default_year fills it in.
    """
    if raw is None:
        raise ExtractionError("date is missing")
    s = str(raw).strip()

    # Try "<day> <MonthName> [year]"
    m = re.search(r"(\d{1,2})\s+([A-Za-z]{3,})\.?\s*(\d{4})?", s)
    if m:
        day = int(m.group(1))
        mon = _MONTHS.get(m.group(2)[:3].lower())
        if mon is None:
            raise ExtractionError(f"unrecognised month in date: {raw!r}")
        year = int(m.group(3)) if m.group(3) else default_year
        return f"{day}.{mon}.{year % 100}"

    # Try numeric "D/M/Y" or "D.M.Y"
    m = re.search(r"(\d{1,2})[./](\d{1,2})[./](\d{2,4})", s)
    if m:
        day, mon, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
        return f"{day}.{mon}.{year % 100}"

    raise ExtractionError(f"could not parse date: {raw!r}")


def parse_time_range(raw: str) -> str:
    """ "5:17-6:17" / "5:17–6:17" / "05:17 - 06:17" -> "05:17-06:17" """
    if raw is None:
        raise ExtractionError("time range is missing")
    parts = re.findall(r"(\d{1,2}):(\d{2})", str(raw))
    if len(parts) < 2:
        raise ExtractionError(f"could not parse time range: {raw!r}")
    (h1, m1), (h2, m2) = parts[0], parts[1]
    return f"{int(h1):02d}:{m1}-{int(h2):02d}:{m2}"


def parse_duration(raw: str) -> str:
    """ "1:00:04" -> "1:00:04";  "30:07" -> "0:30:07" """
    if raw is None:
        raise ExtractionError("duration is missing")
    nums = re.findall(r"\d+", str(raw))
    if len(nums) == 3:
        h, m, s = (int(x) for x in nums)
    elif len(nums) == 2:
        h, m, s = 0, int(nums[0]), int(nums[1])
    else:
        raise ExtractionError(f"could not parse duration: {raw!r}")
    return f"{h}:{m:02d}:{s:02d}"


def parse_pace(raw: str) -> str:
    """ "6'56''/KM" / "6:56" / "6'56\"" -> "6:56" (min/km) """
    if raw is None:
        raise ExtractionError("pace is missing")
    nums = re.findall(r"\d+", str(raw))
    if len(nums) < 2:
        raise ExtractionError(f"could not parse pace: {raw!r}")
    return f"{int(nums[0])}:{int(nums[1]):02d}"


def parse_float(raw, name: str) -> float:
    if raw is None:
        raise ExtractionError(f"{name} is missing")
    m = re.search(r"\d+(?:\.\d+)?", str(raw))
    if not m:
        raise ExtractionError(f"could not parse {name}: {raw!r}")
    return float(m.group(0))


def parse_int(raw, name: str) -> int:
    if raw is None:
        raise ExtractionError(f"{name} is missing")
    m = re.search(r"-?\d+", str(raw))
    if not m:
        raise ExtractionError(f"could not parse {name}: {raw!r}")
    return int(m.group(0))


def parse_int_or_missing(raw) -> object:
    """Elevation & temperature style fields: return int, or MISSING ('X')."""
    if raw is None:
        return MISSING
    m = re.search(r"-?\d+", str(raw))
    return int(m.group(0)) if m else MISSING


def normalise(data: dict, default_year: int | None = None) -> RunRecord:
    """
    Turn the raw JSON dict from the vision model into a validated RunRecord.

    Expected keys (any may be null if not visible on the screenshots):
        date, time_range, duration, distance_km, avg_heart_rate,
        avg_pace, elevation_gain, avg_power, avg_cadence, temperature
    """
    if default_year is None:
        default_year = date.today().year

    return RunRecord(
        date=parse_date(data.get("date"), default_year),
        time_range=parse_time_range(data.get("time_range")),
        duration=parse_duration(data.get("duration")),
        distance_km=parse_float(data.get("distance_km"), "distance"),
        avg_heart_rate=parse_int(data.get("avg_heart_rate"), "avg_heart_rate"),
        avg_pace=parse_pace(data.get("avg_pace")),
        # Elevation is commonly not on the screenshot -> keep the user's "X".
        elevation_gain=parse_int_or_missing(data.get("elevation_gain")),
        avg_power=parse_int(data.get("avg_power"), "avg_power"),
        avg_cadence=parse_int(data.get("avg_cadence"), "avg_cadence"),
        temperature=parse_int(data.get("temperature"), "temperature"),
    )
