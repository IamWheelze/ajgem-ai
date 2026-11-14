"""Qualified (censored) assay values and three-valued threshold evaluation.

``<0.01`` is stored as comparator LT with bound 0.01 and *no* asserted exact
value. Comparing it with a threshold yields YES, NO or INDETERMINATE — never a
guess (FRD §6).
"""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation


class Comparator(str, enum.Enum):
    EQ = "EQ"
    LT = "LT"
    LE = "LE"
    GT = "GT"
    GE = "GE"


class Outcome(str, enum.Enum):
    YES = "YES"
    NO = "NO"
    INDETERMINATE = "INDETERMINATE"


class ParseIssue(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class QualifiedValue:
    raw: str
    comparator: Comparator
    value: Decimal
    warnings: tuple[str, ...] = ()

    @property
    def censored(self) -> bool:
        return self.comparator is not Comparator.EQ

    def scaled(self, factor: Decimal) -> QualifiedValue:
        return QualifiedValue(self.raw, self.comparator, self.value * factor, self.warnings)


_PREFIX = {
    "<=": Comparator.LE,
    "≤": Comparator.LE,
    ">=": Comparator.GE,
    "≥": Comparator.GE,
    "<": Comparator.LT,
    "&lt;": Comparator.LT,
    ">": Comparator.GT,
    "&gt;": Comparator.GT,
}
_THOUSANDS = re.compile(r"^\d{1,3}(,\d{3})+(\.\d+)?$")
_PLAIN = re.compile(r"^\d+(\.\d+)?$|^\.\d+$")
_BELOW_DL_WORDS = {"bdl", "nd", "n.d.", "bd", "<dl", "<lod", "lod"}


def parse_result(raw: str) -> QualifiedValue:
    """Parse an assay result string. Raises ParseIssue for anything not safely parseable."""
    if raw is None or not str(raw).strip():
        raise ParseIssue("RESULT_MISSING", "Result is empty")
    text = str(raw).strip()
    lower = text.lower()
    if lower in _BELOW_DL_WORDS:
        raise ParseIssue(
            "RESULT_BELOW_DL_NO_LIMIT",
            f"'{text}' indicates a below-detection result but gives no limit; supply the detection limit",
        )
    comparator = Comparator.EQ
    body = text
    for prefix in sorted(_PREFIX, key=len, reverse=True):
        if lower.startswith(prefix):
            comparator = _PREFIX[prefix]
            body = text[len(prefix) :].strip()
            break
    warnings: list[str] = []
    if body.startswith("-"):
        raise ParseIssue(
            "RESULT_NEGATIVE_CONVENTION",
            f"'{text}' is negative; laboratories use negative values for several below-limit conventions",
        )
    if _THOUSANDS.match(body):
        warnings.append("THOUSANDS_SEPARATOR_REMOVED")
        body = body.replace(",", "")
    elif "," in body:
        raise ParseIssue("RESULT_DECIMAL_COMMA_AMBIGUOUS", f"'{text}' uses a comma that may be a decimal separator")
    if not _PLAIN.match(body):
        raise ParseIssue("RESULT_UNPARSEABLE", f"'{text}' is not a recognised numeric result")
    try:
        value = Decimal(body)
    except InvalidOperation as exc:  # pragma: no cover - guarded by regex
        raise ParseIssue("RESULT_UNPARSEABLE", str(exc)) from exc
    return QualifiedValue(text, comparator, value, tuple(warnings))


@dataclass(frozen=True)
class _Interval:
    lo: Decimal
    lo_closed: bool
    hi: Decimal | None  # None means unbounded above
    hi_closed: bool


def _interval(q: QualifiedValue) -> _Interval:
    zero = Decimal(0)
    match q.comparator:
        case Comparator.EQ:
            return _Interval(q.value, True, q.value, True)
        case Comparator.LT:
            return _Interval(zero, True, q.value, False)
        case Comparator.LE:
            return _Interval(zero, True, q.value, True)
        case Comparator.GT:
            return _Interval(q.value, False, None, False)
        case Comparator.GE:
            return _Interval(q.value, True, None, False)
    raise AssertionError(q.comparator)


QUERY_OPS = {">", ">=", "<", "<="}


def evaluate(q: QualifiedValue, op: str, threshold: Decimal) -> Outcome:
    """Evaluate ``true_value <op> threshold`` for a possibly censored value (same unit)."""
    if op not in QUERY_OPS:
        raise ValueError(f"Unsupported operator {op!r}")
    iv = _interval(q)
    t = threshold
    if op == ">":
        all_ = iv.lo > t or (iv.lo == t and not iv.lo_closed)
        none = iv.hi is not None and iv.hi <= t
    elif op == ">=":
        all_ = iv.lo >= t
        none = iv.hi is not None and (iv.hi < t or (iv.hi == t and not iv.hi_closed))
    elif op == "<":
        all_ = iv.hi is not None and (iv.hi < t or (iv.hi == t and not iv.hi_closed))
        none = iv.lo >= t
    else:  # "<="
        all_ = iv.hi is not None and iv.hi <= t
        none = iv.lo > t or (iv.lo == t and not iv.lo_closed)
    if all_:
        return Outcome.YES
    if none:
        return Outcome.NO
    return Outcome.INDETERMINATE
