"""Date parsing for sample collection and certificate issue dates (FR09, FR14).

Dates are never guessed. ISO forms are accepted at their stated precision (day, month or
year). Slash and dot forms are read day-first or month-first only when one part is over 12, which
settles the order for that value. Otherwise the date stays unknown and is flagged ambiguous.
Every parsed date is held as an inclusive interval, so a filter can tell "inside the range" apart
from "might be inside the range".
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date

DATE_RULES_VERSION = "dates-2026.11.0"

_ISO = re.compile(r"^(?P<y>\d{4})(?:-(?P<m>\d{1,2})(?:-(?P<d>\d{1,2}))?)?(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?)?$")
_SLASH = re.compile(r"^(?P<a>\d{1,2})[/.](?P<b>\d{1,2})[/.](?P<y>\d{4})$")
_MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_abbr) if m}
_MONTHS.update({m.lower(): i for i, m in enumerate(calendar.month_name) if m})
_NAMED = re.compile(r"^(?P<d>\d{1,2})[ -](?P<mon>[A-Za-z]{3,9})[ -](?P<y>\d{4})$")


@dataclass(frozen=True)
class ParsedDate:
    raw: str
    start: date  # inclusive
    end: date  # inclusive
    precision: str  # "day" | "month" | "year"
    warnings: tuple[str, ...] = ()

    @property
    def iso(self) -> str:
        return {"day": self.start.isoformat(), "month": self.start.isoformat()[:7], "year": str(self.start.year)}[self.precision]


class DateIssue(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _day(y: int, m: int, d: int, raw: str) -> date:
    try:
        return date(y, m, d)
    except ValueError:
        raise DateIssue("DATE_INVALID", f"'{raw}' is not a calendar date") from None


def parse_date(raw: object, today: date | None = None) -> ParsedDate | None:
    """None for an empty value; DateIssue when the text cannot be read without guessing."""
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    today = today or date.today()
    warnings: list[str] = []
    m = _ISO.match(s)
    if m:
        y = int(m["y"])
        if m["d"]:
            start = end = _day(y, int(m["m"]), int(m["d"]), s)
            precision = "day"
        elif m["m"]:
            mo = int(m["m"])
            if not 1 <= mo <= 12:
                raise DateIssue("DATE_INVALID", f"'{s}' has no month {mo}")
            start, end, precision = date(y, mo, 1), date(y, mo, calendar.monthrange(y, mo)[1]), "month"
        else:
            start, end, precision = date(y, 1, 1), date(y, 12, 31), "year"
    elif (m := _NAMED.match(s)) and m["mon"].lower() in _MONTHS:
        start = end = _day(int(m["y"]), _MONTHS[m["mon"].lower()], int(m["d"]), s)
        precision = "day"
    elif m := _SLASH.match(s):
        a, b, y = int(m["a"]), int(m["b"]), int(m["y"])
        if a > 12 and b <= 12:
            start = end = _day(y, b, a, s)
        elif b > 12 and a <= 12:
            start = end = _day(y, a, b, s)
            warnings.append("DATE_MONTH_FIRST")
        elif a == b:
            start = end = _day(y, a, a, s)
        else:
            raise DateIssue("DATE_AMBIGUOUS", f"'{s}' could be day-first or month-first; enter it as YYYY-MM-DD")
        precision = "day"
    else:
        raise DateIssue("DATE_UNREADABLE", f"'{s}' is not a recognised date; enter it as YYYY-MM-DD")
    if start.year < 1900:
        raise DateIssue("DATE_INVALID", f"'{s}' is before 1900")
    if start > today:
        warnings.append("DATE_IN_FUTURE")
    return ParsedDate(s, start, end, precision, tuple(warnings))


def interval(iso: str | None) -> tuple[date, date] | None:
    """Inclusive interval for a stored normalised date (YYYY, YYYY-MM or YYYY-MM-DD)."""
    if not iso:
        return None
    try:
        p = parse_date(iso, today=date.max)
    except DateIssue:
        return None
    return (p.start, p.end) if p else None
